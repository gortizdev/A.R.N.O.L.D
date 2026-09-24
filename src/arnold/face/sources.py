"""Where the face gets its mood from.

Two independent feeds, either of which may be absent:

* **local** - the agent's state file. Always available when the agent runs, and
  tells the face when an alert is firing or when the agent has stopped.
* **voice** - the assistant's live state over MQTT, and this PC's own voice
  session over loopback UDP too (face/local.py), so the face keeps reacting
  when the broker on the Pi is down. Normally that is this
  PC's own voice session, on its own topic prefix. With face.follow_jarvis
  (or when mirroring Jarvis) it is also Jarvis's state, relayed by the
  Pi-side bridge (pi/jarvis_state_bridge.py) - his HTTP API is loopback-only
  on the Pi, so it cannot be read from here directly.

The voice wins when it has something to say, because "listening" and
"speaking" are more interesting than "idle". A firing alert outranks both.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from ..config import Config
from ..state import read_state
from .local import LocalFaceListener
from .state import FaceMode

log = logging.getLogger(__name__)

# Jarvis state older than this is treated as unknown rather than current.
JARVIS_STALE_SECONDS = 25.0
# While the local feed has spoken this recently, the broker's copies of the
# same messages are dropped - otherwise every transcript line arrives twice.
LOCAL_FRESH_SECONDS = 30.0
# Turns of conversation kept in memory. The face ignores these; the dashboard
# shows them, and nothing else needs a second subscriber for the same topic.
TRANSCRIPT_TURNS = 60
# How Jarvis's own state strings map onto face modes.
JARVIS_MODES = {
    "idle": FaceMode.IDLE,
    "listening": FaceMode.LISTENING,
    "thinking": FaceMode.THINKING,
    "processing": FaceMode.THINKING,
    "speaking": FaceMode.SPEAKING,
    "talking": FaceMode.SPEAKING,
    "responding": FaceMode.SPEAKING,
}


class FaceFeed:
    """Aggregates the feeds into a single mode plus audio levels."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._lock = threading.Lock()

        self._jarvis_mode: FaceMode | None = None
        self._jarvis_seen = 0.0
        self._levels: list[float] = []
        # Mouth shape when the source computed it for us, else None.
        self._mouth: tuple[float, float] | None = None
        self._levels_seen = 0.0

        self._alerts: list[str] = []
        self._agent_running = False
        self._stop = threading.Event()
        self._mqtt: Any = None

        self._transcript: deque[dict[str, Any]] = deque(maxlen=TRANSCRIPT_TURNS)
        self._wake_owner = ""
        self._local: LocalFaceListener | None = None
        self._local_seen = 0.0

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        threading.Thread(target=self._poll_local, name="face-local", daemon=True).start()
        self._local = LocalFaceListener(self._config, self._from_local)
        self._local.start()
        if self._config.mqtt.enabled:
            threading.Thread(target=self._run_mqtt, name="face-mqtt", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self._local is not None:
            self._local.stop()
        if self._mqtt is not None:
            try:
                self._mqtt.disconnect()
            except Exception:
                pass

    # -- local agent state --------------------------------------------------

    def _poll_local(self) -> None:
        path = Path(self._config.state_file)
        while not self._stop.is_set():
            state = read_state(path)
            with self._lock:
                if state is None:
                    self._agent_running = False
                    self._alerts = []
                else:
                    self._agent_running = True
                    active = state.get("alerts", {}).get("active", [])
                    self._alerts = active if isinstance(active, list) else []
            self._stop.wait(2.0)

    # -- Jarvis state over MQTT --------------------------------------------

    def _run_mqtt(self) -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            log.warning("paho-mqtt missing; the face will use local state only")
            return

        cfg = self._config.mqtt
        own = self._config.assistant_topic_prefix()
        jarvis = self._config.face.jarvis_topic_prefix.rstrip("/")
        prefixes = [own]
        if self._config.face.follow_jarvis and jarvis != own:
            prefixes.append(jarvis)

        def on_connect(client, userdata, flags, reason_code, properties=None):
            if reason_code != 0:
                log.warning("face mqtt refused: %s", reason_code)
                return
            for prefix in prefixes:
                client.subscribe(f"{prefix}/#", qos=0)
            # The wake claim is on Jarvis's prefix whoever holds it.
            if jarvis not in prefixes:
                client.subscribe(f"{jarvis}/wake_owner", qos=0)
            log.info(
                "face subscribed to %s for %s's state",
                ", ".join(f"{p}/#" for p in prefixes),
                self._config.assistant_name(),
            )

        def on_message(client, userdata, message):
            try:
                raw = message.payload.decode("utf-8")
            except UnicodeDecodeError:
                return
            topic = message.topic
            if topic.startswith(own + "/") and time.time() - self._local_seen < LOCAL_FRESH_SECONDS:
                return  # already had it over loopback
            self._ingest(topic, raw)

        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            # Unique per process: the face window and the dashboard inside
            # the agent both run a feed, and two clients sharing one id take
            # turns being kicked off the broker every couple of seconds -
            # each reconnect re-delivering the retained state on the way in.
            client_id=f"{cfg.client_id}-face-{secrets.token_hex(3)}",
        )
        if cfg.username:
            client.username_pw_set(cfg.username, cfg.password)
        client.on_connect = on_connect
        client.on_message = on_message
        client.reconnect_delay_set(min_delay=2, max_delay=60)
        self._mqtt = client

        try:
            client.connect_async(cfg.host, cfg.port, cfg.keepalive)
            client.loop_start()
        except Exception as exc:
            log.warning("face could not reach the broker: %s", exc)
            return

        self._stop.wait()
        client.loop_stop()

    def _from_local(self, topic: str, raw: str) -> None:
        """A message from this PC's voice session, straight over loopback."""
        if not topic.startswith(self._config.assistant_topic_prefix() + "/"):
            return
        self._local_seen = time.time()
        self._ingest(topic, raw)

    def _ingest(self, topic: str, raw: str) -> None:
        leaf = topic.rsplit("/", 1)[-1]
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw.strip()

        now = time.time()
        with self._lock:
            if leaf == "state":
                text = str(value).strip().lower()
                self._jarvis_mode = JARVIS_MODES.get(text)
                self._jarvis_seen = now
                if self._jarvis_mode is not FaceMode.SPEAKING:
                    # Drop the mouth as soon as speech ends, rather than
                    # letting the last levels linger.
                    self._levels = []
                    self._mouth = None
            elif leaf == "eq":
                # Two shapes. A bare list is the Pi bridge's equaliser
                # levels; a mapping is the PC's own session, which has
                # already worked out the mouth from the raw audio and
                # timed the message to when that audio is heard.
                if isinstance(value, list):
                    self._levels = [
                        float(v) for v in value if isinstance(v, (int, float))
                    ]
                    self._mouth = None
                    self._levels_seen = now
                elif isinstance(value, dict):
                    bands = value.get("bands")
                    self._levels = (
                        [float(v) for v in bands if isinstance(v, (int, float))]
                        if isinstance(bands, list)
                        else []
                    )
                    try:
                        self._mouth = (
                            float(value["open"]),
                            float(value["wide"]),
                        )
                    except (KeyError, TypeError, ValueError):
                        self._mouth = None
                    self._levels_seen = now
            elif leaf == "transcript" and isinstance(value, dict):
                text = str(value.get("text") or "").strip()
                if text:
                    self._transcript.append(
                        {
                            "who": str(value.get("who") or "assistant"),
                            "text": text[:2000],
                            "ts": now,
                        }
                    )
            elif leaf == "wake_owner":
                self._wake_owner = str(value or "").strip()

    # -- aggregation --------------------------------------------------------

    def current(self) -> tuple[FaceMode, list[float], tuple[float, float] | None]:
        """Mode, equaliser levels, and the mouth shape if the source sent one."""
        now = time.time()
        with self._lock:
            alerts = list(self._alerts)
            running = self._agent_running
            jarvis_mode = self._jarvis_mode
            jarvis_fresh = (now - self._jarvis_seen) < JARVIS_STALE_SECONDS
            fresh = (now - self._levels_seen) < 1.5
            levels = list(self._levels) if fresh else []
            mouth = self._mouth if fresh else None

        if not running:
            return FaceMode.OFFLINE, [], None

        # An active alert is the most important thing the face can convey, but
        # not while Jarvis is mid-sentence - interrupting the lip-sync to flash
        # amber would look like a glitch.
        if jarvis_fresh and jarvis_mode in (FaceMode.SPEAKING, FaceMode.LISTENING, FaceMode.THINKING):
            return jarvis_mode, levels, mouth
        if alerts:
            return FaceMode.ALERT, [], None
        if jarvis_fresh and jarvis_mode is not None:
            return jarvis_mode, levels, mouth
        return FaceMode.IDLE, [], None

    def details(self) -> dict[str, Any]:
        """Everything the feed knows, for a reader with more room than a face."""
        now = time.time()
        with self._lock:
            fresh = (now - self._jarvis_seen) < JARVIS_STALE_SECONDS
            return {
                "agent_running": self._agent_running,
                "alerts": list(self._alerts),
                "voice_state": (
                    self._jarvis_mode.value if fresh and self._jarvis_mode else None
                ),
                "voice_seen": self._jarvis_seen or None,
                "wake_owner": self._wake_owner,
                "transcript": list(self._transcript),
            }

    def status_text(self) -> str:
        """Human-readable summary for the context menu."""
        now = time.time()
        with self._lock:
            bits = [f"agent: {'running' if self._agent_running else 'not running'}"]
            if self._alerts:
                bits.append(f"alerts: {', '.join(self._alerts)}")
            name = self._config.assistant_name().lower()
            if (now - self._jarvis_seen) < JARVIS_STALE_SECONDS:
                bits.append(f"{name}: {self._jarvis_mode.value if self._jarvis_mode else 'unknown'}")
            else:
                bits.append(f"{name}: no voice session")
        return " | ".join(bits)
