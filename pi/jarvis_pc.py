"""Call the Windows PC agent from Jarvis.

Drop this next to assistant.py on the Pi and import it. Two transports:

* ``SshPcClient`` (default) reuses the SSH trust Jarvis already has - the
  ``~/.ssh/jarvis_pc`` key it uses in ``_pc_ssh()`` (assistant.py:3014). No new
  credentials, no listener, works whether or not Mosquitto is up.
* ``MqttPcClient`` publishes signed commands to Mosquitto and waits for the
  reply. Lower latency per call and no SSH handshake, but needs MQTT
  credentials and the shared secret.

Both return the agent's JSON, whose ``speech`` field is a finished sentence
ready to hand to Jarvis's announcer.

    from jarvis_pc import SshPcClient

    pc = SshPcClient()
    pc.ask("query.disk", drive="C")["speech"]
    # 'Drive C has 91 gigabytes free of 931 gigabytes, 90 percent used.'

Python 3.9+; the MQTT client additionally needs paho-mqtt.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import subprocess
import time
import uuid
from typing import Any, Dict, Optional

# --- match these to the PC's config.yaml -----------------------------------

PC_HOST = os.environ.get("PC_HOST", "192.168.1.103")
PC_USER = os.environ.get("PC_USER", "geogo")
PC_SSH_KEY = os.environ.get("PC_SSH_KEY", "/home/parzival/.ssh/jarvis_pc")

# Full path, not a bare name: the PC's SSH shell is PowerShell, and a
# non-login session does not have the project's virtualenv on PATH.
PC_AGENT_EXE = os.environ.get(
    "PC_AGENT_EXE",
    "C:/Users/geogo/Downloads/Projects/ARNOLD/.venv/Scripts/arnold.exe",
)

# Same value as security.shared_secret on the PC.
SHARED_SECRET = os.environ.get("CA_SHARED_SECRET", "")

MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "jarvis")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")

BASE_TOPIC = os.environ.get("CA_BASE_TOPIC", "computer_assistant")
DEVICE_ID = os.environ.get("CA_DEVICE_ID", "desktop_rssbqj2")


class PcError(RuntimeError):
    """The PC could not be reached, or refused the command."""


def _canonical(payload: Dict[str, Any]) -> bytes:
    """Must match security.canonical_bytes on the PC exactly."""
    body = {k: v for k, v in payload.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def sign_command(
    cmd: str,
    args: Optional[Dict[str, Any]] = None,
    *,
    secret: str = "",
    reply_to: str = "",
    speak: bool = False,
) -> Dict[str, Any]:
    """Build a signed command envelope the PC agent will accept."""
    envelope: Dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "ts": time.time(),
        "cmd": cmd,
        "args": args or {},
        "nonce": secrets.token_hex(8),
    }
    if reply_to:
        envelope["reply_to"] = reply_to
    if speak:
        envelope["speak"] = True

    secret = secret or SHARED_SECRET
    if secret:
        envelope["sig"] = hmac.new(
            secret.encode("utf-8"), _canonical(envelope), hashlib.sha256
        ).hexdigest()
    return envelope


class SshPcClient:
    """Runs `arnold exec` on the PC over SSH.

    Preferred: it reuses the key Jarvis already has, so there is nothing new to
    provision and no dependency on the broker being up.
    """

    def __init__(
        self,
        host: str = PC_HOST,
        user: str = PC_USER,
        key_path: str = PC_SSH_KEY,
        timeout: float = 20.0,
        agent_exe: str = PC_AGENT_EXE,
    ) -> None:
        self.host = host
        self.user = user
        self.key_path = key_path
        self.timeout = timeout
        self.agent_exe = agent_exe

    def ask(self, command: str, **args: Any) -> Dict[str, Any]:
        argv = [
            "ssh",
            "-i", self.key_path,
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5",
            f"{self.user}@{self.host}",
            self.agent_exe, "exec", command,
        ]
        # Values go as separate argv entries, never interpolated into a shell
        # string, so a transcribed app name with a space or quote is harmless.
        for key, value in args.items():
            argv += ["--arg", "{}={}".format(key, value)]

        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=self.timeout
            )
        except subprocess.TimeoutExpired as exc:
            raise PcError("the PC did not answer within {:.0f}s".format(self.timeout)) from exc
        except FileNotFoundError as exc:
            raise PcError("ssh is not installed on the Pi") from exc

        stdout = (proc.stdout or "").strip()
        if not stdout:
            stderr = (proc.stderr or "").strip()[:300]
            if "Permission denied" in stderr:
                raise PcError(
                    "the PC refused the SSH key. Check that {} is in the PC's "
                    "authorized_keys.".format(self.key_path)
                )
            if "not recognized" in stderr or "CommandNotFoundException" in stderr:
                raise PcError(
                    "the PC could not find the agent at {}. Set PC_AGENT_EXE to the full "
                    "path of arnold.exe - a non-login SSH session does not "
                    "have the virtualenv on PATH.".format(self.agent_exe)
                )
            raise PcError("no reply from the PC: {}".format(stderr or "empty output"))

        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise PcError("the PC returned something that was not JSON: {}".format(stdout[:200])) from exc

    def say_answer(self, command: str, **args: Any) -> str:
        """Ask, and return just the sentence to speak."""
        try:
            reply = self.ask(command, **args)
        except PcError as exc:
            return "I couldn't reach the desktop. {}".format(exc)
        if not reply.get("ok", True):
            return reply.get("error") or "The desktop couldn't do that."
        return reply.get("speech") or "Done."


class MqttPcClient:
    """Publishes signed commands over MQTT and waits for the reply."""

    def __init__(
        self,
        host: str = MQTT_HOST,
        port: int = MQTT_PORT,
        username: str = MQTT_USER,
        password: str = MQTT_PASSWORD,
        device_id: str = DEVICE_ID,
        base_topic: str = BASE_TOPIC,
        secret: str = "",
    ) -> None:
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.root = "{}/{}".format(base_topic, device_id)
        self.secret = secret or SHARED_SECRET

    def ask(self, command: str, timeout: float = 15.0, **args: Any) -> Dict[str, Any]:
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise PcError("paho-mqtt is not installed on the Pi") from exc

        import queue

        replies: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        reply_topic = "{}/cmd/result/jarvis-{}".format(self.root, secrets.token_hex(4))

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if self.username:
            client.username_pw_set(self.username, self.password)

        def on_connect(c, userdata, flags, reason_code, properties=None):
            c.subscribe(reply_topic, qos=1)

        def on_message(c, userdata, message):
            try:
                replies.put(json.loads(message.payload.decode("utf-8")))
            except (ValueError, UnicodeDecodeError):
                pass

        client.on_connect = on_connect
        client.on_message = on_message

        try:
            client.connect(self.host, self.port, 30)
        except OSError as exc:
            raise PcError("could not reach the MQTT broker: {}".format(exc)) from exc

        client.loop_start()
        try:
            envelope = sign_command(
                command, args, secret=self.secret, reply_to=reply_topic
            )
            client.publish(
                "{}/cmd".format(self.root), json.dumps(envelope), qos=1
            )
            try:
                return replies.get(timeout=timeout)
            except queue.Empty:
                raise PcError("the PC agent did not reply - is it running?") from None
        finally:
            client.loop_stop()
            client.disconnect()

    def say_answer(self, command: str, **args: Any) -> str:
        try:
            reply = self.ask(command, **args)
        except PcError as exc:
            return "I couldn't reach the desktop. {}".format(exc)
        if not reply.get("ok", True):
            return reply.get("error") or "The desktop couldn't do that."
        return reply.get("speech") or "Done."


# A phrase -> command mapping to wire into Jarvis's intent handling. Extend it
# rather than parsing free text at the call site.
INTENTS = {
    "pc status": ("query.system", {}),
    "pc cpu": ("query.cpu", {}),
    "pc memory": ("query.memory", {}),
    "pc disk": ("query.disk", {}),
    "pc gpu": ("query.gpu", {}),
    "pc uptime": ("query.uptime", {}),
    "pc network": ("query.network", {}),
    "pc alerts": ("query.alerts", {}),
    "what is on screen": ("query.active_window", {}),
    "lock the pc": ("control.lock", {}),
    "mute the pc": ("control.mute", {"muted": True}),
    "unmute the pc": ("control.mute", {"muted": False}),
    "pause the music": ("control.media", {"action": "pause"}),
    "next track": ("control.media", {"action": "next"}),
}


def handle_intent(phrase: str, client: Optional[SshPcClient] = None) -> Optional[str]:
    """Map a spoken phrase to a command. Returns None if nothing matches."""
    entry = INTENTS.get(phrase.strip().lower())
    if entry is None:
        return None
    command, args = entry
    return (client or SshPcClient()).say_answer(command, **args)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("usage: python3 jarvis_pc.py <command> [key=value ...]")
        print("       python3 jarvis_pc.py query.disk drive=C")
        raise SystemExit(2)

    kwargs = {}
    for pair in sys.argv[2:]:
        key, _, value = pair.partition("=")
        kwargs[key] = value

    pc = SshPcClient()
    try:
        print(json.dumps(pc.ask(sys.argv[1], **kwargs), indent=2))
    except PcError as exc:
        print("error: {}".format(exc), file=sys.stderr)
        raise SystemExit(1)
