"""
Contract of the turn ledger module: per-turn fields reset at every prompt,
session memory (seen / reviewed) survives, writes are atomic, stale ledgers are
swept, and nothing ever raises — a corrupt or missing ledger reads as empty.
"""
from __future__ import annotations

import json
import os
import sys
import time

from helpers import HOOKS, HookTestCase

sys.path.insert(0, HOOKS)
import _common  # noqa: E402
import turn_ledger  # noqa: E402


class TurnLedgerTest(HookTestCase):
    config = {"exit_gate": {"critic_agents": ["spec-critic", "doc-reviewer"]}}

    def setUp(self) -> None:
        super().setUp()
        self._env = os.environ.get("HARNESS_CONFIG")
        os.environ["HARNESS_CONFIG"] = self.config_path
        _common._CONFIG = None  # the module caches config per process

    def tearDown(self) -> None:
        if self._env is None:
            os.environ.pop("HARNESS_CONFIG", None)
        else:
            os.environ["HARNESS_CONFIG"] = self._env
        _common._CONFIG = None
        super().tearDown()

    def ledger_file(self, session: str) -> str:
        return os.path.join(self.state, "ledger", f"{session}.json")

    def test_missing_ledger_reads_as_empty_turn(self):
        d = turn_ledger.read("never-seen")
        self.assertEqual(d["deliverables"], [])
        self.assertEqual(d["blocks"], 0)
        self.assertEqual(d["reviewed"], {})

    def test_corrupt_ledger_reads_as_empty_turn(self):
        turn_ledger.reset("s1", "hello")
        with open(self.ledger_file("s1"), "w") as f:
            f.write("{not json")
        self.assertEqual(turn_ledger.read("s1")["prompt"], "")

    def test_wrongly_typed_fields_fall_back_to_defaults(self):
        turn_ledger.reset("s1")
        with open(self.ledger_file("s1"), "w") as f:
            json.dump({"deliverables": "oops", "blocks": "x", "reviewed": []}, f)
        d = turn_ledger.read("s1")
        self.assertEqual(d["deliverables"], [])
        self.assertEqual(d["reviewed"], {})
        self.assertEqual(turn_ledger.bump_block("s1"), 1)

    def test_reset_clears_turn_but_keeps_session_memory(self):
        turn_ledger.reset("s1", "first", "/p")
        turn_ledger.add_deliverable("s1", "/p/report.md")
        turn_ledger.mark_critic("s1", "spec-critic")
        turn_ledger.bump_block("s1")
        turn_ledger.reset("s1", "second", "/p")
        d = turn_ledger.read("s1")
        self.assertEqual(d["prompt"], "second")
        self.assertEqual(d["deliverables"], [])
        self.assertEqual(d["critics"], [])
        self.assertEqual(d["blocks"], 0)
        self.assertEqual(d["seen"], ["/p/report.md"])
        self.assertIn("/p/report.md", d["reviewed"])

    def test_add_deliverable_is_idempotent(self):
        turn_ledger.reset("s1")
        turn_ledger.add_deliverable("s1", "/p/a.md")
        turn_ledger.add_deliverable("s1", "/p/a.md")
        d = turn_ledger.read("s1")
        self.assertEqual(d["deliverables"], ["/p/a.md"])
        self.assertEqual(d["seen"], ["/p/a.md"])

    def test_critic_marks_every_seen_deliverable_reviewed(self):
        turn_ledger.reset("s1")
        turn_ledger.add_deliverable("s1", "/p/a.md")
        turn_ledger.reset("s1")
        turn_ledger.add_deliverable("s1", "/p/b.md")
        turn_ledger.mark_critic("s1", "spec-critic")
        self.assertEqual(set(turn_ledger.read("s1")["reviewed"]), {"/p/a.md", "/p/b.md"})

    def test_deliverable_after_critic_is_not_reviewed(self):
        turn_ledger.reset("s1")
        turn_ledger.mark_critic("s1", "spec-critic")
        turn_ledger.add_deliverable("s1", "/p/late.md")
        self.assertNotIn("/p/late.md", turn_ledger.read("s1")["reviewed"])

    def test_bump_block_counts(self):
        turn_ledger.reset("s1")
        self.assertEqual(turn_ledger.bump_block("s1"), 1)
        self.assertEqual(turn_ledger.bump_block("s1"), 2)

    def test_session_id_cannot_escape_the_ledger_dir(self):
        turn_ledger.reset("../../evil", "x")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.json")))
        self.assertEqual(turn_ledger.read("../../evil")["prompt"], "x")

    def test_no_temp_files_left_behind(self):
        turn_ledger.reset("s1")
        turn_ledger.add_deliverable("s1", "/p/a.md")
        leftovers = [n for n in os.listdir(os.path.join(self.state, "ledger"))
                     if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_sweep_removes_only_stale_files(self):
        turn_ledger.reset("old")
        turn_ledger.reset("new")
        week_ago = time.time() - 8 * 24 * 3600
        os.utime(self.ledger_file("old"), (week_ago, week_ago))
        turn_ledger.sweep()
        self.assertFalse(os.path.exists(self.ledger_file("old")))
        self.assertTrue(os.path.exists(self.ledger_file("new")))

    def test_deferred_note_is_delivered_once(self):
        turn_ledger.defer_note("s1", "be shorter")
        self.assertEqual(turn_ledger.pop_note("s1"), "be shorter")
        self.assertEqual(turn_ledger.pop_note("s1"), "")

    def test_is_critic_matches_configured_names_with_plugin_prefix(self):
        self.assertTrue(turn_ledger.is_critic("spec-critic"))
        self.assertTrue(turn_ledger.is_critic("someplugin:Spec-Critic"))
        self.assertTrue(turn_ledger.is_critic("doc-reviewer"))
        self.assertFalse(turn_ledger.is_critic("general-purpose"))
        self.assertFalse(turn_ledger.is_critic(""))

    def test_unwritable_state_never_raises(self):
        os.makedirs(os.path.join(self.state, "ledger"), exist_ok=True)
        os.makedirs(self.ledger_file("s1"))  # a directory where the file should be
        turn_ledger.reset("s1", "x")
        turn_ledger.add_deliverable("s1", "/p/a.md")
        turn_ledger.mark_critic("s1", "spec-critic")
        self.assertEqual(turn_ledger.read("s1")["deliverables"], [])


if __name__ == "__main__":
    import unittest
    unittest.main()
