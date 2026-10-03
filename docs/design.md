# Design notes: advice versus gates

This repository has three parts: a spec skill that fixes measurable success criteria before the
work, a verifier that hands each judgment criterion to a critic agent that did not write the work,
and hooks that turn the rules around them into **gates**: code that stops an action, or the end of
a turn, until a check has passed. Rules kept as advice fade over a session; gates don't.

## The problem these hooks solve

A working Claude Code setup accumulates rules: *put the conclusion first*, *have an independent
critic* (a reviewer subagent that did not write the work and tries to refute it) *review any
deliverable* (a file a third party will read), *say who the reader is before writing for them*,
*don't guess which table "the warehouse" means*. Each rule gets written down — in `CLAUDE.md`, in a
skill, in a subagent that can be invoked.

And each rule works only when someone remembers it. A rule in the system prompt fades over a long
session. A skill fires only when its trigger words appear. A critic runs only when it is asked for.
If a rule is only advice, the user ends up invoking it by hand, or correcting output it would have
caught.

**The premise of this repository: a safeguard that is only advice is not a safeguard. It has to be
a gate.**

## What a gate is

Claude Code hooks run deterministic code at fixed points of a turn, and some of those points can
say no:

| Point | What a hook can do there | Used here for |
|---|---|---|
| `UserPromptSubmit` | Add context before the model sees the prompt | `prompt_start.py` starts the turn ledger; `alias_contract.py` resolves data-source nicknames |
| `PreToolUse` | Deny, or force a confirmation, before a tool runs | `guardrails.mjs` adds friction before irreversible shell commands; `aws_readonly_guard.py` keeps AWS CLI access read-only |
| `PostToolUse` | Add context after a tool ran | `deliverable_scrub.py` cleans a written file; `audience_check.py` flags a deliverable written for the wrong reader; `ledger_track.py` records deliverables |
| `SubagentStop` | Act when a subagent finishes | `ledger_track.py --critic` records that a critic ran |
| `Stop` | **Block the end of the turn** and send the model back to work | `exit_gate.py` requires a recorded critic run and a BRIEF marker before a deliverable ships, at most `max_blocks` times a turn |
| `SessionStart` | Add context when a session opens | `plugin_drift.py` warns that a local plugin's loaded copy is stale |

The `Stop` hook is the one that changes the economics. Advice says *"you should run the critic"*.
A `Stop` gate says *"you cannot finish this turn until a critic has run"*, up to a cap of blocks
per turn — and it is code, so it does not fade. It checks that the critic ran, not what it found.

## Decisions, and why

**Hooks record facts in a ledger instead of reading the transcript.** The `Stop` hook needs to
know what happened in the turn: was a deliverable written, did a critic run. The transcript file
is written asynchronously and can lag the live conversation at exactly the moment `Stop` fires.
So the hooks that see each event write it down in a per-session **ledger** (`ledger_track.py`,
through the `turn_ledger.py` module), and the gate, `exit_gate.py`, reads that record. The only
piece of turn content taken from the `Stop` input itself is `last_assistant_message`, the final
answer text, which is used only for the length nudge. Deterministic in, deterministic out.

**A critic's review survives the turn.** The ledger resets on every prompt, except for which
deliverables were already reviewed. Otherwise a one-word fix to a reviewed report would demand a
second full review, and the gate would teach the user to switch it off.

**Length is a nudge, not a block.** A `Stop` block cannot take back the message the user has
already read; it only appends a second version below it, so a length block duplicates the answer.
Length is therefore recorded at `Stop` and delivered as a note at the *next* prompt.

**Blocking a prompt is worse than the problem.** A block at `UserPromptSubmit` ends the turn and
makes the user re-enter the request. For anyone who dictates prompts that is a heavy cost, so
nothing in this harness blocks at that point — it only adds context.

**Every gate has a way out.** A per-turn cap on blocks (`max_blocks`, default 3) stops loops;
`stop_hook_active` releases the turn only when this gate has not blocked yet, so another Stop
hook's block does not start a second loop; an explicit phrase in the prompt ("skip critic")
waives the critic check, and a negated one ("do not skip the critic") does not. A gate with no
escape hatch becomes a gate people remove.

**Fail open, except where "no" is the safe answer.** A bug in a quality gate must never cost the
user a turn, so every hook turns its own exceptions into a silent pass. The AWS guard is the
exception: on an AWS CLI call it cannot parse, or an unknown program handed the CLI as an
argument, it denies, because an unparsed write is the failure it exists to prevent.

**Never emit "allow".** A `PreToolUse` hook that says "allow" approves the *whole* tool call. On a
Bash line such as `aws s3 ls && ./deploy.sh`, allowing the harmless AWS read would also approve
the deploy. The guard only ever denies, asks, or stays silent and lets the normal permission flow
decide.

**The critic is separate, and adversarial by instruction.** The author of a deliverable has the
same blind spots when it checks its own work, and its narration of what it did reads as evidence.
`/verify-spec` gives each judgment criterion to `spec-critic`, an agent that sees the criterion and
the deliverable, not the conversation that produced it, and is told to try to make it fail and to
default to FAIL or UNVERIFIABLE without evidence. That removes the author's framing; it does not
make the verdict correct, and its error rates have not been measured.

**Judge the command's structure, not its text.** A regular expression over a whole command line is
defeated by a prefix (`sudo`, `/bin/`, `git -C …`), a quote, a long flag, a shell keyword or a
second clause. Both command guards split the line on unquoted separators, tokenize each segment,
strip wrappers with their own options, and judge the program in command position. A command line
handed over as a string (`bash -c`, `eval`) is judged the same way, and every rule that fires is
collected so the most severe verdict wins rather than the first match. The same structure keeps
them quiet: a dangerous string inside a commit message, an `echo` or a `grep` pattern is an
argument, not a command. Project rules in `guardrails.config.json` are the exception: they are
plain regular expressions, each run under a time budget, after any built-in deny has been emitted.

**Names resolve through a contract, not through inference.** When the user says "the prod
account", the model should not pick the most plausible candidate. `alias_contract.py` reads a
written table of nicknames with a policy per row — fixed, volatile (ask for the exact name), or
rejected — and injects the answer before the model acts.

**Repository content is not the user's voice.** Whatever the alias hook injects arrives next to
the user's prompt. A contract inside a cloned repository was written by whoever wrote the
repository, so project contracts are off unless the user turns them on, and when on, their
rows are labelled as repository content and cannot override the user's own contract.

## Measure before and after

Most Python hooks log their decisions under the state directory as JSON lines (`ledger_track.py`
and `guardrails.mjs` do not; `prompt_start.py` logs only with `log_prompts`; the AWS guard logs
only ask and deny). That log is the point: a
threshold such as "250 words of prose" is a guess until a week of real blocks shows how many were
false positives. Tune from the log, not from a feeling.

## What this does not do

- It does not make the model correct. It makes specific omissions — no critic, no declared
  reader, a destructive AWS call spelled out on the command line — stop the turn or the call
  instead of passing silently. What each check cannot see is listed under Limits in the README.
- It does not defend against the model itself: a model with unrestricted shell access can edit
  the ledger or the config the gate reads.
- The pattern-based checks (`audience_check.py`, and the prose-length count in `exit_gate.py`)
  are heuristics and only warn. Of the end-of-turn checks, only the Stop gate blocks; it acts on
  the ledger, file modification times and BRIEF text.
- It is not a sandbox. The permission system is the sandbox; these hooks are friction on top.
