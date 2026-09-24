"""Turning a transcript into a spoken reply.

Two backends:

* ``JarvisBrain`` posts the transcript to the Pi and speaks whatever comes
  back. Jarvis keeps its reasoning, memory and tools, so the PC is purely ears
  and mouth. It needs a `rest_command.jarvis_ask` in Home Assistant plus a
  route in assistant.py that returns the reply text.
* ``LocalBrain`` matches PC questions against the command registry and answers
  from here. Useful on its own, and it is what ``JarvisBrain`` falls back to
  when the Pi cannot be reached - "what's my CPU at" should still work when
  the network is down.

JarvisBrain tries the local registry *first* for obviously-PC questions, since
a round trip to the Pi to ask this machine about its own disk is wasteful.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any

from ..commands import CommandContext, build_registry

log = logging.getLogger(__name__)


# Phrases that are unambiguously about this machine. Ordered: the first match
# wins, so more specific patterns come first.
LOCAL_INTENTS: list[tuple[str, str, dict[str, Any]]] = [
    (r"\b(lock|log ?off).*(pc|computer|desktop|machine)\b", "control.lock", {}),
    (r"\block (the )?(screen|workstation)\b", "control.lock", {}),
    (r"\b(what|which).*(window|app|program).*(open|active|foreground|front)\b",
     "query.active_window", {}),
    (r"\bwhat('s| is) on (my |the )?screen\b", "query.active_window", {}),
    (r"\b(disk|drive|storage|space).*(free|left|space|full)\b", "query.disk", {}),
    (r"\bhow much (disk|space|storage)\b", "query.disk", {}),
    (r"\b(gpu|graphics card|video card)\b", "query.gpu", {}),
    (r"\b(cpu|processor).*(usage|load|at|doing|percent)\b", "query.cpu", {}),
    (r"\b(ram|memory)\b", "query.memory", {}),
    (r"\bnetwork (speed|usage|rate)\b", "query.network", {}),
    (r"\b(download|upload).*(speed|rate)\b", "query.network", {}),
    (r"\bhow long.*(up|running|been on)\b", "query.uptime", {}),
    (r"\buptime\b", "query.uptime", {}),
    (r"\b(any )?alerts?\b", "query.alerts", {}),
    (r"\bwhat('s| is).*(using|eating).*(cpu|memory|ram)\b", "query.processes", {}),
    (r"\b(top|biggest) process", "query.processes", {}),
    (r"\b(pc|computer|desktop|machine) (status|health|doing|up to)\b", "query.system", {}),
    (r"\bhow('s| is) (my |the )?(pc|computer|desktop|machine)\b", "query.system", {}),
    (r"\b(mute|silence) (the )?(pc|computer|desktop|volume)\b", "control.mute", {"muted": True}),
    (r"\bunmute\b", "control.mute", {"muted": False}),
    (r"\b(pause|play|resume) (the )?(music|audio|video|track)\b",
     "control.media", {"action": "playpause"}),
    (r"\b(next|skip) (the )?(track|song)\b", "control.media", {"action": "next"}),
    (r"\b(previous|last) (track|song)\b", "control.media", {"action": "previous"}),
    (r"\bjoin (the |my |that )?(meeting|call|standup)\b", "web.join_meeting", {}),
    (r"\bnext (meeting|call)\b", "query.next_meeting", {}),
    (r"\bam i in a meeting\b", "query.next_meeting", {}),
    (r"\bwhat('s| is) (coming up|next)\b", "query.next_meeting", {}),
    # Deliberately narrow: "put it on my calendar" is not a question this PC can
    # answer, and a bare \bcalendar\b would swallow it before Jarvis sees it.
    (r"\bwhat('s| is)?\s+(on )?(my|the|today's) (calendar|agenda|schedule)\b", "query.agenda", {}),
    (r"\b(read|run through) (me )?(my|the) (agenda|schedule)\b", "query.agenda", {}),
    (r"\bwhat have i got (on|today)\b", "query.agenda", {}),
    (r"\bteams\b.{0,20}\b(messages?|notifications?|chats?|pings?)\b", "query.notifications", {"app": "teams"}),
    (r"\b(messages?|notifications?|pings?)\b.{0,20}\bteams\b", "query.notifications", {"app": "teams"}),
    (r"\b(what|anything|have i) .{0,12}\bmiss(ed)?\b", "query.notifications", {}),
    (r"\b(any|new) (work )?notifications\b", "query.notifications", {}),
    (r"\b(any|new|unread) (new )?(mail|email|emails|messages)\b", "query.mail", {}),
    (r"\b(check|read) (my )?(mail|email|inbox)\b", "query.mail", {}),
    (r"\b(anything|something) (come|came) in\b", "query.mail", {}),
    (r"\b(what|anything).{0,15}\b(scheduled|reminders?)\b", "schedule.list", {}),
    (r"\bwhat have i got (coming|set)\b", "schedule.list", {}),
    (r"\b(how fast|how quickly|what rate).{0,25}\b(fill|filling|going|growing)\b",
     "query.trend", {}),
    (r"\b(how long|when).{0,20}\b(until|till|before)\b.{0,20}\b(disk|drive|space|storage)\b",
     "query.trend", {}),
    (r"\b(disk|drive|storage) (trend|projection)\b", "query.trend", {}),
    # "open the dashboard" is about this PC's own console, so it never needs to
    # travel to the Pi. Kept narrow: a bare \bdashboard\b would swallow "put
    # the car dashboard camera on".
    (r"\b(open|show|bring up|pull up|put up).{0,12}\b(dashboard|control panel|console)\b",
     "desktop.dashboard", {}),
    (r"\b(dashboard|control panel)\b.{0,15}\b(open|up|on screen)\b", "desktop.dashboard", {}),
    (r"\btake a screenshot\b", "desktop.screenshot", {}),
    # The Claude Code sessions open on this machine. Kept to plain questions:
    # "tell the ellipse hub session to run the tests" carries an argument and
    # is the realtime model's job to route.
    (r"\b(what|which) .{0,20}\b(claude|sessions?)\b.{0,20}\b(doing|up to|working on|running)\b",
     "claude.list", {}),
    (r"\bhow('s| is| are) (my |the )?(claude|coding|code) (sessions?|going)\b", "claude.list", {}),
    (r"\b(list|show) (my |the )?(claude|coding|code) sessions\b", "claude.list", {}),
    (r"\b(dev|local) servers?\b.{0,20}\b(up|running|listening)\b", "claude.apps", {}),
    (r"\bwhat('s| is) (running|listening) on localhost\b", "claude.apps", {}),
    (r"\bwhat('s| is) (on |in )?(my |the )?clipboard\b", "desktop.clipboard_get", {}),
]

_VOLUME_RE = re.compile(r"\b(?:set )?volume (?:to )?(\d{1,3})\b")

# Memory is phrased, not enumerated, so it needs its argument pulled out of the
# sentence rather than a fixed intent. "remember to..." is left alone: that is a
# reminder, which is Jarvis's timer, not a fact about the user.
_REMEMBER_RE = re.compile(
    r"\b(?:remember|make a note|keep in mind|don't forget)\b(?: that| this)?[:,]?\s+(?!to\b)(.+)"
)
_RECALL_RE = re.compile(
    r"\bwhat do you (?:remember|know)\b(?: about)?\s+(.+)|\bdo you remember\b(?: about)?\s+(.+)"
)
_FORGET_RE = re.compile(r"\b(?:forget|stop remembering)\b(?: about| that)?\s+(.+)")

# A reminder is two things in one sentence - what, and when - so it needs
# splitting rather than matching. The time half is left whole for the scheduler
# to parse, which is the only thing here that knows what "half four" means.
#
# The tail has to look like an actual time. "in twenty minutes" is one; "in
# March" is not, and matching it would turn "remember that her birthday is in
# March" - a fact - into a reminder set for some Tuesday.
_UNITS = r"seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?"
_CLOCK = r"\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?"
_WHEN = (
    rf"(?:in\s+[\w-]+\s+(?:an?\s+)?(?:{_UNITS})"
    rf"|at\s+{_CLOCK}"
    rf"|every\s+\w+(?:\s+at\s+{_CLOCK})?"
    rf"|tomorrow(?:\s+at\s+{_CLOCK})?)"
)
# "remember" only counts with "to" after it: bare "remember that ..." is a fact
# for the memory store, and it gets first refusal on the sentence.
_REMIND_RE = re.compile(
    rf"\b(?:remind me(?:\s+to)?|remember\s+to)\s+(?P<what>.+?)\s+(?P<when>{_WHEN}.*)$"
)
# The other order: "in ten minutes, remind me to X".
_REMIND_FIRST_RE = re.compile(
    rf"^(?P<when>{_WHEN})[,\s]+(?:remind me|tell me)\s+(?:to\s+)?(?P<what>.+)$"
)


class BrainError(RuntimeError):
    pass


class LocalBrain:
    """Answers from this PC's own command registry."""

    def __init__(self, context: CommandContext) -> None:
        self.context = context
        self.registry = build_registry()

    def match(self, text: str) -> tuple[str, dict[str, Any]] | None:
        lowered = text.lower().strip()

        volume = _VOLUME_RE.search(lowered)
        if volume and any(w in lowered for w in ("pc", "computer", "desktop", "speaker")):
            return "control.volume_set", {"level": int(volume.group(1))}

        # Before memory: "remind me to call mum at six" is a reminder, and
        # `remember` reads it as a fact to keep unless this looks first.
        if self.context.config.schedule.enabled:
            for pattern in (_REMIND_RE, _REMIND_FIRST_RE):
                found = pattern.search(lowered)
                if found:
                    return "schedule.add", {
                        "text": found.group("what").strip(" ?.,"),
                        "when": found.group("when").strip(" ?.,"),
                    }

        if self.context.config.memory.enabled:
            recall = _RECALL_RE.search(lowered)
            if recall:
                return "memory.recall", {"query": (recall.group(1) or recall.group(2)).strip(" ?.")}
            forget = _FORGET_RE.search(lowered)
            if forget:
                return "memory.forget", {"query": forget.group(1).strip(" ?.")}
            remember = _REMEMBER_RE.search(lowered)
            if remember:
                # Stored from the original text, not the lowercased copy - a
                # remembered name should keep its capital letter.
                offset = remember.start(1)
                return "memory.remember", {"text": text.strip()[offset:].strip(" ?.")}

        for pattern, command, args in LOCAL_INTENTS:
            if re.search(pattern, lowered):
                # "how much disk on D" - pull the drive letter out if present.
                if command == "query.disk":
                    drive = re.search(r"\bdrive ([a-z])\b", lowered)
                    if drive:
                        args = {**args, "drive": drive.group(1).upper()}
                return command, args
        return None

    def ask(self, text: str) -> str:
        matched = self.match(text)
        if matched is None:
            return ""
        command, args = matched
        log.info("local intent: %s -> %s %s", text, command, args or "")
        result = self.registry.dispatch(command, args, self.context)
        return result.speech or ("Done." if result.ok else "That didn't work.")


class JarvisBrain:
    """Sends the transcript to Jarvis and returns its reply."""

    def __init__(self, config, local: LocalBrain | None = None) -> None:
        self.config = config
        self.local = local
        ha = config.jarvis.home_assistant
        self._url = f"{ha.url.rstrip('/')}/api/services/{config.voice.jarvis_ask_service.strip('/')}"
        self._token = ha.token
        self._timeout = config.voice.jarvis_timeout_seconds
        self._unreachable_logged = False

    def ask(self, text: str) -> str:
        # Answer PC questions here rather than round-tripping to the Pi just to
        # have it ask this machine about itself.
        if self.local is not None:
            local_reply = self.local.ask(text)
            if local_reply:
                return local_reply

        if not self._token:
            raise BrainError("no Home Assistant token configured")

        body = json.dumps({"text": text, "return_response": True}).encode("utf-8")
        request = urllib.request.Request(
            self._url + "?return_response",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = response.read().decode("utf-8", "replace")
            self._unreachable_logged = False
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            if exc.code == 400:
                raise BrainError(
                    "Home Assistant has no rest_command for this yet - see "
                    "pi/homeassistant/configuration.snippet.yaml"
                ) from exc
            raise BrainError(f"Jarvis returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            if not self._unreachable_logged:
                log.warning("Jarvis unreachable: %s", exc.reason)
                self._unreachable_logged = True
            raise BrainError("I couldn't reach Jarvis on the Pi.") from exc

        return self._extract_reply(payload)

    @staticmethod
    def _extract_reply(payload: str) -> str:
        """Pull the spoken text out of HA's response.

        HA wraps rest_command results as {"service_response": {...}}, and what
        is inside depends on how the Pi route answers, so several shapes are
        accepted rather than demanding one.
        """
        if not payload.strip():
            return ""
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return payload.strip()[:500]

        if isinstance(data, str):
            return data.strip()
        if not isinstance(data, dict):
            return ""

        node: Any = data
        for key in ("service_response", "response", "content"):
            if isinstance(node, dict) and key in node:
                node = node[key]
        if isinstance(node, str):
            return node.strip()
        if isinstance(node, dict):
            for key in ("reply", "speech", "text", "message", "answer", "result"):
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""


def build_brain(config, context: CommandContext):
    local = LocalBrain(context)
    if config.voice.brain == "local":
        return local
    return JarvisBrain(config, local=local)
