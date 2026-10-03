---
description: Verify a deliverable against its SPEC's success criteria — the Verifier layer. Runs the mechanical checks, dispatches an independent critic subagent for judgment calls, and routes factual claims to a claims-checking pass and code to review. Use after producing something the spec skill defined, or whenever "done" needs evidence rather than an assertion.
argument-hint: [what to verify — a file, a diff, a claim, or "the last spec"]
---

# verify-spec — make "done" measurable (Verifier layer)

You are running the **Verifier** layer of the method described in
`skills/spec/reference/method.md` (an independent implementation; see `NOTICE`).
Nothing is "done" because it reads well. Take a deliverable and check it against
the **verification criteria of its SPEC**, criterion by criterion, then return a verdict.

Target to verify: **$ARGUMENTS** (if empty, use the most recent spec/deliverable in
this conversation, or ask which one).

## 1. Locate the spec + its criteria
Find the SPEC this deliverable answers (from the argument, the conversation, or the
`claude-code-harness:spec` output). Extract its **Verification** checklist. If there is
no spec, reconstruct the implied success criteria in one line each — and say so.

## 2. Classify each criterion
- **Mechanical** (test / command / typecheck / build) → run it, capture real output.
- **Claim** (a number, benchmark, factual assertion) → hand to a claims-checking skill if you
  have one; otherwise state the claim, its source and its date inline, and mark it UNVERIFIED.
- **Diff correctness / safety** → hand to a code-review or security-review pass if your
  setup has one; otherwise read the diff against the criterion yourself and say which
  lines you checked.
- **Judgment** (does the output truly satisfy intent; is anything missing) →
  dispatch the **spec-critic** subagent (a separate agent that did not write it).

## 3. Dispatch the critic (a separate agent that tries to refute)
For each judgment criterion, spawn the `claude-code-harness:spec-critic` agent with that
one criterion + the deliverable. It is adversarial and defaults to FAIL when unsure.
Do not grade your own work generously — let the critic be the skeptic.

## 4. Return the verdict table
One row per criterion: ✅ pass / ❌ fail / ⚠️ unverifiable — each with the **evidence**
(command output, critic finding, tool result). Close with a one-line **GO / NO-GO**.

## Boundaries
- Verify against the spec's criteria; don't invent new scope.
- Real evidence only — quote the actual output, never assert a pass you didn't check.
- A criterion you could not actually check is ⚠️ unverifiable, never ✅.
- If the spec listed a criterion with no verifier attached, flag it as a gap to fix.
