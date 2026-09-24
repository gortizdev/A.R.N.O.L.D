"""Windows toast notifications, driven through PowerShell's WinRT bridge.

Uses no third-party package: Windows 10 ships the ToastNotificationManager API
and PowerShell can reach it. Title and body are passed via environment
variables rather than interpolated into the script text, so a notification body
containing quotes or `$(...)` cannot inject PowerShell.
"""

from __future__ import annotations

import logging
import os
import subprocess

from .. import process
from . import require_windows

log = logging.getLogger(__name__)

# Toasts must be attributed to a registered AppUserModelID or Windows silently
# drops them. PowerShell's own shortcut ID is present on every Win10 install.
_DEFAULT_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null

$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$nodes = $template.GetElementsByTagName('text')
$nodes.Item(0).AppendChild($template.CreateTextNode($env:CA_TOAST_TITLE)) | Out-Null
$nodes.Item(1).AppendChild($template.CreateTextNode($env:CA_TOAST_BODY)) | Out-Null

$toast = New-Object Windows.UI.Notifications.ToastNotification $template
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:CA_TOAST_APPID).Show($toast)
"""


def notify(title: str, message: str, *, app_id: str = "", timeout: float = 20.0) -> None:
    """Show a Windows toast. Raises on failure so callers can report it back."""
    require_windows("toast notifications")

    env = dict(os.environ)
    env["CA_TOAST_TITLE"] = (title or "Arnold")[:120]
    env["CA_TOAST_BODY"] = (message or "")[:400]
    env["CA_TOAST_APPID"] = app_id or _DEFAULT_APP_ID

    proc = process.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            _SCRIPT,
        ],
        env=env,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"toast failed: {(proc.stderr or proc.stdout).strip()[:300]}")
