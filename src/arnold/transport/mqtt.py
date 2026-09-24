"""MQTT client wrapper around paho-mqtt 2.x.

The Pi's Mosquitto sets `allow_anonymous false`, so credentials are mandatory.
Availability uses a retained last-will: the broker publishes `offline` on the
status topic if this process dies without saying goodbye, which is what makes
Home Assistant show the device as unavailable rather than stale.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from typing import Any, Callable

import paho.mqtt.client as mqtt

from ..config import MqttConfig
from .topics import Topics

log = logging.getLogger(__name__)

PAYLOAD_ONLINE = "online"
PAYLOAD_OFFLINE = "offline"

MessageHandler = Callable[[str, dict[str, Any]], None]


class MqttTransport:
    def __init__(
        self,
        config: MqttConfig,
        topics: Topics,
        *,
        on_command: MessageHandler | None = None,
        on_connected: Callable[[], None] | None = None,
        subscribe_to: str = "",
        announce_availability: bool = True,
    ) -> None:
        self._config = config
        self._topics = topics
        self._on_command = on_command
        self._on_connected = on_connected
        # The agent listens on the command topic; the `send` CLI overrides this
        # to listen for its own reply instead.
        self._subscribe_to = subscribe_to or topics.command
        # Only the long-running agent owns the availability topic. Short-lived
        # CLI connections must not touch it - a retained `offline` from a
        # one-shot `send` would mark the live agent unavailable in HA.
        self._announce = announce_availability
        self._connected = threading.Event()
        self._closing = False

        # An MQTT broker evicts the existing session when a second client
        # connects with the same client id. A short-lived CLI call sharing the
        # agent's id would knock the agent offline, and the two would then kick
        # each other off in a loop - so ephemeral connections get their own id.
        client_id = config.client_id
        if not announce_availability:
            client_id = f"{client_id}-cli-{secrets.token_hex(4)}"

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv311,
            clean_session=True,
        )
        if config.username:
            self._client.username_pw_set(config.username, config.password)
        if config.tls:
            self._client.tls_set()

        if announce_availability:
            self._client.will_set(topics.status, PAYLOAD_OFFLINE, qos=1, retain=True)
        self._client.reconnect_delay_set(min_delay=1, max_delay=60)
        self._client.on_connect = self._handle_connect
        self._client.on_disconnect = self._handle_disconnect
        self._client.on_message = self._handle_message

    # -- lifecycle ----------------------------------------------------------

    def connect(self, timeout: float = 10.0) -> bool:
        """Start the network loop. Returns True once CONNACK arrives."""
        log.info(
            "connecting to mqtt://%s:%s as %s",
            self._config.host,
            self._config.port,
            self._config.username or "(anonymous)",
        )
        self._client.connect_async(self._config.host, self._config.port, self._config.keepalive)
        self._client.loop_start()
        if not self._connected.wait(timeout):
            log.warning("no CONNACK within %.0fs; continuing to retry in the background", timeout)
            return False
        return True

    def disconnect(self) -> None:
        """Publish a clean `offline` before going away, then stop the loop."""
        self._closing = True
        try:
            if self._announce and self._connected.is_set():
                self._client.publish(
                    self._topics.status, PAYLOAD_OFFLINE, qos=1, retain=True
                ).wait_for_publish(timeout=2)
        except Exception as exc:
            log.debug("could not publish offline status: %s", exc)
        finally:
            self._client.loop_stop()
            self._client.disconnect()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # -- publishing ---------------------------------------------------------

    def publish(
        self, topic: str, payload: Any, *, retain: bool = False, qos: int | None = None
    ) -> bool:
        if not isinstance(payload, (str, bytes)):
            payload = json.dumps(payload, default=str, separators=(",", ":"))
        info = self._client.publish(
            topic, payload, qos=self._config.qos if qos is None else qos, retain=retain
        )
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            log.warning("publish to %s failed: %s", topic, mqtt.error_string(info.rc))
            return False
        return True

    # -- callbacks ----------------------------------------------------------

    def _handle_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code != 0:
            log.error("mqtt connection refused: %s", reason_code)
            if getattr(reason_code, "value", None) in (4, 5) or "not authorized" in str(
                reason_code
            ).lower():
                log.error(
                    "check mqtt.username / mqtt.password - the Pi's Mosquitto has "
                    "allow_anonymous false"
                )
            return

        log.info("mqtt connected")
        self._connected.set()
        if self._announce:
            client.publish(self._topics.status, PAYLOAD_ONLINE, qos=1, retain=True)
        client.subscribe(self._subscribe_to, qos=1)
        log.info("subscribed to %s", self._subscribe_to)
        if self._on_connected:
            try:
                self._on_connected()
            except Exception:
                log.exception("on_connected callback raised")

    def _handle_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        self._connected.clear()
        # paho hands back a ReasonCode object that is truthy even when it means
        # "normal disconnection", so compare the numeric value rather than the
        # object - otherwise every clean shutdown logs as a warning.
        code = getattr(reason_code, "value", reason_code)
        if self._closing or code == 0:
            log.info("mqtt disconnected cleanly")
        else:
            log.warning("mqtt disconnected (%s); paho will retry", reason_code)

    def _handle_message(self, client, userdata, message) -> None:
        if self._on_command is None:
            return
        try:
            text = message.payload.decode("utf-8")
        except UnicodeDecodeError:
            log.warning("dropped non-UTF-8 payload on %s", message.topic)
            return

        try:
            envelope = json.loads(text)
        except json.JSONDecodeError as exc:
            log.warning("dropped malformed JSON on %s: %s", message.topic, exc)
            return

        if not isinstance(envelope, dict):
            log.warning("dropped non-object payload on %s", message.topic)
            return

        try:
            self._on_command(message.topic, envelope)
        except Exception:
            log.exception("command handler raised for %s", message.topic)
