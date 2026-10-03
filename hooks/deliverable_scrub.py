#!/usr/bin/env python3
"""
Delivery hygiene for files that are about to leave your machine.

Two layers, both applied only to files with a configured extension:

  1. INVISIBLE CHARACTERS in text files. Zero-width characters, word joiners, a
     stray byte-order mark, soft hyphens, bidirectional controls and "exotic"
     spaces are routinely carried in by copy-paste and model output. They break
     pasting into email clients, throw off justification and search, and make
     two identical-looking strings compare unequal. Unicode TAG characters
     (U+E0000-U+E007F) are worse: they render as nothing yet are read by
     language models, which makes them a channel for smuggling hidden
     instructions into a document someone else will feed to an LLM. Exotic
     spaces are normalized to a regular space (deleting them would glue words
     together); everything else is deleted.

  2. IDENTITY METADATA in DOCX/XLSX/PPTX. These are ZIP archives whose
     docProps/core.xml and docProps/app.xml record who created the file, who
     last saved it, their company, their manager and a local hyperlink base
     path. Those fields are emptied (not removed: Office complains when the
     parts or elements are missing) and every other archive member is copied
     through byte for byte.

Two modes:
  hook   PostToolUse on Write|Edit|MultiEdit: scrubs the file just written and reports
         what was removed. Silent when the file was already clean.
  CLI    python3 deliverable_scrub.py FILE... [--check]
         --check reports without writing and exits 1 if anything was found.

Guarantee: a file is never left half-written. Output goes to a temporary file
in the same directory and is moved over the original with an atomic rename; on
any error the original is untouched.

Known limits (stdlib only):
  - Image EXIF/XMP and PDF metadata are not handled. Removing them without
    re-encoding needs external tools (exiftool, qpdf); PDF metadata in
    particular survives a naive edit because PDFs are updated incrementally.
  - Text files that are not valid UTF-8 are skipped rather than guessed at.

Maintenance rule for THIS file and its tests: every codepoint is declared as an
integer and built with chr(). The characters themselves never appear in the
source, not even as escapes, so the scrubber can safely be run over its own code.

Configuration ("scrub" section):
  text_extensions    text files scrubbed of invisible characters
                     (.md .markdown .txt .html .htm .csv .tsv .rst)
  office_extensions  Office files stripped of identity metadata
                     (.docx .xlsx .pptx)
  skip_dirs          folders never touched: a bare name matches any folder on
                     the path, an entry with "/" or "~" is a path prefix
                     ([".git", "node_modules"])
"""
from __future__ import annotations

import fnmatch
import os
import re
import sys
import tempfile
import unicodedata
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import add_context, log, read_event, run, setting, written_path  # noqa: E402

SECTION = "scrub"

DEFAULT_TEXT_EXT = [".md", ".markdown", ".txt", ".html", ".htm", ".csv", ".tsv", ".rst"]
DEFAULT_OFFICE_EXT = [".docx", ".xlsx", ".pptx"]
DEFAULT_SKIP_DIRS = [".git", "node_modules"]

# ── what counts as invisible ─────────────────────────────────────────────────

# Deleted outright: none has a legitimate use in an email, report or deck.
DELETE_CODEPOINTS = (
    0x200B,  # zero width space
    0x200C,  # zero width non-joiner
    0x200D,  # zero width joiner (kept inside emoji sequences, see below)
    0x2060,  # word joiner
    0xFEFF,  # byte-order mark / zero width no-break space
    0x00AD,  # soft hyphen
    0x180E,  # mongolian vowel separator
    0x061C,  # arabic letter mark
    0x200E, 0x200F,                          # left-to-right / right-to-left mark
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E,  # bidi embeddings and overrides
    0x2066, 0x2067, 0x2068, 0x2069,          # bidi isolates
    0x2061, 0x2062, 0x2063, 0x2064,          # invisible math operators
)
TAG_FIRST, TAG_LAST = 0xE0000, 0xE007F

# Replaced by a regular space: deleting them would join the words around them.
SPACE_CODEPOINTS = (
    0x00A0,  # no-break space
    0x202F,  # narrow no-break space
    0x205F,  # medium mathematical space
    0x3000,  # ideographic space
) + tuple(range(0x2000, 0x200B))  # en quad .. hair space

DELETE = frozenset(chr(c) for c in DELETE_CODEPOINTS) | frozenset(
    chr(c) for c in range(TAG_FIRST, TAG_LAST + 1))
TO_SPACE = frozenset(chr(c) for c in SPACE_CODEPOINTS)

ZWJ = chr(0x200D)
VS16 = chr(0xFE0F)
BLACK_FLAG = chr(0x1F3F4)
CANCEL_TAG = chr(0xE007F)

# Rough emoji ranges. Precision is not needed: a zero-width joiner is only kept
# when BOTH neighbours fall in these ranges, which ordinary prose never does.
_EMOJI_RANGES = ((0x2300, 0x23FF), (0x2600, 0x27BF), (0x2B00, 0x2BFF),
                 (0x1F000, 0x1FAFF))
_SKIN_TONES = (0x1F3FB, 0x1F3FF)


def _is_emoji(ch: str) -> bool:
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in _EMOJI_RANGES)


def _emoji_zwj(text: str, i: int) -> bool:
    """True when the joiner at text[i] glues two emoji (family, profession...).
    Removing it would split one pictograph into several."""
    j = i - 1
    while j >= 0 and (text[j] == VS16 or _SKIN_TONES[0] <= ord(text[j]) <= _SKIN_TONES[1]):
        j -= 1
    return j >= 0 and i + 1 < len(text) and _is_emoji(text[j]) and _is_emoji(text[i + 1])


def _flag_tag_run(text: str, i: int) -> int:
    """Length of a subdivision-flag tag sequence starting at text[i], else 0.

    Regional flags (for example England's) are a black flag followed by a few
    tag letters/digits and a cancel tag. That exact shape is kept; it carries a
    short region code and cannot hide free text. Any other tag run is removed.
    """
    if i == 0 or text[i - 1] != BLACK_FLAG:
        return 0
    j = i
    while j < len(text) and j - i < 7:
        o = ord(text[j])
        if 0xE0030 <= o <= 0xE0039 or 0xE0061 <= o <= 0xE007A:
            j += 1
            continue
        break
    if 2 <= j - i <= 6 and j < len(text) and text[j] == CANCEL_TAG:
        return j - i + 1
    return 0


def _label(ch: str) -> str:
    o = ord(ch)
    if TAG_FIRST <= o <= TAG_LAST:
        # Deliberately a count only: echoing the hidden text into the model's
        # context would deliver the very payload this layer exists to stop.
        return "tag characters (hidden text)"
    try:
        return unicodedata.name(ch).lower()
    except ValueError:
        return "U+%04X" % o


def scrub_text(text: str) -> tuple[str, dict[str, int]]:
    """Returns (clean text, {character label: count})."""
    counts: dict[str, int] = {}
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in DELETE or ch in TO_SPACE:
            if ch == ZWJ and _emoji_zwj(text, i):
                out.append(ch)
                i += 1
                continue
            keep = _flag_tag_run(text, i) if TAG_FIRST <= ord(ch) <= TAG_LAST else 0
            if keep:
                out.append(text[i:i + keep])
                i += keep
                continue
            label = _label(ch)
            counts[label] = counts.get(label, 0) + 1
            if ch in TO_SPACE:
                out.append(" ")
        else:
            out.append(ch)
        i += 1
    return "".join(out), counts


# ── writing safely ───────────────────────────────────────────────────────────

def _atomic_write(path: str, data: bytes) -> None:
    """Replace `path` with `data` in one rename, keeping its permission bits."""
    mode = os.stat(path).st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".scrub-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def process_text(path: str, write: bool) -> dict[str, int]:
    with open(path, "rb") as f:
        raw = f.read()
    try:
        original = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {}
    clean, counts = scrub_text(original)
    if counts and write and clean != original:
        _atomic_write(path, clean.encode("utf-8"))
    return counts


# ── office metadata ──────────────────────────────────────────────────────────

# Emptied rather than deleted, because Office flags a missing element or part as
# a damaged file. HyperlinkBase frequently holds the author's local folder path.
IDENTITY_FIELDS = ("dc:creator", "cp:lastModifiedBy", "Company", "Manager", "HyperlinkBase")
_FIELD_RE = re.compile(
    "<(" + "|".join(re.escape(f) for f in IDENTITY_FIELDS) + r")(\s[^>]*)?>(.*?)</\1>", re.S)
PROPERTY_PARTS = ("docProps/core.xml", "docProps/app.xml")


def _empty_fields(xml: str) -> tuple[str, list[str]]:
    touched: list[str] = []

    def sub(m: re.Match) -> str:
        name, attrs = m.group(1), m.group(2) or ""
        if m.group(3).strip():
            touched.append(name)
        return "<%s%s></%s>" % (name, attrs, name)

    return _FIELD_RE.sub(sub, xml), touched


def process_office(path: str, write: bool) -> dict[str, int]:
    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            members = {i.filename: z.read(i.filename) for i in infos}
            comment = z.comment
    except (zipfile.BadZipFile, OSError):
        return {}

    counts: dict[str, int] = {}
    for part in PROPERTY_PARTS:
        if part not in members:
            continue
        try:
            xml = members[part].decode("utf-8")
        except UnicodeDecodeError:
            continue
        new, touched = _empty_fields(xml)
        if touched:
            members[part] = new.encode("utf-8")
            for name in touched:
                key = "metadata " + name
                counts[key] = counts.get(key, 0) + 1

    if counts and write:
        # Rewrite member by member with the ORIGINAL ZipInfo, so order, dates,
        # compression and attributes of everything else are carried through.
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".scrub-")
        os.close(fd)
        try:
            with zipfile.ZipFile(tmp, "w") as z:
                for info in infos:
                    z.writestr(info, members[info.filename])
                z.comment = comment
            with zipfile.ZipFile(tmp) as z:  # verify before replacing anything
                if z.testzip() is not None:
                    raise zipfile.BadZipFile("rewritten archive failed verification")
            os.chmod(tmp, os.stat(path).st_mode & 0o7777)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    return counts


# ── scope ────────────────────────────────────────────────────────────────────

def _exts(key: str, default: list[str]) -> set[str]:
    return {e.lower() if e.startswith(".") else "." + e.lower()
            for e in setting(SECTION, key, default) if isinstance(e, str)}


def _skipped(path: str) -> bool:
    """True when any directory on the path matches a configured skip entry.
    Entries containing a slash or ~ are path prefixes; others match a folder name."""
    real = os.path.realpath(path)
    parts = real.split(os.sep)[:-1]
    for entry in setting(SECTION, "skip_dirs", DEFAULT_SKIP_DIRS):
        if not isinstance(entry, str) or not entry:
            continue
        if "/" in entry or entry.startswith("~"):
            prefix = os.path.realpath(os.path.expanduser(entry))
            if real == prefix or real.startswith(prefix.rstrip(os.sep) + os.sep):
                return True
        elif any(fnmatch.fnmatch(p, entry) for p in parts):
            return True
    return False


def process(path: str, write: bool = True) -> dict[str, int] | None:
    """Scrub one file. Returns counts, or None when the file is out of scope."""
    if not path or not os.path.isfile(path) or _skipped(path):
        return None
    ext = os.path.splitext(path)[1].lower()
    if ext in _exts("text_extensions", DEFAULT_TEXT_EXT):
        return process_text(path, write)
    if ext in _exts("office_extensions", DEFAULT_OFFICE_EXT):
        return process_office(path, write)
    return None


def summary(counts: dict[str, int]) -> str:
    return ", ".join("%s x%d" % (k, v)
                     for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


# ── modes ────────────────────────────────────────────────────────────────────

def hook() -> int:
    event = read_event()
    path = written_path(event)
    try:
        counts = process(path, write=True)
    except Exception as e:  # the original is untouched; report, never break the turn
        log("deliverable_scrub", {"path": path, "error": str(e)})
        return 0
    if not counts:
        return 0
    log("deliverable_scrub", {"path": path, "removed": counts})
    notice = "deliverable_scrub: %s - removed %s" % (os.path.basename(path), summary(counts))
    add_context("PostToolUse",
                notice + ". The file on disk was cleaned after your write; re-read it "
                "before making an exact-match edit.",
                notice=notice)
    return 0


def cli(argv: list[str]) -> int:
    write = "--check" not in argv
    paths = [a for a in argv if not a.startswith("--")]
    if not paths:
        print("usage: deliverable_scrub.py FILE... [--check]", file=sys.stderr)
        return 2
    found = 0
    for path in paths:
        if not os.path.isfile(path):
            print("  ??  %s - not found" % path)
            continue
        try:
            counts = process(path, write=write)
        except Exception as e:
            print("  !!  %s - left untouched: %s" % (path, e))
            continue
        if counts is None:
            print("  --  %s - out of scope (extension or skipped folder)" % path)
        elif counts:
            found += sum(counts.values())
            print("  %s  %s - %s" % ("OK" if write else "!!", path, summary(counts)))
        else:
            print("  ..  %s - clean" % path)
    return 1 if (found and not write) else 0


if __name__ == "__main__":
    run(lambda: cli(sys.argv[1:]) if len(sys.argv) > 1 else hook())
