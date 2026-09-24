"""The inbox itself, read through Microsoft Graph.

The new Outlook exposes nothing a local process can read, so the only way to
take a message straight from the mailbox is the same API Outlook uses. That
needs a sign-in: once, in a browser, with a code this program shows you. What
comes back is a refresh token, kept on this PC and encrypted to your Windows
account with DPAPI, and from then on the agent asks Graph for new mail on its
own.

Two things shape the design:

* **No secrets in the program.** This is a *public client*: there is no app
  password to leak, and the token lives only in the cache file. The default
  client id is Microsoft's own Graph PowerShell application, which every
  tenant already knows; if yours refuses it, register a public-client app of
  your own (README, "Reading the inbox") and put its id in the config.
* **The sign-in never blocks the tick.** `begin_login` hands back the code to
  show and finishes the flow on a thread; the agent, the dashboard and the CLI
  all read the same state through `status()`.

Only `Mail.Read` is asked for. Nothing here can send, move or delete.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.Read"]
# Microsoft Graph PowerShell: a first-party public client present in every
# tenant. Overridable, because some organisations block it for anything but
# PowerShell itself.
DEFAULT_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
TOKEN_FILE_NAME = "graph-token.bin"
DPAPI_PREFIX = b"DPAPI1\n"


class MailUnavailable(RuntimeError):
    """Graph cannot be used right now. The message says what to do."""


@dataclass(slots=True)
class MailConfig:
    """`todo.mail` - reading the weekly document straight from the inbox."""

    enabled: bool = True
    client_id: str = DEFAULT_CLIENT_ID
    # "common" takes work and personal accounts alike; a tenant id or domain
    # pins it to one organisation.
    tenant: str = "common"
    # Substrings of the subject, case-insensitive. Empty = todo.match.
    subject: list[str] = field(default_factory=list)
    # Only look at mail that has something attached.
    with_attachments: bool = True
    # How often to ask Graph, and how far back to look for the newest match.
    poll_seconds: float = 300.0
    lookback_days: int = 8
    # Where downloaded attachments go. Blank = logs/mail beside the state file.
    save_folder: str = ""
    # The encrypted token cache. Blank = %LOCALAPPDATA%\arnold.
    token_file: str = ""


# -- the token cache on disk ---------------------------------------------------


def _protect(data: bytes) -> bytes:
    try:
        import win32crypt  # type: ignore

        return DPAPI_PREFIX + win32crypt.CryptProtectData(data, "arnold graph", None, None, None, 0)
    except ImportError:
        log.warning("pywin32 is not installed, so the Graph token cache is stored unencrypted")
        return data


def _unprotect(data: bytes) -> bytes:
    if not data.startswith(DPAPI_PREFIX):
        return data
    import win32crypt  # type: ignore

    return win32crypt.CryptUnprotectData(data[len(DPAPI_PREFIX):], None, None, None, 0)[1]


def default_token_file() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "arnold" / TOKEN_FILE_NAME


def token_path(config: MailConfig) -> Path:
    if config.token_file:
        return Path(os.path.expandvars(os.path.expanduser(config.token_file)))
    return default_token_file()


# -- picking things out of Graph's answers --------------------------------------


def subject_matches(subject: str, tokens: list[str]) -> bool:
    text = re.sub(r"\s+", " ", (subject or "")).lower()
    return any(t.lower() in text for t in tokens if t.strip()) if tokens else True


def pick_attachment(attachments: list[dict[str, Any]], extensions: list[str]) -> dict[str, Any] | None:
    """The first real file with a wanted extension; inline images and
    calendar items are not it."""
    exts = {e.lower() if e.startswith(".") else "." + e.lower() for e in extensions}
    for item in attachments:
        kind = str(item.get("@odata.type") or "")
        if kind and not kind.endswith("fileAttachment"):
            continue
        if item.get("isInline"):
            continue
        name = str(item.get("name") or "")
        if Path(name).suffix.lower() in exts:
            return item
    return None


def safe_name(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", name).strip(" .") or "attachment"
    return name[:120]


def _received_ts(message: dict[str, Any]) -> float:
    raw = str(message.get("receivedDateTime") or "")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


# -- the client ------------------------------------------------------------------


class GraphMail:
    """One signed-in mailbox. Shared per process through `shared()`."""

    def __init__(self, config: MailConfig) -> None:
        self.config = config
        self.path = token_path(config)
        self._lock = threading.Lock()
        self._app: Any = None
        self._cache: Any = None
        self._pending: dict[str, Any] | None = None
        self._error = ""
        self._checked_at = 0.0
        self._last_message: dict[str, Any] = {}

    # -- msal plumbing --------------------------------------------------------

    def _msal(self) -> Any:
        try:
            import msal  # type: ignore
        except ImportError as exc:
            raise MailUnavailable(
                "reading the inbox needs the msal package: pip install msal (or the [mail] extra)."
            ) from exc
        return msal

    def _application(self) -> Any:
        if self._app is not None:
            return self._app
        msal = self._msal()
        self._cache = msal.SerializableTokenCache()
        try:
            raw = self.path.read_bytes()
            self._cache.deserialize(_unprotect(raw).decode("utf-8"))
        except FileNotFoundError:
            pass
        except Exception as exc:  # a corrupt or foreign cache is a fresh start
            log.warning("could not read the Graph token cache at %s: %s", self.path, exc)
        self._app = msal.PublicClientApplication(
            self.config.client_id or DEFAULT_CLIENT_ID,
            authority=f"https://login.microsoftonline.com/{self.config.tenant or 'common'}",
            token_cache=self._cache,
        )
        return self._app

    def _save_cache(self) -> None:
        if self._cache is None or not self._cache.has_state_changed:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_bytes(_protect(self._cache.serialize().encode("utf-8")))
        except OSError as exc:
            log.warning("could not write the Graph token cache at %s: %s", self.path, exc)

    def account(self) -> dict[str, Any] | None:
        accounts = self._application().get_accounts()
        return accounts[0] if accounts else None

    def signed_in(self) -> bool:
        try:
            return self.account() is not None
        except MailUnavailable:
            return False

    def token(self) -> str | None:
        """An access token from the cache, refreshed if need be; None when a
        sign-in is needed."""
        with self._lock:
            app = self._application()
            account = self.account()
            if account is None:
                return None
            result = app.acquire_token_silent(SCOPES, account=account) or {}
            self._save_cache()
        if "access_token" in result:
            return result["access_token"]
        self._error = explain(result) if result else "the saved sign-in has expired; sign in again"
        return None

    # -- signing in -----------------------------------------------------------

    def begin_login(self) -> dict[str, Any]:
        """Start the device-code flow. Returns what to show the person; the
        rest happens on a thread and `status()` says how it went."""
        with self._lock:
            if self._pending and self._pending.get("expires_at", 0) > time.time():
                return dict(self._pending)
            app = self._application()
            flow = app.initiate_device_flow(scopes=SCOPES)
            if "user_code" not in flow:
                raise MailUnavailable(explain(flow))
            self._pending = {
                "user_code": flow["user_code"],
                "verification_uri": flow.get("verification_uri") or "https://microsoft.com/devicelogin",
                "message": flow.get("message", ""),
                "expires_at": time.time() + float(flow.get("expires_in") or 900),
            }
            self._error = ""
        threading.Thread(target=self._finish_login, args=(flow,), name="graph-login", daemon=True).start()
        return dict(self._pending)

    def _finish_login(self, flow: dict[str, Any]) -> None:
        try:
            result = self._application().acquire_token_by_device_flow(flow)
        except Exception as exc:  # network gone, msal upset
            result = {"error": "login_failed", "error_description": str(exc)}
        with self._lock:
            self._pending = None
            if "access_token" in result:
                self._error = ""
                self._save_cache()
                log.info("Graph: signed in as %s", (self.account() or {}).get("username", "?"))
            else:
                self._error = explain(result)
                log.warning("Graph sign-in failed: %s", self._error)

    def login_blocking(self, timeout: float = 900.0) -> bool:
        """For the CLI: start the flow and wait for it."""
        self.begin_login()
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if self._pending is None:
                    return self.signed_in() and not self._error
            time.sleep(1.0)
        return False

    def logout(self) -> None:
        with self._lock:
            app = self._application()
            for account in app.get_accounts():
                app.remove_account(account)
            self._save_cache()
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
            self._pending = None
            self._error = ""
            self._last_message = {}

    # -- reading -----------------------------------------------------------------

    def _get(self, url: str, token: str, *, raw: bool = False) -> Any:
        import requests  # type: ignore

        response = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        if response.status_code == 401:
            raise MailUnavailable("Graph refused the token; sign in again.")
        if response.status_code >= 400:
            try:
                detail = response.json().get("error", {}).get("message", "")
            except ValueError:
                detail = response.text[:200]
            raise MailUnavailable(f"Graph answered {response.status_code}: {detail}")
        return response.content if raw else response.json()

    def find_latest(self, tokens: list[str], since_ts: float, *, top: int = 40) -> dict[str, Any] | None:
        """The newest message whose subject mentions one of `tokens`, received
        since `since_ts`. None when there is none, or nobody is signed in."""
        token = self.token()
        if token is None:
            return None
        since = datetime.fromtimestamp(since_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        filters = [f"receivedDateTime ge {since}"]
        if self.config.with_attachments:
            filters.append("hasAttachments eq true")
        url = (
            f"{GRAPH}/me/messages?$filter={' and '.join(filters)}"
            f"&$orderby=receivedDateTime desc&$top={top}"
            "&$select=id,subject,from,receivedDateTime,hasAttachments,webLink"
        )
        self._checked_at = time.time()
        page = self._get(url, token)
        for message in page.get("value") or []:
            if subject_matches(message.get("subject", ""), tokens):
                sender = ((message.get("from") or {}).get("emailAddress") or {})
                found = {
                    "id": message["id"],
                    "subject": message.get("subject") or "",
                    "from": sender.get("name") or sender.get("address") or "",
                    "ts": _received_ts(message),
                    "link": message.get("webLink") or "",
                }
                self._last_message = found
                return found
        return None

    def download_attachment(self, message_id: str, extensions: list[str], folder: Path) -> Path | None:
        """Save the first wanted attachment of a message; its path, or None."""
        token = self.token()
        if token is None:
            return None
        listing = self._get(
            f"{GRAPH}/me/messages/{message_id}/attachments?$select=id,name,contentType,size,isInline",
            token,
        )
        chosen = pick_attachment(listing.get("value") or [], extensions)
        if chosen is None:
            return None
        data = self._get(f"{GRAPH}/me/messages/{message_id}/attachments/{chosen['id']}/$value", token, raw=True)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / safe_name(str(chosen.get("name") or "attachment"))
        target.write_bytes(data)
        return target

    # -- for the page and the CLI -------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            pending = dict(self._pending) if self._pending and self._pending["expires_at"] > time.time() else None
            error = self._error
            checked = self._checked_at
            last = dict(self._last_message)
        try:
            account = self.account()
            available = True
            unavailable = ""
        except MailUnavailable as exc:
            account, available, unavailable = None, False, str(exc)
        return {
            "available": available,
            "signed_in": account is not None,
            "account": (account or {}).get("username", ""),
            "pending": pending,
            "error": unavailable or error,
            "checked_at": checked,
            "last_message": last,
            "client_id": self.config.client_id or DEFAULT_CLIENT_ID,
        }


def explain(result: dict[str, Any]) -> str:
    """A Microsoft error as a sentence somebody can act on."""
    code = str(result.get("error") or "")
    text = str(result.get("error_description") or result.get("error") or "unknown error")
    first = text.split("\n")[0].split(" Trace ID")[0].strip()
    if "AADSTS65001" in text or code == "consent_required":
        return ("your organisation has to approve this app before it can read mail. Ask an "
                "administrator to grant Mail.Read, or register your own app (README, 'Reading the inbox').")
    if "AADSTS7000218" in text or "AADSTS700016" in text:
        return "that client id is not allowed here; register a public-client app and set todo.mail.client_id."
    if "AADSTS50076" in text or "AADSTS50079" in text:
        return "multi-factor sign-in is required; sign in again through the browser."
    if code in ("authorization_pending", "slow_down"):
        return "waiting for the sign-in to finish in the browser."
    if code == "expired_token":
        return "the sign-in code expired before it was used; start again."
    return first or "unknown error"


_SHARED: dict[str, GraphMail] = {}
_SHARED_LOCK = threading.Lock()


def shared(config: MailConfig) -> GraphMail:
    """One client per token file in this process, so the sign-in a command
    starts is the sign-in the sync and the page see."""
    key = str(token_path(config))
    with _SHARED_LOCK:
        client = _SHARED.get(key)
        if client is None:
            client = _SHARED[key] = GraphMail(config)
        return client
