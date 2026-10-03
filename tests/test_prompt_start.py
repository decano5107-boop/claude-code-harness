"""
Contract of prompt_start.py (UserPromptSubmit): it opens a new turn in the
ledger (except for harness-injected messages), logs prompts only when opted in,
delivers the deferred long-answer note once, flags configured dictation errors,
and prints nothing when there is nothing to say.
"""
from __future__ import annotations

import json
import os

from helpers import HookTestCase

HOOK = "prompt_start.py"
SESSION = "sess-prompt"


class PromptStartTest(HookTestCase):
    config = {"prompt_start": {"glossary": {"cloud code": "Claude Code", "pie chart": "PyChart"}}}

    def submit(self, prompt: str, session: str = SESSION) -> tuple[int, str | None]:
        code, out, _ = self.run_hook(HOOK, {"session_id": session, "prompt": prompt,
                                            "cwd": self.tmp,
                                            "hook_event_name": "UserPromptSubmit"})
        if out is None:
            return code, None
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        return code, out["hookSpecificOutput"]["additionalContext"]

    def ledger(self) -> dict:
        with open(os.path.join(self.state, "ledger", f"{SESSION}.json")) as f:
            return json.load(f)

    def test_plain_prompt_is_silent_and_opens_turn(self):
        self.assertEqual(self.submit("rename the variable"), (0, None))
        d = self.ledger()
        self.assertEqual(d["prompt"], "rename the variable")
        self.assertEqual(d["cwd"], self.tmp)

    def test_new_prompt_clears_previous_turn(self):
        self.submit("first")
        path = os.path.join(self.state, "ledger", f"{SESSION}.json")
        d = self.ledger()
        d.update(deliverables=["/x.md"], blocks=2, seen=["/x.md"], reviewed={"/x.md": 1})
        with open(path, "w") as f:
            json.dump(d, f)
        self.submit("second")
        d = self.ledger()
        self.assertEqual((d["deliverables"], d["blocks"]), ([], 0))
        self.assertEqual(d["reviewed"], {"/x.md": 1})

    def test_harness_message_does_not_open_a_turn(self):
        self.submit("write the report")
        self.submit("<task-notification>background job finished</task-notification>")
        self.assertEqual(self.ledger()["prompt"], "write the report")

    def test_prompts_not_logged_by_default(self):
        self.submit("secret project details")
        self.assertFalse(os.path.exists(os.path.join(self.state, "prompts.jsonl")))

    def test_prompts_logged_when_opted_in(self):
        self.write_config({"prompt_start": {"log_prompts": True}})
        self.submit("three word prompt")
        with open(os.path.join(self.state, "prompts.jsonl")) as f:
            rec = json.loads(f.readline())
        self.assertEqual(rec["text"], "three word prompt")
        self.assertEqual(rec["words"], 3)
        self.assertEqual(rec["session"], SESSION)
        self.assertEqual(rec["cwd"], self.tmp)

    def test_glossary_hit_is_flagged(self):
        _, ctx = self.submit("open Cloud Code and draw the pie chart")
        self.assertIn("'Cloud Code' likely means 'Claude Code'".lower(), ctx.lower())
        self.assertIn("'pie chart' likely means 'PyChart'", ctx)
        self.assertEqual(len(ctx.splitlines()), 1)

    def test_glossary_respects_word_boundaries(self):
        self.assertEqual(self.submit("the soundcloud coder"), (0, None))
        self.assertEqual(self.submit("use pie charts here"), (0, None))

    def test_glossary_ignores_bad_entries(self):
        self.write_config({"prompt_start": {"glossary": {"": "x", "foo": 3, "bar": "baz"}}})
        _, ctx = self.submit("foo and bar")
        self.assertIn("'bar' likely means 'baz'", ctx)
        self.assertNotIn("foo", ctx)

    def test_glossary_not_a_dict_is_ignored(self):
        self.write_config({"prompt_start": {"glossary": ["cloud code"]}})
        self.assertEqual(self.submit("cloud code"), (0, None))

    def test_deferred_note_delivered_once(self):
        os.makedirs(os.path.join(self.state, "ledger"), exist_ok=True)
        with open(os.path.join(self.state, "ledger", f"{SESSION}.note"), "w") as f:
            f.write("[exit_gate] too long")
        self.assertEqual(self.submit("next task"), (0, "[exit_gate] too long"))
        self.assertEqual(self.submit("next task"), (0, None))

    def test_deferred_note_belongs_to_its_session(self):
        os.makedirs(os.path.join(self.state, "ledger"), exist_ok=True)
        with open(os.path.join(self.state, "ledger", "other.note"), "w") as f:
            f.write("not yours")
        self.assertEqual(self.submit("next task"), (0, None))

    def test_fails_open_on_malformed_stdin(self):
        for stdin in ("", "{{", "[]", '"text"'):
            code, out, _ = self.run_hook(HOOK, stdin)
            self.assertEqual((code, out), (0, None), stdin)

    def test_missing_prompt_field_is_silent(self):
        code, out, _ = self.run_hook(HOOK, {"session_id": SESSION})
        self.assertEqual((code, out), (0, None))


if __name__ == "__main__":
    import unittest
    unittest.main()
