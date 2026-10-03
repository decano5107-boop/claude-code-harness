"""
One definition of "internal working file": ledger_track.py (which decides what
needs a critic) and audience_check.py (which decides what gets a reader check)
must agree on every name and folder, because both read the shared lists in
_common.py and the one "deliverables" configuration section.
"""
from __future__ import annotations

import os

from helpers import HookTestCase

BIG = ("The platform routes each request to the service that owns it, and the "
       "results are cached so that repeated lookups stay fast. ") * 12
# Enough for audience_check to warn on any file it considers a deliverable.
BAD = BIG + "\nAs we discussed, approved by Alex.\n"

CASES = [
    ("report.md", False), ("guide.html", False), ("proposal.txt", False),
    ("STATUS.md", True), ("README.md", True), ("CLAUDE.md", True), ("AGENTS.md", True),
    ("SPEC.md", True), ("project-spec.md", True), ("HANDOFF.md", True),
    ("next_session.md", True), ("CHANGELOG.md", True), ("DECISIONS.md", True),
    ("TODO.md", True), ("notes.md", True), ("note.md", True), ("call_notes.md", True),
    ("plan_internal.md", True), ("FEEDBACK.md", True), ("MEMORY.md", True),
    ("scratch.md", True), ("_draft.md", True),
    (".claude/out.md", True), (".git/out.md", True), ("_drafts/out.md", True),
    ("_private/out.md", True), ("_archive/out.md", True), ("scratchpad/out.md", True),
    ("tmp/out.md", True), ("node_modules/out.md", True), ("dist/out.md", True),
    ("build/out.md", True), ("sessions/out.md", True), ("memory/out.md", True),
]


class SharedDefinition(HookTestCase):
    def event(self, path: str) -> dict:
        return {"session_id": "s", "hook_event_name": "PostToolUse", "tool_name": "Write",
                "cwd": self.tmp, "tool_input": {"file_path": path},
                "tool_response": {"filePath": path}}

    def tracked(self, path: str) -> bool:
        self.run_hook("ledger_track.py", self.event(path))
        ledger = os.path.join(self.state, "ledger", "s.json")
        if not os.path.exists(ledger):
            return False
        with open(ledger, encoding="utf-8") as f:
            return path in f.read()

    def checked(self, path: str) -> bool:
        _, out, _ = self.run_hook("audience_check.py", self.event(path),
                                  env={"GIT_CONFIG_NOSYSTEM": "1", "USER": "ci",
                                       "XDG_CONFIG_HOME": self.tmp})
        return out is not None

    def assert_agree(self, cases) -> None:
        for i, (name, internal) in enumerate(cases):
            with self.subTest(name=name):
                path = self.make_file(os.path.join(f"p{i}", name), BAD)
                self.assertEqual(self.tracked(path), not internal)
                self.assertEqual(self.checked(path), not internal)

    def test_both_hooks_agree_on_defaults(self):
        self.assert_agree(CASES)

    def test_both_hooks_read_the_deliverables_section(self):
        self.write_config({"deliverables": {"internal_names": ["draft-*"],
                                            "internal_dirs": ["private"]}})
        self.assert_agree([("draft-1.md", True), ("private/out.md", True),
                           ("STATUS.md", False), ("tmp/out.md", False)])
