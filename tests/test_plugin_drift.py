"""
plugin_drift must: stay silent when unconfigured or in sync; report changed,
new and cache-only files per plugin; ignore noise files; pick the copy Claude
Code actually loads; and, with --apply, copy source over cache without ever
deleting from the cache or touching its `.in_use` marker. Every test runs
against a fake cache under the temporary HOME.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest

from helpers import HOOKS, HookTestCase

SCRIPT = "plugin_drift.py"
EVENT = {"hook_event_name": "SessionStart", "source": "startup"}


class DriftTestCase(HookTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = os.path.join(self.tmp, "src", "notes-kit")
        self.cache = os.path.join(self.tmp, ".claude", "plugins", "cache",
                                  "local-market", "notes-kit", "1.0.0")
        for root in (self.source, self.cache):
            self.put(root, "plugin.json", '{"name": "notes-kit"}')
            self.put(root, "skills/summary/SKILL.md", "Summarize the file.\n")
        self.write_config({"plugin_drift": {"sources": {"notes-kit": self.source}}})

    def put(self, root: str, rel: str, text: str) -> str:
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def read(self, root: str, rel: str) -> str:
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            return f.read()

    def hook(self) -> dict | None:
        code, out, _ = self.run_hook(SCRIPT, EVENT)
        self.assertEqual(code, 0)
        return out

    def cli(self, *args: str) -> tuple[int, str]:
        env = {**os.environ, "HARNESS_CONFIG": self.config_path, "HOME": self.tmp}
        r = subprocess.run([sys.executable, os.path.join(HOOKS, SCRIPT), *args],
                           input="", capture_output=True, text=True, env=env, timeout=30)
        return r.returncode, r.stdout


class Detection(DriftTestCase):
    def test_unconfigured_is_silent(self):
        self.write_config({})
        self.put(self.source, "plugin.json", "{}")
        self.assertIsNone(self.hook())

    def test_in_sync_is_silent(self):
        self.assertIsNone(self.hook())

    def test_changed_file_reported(self):
        self.put(self.source, "skills/summary/SKILL.md", "Summarize in three bullets.\n")
        out = self.hook()
        self.assertIn("plugin_drift", out["systemMessage"])
        self.assertIn("--apply", out["systemMessage"])
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("notes-kit", ctx)
        self.assertIn("changed: " + os.path.join("skills", "summary", "SKILL.md"), ctx)

    def test_added_and_cache_only_files_reported(self):
        self.put(self.source, "skills/new/SKILL.md", "New skill.\n")
        self.put(self.cache, "old.md", "Removed from source.\n")
        ctx = self.hook()["hookSpecificOutput"]["additionalContext"]
        self.assertIn("new: " + os.path.join("skills", "new", "SKILL.md"), ctx)
        self.assertIn("only in loaded copy: old.md", ctx)

    def test_noise_files_ignored(self):
        for rel in (".DS_Store", "__pycache__/x.pyc", "plugin.json.bak", ".git/HEAD",
                    "node_modules/a/index.js", "notes.bak-1"):
            self.put(self.source, rel, "noise")
        self.put(self.cache, ".in_use", "session-1")
        self.assertIsNone(self.hook())

    def test_missing_source_reported(self):
        self.write_config({"plugin_drift": {"sources": {"notes-kit": "/nonexistent/src"}}})
        out = self.hook()
        self.assertIn("source folder not found",
                      out["hookSpecificOutput"]["additionalContext"])

    def test_registry_install_path_wins(self):
        # An older version folder with drift exists, but the registry says the
        # loaded copy is 1.0.0, which is in sync.
        old = os.path.join(os.path.dirname(self.cache), "0.9.0")
        self.put(old, "plugin.json", "stale")
        now = time.time()
        os.utime(old, (now + 60, now + 60))  # newest by mtime, to prove the registry wins
        registry = os.path.join(self.tmp, ".claude", "plugins", "installed_plugins.json")
        with open(registry, "w") as f:
            json.dump({"version": 2, "plugins": {"notes-kit@local-market": [
                {"installPath": self.cache, "version": "1.0.0"}]}}, f)
        self.assertIsNone(self.hook())

    def test_newest_version_used_without_registry(self):
        newer = os.path.join(os.path.dirname(self.cache), "1.1.0")
        self.put(newer, "plugin.json", '{"name": "notes-kit"}')
        self.put(newer, "skills/summary/SKILL.md", "Different.\n")
        now = time.time()
        os.utime(newer, (now + 60, now + 60))
        # 1.0.0 is in sync, so any report proves the newer 1.1.0 was compared.
        ctx = self.hook()["hookSpecificOutput"]["additionalContext"]
        self.assertIn("changed: " + os.path.join("skills", "summary", "SKILL.md"), ctx)

    def test_marketplace_qualified_key(self):
        self.write_config({"plugin_drift": {"sources": {"notes-kit@other-market": self.source}}})
        out = self.hook()
        self.assertIn("no installed copy", out["hookSpecificOutput"]["additionalContext"])

    def test_custom_cache_root(self):
        root = os.path.join(self.tmp, "alt-cache")
        alt = os.path.join(root, "m", "notes-kit", "2.0.0")
        self.put(alt, "plugin.json", "different")
        self.write_config({"plugin_drift": {"sources": {"notes-kit": self.source},
                                            "cache_root": root}})
        self.assertIn("changed: plugin.json",
                      self.hook()["hookSpecificOutput"]["additionalContext"])

    def test_malformed_event_fails_open(self):
        code, out, _ = self.run_hook(SCRIPT, "not json")
        self.assertEqual(code, 0)


class Apply(DriftTestCase):
    def test_check_exits_one_on_drift_without_writing(self):
        self.put(self.source, "plugin.json", '{"name": "notes-kit", "v": 2}')
        code, out = self.cli("--check")
        self.assertEqual(code, 1)
        self.assertEqual(self.read(self.cache, "plugin.json"), '{"name": "notes-kit"}')

    def test_apply_copies_and_never_deletes(self):
        self.put(self.source, "skills/summary/SKILL.md", "Updated.\n")
        self.put(self.source, "skills/new/SKILL.md", "Brand new.\n")
        self.put(self.cache, "old.md", "Only in cache.\n")
        self.put(self.cache, ".in_use", "session-1")
        self.put(self.source, ".in_use", "should never be copied")
        code, out = self.cli("--apply")
        self.assertEqual(code, 0)
        self.assertIn("Restart", out)
        self.assertEqual(self.read(self.cache, "skills/summary/SKILL.md"), "Updated.\n")
        self.assertEqual(self.read(self.cache, "skills/new/SKILL.md"), "Brand new.\n")
        self.assertEqual(self.read(self.cache, "old.md"), "Only in cache.\n")
        self.assertEqual(self.read(self.cache, ".in_use"), "session-1")
        # Afterwards only the cache-only file remains as a difference.
        ctx = self.hook()["hookSpecificOutput"]["additionalContext"]
        self.assertIn("0 changed, 0 new, 1 only in the loaded copy", ctx)

    def test_apply_in_sync_is_a_no_op(self):
        code, out = self.cli("--apply")
        self.assertEqual(code, 0)
        self.assertIn("matches", out)

    def test_cli_unconfigured(self):
        self.write_config({})
        code, out = self.cli("--check")
        self.assertEqual(code, 0)
        self.assertIn("no plugin sources configured", out)


if __name__ == "__main__":
    unittest.main()
