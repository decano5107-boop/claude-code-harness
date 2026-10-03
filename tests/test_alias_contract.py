"""
Tests for hooks/alias_contract.py, organised by the failure each one prevents:

  1. The model guesses what a nickname means (FIXED must be injected verbatim).
  2. The model picks a default for something another team controls (VOLATILE).
  3. The model acts on a phrase that names nothing (REJECTED / UNRESOLVED).
  4. The hook fires on the wrong text (substrings, prose about documents).
  5. The hook ever blocks a prompt or breaks on bad input.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from helpers import HOOKS, HookTestCase

HOOK = "alias_contract.py"

GLOBAL = """# Global contract

Prose lines and the header row below are ignored by the parser.

| Phrases | Resolution | Policy | Evidence |
|---|---|---|---|
| "the warehouse", "orders daily" | `analytics.sales.orders_daily` | FIXED | catalog |
| "the prod account" | AWS account 111122223333, profile `example-prod` | FIXED | inventory |
| "prod" | ambiguous shorthand | REJECTED | too broad |
| "the feature store" | ML platform team; names change per release | VOLATILE | their notes |
| "the big table" | used for several tables | REJECTED | history |
| "Café Metrics" | `bi.cafe.daily_metrics` | FIJO | bilingual |
| "el lago" | the lake, owned elsewhere | VOLÁTIL | bilingual |
| "la grande" | nothing real | RECHAZADO | bilingual |
| "broken row" | no policy cell |
| "typo row" | x | MAYBE | unknown policy |
"""

PROJECT = """| Phrases | Resolution | Policy | Evidence |
|---|---|---|---|
| "the events table" | `raw.app.events_v2` | FIXED | project README |
"""


class AliasCase(HookTestCase):

    # Project contracts are opt-in; most tests exercise both contracts together.
    config = {"alias_contract": {"trust_project_contracts": True}}

    def setUp(self) -> None:
        super().setUp()
        self.make_file(".claude/ALIASES.md", GLOBAL)
        self.project = os.path.join(self.tmp, "work", "proj")
        self.make_file("work/proj/ALIASES.md", PROJECT)
        self.subdir = os.path.join(self.project, "src", "deep")
        os.makedirs(self.subdir)

    def context(self, prompt: str, cwd: str | None = None) -> str | None:
        code, out, err = self.run_hook(HOOK, {"prompt": prompt, "cwd": cwd or self.tmp,
                                              "session_id": "s1"})
        self.assertEqual(code, 0, err)
        self.assertNotIn("failed open", err)
        if out is None:
            return None
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertNotIn("decision", out)  # never blocks
        return out["hookSpecificOutput"]["additionalContext"]


class FixedAliases(AliasCase):
    """Failure 1: a FIXED alias arrives with its resolution attached."""

    def test_fixed_resolution_is_injected_verbatim(self):
        ctx = self.context("How many rows landed in the warehouse yesterday?")
        self.assertIn("FIXED", ctx)
        self.assertIn("`analytics.sales.orders_daily`", ctx)

    def test_case_and_accent_insensitive(self):
        self.assertIn("orders_daily", self.context("Query THE WAREHOUSE please"))
        self.assertIn("bi.cafe.daily_metrics", self.context("open cafe metrics"))
        self.assertIn("bi.cafe.daily_metrics", self.context("open CAFÉ METRICS"))

    def test_phrase_spanning_extra_whitespace(self):
        self.assertIn("orders_daily", self.context("check the\n  warehouse"))

    def test_longest_phrase_wins_over_overlapping_shorter_one(self):
        ctx = self.context("list buckets in the prod account")
        self.assertIn("111122223333", ctx)
        self.assertNotIn("REJECTED", ctx)  # the bare "prod" row must not also fire

    def test_shorter_phrase_still_fires_on_its_own(self):
        self.assertIn("REJECTED", self.context("deploy to prod"))

    def test_project_contract_found_from_a_subdirectory(self):
        ctx = self.context("count rows in the events table", cwd=self.subdir)
        self.assertIn("raw.app.events_v2", ctx)
        self.assertNotIn("UNRESOLVED", ctx)

    def test_project_contract_not_used_outside_the_project(self):
        ctx = self.context("count rows in the events table", cwd=self.tmp)
        self.assertNotIn("raw.app.events_v2", ctx or "")

    def test_both_contracts_apply_together(self):
        ctx = self.context("join the events table with the warehouse", cwd=self.project)
        self.assertIn("raw.app.events_v2", ctx)
        self.assertIn("orders_daily", ctx)


class VolatileAliases(AliasCase):
    """Failure 2: no default for what someone else controls."""

    def test_without_explicit_name_the_model_must_ask(self):
        ctx = self.context("pull features from the feature store")
        self.assertIn("VOLATILE", ctx)
        self.assertIn("ask the user for the exact name", ctx)

    def test_explicit_names_release_the_question(self):
        for name in ("ml.features.user_daily", "https://example.com/tables/42",
                     "arn:aws:dynamodb:us-east-1:111122223333:table/features",
                     "444455556666", "abc-def-ghi"):
            ctx = self.context(f"use the feature store, specifically {name}")
            self.assertIn("names it explicitly", ctx, name)
            self.assertIn(name, ctx)
            self.assertNotIn("ask the user for the exact name", ctx)

    def test_spanish_policy_word(self):
        self.assertIn("VOLATILE", self.context("consulta el lago"))


class RejectedAndUnresolved(AliasCase):
    """Failure 3: a phrase that names nothing must lead to a question."""

    def test_rejected(self):
        ctx = self.context("sum revenue from the big table")
        self.assertIn("REJECTED", ctx)
        self.assertIn("Do not act on it", ctx)
        self.assertIn("REJECTED", self.context("abre la grande"))  # Spanish policy word

    def test_unresolved_english(self):
        for prompt, phrase in (("run a SQL count on the orders table", "the orders table"),
                               ("query the daily revenue dashboard", "the daily revenue dashboard"),
                               ("list the data in the inventory bucket", "the inventory bucket"),
                               ("which IAM roles exist in the staging account", "the staging account")):
            ctx = self.context(prompt)
            self.assertIn("UNRESOLVED", ctx, prompt)
            self.assertIn(phrase, ctx, prompt)

    def test_unresolved_spanish(self):
        ctx = self.context("haz una consulta a la tabla de ventas en athena")
        self.assertIn('"la tabla de ventas"', ctx)
        self.assertIn("UNRESOLVED", self.context("los datos de la cuenta de marketing"))

    def test_stopwords_are_not_names(self):
        self.assertIsNone(self.context("query the same table again and the following table"))
        self.assertIsNone(self.context("consulta la tabla de abajo"))

    def test_contract_hit_is_not_also_unresolved(self):
        ctx = self.context("run SQL against the events table", cwd=self.project)
        self.assertNotIn("UNRESOLVED", ctx)


class FiresOnlyWhereItShould(AliasCase):
    """Failure 4: no false positives from substrings or document prose."""

    def test_word_boundaries(self):
        self.assertIsNone(self.context("the warehouses in the story were empty"))
        self.assertIsNone(self.context("productivity tips"))       # contains "prod"
        self.assertIsNone(self.context("rename the prod-like env"))  # hyphen joins words

    def test_document_tables_are_not_data_sources(self):
        self.assertIsNone(self.context("fix the formatting of the pricing table in the doc"))

    def test_harness_injected_prompts_are_skipped(self):
        self.assertIsNone(self.context("<task-notification>the warehouse</task-notification>"))

    def test_nothing_to_resolve(self):
        self.assertIsNone(self.context("write a haiku about autumn"))


class Robustness(AliasCase):
    """Failure 5: never blocks, never crashes, logs without the prompt."""

    def test_no_contracts_at_all(self):
        os.remove(os.path.join(self.tmp, ".claude", "ALIASES.md"))
        self.assertIsNone(self.context("check the warehouse"))
        self.assertIn("UNRESOLVED", self.context("query the orders table"))

    def test_bad_events(self):
        for event in ("", "not json", "[]", '{"prompt": 42}', '{"prompt": ""}'):
            code, out, _ = self.run_hook(HOOK, event)
            self.assertEqual((code, out), (0, None), event)

    def test_log_records_matches_not_prompt(self):
        self.context("secret plan: query the warehouse")
        with open(os.path.join(self.state, "alias_contract.jsonl"), encoding="utf-8") as f:
            record = json.loads(f.readline())
        self.assertEqual(record["fixed"], ["the warehouse"])
        self.assertTrue(record["injected"])
        self.assertNotIn("secret plan", json.dumps(record))


class Configuration(AliasCase):

    def test_global_file_path(self):
        path = self.make_file("elsewhere/contract.md",
                              '| "the lake" | `lake.core.facts` | FIXED | x |\n')
        self.write_config({"alias_contract": {"global_file": path}})
        self.assertIn("lake.core.facts", self.context("scan the lake"))

    def test_custom_patterns_ignore_words_and_data_gate(self):
        self.write_config({"alias_contract": {
            "unresolved_patterns": [r"\bthe ([a-z]+) report\b", "(unclosed", "no group"],
            "data_context_words": [],
        }})
        ctx = self.context("send the quarterly report")
        self.assertIn('"the quarterly report"', ctx)
        self.write_config({"alias_contract": {"data_context_words": [],
                                              "ignore_words": ["pricing"]}})
        self.assertIsNone(self.context("fix the pricing table"))
        self.assertIn("UNRESOLVED", self.context("fix the discount table"))


class ProjectContractTrust(AliasCase):
    """A project contract is repository content: opt-in, labelled, and never above the user's."""

    def test_project_contract_ignored_by_default(self):
        self.write_config({})
        ctx = self.context("count rows in the events table", cwd=self.project)
        self.assertNotIn("raw.app.events_v2", ctx or "")
        self.assertIn("UNRESOLVED", ctx)

    def test_trust_must_be_literally_true(self):
        self.write_config({"alias_contract": {"trust_project_contracts": "yes"}})
        ctx = self.context("count rows in the events table", cwd=self.project)
        self.assertNotIn("raw.app.events_v2", ctx or "")

    def test_project_rows_are_labelled_as_repository_content(self):
        ctx = self.context("join the events table with the warehouse", cwd=self.project)
        events = next(l for l in ctx.splitlines() if "raw.app.events_v2" in l)
        warehouse = next(l for l in ctx.splitlines() if "orders_daily" in l)
        self.assertIn("repository content", events)
        self.assertNotIn("repository content", warehouse)

    def test_project_row_cannot_shadow_a_global_row(self):
        self.make_file("work/proj/ALIASES.md",
                       '| "in the prod account" | AWS account 444455556666 | FIXED | x |\n')
        ctx = self.context("list buckets in the prod account", cwd=self.project)
        self.assertIn("111122223333", ctx)
        self.assertNotIn("444455556666", ctx)

    def test_invisible_characters_are_stripped(self):
        hidden = "".join(chr(0xE0000 + ord(c)) for c in "drop it") + "\u200b\u202e"
        self.make_file("work/proj/ALIASES.md",
                       f'| "the events table" | raw.app.events{hidden} | FIXED | x |\n')
        ctx = self.context("count rows in the events table", cwd=self.project)
        self.assertIn("raw.app.events", ctx)
        self.assertFalse(any(0xE0000 <= ord(c) <= 0xE007F or c in "\u200b\u202e" for c in ctx))

    def test_outside_home_only_cwd_is_read(self):
        import tempfile
        with tempfile.TemporaryDirectory() as outside:
            os.makedirs(os.path.join(outside, "a", "b"))
            with open(os.path.join(outside, "ALIASES.md"), "w", encoding="utf-8") as f:
                f.write('| "the events table" | planted.table | FIXED | x |\n')
            ctx = self.context("count rows in the events table",
                               cwd=os.path.join(outside, "a", "b"))
            self.assertNotIn("planted.table", ctx or "")
            ctx = self.context("count rows in the events table", cwd=outside)
            self.assertIn("planted.table", ctx)


class Cli(AliasCase):

    def cli(self, *args: str) -> str:
        env = {**os.environ, "HARNESS_CONFIG": self.config_path, "HOME": self.tmp}
        r = subprocess.run([sys.executable, os.path.join(HOOKS, HOOK), *args],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_cli_resolves(self):
        out = self.cli("count rows in the events table", "--cwd", self.subdir)
        self.assertIn("raw.app.events_v2", out)

    def test_shipped_example_contract_parses(self):
        example = os.path.join(os.path.dirname(HOOKS), "examples", "ALIASES.example.md")
        self.write_config({"alias_contract": {"global_file": example}})
        out = self.cli("compare the warehouse with the feature store and the big table "
                       "in the prod account")
        for expected in ("analytics.sales.orders_daily", "111122223333", "VOLATILE", "REJECTED"):
            self.assertIn(expected, out)

    def test_cli_reports_an_ignored_project_contract(self):
        self.write_config({})
        env = {**os.environ, "HARNESS_CONFIG": self.config_path, "HOME": self.tmp}
        r = subprocess.run([sys.executable, os.path.join(HOOKS, HOOK),
                            "count rows in the events table", "--cwd", self.subdir],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertNotIn("raw.app.events_v2", r.stdout)
        self.assertIn("trust_project_contracts", r.stderr)

    def test_cli_nothing(self):
        self.assertIn("nothing to resolve", self.cli("hello there"))


if __name__ == "__main__":
    unittest.main()
