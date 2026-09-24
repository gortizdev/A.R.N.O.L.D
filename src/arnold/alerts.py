"""Threshold alerting.

Three mechanisms keep a voice assistant from becoming a nuisance:

* **duration** - a rule only fires once its condition has held for
  `for_seconds`, so a momentary CPU spike while a game loads stays quiet.
* **hysteresis** - a fired rule clears at `clear_threshold`, not at
  `threshold`, so a metric hovering on the line does not chatter.
* **cooldown** - after firing, a rule cannot fire again for
  `cooldown_seconds` regardless of what the metric does.
"""

from __future__ import annotations

import logging
import operator
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .humanize import bytes_human, bytes_speech
from .monitors.collector import get_metric

log = logging.getLogger(__name__)

OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}

SEVERITIES = ("info", "warning", "critical")

# Metrics whose raw value is a byte count, so messages can render them readably.
_BYTE_SUFFIXES = ("_bytes", "_bps")


class RuleError(ValueError):
    pass


@dataclass(slots=True)
class Rule:
    name: str
    severity: str = "warning"
    message: str = ""
    speak: bool = True
    notify: bool = False
    for_seconds: float = 0.0
    cooldown_seconds: float = 600.0
    speak_on_clear: bool = False

    # Threshold rules
    metric: str = ""
    op: str = ">"
    threshold: Any = None
    clear_threshold: Any = None

    # Process rules
    process: str = ""
    expect: str = "running"

    @property
    def is_process_rule(self) -> bool:
        return bool(self.process)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Rule":
        if not isinstance(raw, dict):
            raise RuleError(f"each alert rule must be a mapping, got {type(raw).__name__}")
        known = set(cls.__dataclass_fields__)
        unknown = set(raw) - known
        if unknown:
            raise RuleError(
                f"alert rule {raw.get('name', '<unnamed>')!r} has unknown key(s): "
                f"{', '.join(sorted(unknown))}"
            )

        # Checked before construction: `name` is the only field without a
        # default, so a missing one would otherwise surface as a bare TypeError
        # instead of a message pointing at the config.
        if not raw.get("name"):
            raise RuleError("every alert rule needs a 'name'")

        rule = cls(**raw)
        if not rule.metric and not rule.process:
            raise RuleError(f"alert rule {rule.name!r} needs either 'metric' or 'process'")
        if rule.metric and rule.process:
            raise RuleError(f"alert rule {rule.name!r} sets both 'metric' and 'process'; pick one")
        if rule.severity not in SEVERITIES:
            raise RuleError(
                f"alert rule {rule.name!r} has severity {rule.severity!r}; "
                f"expected one of {', '.join(SEVERITIES)}"
            )
        if rule.metric:
            if rule.op not in OPERATORS:
                raise RuleError(
                    f"alert rule {rule.name!r} has op {rule.op!r}; "
                    f"expected one of {', '.join(OPERATORS)}"
                )
            if rule.threshold is None:
                raise RuleError(f"alert rule {rule.name!r} needs a 'threshold'")
        if rule.process and rule.expect not in ("running", "not_running"):
            raise RuleError(
                f"alert rule {rule.name!r} has expect {rule.expect!r}; "
                "expected 'running' or 'not_running'"
            )
        return rule


@dataclass(slots=True)
class _State:
    status: str = "ok"  # ok | pending | firing
    since: float = 0.0
    last_fired: float = 0.0


@dataclass(slots=True)
class AlertEvent:
    rule: str
    severity: str
    state: str  # firing | cleared
    message: str
    speech: str
    value: Any = None
    threshold: Any = None
    speak: bool = True
    notify: bool = False
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "state": self.state,
            "message": self.message,
            "speech": self.speech,
            "value": self.value,
            "threshold": self.threshold,
            "ts": self.ts,
        }


def _looks_like_bytes(metric: str) -> bool:
    return any(metric.endswith(suffix) for suffix in _BYTE_SUFFIXES)


class _Path:
    """Lets a message template reach into the snapshot: `{mail.latest.subject}`.

    `str.format` reads a dot as attribute access, so each level of the snapshot
    is wrapped in one of these on the way in. A metric alone rarely makes a
    sentence worth hearing - "a meeting starts in 5 minutes" needs to say which
    meeting, and that lives somewhere else in the same snapshot.

    A missing key raises AttributeError, which `_render` treats like any other
    unusable template: log it and fall back to the generic wording.
    """

    __slots__ = ("_value",)

    def __init__(self, value: Any) -> None:
        self._value = value

    def __getattr__(self, name: str) -> "_Path":
        if isinstance(self._value, dict) and name in self._value:
            return _Path(self._value[name])
        raise AttributeError(f"the snapshot has no {name!r} here")

    def __format__(self, spec: str) -> str:
        if self._value is None:
            return "unknown"
        # Rounded floats, because a template writer asking for {value} in a
        # sentence never wants 4.700000000000001 read out.
        if isinstance(self._value, float):
            return format(round(self._value), spec or "d") if not spec else format(self._value, spec)
        return format(self._value, spec)

    def __str__(self) -> str:
        return self.__format__("")


class AlertEngine:
    def __init__(self, rules: list[dict[str, Any]], device_name: str = "the PC") -> None:
        self.rules: list[Rule] = [Rule.from_dict(r) for r in rules]
        self._states: dict[str, _State] = {r.name: _State() for r in self.rules}
        self._device = device_name

        # Lets callers tell a live engine from a freshly-built one that has
        # never seen a snapshot (see state.py).
        self.evaluated = False

        names = [r.name for r in self.rules]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise RuleError(f"duplicate alert rule name(s): {', '.join(sorted(duplicates))}")
        log.info("loaded %d alert rule(s)", len(self.rules))

    # -- condition evaluation ----------------------------------------------

    def _condition_met(self, rule: Rule, snapshot: dict[str, Any]) -> tuple[bool, Any]:
        if rule.is_process_rule:
            running = get_metric(snapshot, f"processes.watched.{rule.process}.running")
            if running is None:
                # Not in monitors.watch_processes - can't evaluate, so stay quiet.
                return False, None
            bad = (not running) if rule.expect == "running" else bool(running)
            return bad, bool(running)

        value = get_metric(snapshot, rule.metric)
        if value is None or isinstance(value, (dict, list, str, bool)):
            return False, value
        try:
            return OPERATORS[rule.op](value, rule.threshold), value
        except TypeError:
            return False, value

    def _condition_cleared(self, rule: Rule, snapshot: dict[str, Any]) -> tuple[bool, Any]:
        """Clearing uses clear_threshold when set, giving the rule hysteresis."""
        if rule.is_process_rule:
            met, value = self._condition_met(rule, snapshot)
            return (not met), value

        value = get_metric(snapshot, rule.metric)
        if value is None or isinstance(value, (dict, list, str, bool)):
            return False, value
        boundary = rule.clear_threshold if rule.clear_threshold is not None else rule.threshold
        try:
            return not OPERATORS[rule.op](value, boundary), value
        except TypeError:
            return False, value

    # -- rendering ----------------------------------------------------------

    def _render(
        self, rule: Rule, value: Any, state: str, snapshot: dict[str, Any] | None = None
    ) -> tuple[str, str]:
        if rule.is_process_rule:
            if state == "firing":
                text = (
                    f"{rule.process} is not running on {self._device}."
                    if rule.expect == "running"
                    else f"{rule.process} has started on {self._device}."
                )
            else:
                text = (
                    f"{rule.process} is running again on {self._device}."
                    if rule.expect == "running"
                    else f"{rule.process} has stopped on {self._device}."
                )
            return text, text

        readable = bytes_human(value) if _looks_like_bytes(rule.metric) else value
        spoken = bytes_speech(value) if _looks_like_bytes(rule.metric) else value
        if isinstance(spoken, float):
            spoken = f"{spoken:.0f}"

        if rule.message and state == "firing":
            # Snapshot sections first, so the named fields below always win a
            # collision and an existing rule cannot change meaning.
            fields: dict[str, Any] = {
                key: _Path(section) for key, section in (snapshot or {}).items()
            }
            fields.update(
                {
                    "name": rule.name,
                    "value": value,
                    "value_human": readable,
                    "value_speech": spoken,
                    "threshold": rule.threshold,
                    "metric": rule.metric,
                    "device": self._device,
                }
            )
            try:
                rendered = rule.message.format(**fields)
                return rendered, rendered
            except (KeyError, IndexError, ValueError, AttributeError, TypeError) as exc:
                log.warning("alert rule %s has an unusable message template: %s", rule.name, exc)

        verb = "is back to normal" if state == "cleared" else f"is {rule.op} {rule.threshold}"
        text = f"{rule.metric} on {self._device} {verb} (currently {readable})."
        speech = f"{rule.metric.replace('.', ' ')} on {self._device} {verb}, currently {spoken}."
        return text, speech

    # -- main entry point ---------------------------------------------------

    def evaluate(self, snapshot: dict[str, Any], *, now: float | None = None) -> list[AlertEvent]:
        now = time.time() if now is None else now
        self.evaluated = True
        events: list[AlertEvent] = []

        for rule in self.rules:
            state = self._states[rule.name]

            if state.status == "firing":
                cleared, value = self._condition_cleared(rule, snapshot)
                if cleared:
                    state.status = "ok"
                    state.since = now
                    message, speech = self._render(rule, value, "cleared", snapshot)
                    events.append(
                        AlertEvent(
                            rule=rule.name,
                            severity="info",
                            state="cleared",
                            message=message,
                            speech=speech,
                            value=value,
                            threshold=rule.threshold,
                            speak=rule.speak_on_clear,
                            notify=False,
                            ts=now,
                        )
                    )
                continue

            met, value = self._condition_met(rule, snapshot)
            if not met:
                if state.status == "pending":
                    state.status = "ok"
                    state.since = now
                continue

            if state.status == "ok":
                state.status = "pending"
                state.since = now

            held_for = now - state.since
            if held_for < rule.for_seconds:
                continue

            if state.last_fired and (now - state.last_fired) < rule.cooldown_seconds:
                # Still firing as far as state goes, just not re-announced.
                state.status = "firing"
                continue

            state.status = "firing"
            state.last_fired = now
            message, speech = self._render(rule, value, "firing", snapshot)
            events.append(
                AlertEvent(
                    rule=rule.name,
                    severity=rule.severity,
                    state="firing",
                    message=message,
                    speech=speech,
                    value=value,
                    threshold=rule.threshold,
                    speak=rule.speak,
                    notify=rule.notify,
                    ts=now,
                )
            )

        return events

    def active(self) -> list[str]:
        return [name for name, state in self._states.items() if state.status == "firing"]
