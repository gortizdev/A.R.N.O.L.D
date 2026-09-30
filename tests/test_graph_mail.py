"""Reading the weekly document straight from the inbox.

Graph itself is never called here: the sync is exercised against a stand-in
mailbox, and the client's own logic (what counts as a match, which attachment
to take, how an error is explained, how the cache is protected) is tested
on its own.
"""

from __future__ import annotations

import time
import zipfile
from pathlib import Path

import pytest

from arnold.config import Config, load_config
from arnold.graph_mail import (
    DEFAULT_CLIENT_ID,
    MailConfig,
    MailUnavailable,
    _protect,
    _unprotect,
    explain,
    pick_attachment,
    safe_name,
    subject_matches,
    token_path,
)
from arnold.todos import TodoSync

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def docx_bytes(*lines: str) -> bytes:
    import io

    body = "".join(
        '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
        f"<w:r><w:t>{line}</w:t></w:r></w:p>" for line in lines
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("word/document.xml", f"<w:document {W}><w:body>{body}</w:body></w:document>")
    return buffer.getvalue()


class TestPieces:
    def test_subject_matching(self):
        assert subject_matches("RE: Geo   Update - week 39", ["geo update"])
        assert not subject_matches("Invoice", ["geo update"])
        assert subject_matches("anything", [])

    def test_attachment_choice(self):
        items = [
            {"@odata.type": "#microsoft.graph.fileAttachment", "name": "logo.png", "isInline": True},
            {"@odata.type": "#microsoft.graph.itemAttachment", "name": "Geo update.docx"},
            {"@odata.type": "#microsoft.graph.fileAttachment", "name": "Geo update.docx", "id": "a1"},
            {"@odata.type": "#microsoft.graph.fileAttachment", "name": "notes.pdf", "id": "a2"},
        ]
        assert pick_attachment(items, [".docx", ".pdf"])["id"] == "a1"
        assert pick_attachment(items, ["pdf"])["id"] == "a2"
        assert pick_attachment(items, [".xlsx"]) is None

    def test_safe_names(self):
        assert safe_name('geo: "update" <9/23>.docx') == "geo_ _update_ _9_23_.docx"
        assert safe_name("") == "attachment"

    def test_errors_become_sentences(self):
        assert "administrator" in explain({"error": "invalid_grant", "error_description": "AADSTS65001: consent"})
        assert "client id" in explain({"error_description": "AADSTS700016: app not found"})
        assert explain({"error": "expired_token"}).startswith("the sign-in code expired")
        assert explain({}) == "unknown error"

    def test_cache_protection_round_trip(self):
        pytest.importorskip("win32crypt")
        secret = b'{"AccessToken": {}}'
        wrapped = _protect(secret)
        assert wrapped != secret and wrapped.startswith(b"DPAPI1\n")
        assert _unprotect(wrapped) == secret
        assert _unprotect(secret) == secret  # a plain file still reads

    def test_token_path(self, tmp_path):
        assert token_path(MailConfig()).name == "graph-token.bin"
        assert token_path(MailConfig(token_file=str(tmp_path / "t.bin"))) == tmp_path / "t.bin"

    def test_config_nests_under_todo(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("todo:\n  match: [weekly]\n  mail:\n    tenant: contoso.com\n", encoding="utf-8")
        cfg = load_config(path)
        assert cfg.todo.match == ["weekly"]
        assert cfg.todo.mail.tenant == "contoso.com"
        assert cfg.todo.mail.client_id == DEFAULT_CLIENT_ID
        assert not Config().todo.mail.enabled  # off for now; the folders are the route


class FakeMail:
    """What the sync needs from the client, and nothing that talks to Graph."""

    path = Path("nowhere")

    def __init__(self, messages: list[dict], attachment: bytes | None) -> None:
        self.messages = messages
        self.attachment = attachment
        self.downloads = 0
        self.searches = 0
        self.signed = True

    def signed_in(self) -> bool:
        return self.signed

    def find_latest(self, tokens, since_ts):
        self.searches += 1
        for message in self.messages:
            if any(t in message["subject"].lower() for t in tokens) and message["ts"] >= since_ts:
                return message
        return None

    def download_attachment(self, message_id, extensions, folder: Path) -> Path | None:
        self.downloads += 1
        if self.attachment is None:
            return None
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "Geo update.docx"
        target.write_bytes(self.attachment)
        return target

    def status(self):
        return {"available": True, "signed_in": self.signed, "account": "geo@example.com",
                "pending": None, "error": "", "checked_at": 0, "last_message": {},
                "client_id": "x"}


def _config(tmp_path) -> Config:
    cfg = Config()
    cfg.state_file = str(tmp_path / "state.json")
    cfg.todo.watch_folders = [str(tmp_path / "inbox")]
    cfg.todo.mail.poll_seconds = 60
    return cfg


class TestSyncFromInbox:
    def test_takes_the_newest_matching_email_once(self, tmp_path):
        cfg = _config(tmp_path)
        sync = TodoSync(cfg)
        now = time.time()
        sync.mail = FakeMail(
            [{"id": "m2", "subject": "Geo update 9/23", "from": "Sam", "ts": now - 60, "link": "https://x"},
             {"id": "m1", "subject": "Geo update 9/16", "from": "Sam", "ts": now - 7 * 86400}],
            docx_bytes("Ship the estimate", "Call Sam"),
        )
        report = sync.sync(force=True)
        assert report is not None and report.added == 2
        assert [i.text for i in sync.list.items()] == ["Ship the estimate", "Call Sam"]
        assert sync.save_folder == tmp_path / "mail"
        assert (tmp_path / "mail" / "Geo update.docx").exists()

        last = sync.list.last_import("geo update")
        assert last["message_id"] == "m2"
        assert last["message"]["from"] == "Sam"
        status = sync.status()
        assert status["state"] == "imported"
        assert status["email"]["from"] == "Sam"
        assert status["mail"]["signed_in"]

        # The same message is not fetched again on the next look; only a
        # forced check ("Check now") goes back for it, and the hash check
        # then says it is the same document.
        assert sync.sync(now=now + 1000) is None
        assert sync.mail.downloads == 1
        again = sync.sync(force=True)
        assert again is not None and again.skipped
        assert sync.mail.downloads == 2

    def test_not_signed_in_falls_back_to_the_folders(self, tmp_path):
        cfg = _config(tmp_path)
        sync = TodoSync(cfg)
        sync.mail = FakeMail([], None)
        sync.mail.signed = False
        (tmp_path / "inbox").mkdir()
        (tmp_path / "inbox" / "geo update.docx").write_bytes(docx_bytes("From the folder"))
        report = sync.sync(force=True)
        assert report is not None and report.added == 1
        assert sync.mail.searches == 0

    def test_email_without_a_document_is_reported_not_retried(self, tmp_path):
        cfg = _config(tmp_path)
        sync = TodoSync(cfg)
        now = time.time()
        sync.mail = FakeMail([{"id": "m9", "subject": "geo update", "from": "Sam", "ts": now}], None)
        assert sync.sync(force=True) is None
        assert "no document attached" in sync.status()["mail"]["error"]
        assert sync.status()["state"] == "email-seen"
        assert sync.sync(now=now + 1000) is None
        assert sync.mail.downloads == 1

    def test_mail_errors_never_raise(self, tmp_path):
        cfg = _config(tmp_path)
        sync = TodoSync(cfg)

        class Broken(FakeMail):
            def find_latest(self, tokens, since_ts):
                raise MailUnavailable("Graph answered 403: blocked")

        sync.mail = Broken([], None)
        assert sync.sync(force=True) is None
        assert sync.status()["mail"]["error"] == "Graph answered 403: blocked"

    def test_cadence(self, tmp_path):
        cfg = _config(tmp_path)
        sync = TodoSync(cfg)
        sync.mail = FakeMail([], None)
        sync._scanned_at = 0
        assert sync.mail_due(1000.0)
        sync.sync(force=True, now=1000.0)
        assert not sync.mail_due(1030.0)
        assert sync.mail_due(1061.0)
