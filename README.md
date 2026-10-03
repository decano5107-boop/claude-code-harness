# Claude Code Harness

[![tests](https://github.com/decano5107-boop/claude-code-harness/actions/workflows/tests.yml/badge.svg)](https://github.com/decano5107-boop/claude-code-harness/actions/workflows/tests.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

**For people who use Claude Code to produce work someone else will read or run, and who have rules for that work — write a spec, get it reviewed, don't run destructive commands — that the model follows only when it remembers them.**

The usual fix is a stricter instruction or asking the model to check its own work. Both rely on the same model and the same memory that skipped the rule. This repository moves the checks out of the model: a spec with measurable criteria written before the work, a separate critic agent that did not write the deliverable and tries to refute each criterion, and Claude Code hooks — code that runs at fixed points of a session — that send the model back to work, up to three times a turn, while a deliverable written in the turn has no critic run recorded after it was written, and ask or deny before the irreversible commands they recognise run.

It ships as one Claude Code plugin: a skill, a command, an agent and nine hook scripts. Python 3.11+ (standard library only) and Node 20+.

## How the pieces fit

| Layer | Ships as | What it does |
|---|---|---|
| **Spec** | skill `spec` | Separates the literal ask from the decision it serves, cuts the work to the smallest shippable slice, asks only the questions whose answers would change the spec, compares 2-3 approaches, and ends with measurable success criteria, each tied to the check that decides it. |
| **Verifier** | command `/verify-spec`, agent `spec-critic` | Runs the mechanical checks, then gives each judgment criterion to the `spec-critic` agent, which is instructed to try to make it fail and to return PASS only when it cannot, FAIL or UNVERIFIABLE otherwise. This is a separate refutation attempt by a model that did not write the work; how often it is right has not been measured. |
| **Gates** | hooks | Enforce what must not be skipped (below). |

The method, and the published ideas it draws on, are in [skills/spec/reference/method.md](skills/spec/reference/method.md); the reasoning behind the main design choices is in [docs/design.md](docs/design.md).

## The hooks

| Hook | Event | What it does | Effect |
|---|---|---|---|
| `exit_gate.py` | `Stop` | Sends the model back to work while a deliverable written this turn has no critic subagent run recorded after it was written, or no BRIEF marker. It checks that a critic ran, not what it concluded. It keeps blocking until both are in place or it has blocked 3 times in the turn (`max_blocks`); a prompt that asks to skip the critic ("skip critic", not "do not skip the critic") waives the critic check only. Long answers get a nudge at the next prompt instead. | **blocks** |
| `guardrails.mjs` | `PreToolUse` | Judges each Bash command by its structure — wrappers, quotes, chains, `bash -c` and shell keywords resolved — and adds friction where a mistake is irreversible: recursive and root-level deletes, force-pushes to protected branches, history rewrites, staging secrets or the whole tree, reading or sending `.env` files. Project rules come from `guardrails.config.json`. It only asks or denies. | **denies / asks** |
| `aws_readonly_guard.py` | `PreToolUse` | Default-deny for the AWS CLI, in Bash commands and in MCP tools whose name ends in `call_aws` (the AWS API MCP server): known reads pass silently, secret reads and downloads ask, every other call it finds is denied, through wrappers, shell keywords, `bash -c` and `python -m awscli` included. | **denies / asks** |
| `alias_contract.py` | `UserPromptSubmit` | Resolves nicknames like "the warehouse" against your `~/.claude/ALIASES.md` contract (fixed, volatile, rejected) before the model acts. In prompts that look like data work, it also flags phrases shaped like "the X table/dashboard/account" that no contract names. A project's own `ALIASES.md` is read only if you opt in. | adds context |
| `prompt_start.py` | `UserPromptSubmit` | Opens the turn ledger, delivers the deferred length note, flags speech-to-text mishearings you list in a glossary (empty by default). Prompt logging is off by default. | adds context |
| `ledger_track.py` | `PostToolUse`, `SubagentStop` | Records deliverables as they are written and critics as they finish, so the exit gate works from facts instead of the transcript. | records |
| `audience_check.py` | `PostToolUse` | Warns when a deliverable reads as written for the person in the chat rather than its reader: references to the conversation, process scaffolding, the author's own name, words of the chat language leaking into an English document (the word list is configurable; Spanish is the default example). | warns |
| `deliverable_scrub.py` | `PostToolUse` | Strips invisible Unicode (zero-width, bidi controls, tag characters that can hide instructions) from UTF-8 text files, and blanks five identity fields (creator, last modified by, company, manager, hyperlink base) in DOCX/XLSX/PPTX. | edits the file |
| `plugin_drift.py` | `SessionStart` | Warns when a local plugin you list in the config was edited but Claude Code is still loading its older installed copy. `--apply` copies changed and new files over the installed copy, never deletes, and needs a session restart. | warns |

`turn_ledger.py` is the per-session record the tracking hooks write and the exit gate reads;
`_common.py` holds the plumbing the Python hooks share. Every hook fails open — a bug never costs
you a turn — except `aws_readonly_guard.py`: an AWS CLI call it cannot parse, or an unknown program
handed the CLI as an argument, is denied, because there "no" is the safe answer.

## Install

Requirements: Claude Code, `git`, Python 3.11+ and Node 20+ on `PATH`.

```bash
git clone https://github.com/decano5107-boop/claude-code-harness.git
```

Then, inside Claude Code, add the clone as a plugin marketplace (use the absolute path of the
folder you just cloned) and install the plugin from it:

```
/plugin marketplace add /absolute/path/to/claude-code-harness
/plugin install claude-code-harness@claude-code-harness
```

Restart the session so the hooks load. Installed this way, the skill, command and agent carry the
plugin's name as a prefix: `/claude-code-harness:spec`, `/claude-code-harness:verify-spec`, and the
agent `claude-code-harness:spec-critic`, which the exit gate already counts as a critic.

Recommended: merge the `permissions.deny` entries from
[`examples/settings.example.json`](examples/settings.example.json) into `~/.claude/settings.json`.
They stop the Edit and Write tools from changing the gate's ledger and configuration at their
default paths, which a plugin cannot set for you (see Limits for what they do not stop).

## Use it

```
/claude-code-harness:spec  add rate limiting to the public API
# -> asks what it needs, proposes approaches, writes a spec with measurable criteria

# ...build...

/claude-code-harness:verify-spec
# -> mechanical checks + one critic run per judgment criterion -> PASS / FAIL / UNVERIFIABLE
```

The skill's description also lets the model reach for it on its own when a task is big or
ambiguous. Whether or not you use it, the exit gate applies: a turn that writes a deliverable
(by default a `.md`, `.markdown`, `.html`, `.htm`, `.pdf`, `.docx`, `.xlsx`, `.pptx`, `.csv`, `.txt`
or `.rst` file of 700 bytes or more, not named or placed like a working file) is sent back until a critic subagent has run after the
file was written and the file has a BRIEF. The BRIEF should be four lines — who the reader is and
what they already know, the thesis in one line, what must not appear, where the numbers come from.
The gate checks only that a marker exists: `BRIEF`, `Audience` or `Intended reader` followed by
`:` or `=` anywhere in the file's first 60 lines (an HTML comment keeps it invisible), or a line
starting `BRIEF` that names the file in the nearest `STATUS.md` above it. A compressed Office file
(`.docx`, `.xlsx`, `.pptx`) can only use `STATUS.md`.

## Configuration

Both files are optional; every hook has working defaults. Keep them outside the plugin folder,
which Claude Code replaces when the plugin updates.

- **`~/.claude/harness.config.json`**, for the Python hooks. Lookup order: `$HARNESS_CONFIG`, then
  `~/.claude/harness.config.json`, then `harness.config.json` at the plugin root; the first file
  that exists wins, and a malformed one stops the search and leaves every hook on its defaults.
  Start from [`examples/harness.config.example.json`](examples/harness.config.example.json). Each
  hook reads its own section, documented with its defaults at the end of the hook's docstring.
  Common changes: `exit_gate.require_critic` / `require_brief` to turn a check off;
  `deliverables.internal_names` / `internal_dirs` for what counts as a working file;
  `alias_contract.trust_project_contracts` to read a project's `ALIASES.md` (only for repositories
  you control: whatever it says is put into the model's context); `plugin_drift.sources` to watch
  your own plugins. Logs go to `harness.state_dir` (default `~/.claude/harness-state`) as JSON
  lines — the data for tuning thresholds; per-session state lives there too, as a JSON ledger and a
  `.note` file per session.
- **`guardrails.config.json`**, for project rules in the guardrail hook: the first of the path in
  `$GUARDRAILS_CONFIG`, the file in the session's working directory, or the one in the plugin's
  `hooks/` folder that parses. Start from
  [`examples/guardrails.config.example.json`](examples/guardrails.config.example.json). A rule's
  `when` is a regular expression over the whole command line, its project `match` a substring of
  the working directory, and its `decision` `ask` or `deny`; built-in rules always apply.
- **`~/.claude/ALIASES.md`**, for `alias_contract.py`: follow
  [`examples/ALIASES.example.md`](examples/ALIASES.example.md).

## Tests

```bash
python3 -m unittest discover -s tests
node --test tests/guardrails.test.mjs
```

Both suites run each hook as Claude Code runs it — a subprocess, JSON on stdin — against private
temporary state. The cases come from what each hook must prevent, crossed with the ways a command
can be spelled so a naive matcher misses it, including that each hook stays silent on events that
are not its business and fails open on malformed input. CI runs them on Linux (Python 3.11 to
3.13, Node 20 and 22) and macOS (Python 3.12, Node 20 and 22); Windows is untested.

## Limits

- These are gates for specific omissions, not a guarantee of correct output. The critic is a
  model too; the pattern checks (`audience_check.py`, the length count) are heuristics and only warn.
- The command guards read the command text before the shell runs it. Neither sees the results
  of variable expansion and command substitution (`A=aws; $A …`, `rm -rf $DIR`), encoded payloads,
  scripts on disk, text piped or fed into a shell, shell aliases and functions, git aliases, the
  AWS CLI's own aliases (`~/.aws/cli/alias`), `ssh host '…'`, code run by another interpreter
  (`python -c`, `node -e`, `perl -e`), or commands run inside a container (`docker run …`).
  `guardrails.mjs` strips stdin redirection before judging, so `some-upload-tool < .env` is not
  seen as reading `.env`.
- `guardrails.mjs` covers a listed set of programs. Output redirection that truncates a file
  (`: > file`), `mv`, `unlink`, `rsync --delete`, `cp /dev/null`, git's branch-pointer rewrites
  (`branch -f`, `switch -C`, `update-ref`) and cloud CLIs are not covered. It is a seatbelt
  against a careless command, not a sandbox; the permission system is the sandbox.
- `aws_readonly_guard.py` sees the AWS CLI only. Other AWS clients — SDK code, `curl --aws-sigv4`,
  `sam`, `s5cmd`, Terraform — and MCP tools other than `*call_aws` pass unexamined. IAM remains
  the real boundary.
- The exit gate works from its ledger and from file modification times under the session's
  working directory. What counts as a deliverable is a heuristic (extension, size, and not an
  internal name or folder such as `notes*`, `README*`, `_*`, `build/`, `tmp/`), so a deliverable
  saved under an internal name is not gated. Notebook edits are not tracked. The modification-time
  scan stops after 5000 files.
- Review is session memory: once a critic has run after a file was written, later edits to that
  file in the same session are not sent back for another review.
- The model can write files. The ledger and the config decide whether the gate blocks, so a model
  that rewrites them can open it. The example `permissions.deny` entries stop the Edit and Write
  tools; they do not stop a shell command that writes the same files. Keep Bash behind a
  permission prompt, or a sandbox, if that matters to you.
- `deliverable_scrub.py` skips text files that are not valid UTF-8, blanks only the five Office
  fields above, and does not clean image or PDF metadata; that needs tools outside the standard
  library.

## Attribution and license

The spec method draws on ideas Andrej Karpathy has published; this project is not affiliated with
or endorsed by him. Sources are in [skills/spec/reference/method.md](skills/spec/reference/method.md)
and [NOTICE](NOTICE).

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
