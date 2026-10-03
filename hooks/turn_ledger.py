"""
The turn ledger: a small per-session JSON record of what happened in the current
turn. This is a module shared by several hooks, not a hook itself.

Why it exists: the Stop hook has to know what the turn did (was a deliverable
written? did a critic review it?), and it cannot reliably learn that from the
transcript. Claude Code writes the transcript asynchronously, so at the moment a
Stop hook fires it may not yet contain the turn in progress. Instead, the hooks
that witness each event record the fact here as it happens, and the Stop hook
reads this file.

Layout: one file per session, `<state_dir>/ledger/<session>.json`:

    prompt, cwd, ts      the user prompt that opened the turn, and where
    deliverables         external deliverables written during THIS turn
    critics              critic runs during THIS turn: [{"agent", "ts"}]
    blocks               how many times the Stop hook blocked in THIS turn
    seen                 every deliverable written during the SESSION
    reviewed             {path: ts} deliverables a critic has reviewed (SESSION)

`reset()` clears the per-turn fields at every user prompt but keeps `seen` and
`reviewed`, so that a small follow-up edit to a deliverable a critic already
reviewed does not demand a fresh review.

Nothing here raises. A ledger that cannot be read looks like an empty turn, and
a write that fails is dropped: a hook must never cost the user a turn because of
its bookkeeping.
"""
from __future__ import annotations

import json
import os
import time

from _common import setting, state_dir

MAX_AGE_S = 7 * 24 * 3600  # ledgers of sessions idle for a week are swept
DEFAULT_CRITICS = ["spec-critic"]


def _empty() -> dict:
    return {"prompt": "", "cwd": "", "ts": 0.0, "deliverables": [], "critics": [],
            "blocks": 0, "seen": [], "reviewed": {}}


def _dir() -> str:
    return state_dir("ledger")


def _safe(session: str) -> str:
    # Session ids come from the event payload; keep them from escaping the directory.
    safe = "".join(c for c in (session or "") if c.isalnum() or c in "-_")[:80]
    return safe or "no-session"


def _path(session: str, suffix: str = ".json") -> str:
    return os.path.join(_dir(), _safe(session) + suffix)


def read(session: str) -> dict:
    """The session's ledger, with every field present. Never raises."""
    d = _empty()
    try:
        with open(_path(session), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for key, default in _empty().items():
                value = data.get(key, default)
                d[key] = value if isinstance(value, type(default)) else default
    except Exception:
        pass
    return d


def _write(session: str, d: dict) -> None:
    # Write-then-rename: a reader never sees a half-written file, even if two
    # hooks of the same session run concurrently.
    tmp = ""
    try:
        path = _path(session)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            if tmp:
                os.remove(tmp)
        except OSError:
            pass


def reset(session: str, prompt: str = "", cwd: str = "") -> None:
    """Start a new turn: clear per-turn fields, keep session-level memory."""
    prev = read(session)
    d = _empty()
    d.update(prompt=(prompt or "")[:4000], cwd=cwd or "", ts=time.time(),
             seen=prev["seen"], reviewed=prev["reviewed"])
    _write(session, d)
    sweep()


def add_deliverable(session: str, path: str) -> None:
    d = read(session)
    changed = False
    for key in ("deliverables", "seen"):
        if path not in d[key]:
            d[key].append(path)
            changed = True
    if changed:
        _write(session, d)


def mark_critic(session: str, agent: str) -> None:
    """A critic ran: every deliverable seen so far in the session counts as reviewed."""
    d = read(session)
    now = time.time()
    d["critics"].append({"agent": agent, "ts": now})
    for p in set(d["seen"]) | set(d["deliverables"]):
        d["reviewed"][p] = now
    _write(session, d)


def bump_block(session: str) -> int:
    d = read(session)
    d["blocks"] = int(d.get("blocks") or 0) + 1
    _write(session, d)
    return d["blocks"]


def is_critic(agent: str) -> bool:
    """Whether a subagent type counts as an independent critic.

    Plugin-namespaced agents ("myplugin:spec-critic") count without listing
    every prefix.
    """
    names = setting("exit_gate", "critic_agents", DEFAULT_CRITICS)
    agent = (agent or "").lower()
    # The name itself, or the name under a plugin prefix ("myplugin:spec-critic");
    # never a substring ("not-a-spec-critic-rubber-stamp").
    return bool(agent) and any(
        isinstance(n, str) and n and (agent == n.lower() or agent.endswith(":" + n.lower()))
        for n in names)


# ── deferred notes ───────────────────────────────────────────────────────────
# A Stop hook cannot retract a message the user has already read; it can only
# ask for another one. Feedback about the shape of an answer is therefore
# parked here and delivered with the next prompt instead.

def defer_note(session: str, text: str) -> None:
    try:
        with open(_path(session, ".note"), "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass


def pop_note(session: str) -> str:
    """The parked note, removed on read; "" when there is none."""
    path = _path(session, ".note")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
        os.remove(path)
        return text
    except Exception:
        return ""


def sweep(max_age_s: float = MAX_AGE_S) -> None:
    """Delete ledger files untouched for longer than `max_age_s`. Never raises."""
    try:
        cutoff = time.time() - max_age_s
        base = _dir()
        for name in os.listdir(base):
            p = os.path.join(base, name)
            try:
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except OSError:
                continue
    except Exception:
        pass
