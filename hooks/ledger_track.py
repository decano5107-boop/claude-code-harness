#!/usr/bin/env python3
"""
Records facts in the turn ledger (see turn_ledger.py) as they happen, so the
Stop hook can act on them without parsing a transcript that may lag.

Two modes, both silent and both exit 0:

  PostToolUse (Write|Edit|MultiEdit)
      If the file just written is an EXTERNAL deliverable (something a person
      outside the session will read), add it to the ledger. That record is what
      lets exit_gate.py require a critic and a BRIEF before the turn ends.

  SubagentStop, with --critic
      Record that an independent critic ran. The agent type is checked against
      `exit_gate.critic_agents` here as well as by the settings matcher, so a
      loosely written matcher cannot turn an ordinary subagent into a reviewer.

What counts as a deliverable is deliberately conservative: a shareable format,
at least `min_bytes` long, and not an internal working file by name (STATUS,
README, SPEC, notes, anything starting with "_") or by location (.claude, .git,
build output, scratch space). Working files are where talking about the session
is correct; there is nothing for a critic to review. The internal-file lists
are shared with audience_check.py and defined once, in _common.py.

Configuration ("deliverables" section, shared with audience_check.py):
  extensions      file extensions that count, with the dot
                  (.md .markdown .html .htm .pdf .xlsx .csv .pptx .docx .txt .rst)
  internal_names  file-name globs treated as internal, case-insensitive
                  (_common.INTERNAL_NAMES)
  internal_dirs   folder-name globs treated as internal, judged below the
                  project root or home folder  (_common.INTERNAL_DIRS)
  min_bytes       smaller files are notes, not deliverables  (700)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import is_internal, read_event, run, setting, written_path  # noqa: E402
import turn_ledger  # noqa: E402

DEFAULT_EXTENSIONS = [".md", ".markdown", ".html", ".htm", ".pdf", ".xlsx", ".csv",
                      ".pptx", ".docx", ".txt", ".rst"]
DEFAULT_MIN_BYTES = 700


def is_deliverable(path: str, cwd: str = "") -> bool:
    if not path:
        return False
    exts = [e.lower() for e in setting("deliverables", "extensions", DEFAULT_EXTENSIONS)
            if isinstance(e, str)]
    if os.path.splitext(path)[1].lower() not in exts:
        return False
    if is_internal(path, cwd):
        return False
    try:
        return os.path.getsize(path) >= setting("deliverables", "min_bytes", DEFAULT_MIN_BYTES)
    except OSError:
        return False


def main(argv: list[str]) -> int:
    event = read_event()
    session = str(event.get("session_id") or "")

    if "--critic" in argv:
        agent = str(event.get("agent_type") or event.get("subagent_type") or "")[:80]
        if turn_ledger.is_critic(agent):
            turn_ledger.mark_critic(session, agent)
        return 0

    path = written_path(event)
    if not path:
        return 0
    cwd = str(event.get("cwd") or "")
    if not os.path.isabs(path) and cwd:
        path = os.path.join(cwd, path)
    path = os.path.abspath(path)
    if is_deliverable(path, os.path.abspath(cwd) if cwd else ""):
        turn_ledger.add_deliverable(session, path)
    return 0


if __name__ == "__main__":
    run(lambda: main(sys.argv[1:]))
