"""The Elegoo Centauri Carbon 2, watched over the LAN.

The CC2 does not speak SDCP like the first Centauri Carbon. It runs its own
MQTT broker on port 1883 and a JSON discovery responder on UDP 52700; the
protocol below was worked out from elegoo-link and the community Home
Assistant integration (github.com/danielcherubini/elegoo-homeassistant).

* **Discovery.** ``{"id":0,"method":7000}`` broadcast to UDP 52700 comes back
  as ``{"result": {"host_name", "machine_model", "sn", "token_status"}}``.
  ``token_status`` 1 means the broker wants the access code shown on the
  printer's screen (Settings > Network).
* **Login.** Username ``elegoo``, password the access code. Then a
  registration on ``elegoo/<sn>/api_register`` with a client id and request
  id, answered on ``elegoo/<sn>/<request_id>/register_response``. The printer
  takes only a handful of clients, so exactly one process - the agent - holds
  the connection and everything else reads what it writes to the state file.
* **Status.** Method 1002 on ``elegoo/<sn>/<client_id>/api_request`` returns
  the full picture; after that ``elegoo/<sn>/api_status`` pushes deltas
  (method 6000) that are deep-merged over it. A ``{"type":"PING"}`` every ten
  seconds keeps the session alive.

Everything is read-only: nothing here starts, pauses or stops a print.
"""

from __future__ import annotations

import json
import logging
import secrets
import socket
import threading
import time
from copy import deepcopy
from typing import Any

log = logging.getLogger(__name__)

DISCOVERY_PORT = 52700
MQTT_PORT = 1883
USERNAME = "elegoo"

CMD_GET_ATTRIBUTES = 1001
CMD_GET_STATUS = 1002
EVENT_STATUS = 6000

HEARTBEAT_SECONDS = 10.0
HEARTBEAT_TIMEOUT = 65.0
# A full re-read now and then, in case a delta was missed.
REFRESH_SECONDS = 300.0

# machine_status.status
_MACHINE = {
    0: "initializing", 1: "idle", 2: "printing", 3: "loading filament",
    4: "loading filament", 5: "auto-levelling", 6: "PID calibrating",
    7: "resonance testing", 8: "self-checking", 9: "updating firmware",
    10: "homing", 11: "receiving a file", 12: "making a timelapse",
    13: "working the extruder", 14: "emergency stopped",
    15: "recovering from power loss",
}
# machine_status.sub_status while printing
_HEATING = {1045, 1096, 1405, 1906}
_PAUSED = {2501, 2502, 2505}
_STOPPED = {2503, 2504}
_FINISHED = {2077}


def discover(host: str = "", timeout: float = 3.0) -> list[dict[str, Any]]:
    """Ask the network (or one host) which CC2 printers are there."""
    found: dict[str, dict[str, Any]] = {}
    msg = json.dumps({"id": 0, "method": 7000}).encode()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.5)
        targets = [host] if host else ["255.255.255.255"]
        for target in targets:
            try:
                sock.sendto(msg, (target, DISCOVERY_PORT))
            except OSError as exc:
                log.debug("printer discovery to %s failed: %s", target, exc)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, (ip, _port) = sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                result = json.loads(data.decode("utf-8")).get("result")
            except (ValueError, UnicodeDecodeError, AttributeError):
                continue
            if not isinstance(result, dict) or not result.get("sn"):
                continue  # our own broadcast echoing back, or some other device
            found[result["sn"]] = {
                "host": ip,
                "sn": result["sn"],
                "name": result.get("host_name") or "",
                "model": result.get("machine_model") or "",
                "needs_code": result.get("token_status") == 1,
            }
            if host:
                break
    return list(found.values())


def _merge(base: dict, update: dict) -> None:
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def summarise(raw: dict[str, Any]) -> dict[str, Any]:
    """The printer's own nested status, cut down to what a person asks about."""
    machine = raw.get("machine_status") or {}
    job = raw.get("print_status") or {}
    code = machine.get("status")
    sub = machine.get("sub_status") or 0
    state = _MACHINE.get(code, "unknown") if code is not None else "unknown"
    if code == 2:
        state = (
            "heating" if sub in _HEATING
            else "paused" if sub in _PAUSED
            else "stopped" if sub in _STOPPED
            else "finished" if sub in _FINISHED
            else "printing"
        )
    progress = job.get("progress")
    if progress is None:
        progress = machine.get("progress")
    nozzle = raw.get("extruder") or {}
    bed = raw.get("heater_bed") or {}
    chamber = raw.get("ztemperature_sensor") or {}

    def temp(section: dict, key: str) -> float | None:
        value = section.get(key)
        return round(float(value), 1) if isinstance(value, (int, float)) else None

    return {
        "state": state,
        "status_code": code,
        "sub_status": sub,
        "file": job.get("filename") or "",
        "progress": int(progress) if isinstance(progress, (int, float)) else None,
        "layer": job.get("current_layer"),
        "layers": job.get("total_layer"),
        "elapsed_seconds": job.get("print_duration"),
        "remaining_seconds": job.get("remaining_time_sec"),
        "nozzle": temp(nozzle, "temperature"),
        "nozzle_target": temp(nozzle, "target"),
        "bed": temp(bed, "temperature"),
        "bed_target": temp(bed, "target"),
        "chamber": temp(chamber, "temperature"),
        "error_code": raw.get("error_code") or 0,
    }


def job_name(filename: str) -> str:
    """'Benchy_PLA_0.2mm.gcode' -> 'Benchy PLA 0.2mm', for saying aloud."""
    name = (filename or "").rsplit("/", 1)[-1]
    for ext in (".gcode.3mf", ".gcode", ".3mf"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
            break
    return name.replace("_", " ").strip() or "the print"


class PrinterWatch:
    """Holds the one MQTT session to the printer and turns it into events."""

    def __init__(self, config: Any) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._raw: dict[str, Any] = {}
        self._summary: dict[str, Any] = {}
        self._events: list[dict[str, Any]] = []
        self._connected = False
        self._error = ""
        self._last_seen = 0.0
        self._host = (config.host or "").strip()
        self._sn = (config.serial or "").strip()
        self._name = ""
        self._model = ""

    @property
    def enabled(self) -> bool:
        return bool(self._config.enabled)

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="printer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- reads --------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """What the state file carries, and what the dashboard and voice read."""
        with self._lock:
            return {
                "enabled": self.enabled,
                "connected": self._connected,
                "name": self._name or "the printer",
                "model": self._model,
                "host": self._host,
                "error": self._error,
                "last_seen": self._last_seen,
                **self._summary,
            }

    def drain_new(self) -> list[dict[str, Any]]:
        with self._lock:
            events, self._events = self._events, []
        return events

    # -- the session --------------------------------------------------------

    def _set(self, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self, "_" + key, value)

    def _run(self) -> None:
        backoff = 15.0
        while not self._stop.is_set():
            try:
                self._locate()
                outcome = self._session()
            except Exception as exc:  # the watch must never take the agent down
                log.warning("printer watch: %s", exc)
                self._set(connected=False, error=str(exc))
                outcome = "error"
            if outcome == "auth":
                wait = 600.0  # a wrong code will not fix itself; do not hammer it
            elif outcome == "ok":
                backoff, wait = 15.0, 5.0  # it was up; try again promptly
            else:
                wait, backoff = backoff, min(300.0, backoff * 2)
            self._stop.wait(wait)

    def _locate(self) -> None:
        """Fill in whatever of host and serial the config left blank."""
        if self._host and self._sn and self._name:
            return
        found = discover(self._host, timeout=3.0)
        if not found:
            raise RuntimeError(
                f"no Centauri Carbon 2 answered at {self._host}" if self._host
                else "no Centauri Carbon 2 answered on the network (is it on?)"
            )
        pick = next((p for p in found if not self._sn or p["sn"] == self._sn), found[0])
        self._set(host=pick["host"], sn=pick["sn"], name=pick["name"], model=pick["model"])

    def _session(self) -> str:
        """One connection, held until it drops. Returns ok, auth or error."""
        import paho.mqtt.client as mqtt

        sn = self._sn
        client_id = ("0cli" + format(int(time.time() * 1000), "x")[-5:]
                     + format(secrets.randbelow(4096), "x"))[:10]
        request_id = secrets.token_hex(8) + format(int(time.time() * 1000), "x")
        request_topic = f"elegoo/{sn}/{client_id}/api_request"
        registered = threading.Event()
        dropped = threading.Event()
        verdict: dict[str, str] = {}
        last_heard = [time.monotonic()]
        counter = [0]

        def send(method: int) -> None:
            counter[0] += 1
            client.publish(request_topic, json.dumps({"id": counter[0], "method": method, "params": {}}))

        def on_connect(cl, _userdata, _flags, reason, _props=None):
            if reason != 0:
                verdict["connect"] = str(reason)
                dropped.set()
                return
            for topic in (f"elegoo/{sn}/{client_id}/api_response",
                          f"elegoo/{sn}/api_status",
                          f"elegoo/{sn}/{request_id}/register_response"):
                cl.subscribe(topic)
            cl.publish(f"elegoo/{sn}/api_register",
                       json.dumps({"client_id": client_id, "request_id": request_id}))

        def on_disconnect(_cl, _userdata, *_rest):
            dropped.set()

        def on_message(_cl, _userdata, message):
            last_heard[0] = time.monotonic()
            try:
                data = json.loads(message.payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return
            if not isinstance(data, dict) or data.get("type") == "PONG":
                return
            topic = message.topic
            if topic.endswith("/register_response"):
                verdict["register"] = str(data.get("error", ""))
                registered.set()
            elif topic.endswith("/api_response") and data.get("method") == CMD_GET_STATUS:
                self._absorb(data.get("result") or {}, full=True)
            elif topic.endswith("/api_status") and data.get("method") == EVENT_STATUS:
                self._absorb(data.get("result") or {}, full=False)

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        client.username_pw_set(USERNAME, self._config.access_code or "")
        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message
        client.connect(self._host, MQTT_PORT, keepalive=60)
        client.loop_start()
        try:
            registered.wait(8.0)
            if "connect" in verdict:
                bad_code = "authori" in verdict["connect"].lower() or "password" in verdict["connect"].lower()
                message = (
                    "the printer refused the access code - set printer.access_code to the code "
                    "under Settings > Network on its screen" if bad_code
                    else f"the printer refused the connection ({verdict['connect']})"
                )
                if self._error != message:
                    log.warning("printer: %s", message)
                self._set(connected=False, error=message)
                return "auth" if bad_code else "error"
            if verdict.get("register") != "ok":
                message = f"the printer would not register this client ({verdict.get('register') or 'no answer'})"
                log.warning("printer: %s", message)
                self._set(connected=False, error=message)
                return "error"

            log.info("printer: connected to %s at %s", self._name or sn, self._host)
            self._set(connected=True, error="")
            send(CMD_GET_ATTRIBUTES)
            send(CMD_GET_STATUS)
            next_ping = next_refresh = time.monotonic()
            next_refresh += REFRESH_SECONDS
            while not self._stop.is_set() and not dropped.is_set():
                now = time.monotonic()
                if now - last_heard[0] > HEARTBEAT_TIMEOUT:
                    log.warning("printer: nothing heard for %.0fs; reconnecting", now - last_heard[0])
                    break
                if now >= next_ping:
                    client.publish(request_topic, json.dumps({"type": "PING"}))
                    next_ping = now + HEARTBEAT_SECONDS
                if now >= next_refresh:
                    send(CMD_GET_STATUS)
                    next_refresh = now + REFRESH_SECONDS
                self._stop.wait(1.0)
            return "ok"
        finally:
            was_printing = self._summary.get("state") in ("printing", "heating", "paused")
            self._set(connected=False)
            if was_printing and not self._stop.is_set():
                self._emit("lost", self._summary)
            client.loop_stop()
            try:
                client.disconnect()
            except Exception:
                pass

    # -- turning status into events ----------------------------------------

    def _absorb(self, frame: dict[str, Any], *, full: bool) -> None:
        with self._lock:
            if full or not self._raw:
                self._raw = deepcopy(frame)
            else:
                _merge(self._raw, frame)
            before = self._summary
            after = summarise(self._raw)
            self._summary = after
            self._last_seen = time.time()
        self._compare(before, after)

    def _emit(self, kind: str, summary: dict[str, Any]) -> None:
        event = {"kind": kind, "ts": time.time(), "name": self._name or "the printer", **summary}
        log.info("printer: %s (%s)", kind, summary.get("file") or "-")
        with self._lock:
            self._events.append(event)

    def _compare(self, before: dict[str, Any], after: dict[str, Any]) -> None:
        if not before:
            return  # the first picture is a baseline, not news
        was, now = before.get("state"), after.get("state")
        active = ("printing", "heating")
        if now != was:
            if now in active and was not in active + ("paused",):
                self._emit("started", after)
            elif now == "paused":
                self._emit("paused", after)
            elif now in active and was == "paused":
                self._emit("resumed", after)
            elif now == "finished":
                self._emit("finished", after)
            elif now == "stopped":
                self._emit("stopped", after)
            elif now == "idle" and was in active and (before.get("progress") or 0) >= 99:
                # Some firmware skips the "completed" sub-state and goes
                # straight back to idle.
                self._emit("finished", before)
            elif now == "emergency stopped":
                self._emit("error", after)
        code = after.get("error_code") or 0
        if code and code != (before.get("error_code") or 0):
            self._emit("error", after)
