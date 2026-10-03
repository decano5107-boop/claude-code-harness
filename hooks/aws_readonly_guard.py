#!/usr/bin/env python3
"""
aws_readonly_guard.py: PreToolUse hook that keeps the AWS CLI read-only.

Wire it on the Bash tool and on any AWS MCP tool whose name ends in `call_aws`
(its command arrives in `tool_input.cli_command`). Every AWS CLI invocation the
tool call would run is classified:

  read     `describe-*`, `get-*`, `list-*`, `s3 ls`, ...  -> no output at all
  secret   `secretsmanager get-secret-value`, `ssm get-parameter`, `kms decrypt`,
           credential `get-*` calls (`sso get-role-credentials`,
           `ec2 get-password-data`, ...), S3 downloads, anything with
           `--with-decryption`                             -> "ask"
  other    everything else                                 -> "deny"

Why an allowlist and not a blocklist: AWS has hundreds of services and adds
mutating operations every month. A blocklist of "dangerous verbs" is out of date
the day it is written; a list of read verbs is short, stable, and anything it does
not recognise is refused. The cost of a false "deny" is one sentence from the
user; the cost of a false "allow" can be a deleted bucket.

Why reads produce no output instead of "allow": a Bash line can mix AWS reads with
anything else (`aws s3 ls && rm -rf build`). Emitting "allow" would auto-approve
the whole line, including the parts this hook never judged. Staying silent hands
the line back to the normal permission flow. This harness never emits "allow".

Why this hook fails CLOSED while the rest of the harness fails open: once a
command is known to invoke the AWS CLI, "I could not work out what it does" must
mean "no". Input that does not mention AWS at all is never touched, so a parser
bug here costs a refused AWS call, never an unrelated tool call.

Scope and limits: this is a guardrail for an assistant's tool calls, not a
sandbox. It reads shell text; it cannot see variable indirection
(`A=aws; $A ...`, `$(echo aws) ...`), scripts on disk, remote shells
(`ssh host '...'`), the CLI inside a container (`docker run amazon/aws-cli ...`),
other AWS clients (SDK code in `python -c` / `node -e`, `curl --aws-sigv4`,
`sam`, `s5cmd`, Terraform), MCP tools other than `*call_aws`, or AWS CLI aliases
defined in ~/.aws/cli/alias. The real boundary is the IAM policy of the
credentials in use; this hook is the layer that stops the assistant from asking
IAM in the first place.

Configuration ("aws_guard" section):
  allow_extra             ["service operation", ...] treated as reads  ([])
  ask_extra               ["service operation", ...] always ask, wins over
                          allow_extra and every built-in rule          ([])
  deny_message_hint       text appended to every deny reason
                          ("Ask the user to run it themselves if it is intended.")
  allow_model_invocation  let bedrock-runtime invoke-model / converse through
                                                                       (true)
"""
from __future__ import annotations

import os
import re
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import log, permission, read_event, run, setting  # noqa: E402

SECTION = "aws_guard"

# ── classification tables ────────────────────────────────────────────────────

# Verb prefixes that are read-only across AWS services. The trailing hyphen
# matters: `get-` matches `get-object` but not a hypothetical `getaway`.
READ_PREFIXES = (
    "describe-", "get-", "list-", "search-", "lookup-", "batch-get-", "head-",
    "select-", "estimate-", "preview-", "summarize-", "count-",
)
# Bare read verbs, matched exactly (DynamoDB `scan`/`query`, CloudSearch `search`).
READ_VERBS = {"scan", "query", "select", "search"}
# Reads whose verb does not reveal it. Matched exactly, on any service: note that
# `start-query` (CloudWatch Logs Insights) is here, while Athena's
# `start-query-execution`, which can run DDL and DML, deliberately is not.
READ_EXACT = {
    "filter-log-events", "start-query", "stop-query", "get-query-results",
    "simulate-principal-policy", "simulate-custom-policy",
}
READ_PAIRS = {
    ("s3", "ls"),
    ("logs", "tail"),
    ("sts", "get-caller-identity"),
    ("configure", "list"),
    ("configure", "list-profiles"),
    # Refreshes a local token cache through the browser; mutates no AWS resource.
    ("sso", "login"),
}
# Model invocation reads nothing and changes nothing in the account, but it costs
# money per call, so it is a switch rather than a hard-coded read.
MODEL_PAIRS = {
    ("bedrock-runtime", "invoke-model"),
    ("bedrock-runtime", "invoke-model-with-response-stream"),
    ("bedrock-runtime", "converse"),
    ("bedrock-runtime", "converse-stream"),
}
# Reads that return secrets or credentials, or copy data out of the account. They
# mutate nothing, so denying them would push the user to work around the guard;
# they are sensitive, so each one is confirmed by a human.
ASK_PAIRS = {
    ("secretsmanager", "get-secret-value"),
    ("secretsmanager", "batch-get-secret-value"),
    ("ssm", "get-parameter"),
    ("ssm", "get-parameters"),
    ("ssm", "get-parameters-by-path"),
    ("kms", "decrypt"),
    ("sts", "get-session-token"),
    ("sts", "get-federation-token"),
    ("ecr", "get-login-password"),
    ("ecr", "get-authorization-token"),
    ("ecr-public", "get-login-password"),
    ("ecr-public", "get-authorization-token"),
    ("codeartifact", "get-authorization-token"),
    ("eks", "get-token"),
    ("rds", "generate-db-auth-token"),
    ("redshift", "get-cluster-credentials"),
    ("redshift", "get-cluster-credentials-with-iam"),
    ("configure", "export-credentials"),
    # Credentials behind a read-looking `get-` verb.
    ("sso", "get-role-credentials"),
    ("cognito-identity", "get-credentials-for-identity"),
    ("lightsail", "get-instance-access-details"),
    ("ec2", "get-password-data"),
    # Downloads: the object lands on the local disk (see also `s3 cp` below).
    ("s3api", "get-object"),
    ("s3api", "get-object-torrent"),
    ("s3api", "select-object-content"),
    # A presigned URL is a bearer credential for the object.
    ("s3", "presign"),
}
# Any command carrying this flag returns decrypted secret material.
SECRET_FLAGS = {"--with-decryption"}

# The complete set of AWS CLI global options. The ones taking a value must be
# skipped together with their value, or `aws --profile prod s3 ls` would read
# "prod" as the service. An option not in either set, before the operation,
# makes the command unparseable (and therefore denied): guessing whether it
# takes a value is exactly the ambiguity an attacker would use.
GLOBAL_VALUE_FLAGS = {
    "--profile", "--region", "--output", "--query", "--endpoint-url", "--color",
    "--ca-bundle", "--cli-read-timeout", "--cli-connect-timeout",
    "--cli-binary-format",
}
GLOBAL_BOOL_FLAGS = {
    "--debug", "--no-verify-ssl", "--no-paginate", "--no-sign-request",
    "--no-cli-pager", "--cli-auto-prompt", "--no-cli-auto-prompt",
}

# ── finding AWS invocations in shell text ───────────────────────────────────

# "Does this text mention the aws binary at all?" Deliberately loose: it decides
# only whether the hook looks further, never what the verdict is.
AWS_WORD = re.compile(r"(?<![\w.-])aws(?:cli)?(?![\w-])", re.IGNORECASE)

# Shell grammar words that precede a command without being one:
# `if aws ...`, `then aws ...`, `do aws ...`, `{ aws ...; }`, `! aws ...`.
SHELL_KEYWORDS = {"if", "then", "elif", "else", "do", "while", "until", "!", "{", "}",
                  "coproc", "fi", "done", "esac"}
# Commands known to treat their arguments as data. Any OTHER command that carries
# the AWS CLI as an argument may run it (`flock l aws ...`, `uv run aws ...`,
# `su -c 'aws ...'`), so it is judged instead of ignored.
DATA_COMMANDS = {"echo", "printf", "grep", "egrep", "fgrep", "rg", "ag", "ack", "cat",
                 "less", "more", "head", "tail", "man", "which", "type", "whereis",
                 "whatis", "apropos", "file", "stat", "ls", "alias", "unalias", "hash",
                 "complete", "compgen", "export", "unset", "declare", "local", "test",
                 "[", "[[", "git", "brew", "pip", "pip3", "npm", "case", "for", "in",
                 "cd", "pushd", "popd", "mkdir", "rmdir", "touch", "cp", "mv", "ln",
                 "rm", "find", "tree", "du", "wc", "sort", "uniq", "jq", "diff", "vim",
                 "vi", "nano", "code", "open", "tar", "zip", "unzip"}
PYTHONS = re.compile(r"^python(\d(\.\d+)?)?$")

# Commands that run the command that follows them. Values are the options that
# consume an argument, so `sudo -u admin aws ...` skips "admin" too.
WRAPPERS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-U", "-r", "-t", "-D", "-R", "-T"},
    "doas": {"-u", "-C"},
    "env": {"-u", "-C", "--unset", "--chdir"},
    "command": set(), "exec": {"-a"}, "builtin": set(), "nohup": set(),
    "time": {"-f", "-o"}, "nice": {"-n"}, "ionice": {"-c", "-n", "-p"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "stdbuf": {"-i", "-o", "-e"}, "chronic": set(), "unbuffer": set(),
    "caffeinate": {"-t", "-w"}, "watch": {"-n", "-d"},
    "xargs": {"-I", "-n", "-P", "-L", "-s", "-d", "-E", "-a", "--max-args",
              "--max-procs", "--replace", "--delimiter", "--arg-file"},
}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
CREDENTIAL_RUNNERS = {"aws-vault", "saml2aws", "aws2-wrap", "aws-sso"}
MAX_DEPTH = 4


class Unparseable(Exception):
    """Raised when text that invokes AWS cannot be taken apart reliably."""


def tokenize(text: str) -> list[str]:
    """Shell-like tokens with operators kept as their own tokens.

    Comments are NOT honoured on purpose: shlex would swallow the newline that
    ends a comment, gluing the next command onto the current one and letting a
    write hide behind a read (`aws s3 ls # x<newline>aws s3 rm ...`).
    """
    text = text.replace("\\\n", " ")
    lexer = shlex.shlex(text, posix=True, punctuation_chars="();<>|&\n")
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError as e:
        raise Unparseable(str(e)) from e


def split_segments(tokens: list[str]) -> list[list[str]]:
    """Cut a token stream into simple commands, dropping redirection targets."""
    segments, current, skip_next = [], [], False
    for tok in tokens:
        if skip_next:
            skip_next = False
            continue
        if tok and all(c in "();<>|&\n" for c in tok):
            if tok[0] in "<>" and "(" not in tok:
                skip_next = True  # `> file`, `2>&1`, `<<EOF`: the next word is not a command
                continue
            segments.append(current)  # `;`, `&&`, `|`, `(`, `<(`, newline, ...
            current = []
            continue
        current.append(tok)
    segments.append(current)
    return [s for s in segments if s]


def substitutions(text: str) -> list[str]:
    """Bodies of `$(...)` and backtick substitutions, wherever they appear.

    The tokenizer keeps double-quoted strings whole, so `echo "$(aws s3 rm x)"`
    would otherwise hide the inner command. Single-quoted text is extracted too;
    that can only over-report, which is the safe direction here.
    """
    bodies = re.findall(r"`([^`]*)`", text)
    i = text.find("$(")
    while i != -1:
        depth, j = 1, i + 2
        while j < len(text) and depth:
            depth += {"(": 1, ")": -1}.get(text[j], 0)
            j += 1
        bodies.append(text[i + 2:j - 1] if depth == 0 else text[i + 2:])
        i = text.find("$(", i + 2)
    return bodies


ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def is_aws(token: str) -> bool:
    # Case-insensitive: on a case-insensitive filesystem `AWS` runs the same binary.
    return os.path.basename(token).lower() == "aws"


def unwrap(segment: list[str]) -> tuple[str, list[str]] | None:
    """Return ("aws", args) or ("shell", [code]) for one simple command, else None.

    Only the command position counts, so `grep aws notes.txt` is not an AWS call,
    while `sudo -u ops env X=1 /usr/local/bin/aws s3 rm ...` is.
    """
    i, n = 0, len(segment)
    via_xargs = False
    while i < n:
        tok = segment[i]
        name = os.path.basename(tok)
        if ENV_ASSIGNMENT.match(tok):  # FOO=1 aws ...
            i += 1
            continue
        if tok in SHELL_KEYWORDS:
            i += 1
            continue
        if is_aws(tok):
            # `xargs aws` gets its service and operation from stdin: unknowable here.
            return ("aws_stdin" if via_xargs else "aws"), segment[i + 1:]
        if PYTHONS.match(name.lower()):
            rest = segment[i + 1:]
            for k, t in enumerate(rest[:-1]):
                if t == "-m" and rest[k + 1].lower() == "awscli":  # python -m awscli == aws
                    return "aws", rest[k + 2:]
        if i > 0 and any(c in tok for c in " \t\n"):
            # A whole command line as one word after a wrapper is run by a shell:
            # `watch 'aws ...'`, `env -S 'aws ...'`.
            return "shell", [tok]
        if name in WRAPPERS:
            value_flags = WRAPPERS[name]
            via_xargs = via_xargs or name == "xargs"
            i += 1
            while i < n:
                t = segment[i]
                if t == "--":
                    i += 1
                    break
                if t in value_flags:
                    i += 2
                elif t.startswith("-") or ENV_ASSIGNMENT.match(t) \
                        or re.match(r"^\d+(\.\d+)?[smhd]?$", t):
                    i += 1  # an option, an env assignment, or a timeout duration
                else:
                    break
            continue
        rest = segment[i + 1:]
        if name == "eval":
            return "shell", [" ".join(rest)]
        if name in SHELLS:
            for k, t in enumerate(rest):
                if re.match(r"^-[a-zA-Z]*c[a-zA-Z]*$", t) and k + 1 < len(rest):
                    return "shell", [rest[k + 1]]
            if not any(not t.startswith("-") for t in rest):
                # No -c and no script file: the shell reads its commands from
                # stdin (`echo 'aws ...' | bash`, `bash -s < x`).
                return "stdin_shell", []
            return None
        # Credential helpers exist to run the AWS CLI with borrowed credentials
        # (`aws-vault exec prod -- aws ...`), so any `aws` among their arguments
        # is the command they will run.
        if name in CREDENTIAL_RUNNERS:
            for k, t in enumerate(rest):
                if is_aws(t):
                    return "aws", rest[k + 1:]
            return None
        # `find ... -exec aws ... \;` runs its argument as a command.
        for k, t in enumerate(rest):
            if t in ("-exec", "-execdir", "-ok", "-okdir"):
                return unwrap(rest[k + 1:])
        # Generic runners hand everything after `--` to another program.
        if "--" in rest:
            after = rest[rest.index("--") + 1:]
            found = unwrap(after) if after else None
            if found:
                return found
        # Fail closed: an unknown program that carries the AWS CLI as an argument
        # (`flock l aws ...`, `uv run aws ...`, `parallel aws ...`, `su -c 'aws ...'`)
        # may well run it.
        if name.lower() not in DATA_COMMANDS:
            for t in rest:
                if is_aws(t) or t.lower() == "awscli":
                    return "opaque", [name]
                if any(c in t for c in " \t\n") and AWS_WORD.search(t):
                    return "shell", [t]
        return None
    return None


# ── classifying one invocation ───────────────────────────────────────────────

def _pairs(key: str) -> set[tuple[str, str]]:
    out = set()
    for item in setting(SECTION, key, []):
        parts = str(item).lower().split()
        if len(parts) == 2:
            out.add((parts[0], parts[1]))
    return out


def classify(args: list[str]) -> tuple[str, str]:
    """(verdict, reason) for the arguments that follow `aws`.

    verdict is "read", "ask" or "deny".
    """
    positional: list[str] = []
    i = 0
    while i < len(args) and len(positional) < 2:
        tok = args[i]
        flag = tok.split("=", 1)[0]
        # `help` counts only as the last word: `aws s3 help` and
        # `aws ec2 terminate-instances help` print documentation, while a `help`
        # somewhere in the middle is an argument whose effect is not obvious.
        if (tok == "help" and i == len(args) - 1) or args == ["--version"]:
            return "read", "help or version"
        if tok.startswith("-"):
            if flag in GLOBAL_VALUE_FLAGS:
                i += 1 if "=" in tok else 2
                continue
            if flag in GLOBAL_BOOL_FLAGS:
                i += 1
                continue
            raise Unparseable(f"unrecognised option {tok!r} before the operation")
        positional.append(tok)
        i += 1
    rest = args[i:]

    if not positional:
        return "read", "bare `aws` prints usage"
    if len(positional) < 2:
        raise Unparseable(f"no operation after service {positional[0]!r}")
    service, op = positional[0].lower(), positional[1].lower()
    label = f"aws {service} {op}"
    if rest == ["help"]:
        return "read", "help"

    if (service, op) in _pairs("ask_extra"):
        return "ask", f"{label} is configured to require confirmation."
    if (service, op) in _pairs("allow_extra"):
        # An explicit entry is the user's decision and wins over every built-in rule.
        return "read", f"{label} is allowed by configuration."
    if (service, op) in ASK_PAIRS:
        return "ask", f"{label} returns secrets, credentials or data leaving the account; confirm it first."
    elif service == "s3" and op in ("cp", "sync"):
        return _s3_transfer(label, rest)
    elif service == "configure" and op == "get":
        key = next((t for t in rest if not t.startswith("-")), "").lower()
        if "secret" in key or "token" in key:
            return "ask", f"{label} {key} prints a credential; confirm it first."
        verdict, reason = "read", label
    elif (service, op) in MODEL_PAIRS:
        if not setting(SECTION, "allow_model_invocation", True):
            return "deny", f"{label}: model invocation is disabled by configuration."
        verdict, reason = "read", label
    elif ((service, op) in READ_PAIRS or op in READ_EXACT or op in READ_VERBS
          or op.startswith(READ_PREFIXES)):
        verdict, reason = "read", label
    else:
        return "deny", f"{label} is not a read-only operation."

    if any(t.split("=", 1)[0] in SECRET_FLAGS for t in rest):
        return "ask", f"{label} with --with-decryption returns decrypted secrets; confirm it first."
    return verdict, reason


def _s3_transfer(label: str, rest: list[str]) -> tuple[str, str]:
    """`s3 cp` / `s3 sync`: a download asks, anything that writes to S3 is denied.

    Downloading copies account data onto the local disk, which is exactly what a
    user may want and exactly what they should see first, so it asks. Uploads and
    S3-to-S3 copies change the account and are denied. Positional detection is
    rough (an option value can be mistaken for a path); every such mistake lands
    on "deny", never on a silent pass.
    """
    paths = [t for t in rest if t == "-" or not t.startswith("-")]  # "-" is stdin/stdout
    if len(paths) >= 2 and paths[0].startswith("s3://") \
            and not any(p.startswith("s3://") for p in paths[1:]):
        return "ask", f"{label} downloads {paths[0]} to {paths[1]}; confirm it first."
    return "deny", f"{label} writes to S3."


# ── judging a whole tool call ────────────────────────────────────────────────

RANK = {"read": 0, "ask": 1, "deny": 2}


def judge_text(text: str, depth: int = 0) -> list[tuple[str, str]]:
    """Verdicts for every AWS invocation in a piece of shell text."""
    if not AWS_WORD.search(text):
        # Quoting can hide the word from a regex (`a''ws`, `a\ws`) but not from
        # the tokenizer. Text that does not tokenize and does not mention AWS is
        # not this hook's business.
        try:
            if not any(is_aws(t) for t in tokenize(text)):
                return []
        except Unparseable:
            return []
    if depth > MAX_DEPTH:
        return [("deny", "AWS command nested too deeply to inspect.")]
    verdicts: list[tuple[str, str]] = []
    for body in substitutions(text):
        verdicts += judge_text(body, depth + 1)
    try:
        segments = split_segments(tokenize(text))
    except Unparseable as e:
        return verdicts + [("deny", f"Could not parse a command that mentions AWS ({e}).")]
    for segment in segments:
        found = unwrap(segment)
        if not found:
            continue
        kind, payload = found
        if kind == "shell":
            verdicts += judge_text(payload[0], depth + 1)
            continue
        if kind == "stdin_shell":
            verdicts.append(("deny", "A shell reading commands from stdin in a line that "
                                     "mentions AWS: what it runs cannot be inspected."))
            continue
        if kind == "opaque":
            verdicts.append(("deny", f"`{payload[0]}` is handed the AWS CLI as an argument "
                                     "and may run it; it cannot be inspected."))
            continue
        try:
            if kind == "aws_stdin" and len([t for t in payload if not t.startswith("-")]) < 2:
                raise Unparseable("xargs supplies the service or operation from stdin")
            verdicts.append(classify(payload))
        except Unparseable as e:
            verdicts.append(("deny", f"Could not parse AWS command ({e})."))
    return verdicts


def main() -> int:
    event = read_event()
    tool = str(event.get("tool_name") or "")
    tool_input = event.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    is_mcp = tool.endswith("call_aws")
    if is_mcp:
        text = str(tool_input.get("cli_command") or "").strip()
    elif tool == "Bash":
        text = str(tool_input.get("command") or "")
    else:
        return 0

    try:
        verdicts = judge_text(text)
    except Exception as e:  # a bug in this file must not turn into a silent pass
        verdicts = [("deny", f"The AWS guard failed while inspecting the command ({e}).")] \
            if (is_mcp or AWS_WORD.search(text)) else []
    if is_mcp and not verdicts:
        # The tool exists only to call AWS: an input we cannot read is refused.
        verdicts = [("deny", "Could not find an AWS CLI command in the tool input.")]

    if not verdicts:
        return 0
    verdict, reason = max(verdicts, key=lambda v: RANK[v[0]])
    if verdict == "read":
        return 0  # silence, never "allow": see the module docstring
    if verdict == "deny":
        hint = setting(SECTION, "deny_message_hint",
                       "Ask the user to run it themselves if it is intended.")
        reason = f"AWS access is read-only here. {reason} {hint}".strip()
    log("aws_guard", {"tool": tool, "decision": verdict, "reason": reason,
                      "session": event.get("session_id")})
    permission(verdict, reason)
    return 0


if __name__ == "__main__":
    run(main)
