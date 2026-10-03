#!/usr/bin/env python3
"""
Detects a local plugin that was edited in its source folder while Claude Code
keeps loading an older copy.

Installing a plugin from a local folder does not link to that folder: Claude
Code copies it into ~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/
and loads the copy from then on. Editing the source afterwards changes nothing
the session sees, and nothing says so. This hook compares the two at session
start and reports the files that differ.

Two modes:
  hook   SessionStart. Reports drift to the user and the model; silent when
         everything matches or nothing is configured. Never modifies anything.
  CLI    python3 plugin_drift.py --check    report (exit 1 on drift)
         python3 plugin_drift.py --apply    copy changed and new files from the
                                            source over the cache copy

--apply is deliberately conservative:
  - It copies over the top and never deletes. A file that exists only in the
    cache is reported, and removing it is left to a proper reinstall.
  - The `.in_use` marker Claude Code keeps in a cache folder is never compared,
    copied or overwritten.
  - A running session has already loaded the old copy, so the session must be
    restarted to pick up the change.

Which copy is "the installed one" comes from installed_plugins.json next to the
cache folder when it names one; otherwise the most recently modified version
folder of each marketplace is used.

Configuration ("plugin_drift" section):
  sources     {"<plugin>" or "<plugin>@<marketplace>": "<source folder>"}  ({})
  cache_root  where installed copies live           (~/.claude/plugins/cache)
  ignore      file/folder name globs never compared
              ([".git", "__pycache__", "node_modules", ".DS_Store", "*.bak*",
              "*.pyc", ".in_use"]; .in_use is ignored even when omitted)
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import add_context, log, read_event, run, setting  # noqa: E402

SECTION = "plugin_drift"
DEFAULT_CACHE_ROOT = "~/.claude/plugins/cache"
DEFAULT_IGNORE = [".git", "__pycache__", "node_modules", ".DS_Store", "*.bak*",
                  "*.pyc", ".in_use"]
IN_USE = ".in_use"  # always ignored, even if a custom ignore list omits it
MAX_LISTED = 12


def _ignored(name: str, patterns: list[str]) -> bool:
    return name == IN_USE or any(fnmatch.fnmatch(name, p) for p in patterns)


def snapshot(root: str, patterns: list[str]) -> dict[str, str]:
    """{relative path: sha256} for every file under `root` that is not noise."""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not _ignored(d, patterns))
        for name in filenames:
            if _ignored(name, patterns):
                continue
            full = os.path.join(dirpath, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            h = hashlib.sha256()
            with open(full, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 16), b""):
                    h.update(chunk)
            out[os.path.relpath(full, root)] = h.hexdigest()
    return out


def installed_copies(cache_root: str, plugin_key: str) -> list[str]:
    """Folders Claude Code loads for this plugin."""
    plugin, _, market = plugin_key.partition("@")
    found: list[str] = []
    registry = os.path.join(os.path.dirname(cache_root.rstrip(os.sep)), "installed_plugins.json")
    try:
        with open(registry, encoding="utf-8") as f:
            entries = (json.load(f) or {}).get("plugins") or {}
        for key, installs in entries.items():
            name, _, mk = str(key).partition("@")
            if name != plugin or (market and mk != market):
                continue
            for inst in installs if isinstance(installs, list) else []:
                path = (inst or {}).get("installPath") if isinstance(inst, dict) else None
                if path and os.path.isdir(path) and path not in found:
                    found.append(path)
    except (OSError, ValueError, AttributeError):
        pass
    if found:
        return found

    # No registry entry: take the newest version folder under each marketplace.
    try:
        markets = sorted(os.listdir(cache_root))
    except OSError:
        return []
    for mk in markets:
        if market and mk != market:
            continue
        pdir = os.path.join(cache_root, mk, plugin)
        if not os.path.isdir(pdir):
            continue
        versions = [os.path.join(pdir, v) for v in os.listdir(pdir)
                    if os.path.isdir(os.path.join(pdir, v))]
        if versions:
            found.append(max(versions, key=os.path.getmtime))
    return found


def compare(source: str, cache: str, patterns: list[str]) -> dict[str, list[str]]:
    s, c = snapshot(source, patterns), snapshot(cache, patterns)
    return {
        "changed": sorted(r for r in s if r in c and s[r] != c[r]),
        "added": sorted(r for r in s if r not in c),
        "cache_only": sorted(r for r in c if r not in s),
    }


def apply(source: str, cache: str, diff: dict[str, list[str]]) -> int:
    copied = 0
    for rel in diff["changed"] + diff["added"]:
        dst = os.path.join(cache, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(source, rel), dst)
        copied += 1
    return copied


def scan(do_apply: bool) -> tuple[list[str], int]:
    """Returns (report lines, number of drifted files)."""
    sources = setting(SECTION, "sources", {})
    cache_root = os.path.expanduser(setting(SECTION, "cache_root", DEFAULT_CACHE_ROOT))
    patterns = [p for p in setting(SECTION, "ignore", DEFAULT_IGNORE) if isinstance(p, str)]
    lines: list[str] = []
    drift = 0
    for key, src in sorted(sources.items()):
        if not isinstance(src, str):
            continue
        source = os.path.expanduser(src)
        if not os.path.isdir(source):
            lines.append("%s: source folder not found (%s)" % (key, src))
            continue
        copies = installed_copies(cache_root, key)
        if not copies:
            lines.append("%s: no installed copy under %s" % (key, cache_root))
            continue
        for cache in copies:
            diff = compare(source, cache, patterns)
            n = len(diff["changed"]) + len(diff["added"])
            if not n and not diff["cache_only"]:
                continue
            drift += n + len(diff["cache_only"])
            lines.append("%s: the loaded copy differs from the source (%d changed, "
                         "%d new, %d only in the loaded copy)"
                         % (key, len(diff["changed"]), len(diff["added"]),
                            len(diff["cache_only"])))
            listed = ([("changed", r) for r in diff["changed"]]
                      + [("new", r) for r in diff["added"]]
                      + [("only in loaded copy", r) for r in diff["cache_only"]])
            for kind, rel in listed[:MAX_LISTED]:
                lines.append("    %s: %s" % (kind, rel))
            if len(listed) > MAX_LISTED:
                lines.append("    ... and %d more" % (len(listed) - MAX_LISTED))
            if do_apply and n:
                copied = apply(source, cache, diff)
                lines.append("    copied %d file(s) into %s" % (copied, cache))
            if diff["cache_only"]:
                lines.append("    files only in the loaded copy are never deleted; "
                             "reinstall the plugin to drop them")
    return lines, drift


def hook() -> int:
    read_event()  # SessionStart carries nothing this hook needs; drain stdin
    lines, drift = scan(do_apply=False)
    if not lines:
        return 0
    log("plugin_drift", {"drifted_files": drift, "report": lines})
    script = os.path.abspath(__file__)
    notice = ("plugin_drift: a local plugin was edited but this session loads the old "
              "copy. Fix: python3 %s --apply, then restart the session." % script)
    add_context("SessionStart", notice + "\n" + "\n".join(lines), notice=notice)
    return 0


def cli(argv: list[str]) -> int:
    do_apply = "--apply" in argv
    lines, drift = scan(do_apply)
    if not setting(SECTION, "sources", {}):
        print("plugin_drift: no plugin sources configured (plugin_drift.sources)")
        return 0
    for line in lines:
        print(line)
    if not lines:
        print("plugin_drift: every configured plugin matches its source")
        return 0
    if do_apply:
        print("Restart the Claude Code session to load the updated copy.")
        return 0
    print("Run with --apply to copy the source over the loaded copy.")
    return 1 if drift else 0


def _is_cli() -> bool:
    # A hook always gets JSON on stdin; a person at a terminal gets a CLI.
    return len(sys.argv) > 1 or sys.stdin.isatty()


if __name__ == "__main__":
    run(lambda: cli(sys.argv[1:]) if _is_cli() else hook())
