"""
audience_check must: warn (never block) when a deliverable carries symptoms of
being written for the requester instead of its reader; stay silent on clean
prose, on a single weak signal, on internal working files, on short notes and on
quoted code; and match proper names case-sensitively.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest

from helpers import HOOKS, HookTestCase

SCRIPT = "audience_check.py"

# Neutral English prose, long enough to be treated as a deliverable.
FILLER = ("The platform routes each request to the service that owns it, and the "
          "results are cached so that repeated lookups stay fast for the users "
          "who depend on them. ") * 8


class AudienceTestCase(HookTestCase):
    def env(self, **extra: str) -> dict:
        # Hermetic identity: no system git config, a generic login, and a home
        # folder (set by run_hook) with no .gitconfig unless a test writes one.
        return {"GIT_CONFIG_NOSYSTEM": "1", "XDG_CONFIG_HOME": self.tmp,
                "USER": "ci", **extra}

    def check(self, name: str, text: str, **env: str) -> dict | None:
        path = self.make_file(name, text)
        event = {"hook_event_name": "PostToolUse", "tool_name": "Write",
                 "cwd": self.tmp, "tool_input": {"file_path": path},
                 "tool_response": {"filePath": path}}
        code, out, _ = self.run_hook(SCRIPT, event, env=self.env(**env))
        self.assertEqual(code, 0)  # never blocks
        return out

    def context(self, out: dict) -> str:
        return out["hookSpecificOutput"]["additionalContext"]


class Symptoms(AudienceTestCase):
    def test_clean_deliverable_is_silent(self):
        self.assertIsNone(self.check("guide.md", FILLER))

    def test_conversation_reference_warns_with_line_and_snippet(self):
        out = self.check("guide.md", FILLER + "\nAs we discussed, the cache expires hourly.\n")
        self.assertIn("audience_check", out["systemMessage"])
        ctx = self.context(out)
        self.assertIn("reference to the conversation", ctx)
        self.assertIn("L2:", ctx)
        self.assertIn("As we discussed", ctx)

    def test_spanish_conversation_reference_warns(self):
        out = self.check("guide.md", FILLER + "\nComo acordamos, the cache expires hourly.\n")
        self.assertIn("reference to the conversation", self.context(out))

    def test_process_scaffolding_warns(self):
        out = self.check("guide.md", FILLER + "\nThis was fixed in QA round 2.\n")
        self.assertIn("process scaffolding", self.context(out))

    def test_plain_numbered_step_is_not_scaffolding(self):
        self.assertIsNone(self.check("guide.md", FILLER + "\nStep 3: restart the service.\n"))

    def test_person_cited_as_authority_warns(self):
        out = self.check("guide.md", FILLER + "\nThe limit was approved by Alex.\n")
        self.assertIn("a person cited as authority", self.context(out))

    def test_authority_rule_is_case_sensitive(self):
        self.assertIsNone(self.check("guide.md", FILLER + "\nThe limit was approved by the board.\n"))

    def test_single_weak_signal_is_below_threshold(self):
        self.assertIsNone(self.check("guide.md", FILLER + "\nI decided to cache results.\n"))

    def test_two_weak_signals_cross_threshold(self):
        out = self.check("guide.md", FILLER + "\nI decided to cache results. "
                         "It meets your threshold.\n")
        ctx = self.context(out)
        self.assertIn("first-person narration", ctx)
        self.assertIn("second person", ctx)

    def test_threshold_is_configurable(self):
        self.write_config({"audience_check": {"threshold": 2}})
        out = self.check("guide.md", FILLER + "\nI decided to cache results.\n")
        self.assertIsNotNone(out)

    def test_findings_capped_at_five(self):
        self.write_config({"audience_check": {"names": ["Jordan"]}})
        text = FILLER + ("\nAs we discussed, see QA round 2. Approved by Alex. "
                         "I decided this. It meets your threshold. Jordan checked it.\n")
        ctx = self.context(self.check("guide.md", text))
        self.assertEqual(sum(1 for l in ctx.splitlines() if l.startswith("  - ")), 5)


class Names(AudienceTestCase):
    def test_name_from_git_config(self):
        with open(os.path.join(self.tmp, ".gitconfig"), "w") as f:
            f.write("[user]\n\tname = Alex Rivera\n")
        out = self.check("guide.md", FILLER + "\nOwner: Rivera.\n")
        self.assertIn("the user's own name", self.context(out))

    def test_name_match_is_case_sensitive(self):
        with open(os.path.join(self.tmp, ".gitconfig"), "w") as f:
            f.write("[user]\n\tname = Sky Rivera\n")
        self.assertIsNone(self.check("guide.md", FILLER + "\nThe sky is clear.\n"))

    def test_name_from_login(self):
        out = self.check("guide.md", FILLER + "\nOwner: Jordan.\n", USER="jordan")
        self.assertIn("the user's own name", self.context(out))

    def test_generic_login_ignored(self):
        self.assertIsNone(self.check("guide.md", FILLER + "\nAdmin rights required.\n",
                                     USER="admin"))

    def test_configured_names(self):
        self.write_config({"audience_check": {"names": ["Morgan"]}})
        out = self.check("guide.md", FILLER + "\nMorgan reviewed this.\n")
        self.assertIn("the user's own name", self.context(out))


class Language(AudienceTestCase):
    MIXED = FILLER + ("\nEsto es para el equipo porque cuando se rompe, también "
                      "se cae el resto, aunque siempre vuelve.\n")

    def test_chat_language_alone_is_weak(self):
        self.assertIsNone(self.check("guide.md", self.MIXED))

    def test_chat_language_reported_with_threshold_two(self):
        self.write_config({"audience_check": {"threshold": 2}})
        out = self.check("guide.md", self.MIXED)
        self.assertIn("chat language", self.context(out))

    def test_below_ratio_is_ignored(self):
        self.write_config({"audience_check": {"threshold": 2, "language_min_ratio": 0.5}})
        self.assertIsNone(self.check("guide.md", self.MIXED))

    def test_chat_language_words_can_be_turned_off(self):
        self.write_config({"audience_check": {"threshold": 2, "chat_language_words": []}})
        self.assertIsNone(self.check("guide.md", self.MIXED))

    def test_chat_language_words_are_configurable(self):
        german = FILLER + ("\nDas ist für das Team, weil wenn es bricht, dann auch "
                           "der Rest, obwohl es immer wieder kommt.\n")
        self.write_config({"audience_check": {"threshold": 2, "chat_language_words":
                                              ["das", "weil", "wenn", "dann", "auch",
                                               "obwohl", "immer"]}})
        self.assertIn("chat language", self.context(self.check("guide.md", german)))
        self.assertIsNone(self.check("guide.md", self.MIXED))


class Scope(AudienceTestCase):
    BAD = FILLER + "\nAs we discussed, approved by Alex.\n"

    def test_code_is_not_prose(self):
        text = FILLER + "\n```\n# as we discussed\n```\nRun `approved by Alex` now.\n"
        self.assertIsNone(self.check("guide.md", text))

    def test_internal_file_names_skipped(self):
        for name in ("STATUS.md", "HANDOFF_v2.md", "_draft.md", "meeting_notes.md"):
            self.assertIsNone(self.check(name, self.BAD), name)

    def test_internal_dirs_skipped(self):
        for name in ("_drafts/out.md", ".claude/plan.md", "tmp/out.md", "scratchpad/a.md"):
            self.assertIsNone(self.check(name, self.BAD), name)

    def test_internal_lists_configurable(self):
        # The lists live in the "deliverables" section, shared with ledger_track.
        self.write_config({"deliverables": {"internal_names": [], "internal_dirs": []}})
        self.assertIsNotNone(self.check("STATUS.md", self.BAD))
        self.assertIsNotNone(self.check("_drafts/out.md", self.BAD))

    def test_shared_internal_names_skipped(self):
        # Names that ledger_track has always treated as working files.
        for name in ("README.md", "CHANGELOG.md", "TODO-list.md", "notes-q3.md"):
            self.assertIsNone(self.check(name, self.BAD), name)

    def test_shared_internal_dirs_skipped(self):
        for name in ("build/out.md", "dist/out.md"):
            self.assertIsNone(self.check(name, self.BAD), name)

    def test_folders_above_home_are_not_judged(self):
        # A home folder that happens to sit under a "tmp" folder is not scratch.
        home = os.path.join(self.tmp, "tmp", "home")
        path = self.make_file("tmp/home/proj/guide.md", self.BAD)
        event = {"hook_event_name": "PostToolUse", "tool_name": "Write",
                 "cwd": os.path.join(self.tmp, "elsewhere"),
                 "tool_input": {"file_path": path}, "tool_response": {"filePath": path}}
        code, out, _ = self.run_hook(SCRIPT, event, env=self.env(HOME=home))
        self.assertEqual(code, 0)
        self.assertIsNotNone(out)

    def test_short_note_skipped(self):
        self.assertIsNone(self.check("guide.md", "As we discussed, approved by Alex."))

    def test_other_extensions_skipped(self):
        self.assertIsNone(self.check("tool.py", self.BAD))

    def test_malformed_event_fails_open(self):
        code, out, _ = self.run_hook(SCRIPT, "{not json")
        self.assertEqual((code, out), (0, None))


class Cli(AudienceTestCase):
    def cli(self, *paths: str) -> tuple[int, str]:
        env = {**os.environ, "HARNESS_CONFIG": self.config_path, "HOME": self.tmp,
               **self.env()}
        r = subprocess.run([sys.executable, os.path.join(HOOKS, SCRIPT), *paths],
                           capture_output=True, text=True, env=env, cwd=self.tmp, timeout=30)
        return r.returncode, r.stdout

    def test_cli_flags_and_passes(self):
        bad = self.make_file("bad.md", FILLER + "\nAs we discussed.\n")
        good = self.make_file("good.md", FILLER)
        code, out = self.cli(bad, good)
        self.assertEqual(code, 1)
        self.assertIn("[!!]", out)
        self.assertIn("[ok]", out)
        self.assertEqual(self.cli(good)[0], 0)


if __name__ == "__main__":
    unittest.main()
