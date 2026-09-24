"""Topic namespace.

Everything hangs off `<base_topic>/<device_id>` so several machines can report
to the same broker without colliding.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Topics:
    base_topic: str
    device_id: str

    @property
    def root(self) -> str:
        return f"{self.base_topic}/{self.device_id}"

    @property
    def status(self) -> str:
        """Retained availability marker; also the MQTT last-will topic."""
        return f"{self.root}/status"

    @property
    def telemetry(self) -> str:
        """Full metric snapshot; every HA sensor reads from here."""
        return f"{self.root}/telemetry"

    @property
    def alert(self) -> str:
        return f"{self.root}/alert"

    @property
    def window(self) -> str:
        return f"{self.root}/active_window"

    @property
    def notice(self) -> str:
        """Something the agent decided to say, or a scheduled job firing.

        Separate from `alert`: an alert is a threshold crossing that Home
        Assistant may want to automate on, while this is the assistant having
        chosen to speak. Conflating them would make a remark about the disk
        indistinguishable from a rule firing.
        """
        return f"{self.root}/notice"

    @property
    def capabilities(self) -> str:
        return f"{self.root}/capabilities"

    @property
    def command(self) -> str:
        """Inbound: Jarvis or a Home Assistant automation publishes here."""
        return f"{self.root}/cmd"

    @property
    def result(self) -> str:
        return f"{self.root}/cmd/result"
