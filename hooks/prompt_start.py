#!/usr/bin/env python3
"""
UserPromptSubmit hook: the start of a turn for the rest of the harness.

In order:

  1. Reset the turn ledger with the prompt and working directory, so the Stop
     hook judges this turn and not the previous one.
  2. Optionally log the prompt. Off by default: prompts often contain things
     that should not end up in a log file unless the user opted in.
  3. Deliver the long-answer note exit_gate.py parked at the end of the previous
     turn. It is delivered here, before the next answer is written, because a
     Stop block at that point would only duplicate an answer already on screen.
  4. Dictation glossary. Speech-to-text reliably mishears certain terms; when a
     configured misheard term appears in the prompt, tell the model what was
     most likely meant instead of letting it act on the literal text.

Prints nothing unless there is something to say.

Configuration ("prompt_start" section):
  log_prompts   write {session, cwd, words, text} to <state_dir>/prompts.jsonl
                                                                    (false)
  glossary      {"misheard term": "intended term"}, matched case-insensitively
                on word boundaries                                  ({})
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import add_context, log, read_event, run, setting  # noqa: E402
import turn_ledger  # noqa: E402


def is_harness_message(prompt: str) -> bool:
    # Background-task notifications and similar harness-injected messages come
    # through UserPromptSubmit wrapped in a tag. They are not a new user turn:
    # resetting the ledger there would erase a critic run from the turn in
    # progress, and the Stop hook would then demand it again.
    # Only the harness's own tags count: a user who pastes HTML or "<file>" still
    # opens a new turn (and must not inherit the last turn's waiver or blocks).
    return bool(HARNESS_TAG.match(prompt.lstrip()))


HARNESS_TAG = re.compile(
    r"<(task-notification|system-reminder|command-[\w-]+|local-command-[\w-]+|"
    r"bash-[\w-]+|user-memory-input)\b")


def glossary_hits(prompt: str) -> list[tuple[str, str]]:
    glossary = setting("prompt_start", "glossary", {})
    hits = []
    for heard, meant in glossary.items():
        if not (isinstance(heard, str) and heard.strip() and isinstance(meant, str)):
            continue
        # Lookarounds instead of \b so keys that start or end in a non-word
        # character ("c++", "node.js") still match on their edges.
        if re.search(rf"(?<!\w){re.escape(heard.strip())}(?!\w)", prompt, re.I):
            hits.append((heard.strip(), meant))
    return hits


def main() -> int:
    event = read_event()
    if not event:
        return 0
    session = str(event.get("session_id") or "")
    prompt = str(event.get("prompt") or "")
    cwd = str(event.get("cwd") or "")
    harness_message = is_harness_message(prompt)

    if not harness_message:
        turn_ledger.reset(session, prompt, cwd)
        if setting("prompt_start", "log_prompts", False):
            log("prompts", {"session": session, "cwd": cwd,
                            "words": len(prompt.split()), "text": prompt})

    notes = []
    note = turn_ledger.pop_note(session)
    if note:
        notes.append(note)
    if not harness_message:
        hits = glossary_hits(prompt)
        if hits:
            notes.append("[glossary] This prompt was likely dictated; probable "
                         "speech-to-text errors: "
                         + "; ".join(f"'{h}' likely means '{m}'" for h, m in hits)
                         + ". Read it that way unless the context says otherwise.")

    if notes:
        add_context("UserPromptSubmit", "\n".join(notes))
    return 0


if __name__ == "__main__":
    run(main)
