"""The to-do list and the document it is filled from.

The Word documents here are built by hand with zipfile: the reader only needs
word/document.xml, and a fixture file would hide exactly the structure under
test (numbering, checkboxes, tables, headings).
"""

from __future__ import annotations

import os
import time
import zipfile
from pathlib import Path

import pytest

from arnold.commands import CommandContext, build_registry
from arnold.config import Config
from arnold.todos import (
    MANUAL_SOURCE,
    DocumentError,
    TodoList,
    TodoSync,
    extract_items,
    find_document,
    todo_path,
    week_start,
    weekday_index,
)

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
W14 = 'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"'


def para(text: str, *, style: str = "", numbered: bool = False, checked: bool | None = None) -> str:
    ppr = ""
    if style or numbered:
        ppr = "<w:pPr>"
        if style:
            ppr += f'<w:pStyle w:val="{style}"/>'
        if numbered:
            ppr += '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>'
        ppr += "</w:pPr>"
    box = ""
    if checked is not None:
        box = (
            '<w:sdt><w:sdtPr><w14:checkbox><w14:checked w14:val="%s"/></w14:checkbox>'
            "</w:sdtPr><w:sdtContent><w:r><w:t>%s</w:t></w:r></w:sdtContent></w:sdt>"
            % ("1" if checked else "0", "☒" if checked else "☐")
        )
    return f"<w:p>{ppr}{box}<w:r><w:t xml:space=\"preserve\">{text}</w:t></w:r></w:p>"


def table(rows: list[list[str]]) -> str:
    out = "<w:tbl>"
    for row in rows:
        out += "<w:tr>" + "".join(f"<w:tc>{para(cell)}</w:tc>" for cell in row) + "</w:tr>"
    return out + "</w:tbl>"


def docx(path: Path, *blocks: str) -> Path:
    body = "".join(blocks)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><w:document {W} {W14}><w:body>{body}</w:body></w:document>'
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("[Content_Types].xml", "<Types/>")
        bundle.writestr("word/document.xml", xml)
    return path


class TestExtract:
    def test_numbered_list_under_headings(self, tmp_path):
        path = docx(
            tmp_path / "geo update.docx",
            para("Geo update", style="Title"),
            para("Some intro prose that is not a task."),
            para("This week", style="Heading1"),
            para("Finish the Azure estimate", numbered=True),
            para("Send the data dictionary to Sam", numbered=True),
            para("Next week", style="Heading1"),
            para("Draft the runbook", numbered=True),
        )
        items = extract_items(path)
        assert [i.text for i in items] == [
            "Finish the Azure estimate", "Send the data dictionary to Sam", "Draft the runbook",
        ]
        assert [i.section for i in items] == ["This week", "This week", "Next week"]
        assert not any(i.done for i in items)

    def test_checkboxes_carry_their_state(self, tmp_path):
        path = docx(
            tmp_path / "u.docx",
            para("Book the room", checked=False),
            para("Order lunch", checked=True),
            para("☑ Confirm the agenda - done"),
        )
        items = extract_items(path)
        assert [(i.text, i.done) for i in items] == [
            ("Book the room", False), ("Order lunch", True), ("Confirm the agenda", True),
        ]

    def test_table_rows_skip_the_header(self, tmp_path):
        path = docx(
            tmp_path / "t.docx",
            table([
                ["Task", "Owner", "Status"],
                ["Migrate the pipeline", "Geo", "In progress"],
                ["Close ticket 4411", "Geo", "Done"],
            ]),
        )
        items = extract_items(path)
        assert [(i.text, i.done) for i in items] == [
            ("Migrate the pipeline", False), ("Close ticket 4411", True),
        ]
        assert items[0].detail == "Geo / In progress"

    def test_prose_under_a_tasky_heading(self, tmp_path):
        path = docx(
            tmp_path / "p.docx",
            para("Background", style="Heading1"),
            para("Everything went fine last week and nothing needs saying about it."),
            para("Action items", style="Heading2"),
            para("Renew the certificate before Friday."),
            para("Call the vendor about the invoice."),
        )
        assert [i.text for i in extract_items(path)] == [
            "Renew the certificate before Friday.", "Call the vendor about the invoice.",
        ]

    def test_markdown_and_text(self, tmp_path):
        path = tmp_path / "list.md"
        path.write_text("# Monday\n- [ ] water the plants\n- [x] pay rent\n* call mum\n", encoding="utf-8")
        items = extract_items(path)
        assert [(i.text, i.done, i.section) for i in items] == [
            ("water the plants", False, "Monday"), ("pay rent", True, "Monday"),
            ("call mum", False, "Monday"),
        ]

    def test_unreadable_things_say_so(self, tmp_path):
        with pytest.raises(DocumentError):
            extract_items(tmp_path / "missing.docx")
        bad = tmp_path / "bad.docx"
        bad.write_bytes(b"not a zip")
        with pytest.raises(DocumentError):
            extract_items(bad)
        odd = tmp_path / "thing.xyz"
        odd.write_text("x")
        with pytest.raises(DocumentError):
            extract_items(odd)


class TestList:
    def test_add_done_remove(self, tmp_path):
        todos = TodoList(tmp_path / "todos.json")
        a = todos.add("Call the dentist")
        b = todos.add("Buy milk")
        assert todos.add("call the dentist").id == a.id  # no twins
        assert [i.text for i in todos.items()] == ["Call the dentist", "Buy milk"]

        ticked = todos.set_done("dentist")
        assert ticked is not None and ticked.id == a.id and ticked.done
        assert [i.id for i in todos.items(include_done=False)] == [b.id]

        assert todos.set_done(a.id, False).done is False
        assert todos.remove("milk").id == b.id
        assert todos.find("nothing like it") is None
        assert todos.clear_done() == 0

    def test_import_replaces_its_own_items_and_keeps_ticks(self, tmp_path):
        todos = TodoList(tmp_path / "todos.json")
        todos.add("Mine by hand")
        first = docx(tmp_path / "week1.docx", para("Alpha", numbered=True), para("Beta", numbered=True))
        report = todos.import_file(first, "geo update")
        assert (report.added, report.kept, report.dropped, report.skipped) == (2, 0, 0, False)
        todos.set_done("alpha")

        # The same file again is a no-op.
        again = todos.import_file(first, "geo update")
        assert again.skipped and again.kept == 2

        second = docx(tmp_path / "week2.docx", para("Alpha", numbered=True), para("Gamma", numbered=True))
        report = todos.import_file(second, "geo update")
        assert (report.added, report.kept, report.dropped) == (1, 1, 1)
        by_text = {i.text: i for i in todos.items()}
        assert set(by_text) == {"Mine by hand", "Alpha", "Gamma"}
        assert by_text["Alpha"].done is True  # the tick survived the new document
        assert by_text["Mine by hand"].source == MANUAL_SOURCE
        assert todos.last_import("geo update")["name"] == "week2.docx"

    def test_describe_has_what_the_page_needs(self, tmp_path):
        todos = TodoList(tmp_path / "todos.json")
        todos.add("One")
        todos.set_done("One")
        todos.add("Two")
        section = todos.describe()
        assert section["open"] == 1 and section["done"] == 1
        assert len(section["items"]) == 2

    def test_cap_drops_finished_items_first(self, tmp_path):
        todos = TodoList(tmp_path / "todos.json", max_items=10)
        for n in range(12):
            todos.add(f"item {n}")
        for n in range(6):
            todos.set_done(f"item {n}")
        todos.add("one more")
        items = todos.items()
        assert len(items) == 10
        assert all(not i.done for i in items if i.text.startswith("item 1") or i.text == "one more")


class TestFind:
    def test_newest_matching_path_wins(self, tmp_path):
        inbox = tmp_path / "Email attachments"
        (inbox / "geo update").mkdir(parents=True)
        old = docx(inbox / "geo update" / "notes.docx", para("x"))
        new = docx(inbox / "Geo_Update 9-23.docx", para("y"))
        noise = docx(inbox / "invoice.docx", para("z"))
        os.utime(old, (1_000_000, 1_000_000))
        os.utime(new, (2_000_000, 2_000_000))
        os.utime(noise, (3_000_000, 3_000_000))
        assert find_document([inbox], ["geo update"]) == new
        assert find_document([inbox], []) == noise  # no match = any document
        assert find_document([tmp_path / "nowhere"], ["geo update"]) is None
        (inbox / "~$lock.docx").write_bytes(b"")
        assert find_document([inbox], []) == noise


class TestSync:
    def _config(self, tmp_path) -> Config:
        cfg = Config()
        cfg.state_file = str(tmp_path / "state.json")
        cfg.todo.watch_folders = [str(tmp_path / "inbox")]
        cfg.todo.scan_seconds = 60
        return cfg

    def test_takes_the_document_once(self, tmp_path):
        cfg = self._config(tmp_path)
        (tmp_path / "inbox").mkdir()
        sync = TodoSync(cfg)
        assert sync.sync(force=True) is None
        assert sync.status()["state"] in ("waiting", "due", "overdue")

        docx(tmp_path / "inbox" / "geo update.docx", para("Ship it", numbered=True))
        report = sync.sync(force=True)
        assert report is not None and report.added == 1
        assert sync.status()["state"] == "imported"
        assert [i.text for i in sync.list.items()] == ["Ship it"]
        # Not due again yet, and the same file anyway.
        assert sync.sync() is None
        assert sync.sync(force=True) is None

    def test_email_toast_is_noted(self, tmp_path):
        cfg = self._config(tmp_path)
        sync = TodoSync(cfg)
        now = time.time()
        assert sync.note_toasts({"recent": [
            {"app": "Outlook", "title": "Sam Lee", "text": "Geo update: this week", "ts": now},
            {"app": "Teams", "title": "Bob", "text": "lunch?", "ts": now},
        ]})
        status = sync.status()
        assert status["state"] == "email-seen"
        assert status["email"]["from"] == "Sam Lee"
        # An older toast does not move it backwards.
        assert not sync.note_toasts({"recent": [
            {"app": "Outlook", "title": "Old", "text": "geo update", "ts": now - 100},
        ]})

    def test_week_helpers(self):
        assert weekday_index("wednesday") == 2
        assert weekday_index("Wed") == 2
        assert weekday_index("") is None
        start = week_start(time.time())
        assert time.localtime(start).tm_wday == 0
        assert time.localtime(start).tm_hour == 0

    def test_path_follows_the_config(self, tmp_path):
        cfg = self._config(tmp_path)
        assert todo_path(cfg) == tmp_path / "todos.json"
        cfg.todo.file = "lists/mine.json"
        cfg.source_path = tmp_path / "config.yaml"
        assert todo_path(cfg) == tmp_path / "lists" / "mine.json"


class TestCommands:
    def _ctx(self, tmp_path) -> CommandContext:
        cfg = Config()
        cfg.state_file = str(tmp_path / "state.json")
        cfg.todo.watch_folders = [str(tmp_path / "inbox")]
        return CommandContext(config=cfg, collector=None, alerts=None)  # type: ignore[arg-type]

    def test_round_trip(self, tmp_path):
        ctx = self._ctx(tmp_path)
        registry = build_registry()
        assert registry.dispatch("todo.list", {}, ctx).speech == "Nothing on the list."
        added = registry.dispatch("todo.add", {"text": "Call the dentist"}, ctx)
        assert added.ok and added.speech == "Added Call the dentist."
        listed = registry.dispatch("todo.list", {}, ctx)
        assert listed.result["open"] == 1 and "Call the dentist" in listed.speech
        done = registry.dispatch("todo.done", {"which": "dentist"}, ctx)
        assert done.ok and done.speech.startswith("Ticked off")
        assert registry.dispatch("todo.list", {}, ctx).speech.startswith("Everything on the list is done")
        assert not registry.dispatch("todo.done", {"which": "nothing here"}, ctx).ok
        cleared = registry.dispatch("todo.remove", {"which": "done"}, ctx)
        assert cleared.result["removed"] == 1

    def test_import_and_sync(self, tmp_path):
        ctx = self._ctx(tmp_path)
        registry = build_registry()
        (tmp_path / "inbox").mkdir()
        nothing = registry.dispatch("todo.sync", {}, ctx)
        assert nothing.ok and "No geo update document yet" in nothing.speech

        path = docx(tmp_path / "inbox" / "geo update.docx", para("A", numbered=True), para("B", numbered=True))
        took = registry.dispatch("todo.sync", {}, ctx)
        assert took.ok and took.result["total"] == 2
        assert registry.dispatch("todo.sync", {}, ctx).speech.startswith("Nothing new")

        other = docx(tmp_path / "extra.docx", para("C", numbered=True))
        imported = registry.dispatch("todo.import", {"path": str(other), "source": "extra"}, ctx)
        assert imported.ok and imported.result["added"] == 1
        assert registry.dispatch("todo.list", {}, ctx).result["open"] == 3
        assert not registry.dispatch("todo.import", {"path": str(tmp_path / "no.docx")}, ctx).ok
        assert path.exists()
