"""Home Assistant MQTT Discovery payloads.

Publishing these retained configs makes the PC show up in Home Assistant as a
single device with a set of sensors, without anyone editing HA's YAML. Every
sensor reads the one telemetry topic through a `value_template`, so a snapshot
publish updates all of them at once.

That HA device is the bridge to Jarvis: an HA automation triggering on, say,
`sensor.<pc>_disk_c_free` can call `rest_command.jarvis_say`, which reaches
Jarvis's loopback-only :8765 because HA runs on the same Pi.
"""

from __future__ import annotations

import logging
from typing import Any

from .. import __version__
from ..config import Config
from .topics import Topics

log = logging.getLogger(__name__)

_BYTES_PER_GB = 1073741824


def _device_block(config: Config) -> dict[str, Any]:
    return {
        "identifiers": [config.device.id],
        "name": config.device.friendly_name,
        "manufacturer": "arnold",
        "model": "Windows PC agent",
        "sw_version": __version__,
    }


def _sensor(
    key: str,
    name: str,
    template: str,
    *,
    unit: str = "",
    device_class: str = "",
    state_class: str = "measurement",
    icon: str = "",
    entity_category: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "_key": key,
        "_component": "sensor",
        "name": name,
        "value_template": template,
    }
    if unit:
        payload["unit_of_measurement"] = unit
    if device_class:
        payload["device_class"] = device_class
    if state_class:
        payload["state_class"] = state_class
    if icon:
        payload["icon"] = icon
    if entity_category:
        payload["entity_category"] = entity_category
    return payload


def build_entities(config: Config, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive the entity list from a live snapshot, so disks and GPUs match reality."""
    entities: list[dict[str, Any]] = [
        _sensor("cpu_percent", "CPU", "{{ value_json.cpu.percent }}", unit="%", icon="mdi:cpu-64-bit"),
        _sensor(
            "memory_percent", "Memory", "{{ value_json.memory.percent }}", unit="%", icon="mdi:memory"
        ),
        _sensor(
            "memory_used",
            "Memory used",
            f"{{{{ (value_json.memory.used_bytes / {_BYTES_PER_GB}) | round(1) }}}}",
            unit="GB",
            device_class="data_size",
        ),
        _sensor(
            "uptime",
            "Uptime",
            "{{ value_json.uptime_seconds }}",
            unit="s",
            device_class="duration",
            icon="mdi:timer-outline",
        ),
        _sensor(
            "process_count",
            "Processes",
            "{{ value_json.processes.count }}",
            icon="mdi:format-list-numbered",
        ),
        _sensor(
            "network_down",
            "Network down",
            "{{ (value_json.network.recv_rate_bps | float(0) / 1024) | round(1) }}",
            unit="kB/s",
            icon="mdi:download-network",
        ),
        _sensor(
            "network_up",
            "Network up",
            "{{ (value_json.network.sent_rate_bps | float(0) / 1024) | round(1) }}",
            unit="kB/s",
            icon="mdi:upload-network",
        ),
        _sensor(
            "alerts_active",
            "Active alerts",
            "{{ value_json.alerts.count | default(0) }}",
            icon="mdi:alert-circle-outline",
        ),
        _sensor(
            "top_process",
            "Top process",
            "{{ value_json.processes.top_cpu[0].name | default('none') }}",
            state_class="",
            icon="mdi:chart-box-outline",
        ),
    ]

    if config.telemetry.publish_active_window:
        entities.append(
            _sensor(
                "active_window",
                "Active window",
                # HA rejects states over 255 chars, so truncate in the template.
                "{{ (value_json.active_window.title | default('none'))[:250] }}",
                state_class="",
                icon="mdi:window-restore",
            )
        )

    for drive in sorted(snapshot.get("disks", {})):
        entities.append(
            _sensor(
                f"disk_{drive.lower()}_percent",
                f"Disk {drive} used",
                f"{{{{ value_json.disks.{drive}.percent }}}}",
                unit="%",
                icon="mdi:harddisk",
            )
        )
        entities.append(
            _sensor(
                f"disk_{drive.lower()}_free",
                f"Disk {drive} free",
                f"{{{{ (value_json.disks.{drive}.free_bytes / {_BYTES_PER_GB}) | round(1) }}}}",
                unit="GB",
                device_class="data_size",
                icon="mdi:harddisk",
            )
        )

    if snapshot.get("battery"):
        entities.append(
            _sensor(
                "battery",
                "Battery",
                "{{ value_json.battery.percent }}",
                unit="%",
                device_class="battery",
            )
        )

    for gpu in snapshot.get("gpus") or []:
        index = gpu.get("index", 0)
        label = f"GPU {index}" if len(snapshot.get("gpus") or []) > 1 else "GPU"
        if gpu.get("utilization_percent") is not None:
            entities.append(
                _sensor(
                    f"gpu{index}_percent",
                    label,
                    f"{{{{ value_json.gpus[{index}].utilization_percent }}}}",
                    unit="%",
                    icon="mdi:expansion-card",
                )
            )
        if gpu.get("temperature_c") is not None:
            entities.append(
                _sensor(
                    f"gpu{index}_temp",
                    f"{label} temperature",
                    f"{{{{ value_json.gpus[{index}].temperature_c }}}}",
                    unit="°C",
                    device_class="temperature",
                )
            )
        if gpu.get("memory_used_mb") is not None:
            entities.append(
                _sensor(
                    f"gpu{index}_memory",
                    f"{label} memory",
                    f"{{{{ (value_json.gpus[{index}].memory_used_mb / 1024) | round(1) }}}}",
                    unit="GB",
                    device_class="data_size",
                )
            )

    # Only announced when Outlook is actually readable: an entity that reports
    # "unavailable" forever is worse in HA than no entity at all.
    if (snapshot.get("mail") or {}).get("available"):
        entities.append(
            _sensor(
                "mail_unread",
                "Unread mail",
                "{{ value_json.mail.unread | default(0) }}",
                icon="mdi:email-outline",
            )
        )
        entities.append(
            _sensor(
                "mail_latest",
                "Newest mail",
                # HA rejects states over 255 characters.
                "{{ (value_json.mail.latest.from | default('nothing'))[:250] }}",
                state_class="",
                icon="mdi:email-arrow-left-outline",
            )
        )

    if (snapshot.get("calendar") or {}).get("available"):
        entities.append(
            _sensor(
                "next_meeting",
                "Next meeting",
                "{{ (value_json.calendar.next.subject | default('nothing'))[:250] }}",
                state_class="",
                icon="mdi:calendar-clock",
            )
        )
        entities.append(
            _sensor(
                "next_meeting_in",
                "Next meeting in",
                "{{ value_json.calendar.minutes_until_next | default('unknown') }}",
                unit="min",
                icon="mdi:calendar-arrow-right",
            )
        )
        entities.append(
            {
                "_key": "in_meeting",
                "_component": "binary_sensor",
                "name": "In a meeting",
                "value_template": (
                    "{{ 'ON' if value_json.calendar.in_progress | default(false) else 'OFF' }}"
                ),
                "payload_on": "ON",
                "payload_off": "OFF",
                "icon": "mdi:account-voice",
            }
        )

    for name in snapshot.get("processes", {}).get("watched", {}):
        safe = "".join(c if c.isalnum() else "_" for c in name).strip("_").lower()
        entities.append(
            {
                "_key": f"watch_{safe}",
                "_component": "binary_sensor",
                "name": f"{name} running",
                "value_template": (
                    f"{{{{ 'ON' if value_json.processes.watched['{name}'].running else 'OFF' }}}}"
                ),
                "payload_on": "ON",
                "payload_off": "OFF",
                "icon": "mdi:application-cog",
            }
        )

    return entities


def publish_discovery(transport, config: Config, topics: Topics, snapshot: dict[str, Any]) -> int:
    """Publish retained discovery configs. Returns the number of entities announced."""
    if not config.mqtt.enable_discovery:
        log.info("HA discovery disabled by config")
        return 0

    device = _device_block(config)
    prefix = config.mqtt.discovery_prefix
    published = 0

    # Availability: HA marks every entity unavailable when the LWT fires.
    availability = [
        {
            "topic": topics.status,
            "payload_available": "online",
            "payload_not_available": "offline",
        }
    ]

    for entity in build_entities(config, snapshot):
        key = entity.pop("_key")
        component = entity.pop("_component")
        payload = {
            **entity,
            "unique_id": f"{config.device.id}_{key}",
            "object_id": f"{config.device.id}_{key}",
            "state_topic": topics.telemetry,
            "availability": availability,
            "device": device,
        }
        topic = f"{prefix}/{component}/{config.device.id}/{key}/config"
        if transport.publish(topic, payload, retain=True, qos=1):
            published += 1

    # Availability itself, as a connectivity entity.
    transport.publish(
        f"{prefix}/binary_sensor/{config.device.id}/status/config",
        {
            "name": "Status",
            "unique_id": f"{config.device.id}_status",
            "object_id": f"{config.device.id}_status",
            "state_topic": topics.status,
            "payload_on": "online",
            "payload_off": "offline",
            "device_class": "connectivity",
            "entity_category": "diagnostic",
            "device": device,
        },
        retain=True,
        qos=1,
    )
    published += 1

    log.info("published %d Home Assistant discovery config(s)", published)
    return published


def clear_discovery(transport, config: Config, topics: Topics, snapshot: dict[str, Any]) -> None:
    """Remove the device from HA by publishing empty retained configs."""
    prefix = config.mqtt.discovery_prefix
    for entity in build_entities(config, snapshot):
        topic = f"{prefix}/{entity['_component']}/{config.device.id}/{entity['_key']}/config"
        transport.publish(topic, "", retain=True, qos=1)
    transport.publish(
        f"{prefix}/binary_sensor/{config.device.id}/status/config", "", retain=True, qos=1
    )
