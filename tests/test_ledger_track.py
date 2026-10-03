"""
Contract of ledger_track.py: it records external deliverables and critic runs
in the turn ledger, ignores working files and non-critic subagents, never
prints anything, and always exits 0.
"""
from __future__ import annotations

import json
import os

from helpers import HookTestCase

HOOK = "ledger_track.py"
BIG = "Quarterly findings.\n" + "Lorem ipsum dolor sit amet. " * 40


class LedgerTrackTest(HookTestCase):
    session = "sess-1"

    def ledger(self) -> dict:
        path = os.path.join(self.state, "ledger", f"{self.session}.json")
        if not os.path.exists(path):
            return {}
        with open(path) as f:
            return json.load(f)

    def write_event(self, path: str, cwd: str | None = None) -> dict:
        return {"session_id": self.session, "hook_event_name": "PostToolUse",
                "tool_name": "Write", "cwd": cwd if cwd is not None else self.tmp,
                "tool_input": {"file_path": path, "content": "..."},
                "tool_response": {"filePath": path, "success": True}}

    def track(self, path: str, **kw) -> list:
        code, out, _ = self.run_hook(HOOK, self.write_event(path, **kw))
        self.assertEqual(code, 0)
        self.assertIsNone(out)
        return self.ledger().get("deliverables", [])

    def test_records_external_deliverable(self):
        path = self.make_file("proj/report.md", BIG)
        self.assertEqual(self.track(path), [path])

    def test_relative_path_resolved_against_cwd(self):
        path = self.make_file("proj/report.html", BIG)
        self.assertEqual(self.track("proj/report.html"), [path])

    def test_ignores_code_and_config_files(self):
        for name in ("proj/app.py", "proj/settings.json"):
            self.assertEqual(self.track(self.make_file(name, BIG)), [])

    def test_ignores_small_files(self):
        self.assertEqual(self.track(self.make_file("proj/short.md", "four lines")), [])

    def test_ignores_internal_names(self):
        for name in ("STATUS.md", "README.md", "CLAUDE.md", "HANDOFF-next.md",
                     "CHANGELOG.md", "TODO.md", "notes.md", "meeting_notes.md", "_draft.md"):
            self.assertEqual(self.track(self.make_file(f"proj/{name}", BIG)), [], name)

    def test_ignores_shared_internal_names(self):
        # Names that audience_check has always treated as working files.
        for name in ("SPEC.md", "AGENTS.md", "DECISIONS.md", "FEEDBACK.md",
                     "NEXT_SESSION.md", "scratch.md", "memory.md", "plan_internal.md"):
            self.assertEqual(self.track(self.make_file(f"proj/{name}", BIG)), [], name)

    def test_ignores_internal_directories(self):
        for d in (".claude", ".git", "_drafts", "_private", "_archive", "scratchpad",
                  "node_modules", "tmp", "dist", "build", "sessions", "memory"):
            self.assertEqual(self.track(self.make_file(f"proj/{d}/out.md", BIG)), [], d)

    def test_project_root_under_a_scratch_named_dir_still_counts(self):
        # Only directories below the project root are judged.
        root = os.path.join(self.tmp, "tmp", "proj")
        path = self.make_file("tmp/proj/report.md", BIG)
        self.assertEqual(self.track(path, cwd=root), [path])

    def test_outside_project_folders_above_home_are_not_judged(self):
        home = os.path.join(self.tmp, "tmp", "home")
        path = self.make_file("tmp/home/proj/report.md", BIG)
        event = self.write_event(path, cwd=os.path.join(self.tmp, "elsewhere"))
        code, out, _ = self.run_hook(HOOK, event, env={"HOME": home})
        self.assertEqual((code, out), (0, None))
        self.assertEqual(self.ledger().get("deliverables"), [path])

    def test_internal_patterns_are_configurable(self):
        self.write_config({"deliverables": {"internal_names": ["draft-*"],
                                            "internal_dirs": ["private"],
                                            "extensions": [".md"], "min_bytes": 10}})
        self.assertEqual(self.track(self.make_file("proj/draft-1.md", BIG)), [])
        self.assertEqual(self.track(self.make_file("proj/private/x.md", BIG)), [])
        self.assertEqual(self.track(self.make_file("proj/x.html", BIG)), [])
        small = self.make_file("proj/README.md", "enough bytes")
        self.assertEqual(self.track(small), [small])  # defaults replaced, not merged

    def test_critic_run_marks_deliverables_reviewed(self):
        path = self.make_file("proj/report.md", BIG)
        self.track(path)
        for field in ("agent_type", "subagent_type"):
            with self.subTest(field=field):
                code, out, _ = self.run_hook(
                    HOOK, {"session_id": self.session, "hook_event_name": "SubagentStop",
                           field: "myplugin:spec-critic"}, "--critic")
                self.assertEqual((code, out), (0, None))
                self.assertIn(path, self.ledger()["reviewed"])
                self.assertTrue(self.ledger()["critics"])

    def test_non_critic_subagent_is_not_a_review(self):
        path = self.make_file("proj/report.md", BIG)
        self.track(path)
        for agent in ("general-purpose", ""):
            self.run_hook(HOOK, {"session_id": self.session, "agent_type": agent}, "--critic")
        self.assertEqual(self.ledger().get("reviewed"), {})

    def test_irrelevant_event_is_silent(self):
        code, out, _ = self.run_hook(HOOK, {"session_id": self.session, "tool_name": "Bash",
                                            "tool_input": {"command": "ls"}})
        self.assertEqual((code, out), (0, None))
        self.assertEqual(self.ledger(), {})

    def test_fails_open_on_malformed_stdin(self):
        for stdin in ("", "not json", "[1, 2]", '{"tool_response": "str"}'):
            code, out, _ = self.run_hook(HOOK, stdin)
            self.assertEqual((code, out), (0, None), stdin)
            code, out, _ = self.run_hook(HOOK, stdin, "--critic")
            self.assertEqual((code, out), (0, None), stdin)


if __name__ == "__main__":
    import unittest
    unittest.main()
