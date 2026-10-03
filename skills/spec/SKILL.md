---
name: spec
description: "Spec-first development: design the work before building it. Turns a rough task into a small, DETAILED, verifiable spec BEFORE any code/output is produced: separates the literal ask from the decision it serves, slices to the smallest shippable increment, interviews you for only the few high-value unknowns, proposes 2-3 approaches with pros/cons, and defines measurable verification criteria wired to whatever verification tools you already run. Triggers in English and Spanish: 'spec this', 'write a spec', 'haz un spec', 'especifica esto', 'vamos a especificar'. Also use when a task is big/ambiguous/risky enough that jumping straight to execution would drift. Distinct from prompt-polishing: spec shapes the WORK into a verifiable plan and then hands off to execution + verification. Implements the Spec layer of the three-layer method (Spec -> Verifier -> Gates)."
---

# spec — turn a task into a verifiable spec

> The Spec layer of this project's three-layer method (Spec → Verifier → Gates), which
> draws on Andrej Karpathy's public writing about AI-assisted coding. It is an independent
> implementation, not affiliated with or endorsed by him — see `NOTICE`.

**Thesis:** don't prompt for an output — **design the work first**. The model can do the
work; understanding what the work is for stays with the person, and the spec is where it
is written down. Read `reference/method.md` for the method and its primary sources; read
`reference/spec-template.md` for the output shape and a worked example.

Three non-negotiable principles drive this skill:

1. **Confidence is not evidence** — model output imitates human text; it is not
   grounded experience, and it is often confidently wrong. So every claim and every
   "done" must be **verifiable**, never trusted on how it reads.
2. **Tight leash** — treat the model as an eager, knowledgeable junior who is often
   wrong: work slowly and defensively. Stop at the spec and get a GO before building.
3. **Agile, not waterfall** — produce a **detailed spec for the SMALLEST next
   increment**, not a vague high-level plan for the whole project. A shallow plan
   is the failure this avoids. Depth on a small slice beats breadth on everything.

## 1. Take the task
Use whatever the user passed when invoking the skill. If it was invoked with no
task, ask one line and wait:

*"What do you want to specify? Give me the raw task and I will turn it into a spec."*

Answer in whatever language the user writes. If the input looks like a raw voice
transcription, fix obvious transcription errors; if a fix would change the meaning, ask.
If the project's `CLAUDE.md` defines house terms, use it to resolve them.

## 2. Separate task from decision
State back, in one line each:
- **Literal ask** — what they literally said.
- **Real goal** — the underlying decision/outcome it serves (the *why*). If you
  can't name the decision, that's the first thing to ask.

## 3. Slice to the smallest shippable increment
Name the **one concrete change** that moves the goal forward and can be verified
on its own. Everything else goes to **Parked for later** — explicitly out of
scope now. Resist speccing the whole project.

## 4. Surface interpretations, then interview (ask ONLY what's worth asking)
- If the request has **multiple valid readings**, list them and let the user
  choose — never pick silently.
- Ask **only the few high-value unknowns** (max ~3, batched as one numbered
  list) whose answers would change the spec. Prefer offering a smart default the
  user can accept with one word. If nothing material is missing, skip ahead.

## 5. Propose approaches, not code
Don't jump to implementation — offer **2-3 high-level
approaches with pros/cons**, then **recommend one** with a one-line reason. The
model's first instinct isn't always the best; make the tradeoff explicit.

## 6. Emit the SPEC (the deliverable)
Fill `reference/spec-template.md`. Every spec MUST end with **measurable
verification criteria**, each wired to *how* it gets checked — a test, or one of
the user's existing tools:
- a claims-checking skill, if you have one → factual/financial/benchmark claims;
  otherwise state the claim with its source and date and mark it UNVERIFIED
- `/verify-spec` → the criteria that need judgment, checked by an independent critic
- a command that launches and drives the app → does it actually work for real
- a code-review or security-review pass → correctness and safety of a diff
- a concrete unit test or command → anything mechanically checkable

A criterion with no verifier is not done — flag it.

## 7. Stop at the spec — get a GO (tight leash)
By default, **end at the spec and ask for a GO** before executing. If the user
said "spec and build" or the increment is trivial, proceed — but still execute
**only the single increment**, then run its verification, then report. After a
green increment, offer the next slice from "Parked for later."

## Boundaries
- One **small** slice, **deeply** specified — never a waterfall doc for the whole
  project.
- **Ask over guess.** Stop when confused; name what's unclear.
- **Cite sources** for any factual claim in the spec (code path, doc, span) — say
  "I don't know" over inventing.
- Match the user's language and register.
- This skill SHAPES and VERIFIES the work; it is not a license to over-build.
  Minimum code that satisfies the spec. Nothing speculative.
