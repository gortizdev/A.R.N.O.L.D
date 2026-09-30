"""Tools the PC's realtime session can call.

Two groups:

* **pc_agent** - this machine's own command registry, the same surface Jarvis
  gained on the Pi, so the tool behaves identically whichever assistant answers.
* **Home Assistant** - implemented against HA's REST API directly. The PC can
  reach :8123 over the LAN, so lights and switches work without involving the
  Pi at all.

* **Jarvis** - `ask_jarvis` and `tell_jarvis`. What genuinely lives in that
  process (what the house has been told, the rooms themselves) is deliberately
  not copied here, because a second copy would drift. So the PC assistant asks
  him, the way a colleague would, over the same Home Assistant relay the alerts
  use - and can have him say something aloud in the other room.

  Timers and alarms used to be on that list and are not any more. A timer for
  the person at this desk should ring at this desk, and routing it through the
  Pi meant it failed outright whenever the Pi was off. `timer.*` is local.

Memory is the exception. Jarvis has its own, but this machine holds
conversations the Pi never hears - the ones started by the wake word claimed
here - and an assistant that forgets those the moment the session times out is
not one. So `memory.*` is local, and the two remember different rooms.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from ..commands import CommandContext, build_registry
from ..jarvis import JarvisError
from ..platform_win import screenshot as screen
from .brain import BrainError, JarvisBrain

log = logging.getLogger(__name__)

PC_COMMANDS = [
    "query.system", "query.cpu", "query.memory", "query.disk", "query.gpu",
    "query.network", "query.battery", "query.uptime", "query.processes",
    "query.process", "query.active_window", "query.alerts", "query.notifications", "query.volume",
    "query.trend",
    "system.info",
    "clock.now", "weather.now", "weather.forecast",
    "schedule.add", "schedule.list", "schedule.cancel",
    "timer.set", "timer.list", "timer.cancel",
    "todo.list", "todo.add", "todo.done", "todo.remove", "todo.sync",
    "todo.projects", "todo.link", "todo.send",
    "control.volume_set", "control.volume_adjust", "control.mute", "control.media",
    "control.launch", "control.lock", "control.cancel_shutdown",
    "control.shutdown", "control.restart", "control.sleep", "control.kill_process",
    "desktop.notify", "desktop.clipboard_get", "desktop.clipboard_set",
    "desktop.screenshot", "desktop.dashboard", "desktop.read_window",
    "web.open", "web.search", "web.sites",
    "artifact.create", "artifact.open", "artifact.list",
    "code.task", "code.status", "code.projects",
    "claude.list", "claude.status", "claude.prompt", "claude.apps", "claude.open",
    "memory.remember", "memory.recall", "memory.forget", "memory.list",
    "printer.status", "printer.make", "printer.sculpt", "printer.adjust", "printer.parts", "printer.open",
    "printer.settings",
]


def build_tools(config) -> list[dict]:
    """Flat function entries, the shape the Realtime API expects."""
    tools: list[dict] = [
        {
            "type": "function",
            "name": "pc_agent",
            "description": (
                "Query or control this Windows desktop. Use for any question about "
                "the PC - disk space, CPU, memory, GPU temperature, network speed, "
                "uptime, running processes, the active window, active alerts - and "
                "for notifications, clipboard, volume and media control. Returns a "
                "'speech' field already phrased for speaking; prefer it verbatim.\n"
                "command must be one of: " + ", ".join(PC_COMMANDS) + ".\n"
                'args examples: query.disk {"drive":"C"}; query.process {"name":"steam"}; '
                'query.processes {"by":"memory","limit":3}; control.volume_set {"level":40}; '
                'control.media {"action":"playpause"}; desktop.notify {"message":"..."}.\n'
                "clock.now answers what time, day or date it is - never guess the "
                "time. weather.now is the weather right now; weather.forecast "
                '{"day":"tomorrow"} or {"days":5} is what is coming. Both take an '
                'optional {"place":"Paris"}; without one they mean here.\n'
                "code.task hands a real job to Claude Code in one of the user's "
                'projects: {"project":"assistant","prompt":"add a dark mode '
                'toggle to the dashboard"}. It runs for minutes in the '
                "background and the user is told aloud when it lands, so say "
                "you have set it going rather than waiting for it. "
                "code.projects lists what it may touch; code.status reports on "
                "the last job.\n"
                "The user also has Claude Code sessions OPEN in the Claude desktop "
                "app and VS Code; claude.* is about those, not new jobs. claude.list "
                "says which sessions there are and what each is doing; claude.status "
                '{"which":"ellipse hub"} says what one is on, what was last asked and '
                "what it last said. claude.prompt puts words INTO a session as if the "
                'user typed them there: {"which":"ellipse hub","text":"run the tests '
                'and fix what fails"} - use it whenever the user says to tell, ask, '
                "have or get a session (or 'Claude', 'that session', 'the ROI one') to "
                "do something. Repeat the user's instruction faithfully in text; do not "
                "add your own. The session works on it in the background and the user is "
                "told when it lands. claude.apps lists the local dev servers those "
                "sessions have running and whether they answer; claude.open "
                '{"which":"roi"} opens one in the browser.\n'
                "To SHOW the user something you have made - a chart, a table, a "
                "checklist, a summary, a little tool - use artifact.create "
                '{"title":"...","html":"<h2>..."} and it appears on screen. Send '
                "a FRAGMENT, not a whole document: no <html>, <head> or <body>, "
                "and no page background, text colour or font of your own. It is "
                "dropped into the assistant's own dark page shell that supplies all of "
                "that, and these classes are already styled for you: .grid of "
                '.card, .stat with a .label and a .value, .bar with one child '
                'sized by width, .badge with .ok/.warn/.crit, .dim, .mono. Colours '
                "are var(--accent), var(--ink), var(--dim), var(--ok), var(--warn), "
                "var(--crit) - use those rather than hex. Tables, headings, code, "
                "inputs and buttons need no classes at all. Add inline <style> or "
                "<script> for anything else, and inline SVG for charts. Everything "
                "must be self-contained, because there is no network to load a "
                'library from. Use {"markdown":"..."} instead when it is only '
                "prose. Prefer this over reading a long list aloud, and say one "
                "short sentence about what you put up.\n"
                "You can act at a time, not only when spoken to. schedule.add "
                'keeps a reminder: {"text":"check the render","when":"in 20 '
                'minutes"}, or {"text":"stand up","when":"every day at 11"}. '
                "Say what you have set and when, in your own words. "
                "schedule.list reads back what is pending and schedule.cancel "
                'drops one: {"which":"the render"}.\n'
                "timer.set is the kitchen timer, and it RINGS on these speakers: "
                '{"minutes":20,"name":"pasta"} or {"for":"an hour and a half"}. '
                'A clock time makes it an alarm: {"when":"at 7","name":"wake up"}. '
                "timer.list says what is left on each and timer.cancel stops one: "
                '{"which":"pasta"}. Timers and reminders for the person at this '
                "desk are yours - never ask the Pi to keep one.\n"
                "todo.list is the to-do list on the dashboard; todo.add puts something on "
                'it ({"text":"call the dentist"}), todo.done ticks one off ({"which":"dentist"}) '
                "and todo.sync looks for this week's emailed document. Read the list back "
                "as a short sentence, not item by item, unless asked for all of it.\n"
                "printer.status is the user's Elegoo 3D printer: what it is printing, "
                "percent done, layer, time left and temperatures. Use it for any "
                "question about the printer or a print.\n"
                "printer.make DESIGNS a part for that printer and opens it in Elegoo "
                'Slicer: {"title":"cable clip","scad":"..."} where scad is OpenSCAD '
                "you write. Units are millimetres; build it on z=0 with the flattest "
                "face down, keep overhangs under 45 degrees where you can, walls at "
                "least 1.2 mm, and about 0.2 mm clearance wherever one thing must "
                "fit over or into another. Put the key dimensions in named variables "
                "at the top and use $fn=64 or so for round things. Ask for a "
                "measurement only when you cannot guess a sensible one. If it comes "
                "back with an OpenSCAD error, fix the code and call it again without "
                "mentioning the error; to change a part the user has seen, edit your "
                "previous code and call it again. It never starts a print - the user "
                "checks it in the slicer and prints from there. Say the size it "
                "reports.\n"
                "printer.sculpt is for ORGANIC shapes OpenSCAD cannot draw - an "
                "animal, a character, a bust, a figurine: "
                '{"title":"dragon","prompt":"a small cartoon dragon sitting",'
                '"height_mm":60}. Describe what it looks like, not how to print '
                'it. {"image":"C:\\\\path\\\\photo.png"} sculpts from a picture '
                "instead. When the request is short ('sculpt me an owl'), ask "
                "two or three quick questions first, one at a time, each with a "
                "couple of suggestions - style (cartoon or realistic), pose, a "
                "key feature or accessory, a base, size - never colour or "
                "material, since it prints in one colour. Then fold every "
                "answer into one detailed visual prompt. Skip the questions "
                "when the request is already specific or the user says just do "
                "it. It takes about a minute and says when it is in the "
                "slicer, so tell the user it is under way and carry on. Use "
                "printer.make for anything with measurements that must fit.\n"
                "printer.adjust makes a NEW VERSION of a part with one change, "
                "keeping the original: "
                '{"name":"turtle","change":"give it a small party hat"} or '
                '{"change":"walls 2 mm thicker"} for the newest part, or '
                '{"height_mm":80} alone to just resize a sculpture. Use it for '
                "any 'make it...', 'change the...', 'can you add...' about a part "
                "already made - including one from an earlier conversation. A "
                "sculpture takes about a minute and says when it is done.\n"
                "printer.parts lists what has been made so far; printer.open "
                '{"name":"owl"} puts an earlier part back in the slicer. '
                'printer.settings {"name":"owl"} is the slicer settings worked out '
                "from that part's shape - layer height, supports, brim and the rest, "
                "each with a reason; say the summary and any warning, not every row. Every "
                "part, and one being made, is also on the dashboard's Workshop "
                "tab (desktop.dashboard), with a 3D view and how it was made.\n"
                "query.notifications says what has come in on Teams and Outlook lately - "
                '{"hours":4} or {"app":"teams"} - which is the answer to "what did I miss". '
                "It reads the toasts Windows showed, so it knows the sender and the first "
                "line, not the whole thread.\n"
                "query.trend says which way a number is heading and when it runs "
                'out - {"path":"disks.C.free_bytes"} - which is the useful answer '
                "to 'how bad is the disk' and one you cannot get from a single "
                "reading. It needs a day or so of history before it will say.\n"
                "desktop.read_window reads the TEXT of the window in front, with "
                "no picture taken and nothing sent as an image - "
                '{"scope":"window"}, or {"scope":"focus"} for just the box the '
                "caret is in. Prefer it for 'what am I looking at', 'read this to "
                "me', 'what does this error say', and anything where the answer is "
                "words. Use view_screen instead when the question is about layout, "
                "a picture, a chart, or where something is on screen. It refuses "
                "password managers and private windows.\n"
                "desktop.dashboard puts this PC's own control panel on screen - "
                "live vitals, firing alerts, the conversation and every command "
                "with a button. Use it when the user asks for the dashboard, the "
                "console, or to SEE how the machine is doing rather than be told; "
                "it starts itself if it is not already running.\n"
                "To put something on screen in a browser, use web.open "
                '{"site":"youtube"} or {"url":"https://..."} and web.search '
                '{"query":"lofi beats","site":"youtube"} - site defaults to '
                'google. Both take an optional {"browser":"firefox"}. Never '
                "spell a URL out loud; just open it.\n"
                "You have a memory that outlives this conversation. "
                'memory.remember {"text":"..."} keeps something durable the user '
                "has told you - a name, a preference, where something lives, how "
                "they like a thing done. Store the fact, not the sentence, and do "
                "not announce it. What you already know is in your instructions "
                'already; memory.recall {"query":"..."} searches the rest, and '
                'memory.forget {"query":"..."} drops one when you are corrected. '
                "Never store passwords, card numbers or anything the user says in "
                "confidence about someone else.\n"
                "desktop.screenshot only SAVES a PNG to disk - it does not let you "
                "see anything. To actually look at the screen, use view_screen."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "args": {"type": "object", "additionalProperties": True},
                },
                "required": ["command"],
            },
        },
    ]

    if config.voice.vision:
        tools.append({
            "type": "function",
            "name": "view_screen",
            "description": (
                "LOOK at what is on the screen right now, as a picture. Use this "
                "when the question is about layout, an image, a chart, a colour, "
                "or where something sits on screen - and for 'help me with what's "
                "on screen' when you need the whole display rather than one "
                "window. When the answer is words ('read this to me', 'what does "
                "this say'), prefer pc_agent desktop.read_window: it returns the "
                "text itself and sends no image. The screenshot is attached as an "
                "image you can read directly, so describe what you genuinely see - "
                "never guess. Call it again for a fresh look; the screen may have "
                "changed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "screen": {
                        "type": "string",
                        "description": (
                            "Omit for the monitor the user is actually working on "
                            "- that is what 'my screen' means. 'all' stitches "
                            "every monitor into one wide image (lower detail per "
                            "screen; use it only when they say something like "
                            "'both screens' or you need to find which one has "
                            "something). Or a monitor number counting from the "
                            "left: '1', '2'."
                        ),
                    },
                },
            },
        })

    if not config.assistant.mirror_jarvis:
        tools += jarvis_tools(config)

    try:
        profile_names = sorted(config.profiles())
    except Exception:  # a bad profiles block must not cost the other tools
        profile_names = []
    if profile_names:
        tools.append({
            "type": "function",
            "name": "switch_profile",
            "description": (
                "Become a different assistant identity for a while - name, voice, "
                "wake word and face. Use when asked to 'be Jarvis', 'switch to "
                "Arnold', 'go back to being yourself', and so on. Profiles: "
                + ", ".join(profile_names)
                + ". The change is saved and takes effect from the NEXT "
                "conversation (a voice cannot change mid-call), so after calling "
                "it say one short line naming who will answer and to which wake "
                "word, then call end_conversation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Which profile: " + ", ".join(profile_names),
                    },
                },
                "required": ["name"],
            },
        })

    tools.append({
        "type": "function",
        "name": "end_conversation",
        "description": (
            "Close the voice session. Call ONLY after the user clearly signals "
            "they are finished ('thank you', 'that will be all', 'goodnight'), "
            "and only after speaking a brief sign-off."
        ),
        "parameters": {"type": "object", "properties": {}},
    })

    if config.jarvis.enabled and config.jarvis.home_assistant.token:
        tools += [
            {
                "type": "function",
                "name": "ha_get_state",
                "description": "Get the current state and attributes of a Home Assistant entity.",
                "parameters": {
                    "type": "object",
                    "properties": {"entity_id": {"type": "string"}},
                    "required": ["entity_id"],
                },
            },
            {
                "type": "function",
                "name": "ha_call_service",
                "description": (
                    "Call a Home Assistant service to control devices. data MUST "
                    "include a target: entity_id, area_id or device_id."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "domain": {"type": "string"},
                        "service": {"type": "string"},
                        "data": {"type": "object", "additionalProperties": True},
                        "entity_id": {"type": "string"},
                        "area_id": {"type": "string"},
                    },
                    "required": ["domain", "service"],
                },
            },
            {
                "type": "function",
                "name": "ha_list_entities",
                "description": (
                    "List Home Assistant entities and their states. Optionally "
                    "filter by domain (light, switch, sensor, media_player...)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"domain": {"type": "string"}},
                },
            },
        ]
    return tools


def jarvis_tools(config) -> list[dict]:
    """The two ways this assistant reaches the other one.

    Only offered when the PC is its own assistant - when it is mirroring
    Jarvis, asking Jarvis would be asking itself.
    """
    tools: list[dict] = []
    if not config.jarvis.enabled:
        return tools
    if config.jarvis.home_assistant.token:
        tools.append({
            "type": "function",
            "name": "ask_jarvis",
            "description": (
                "Ask Jarvis - the separate assistant on the Raspberry Pi that runs the "
                "house - a question, or hand him a job only he can do: anything about "
                "the house that is not a Home Assistant entity, anything he has been "
                "told that you have not, or something that has to be heard in another "
                "room. Returns his reply as text. Relay it in your own words and say "
                "it came from him. Do NOT use this for anything about this PC, and do "
                "not use it for timers, alarms or reminders - those are yours "
                "(timer.set, schedule.add) and they ring here."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "What to ask or tell him, as you would say it aloud.",
                    },
                },
                "required": ["text"],
            },
        })
    if config.jarvis.speech_route != "none":
        tools.append({
            "type": "function",
            "name": "tell_jarvis",
            "description": (
                "Have Jarvis say something out loud through the Pi's speaker, in the "
                "other room. Use when the user asks you to pass a message to Jarvis or "
                "to whoever is near him. It is spoken verbatim; nothing comes back."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Exactly what he should say."},
                },
                "required": ["message"],
            },
        })
    return tools


class ToolDispatcher:
    def __init__(self, config, context: CommandContext) -> None:
        self.config = config
        self.context = context
        self.registry = build_registry()
        ha = config.jarvis.home_assistant
        self._ha_url = ha.url.rstrip("/")
        self._ha_token = ha.token
        self._brain: JarvisBrain | None = None

    def __call__(self, name: str, args: dict[str, Any]) -> Any:
        if name == "pc_agent":
            return self._pc_agent(args)
        if name == "ask_jarvis":
            return self._ask_jarvis(args)
        if name == "tell_jarvis":
            return self._tell_jarvis(args)
        if name == "view_screen":
            return self._view_screen(args)
        if name == "switch_profile":
            return self._switch_profile(args)
        if name == "ha_get_state":
            return self._ha_get_state(args)
        if name == "ha_call_service":
            return self._ha_call_service(args)
        if name == "ha_list_entities":
            return self._ha_list_entities(args)
        return {"error": f"unknown tool {name}"}

    # -- pc ------------------------------------------------------------------

    def _pc_agent(self, args: dict[str, Any]) -> dict[str, Any]:
        command = str(args.get("command") or "").strip()
        if command not in PC_COMMANDS:
            return {"error": f"unknown command '{command}'", "valid": PC_COMMANDS}
        result = self.registry.dispatch(command, args.get("args") or {}, self.context)
        if not result.ok:
            return {"error": result.error or "the command failed"}
        return {"speech": result.speech, "result": result.result}

    def _switch_profile(self, args: dict[str, Any]) -> dict[str, Any]:
        """Persist a new identity. The runner's watcher does the rest once
        this conversation ends - there is no runtime handle to poke here."""
        name = str(args.get("name") or "").strip()
        if not name:
            return {"error": "which profile?"}
        result = self.registry.dispatch("profile.use", {"name": name}, self.context)
        if not result.ok:
            return {"error": result.error or "could not switch"}
        return {
            "ok": True,
            **result.result,
            "note": (
                "Saved. It takes effect after this conversation: say one line "
                "about who answers next and to which wake word, then call "
                "end_conversation."
            ),
        }

    # -- the other assistant -------------------------------------------------

    def _ask_jarvis(self, args: dict[str, Any]) -> dict[str, Any]:
        text = str(args.get("text") or "").strip()
        if not text:
            return {"error": "nothing to ask"}
        if self._brain is None:
            # No local brain in front of it: this is for what Jarvis knows,
            # and PC questions never come this way.
            self._brain = JarvisBrain(self.config, local=None)
        try:
            reply = self._brain.ask(text)
        except BrainError as exc:
            return {"error": str(exc)}
        log.info("asked jarvis: %s -> %s", text, reply[:120])
        return {"from": "Jarvis", "reply": reply or "(Jarvis had nothing to say)"}

    def _tell_jarvis(self, args: dict[str, Any]) -> dict[str, Any]:
        message = str(args.get("message") or "").strip()
        if not message:
            return {"error": "nothing to say"}
        try:
            self.context.jarvis.say(message)
        except JarvisError as exc:
            return {"error": f"could not reach Jarvis: {exc}"}
        log.info("told jarvis: %s", message)
        return {"ok": True, "spoken_by": "Jarvis, on the Pi"}

    # -- vision --------------------------------------------------------------

    def _view_screen(self, args: dict[str, Any]) -> dict[str, Any]:
        """Capture the screen for the model to look at.

        The image cannot travel in the tool result - a function_call_output is
        a plain string - so it goes back under `__screen_b64__` and the
        realtime layer turns it into an input_image message. Same key and same
        contract as Jarvis's pc_view_screen on the Pi.
        """
        if not self.config.voice.vision:
            return {"error": "looking at the screen is disabled on this PC"}
        try:
            shot = screen.capture_for_vision(
                screen=str(args.get("screen") or "active"),
                max_width=self.config.voice.vision_max_width,
                quality=self.config.voice.vision_jpeg_quality,
            )
        except Exception as exc:
            log.warning("screen capture failed: %s", exc)
            return {"error": f"I couldn't capture the screen: {exc}"}

        log.info(
            "captured screen %s (%dx%d, %d KB)",
            shot["screen"], shot["width"], shot["height"], int(shot["bytes"]) // 1024,
        )
        return {
            "status": "captured",
            "note": "the screenshot is attached as an image - describe what you see",
            "screen": shot["screen"],
            "width": shot["width"],
            "height": shot["height"],
            "__screen_b64__": shot["jpeg_base64"],
        }

    # -- home assistant ------------------------------------------------------

    def _ha_request(self, path: str, payload: dict | None = None) -> Any:
        if not self._ha_token:
            return {"error": "no Home Assistant token configured"}
        url = f"{self._ha_url}/api/{path.lstrip('/')}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method="POST" if data is not None else "GET",
            headers={
                "Authorization": f"Bearer {self._ha_token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = response.read().decode("utf-8", "replace")
            return json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as exc:
            return {"error": f"Home Assistant returned {exc.code}"}
        except urllib.error.URLError as exc:
            return {"error": f"could not reach Home Assistant: {exc.reason}"}
        except json.JSONDecodeError:
            return {"error": "Home Assistant returned an unreadable response"}

    def _ha_get_state(self, args: dict[str, Any]) -> Any:
        entity = str(args.get("entity_id") or "").strip()
        if not entity:
            return {"error": "entity_id is required"}
        result = self._ha_request(f"states/{entity}")
        if isinstance(result, dict) and "state" in result:
            return {
                "entity_id": entity,
                "state": result.get("state"),
                "attributes": result.get("attributes", {}),
            }
        return result

    def _ha_call_service(self, args: dict[str, Any]) -> Any:
        domain = str(args.get("domain") or "").strip()
        service = str(args.get("service") or "").strip()
        if not domain or not service:
            return {"error": "domain and service are required"}

        data = dict(args.get("data") or {})
        # Accept a target given at the top level too - the model often puts it
        # there rather than inside data.
        for key in ("entity_id", "area_id", "device_id"):
            if args.get(key) and key not in data:
                data[key] = args[key]
        if not any(k in data for k in ("entity_id", "area_id", "device_id")):
            return {"error": "a target is required: entity_id, area_id or device_id"}

        result = self._ha_request(f"services/{domain}/{service}", data)
        if isinstance(result, dict) and "error" in result:
            return result
        return {"status": "ok", "domain": domain, "service": service}

    def _ha_list_entities(self, args: dict[str, Any]) -> Any:
        domain = str(args.get("domain") or "").strip().lower()
        states = self._ha_request("states")
        if isinstance(states, dict) and "error" in states:
            return states
        if not isinstance(states, list):
            return {"error": "unexpected response from Home Assistant"}

        entities = []
        for item in states:
            entity_id = item.get("entity_id", "")
            if domain and not entity_id.startswith(domain + "."):
                continue
            entities.append(
                {
                    "entity_id": entity_id,
                    "state": item.get("state"),
                    "name": (item.get("attributes") or {}).get("friendly_name", ""),
                }
            )
        # Keep the payload small - a full HA instance is far too much context.
        return {"count": len(entities), "entities": entities[:120]}
