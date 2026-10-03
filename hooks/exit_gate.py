#!/usr/bin/env python3
"""
Stop hook: a gate at the end of the turn.

When Claude tries to finish a turn, this hook checks what the turn produced and,
if something is missing, returns `{"decision": "block", "reason": ...}` — the
documented Stop-hook form — so the model keeps working instead of ending. This is
enforcement by the harness, not advice the model may ignore.

Checks:

  (a) LENGTH   Never blocks. A Stop block cannot retract the message the user
               has already read; it only makes the model append a second
               version below it. So when the final answer exceeds `max_words`
               words of prose and the prompt did not ask for an explanation, a
               note is parked in the ledger and prompt_start.py delivers it
               with the next prompt, where it can shape the next answer.
  (b) CRITIC   The turn wrote an external deliverable that no independent critic
               has reviewed  ->  block until one does. A deliverable counts when
               ledger_track.py recorded it (Write, Edit, MultiEdit) or when a
               file that qualifies as one was modified under the session's
               working directory since the turn began (a Bash heredoc, a `mv`).
               Only the fact that a critic ran is checked, not its verdict; a
               file found by modification time counts as reviewed when a
               critic ran after it was last written.
  (c) BRIEF    The turn wrote an external deliverable with no BRIEF saying who
               it is for  ->  block until one exists.

Where the facts come from: the final answer is `last_assistant_message`, part of
the documented Stop input; everything else is read from the turn ledger that
ledger_track.py and prompt_start.py fill. The transcript is not parsed, because
it is written asynchronously and may not yet contain this turn.

Escape hatches, because a gate with no service door is a trap:
  - `max_blocks` blocks already issued this turn  ->  pass. This counter is the
    loop guard.
  - `stop_hook_active` in the input and this gate has not blocked this turn
    ->  pass (another Stop hook sent the model back; this one stays out of it).
  - the prompt says "no critic", "skip critic", "ship it direct" (or the
    Spanish "sin crítico", "sin revisión"), not negated ("do not skip the
    critic")  ->  check (b) is skipped.
  - any exception  ->  pass (fail open via _common.run).

Every decision is logged to `<state_dir>/exit_gate.jsonl`, which is what lets
the thresholds be tuned from data rather than impressions.

Configuration ("exit_gate" section):
  critic_agents    subagent types that count as critics: the exact name, or the
                   name after a plugin prefix ("plugin:spec-critic")
                   (["spec-critic"])
  max_words        prose-word threshold for the length note        (250)
  max_blocks       blocks per turn before the gate stops insisting  (3)
  require_critic   enable check (b)                                 (true)
  require_brief    enable check (c)                                 (true)
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import emit, log, read_event, run, setting  # noqa: E402
import turn_ledger  # noqa: E402

DEFAULT_MAX_WORDS = 250
DEFAULT_MAX_BLOCKS = 3
BRIEF_HEADER_LINES = 60
STATUS_SEARCH_DEPTH = 6

# The user asked for depth: length stops being a defect.
RX_EXPLAIN = re.compile(
    r"\b(explain\w*|walk me through|teach me|in detail|step by step|deep dive|"
    r"break (?:it|this|that) down|compare\w*|what are my options|which options|"
    r"expl[ií]ca\w*|ens[eé][nñ]ame|cu[eé]ntame|detalla\w*|profundiza\w*|paso a paso|"
    r"a fondo|en detalle|compara\w*|qu[eé] opciones)\b", re.I)

# Explicit opt-out of the critic for this turn.
RX_SKIP_CRITIC = re.compile(
    r"\b(no critic|skip (?:the )?critic|without (?:a |the )?critic|ship it direct\w*|"
    r"sin cr[ií]tico|sin revisi[oó]n)\b", re.I)

# A BRIEF can live in the artifact's own header (e.g. an HTML comment).
RX_BRIEF = re.compile(r"\b(BRIEF|Audience|Intended reader)\b\s*[:=]", re.I)

CLOSING = (
    "IMPORTANT: the user has ALREADY SEEN your previous message; this block does "
    "not remove it. Do not repeat or re-summarize it. Do the missing work, then "
    "report ONLY what changed relative to that message (what the critic found, "
    "what you fixed) and a one-line status. If you believe this block is a false "
    "positive, say so in one line and finish.")


def prose(text: str) -> str:
    """The answer minus what is not prose: code fences, inline code, table rows."""
    t = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    t = re.sub(r"~~~.*?~~~", " ", t, flags=re.S)
    t = re.sub(r"`[^`\n]{1,200}`", " ", t)
    return "\n".join(line for line in t.splitlines() if not line.lstrip().startswith("|"))


def has_brief(path: str) -> bool:
    """A BRIEF in the artifact's header, or in the nearest STATUS.md naming it."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            header = "".join(next(f, "") for _ in range(BRIEF_HEADER_LINES))
        if RX_BRIEF.search(header):
            return True
    except OSError:
        pass

    # A STATUS.md BRIEF has to be about THIS artifact, not a sibling, so its
    # name (stem or full file name) must appear on the BRIEF line itself.
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    cur = os.path.dirname(os.path.abspath(path))
    home = os.path.abspath(os.path.expanduser("~"))
    for _ in range(STATUS_SEARCH_DEPTH):
        status = os.path.join(cur, "STATUS.md")
        if os.path.isfile(status):
            try:
                with open(status, encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            except OSError:
                return False
            # The file must be NAMED on the BRIEF line itself, as a whole word:
            # a stem like "deck" must not match "board-deck.md".
            named = re.compile(rf"(?<![\w.-]){re.escape(stem)}(\.\w+)?(?![\w-])", re.I)
            return any(named.search(m.group(1))
                       for m in re.finditer(r"\bBRIEF\b([^\n]*)", text, re.I))
        parent = os.path.dirname(cur)
        if parent == cur or cur == home:
            break
        cur = parent
    return False


RX_NEGATION = re.compile(r"\b(not|n't|never|no|nunca|ni)\s+(\w+\s+){0,2}$", re.I)
SCAN_LIMIT = 5000


def skip_requested(prompt: str) -> bool:
    """An explicit waiver, not a negated one ("do NOT skip the critic")."""
    return any(not RX_NEGATION.search(prompt[max(0, m.start() - 40):m.start()])
               for m in RX_SKIP_CRITIC.finditer(prompt))


def recent_deliverables(cwd: str, since: float) -> list[str]:
    """Deliverables under the project modified since the turn started."""
    if not cwd or not since or not os.path.isdir(cwd):
        return []
    from ledger_track import is_deliverable
    from _common import is_internal
    found, seen = [], 0
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if not is_internal(os.path.join(root, d, "x"), cwd)]
        for name in files:
            seen += 1
            if seen > SCAN_LIMIT:
                return found
            p = os.path.join(root, name)
            try:
                if os.path.getmtime(p) >= since and is_deliverable(p, cwd):
                    found.append(os.path.abspath(p))
            except OSError:
                continue
    return found


def _names(paths: list[str]) -> str:
    return " · ".join(os.path.basename(p) for p in paths[:4])


def decide(event: dict, ledger: dict) -> tuple[str | None, dict]:
    """(block reason or None, details for the log). Pure apart from file reads."""
    prompt = ledger.get("prompt") or ""
    words = len(prose(str(event.get("last_assistant_message") or "")).split())
    deliverables = [p for p in ledger.get("deliverables") or [] if os.path.isfile(p)]
    # Files written outside Write/Edit (a Bash heredoc, `mv`, `>>`) never reach
    # ledger_track.py: find them by modification time instead.
    for p in recent_deliverables(str(ledger.get("cwd") or ""), float(ledger.get("ts") or 0)):
        if p not in deliverables:
            deliverables.append(p)
    reviewed = ledger.get("reviewed") or {}
    asked_explanation = bool(RX_EXPLAIN.search(prompt))
    skip_critic = skip_requested(prompt)
    max_words = setting("exit_gate", "max_words", DEFAULT_MAX_WORDS)
    critics = setting("exit_gate", "critic_agents", turn_ledger.DEFAULT_CRITICS)

    details = {"words": words, "deliverables": deliverables,
               "critics": [c.get("agent") for c in ledger.get("critics") or []
                           if isinstance(c, dict)],
               "asked_explanation": asked_explanation, "skip_critic": skip_critic,
               "long": words > max_words and not asked_explanation,
               "checks": []}
    reasons = []

    # (b) critic. `reviewed` is session memory, so an edit to a deliverable a
    # critic already passed does not require another review. A file found only by
    # modification time was never recorded, so it counts as reviewed when a critic
    # ran after it was last written.
    last_critic = max((float(c.get("ts") or 0) for c in ledger.get("critics") or []
                       if isinstance(c, dict)), default=0.0)

    def critic_ran_after(p: str) -> bool:
        try:
            return bool(last_critic) and os.path.getmtime(p) <= last_critic
        except OSError:
            return False

    unreviewed = [p for p in deliverables if p not in reviewed and not critic_ran_after(p)]
    if (unreviewed and not skip_critic
            and setting("exit_gate", "require_critic", True)):
        details["checks"].append("critic")
        names = " or ".join(f"`{c}`" for c in critics if isinstance(c, str)) or "a critic"
        reasons.append(
            f"CRITIC: this turn wrote a deliverable for other readers ({_names(unreviewed)}) "
            f"that no independent critic has reviewed. Before delivering, run the {names} "
            "subagent with ONE success criterion per call, apply what it confirms, and "
            "report the verdict table in the chat, never inside the deliverable. Budget: at "
            "most 2 rounds; if something still fails after the second, report it as open "
            "and let the user decide instead of iterating further.")

    # (c) brief
    if setting("exit_gate", "require_brief", True):
        missing = [p for p in deliverables if not has_brief(p)]
        if missing:
            details["checks"].append("brief")
            reasons.append(
                f"BRIEF: there is no BRIEF for {_names(missing)}. Declare one, either in the "
                "project's STATUS.md (a block starting with `BRIEF <file name>`) or as an "
                "invisible comment in the artifact's own header (`BRIEF: ...`), in four "
                "lines: intended reader and what they already know · the thesis in one line "
                "· what must not appear · sources of the numbers. Then check the artifact "
                "against that reader: remove any narrative of how it was iterated, the names "
                "of internal working steps, and anything that only makes sense to someone "
                "who saw this session.")

    if not reasons:
        return None, details
    return "\n\n".join(reasons) + "\n\n" + CLOSING, details


def long_answer_note(words: int, max_words: int) -> str:
    return (f"[exit_gate] Your previous answer had {words} words of prose although the "
            f"user did not ask for an explanation (limit {max_words}). In this answer: put "
            "the conclusion in the first line, keep only what changes the user's decision, "
            "end with a one-line status. Do not repeat or summarize the previous answer.")


def main() -> int:
    event = read_event()
    session = str(event.get("session_id") or "")
    if not event:
        return 0

    ledger = turn_ledger.read(session)
    # `stop_hook_active` is true on EVERY stop after the first block of a turn, so
    # passing on it alone would make the gate single-shot. The per-turn block
    # counter is the loop guard; stop_hook_active only lets go when this gate has
    # not blocked yet (another Stop hook did, or the ledger cannot be written).
    if event.get("stop_hook_active") and int(ledger.get("blocks") or 0) == 0:
        log("exit_gate", {"session": session, "blocked": False, "skip": "stop_hook_active"})
        return 0

    max_blocks = setting("exit_gate", "max_blocks", DEFAULT_MAX_BLOCKS)
    if int(ledger.get("blocks") or 0) >= max_blocks:
        log("exit_gate", {"session": session, "blocked": False, "skip": "max_blocks",
                          "blocks": ledger.get("blocks")})
        return 0

    reason, details = decide(event, ledger)
    if details["long"]:
        turn_ledger.defer_note(session, long_answer_note(
            details["words"], setting("exit_gate", "max_words", DEFAULT_MAX_WORDS)))

    if reason:
        details["blocks"] = turn_ledger.bump_block(session)
    log("exit_gate", {"session": session, "blocked": bool(reason), **details})
    if reason:
        emit({"decision": "block", "reason": reason})
    return 0


if __name__ == "__main__":
    run(main)
