"""
Shared plumbing for every hook in this repository.

Three rules hold for all of them, and this module is where they are enforced:

  1. FAIL OPEN. A hook that crashes must never cost the user a turn. Every entry
     point runs through `run()`, which turns any exception into a silent exit 0.
     The one deliberate exception is the AWS guard, which fails CLOSED on input it
     cannot parse, because there the safe direction is "no".
  2. STDLIB ONLY. No dependencies, so installing the harness is copying a folder.
  3. STATE LIVES IN ONE PLACE. Logs and per-session state go under `state_dir()`,
     never next to the code and never in the project being worked on.

Configuration is one JSON file, looked for in this order (first match wins):
  $HARNESS_CONFIG  →  ~/.claude/harness.config.json  →  harness.config.json next to hooks/
A missing or malformed file is not an error: every hook has working defaults.

This module also owns the one definition of an INTERNAL WORKING FILE (status
notes, handoffs, specs, scratch folders), shared by every hook that must tell
those apart from deliverables written for someone outside the session.

Configuration:
  "harness" section:
    state_dir       where logs and per-session state live  (~/.claude/harness-state)
  "deliverables" section (read through `is_internal`):
    internal_names  file-name globs treated as internal, case-insensitive
                    (INTERNAL_NAMES)
    internal_dirs   folder-name globs treated as internal, case-insensitive
                    (INTERNAL_DIRS)
"""
from __future__ import annotations

import fnmatch
import json
import os
import sys
import time
from typing import Any, Callable

HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(HOOKS_DIR)


# ── configuration ────────────────────────────────────────────────────────────

def _config_candidates() -> list[str]:
    return [p for p in (
        os.environ.get("HARNESS_CONFIG"),
        os.path.expanduser("~/.claude/harness.config.json"),
        os.path.join(REPO_DIR, "harness.config.json"),
    ) if p]


_CONFIG: dict | None = None


def config() -> dict:
    """The whole configuration object, or {} when there is none."""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = {}
        for path in _config_candidates():
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    _CONFIG = data
                    break
            except FileNotFoundError:
                continue
            except Exception as e:  # malformed: warn, fall back to defaults
                print(f"harness: could not read {path}: {e}", file=sys.stderr)
                break
    return _CONFIG


def setting(section: str, key: str, default: Any) -> Any:
    """`config()[section][key]`, or `default` when absent or of the wrong type."""
    value = (config().get(section) or {}).get(key, default)
    if default is not None and not isinstance(value, type(default)):
        return default
    return value


def state_dir(*parts: str) -> str:
    """Where logs and per-session state live. Created on demand."""
    base = os.path.expanduser(setting("harness", "state_dir", "~/.claude/harness-state"))
    path = os.path.join(base, *parts)
    os.makedirs(path, exist_ok=True)
    return path


# ── internal working files ───────────────────────────────────────────────────

# File names that mark a working file rather than a deliverable. Talking about
# the session, its rounds and its decisions is correct in these.
INTERNAL_NAMES = [
    "status.*", "readme*", "claude.*", "agents.*", "spec.*", "project-spec.*",
    "handoff*", "next_session*", "changelog*", "decisions.*", "todo*", "notes*",
    "note.*", "*_notes.*", "*_internal.*", "feedback.*", "memory.*", "scratch*", "_*",
]
# Folders whose contents are working material, build output or tooling.
INTERNAL_DIRS = [
    ".claude", ".git", "_drafts", "_private", "_archive", "scratchpad", "tmp",
    "node_modules", "dist", "build", "sessions", "memory",
]


def _matches_any(name: str, patterns: Any) -> bool:
    name = name.lower()
    return isinstance(patterns, list) and any(
        isinstance(p, str) and fnmatch.fnmatchcase(name, p.lower()) for p in patterns)


def _judged_dirs(path: str, cwd: str) -> list[str]:
    """The folders of `path` that are judged, relative to the project or home.

    Only folders BELOW the project root (or, outside the project, below the home
    folder) count: a project that lives under ~/tmp or /build is not itself a
    scratch area, and neither is a system temp dir above home. Outside both, the
    whole path is judged.
    """
    real = os.path.realpath(path)
    parent = os.path.dirname(real)
    for base in (cwd, os.path.expanduser("~")):
        if not base:
            continue
        b = os.path.realpath(base)
        if real.startswith(b.rstrip(os.sep) + os.sep):
            parent = os.path.relpath(parent, b)
            break
    return [p for p in parent.replace("\\", "/").split("/") if p and p != "."]


def is_internal(path: str, cwd: str = "") -> bool:
    """True when `path` is an internal working file, by name or by location.

    Names and folders come from the "deliverables" section (`internal_names`,
    `internal_dirs`); a configured list replaces the default, it is not merged.
    A relative `path` is resolved against `cwd`.
    """
    if not path:
        return False
    if cwd and not os.path.isabs(path):
        path = os.path.join(cwd, path)
    names = setting("deliverables", "internal_names", INTERNAL_NAMES)
    if _matches_any(os.path.basename(path), names):
        return True
    dirs = setting("deliverables", "internal_dirs", INTERNAL_DIRS)
    return any(_matches_any(d, dirs) for d in _judged_dirs(path, cwd))


# ── event I/O ────────────────────────────────────────────────────────────────

def read_event() -> dict:
    """The hook's JSON input from stdin, or {} when it is empty or malformed."""
    try:
        data = json.loads(sys.stdin.read() or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def emit(payload: dict) -> None:
    """Write the hook's JSON output. Claude Code reads stdout on exit 0."""
    print(json.dumps(payload, ensure_ascii=False))


def add_context(event_name: str, text: str, notice: str | None = None) -> None:
    """Inject `text` into the model's context; optionally show `notice` to the user."""
    payload: dict = {"hookSpecificOutput": {"hookEventName": event_name,
                                            "additionalContext": text}}
    if notice:
        payload["systemMessage"] = notice
    emit(payload)


def permission(decision: str, reason: str) -> None:
    """PreToolUse verdict: "deny" or "ask". This harness never emits "allow"."""
    emit({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                 "permissionDecision": decision,
                                 "permissionDecisionReason": reason}})


def written_path(event: dict) -> str:
    """The file a Write/Edit tool call touched, from its response or its input."""
    response = event.get("tool_response") or {}
    tool_input = event.get("tool_input") or {}
    if not isinstance(response, dict):
        response = {}
    return str(response.get("filePath") or tool_input.get("file_path") or "")


def log(name: str, record: dict) -> None:
    """Append one JSON line to `<state_dir>/<name>.jsonl`. Never raises."""
    try:
        with open(os.path.join(state_dir(), f"{name}.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), **record}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def run(main: Callable[[], int | None]) -> None:
    """Entry point wrapper: any exception becomes a silent exit 0 (fail open)."""
    try:
        code = main()
    except SystemExit as e:
        raise e
    except Exception as e:
        if os.environ.get("HARNESS_DEBUG"):
            raise
        print(f"harness: {os.path.basename(sys.argv[0])} failed open: {e}", file=sys.stderr)
        code = 0
    sys.exit(code or 0)
