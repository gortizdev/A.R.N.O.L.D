"""The weekly status update, read the way it is written: a dated title, a
bullet per project, a sub-bullet per task, and only the week that is wanted.
"""

from __future__ import annotations

import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from arnold.config import Config
from arnold.todos import (
    DocumentError,
    TodoList,
    TodoSync,
    extract_items,
    pick_week,
)

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def para(text: str, *, level: int | None = None, style: str = "") -> str:
    ppr = ""
    if level is not None or style:
        ppr = "<w:pPr>"
        if style:
            ppr += f'<w:pStyle w:val="{style}"/>'
        if level is not None:
            ppr += f'<w:numPr><w:ilvl w:val="{level}"/><w:numId w:val="1"/></w:numPr>'
        ppr += "</w:pPr>"
    if "\t" in text:
        # The title has the date on the right, past a tab, as Word writes it.
        left, right = text.split("\t", 1)
        runs = (
            f'<w:r><w:t xml:space="preserve">{left}</w:t></w:r><w:r><w:tab/></w:r>'
            f'<w:r><w:t xml:space="preserve">{right}</w:t></w:r>'
        )
    else:
        runs = f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'
    return f"<w:p>{ppr}{runs}</w:p>"


def docx(path: Path, *blocks: str) -> Path:
    xml = f'<?xml version="1.0" encoding="UTF-8"?><w:document {W}><w:body>{"".join(blocks)}</w:body></w:document>'
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("word/document.xml", xml)
    return path


def status_update(path: Path, date: str, *, second: str | None = None) -> Path:
    """The document as it looks: title with the date on the right, projects,
    tasks, and a few projects with their status inline."""
    blocks = [
        para(f"Geo Projects Status Update\t{date}"),
        para("Certification Evaluation:", level=0),
        para("Updated LOD; (hopefully) to be completed by 09/09/2026", level=1),
        para("Once ready, Ryan to Test", level=1),
        para("Purity Award Evaluation:", level=0),
        para("Lab Team / Bethani to update Serving Sizes in category testing orders in QBench;", level=1),
        para("Once that’s completed, the tool should work – Ryan to Test", level=1),
        para("Seed Oil – Nothing to Update", level=0),
        para("ROI Calculator – Nothing to Update; Waiting on Matt for Update", level=0),
        para("Bracketing Evaluation: Ryan to Test", level=0),
        para("Tin QBench Reporting:", level=0),
        para("Geo to watch demo video Ryan sent and make determination as to how to move forward from there", level=1),
    ]
    if second:
        blocks += [
            para(f"Geo Projects Status Update\t{second}"),
            para("Certification Evaluation:", level=0),
            para("LOD done; Ryan testing", level=1),
            para("Seed Oil – Nothing to Update", level=0),
        ]
    return docx(path, *blocks)


def mdy(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%m/%d/%Y")


def this_wednesday() -> float:
    now = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return (now - timedelta(days=now.weekday()) + timedelta(days=2)).timestamp()


class TestShape:
    def test_projects_become_groups_and_tasks_items(self, tmp_path):
        items = extract_items(status_update(tmp_path / "u.docx", "09/02/2026"))
        got = [(i.section, i.text) for i in items]
        assert got == [
            ("Certification Evaluation", "Updated LOD; (hopefully) to be completed by 09/09/2026"),
            ("Certification Evaluation", "Once ready, Ryan to Test"),
            ("Purity Award Evaluation", "Lab Team / Bethani to update Serving Sizes in category testing orders in QBench;"),
            ("Purity Award Evaluation", "Once that’s completed, the tool should work – Ryan to Test"),
            ("ROI Calculator", "Nothing to Update; Waiting on Matt for Update"),
            ("Bracketing Evaluation", "Ryan to Test"),
            ("Tin QBench Reporting", "Geo to watch demo video Ryan sent and make determination as to how to move forward from there"),
        ]  # "Seed Oil - Nothing to Update" is a status, not a task
        assert {i.date for i in items} == {"09/02/2026"}
        assert not any(i.done for i in items)

    def test_the_title_date_is_the_sections_date(self, tmp_path):
        items = extract_items(status_update(tmp_path / "u.docx", "09/02/2026", second="09/09/2026"))
        dates = {i.date for i in items}
        assert dates == {"09/02/2026", "09/09/2026"}
        assert [i.text for i in items if i.date == "09/09/2026"] == ["LOD done; Ryan testing"]

    def test_a_project_line_with_status_and_sub_bullets(self, tmp_path):
        path = docx(
            tmp_path / "u.docx",
            para("Bracketing Evaluation: Ryan to Continue Testing; waiting on the release", level=0),
            para("Color feature doesn’t work", level=1),
            para("Dragging a file in doesn’t work", level=1),
            para("ROI Calculator – Nothing to Update; Waiting on Matt", level=0),
            para("Geo to follow up with Matt", level=1),
            para("Seed Oil – Nothing to Update", level=0),
            para("CLP Mobile App:", level=0),
            para("Nothing to Update", level=1),
        )
        got = [(i.section, i.text) for i in extract_items(path)]
        assert got == [
            ("Bracketing Evaluation", "Ryan to Continue Testing; waiting on the release"),
            ("Bracketing Evaluation", "Color feature doesn’t work"),
            ("Bracketing Evaluation", "Dragging a file in doesn’t work"),
            ("ROI Calculator", "Nothing to Update; Waiting on Matt"),
            ("ROI Calculator", "Geo to follow up with Matt"),
        ]

    def test_markdown_with_the_same_shape(self, tmp_path):
        path = tmp_path / "u.md"
        path.write_text(
            "Geo Projects Status Update 09/02/2026\n"
            "- Certification Evaluation:\n"
            "  - Updated LOD\n"
            "  - Ryan to Test\n"
            "- Seed Oil - Nothing to Update\n",
            encoding="utf-8",
        )
        items = extract_items(path)
        assert [(i.section, i.text, i.date) for i in items] == [
            ("Certification Evaluation", "Updated LOD", "09/02/2026"),
            ("Certification Evaluation", "Ryan to Test", "09/02/2026"),
        ]


class TestWhichWeek:
    def test_newest_section_by_default(self, tmp_path):
        items = extract_items(status_update(tmp_path / "u.docx", "09/02/2026", second="09/09/2026"))
        kept, dated = pick_week(items, None)
        assert dated == "09/09/2026" and len(kept) == 1

    def test_this_weeks_section_when_asked(self, tmp_path):
        wed = this_wednesday()
        items = extract_items(status_update(tmp_path / "u.docx", mdy(wed - 7 * 86400), second=mdy(wed)))
        kept, dated = pick_week(items, time.time())
        assert dated == mdy(wed) and len(kept) == 1

    def test_a_document_without_this_week_is_refused(self, tmp_path):
        items = extract_items(status_update(tmp_path / "u.docx", "09/02/2026"))
        with pytest.raises(DocumentError, match="newest is dated 09/02/2026"):
            pick_week(items, time.time(), name="u.docx")

    def test_undated_documents_come_back_whole(self, tmp_path):
        path = docx(tmp_path / "plain.docx", para("A", level=0), para("one", level=1), para("two", level=1))
        kept, dated = pick_week(extract_items(path), time.time())
        assert dated == "" and [i.text for i in kept] == ["one", "two"]


class TestSyncByWeek:
    def _config(self, tmp_path) -> Config:
        cfg = Config()
        cfg.state_file = str(tmp_path / "state.json")
        cfg.todo.watch_folders = [str(tmp_path / "inbox")]
        cfg.todo.mail.enabled = False
        (tmp_path / "inbox").mkdir()
        return cfg

    def test_last_weeks_document_is_reported_not_imported(self, tmp_path):
        cfg = self._config(tmp_path)
        wed = this_wednesday()
        status_update(tmp_path / "inbox" / "geo update.docx", mdy(wed - 7 * 86400))
        sync = TodoSync(cfg)
        assert sync.sync(force=True) is None
        assert "no update dated this week" in sync.status()["error"]
        assert sync.list.items() == []
        # Not parsed again on the next scan; a forced look tries once more.
        assert sync.sync(force=False, now=time.time() + 1000) is None
        assert sync._rejected is not None

    def test_this_weeks_document_is_taken(self, tmp_path):
        cfg = self._config(tmp_path)
        wed = this_wednesday()
        status_update(tmp_path / "inbox" / "geo update.docx", mdy(wed - 7 * 86400), second=mdy(wed))
        sync = TodoSync(cfg)
        report = sync.sync(force=True)
        assert report is not None and report.dated == mdy(wed) and report.total == 1
        assert sync.list.last_import("geo update")["dated"] == mdy(wed)
        assert sync.status()["state"] == "imported"
        assert sync.status()["error"] == ""

    def test_automatic_scan_only_runs_on_the_day(self, tmp_path):
        cfg = self._config(tmp_path)
        wed = this_wednesday()
        status_update(tmp_path / "inbox" / "geo update.docx", mdy(wed))
        sync = TodoSync(cfg)
        thursday = wed + 86400 + 10 * 3600
        assert not sync.on_day(thursday)
        assert sync.sync(now=thursday) is None
        assert sync.list.items() == []
        report = sync.sync(now=wed + 10 * 3600)
        assert report is not None and report.dated == mdy(wed) and report.total > 0
        cfg.todo.weekday_only = False
        assert sync.on_day(thursday)

    def test_manual_import_takes_the_newest_section(self, tmp_path):
        todos = TodoList(tmp_path / "todos.json")
        path = status_update(tmp_path / "u.docx", "09/02/2026", second="09/09/2026")
        report = todos.import_file(path, "geo update", force=True)
        assert report.dated == "09/09/2026" and report.total == 1
        assert {i.section for i in todos.items()} == {"Certification Evaluation"}
