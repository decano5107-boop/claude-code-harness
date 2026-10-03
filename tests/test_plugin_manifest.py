"""
The plugin manifest is the install path, so it is tested like code:

  - every manifest parses and names the plugin the same way;
  - every hook command in hooks/hooks.json points at a file that exists, and
    runs to exit 0 on an empty event (the plugin root substituted as Claude
    Code substitutes ${CLAUDE_PLUGIN_ROOT});
  - the skill, command and agent files carry the frontmatter Claude Code reads.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import unittest

from helpers import HookTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(rel: str) -> dict:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return json.load(f)


def frontmatter(rel: str) -> dict[str, str]:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        text = f.read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


class Manifests(unittest.TestCase):

    def test_plugin_and_marketplace_agree(self):
        plugin = load(".claude-plugin/plugin.json")
        market = load(".claude-plugin/marketplace.json")
        self.assertEqual([p["name"] for p in market["plugins"]], [plugin["name"]])
        self.assertEqual(market["plugins"][0]["source"], "./")

    def test_skill_command_and_agent_frontmatter(self):
        self.assertEqual(frontmatter("skills/spec/SKILL.md").get("name"), "spec")
        self.assertTrue(frontmatter("skills/spec/SKILL.md").get("description"))
        self.assertTrue(frontmatter("commands/verify-spec.md").get("description"))
        agent = frontmatter("agents/spec-critic.md")
        self.assertEqual(agent.get("name"), "spec-critic")
        self.assertTrue(agent.get("description"))

    def test_exit_gate_default_critic_is_the_shipped_agent(self):
        import sys
        sys.path.insert(0, os.path.join(ROOT, "hooks"))
        import turn_ledger
        self.assertIn(frontmatter("agents/spec-critic.md")["name"], turn_ledger.DEFAULT_CRITICS)


class HookCommands(HookTestCase):

    def commands(self) -> list[str]:
        out = []
        for groups in load("hooks/hooks.json")["hooks"].values():
            for group in groups:
                out += [h["command"] for h in group["hooks"] if h.get("type") == "command"]
        return out

    def test_every_hook_script_exists(self):
        cmds = self.commands()
        self.assertTrue(cmds)
        for cmd in cmds:
            argv = shlex.split(cmd.replace("${CLAUDE_PLUGIN_ROOT}", ROOT))
            self.assertTrue(os.path.isfile(argv[1]), cmd)

    def test_every_hook_runs_on_an_empty_event(self):
        for cmd in self.commands():
            argv = shlex.split(cmd.replace("${CLAUDE_PLUGIN_ROOT}", ROOT))
            if argv[0] == "node" and not shutil.which("node"):
                continue
            env = {**os.environ, "HARNESS_CONFIG": self.config_path, "HOME": self.tmp}
            r = subprocess.run(argv, input="{}", capture_output=True, text=True,
                               env=env, timeout=30, cwd=self.tmp)
            self.assertEqual(r.returncode, 0, (cmd, r.stderr))
            self.assertEqual(r.stdout.strip(), "", cmd)


if __name__ == "__main__":
    unittest.main()
