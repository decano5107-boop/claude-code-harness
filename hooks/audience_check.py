#!/usr/bin/env python3
"""
Warns when a deliverable reads as if it were written for the person in the chat
rather than for its real reader.

The failure it catches is common and quiet. Someone asks for a document FOR a
team, a manager or a client; the work is iterated over many turns; and the
final file ends up narrating that iteration. The real reader then meets "as we
discussed", "QA round 2", "approved by Alex" or "your threshold", none of which
means anything outside the conversation where it was born.

The hook cannot know who the reader is. It looks for the SYMPTOMS of writing to
the wrong person, weights them, and warns when the total crosses a threshold:

  - It never blocks and never rewrites. A false positive must not cost a turn.
  - Internal working files (status notes, handoffs, specs, scratch folders) are
    out of scope: in those, talking about "us" and "the last round" is correct.
  - Code blocks and inline code are quoted material, not prose, and are ignored.

Two modes:
  hook   PostToolUse on Write|Edit|MultiEdit.
  CLI    python3 audience_check.py FILE...   (prints findings; exit 1 if any)

Configuration ("audience_check" section):
  threshold           total symptom weight needed to warn          (3)
  min_chars           shorter files are notes, not deliverables     (400)
  names               extra names that should not appear            ([])
  extensions          files considered at all
                      (.md .markdown .html .htm .txt .rst)
  chat_language_words function words of the language you chat in, to catch it
                      leaking into an English document. The default is a Spanish
                      example list (CHAT_MARKERS); replace it with your own
                      language's words, or set [] to turn the check off.
  language_min_ratio  share of chat-language function words, relative to
                      English ones, above which a mixed register is flagged (0.02).

Which files are internal is shared with ledger_track.py and defined once, in
_common.py; it is configured in the "deliverables" section:
  internal_names      file-name globs treated as internal  (_common.INTERNAL_NAMES)
  internal_dirs       folder-name globs treated as internal, judged below the
                      project root or home folder          (_common.INTERNAL_DIRS)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (add_context, is_internal, log, read_event, run,  # noqa: E402
                     setting, written_path)

SECTION = "audience_check"

DEFAULT_EXT = [".md", ".markdown", ".html", ".htm", ".txt", ".rst"]
MAX_BYTES = 4_000_000

# Logins that say nothing about the person, so they are never treated as a name.
GENERIC_LOGINS = {"root", "user", "admin", "runner", "ubuntu", "ec2-user", "vscode",
                  "codespace", "node", "app", "build", "ci"}


def _rx(*parts: str) -> re.Pattern:
    return re.compile("|".join(parts), re.I | re.M)


def _rx_cs(*parts: str) -> re.Pattern:
    """Case-SENSITIVE. Required wherever the rule looks for a proper name:
    under re.I, [A-Z] also matches lower case and "approved by the board"
    would read as a person."""
    return re.compile("|".join(parts), re.M)


# Each rule: (symptom, weight, pattern, advice). Weight 3 is for what never
# belongs in an external deliverable; weight 2 needs a second signal to warn.
RULES = [
    (
        "reference to the conversation", 3,
        _rx(
            r"\bas (?:we|you) (?:discussed|agreed|asked|requested|said|decided)\b",
            r"\byou (?:asked|requested|wanted|mentioned|said|told me|were right)\b",
            r"\bwe (?:agreed|decided|discussed|iterated|settled on)\b",
            r"\bper your (?:request|instructions?|feedback|decision)\b",
            r"\bcomo (?:acordamos|hablamos|quedamos|dijiste|pediste)\b",
            r"\bme (?:pediste|dijiste|indicaste)\b",
            r"\bt[uú] (?:pediste|dijiste|marcaste|aprobaste|corregiste)\b",
            r"\bseg[uú]n (?:tu|lo que) (?:pediste|acordamos)\b",
        ),
        "the reader was not in that conversation; state the fact, not its origin",
    ),
    (
        "process scaffolding", 3,
        _rx(
            r"\b(?:step|paso|phase|fase) \d+[a-z]\b",
            r"\b(?:QA |review |feedback )?(?:round|ronda) \d+\b",
            r"\b(?:draft|iteration) v?\d+\b",
            r"\b(?:borrador|iteraci[oó]n) v?\d+\b",
            r"\bthe spec\b",
            r"\bel spec\b",
            r"\bthe (?:first|second|final) pass\b",
        ),
        "this names how the work was done, not what the document is; drop or translate it",
    ),
    (
        "a person cited as authority", 3,
        _rx_cs(
            r"\b(?:[Aa]greed with|[Aa]pproved by|[Dd]ecided by|[Rr]equested by|"
            r"[Aa]s instructed by|[Aa]cordado con|[Aa]probado por)\s+[A-Z][a-z]{2,}\b",
            # Mid-sentence only (after a lower-case word or comma), so a heading
            # such as "Changes approved" is not mistaken for a person.
            r"(?<=[a-z,] )[A-Z][a-z]{2,}\s+(?:was right|decided that|approved|"
            r"asked for|corrected|rejected|set the bar)\b",
        ),
        "the reader does not know who that is or why they decide; cite the reason instead",
    ),
    (
        "first-person narration of the work", 2,
        _rx(
            r"^\s*(?:first|then|next|finally)[, ].{0,30}\bI\b",
            r"\bI (?:decided|chose|built|wrote|ran|found|corrected|retracted|measured)\b",
            r"\bmy (?:mistake|error|hypothesis|earlier claim)\b",
            r"\byo (?:decid[ií]|constru[ií]|escrib[ií]|correg[ií]|encontr[eé])\b",
            r"\bmi (?:error|hip[oó]tesis|recomendaci[oó]n)\b",
        ),
        "a deliverable describes the subject, not the person who produced it",
    ),
    (
        "second person", 2,
        _rx(
            r"\byour (?:bar|threshold|correction|verdict|criterion|rule|call|instinct)\b",
            r"\bt[uú]s? (?:correcci[oó]n|criterio|regla|umbral)\b",
            r"\bte (?:entrego|dejo|aviso|paso)\b",
        ),
        "the document is addressing the requester, not its reader",
    ),
]

# Chat-register leakage: function words of the conversation language inside an
# English document. They are short, frequent and topic-free, which makes them a
# reliable signal that a sentence was pasted from the chat. The default list is
# Spanish, as an example; `chat_language_words` replaces it.
CHAT_MARKERS = re.compile(
    r"\b(?:que|para|porque|cuando|donde|entonces|tambi[eé]n|adem[aá]s|"
    r"seg[uú]n|desde|hasta|aunque|mientras|siempre|nunca)\b", re.I)
EN_MARKERS = re.compile(
    r"\b(?:the|and|for|with|that|this|from|which|when|where|because|"
    r"should|would|there|these|those)\b", re.I)
MIN_EN_WORDS = 40
MIN_CHAT_WORDS = 4


def chat_markers() -> re.Pattern | None:
    """The configured chat-language words as one pattern; None turns the check off."""
    words = setting(SECTION, "chat_language_words", None)
    if not isinstance(words, list):
        return CHAT_MARKERS
    words = [w.strip() for w in words if isinstance(w, str) and w.strip()]
    if not words:
        return None
    return re.compile(r"\b(?:" + "|".join(map(re.escape, words)) + r")\b", re.I)


def user_names(cwd: str = "") -> set[str]:
    """Names of the person at the keyboard, from git, $USER and configuration.

    Derived rather than hard-coded so the hook works for anyone. Only
    capitalised forms are kept because matching is case-sensitive: a login
    like "mark" must not fire on the verb "mark".
    """
    names: set[str] = set()
    try:
        r = subprocess.run(["git", "config", "user.name"], capture_output=True,
                           text=True, timeout=3, cwd=cwd or None)
        if r.returncode == 0:
            names.update(p for p in r.stdout.split() if p.isalpha() and len(p) >= 3)
    except Exception:
        pass
    login = os.environ.get("USER", "")
    if login.isalpha() and len(login) >= 3 and login.lower() not in GENERIC_LOGINS:
        names.add(login[0].upper() + login[1:])
    for n in setting(SECTION, "names", []):
        if isinstance(n, str) and n.strip():
            names.add(n.strip())
    return {n for n in names if n[:1].isupper()}


def _prose(text: str) -> str:
    """Blank out fenced and inline code, keeping newlines so line numbers hold."""
    def blank(m: re.Match) -> str:
        return re.sub(r"[^\n]", " ", m.group(0))
    text = re.sub(r"```.*?```|~~~.*?~~~", blank, text, flags=re.S)
    return re.sub(r"`[^`\n]{1,200}`", blank, text)


def _where(text: str, pos: int) -> tuple[int, str]:
    line_no = text.count("\n", 0, pos) + 1
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    line = text[start:end if end != -1 else len(text)].strip()
    return line_no, (line[:77] + "...") if len(line) > 80 else line


def analyze(text: str, names: set[str]) -> list[dict]:
    """Returns findings: [{symptom, weight, line, snippet, advice}]."""
    prose = _prose(text)
    findings: list[dict] = []

    def add(symptom: str, weight: int, pos: int, advice: str) -> None:
        line, snippet = _where(prose, pos)
        findings.append({"symptom": symptom, "weight": weight, "line": line,
                         "snippet": snippet, "advice": advice})

    for symptom, weight, pattern, advice in RULES:
        m = pattern.search(prose)
        if m:
            add(symptom, weight, m.start(), advice)

    for name in sorted(names):
        m = re.search(r"\b" + re.escape(name) + r"\b", prose)
        if m:
            add("the user's own name", 3, m.start(),
                "unless they sign the document, the reader does not know who this is")
            break

    markers = chat_markers()
    chat = markers.findall(prose) if markers else []
    english = len(EN_MARKERS.findall(prose))
    ratio = setting(SECTION, "language_min_ratio", 0.02)
    if (markers and english >= MIN_EN_WORDS and len(chat) >= MIN_CHAT_WORDS
            and ratio * english <= len(chat) < english / 4):
        m = markers.search(prose)
        add("chat language inside an English document", 2, m.start() if m else 0,
            "the conversation's register leaked into the deliverable")
    return findings


def check(path: str, cwd: str = "") -> list[dict] | None:
    """Findings for `path`, or None when the file is out of scope."""
    exts = {e.lower() for e in setting(SECTION, "extensions", DEFAULT_EXT)}
    if not path or os.path.splitext(path)[1].lower() not in exts:
        return None
    if not os.path.isfile(path) or is_internal(path, cwd):
        return None
    if os.path.getsize(path) > MAX_BYTES:
        return None
    with open(path, encoding="utf-8", errors="ignore") as f:
        text = f.read()
    if len(text) < setting(SECTION, "min_chars", 400):
        return None
    return analyze(text, user_names(os.path.dirname(os.path.abspath(path))))


def report(path: str, findings: list[dict]) -> tuple[str, str]:
    """(one-line notice for the user, full text for the model)."""
    notice = ("audience_check: %s reads as written for this conversation, "
              "not for its reader." % os.path.basename(path))
    lines = [notice]
    for f in findings[:5]:
        lines.append("  - %s (L%d: %r) -> %s"
                     % (f["symptom"], f["line"], f["snippet"], f["advice"]))
    lines.append("  Test: if a sentence only makes sense to someone who saw this "
                 "session, cut it or translate it into a property of the document. "
                 "If the file is internal, name or place it as such.")
    return notice, "\n".join(lines)


def hook() -> int:
    event = read_event()
    path = written_path(event)
    findings = check(path, str(event.get("cwd") or ""))
    if not findings:
        return 0
    weight = sum(f["weight"] for f in findings)
    if weight < setting(SECTION, "threshold", 3):
        return 0  # one weak signal alone is not worth interrupting for
    log("audience_check", {"path": path, "weight": weight,
                           "symptoms": [f["symptom"] for f in findings]})
    notice, text = report(path, findings)
    add_context("PostToolUse", text, notice=notice)
    return 0


def cli(paths: list[str]) -> int:
    threshold = setting(SECTION, "threshold", 3)
    flagged = 0
    for path in paths:
        findings = check(path, os.getcwd())
        if findings is None:
            print("[--] %s - out of scope (internal, short or not prose)" % path)
            continue
        weight = sum(f["weight"] for f in findings)
        if weight >= threshold:
            flagged = 1
            print("[!!] weight=%d %s" % (weight, report(path, findings)[1]))
        else:
            print("[ok] %s%s" % (path, " (weight %d, below threshold)" % weight if weight else ""))
    return flagged


if __name__ == "__main__":
    run(lambda: cli(sys.argv[1:]) if len(sys.argv) > 1 else hook())
