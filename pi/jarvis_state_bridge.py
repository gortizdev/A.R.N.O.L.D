"""Relay Jarvis's live state onto MQTT so the PC's face can follow it.

Jarvis publishes state over Server-Sent Events on 127.0.0.1:8765 (assistant.py
:319-469), which is loopback-only and therefore invisible to the Windows PC.
This runs *on the Pi*, where that endpoint is reachable, and republishes the
interesting events to Mosquitto - which the PC can already reach and
authenticate against.

Two events matter for the face:

* ``state`` - idle / listening / thinking / speaking
* ``eq``    - 16 audio-band levels while Jarvis speaks, which drives lip-sync

Everything else (timers, alarms, volume, weight, telemetry) is forwarded too,
so other consumers can use it.

Install as a user service:

    cp jarvis_state_bridge.py /home/parzival/voiceassistant/
    # see jarvis-state-bridge.service in this directory
    systemctl --user enable --now jarvis-state-bridge
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
import urllib.error
import urllib.request

try:
    import paho.mqtt.client as mqtt
except ImportError:
    sys.exit("paho-mqtt is required: pip3 install paho-mqtt")

log = logging.getLogger("jarvis-state-bridge")

HUD_URL = os.environ.get("JARVIS_HUD_URL", "http://127.0.0.1:8765/events")
MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
TOPIC_PREFIX = os.environ.get("JARVIS_TOPIC_PREFIX", "jarvis/hud")

# Forwarded on every update. `eq` arrives many times a second during speech, so
# it is published at QoS 0 without retain - a dropped frame of lip-sync is
# invisible, whereas a retained one would leave the mouth stuck open.
REALTIME_EVENTS = {"eq"}
# Retained so a face starting up immediately knows what Jarvis is doing.
RETAINED_EVENTS = {"state", "vol", "alarmvol", "timers", "alarms"}

_running = True


def _stop(signum, frame):
    global _running
    _running = False
    log.info("signal %s received, stopping", signum)


def iter_sse(url: str, timeout: float = 45.0):
    """Yield parsed `data:` payloads from an SSE stream.

    The server sends a bare `: ping` comment every ~15s (assistant.py:445-449);
    those keep the socket warm and are skipped here.
    """
    request = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    with urllib.request.urlopen(request, timeout=timeout) as stream:
        for raw in stream:
            if not _running:
                return
            line = raw.decode("utf-8", "replace").strip()
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if not body:
                continue
            try:
                yield json.loads(body)
            except json.JSONDecodeError:
                log.debug("skipping non-JSON event: %.80s", body)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hud-url", default=HUD_URL)
    parser.add_argument("--mqtt-host", default=MQTT_HOST)
    parser.add_argument("--mqtt-port", type=int, default=MQTT_PORT)
    parser.add_argument("--mqtt-user", default=MQTT_USER)
    parser.add_argument("--mqtt-password", default=MQTT_PASSWORD)
    parser.add_argument("--prefix", default=TOPIC_PREFIX)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="jarvis-state-bridge")
    if args.mqtt_user:
        client.username_pw_set(args.mqtt_user, args.mqtt_password)
    availability = "{}/bridge".format(args.prefix.rstrip("/"))
    client.will_set(availability, "offline", qos=1, retain=True)
    client.reconnect_delay_set(min_delay=2, max_delay=60)

    try:
        client.connect(args.mqtt_host, args.mqtt_port, 45)
    except OSError as exc:
        log.error("cannot reach the broker at %s:%s - %s", args.mqtt_host, args.mqtt_port, exc)
        return 1
    client.loop_start()
    client.publish(availability, "online", qos=1, retain=True)
    log.info("publishing Jarvis state to %s/# on %s", args.prefix, args.mqtt_host)

    backoff = 1.0
    try:
        while _running:
            try:
                for event in iter_sse(args.hud_url):
                    kind = event.get("t")
                    if not kind:
                        continue
                    value = event.get("v")
                    topic = "{}/{}".format(args.prefix.rstrip("/"), kind)
                    payload = value if isinstance(value, str) else json.dumps(value)
                    client.publish(
                        topic,
                        payload,
                        qos=0 if kind in REALTIME_EVENTS else 1,
                        retain=kind in RETAINED_EVENTS,
                    )
                    if kind == "state":
                        log.info("jarvis state -> %s", value)
                    backoff = 1.0
            except urllib.error.URLError as exc:
                log.warning(
                    "Jarvis HUD unreachable (%s); retrying in %.0fs. "
                    "Is jarvis-assistant.service running?",
                    getattr(exc, "reason", exc), backoff,
                )
            except Exception as exc:
                log.warning("stream error (%s); retrying in %.0fs", exc, backoff)

            if not _running:
                break
            # The stream also ends normally when Jarvis restarts; back off so a
            # crash-looping assistant is not hammered.
            time.sleep(backoff)
            backoff = min(30.0, backoff * 1.8)
    finally:
        client.publish(availability, "offline", qos=1, retain=True)
        time.sleep(0.3)
        client.loop_stop()
        client.disconnect()
        log.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
