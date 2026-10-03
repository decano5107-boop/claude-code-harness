"""
Contract of exit_gate.py (Stop hook):

  - blocks when the turn wrote an external deliverable no critic reviewed, or
    one with no BRIEF, with a reason that tells the model the user already saw
    its previous message;
  - never blocks for length: it parks a note for the next prompt instead;
  - lets the turn end after max_blocks, on stop_hook_active only while it has
    not blocked this turn itself, and skips the critic check when the prompt
    opts out (a negated opt-out does not count);
  - is silent on turns with nothing to judge, fails open, always exits 0,
    and logs each decision.
"""
from __future__ import annotations

import json
import os

from helpers import HookTestCase

GATE = "exit_gate.py"
SESSION = "sess-gate"
BODY = "Findings for the operations team.\n" + "The metric moved because of X. " * 40
LONG = "word " * 400
SHORT = "Done: report written. Next: nothing."


class ExitGateTest(HookTestCase):

    # ── scenario plumbing: drive the same hooks Claude Code would ──────────
    def start_turn(self, prompt: str) -> None:
        code, _, _ = self.run_hook("prompt_start.py", {
            "session_id": SESSION, "hook_event_name": "UserPromptSubmit",
            "prompt": prompt, "cwd": self.tmp})
        self.assertEqual(code, 0)

    def write_deliverable(self, name: str = "proj/report.md", text: str = BODY) -> str:
        path = self.make_file(name, text)
        self.run_hook("ledger_track.py", {
            "session_id": SESSION, "cwd": self.tmp, "tool_name": "Write",
            "tool_input": {"file_path": path}, "tool_response": {"filePath": path}})
        return path

    def run_critic(self, agent: str = "reviewers:spec-critic") -> None:
        self.run_hook("ledger_track.py", {"session_id": SESSION, "agent_type": agent},
                      "--critic")

    def stop(self, message: str = SHORT, **extra) -> dict | None:
        code, out, _ = self.run_hook(GATE, {
            "session_id": SESSION, "hook_event_name": "Stop",
            "last_assistant_message": message, **extra})
        self.assertEqual(code, 0)
        return out

    def next_prompt_context(self) -> str:
        _, out, _ = self.run_hook("prompt_start.py", {
            "session_id": SESSION, "prompt": "ok next", "cwd": self.tmp})
        return (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")

    def log_records(self) -> list[dict]:
        path = os.path.join(self.state, "exit_gate.jsonl")
        with open(path) as f:
            return [json.loads(line) for line in f]

    # ── nothing to judge ────────────────────────────────────────────────────
    def test_plain_turn_passes_silently(self):
        self.start_turn("fix the script")
        self.assertIsNone(self.stop())

    def test_fails_open_on_malformed_stdin(self):
        for stdin in ("", "garbage", "[]", "null"):
            code, out, _ = self.run_hook(GATE, stdin)
            self.assertEqual((code, out), (0, None), stdin)

    def test_unknown_session_passes(self):
        self.assertIsNone(self.stop())

    # ── (b) critic ──────────────────────────────────────────────────────────
    def test_unreviewed_deliverable_blocks(self):
        self.start_turn("write the report")
        self.write_deliverable("proj/report.md", "<!-- BRIEF: ops team -->\n" + BODY)
        out = self.stop()
        self.assertEqual(out["decision"], "block")
        self.assertIn("CRITIC", out["reason"])
        self.assertIn("report.md", out["reason"])
        self.assertIn("`spec-critic`", out["reason"])
        self.assertNotIn("BRIEF:", out["reason"])

    def test_block_reason_says_user_already_saw_the_message(self):
        self.start_turn("write the report")
        self.write_deliverable()
        reason = self.stop()["reason"]
        self.assertIn("ALREADY SEEN", reason)
        self.assertIn("only what changed".lower(), reason.lower())
        self.assertIn("one-line status", reason)
        self.assertIn("false positive", reason)

    def test_reviewed_deliverable_with_brief_passes(self):
        self.start_turn("write the report")
        self.write_deliverable("proj/report.md", "<!-- BRIEF: ops team -->\n" + BODY)
        self.run_critic()
        self.assertIsNone(self.stop())

    def test_non_critic_subagent_does_not_count(self):
        self.start_turn("write the report")
        self.write_deliverable("proj/report.md", "Audience: ops\n" + BODY)
        self.run_critic("general-purpose")
        self.assertEqual(self.stop()["decision"], "block")

    def test_critic_names_are_configurable(self):
        self.write_config({"exit_gate": {"critic_agents": ["doc-reviewer"]}})
        self.start_turn("write the report")
        self.write_deliverable("proj/report.md", "Audience: ops\n" + BODY)
        self.run_critic("spec-critic")
        self.assertIn("`doc-reviewer`", self.stop()["reason"])
        self.run_critic("team:doc-reviewer")
        self.assertIsNone(self.stop())

    def test_edit_to_reviewed_deliverable_in_later_turn_passes(self):
        self.start_turn("write the report")
        self.write_deliverable("proj/report.md", "Audience: ops\n" + BODY)
        self.run_critic()
        self.assertIsNone(self.stop())
        self.start_turn("fix a typo in the report")
        self.write_deliverable("proj/report.md", "Audience: ops\n" + BODY + " typo fixed")
        self.assertIsNone(self.stop())

    def test_new_deliverable_after_critic_blocks(self):
        self.start_turn("write two reports")
        self.write_deliverable("proj/a.md", "Audience: ops\n" + BODY)
        self.run_critic()
        self.write_deliverable("proj/b.md", "Audience: ops\n" + BODY)
        out = self.stop()
        self.assertIn("b.md", out["reason"])
        self.assertNotIn("a.md", out["reason"])

    def test_skip_critic_phrases(self):
        for prompt in ("write it, no critic", "Skip critic this time",
                       "ship it direct", "hazlo sin crítico", "entrégalo sin revisión"):
            with self.subTest(prompt=prompt):
                self.start_turn(prompt)
                self.write_deliverable("proj/report.md", "Audience: ops\n" + BODY)
                self.assertIsNone(self.stop())

    def test_skip_critic_does_not_skip_brief(self):
        self.start_turn("write it, no critic")
        self.write_deliverable()
        out = self.stop()
        self.assertIn("BRIEF", out["reason"])
        self.assertNotIn("CRITIC", out["reason"])

    def test_deleted_deliverable_is_ignored(self):
        self.start_turn("write the report")
        os.remove(self.write_deliverable())
        self.assertIsNone(self.stop())

    # ── (c) brief ───────────────────────────────────────────────────────────
    def test_missing_brief_blocks(self):
        self.start_turn("write the report")
        self.write_deliverable()
        self.run_critic()
        out = self.stop()
        self.assertIn("BRIEF", out["reason"])
        self.assertNotIn("CRITIC", out["reason"])

    def test_brief_in_header_variants(self):
        for header in ("<!-- BRIEF: ops team -->", "Audience = product leads",
                       "Intended reader: the board"):
            with self.subTest(header=header):
                self.start_turn("write the report")
                self.write_deliverable("proj/report.md", header + "\n" + BODY)
                self.run_critic()
                self.assertIsNone(self.stop())

    def test_brief_below_header_window_does_not_count(self):
        self.start_turn("write the report")
        self.write_deliverable("proj/report.md", "line\n" * 70 + "BRIEF: late\n" + BODY)
        self.run_critic()
        self.assertIn("BRIEF", self.stop()["reason"])

    def test_brief_in_status_md_naming_the_file(self):
        self.make_file("proj/STATUS.md", "# Status\n\nBRIEF report\nReader: ops\n")
        self.start_turn("write the report")
        self.write_deliverable("proj/deep/er/report.md")
        self.run_critic()
        self.assertIsNone(self.stop())

    def test_brief_in_status_md_for_another_file_does_not_count(self):
        self.make_file("proj/STATUS.md", "BRIEF summary-deck\nReader: ops\n")
        self.start_turn("write the report")
        self.write_deliverable("proj/report.md")
        self.run_critic()
        self.assertIn("BRIEF", self.stop()["reason"])

    def test_brief_check_can_be_disabled(self):
        self.write_config({"exit_gate": {"require_brief": False}})
        self.start_turn("write the report")
        self.write_deliverable()
        self.run_critic()
        self.assertIsNone(self.stop())

    # ── (a) length: never blocks, deferred to the next prompt ─────────────
    def test_long_answer_does_not_block_but_is_noted_next_prompt(self):
        self.start_turn("fix the script")
        self.assertIsNone(self.stop(LONG))
        ctx = self.next_prompt_context()
        self.assertIn("400 words", ctx)
        self.assertIn("conclusion in the first line", ctx)
        self.assertEqual(self.next_prompt_context(), "")  # delivered once

    def test_long_answer_is_fine_when_explanation_was_asked(self):
        for prompt in ("explain how the cache works", "walk me through it",
                       "what are my options here", "explícame el flujo",
                       "hazlo paso a paso", "compara las dos"):
            with self.subTest(prompt=prompt):
                self.start_turn(prompt)
                self.assertIsNone(self.stop(LONG))
                self.assertEqual(self.next_prompt_context(), "")

    def test_code_and_tables_do_not_count_as_prose(self):
        self.start_turn("fix the script")
        message = ("Fixed.\n```python\n" + "x = 1\n" * 400 + "```\n"
                   + "| a | b |\n" * 300 + "`" + "tok " * 30 + "`")
        self.stop(message)
        self.assertEqual(self.next_prompt_context(), "")

    def test_word_threshold_is_configurable(self):
        self.write_config({"exit_gate": {"max_words": 3}})
        self.start_turn("fix the script")
        self.stop("one two three four five")
        self.assertIn("limit 3", self.next_prompt_context())

    # ── escape hatches ─────────────────────────────────────────────────────
    def test_stop_hook_active_passes(self):
        self.start_turn("write the report")
        self.write_deliverable()
        self.assertIsNone(self.stop(stop_hook_active=True))

    def test_gives_up_after_max_blocks(self):
        self.write_config({"exit_gate": {"max_blocks": 2}})
        self.start_turn("write the report")
        self.write_deliverable()
        self.assertIsNotNone(self.stop())
        self.assertIsNotNone(self.stop())
        self.assertIsNone(self.stop())

    def test_block_counter_resets_each_turn(self):
        self.write_config({"exit_gate": {"max_blocks": 1}})
        self.start_turn("write the report")
        self.write_deliverable("proj/a.md")
        self.assertIsNotNone(self.stop())
        self.assertIsNone(self.stop())
        self.start_turn("write another")
        self.write_deliverable("proj/b.md")
        self.assertIsNotNone(self.stop())

    # ── logging ─────────────────────────────────────────────────────────────
    def test_every_decision_is_logged(self):
        self.start_turn("write the report")
        self.write_deliverable()
        self.stop()
        self.stop(stop_hook_active=True)
        records = self.log_records()
        # stop_hook_active no longer releases the gate on its own: it keeps
        # blocking until the critic runs or max_blocks is reached.
        self.assertEqual([r["blocked"] for r in records], [True, True])
        self.assertEqual(set(records[0]["checks"]), {"critic", "brief"})

    def test_stop_hook_active_does_not_release_after_own_block(self):
        self.start_turn("write the report")
        self.write_deliverable()
        self.assertIsNotNone(self.stop())
        self.assertIsNotNone(self.stop(stop_hook_active=True))


    # ── evasion ─────────────────────────────────────────────────────────────
    def test_deliverable_written_outside_write_tool_blocks(self):
        self.start_turn("write the report")
        self.make_file("proj/report.md", BODY)  # e.g. a Bash heredoc: no PostToolUse
        self.assertIsNotNone(self.stop())

    def test_deliverable_written_outside_write_tool_passes_after_critic(self):
        self.start_turn("write the report")
        self.make_file("proj/report.md", "<!-- BRIEF: ops team -->\n" + BODY)
        self.run_critic()
        self.assertIsNone(self.stop())

    def test_renamed_deliverable_blocks(self):
        self.start_turn("write the report")
        path = self.write_deliverable()
        os.rename(path, os.path.join(os.path.dirname(path), "report-final.md"))
        self.assertIsNotNone(self.stop())

    def test_negated_skip_does_not_waive_the_critic(self):
        self.start_turn("Do NOT skip the critic on this one")
        self.write_deliverable(text="<!-- BRIEF: ops team -->\n" + BODY)
        self.assertIn("CRITIC", self.stop()["reason"])

    def test_user_prompt_starting_with_angle_bracket_opens_a_new_turn(self):
        self.start_turn("write the report, skip critic")
        self.start_turn("<div>new request</div> write the memo")
        self.write_deliverable("proj/memo.md", "<!-- BRIEF: ops team -->\n" + BODY)
        self.assertIn("CRITIC", self.stop()["reason"])

    def test_critic_name_must_match_whole(self):
        self.start_turn("write the report")
        self.write_deliverable(text="<!-- BRIEF: ops team -->\n" + BODY)
        self.run_critic("not-a-spec-critic-rubber-stamp")
        self.assertIn("CRITIC", self.stop()["reason"])

    def test_status_brief_must_name_the_file_not_a_longer_one(self):
        self.make_file("proj/STATUS.md", "BRIEF board-report.md\nReader: ops\n")
        self.start_turn("write the report")
        self.write_deliverable("proj/report.md")
        self.run_critic()
        self.assertIn("BRIEF", self.stop()["reason"])


if __name__ == "__main__":
    import unittest
    unittest.main()
