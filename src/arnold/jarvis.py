"""Talking to Jarvis on the Pi.

Jarvis's own HTTP API (`HudBroadcaster`, assistant.py:319-469) binds to
127.0.0.1:8765 and is therefore unreachable from this machine. Two routes work
around that, both landing on the same `/say` endpoint:

* ``home_assistant`` (default) - POST to Home Assistant's REST API on
  :8123 and let an HA ``rest_command`` relay the text. HA runs on the Pi, so
  it *can* reach the loopback endpoint. Authenticated, LAN-native, and needs
  nothing held open. Requires ``rest_command.jarvis_say`` to exist in HA's
  configuration - see pi/homeassistant/configuration.snippet.yaml.
* ``ssh`` - shell out to ssh and curl the loopback endpoint from the Pi
  itself. No HA dependency, but needs an SSH key from this PC to the Pi
  (which does not exist by default - the ``~/.ssh/jarvis_pc`` key runs the
  other direction, Pi to PC).

Text is never interpolated into a shell string; on the SSH route it travels
over stdin so a spoken message containing quotes or backticks cannot inject.
"""

from __future__ import annotations

import json
import logging
import subprocess
import urllib.error
import urllib.request
from typing import Any

from . import process
from .config import JarvisConfig

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 500  # Jarvis truncates at 500 (assistant.py:384-390)

_REMOTE_POST = (
    "curl -sS --max-time 8 -X POST -H 'Content-Type: application/json' "
    "--data-binary @- http://127.0.0.1:{port}{path}"
)


class JarvisError(RuntimeError):
    pass


class JarvisClient:
    def __init__(self, config: JarvisConfig) -> None:
        self._config = config
        self.route = config.speech_route if config.enabled else "none"

    @property
    def enabled(self) -> bool:
        return self.route != "none"

    # -- public API ---------------------------------------------------------

    def say(self, message: str) -> dict[str, Any]:
        """Speak `message` through Jarvis. Raises JarvisError on failure."""
        text = (message or "").strip()
        if not text:
            raise JarvisError("nothing to say")
        text = text[:MAX_MESSAGE_CHARS]

        if self.route == "none":
            log.debug("speech route is 'none'; not speaking: %s", text)
            return {"spoken": False, "route": "none", "message": text}

        if self.route == "home_assistant":
            self._say_via_home_assistant(text)
        elif self.route == "ssh":
            self._post_via_ssh("/say", {"message": text})
        else:
            raise JarvisError(f"unknown speech route {self.route!r}")

        log.info("jarvis said: %s", text)
        return {"spoken": True, "route": self.route, "message": text}

    def set_volume(self, level: int) -> dict[str, Any]:
        """Set Jarvis's own speaker volume (SSH route only - HA has no relay)."""
        self._require_ssh("setting Jarvis's volume")
        self._post_via_ssh("/volume", {"level": int(level)})
        return {"ok": True, "level": int(level)}

    def alarm(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Set or cancel a Jarvis alarm (SSH route only)."""
        self._require_ssh("controlling Jarvis alarms")
        self._post_via_ssh("/alarm", payload)
        return {"ok": True, "payload": payload}

    def check(self) -> dict[str, Any]:
        """Probe the configured route without speaking. Never raises."""
        if not self._config.enabled:
            return {"ok": False, "route": "none", "detail": "the Pi is switched off (jarvis.enabled)"}
        if self.route == "none":
            return {"ok": True, "route": "none", "detail": "speech disabled"}
        try:
            if self.route == "home_assistant":
                return self._check_home_assistant()
            return self._check_ssh()
        except Exception as exc:
            return {"ok": False, "route": self.route, "detail": str(exc)}

    # -- Home Assistant route ----------------------------------------------

    def _say_via_home_assistant(self, text: str) -> None:
        ha = self._config.home_assistant
        if not ha.token:
            raise JarvisError(
                "jarvis.home_assistant.token is empty - create a long-lived access "
                "token on your Home Assistant profile page"
            )

        url = f"{ha.url.rstrip('/')}/api/services/{ha.say_service.strip('/')}"
        body = json.dumps({ha.message_field: text}).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {ha.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=ha.timeout_seconds) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code == 401:
                raise JarvisError(
                    "Home Assistant rejected the token (401). Check jarvis.home_assistant.token."
                ) from exc
            if exc.code == 400:
                raise JarvisError(
                    f"Home Assistant rejected the call (400): {detail}. "
                    f"Does rest_command.{ha.say_service.split('/')[-1]} exist in its config?"
                ) from exc
            raise JarvisError(f"Home Assistant returned {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise JarvisError(f"could not reach Home Assistant at {ha.url}: {exc.reason}") from exc

    def _check_home_assistant(self) -> dict[str, Any]:
        ha = self._config.home_assistant
        if not ha.token:
            return {"ok": False, "route": "home_assistant", "detail": "no token configured"}

        request = urllib.request.Request(
            f"{ha.url.rstrip('/')}/api/",
            headers={"Authorization": f"Bearer {ha.token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=ha.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return {
                "ok": True,
                "route": "home_assistant",
                "detail": payload.get("message", "connected"),
                "url": ha.url,
            }
        except urllib.error.HTTPError as exc:
            hint = " (token rejected)" if exc.code == 401 else ""
            return {"ok": False, "route": "home_assistant", "detail": f"HTTP {exc.code}{hint}"}
        except urllib.error.URLError as exc:
            return {"ok": False, "route": "home_assistant", "detail": f"unreachable: {exc.reason}"}

    # -- SSH route ----------------------------------------------------------

    def _require_ssh(self, what: str) -> None:
        if self.route != "ssh":
            raise JarvisError(
                f"{what} needs jarvis.speech_route: ssh - Home Assistant only relays /say"
            )

    def _ssh_argv(self) -> list[str]:
        ssh = self._config.ssh
        argv = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=accept-new",
        ]
        if ssh.port != 22:
            argv += ["-p", str(ssh.port)]
        if ssh.key_path:
            argv += ["-i", ssh.key_path]
        argv.append(f"{ssh.user}@{ssh.host}")
        return argv

    def _post_via_ssh(self, path: str, payload: dict[str, Any]) -> None:
        ssh = self._config.ssh
        argv = self._ssh_argv() + [_REMOTE_POST.format(port=ssh.hud_port, path=path)]

        try:
            proc = process.run(
                argv,
                input=json.dumps(payload),
                timeout=ssh.timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise JarvisError("ssh is not on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise JarvisError(f"ssh to {ssh.host} timed out") from exc

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()[:300]
            if "Permission denied" in stderr or "publickey" in stderr:
                raise JarvisError(
                    f"ssh to {ssh.user}@{ssh.host} was refused. This PC needs its own key on "
                    f"the Pi: ssh-keygen, then add the public key to the Pi's authorized_keys."
                )
            raise JarvisError(f"ssh failed: {stderr or 'exit ' + str(proc.returncode)}")

        stdout = (proc.stdout or "").strip()
        if stdout:
            try:
                response = json.loads(stdout)
                if isinstance(response, dict) and response.get("ok") is False:
                    raise JarvisError(f"Jarvis rejected the request: {response.get('error')}")
            except json.JSONDecodeError:
                log.debug("non-JSON reply from Jarvis %s: %s", path, stdout[:200])

    def _check_ssh(self) -> dict[str, Any]:
        ssh = self._config.ssh
        argv = self._ssh_argv() + [
            f"curl -sS --max-time 5 -o /dev/null -w '%{{http_code}}' "
            f"http://127.0.0.1:{ssh.hud_port}/events"
        ]
        try:
            proc = process.run(argv, timeout=ssh.timeout_seconds)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "route": "ssh", "detail": str(exc)}

        if proc.returncode != 0:
            return {"ok": False, "route": "ssh", "detail": (proc.stderr or "").strip()[:200]}
        return {
            "ok": True,
            "route": "ssh",
            "detail": f"reached Jarvis on {ssh.host}:{ssh.hud_port}",
        }
