# The method: spec, independent critic, gates (reference)

This is the method the `spec` skill, the `/verify-spec` command, the `spec-critic` agent and the
hooks in this repository implement, described in this project's own words. It draws on Andrej
Karpathy's public writing about working with language models, cited below where an idea comes
from him. The three-layer arrangement is this project's framing, not his; this project is not
affiliated with or endorsed by him (see `NOTICE`).

Premise: asking a model for an output and judging the result by eye does not scale. Design the
work first, decide in advance how "done" will be checked, and have something other than the
author do the checking. Understanding the problem stays with the person; the spec is where that
understanding is written down.

---

## Layer 1 — SPEC: from a goal to a slice the model can execute (the `spec` skill)

- Separate the literal task from the decision it serves.
- Specify the **smallest next increment in detail**, not the whole project in outline. A shallow
  plan for everything is the failure this avoids.
- Have the model ask the few questions whose answers would change the spec, instead of guessing.
- Compare two or three approaches before choosing one.
- End with **measurable success criteria**, each tied to the check that will decide it.

## Layer 2 — VERIFIER: an attempt to refute, not a self-assessment (`/verify-spec`, `spec-critic`)

- Mechanical criteria are checked by running the test or command and reading its output.
- Judgment criteria go to a separate critic agent, given one criterion and the deliverable, and
  instructed to try to make it fail. It is instructed to return PASS only when that attempt finds
  nothing, and FAIL or UNVERIFIABLE when evidence is missing.
- The critic is the same kind of model as the author. Running it separately removes the author's
  narration from the judgment; it does not make the judgment correct. How often it catches a real
  defect, or rejects a good deliverable, has not been measured.

## Layer 3 — GATES: rules enforced by hooks, not remembered by the model

- A rule written in a prompt or a `CLAUDE.md` fades over a long session. A hook runs every time.
- The `Stop` hook (`exit_gate.py`) sends the model back, up to a set number of times per turn,
  while a deliverable written in the turn has no critic run recorded after it was written, or no
  BRIEF. It checks that the critic ran, not its verdict.
- `PreToolUse` hooks add friction before the irreversible commands they recognise
  (`guardrails.mjs`) and deny non-read AWS CLI calls they can parse or cannot parse
  (`aws_readonly_guard.py`). They ask or deny; they never approve.

---

## Where the ideas come from (primary sources)

**Model output imitates text; it is not grounded experience.** Karpathy contrasts animals, shaped
by evolution and a body in the world, with language models, distilled from human-written text,
and calls the latter "ghosts". The practical consequence taken here: a confident answer is not
evidence, so every "done" needs a check.
- Andrej Karpathy, "Animals vs Ghosts", https://karpathy.bearblog.dev/animals-vs-ghosts/

**Small increments on a tight leash.** Karpathy describes his own rhythm for AI-assisted coding on
code he cares about. Paraphrased, not quoted:
1. Put everything relevant into context.
2. Describe the next single, concrete, incremental change.
3. Ask for a few high-level approaches with pros and cons before any code.
4. Pick one and ask for that implementation.
5. Review it and learn from it rather than accepting it blindly; test; commit; repeat.

In the same post he describes treating the model as an eager, knowledgeable junior who is often
confidently wrong, and working slowly and defensively as a result.
- Andrej Karpathy, post on X, 25 April 2025, https://x.com/karpathy/status/1915581920022585597

---

## How the layers map onto an existing setup

- **Spec** → the `spec` skill.
- **Verifier** → `/verify-spec` and the `spec-critic` agent, plus whatever checks the project
  already runs (tests, a claims-checking pass for numbers, a code review for diffs). The spec
  names which check decides each criterion.
- **Gates** → the hooks in this repository, on top of your permission settings. The point is not
  to add tools; it is to know which existing check decides each criterion, and to put the ones
  that must not be skipped behind a hook.
