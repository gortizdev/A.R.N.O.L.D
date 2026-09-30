"""A to-do list, and the weekly document it is filled from.

The list itself is small and dull on purpose: a JSON file of items, each with
a sentence, a tick, and where it came from. What makes it worth a module is
the second half - turning a Word document that arrives by email every week
into those items without anyone retyping them.

Why a folder and not the mailbox: the mail lives in the *new* Outlook, which
has no COM, no local store a process can read, and a Graph API that wants an
app registration and a tenant administrator's consent. But the attachment
does touch the disk, in two places this can watch:

* Outlook's own attachment cache (`%LOCALAPPDATA%\\Microsoft\\Olk\\Attachments`),
  which gets a copy the moment the attachment is opened or previewed.
* Wherever a "save attachments to OneDrive" flow puts it - OneDrive's
  `Email attachments` folder by default - which needs no click at all.

Either way the document is a file, and a file needs nobody's permission.

Two things shape the design:

* **The document is the source of truth for its own items.** Importing this
  week's file replaces last week's items from the same source; a tick on an
  item that is still in the new document survives. Items added by hand are a
  different source and are never touched by an import.
* **The same file is never imported twice.** The list remembers the hash of
  what it last took, so the agent, the dashboard and a `todo.sync` from the
  shell can all scan on their own cadence without stepping on each other.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree

from .graph_mail import MailUnavailable
from .graph_mail import shared as shared_mail

log = logging.getLogger(__name__)

MANUAL_SOURCE = "manual"

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_W14 = "{http://schemas.microsoft.com/office/word/2010/wordml}"

# Glyphs people use as checkboxes in a document, ticked and unticked.
_UNTICKED = "☐□▢◻◽⬜○◯"
_TICKED = "☑☒✓✔✅■◼◾⬛●"
_BULLETS = "•‣◦⁃∙·-*–—▪▫→"

# A line that says it is done, at the end: "... - done", "(complete)".
_TRAILING_DONE = re.compile(
    r"[\s\-–—(\[]*(done|complete|completed|finished)[)\]]?[.!]?\s*$", re.I
)
# Markdown-ish checkbox at the start of a line.
_MD_CHECKBOX = re.compile(r"^\s*(?:[-*+]\s*)?\[(?P<mark>[ xX✓✔])\]\s*")
_MD_BULLET = re.compile(r"^\s*(?:[-*+•◦‣▪]|\d+[.)])\s+")
_MD_HEADING = re.compile(r"^\s*#{1,6}\s+(?P<text>.+?)\s*#*\s*$")

# Headings under which plain paragraphs (not lists) are still worth taking as
# items, for a document that lists its asks in prose.
_TASKY_HEADING = re.compile(
    r"\b(to[\s-]?do|todo|action(s| items?)?|tasks?|next steps?|asks?|"
    r"follow[\s-]?ups?|deliverables?|priorit(y|ies)|this week|reminders?)\b",
    re.I,
)
# A table's first row when it is a header rather than a task.
_HEADER_WORDS = {
    "task", "tasks", "item", "items", "action", "actions", "owner", "status",
    "due", "date", "notes", "note", "priority", "who", "what", "when", "done",
    "description", "deadline", "#", "no", "no.", "progress", "update",
}

SUPPORTED_EXTENSIONS = (".docx", ".txt", ".md", ".pdf")

# A date as people type it in a title: 09/02/2026, 9/2/26, 09-02-2026.
_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[/-](\d{1,2})[/-](\d{4}|\d{2})(?!\d)")
# "Project: task" or "Project - task" on one bullet, for a project with its
# status written inline rather than as a sub-bullet.
# A line that only says there is nothing to do. Faithful to the document
# but not a task, so it is left off the list.
_NOTHING_RE = re.compile(
    r"^(nothing|no updates?|no progress|no change|n/?a|none|same as last week)"
    r"(\s+(to\s+(update|report)|since last (week|meeting|time)|made))?[.!;]?$",
    re.I,
)
_PROJECT_SPLIT_RE = re.compile(r"^(?P<project>[^:\u2013\u2014-]{2,48}?)\s*(?::|\s[\u2013\u2014-]\s)\s*(?P<rest>.+)$")


def _date_in(text: str) -> tuple[float, str] | None:
    """The first date in a line, as (midnight local timestamp, as written)."""
    match = _DATE_RE.search(text or "")
    if not match:
        return None
    month, day, year = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    if year < 100:
        year += 2000
    try:
        return datetime(year, month, day).timestamp(), match.group(0)
    except (ValueError, OverflowError, OSError):
        return None


def _split_project(text: str) -> tuple[str, str]:
    """'Bracketing Evaluation: Ryan to Test' -> ('Bracketing Evaluation', 'Ryan to Test')."""
    match = _PROJECT_SPLIT_RE.match(text)
    if not match:
        return "", text
    return match.group("project").strip(), match.group("rest").strip()


def date_text(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%m/%d/%Y") if ts else ""


@dataclass(slots=True)
class Extracted:
    """One candidate item pulled out of a document."""

    text: str
    section: str = ""
    done: bool = False
    detail: str = ""
    # The date on the nearest line above it that carries one - the title of a
    # weekly update, typically - so a running document can be read one week
    # at a time. Midnight local time, and the text as written.
    date_ts: float | None = None
    date: str = ""


class DocumentError(RuntimeError):
    """The file could not be read as a document. Fit to speak."""


# -- reading documents --------------------------------------------------------


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _strip_marks(text: str) -> tuple[str, bool | None]:
    """Take a leading checkbox glyph or bullet off; say whether it was ticked.

    Returns (text, ticked) with ticked None when there was no checkbox.
    """
    text = text.lstrip()
    ticked: bool | None = None
    while text and (text[0] in _UNTICKED or text[0] in _TICKED or text[0] in _BULLETS):
        if text[0] in _TICKED:
            ticked = True
        elif text[0] in _UNTICKED:
            ticked = False
        text = text[1:].lstrip()
    match = _MD_CHECKBOX.match(text)
    if match:
        ticked = match.group("mark").strip() != ""
        text = text[match.end():]
    if _TRAILING_DONE.search(text) and len(text) > 8:
        stripped = _TRAILING_DONE.sub("", text).rstrip(" -–—(")
        if stripped:
            text, ticked = stripped, True
    return _clean(text), ticked


def _docx_blocks(path: Path) -> list[dict[str, Any]]:
    """The document body as a flat list of paragraphs and table rows, in order.

    Each block: {"kind": "p"|"row", "text": str, "style": str, "list": bool,
    "level": int, "checked": bool|None, "cells": [str]}.
    """
    try:
        with zipfile.ZipFile(path) as bundle:
            raw = bundle.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise DocumentError(f"{path.name} is not a Word document I can read ({exc}).") from exc
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise DocumentError(f"{path.name} has a broken document body ({exc}).") from exc

    body = root.find(f"{_W}body")
    if body is None:
        return []

    def paragraph(node: ElementTree.Element) -> dict[str, Any]:
        ppr = node.find(f"{_W}pPr")
        style = ""
        is_list = False
        level = 0
        if ppr is not None:
            pstyle = ppr.find(f"{_W}pStyle")
            if pstyle is not None:
                style = pstyle.get(f"{_W}val", "") or ""
            numpr = ppr.find(f"{_W}numPr")
            if numpr is not None:
                is_list = True
                ilvl = numpr.find(f"{_W}ilvl")
                try:
                    level = int(ilvl.get(f"{_W}val", "0")) if ilvl is not None else 0
                except ValueError:
                    level = 0
        checked: bool | None = None
        parts: list[str] = []
        for el in node.iter():
            tag = el.tag
            if tag == f"{_W}t":
                parts.append(el.text or "")
            elif tag in (f"{_W}tab", f"{_W}br", f"{_W}cr"):
                parts.append(" ")
            elif tag == f"{_W14}checked":
                checked = el.get(f"{_W14}val", "0") in ("1", "true")
            elif tag == f"{_W}sym":
                # Wingdings checkboxes: F0A8/F06F unticked, F0FE/F0FD ticked.
                char = (el.get(f"{_W}char") or "").upper()
                if char in ("F0A8", "F06F", "F0A2"):
                    checked = False if checked is None else checked
                elif char in ("F0FE", "F0FD", "F0FC"):
                    checked = True
        style_l = style.lower()
        if "list" in style_l and not is_list:
            is_list = True
        return {
            "kind": "p",
            "text": _clean("".join(parts)),
            "style": style,
            "list": is_list,
            "level": level,
            "checked": checked,
            "cells": [],
        }

    blocks: list[dict[str, Any]] = []
    for child in body:
        if child.tag == f"{_W}p":
            blocks.append(paragraph(child))
        elif child.tag == f"{_W}tbl":
            for row in child.iter(f"{_W}tr"):
                cells: list[str] = []
                checked: bool | None = None
                for cell in row.findall(f"{_W}tc"):
                    texts = []
                    for para in cell.iter(f"{_W}p"):
                        block = paragraph(para)
                        if block["checked"] is not None:
                            checked = block["checked"]
                        if block["text"]:
                            texts.append(block["text"])
                    cells.append(" ".join(texts).strip())
                blocks.append({
                    "kind": "row", "text": " ".join(c for c in cells if c), "style": "",
                    "list": False, "level": 0, "checked": checked, "cells": cells,
                })
    return blocks


def _is_heading(block: dict[str, Any]) -> bool:
    style = block["style"].lower()
    return block["kind"] == "p" and (style.startswith("heading") or style == "title")


def _looks_like_header_row(cells: list[str]) -> bool:
    words = [c.strip().lower().rstrip(":") for c in cells if c.strip()]
    if not words:
        return False
    hits = sum(1 for w in words if w in _HEADER_WORDS or len(w.split()) == 1 and len(w) <= 12)
    return hits == len(words) and any(w in _HEADER_WORDS for w in words)


def _items_from_blocks(blocks: list[dict[str, Any]]) -> list[Extracted]:
    """Pick the items out of a document's blocks, best structure first.

    A weekly update is a list two levels deep: a bullet per project, a
    sub-bullet per task. So a top-level bullet followed by sub-bullets is the
    *group* those tasks belong to, and a top-level bullet on its own is a task
    with its project written inline ("ROI Calculator - waiting on Matt"). A
    line with a date on it, outside the list, dates everything that follows,
    so a document that grows a section a week can be read one week at a time.

    Failing a list: table rows; then the paragraphs under a heading that
    sounds like a list of things to do; then every short paragraph, because a
    document with nothing but sentences in it is still a document somebody
    sent.
    """
    section = ""
    date_ts: float | None = None
    date = ""
    listed: list[Extracted] = []
    rows: list[Extracted] = []
    tasky: list[Extracted] = []
    prose: list[Extracted] = []
    header_pending = True
    project = ""
    parent: Extracted | None = None  # a top-level bullet waiting to see if tasks follow

    def flush_parent() -> None:
        nonlocal parent
        if parent is not None:
            own_project, text = _split_project(parent.text)
            parent.section = own_project or section
            parent.text = text
            if not _NOTHING_RE.match(text):
                listed.append(parent)
            parent = None

    def open_parent() -> None:
        """Sub-bullets follow: the bullet above is their project. A status
        written on that same line is a task in its own right."""
        nonlocal parent, project
        if parent is None:
            return
        own_project, text = _split_project(parent.text)
        if own_project:
            project = own_project
            parent.section = own_project
            parent.text = text
            if not _NOTHING_RE.match(text):
                listed.append(parent)
        else:
            project = parent.text.rstrip(":").strip()
        parent = None

    for block in blocks:
        text = block["text"]
        if _is_heading(block):
            flush_parent()
            section = text
            project = ""
            header_pending = True
            found = _date_in(text)
            if found:
                date_ts, date = found
            continue
        if not text:
            continue

        if block["kind"] == "row":
            flush_parent()
            cells = [c for c in block["cells"]]
            if header_pending and _looks_like_header_row(cells):
                header_pending = False
                continue
            header_pending = False
            nonempty = [c for c in cells if c.strip()]
            if not nonempty:
                continue
            main, ticked = _strip_marks(nonempty[0])
            rest = [c for c in nonempty[1:] if c.strip()]
            if not main and rest:
                main, more = _strip_marks(rest[0])
                rest = rest[1:]
                ticked = ticked if ticked is not None else more
            if block["checked"] is not None:
                ticked = block["checked"]
            if not main:
                continue
            if any(r.strip().lower() in ("done", "complete", "completed") for r in rest):
                ticked = True
            rows.append(Extracted(main, section, bool(ticked), " / ".join(rest), date_ts, date))
            continue

        main, ticked = _strip_marks(text)
        if block["checked"] is not None:
            ticked = block["checked"]
        if not main:
            continue
        is_list = block["list"] or block["checked"] is not None or text[0] in _UNTICKED + _TICKED + "\u2022\u25e6"
        if is_list:
            if block["level"] == 0:
                flush_parent()
                project = main.rstrip(":").strip()
                parent = Extracted(main, section, bool(ticked), "", date_ts, date)
            else:
                open_parent()
                if not _NOTHING_RE.match(main):
                    listed.append(Extracted(main, project or section, bool(ticked), "", date_ts, date))
            continue

        # A plain paragraph. One carrying a date starts a dated stretch of the
        # document; the rest are prose, taken only if nothing better turns up.
        found = _date_in(main)
        if found and len(main) <= 80:
            flush_parent()
            date_ts, date = found
            project = ""
            continue
        item = Extracted(main, section, bool(ticked), "", date_ts, date)
        if _TASKY_HEADING.search(section) and len(main) <= 240:
            tasky.append(item)
        elif 3 <= len(main) <= 200 and not main.endswith(":"):
            prose.append(item)

    flush_parent()
    for candidate in (listed, rows, tasky, prose):
        if candidate:
            return candidate
    return []

def _text_items(lines: Iterable[str]) -> list[Extracted]:
    section = ""
    date_ts: float | None = None
    date = ""
    project = ""
    parent: Extracted | None = None
    listed: list[Extracted] = []
    prose: list[Extracted] = []

    def flush_parent() -> None:
        nonlocal parent
        if parent is not None:
            own_project, text = _split_project(parent.text)
            parent.section = own_project or section
            parent.text = text
            if not _NOTHING_RE.match(text):
                listed.append(parent)
            parent = None

    def open_parent() -> None:
        nonlocal parent, project
        if parent is None:
            return
        own_project, text = _split_project(parent.text)
        if own_project:
            project = own_project
            parent.section = own_project
            parent.text = text
            if not _NOTHING_RE.match(text):
                listed.append(parent)
        else:
            project = parent.text.rstrip(":").strip()
        parent = None

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue
        heading = _MD_HEADING.match(line)
        if heading:
            flush_parent()
            section = _clean(heading.group("text"))
            project = ""
            found = _date_in(section)
            if found:
                date_ts, date = found
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        bulleted = bool(_MD_BULLET.match(line) or _MD_CHECKBOX.match(line)
                        or line.lstrip()[:1] in _UNTICKED + _TICKED)
        text = _MD_BULLET.sub("", line, count=1) if _MD_BULLET.match(line) else line
        main, ticked = _strip_marks(text)
        if not main:
            continue
        if bulleted:
            if indent < 2:
                flush_parent()
                project = main.rstrip(":").strip()
                parent = Extracted(main, section, bool(ticked), "", date_ts, date)
            else:
                open_parent()
                if not _NOTHING_RE.match(main):
                    listed.append(Extracted(main, project or section, bool(ticked), "", date_ts, date))
            continue
        found = _date_in(main)
        if found and len(main) <= 80:
            flush_parent()
            date_ts, date = found
            project = ""
            continue
        prose.append(Extracted(main, section, bool(ticked), "", date_ts, date))
    flush_parent()
    return listed or [p for p in prose if len(p.text) <= 200]

def _pdf_lines(path: Path) -> list[str]:
    try:
        import pypdf  # type: ignore
    except ImportError as exc:
        raise DocumentError(
            f"{path.name} is a PDF, and reading PDFs needs the pypdf package "
            "(pip install pypdf)."
        ) from exc
    try:
        reader = pypdf.PdfReader(str(path))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:  # pypdf raises a zoo of its own
        raise DocumentError(f"{path.name} could not be read as a PDF ({exc}).") from exc
    return text.splitlines()


def extract_items(path: str | Path) -> list[Extracted]:
    """Everything in the document that reads as a thing to do."""
    path = Path(path)
    if not path.is_file():
        raise DocumentError(f"There is no file at {path}.")
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return _items_from_blocks(_docx_blocks(path))
    if suffix in (".txt", ".md"):
        try:
            return _text_items(path.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError as exc:
            raise DocumentError(f"{path.name} could not be read ({exc}).") from exc
    if suffix == ".pdf":
        return _text_items(_pdf_lines(path))
    raise DocumentError(
        f"I can read Word documents, PDFs and text files, not {suffix or 'that'}."
    )


def pick_week(
    items: list[Extracted], week_of: float | None, *, name: str = "the document"
) -> tuple[list[Extracted], str]:
    """The items from one dated section, and that date as written.

    Undated documents come back whole. With `week_of`, the section dated in
    that Monday-to-Sunday week is the one wanted, and its absence is an
    error; without it, the newest section is taken.
    """
    dates = sorted({e.date_ts for e in items if e.date_ts})
    if not dates:
        return items, ""
    if week_of is None:
        chosen = dates[-1]
    else:
        start = week_start(week_of)
        in_week = [d for d in dates if start <= d < start + 7 * 86400]
        if not in_week:
            raise DocumentError(
                f"{name} has no update dated this week; its newest is dated {date_text(dates[-1])}."
            )
        chosen = in_week[-1]
    kept = [e for e in items if e.date_ts == chosen]
    return kept, (kept[0].date if kept else date_text(chosen))


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


# -- finding the week's document ---------------------------------------------


def default_watch_folders() -> list[str]:
    """Where an emailed attachment turns up on this machine without help."""
    folders: list[str] = []
    home = Path.home()
    # Every OneDrive synced here - "OneDrive" is the personal one, "OneDrive -
    # <Company>" the work one - and both names the stock Power Automate
    # template uses for its folder.
    roots = sorted(p for p in home.glob("OneDrive*") if p.is_dir()) or [home / "OneDrive"]
    for root in roots:
        for name in ("Email attachments", "Email attachments from Power Automate"):
            folders.append(str(root / name))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        folders.append(str(Path(local) / "Microsoft" / "Olk" / "Attachments"))
    return folders


def expand_folder(text: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(text)))


def find_document(
    folders: Iterable[str | Path],
    match: Iterable[str],
    extensions: Iterable[str] = SUPPORTED_EXTENSIONS,
    *,
    max_depth: int = 4,
    newer_than: float = 0.0,
) -> Path | None:
    """The newest file under any folder whose path mentions one of `match`.

    The match runs on the whole path below the folder, not just the file
    name, so a flow that files things under `Email attachments/geo update/`
    finds them however the attachment itself was named. An empty `match`
    takes any document in the folder: that is what a dedicated folder means.
    """
    tokens = [t.lower() for t in match if t and t.strip()]
    exts = {e.lower() if e.startswith(".") else "." + e.lower() for e in extensions}
    best: tuple[float, Path] | None = None
    for folder in folders:
        root = expand_folder(str(folder))
        if not root.is_dir():
            continue
        root_depth = len(root.parts)
        try:
            walker = os.walk(root)
            for dirpath, dirnames, filenames in walker:
                if len(Path(dirpath).parts) - root_depth >= max_depth:
                    dirnames[:] = []
                for filename in filenames:
                    if filename.startswith("~$"):  # Word's lock file
                        continue
                    candidate = Path(dirpath) / filename
                    if candidate.suffix.lower() not in exts:
                        continue
                    rel = str(candidate.relative_to(root)).lower().replace("_", " ")
                    if tokens and not any(t in rel for t in tokens):
                        continue
                    try:
                        mtime = candidate.stat().st_mtime
                    except OSError:
                        continue
                    if mtime <= newer_than:
                        continue
                    if best is None or mtime > best[0]:
                        best = (mtime, candidate)
        except OSError as exc:
            log.debug("could not walk %s: %s", root, exc)
    return best[1] if best else None


# -- sections and Claude Code projects -------------------------------------------

# Words that say nothing about which project a section is: they appear in
# many section names and in many project folders.
_PROJECT_STOP = {
    "the", "a", "an", "and", "of", "for", "to", "in", "on", "new", "app", "tool",
    "evaluation", "management", "reporting", "report", "project", "projects",
    "development", "dev", "work", "v2", "main", "src",
}


def _words(text: str) -> list[str]:
    return [w for w in re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split() if w not in _PROJECT_STOP]


def match_project(section: str, projects: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The Claude Code project a section most plausibly means, or None.

    Every candidate has a `project` (its spoken name) and a `cwd`. The words
    of the section are matched against the words of both; a prefix match
    counts, so "Calculator" finds "ROI-Calculator". At least half of the
    section's words must hit - "Data Migration / Management" against
    "Ellipse Data" is one word in two, which is a guess, not a match, so
    the bar is strictly more than half when there are two or more words.
    """
    words = _words(section)
    if not words:
        return None
    best: tuple[float, dict[str, Any]] | None = None
    for candidate in projects:
        labels = [str(candidate.get("project") or ""), Path(str(candidate.get("cwd") or "")).name]
        for label in labels:
            tokens = _words(label)
            if not tokens:
                continue
            hits = sum(1 for w in words if any(t.startswith(w) or w.startswith(t) for t in tokens))
            if hits == 0 or (len(words) > 1 and hits / len(words) <= 0.5):
                continue
            score = hits / len(words) + 0.5 * hits / len(tokens)
            if best is None or score > best[0]:
                best = (score, candidate)
    return best[1] if best else None


def section_key(section: str) -> str:
    return _norm(section)


# -- the list -----------------------------------------------------------------


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


@dataclass(slots=True)
class TodoItem:
    id: str
    text: str
    done: bool = False
    section: str = ""
    source: str = MANUAL_SOURCE
    detail: str = ""
    added: float = field(default_factory=time.time)
    done_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "text": self.text, "done": self.done, "section": self.section,
            "source": self.source, "detail": self.detail, "added": self.added,
            "done_at": self.done_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TodoItem | None":
        text = str(raw.get("text") or "").strip()
        if not text:
            return None
        done_at = raw.get("done_at")
        return cls(
            id=str(raw.get("id") or uuid.uuid4().hex[:8]),
            text=text,
            done=bool(raw.get("done")),
            section=str(raw.get("section") or ""),
            source=str(raw.get("source") or MANUAL_SOURCE),
            detail=str(raw.get("detail") or ""),
            added=float(raw.get("added") or time.time()),
            done_at=float(done_at) if isinstance(done_at, (int, float)) else None,
        )


@dataclass(slots=True)
class ImportReport:
    source: str
    file: str
    added: int = 0
    kept: int = 0
    dropped: int = 0
    skipped: bool = False  # the same file again
    dated: str = ""  # the section's date, as written in the document

    @property
    def total(self) -> int:
        return self.added + self.kept


class TodoList:
    """The file of items. Every operation reads, changes and writes: the
    agent, the dashboard and a shell command all share it, and holding it in
    memory in any one of them would make the others wrong."""

    def __init__(self, path: Path | str, max_items: int = 300) -> None:
        self.path = Path(path)
        self.max_items = max(10, int(max_items))

    # -- storage ------------------------------------------------------------

    def _load(self) -> tuple[list[TodoItem], dict[str, Any]]:
        """The file as it is. A file that exists but cannot be read is an
        error, never an empty list: every write starts from a load, and a
        load that shrugged would let the next write erase the list."""
        raw: Any = None
        last: Exception | None = None
        for attempt in range(3):
            try:
                with self.path.open("r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                break
            except FileNotFoundError:
                return [], {}
            except (OSError, json.JSONDecodeError) as exc:
                # Another process may be mid-replace; on Windows that shows
                # as a sharing violation for a moment.
                last = exc
                time.sleep(0.05 * (attempt + 1))
        if last is not None and raw is None:
            raise OSError(f"could not read the to-do list at {self.path}: {last}")
        if not isinstance(raw, dict):
            raise OSError(f"the to-do list at {self.path} is not a JSON object")
        items = [
            item for item in (TodoItem.from_dict(r) for r in raw.get("items") or []
                              if isinstance(r, dict)) if item is not None
        ]
        meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
        return items, meta

    def _write(self, items: list[TodoItem], meta: dict[str, Any]) -> None:
        # Oldest finished items go first when the list is over its cap; an
        # open item is never dropped to make room.
        if len(items) > self.max_items:
            finished = sorted(
                (i for i in items if i.done), key=lambda i: i.done_at or i.added
            )
            for item in finished:
                if len(items) <= self.max_items:
                    break
                items.remove(item)
        payload = {"items": [i.to_dict() for i in items], "meta": meta, "ts": time.time()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".todos-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=1)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # -- reads --------------------------------------------------------------

    def items(self, *, include_done: bool = True) -> list[TodoItem]:
        items, _ = self._load()
        return items if include_done else [i for i in items if not i.done]

    def meta(self) -> dict[str, Any]:
        return self._load()[1]

    def find(self, query: str) -> TodoItem | None:
        """By id first, then the open item whose text best matches the words."""
        query = (query or "").strip()
        if not query:
            return None
        items, _ = self._load()
        for item in items:
            if item.id == query:
                return item
        words = _norm(query).split()
        if not words:
            return None
        best: tuple[int, TodoItem] | None = None
        for item in sorted(items, key=lambda i: i.done):  # open ones first
            hay = _norm(item.text)
            if hay == " ".join(words):
                return item
            score = sum(1 for w in words if w in hay)
            if score and (best is None or score > best[0]):
                best = (score, item)
        return best[1] if best and best[0] == len(words) else (best[1] if best else None)

    def describe(self) -> dict[str, Any]:
        """Everything the dashboard shows, in one read."""
        items, meta = self._load()
        open_items = [i for i in items if not i.done]
        return {
            "items": [i.to_dict() for i in items],
            "open": len(open_items),
            "done": len(items) - len(open_items),
            "sources": meta.get("imports", {}),
            "email": meta.get("email", {}),
            "links": meta.get("links", {}),
            "ts": meta.get("ts"),
        }

    # -- writes -------------------------------------------------------------

    def add(self, text: str, *, section: str = "", source: str = MANUAL_SOURCE) -> TodoItem:
        text = _clean(text)
        if not text:
            raise ValueError("Nothing to add.")
        items, meta = self._load()
        for item in items:
            if not item.done and _norm(item.text) == _norm(text):
                return item  # already on the list; saying so beats a twin
        item = TodoItem(id=uuid.uuid4().hex[:8], text=text[:800], section=section, source=source)
        items.append(item)
        self._write(items, meta)
        return item

    def set_done(self, query: str, done: bool = True) -> TodoItem | None:
        item = self.find(query)
        if item is None:
            return None
        items, meta = self._load()
        for stored in items:
            if stored.id == item.id:
                stored.done = done
                stored.done_at = time.time() if done else None
                self._write(items, meta)
                return stored
        return None

    def remove(self, query: str) -> TodoItem | None:
        item = self.find(query)
        if item is None:
            return None
        items, meta = self._load()
        items = [i for i in items if i.id != item.id]
        self._write(items, meta)
        return item

    def clear_done(self) -> int:
        items, meta = self._load()
        kept = [i for i in items if not i.done]
        self._write(kept, meta)
        return len(items) - len(kept)

    def note_email(self, subject: str, sender: str, ts: float) -> bool:
        """Record that the week's email was seen arriving (from a toast), so
        the page can say so while the document is still on its way."""
        items, meta = self._load()
        seen = meta.get("email") or {}
        if isinstance(seen, dict) and float(seen.get("ts") or 0) >= ts:
            return False
        meta["email"] = {"subject": subject, "from": sender, "ts": ts}
        self._write(items, meta)
        return True

    def last_import(self, source: str) -> dict[str, Any]:
        _, meta = self._load()
        imports = meta.get("imports") or {}
        record = imports.get(source) if isinstance(imports, dict) else None
        return dict(record) if isinstance(record, dict) else {}

    # -- links to Claude Code projects ---------------------------------------

    def links(self) -> dict[str, dict[str, Any]]:
        """Explicit section -> project links, keyed by the section's key."""
        _, meta = self._load()
        links = meta.get("links")
        return dict(links) if isinstance(links, dict) else {}

    def link(self, section: str, project: str, cwd: str) -> dict[str, Any]:
        """Tie a section (a document project) to a Claude Code project. An
        empty project and cwd is an explicit nothing: no guessing either."""
        section = _clean(section)
        if not section:
            raise ValueError("Which section?")
        items, meta = self._load()
        links = meta.setdefault("links", {})
        record = {"section": section, "project": _clean(project), "cwd": cwd.strip()}
        links[section_key(section)] = record
        self._write(items, meta)
        return record

    def unlink(self, section: str) -> bool:
        """Forget a link, so the section is guessed again."""
        items, meta = self._load()
        links = meta.get("links") or {}
        gone = links.pop(section_key(section), None) is not None
        if gone:
            meta["links"] = links
            self._write(items, meta)
        return gone

    def sections(self) -> list[str]:
        """Every section on the list, in the order it first appears."""
        seen: list[str] = []
        for item in self.items():
            if item.section and item.section not in seen:
                seen.append(item.section)
        return seen

    def resolve_links(self, projects: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Each section's project: the explicit link if there is one, else
        the best guess, marked as such. Keyed by section name."""
        links = self.links()
        by_cwd = {_folder_key(str(p.get("cwd") or "")): p for p in projects}
        out: dict[str, dict[str, Any]] = {}
        for section in self.sections():
            explicit = links.get(section_key(section))
            if explicit is not None:
                if not explicit.get("cwd") and not explicit.get("project"):
                    continue
                known = by_cwd.get(_folder_key(str(explicit.get("cwd") or "")), {})
                out[section] = {
                    "project": explicit.get("project") or known.get("project", ""),
                    "cwd": explicit.get("cwd", ""),
                    "auto": False,
                    "live": bool(known.get("live")),
                    "turn": known.get("turn", ""),
                    "session_id": known.get("session_id", ""),
                }
                continue
            guess = match_project(section, projects)
            if guess is not None:
                out[section] = {
                    "project": guess.get("project", ""),
                    "cwd": guess.get("cwd", ""),
                    "auto": True,
                    "live": bool(guess.get("live")),
                    "turn": guess.get("turn", ""),
                    "session_id": guess.get("session_id", ""),
                }
        return out

    def note_message(self, source: str, message: dict[str, Any]) -> None:
        """Remember which email the source's document came from, so the same
        message is not downloaded again on the next look."""
        items, meta = self._load()
        imports = meta.setdefault("imports", {})
        record = imports.get(source) if isinstance(imports.get(source), dict) else {}
        record.update({
            "message_id": message.get("id", ""),
            "message": {k: message.get(k) for k in ("subject", "from", "ts", "link")},
        })
        imports[source] = record
        self._write(items, meta)

    def import_file(
        self,
        path: str | Path,
        source: str,
        *,
        force: bool = False,
        week_of: float | None = None,
    ) -> ImportReport:
        """Replace the items from `source` with what the document says now.

        A document with dated sections is read one section at a time: the
        one dated within the week of `week_of`, or with no week given, the
        newest. Asking for a week the document does not have is an error
        that says which date it does have, because a list quietly left as
        last week's is the worst outcome.
        """
        path = Path(path)
        extracted, dated = pick_week(extract_items(path), week_of, name=path.name)
        digest = file_digest(path)
        items, meta = self._load()
        imports = meta.setdefault("imports", {})
        previous = imports.get(source) if isinstance(imports.get(source), dict) else {}
        report = ImportReport(source=source, file=str(path), dated=dated)
        if previous.get("sha") == digest and previous.get("dated", "") == dated and not force:
            report.skipped = True
            report.kept = len([i for i in items if i.source == source])
            return report

        old = [i for i in items if i.source == source]
        others = [i for i in items if i.source != source]
        old_by_text = {_norm(i.text): i for i in old}
        fresh: list[TodoItem] = []
        seen: set[str] = set()
        for ex in extracted:
            key = _norm(ex.text)
            if not key or key in seen:
                continue
            seen.add(key)
            before = old_by_text.get(key)
            if before is not None:
                before.section = ex.section
                before.detail = ex.detail
                if ex.done and not before.done:
                    before.done, before.done_at = True, time.time()
                fresh.append(before)
                report.kept += 1
            else:
                fresh.append(TodoItem(
                    id=uuid.uuid4().hex[:8], text=ex.text[:800], section=ex.section,
                    source=source, detail=ex.detail, done=ex.done,
                    done_at=time.time() if ex.done else None,
                ))
                report.added += 1
        report.dropped = len(old) - report.kept

        imports[source] = {
            # Which email it came from is noted before the import; keep it.
            **{k: previous[k] for k in ("message_id", "message") if k in previous},
            "file": str(path), "name": path.name, "sha": digest, "ts": time.time(),
            "file_ts": path.stat().st_mtime, "count": len(fresh), "dated": dated,
        }
        self._write(others + fresh, meta)
        return report


# -- the weekly sync ---------------------------------------------------------

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def weekday_index(name: str) -> int | None:
    name = (name or "").strip().lower()
    for index, day in enumerate(WEEKDAYS):
        if day.startswith(name[:3]) and name:
            return index
    return None


def week_start(ts: float, first_day: int = 0) -> float:
    """Midnight of the most recent `first_day` (Monday by default), local time."""
    when = datetime.fromtimestamp(ts)
    back = (when.weekday() - first_day) % 7
    start = when.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp() - back * 86400


class TodoSync:
    """Looks for the week's document and imports it. Cheap enough to call
    every tick: it walks the folders only every `scan_seconds`."""

    def __init__(self, config: Any) -> None:
        # `config` is the top-level Config; the todo section is config.todo.
        self.config = config
        self.todo = config.todo
        self.list = TodoList(todo_path(config), self.todo.max_items)
        self._scanned_at = 0.0
        self._last_error = ""
        # The inbox, when configured. Shared with the commands in this process
        # so a sign-in started from the page is the one the sync uses.
        mail_cfg = getattr(self.todo, "mail", None)
        self.mail = shared_mail(mail_cfg) if mail_cfg is not None and mail_cfg.enabled else None
        self._mail_checked = 0.0
        self._mail_error = ""
        # A file it looked at and could not use (wrong week, unreadable), so
        # the same one is not parsed and logged again every scan.
        self._rejected: tuple[str, float] | None = None

    @property
    def folders(self) -> list[str]:
        return list(self.todo.watch_folders) or default_watch_folders()

    @property
    def save_folder(self) -> Path:
        chosen = getattr(self.todo.mail, "save_folder", "") if self.mail is not None else ""
        if chosen:
            return expand_folder(chosen)
        return Path(self.config.state_file).with_name("mail")

    def due(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return (now - self._scanned_at) >= max(5.0, float(self.todo.scan_seconds))

    def on_day(self, now: float | None = None) -> bool:
        """Whether today is the day the update comes, when that is the only
        day worth looking (`weekday_only`)."""
        if not getattr(self.todo, "weekday_only", False):
            return True
        day = weekday_index(self.todo.weekday)
        if day is None:
            return True
        now = time.time() if now is None else now
        return datetime.fromtimestamp(now).weekday() == day

    def mail_due(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if self.mail is None:
            return False
        return (now - self._mail_checked) >= max(30.0, float(self.todo.mail.poll_seconds))

    def _sync_mail(self, now: float, *, force: bool = False) -> ImportReport | None:
        """Ask the inbox for the newest matching email and take its document.

        None when nobody is signed in, nothing matched, or the message was
        already taken; a report otherwise. Errors are kept for `status()`
        and never raised: the tick must go on.
        """
        assert self.mail is not None
        self._mail_checked = now
        source = self.todo.source_name
        try:
            if not self.mail.signed_in():
                return None
            tokens = list(self.todo.mail.subject) or list(self.todo.match)
            since = now - max(1, int(self.todo.mail.lookback_days)) * 86400
            message = self.mail.find_latest(tokens, since)
            if message is None:
                self._mail_error = ""
                return None
            self.list.note_email(message["subject"], message["from"], message["ts"])
            if self.list.last_import(source).get("message_id") == message["id"] and not force:
                return None
            path = self.mail.download_attachment(message["id"], self.todo.extensions, self.save_folder)
            # Noted before the import, so a document it cannot use (last
            # week's date, nothing readable) is not fetched again every poll.
            self.list.note_message(source, message)
            if path is None:
                self._mail_error = f"the email '{message['subject']}' has no document attached that I can read"
                return None
            report = self.list.import_file(path, source, week_of=now)
        except (MailUnavailable, DocumentError, OSError) as exc:
            self._mail_error = str(exc)
            log.warning("inbox sync: %s", exc)
            return None
        self._mail_error = ""
        if not report.skipped:
            log.info(
                "inbox sync: took %d item(s) from '%s' (%s), %d new, %d kept, %d dropped",
                report.total, message["subject"], path.name, report.added, report.kept, report.dropped,
            )
        return report

    def sync(self, *, force: bool = False, now: float | None = None) -> ImportReport | None:
        """Import the newest matching document if it is new. None when there
        was nothing to do; the report otherwise (skipped=True when the file
        was already taken).

        `force` means "look now" rather than "take it again": the cadence is
        skipped, the hash check is not. Re-reading a file on purpose is what
        `todo.import` is for.
        """
        now = time.time() if now is None else now
        if not self.todo.enabled:
            return None
        if not force and not self.due(now):
            return None
        if not force and not self.on_day(now):
            return None
        self._scanned_at = now

        # The inbox first, on its own slower cadence; the folders are the
        # fallback for a week the mail route did not deliver.
        if self.mail is not None and (force or self.mail_due(now)):
            report = self._sync_mail(now, force=force)
            if report is not None:
                return report

        found = find_document(self.folders, self.todo.match, self.todo.extensions)
        if found is None:
            return None
        previous = self.list.last_import(self.todo.source_name)
        mtime = found.stat().st_mtime
        if previous.get("file") == str(found) and previous.get("file_ts") == mtime:
            return None
        if self._rejected == (str(found), mtime) and not force:
            return None
        try:
            report = self.list.import_file(found, self.todo.source_name, week_of=now)
        except DocumentError as exc:
            self._rejected = (str(found), mtime)
            self._last_error = str(exc)
            log.warning("todo sync: %s", exc)
            return None
        self._rejected = None
        self._last_error = ""
        if not report.skipped:
            log.info(
                "todo sync: took %d item(s) from %s (%d new, %d kept, %d dropped)",
                report.total, found.name, report.added, report.kept, report.dropped,
            )
        return report

    def note_toasts(self, notifications: dict[str, Any] | None) -> bool:
        """Spot the email itself landing, from the notification centre."""
        if not notifications or not self.todo.match:
            return False
        tokens = [t.lower() for t in self.todo.match if t.strip()]
        newest: dict[str, Any] | None = None
        for note in notifications.get("recent") or []:
            if not isinstance(note, dict):
                continue
            text = " ".join(str(note.get(k) or "") for k in ("title", "text", "subject")).lower()
            if any(t in text for t in tokens):
                if newest is None or float(note.get("ts") or 0) > float(newest.get("ts") or 0):
                    newest = note
        if newest is None:
            return False
        return self.list.note_email(
            str(newest.get("text") or newest.get("subject") or ""),
            str(newest.get("title") or ""),
            float(newest.get("ts") or time.time()),
        )

    def status(self, now: float | None = None) -> dict[str, Any]:
        """Where this week stands: for the page's source strip and for speech."""
        now = time.time() if now is None else now
        cfg = self.todo
        last = self.list.last_import(cfg.source_name)
        email = self.list.meta().get("email") or {}
        start = week_start(now)
        day = weekday_index(cfg.weekday)
        expected_ts = start + (day or 0) * 86400 if day is not None else None
        imported_this_week = bool(last) and float(last.get("ts") or 0) >= start
        email_this_week = bool(email) and float(email.get("ts") or 0) >= start
        if imported_this_week:
            state = "imported"
        elif email_this_week:
            state = "email-seen"
        elif expected_ts is not None and now >= expected_ts:
            state = "overdue" if now >= expected_ts + 86400 else "due"
        else:
            state = "waiting"
        mail: dict[str, Any] | None = None
        if self.mail is not None:
            mail = self.mail.status()
            if self._mail_error and not mail.get("error"):
                mail["error"] = self._mail_error
        return {
            "source": cfg.source_name,
            "weekday": WEEKDAYS[day] if day is not None else "",
            "state": state,
            "expected_ts": expected_ts,
            "last": last,
            "email": email if email_this_week else {},
            "folders": self.folders,
            "match": list(cfg.match),
            "error": self._last_error,
            "mail": mail,
        }


def _folder_key(path: str) -> str:
    return path.replace(chr(92), "/").rstrip("/").lower()


def claude_projects(config: Any, claude_section: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The Claude Code projects on this PC: one per working directory seen in
    a session, carrying the liveliest session's state, plus the allowlisted
    `code.projects` that have no session yet."""
    order = {"busy": 0, "waiting": 1, "idle": 2}
    by_cwd: dict[str, dict[str, Any]] = {}
    for row in (claude_section or {}).get("sessions") or []:
        cwd = str(row.get("cwd") or "")
        if not cwd:
            continue
        key = _folder_key(cwd)
        entry = by_cwd.get(key)
        rank = (0 if row.get("live") else 1, order.get(str(row.get("turn") or ""), 3))
        if entry is None or rank < entry["_rank"]:
            by_cwd[key] = {
                "project": str(row.get("project") or ""),
                "cwd": cwd,
                "live": bool(row.get("live")),
                "turn": str(row.get("turn") or ""),
                "session_id": str(row.get("id") or ""),
                "session": str(row.get("name") or ""),
                "sessions": (entry or {}).get("sessions", 0) + 1,
                "_rank": rank,
            }
        else:
            entry["sessions"] += 1
    code = getattr(getattr(config, "code", None), "projects", None) or {}
    for name, folder in code.items():
        key = _folder_key(str(folder))
        if key not in by_cwd:
            by_cwd[key] = {
                "project": str(name), "cwd": str(folder), "live": False, "turn": "",
                "session_id": "", "session": "", "sessions": 0, "_rank": (2, 9),
            }
    projects = sorted(by_cwd.values(), key=lambda p: (p["_rank"], p["project"].lower()))
    for p in projects:
        p.pop("_rank", None)
    return projects


def compose_prompt(item: TodoItem, source_name: str = "") -> str:
    """What to say to the session about one task, as a person would."""
    where = f"under {item.section}" if item.section else "on the list"
    origin = f"this week's {source_name}" if source_name and item.source != MANUAL_SOURCE else "my to-do list"
    text = f"From {origin}, {where}: {item.text}"
    if item.detail:
        text += f" ({item.detail})"
    return text + ". Please take a look and tell me what you'd do about it."


def todo_path(config: Any) -> Path:
    """Beside the state file unless `todo.file` says otherwise."""
    chosen = getattr(config.todo, "file", "") or ""
    if chosen:
        path = Path(os.path.expandvars(os.path.expanduser(chosen)))
        if not path.is_absolute() and getattr(config, "source_path", None):
            path = Path(config.source_path).resolve().parent / path
        return path
    return Path(config.state_file).with_name("todos.json")
