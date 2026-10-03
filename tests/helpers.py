"""
Test plumbing: run a hook the way Claude Code runs it — a subprocess, JSON on
stdin, JSON (or nothing) on stdout — inside an isolated state directory, so no
test ever reads or writes the real ~/.claude.

Run the whole suite with:  python3 -m unittest discover -s tests
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

HOOKS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks")


class HookTestCase(unittest.TestCase):
    """Gives every test a private state dir and config file, removed afterwards."""

    config: dict = {}

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.state = os.path.join(self.tmp, "state")
        self.write_config(self.config)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_config(self, cfg: dict) -> None:
        cfg = json.loads(json.dumps(cfg))  # deep copy
        cfg.setdefault("harness", {})["state_dir"] = self.state
        self.config_path = os.path.join(self.tmp, "harness.config.json")
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def run_hook(self, script: str, event: dict | str, *args: str,
                 env: dict | None = None) -> tuple[int, dict | None, str]:
        """Returns (exit code, parsed stdout JSON or None, stderr)."""
        full_env = {**os.environ, "HARNESS_CONFIG": self.config_path,
                    "HOME": self.tmp, **(env or {})}
        full_env.pop("HARNESS_DEBUG", None)
        stdin = event if isinstance(event, str) else json.dumps(event)
        r = subprocess.run([sys.executable, os.path.join(HOOKS, script), *args],
                           input=stdin, capture_output=True, text=True, env=full_env,
                           timeout=30)
        out = r.stdout.strip()
        return r.returncode, (json.loads(out) if out else None), r.stderr

    def make_file(self, name: str, text: str) -> str:
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path
