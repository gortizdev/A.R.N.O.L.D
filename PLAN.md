# PLAN: a voice of its own, timers, more observers, and reading the screen

Four features, in dependency order. **A must land before B** (a timer that rings
needs a mouth). **C and D are independent** of A/B and of each other, except that
one observer (`pi_unreachable`) reads a field A adds - it is written last and can
be dropped if A slips.

## Goal

1. **A - local speech for the agent.** Everything the agent says goes through
   `JarvisClient.say()` to the Pi. The Pi is frequently off, so alerts, notices,
   work notifications and reminders are logged and then lost
   (`service.py:119-128` says so out loud). Give the agent process a mouth of its
   own: a route policy (`jarvis | local | both | auto | none`), a single worker
   thread so speaking never blocks the tick, and total tolerance of a missing
   sounddevice / API key / audio device / Pi.
2. **B - timers and alarms on this PC.** The persona and the tool descriptions
   currently *order* the model to send timers to the Pi
   (`session_config.py:52-53`, `tools.py:12,283-287`), so with the Pi off
   "set a timer for twenty minutes" fails outright. Make timers a first-class
   `Job` kind that **rings** locally, reports a countdown, and is reachable by
   voice, by CLI and over SSH.
3. **C - six more observers** for the Noticed feed, all cheap enough for the
   proactive pass.
4. **D - read the focused window's text** through UI Automation, so
   "what am I looking at" / "read this to me" works with no picture leaving the
   machine and with `voice.vision: false`.

---

## Facts checked against the code (where the request's assumptions need adjusting)

- **`mouth.py` is taken.** `src/arnold/mouth.py` is the lip-sync DSP
  (`MouthAnalyser`, `MouthTuning`, `envelope_from_wav`), used by the face and the
  `mouth-test` CLI. `voice/` is the voice *session* package. So the new module is
  **`src/arnold/speech.py`** with class **`Voice`**, and the agent
  attribute is `self.speech`. `ctx.jarvis` keeps meaning "the Pi", because
  `tell_jarvis` (`tools.py:396-405`) must stay Pi-only.
- **Importing anything from `voice/` drags in the whole voice stack.**
  `voice/__init__.py:13` does `from .pipeline import VoiceAssistant`, and
  `pipeline.py:22-41` imports numpy, `.audio`, `.stt`, `.tts`, `.wake_ack`.
  numpy is **not** a core dependency (pyproject core = paho-mqtt, psutil, PyYAML,
  pyperclip, pillow, markdown). So `speech.py` must import `voice.openai_tts` /
  `voice.audio` **lazily, on the worker thread, inside `try/except ImportError`** -
  never at module import. This is the single most important constraint in A.
- **The MQTT broker is on the Pi** (`config.yaml:33` `host: 192.168.1.171`). So
  MQTT is *also* down exactly when local speech matters. That kills two ideas:
  MQTT cannot be the "is a conversation live" signal, and it cannot be relied on
  to drive the face. Both need a local file instead.
- **The face already fakes a mouth when it has no levels.**
  `face/state.py:363-377`: in `FaceMode.SPEAKING` with `_has_levels` false it
  synthesises a plausible envelope. `face/sources.py:148-156` maps a bare
  `state` payload of `"speaking"` to that mode. **So publishing `state: speaking`
  alone gives a fully animated talking face - no eq stream, no MouthAnalyser in
  the agent.** That settles the face question for v1 (see Risks 3).
- **Observers can already keep memory.** `notices.py:473` sets `world.book =
  self.book` *before* `observe(world)` at 476, so an observer can read the
  `NoticeBook`. It has no general key/value store yet - C adds a small one.
- **History cannot answer "did Steam exit".** `history.flatten` (`history.py:76`)
  drops booleans explicitly, and `BASE_METRICS` (42-51) has no
  `processes.watched.*`. Edge detection needs the notice book's memo, not a trend.
- **`comtypes` is not installed** (checked `.venv/Lib/site-packages`); `pywin32
  312` **is** (from the `outlook` extra). Python in the venv is **3.14.2**.
- **`ProactiveConfig` is where observer thresholds live** (`config.py:627-657`),
  and the example config documents them at `config.example.yaml:265-270`.
- **`ui/server.py:341-343` has a now-obsolete docstring**: "This assistant has no
  speaker of its own outside a conversation, so the room's voice is his." A makes
  that false; update it.
- `tests/test_process.py:56-88` AST-scans every module under `src/` for direct
  `subprocess.*` calls. Nothing in this plan spawns a process at all: D uses
  ctypes/comtypes, C uses `winreg` and `ctypes`.
- **Nothing here is identity**, so `IdentityWatcher` (`config.py:1150-1203`) is
  *not* extended: `speech.*`, the timer config, the new `proactive.*` thresholds
  and the screen-text switches all take effect on the next restart of the
  `Arnold` scheduled task. Say so in the README.

---

# A. A voice of its own

## Files to change (A)

| file | lines | edit |
|---|---|---|
| `src/arnold/speech.py` | new | `Voice`, `SpeechError`, `make_ring`, `conversation_is_live` |
| `src/arnold/config.py` | after 246 (`JarvisConfig`) | new `SpeechConfig` dataclass + `ROUTES` |
| | 715-726 (`Config` fields) | `speech: SpeechConfig = field(default_factory=SpeechConfig)` |
| | near 829 | `Config.speech_route()` resolver (back-compat with `jarvis.speech_route`) |
| | 858-967 (`validate`) | reject an unknown `speech.route` / `speech.backend` |
| | 1009-1033 (`load_config`) | `speech=_build(SpeechConfig, raw.get("speech"), "speech")` |
| `src/arnold/service.py` | 48 | keep `self.jarvis`; add `self.speech = Voice(config, jarvis=self.jarvis, publish=self._publish_face)` |
| | 102-109 (`context`) | pass `speech=self.speech` into `CommandContext` |
| | 119-128 | probe both routes; log what will actually be audible |
| | 185-201 (`_shutdown`) | `self.speech.close()` before the transport goes |
| | 284, 319-322 | `_announce_work`: `self.speech.enabled`, `self.speech.say(speech)` |
| | 341-345 | `_notice`: `self.speech.say(notice.text)` |
| | 357-370, 378-389 | `_run_due_jobs` / `_run_job_command`: `self.speech.say(...)` |
| | 409-413 | `_handle_alert`: `self.speech.say(event.speech)` |
| | 452-456 | `_on_command` speak-back: `self.speech.say(result.speech)` |
| | new method | `_publish_face(leaf, payload)` -> `self.transport.publish(..., retain=True, qos=1)` |
| `src/arnold/commands/registry.py` | 30-38 | `CommandContext` gains `speech: Any = None` |
| `src/arnold/commands/code.py` | 279 | `(ctx.speech or ctx.jarvis).say(...)` |
| `src/arnold/cli.py` | 30-45 (`_build_context`) | build a `Voice` and put it on the context |
| | 189-193 | `exec --speak` goes through `Voice`, then `close()` |
| | 198-207 (`cmd_say`) | `--route`/`--local`; speak through `Voice`; `close()` before returning |
| | 240-250 (`cmd_diag`) | print the resolved route and whether a local backend exists |
| | 873-875 | `say` subparser: `--route {jarvis,local,both,auto}`, `--local` |
| `src/arnold/ui/server.py` | 94, 301, 341-350 | `self.speech = Voice(...)`; `say()` uses it; fix the docstring |
| `src/arnold/voice/realtime_runner.py` | ~424, ~459-470 | write / clear the conversation marker |
| `config.example.yaml` | after the `jarvis:` block (ends 93) | new `speech:` block |
| `config.yaml` | same place | same block, `route: auto` |
| `README.md` | new `## Speaking out loud` after 77-112; `## Layout` 735-760; `## Commands` 650-678 | |
| `tests/test_speech.py` | new | see below |

## Exact config shape (A)

```yaml
# How this PC says things out loud.
#
# Everything the agent volunteers - an alert, a notice, a reminder, a timer -
# used to go to Jarvis on the Pi and nowhere else, so with the Pi off it was
# written to the log and lost. This is the PC's own mouth.
speech:
  # jarvis - the Pi only, exactly as before.
  # local  - this PC's speakers only.
  # both   - say it in both rooms.
  # auto   - try the Pi; speak here only if he could not be reached. Nothing
  #          changes while the Pi is up, and nothing is silently lost when it
  #          is not.
  # none   - never speak.
  # Blank follows jarvis.speech_route: 'none' there still means silence,
  # anything else means auto.
  route: auto

  # auto   - whatever voice.tts_backend says (openai, falling back to Piper).
  # openai - gpt-4o-mini-tts in the assistant's own voice. Needs the network.
  # piper  - fully local and free, but plainer. Works with nothing at all.
  backend: auto
  # Blank uses voice.output_device, then the system default. Matched as a
  # case-insensitive substring, like every other device name here.
  output_device: ""
  volume: 0.8

  # Speaking is slow - a TTS round trip plus playback - so it happens on one
  # worker thread and the agent's tick never waits for it.
  max_queued: 8              # beyond this the oldest waiting line is dropped
  stale_after_seconds: 90    # nobody wants a nine-minute-old alert read out
  shutdown_wait_seconds: 5   # how long a stop waits for the line being spoken

  # After the Pi refuses or times out, `auto` stops trying it for this long and
  # goes straight to the local speakers. Otherwise every alert would pay a
  # ten-second Home Assistant timeout before saying anything.
  jarvis_retry_seconds: 60
  jarvis_retry_max_seconds: 600

  # Do not talk over a live conversation. The voice session leaves a marker
  # file beside the state file; anything queued waits for it, up to this long.
  defer_to_conversation: true
  defer_seconds: 60

  # Tell the face when the agent is talking, so it moves its mouth for an
  # alert as well as for a conversation. One retained MQTT message per
  # utterance, and a no-op when the broker is unreachable.
  publish_face_state: true

  # The timer ring (see `timer.set`), generated locally so it needs nothing.
  ring_volume: 0.6
```

```python
@dataclass(slots=True)
class SpeechConfig:
    route: str = ""            # "" = derive from jarvis.speech_route
    backend: str = "auto"      # auto | openai | piper
    output_device: str = ""
    volume: float = 0.8
    max_queued: int = 8
    stale_after_seconds: float = 90.0
    shutdown_wait_seconds: float = 5.0
    jarvis_retry_seconds: float = 60.0
    jarvis_retry_max_seconds: float = 600.0
    defer_to_conversation: bool = True
    defer_seconds: float = 60.0
    publish_face_state: bool = True
    ring_volume: float = 0.6

ROUTES = ("jarvis", "local", "both", "auto", "none")
```

```python
# Config, next to assistant_name() (config.py:829)
def speech_route(self) -> str:
    """Where spoken output goes. Blank in config means: keep doing what the
    old jarvis.speech_route said, so an upgrade changes nothing by itself."""
    chosen = (self.speech.route or "").strip().lower()
    if chosen:
        return chosen
    return "none" if self.jarvis.speech_route == "none" else "auto"
```

## Approach (A) - in order, each step one edit

1. **`speech.py` module docstring**: why this exists (the agent had no mouth, the
   mouth was the Pi, the Pi is often off) and why it is a thread (speaking is
   seconds of network and playback; the tick is on a 10 s cadence,
   `service.py:159-166`).
2. `make_ring(seconds=1.6, rate=24000) -> np.ndarray | None` - a three-note
   figure with the same raised-cosine envelope as `wake_ack.make_chime`
   (`wake_ack.py:54-65`), louder and repeated, so a timer is unmistakably not an
   acknowledgement. numpy guarded exactly as `mouth.py:22-25` does (`np = None`
   on ImportError; returns `None` and the ring is skipped). It lives here, not in
   `wake_ack`, so the agent never imports `voice/` just to make a noise.
3. `class Voice`:

```python
def __init__(self, config, jarvis=None, publish=None, route: str = "") -> None
@property
def enabled(self) -> bool                 # resolved route != "none"
@property
def pi_down_since(self) -> float          # 0.0 when the Pi answers (used by C)
def say(self, text: str, *, ring: bool = False) -> bool
def check(self) -> dict                   # for diag; synthesises nothing
def close(self, timeout: float | None = None) -> None
```

   - `say()` **never blocks and never raises.** It strips the text, caps it
     (`jarvis.MAX_MESSAGE_CHARS = 500` for the Pi leg, 2000 locally), stamps
     `time.monotonic()` and enqueues `_Utterance(text, ring, queued_at)`. Returns
     False when the route is `none` or the queue refused it.
   - `queue.Queue(maxsize=config.speech.max_queued)`. On `queue.Full`, **drop the
     oldest** (`get_nowait()`, then retry) and log
     "speech is backed up; dropped: %s" - for a monitoring agent the newest line
     is the true one.
   - The worker (`threading.Thread(daemon=True, name="speech")`) starts lazily on
     the first `say()`, so a `Voice` built in a one-shot `exec` that never speaks
     costs nothing at all.
4. **Worker loop**, per utterance:
   1. drop it if `monotonic() - queued_at > stale_after_seconds` (DEBUG line);
   2. if `defer_to_conversation`, poll the conversation marker every 2 s for up
      to `defer_seconds`, then speak anyway and log that it waited;
   3. `ring` - play `make_ring()` locally first, whatever the route;
   4. route in ("jarvis", "both", "auto") and the Pi is not in backoff -
      `self._jarvis.say(text)` inside `try/except JarvisError`. On failure record
      `self._pi_down_since` (once) and push `self._retry_at` out, doubling from
      `jarvis_retry_seconds` to `jarvis_retry_max_seconds`; on success clear both
      and log one recovery line;
   5. speak locally when the route is local/both, or auto and the Pi leg did not
      succeed;
   6. a sentinel `None` ends the loop.
5. `_speak_locally(text)`:
   - `self._speaker or self._build_speaker()`; if that is None, log **once** at
     INFO ("no local speech backend (%s); %r was only written to the log") and
     return False - the `self._degraded` flag pattern from `openai_tts.py:98-104`,
     so a machine with no speakers does not fill the log;
   - `audio, rate = speaker.synthesize(text)` - both `Speaker` (`tts.py:50-57`)
     and `OpenAISpeaker` (`openai_tts.py:76-86`) expose it, which is why we do
     not call their `say()`: we need to scale the volume ourselves, exactly as
     `wake_ack.play` does (`wake_ack.py:269-270`);
   - `sd.play(samples, rate, device=self._device)`, `sd.wait()`, `sd.stop()`;
   - `publish("state", "speaking")` before and `publish("state", "idle")` in a
     `finally`; the whole body wrapped in `try/except Exception` - an audio
     failure is a log line and nothing more.
6. `_build_speaker()` - **every voice import lazy, in one try/except**:

```python
from .voice.audio import AudioError, resolve_device      # pulls the voice package
from .voice.openai_tts import OpenAISpeaker, build_speaker
from .voice.session_config import delivery_for
```

   `backend == "auto"` calls `build_speaker(config, device)`, which already
   prefers OpenAI with Piper as the net (`openai_tts.py:130-156`); "openai" and
   "piper" construct directly, with `delivery_for(config)` as the instruction so
   the agent sounds like the assistant rather than like a different product.
   `ImportError`, `AudioError`, `RuntimeError`, `OSError` all give `None` plus one
   log line. Device: `speech.output_device or config.voice.output_device`.
7. `check()` returns `{"route", "jarvis": self._jarvis.check(), "local": {"ok",
   "backend", "detail"}}`, building the speaker only if one has not been built.
   `cmd_diag` prints it under the existing Jarvis probe.
8. **`service.py` wiring.** `_publish_face(leaf, payload)` publishes to
   `f"{self.config.assistant_topic_prefix()}/{leaf}"` when `self.transport` is up
   and `config.speech.publish_face_state`. Replace the seven `self.jarvis.say(...)`
   sites and the four `self.jarvis.enabled` guards listed above; the
   `except JarvisError` blocks around them go, because `Voice.say` never raises.
   The startup log at 119-128 becomes one of:
   - "speech route 'auto': the Pi answered, so this goes to him"
   - "speech route 'auto': the Pi is not answering, so it will be spoken here"
   - "speech route 'none': nothing will be spoken" - the only case where today's
     warning is still the right one.
   `_shutdown` calls `self.speech.close()` **before** `self.transport.disconnect()`,
   so the final idle state still goes out.
9. **The conversation marker.** In `speech.py`:

```python
def conversation_marker(config) -> Path   # Path(config.state_file).with_name("voice.json")
def conversation_is_live(config, max_age: float = 30.0) -> bool
```

   Built on `state.write_state` / `state.read_state` (`state.py:30-61`) so the
   staleness rule is the one that already exists and a crashed voice process
   cannot mute the agent forever. `realtime_runner` writes `{"busy": true}` beside
   `self._on_state("listening")` (~424), refreshes it per turn, and writes
   `{"busy": false}` when the conversation closes (~459-470). *A file and not
   MQTT because the broker is on the Pi* - put that in the comment.
10. **`cmd_say`** gains `--route {jarvis,local,both,auto}` (default: config) and
    `--local` as a synonym. It builds a `Voice` with the route overridden, calls
    `say()`, then `close()` - which is what makes a one-shot CLI actually wait for
    playback - and prints `{"spoken": [...], "text": ...}`.

## Tests (A) - `tests/test_speech.py`

Docstring: *the only unforgivable failures here are taking the agent down, and
blocking its tick, in order to say something.*

Fixtures: `FakeJarvis` (counts `say()`, optionally raises `JarvisError`),
`FakeSpeaker` (`synthesize()` returns `np.zeros(100, int16), 24000`, counts
calls), and a `voice` fixture that always closes.

- `test_route_defaults_to_auto` / `test_blank_route_follows_jarvis_none` - the
  back-compat resolver.
- `test_unknown_route_is_a_config_problem` - `validate()` names it.
- `test_say_returns_immediately` - the local speaker sleeps 0.5 s; `say()` returns
  in under 50 ms.
- `test_jarvis_route_never_touches_the_speakers`.
- `test_auto_falls_back_to_local_when_the_pi_refuses`.
- `test_auto_stops_asking_a_dead_pi` - three lines, one `JarvisError`:
  `FakeJarvis.calls == 1`, all three spoken locally.
- `test_the_pi_is_tried_again_after_the_backoff` - monkeypatched clock.
- `test_both_speaks_in_both_places`.
- `test_no_sounddevice_is_a_log_line_not_a_crash` - `_build_speaker` raises
  `ImportError`; `say()` and `close()` both succeed and `caplog` has exactly one
  warning, not one per line.
- `test_no_api_key_and_no_piper_degrades_quietly`.
- `test_a_full_queue_drops_the_oldest` - `max_queued=2`, four lines, the last two
  are spoken.
- `test_a_stale_line_is_not_read_out` - `stale_after_seconds=0`.
- `test_close_drains_and_stops_the_thread`.
- `test_close_is_safe_when_nothing_was_ever_said` - no thread was ever started.
- `test_speaking_publishes_the_face_state` - a recording publish sees
  ("<prefix>/state", "speaking") then ("<prefix>/state", "idle").
- `test_a_live_conversation_defers_speech` - marker written, `defer_seconds=0.2`;
  the line is still spoken, after the wait.
- `test_route_none_says_nothing_anywhere`.
- `test_the_agent_speaks_through_the_voice_not_the_pi_client` - an AST scan of
  `service.py` for `self.jarvis.say(`, in the manner of `test_process.py:56-88`.
  Cheap, and it stays true as the file grows.

---

# B. Timers and alarms

## Decisions, with reasons

- **A new `Job.kind == "timer"`, not a separate concept.** It shares persistence,
  `LATE_TOLERANCE_SECONDS`, the agent tick as its clock, `max_jobs`,
  cancel-by-words, the state file (`service.py:238-242`) and the dashboard's jobs
  panel. It differs in exactly two places: how it fires (ring, then speak) and how
  it describes itself (a countdown). Two small branches beat a second file, a
  second loader, a second thing for the tick to read and a second answer to
  "what have I got on".
- **Separate commands `timer.set` / `timer.list` / `timer.cancel`, one store.**
  The model routes far better on a verb that matches the request; the argument
  shape genuinely differs (`minutes` + `name` against `text` + `when`); and
  `timer.cancel "the pasta"` must not be able to delete a nine o'clock reminder.
  `schedule.list` still reports **both** ("one timer and two reminders"), because
  one question deserves one answer.
- **Ring twice, then speak, then toast. No escalation in v1.** There is nothing
  to acknowledge with: no button, the microphone belongs to another process, and
  the wake word opens a conversation with the model rather than dismissing an
  alarm. An escalating alarm that can only be stopped from Task Manager is worse
  than one that is missed. The toast (`platform_win.toast.notify`, used at
  `service.py:415-421`) stays in the notification centre, which is a real
  acknowledgement surface, and the fired job is published on `topics.notice` like
  every other one.
- **The ring is always local**, whatever `speech.route` says: it is a sound for
  the person at this desk. The *sentence* follows the route, so with `both` the Pi
  says it in the kitchen too.
- **`mirror_jarvis`.** In mirror mode `jarvis_tools` are withdrawn
  (`tools.py:179-180`), so there is no `ask_jarvis` and the PC *must* own timers -
  which it now does. But the instructions come from the Pi verbatim
  (`session_config.py:229-239`) and still describe Pi-side timer tools that do not
  exist in this session. Fix with a short addendum appended to the mirrored
  instructions (step 8 below), never by editing the Pi.

## Files to change (B)

| file | lines | edit |
|---|---|---|
| `src/arnold/scheduler.py` | 1-18 | docstring gains a paragraph on timers |
| | 40-57 | reuse `_UNITS` / `_WORD_NUMBERS` for a new `parse_duration` |
| | after 167 | `parse_duration(text) -> float` |
| | 184-220 (`Job`) | `total_seconds: float = 0.0`, carried by `to_dict`/`from_dict` |
| | after 220 | `Job.is_timer`, `Job.remaining(now)` |
| | 222-244 (`describe`) | a timer branch with the countdown |
| | 277-303 (`add`) | accept `total_seconds`; refuse a repeat on a duration timer |
| | after 303 | `Schedule.timers()`, `Schedule.reminders()` |
| | 305-323 (`cancel`) | `cancel(query, kinds=None)` |
| `src/arnold/commands/timer.py` | new | `timer.set`, `timer.list`, `timer.cancel` |
| `src/arnold/commands/registry.py` | 164-171 | import and register `timer` |
| `src/arnold/commands/schedule.py` | 95-111 (`_list`) | count timers and reminders apart |
| `src/arnold/service.py` | 357-370 | timers ring, reminders do not; toast on fire |
| `src/arnold/speech.py` | (from A) | `say(..., ring=True)` plays the ring first |
| `src/arnold/voice/tools.py` | 39-55 | three `timer.*` names in `PC_COMMANDS` |
| | 98-105 | rewrite the schedule paragraph: timers are ours, the house is his |
| | 11-16 | module docstring: timers are no longer "deliberately not copied here" |
| | 281-288 | drop "timers, alarms, reminders" from the `ask_jarvis` description |
| `src/arnold/voice/session_config.py` | 48-61 | rewrite `DEFAULT_PERSONA` lines 52-55 |
| | after 239 | `mirror_addendum(config)`, applied in `load_session_config` |
| `src/arnold/config.py` | 660-670 | `ScheduleConfig`: `timers`, `ring`, `ring_seconds`, `max_timer_hours` |
| `config.example.yaml` | 273-282 | document the four new `schedule:` keys |
| `README.md` | 275-294, 650-678 | `### Schedule` becomes `### Reminders, timers and alarms`; command rows |
| `tests/test_timers.py` | new | see below |
| `tests/test_scheduler.py` | append | `parse_duration`, timer describe / remaining |

## Exact shapes (B)

```python
# scheduler.py
@dataclass(slots=True)
class Job:
    ...
    # Only for kind == "timer": what it was set for, so `list` can say "the
    # twenty-minute pasta timer" rather than reading back an absolute time
    # nobody chose. 0 means it was set at a clock time - an alarm.
    total_seconds: float = 0.0

    @property
    def is_timer(self) -> bool:
        return self.kind == "timer"

    def remaining(self, now: float | None = None) -> float:
        return max(0.0, self.when - (time.time() if now is None else now))


def parse_duration(text: str) -> float:
    """'twenty minutes', '20m', '1:30', 'an hour and a half' -> seconds.

    Deliberately separate from parse_when: a timer is a length, and 'twenty
    minutes' with no 'in' in front of it is the commonest way anyone says one.
    Raises ScheduleError with a spoken sentence when it cannot be certain -
    'set a timer for 20' is refused rather than guessed at.
    """
```

`describe()` timer branch, using `humanize.duration_speech` (`humanize.py:50-74`):

```
"the pasta timer, nine minutes left"
"the pasta timer, less than a minute left"
"a twenty-minute timer, nineteen minutes left"     # no name given
"the alarm for 07:00, every weekday"               # total_seconds == 0
```

`timer.set` resolves its time in this order: numeric `minutes`/`seconds`/`hours`
args, then `parse_duration(args["for"])`, then `parse_when(args["when"])`.

```python
# commands/timer.py
registry.register(
    "timer.set", _set,
    "Set a kitchen-style timer, or an alarm, that rings on this PC.",
    {"minutes": "how long, e.g. 20",
     "for": "or a spoken length: 'twenty minutes', 'an hour and a half'",
     "when": "or a clock time for an alarm: 'at 7', 'every weekday at 7'",
     "name": "what it is for, e.g. 'pasta' - so it can be read back and cancelled"},
)
registry.register("timer.list", _list,
                  "Timers and alarms running now, with what is left on each.")
registry.register("timer.cancel", _cancel, "Stop a timer by name, or 'all'.",
                  {"which": "'pasta', its id, or 'all'"})
```

Speech, written to be read aloud:

- set: "Right - twenty minutes on the pasta."
- list: "Two: the pasta timer, nine minutes left; and the alarm for 07:00."
- list, empty: "Nothing running."
- cancel: "Stopped the pasta timer."
- over `max_timer_hours`: "That's longer than a day - set it as a reminder instead."
- `schedule.timers` off: "Timers are switched off on this PC."

`service._run_due_jobs` (347-370) becomes:

```python
if job.kind == "command":
    self._run_job_command(job)
elif job.is_timer:
    self.speech.say(job.what, ring=self.config.schedule.ring)
    if IS_WINDOWS:  # a ring you slept through is still in the notification centre
        toast.notify(self.config.assistant_name(), job.what)
else:
    self.speech.say(job.what)
```

New persona text for `session_config.py:52-55`:

```
"...you are the one who runs this computer. The two of you are colleagues.
Timers, alarms and reminders for the person sitting here are YOURS - set them
with timer.set and schedule.add and never hand them over. Use ask_jarvis for
what only {jarvis} can do: the house, anything he has been told that you have
not, and anything that has to ring in another room; and tell_jarvis to have him
say something aloud there. When you relay something from him, say so."
```

`mirror_addendum(config)`, appended to the Pi's instructions in mirror mode only:

```
"You are answering from the Windows desktop, not the Pi. Your Pi-side tools are
not available here: set timers and alarms with the pc_agent tool
(timer.set / timer.list / timer.cancel) and they will ring on these speakers."
```

## Tests (B) - `tests/test_timers.py`, plus additions to `test_scheduler.py`

Docstring: *a timer that is silently swallowed is the failure mode, and the Pi
being off must not be able to cause it.* Same fixed-`NOW` discipline as
`test_scheduler.py:25`.

- `TestParseDuration`: `test_spoken_lengths` (parametrised - "twenty minutes"
  1200, "an hour and a half" 5400, "90 seconds" 90, "1:30" 90, "20m" 1200);
  `test_a_bare_number_is_refused` ("for 20" names minutes vs seconds in the
  message); `test_too_short_is_refused`.
- `TestSetting`: `test_a_timer_is_a_job_with_a_total` (kind, `total_seconds`,
  `when == NOW + 1200`); `test_a_clock_time_is_an_alarm` (`total_seconds == 0`);
  `test_a_repeat_on_a_duration_is_refused`; `test_longer_than_a_day_is_refused`;
  `test_timers_off_in_config_is_refused_with_a_sentence`.
- `TestDescribing`: `test_a_named_timer_reads_back_with_what_is_left`;
  `test_an_unnamed_timer_says_its_length`; `test_under_a_minute`;
  `test_an_alarm_reads_as_a_time`.
- `TestListing`: `test_timer_list_ignores_reminders`;
  `test_schedule_list_counts_both`; `test_timer_cancel_cannot_drop_a_reminder`;
  `test_cancel_all_only_stops_timers`.
- `TestFiring`: `test_a_due_timer_rings_and_speaks` (a fake `Voice` recording
  `(text, ring)`; `ring is True`); `test_a_due_reminder_does_not_ring`;
  `test_a_timer_that_fired_while_the_pc_slept_is_dropped`
  (`LATE_TOLERANCE_SECONDS + 60`).
- `TestReachable`, in the `tests/test_session.py` style:
  `test_timer_commands_are_offered_to_the_voice_session` (all three in
  `PC_COMMANDS`); `test_timer_commands_are_in_the_registry`;
  `test_timers_do_not_need_the_desktop` (so Jarvis can set one over SSH).
- `TestPersona`: `test_the_persona_no_longer_sends_timers_to_the_pi` -
  `persona_for(Config())` mentions `timer.set` and no longer lists timers among
  the ask_jarvis jobs; `test_ask_jarvis_no_longer_claims_timers` - "timers" is
  absent from that tool's description; `test_mirror_mode_gets_the_local_timer_addendum`.

---

# C. More observers

Six new pure `(World) -> list[Notice]` functions in the style of
`notices.py:70-190`, appended to `OBSERVERS` (193-198). Thresholds live in
`ProactiveConfig` (`config.py:627-657`) and are documented alongside the existing
ones at `config.example.yaml:265-270`.

## Two small enablers first

- **A memo on the notice book** (`notices.py:266-317`), which is what makes edge
  detection possible:

```python
def remember(self, key: str, value: Any) -> None   # into self._memo, then _save()
def recall(self, key: str, default: Any = None) -> Any
```

  persisted in the same JSON under `"memo"` (loaded at 280-291, saved at
  293-299), capped at 100 keys so a runaway observer cannot grow the file. Safe
  to read from an observer because `world.book` is set at `notices.py:473`,
  *before* `observe(world)` at 476.
- **`World` gains `links`** so an observer can see what the agent knows about the
  network: `World(config, snapshot, history, alerts, links=None)`, a plain dict
  defaulting to `{}`. `service.py:329-331` passes
  `{"mqtt_connected": bool(self.transport and self.transport.connected),
  "pi_down_since": self.speech.pi_down_since}`. An optional keyword, so every
  existing `World(...)` in the tests keeps working unchanged.

## The six observers

| observer | condition | what it says | priority | config | key |
|---|---|---|---|---|---|
| `pending_reboot` | any of the three registry markers **and** `uptime_seconds > reboot_after_hours * 3600` | "Windows has been waiting to restart since an update, and this machine has been up for eleven days. It's a good moment to reboot." | 6, or 8 past 21 days up | `reboot_after_hours: 72.0` | `pending_reboot` |
| `watched_process_ended` | `processes.watched.<name>.running` was True in the memo and is False now | "Steam has closed, after four hours." | 4 | `announce_watched_exit: true` | `watched_ended:<name>:<started ts>` |
| `battery_low` | `battery` present, `plugged_in` false, and (`percent <= battery_percent` or `seconds_left <= battery_minutes * 60`) | "You're on battery, at 14 percent - about twenty minutes left." | 8 | `battery_percent: 20.0`, `battery_minutes: 25.0` | `battery_low:<20/10/5 band>` |
| `pi_unreachable` | `links["pi_down_since"]` older than `pi_quiet_hours * 3600`, or `mqtt_connected` false for as long | "I haven't been able to reach the Pi since two o'clock, so anything I say is only coming out of these speakers." | 5 | `pi_quiet_hours: 2.0` | `pi_unreachable:<hour bucket>` |
| `drive_vanished` | a drive in the oldest of `history.recent(60)` that is absent from `snapshot["disks"]`, and which has hourly buckets going back over a day | "Drive D isn't there any more. It was showing 1.8 terabytes an hour ago." | 7 | `announce_missing_drives: true` | `drive_gone:<letter>` |
| `long_session` | `idle_seconds()` has not passed 300 for `break_after_hours`; **off by default** | "You've been at this for four hours without a break." | 3 | `break_after_hours: 0.0` (0 = off) | `long_session:<hour it crossed>` |

Details that matter:

- **`pending_reboot` uses `winreg`, never a subprocess** (`test_process.py:56-88`
  would fail it). Three reads, each in its own `try/except OSError`, all behind
  `if not IS_WINDOWS: return []`:
  - `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending` - does the key exist?
  - `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired` - does the key exist?
  - `HKLM\SYSTEM\CurrentControlSet\Control\Session Manager` - is
    `PendingFileRenameOperations` present and non-empty?
  All three open `KEY_READ | KEY_WOW64_64KEY` and need no elevation. The answer
  is cached in the memo for 30 minutes, because it cannot change without a reboot
  or an update landing.
- **The uptime part is folded in rather than being its own observer.** A long
  uptime with nothing waiting is not news; a long uptime with an update waiting
  is. A freshly rebooted machine with a marker says nothing at all - they have
  just done it.
- **`long_session` needs an idle clock**: add
  `platform_win/window.py::idle_seconds() -> float | None` using `GetLastInputInfo`
  and `GetTickCount` through `ctypes.windll.user32`, in the style of
  `cursor_position()` (`window.py:14-30`). None off Windows.
- **Overlap with the alert rules.** `config.example.yaml:378-386` already has a
  (disabled) `steam_stopped` rule. The README should say the difference plainly:
  the observer is the *transition* ("Steam has closed"), the rule is the
  *condition* ("Steam should always be running"). `_silenced`
  (`notices.py:452-462`) already keeps every observer quiet while any alert fires.
- Every observer inherits the existing policy for free: quiet hours,
  `min_gap_minutes`, `max_per_day`, `repeat_after_hours`, and `min_priority: 4` -
  which is exactly why `long_session` sits at 3. Even switched on it is only
  *recorded* until the operator also lowers the floor. Two switches for the one
  nagging feature is the right ratio.

## Files to change (C)

| file | lines | edit |
|---|---|---|
| `src/arnold/notices.py` | 26-39 | import `IS_WINDOWS` from `.platform_win`; `winreg` imported inside the observer |
| | after 190 | the six observers, each with a docstring saying why it is worth interrupting for |
| | 193-198 | extend `OBSERVERS` |
| | 204-212 (`World`) | `links: dict | None = None` |
| | 266-317 (`NoticeBook`) | `remember` / `recall`, `_memo` persisted and capped |
| `src/arnold/config.py` | 653-657 | eight new `ProactiveConfig` fields with comments |
| `src/arnold/platform_win/window.py` | after 30 | `idle_seconds()` |
| `src/arnold/service.py` | 329-331 | pass `links=` |
| `config.example.yaml` | 265-270 | document the new thresholds |
| `README.md` | 248-274 | list the new observers; explain observer vs alert rule |
| `tests/test_notices.py` | append | see below |

## Tests (C) - appended to `tests/test_notices.py`

Reuses the `config` fixture (33-39) and widens `snapshot()` (42-50) with
`battery=`, `watched=` and `disks=` keywords.

- `TestPendingReboot`: `test_it_says_nothing_without_a_marker` (the three probes
  monkeypatched False); `test_a_marker_and_a_long_uptime_is_worth_saying`;
  `test_a_marker_on_a_freshly_booted_machine_waits`;
  `test_it_is_urgent_after_three_weeks_up`;
  `test_a_registry_error_is_not_a_crash` (probe raises `OSError`);
  `test_it_does_not_read_the_registry_twice_in_a_minute`.
- `TestWatchedProcess`: `test_nothing_on_the_first_pass`;
  `test_it_notices_the_transition_to_gone`;
  `test_it_says_nothing_while_it_is_still_running`;
  `test_it_says_nothing_twice_for_one_exit`; `test_it_says_how_long_it_ran`.
- `TestBattery`: `test_a_desktop_with_no_battery_is_silent`;
  `test_plugged_in_is_silent_at_five_percent`; `test_low_and_unplugged_is_urgent`;
  `test_minutes_left_alone_can_trigger_it`;
  `test_the_key_changes_by_band_so_it_can_speak_twice_on_the_way_down`.
- `TestPiUnreachable`: `test_a_brief_blip_says_nothing`;
  `test_two_hours_down_is_worth_saying`; `test_no_links_means_no_notice` (the old
  four-argument `World(...)` stays inert).
- `TestDriveVanished`: `test_a_drive_present_ten_minutes_ago_and_gone_now`;
  `test_a_usb_stick_plugged_in_this_morning_is_not_news`;
  `test_a_drive_that_is_simply_full_is_not_reported`.
- `TestLongSession`: `test_off_by_default`; `test_four_hours_without_a_break`
  (monkeypatched `idle_seconds`); `test_a_real_break_resets_it`;
  `test_it_is_below_min_priority_so_it_is_recorded_not_spoken` - through
  `ProactiveEngine.run`, asserting `book.recent()[-1]["spoken"] is False`.
- `TestBook`: `test_the_memo_survives_a_reload`; `test_the_memo_is_capped`.
- `test_every_observer_survives_an_empty_snapshot` - parametrised over
  `OBSERVERS` with `World(config, {}, empty_history, [])`. This is the one that
  keeps the tick alive.

---

# D. Reading the active window's text

## Decisions, with reasons

- **UI Automation through `comtypes`**, in a new `platform_win/uia.py`, behind a
  new `uia` extra. Rejected:
  - the `uiautomation` PyPI package - a large wrapper that does console and DPI
    setup at import time, when we need three calls;
  - pywin32 alone - UIA exposes no IDispatch, so `win32com` cannot reach it;
  - `WM_GETTEXT` alone - fine for classic Win32 controls (Notepad, dialogs) and
    completely blind to Chrome, Electron, WPF and UWP, which is most of what is
    on this screen. It stays as the **last tier**, written with `ctypes` in the
    style of `window.py`, so something comes back even with no comtypes.
- **Order of attack** inside `read_active_window()`:
  1. `IUIAutomation.ElementFromHandle(GetForegroundWindow())`, or
     `GetFocusedElement()` when `scope="focus"`;
  2. `TextPattern` (`UIA_TextPatternId = 10014`) then
     `DocumentRange.GetText(cap)` - the one that reads a browser page or an
     editor buffer;
  3. failing that, a breadth-first walk of the control tree collecting `Name` and
     `ValueValue`, capped at 400 nodes and depth 12, de-duplicated in order;
  4. failing that, `WM_GETTEXT` over the window and its direct children.
- **It runs on a short-lived worker thread with a hard timeout.** UIA is a
  cross-process call and a hung application hangs its caller. The thread calls
  `CoInitializeEx(COINIT_MULTITHREADED)` - MTA, because an STA client marshals
  through a message pump the agent does not have - and the caller
  `join(timeout=voice.screen_text_timeout_seconds)` and raises `CommandError`
  ("That window isn't answering.") on timeout. A leaked daemon thread is the
  price; log it.
- **Command: `desktop.read_window`**, `needs_desktop=True`. Not `query.*`: the
  `query.*` family reads the telemetry snapshot, and this reaches into the
  logged-on session exactly like `desktop.screenshot` does. Over SSH,
  `_delegate_if_headless` (`cli.py:104-151`) forwards it to the agent for free.
- **No new realtime tool.** It is reached through `pc_agent`, with a paragraph in
  that description telling the model to prefer it for text and to fall back to
  `view_screen` for layout, images and charts. Reasons: it works when
  `voice.vision` is false, and therefore when `view_screen` is not even offered
  (`tools.py:147`); it costs no extra tool schema; and "read this to me" is
  answered far better by text than by a JPEG.

## Privacy story

`voice.vision` guarantees no *picture* leaves the machine. `desktop.read_window`
is strictly narrower than a screenshot - one window rather than every monitor -
but the text still goes to the model when a voice session asks for it, and
saying otherwise would be a lie. So:

- `voice.screen_text: bool = True` - the master switch, independent of
  `voice.vision`. With `vision: false, screen_text: true` the assistant can read
  what you are looking at and no screenshot is ever taken; with both false it
  cannot see the screen at all.
- `voice.screen_text_deny: list[str]` - default
  `["keepass", "bitwarden", "1password", "lastpass", "private browsing", "incognito"]`,
  matched case-insensitively against the process name **and** the window title. A
  match is refused before any UIA call is made.
- `voice.screen_text_max_chars: int = 4000` - a hard cap, truncated with
  " ... (truncated)" so the model knows it did not get everything.
- `voice.screen_text_timeout_seconds: float = 4.0`.
- The result always carries `title`, `process` and `chars`, and the handler logs
  "read %d characters from %s" - never the text itself, at any level.

## Files to change (D)

| file | lines | edit |
|---|---|---|
| `src/arnold/platform_win/uia.py` | new | `UiaError`, `available()`, `read_active_window()`, `_text_via_pattern`, `_text_via_tree`, `_text_via_wm_gettext` |
| `src/arnold/platform_win/window.py` | 33-72 | factor `foreground_hwnd()` out of `active_window()` and reuse it from uia.py |
| `src/arnold/commands/desktop.py` | 20-51 | register `desktop.read_window`, `needs_desktop=True` |
| | after 57 | the `_read_window` handler |
| `src/arnold/config.py` | 406-414 | four `screen_text*` fields in `VoiceConfig`, right under `vision` |
| `src/arnold/voice/tools.py` | 39-55 | `"desktop.read_window"` in `PC_COMMANDS` |
| | 133-135 | rewrite the closing paragraph: read_window for text, view_screen for pixels |
| | 150-159 | `view_screen` description points at read_window for text |
| `pyproject.toml` | after 19 | `uia = ["comtypes>=1.4.1"]` - the same package the `audio` extra already names |
| `config.example.yaml` | 47-53 | the screen-text keys, with the privacy comment |
| `README.md` | 630-649, 113-124, 650-678 | new `### Reading the screen`; the install line; command rows |
| `tests/test_screen_text.py` | new | see below |

## Exact shape (D)

```python
# platform_win/uia.py
class UiaError(RuntimeError):
    """UI Automation is unavailable, or the window would not answer."""

INSTALL_HINT = 'reading the screen needs comtypes - uv pip install -e ".[uia]"'

def available() -> bool: ...    # comtypes imports and CUIAutomation constructs

def read_active_window(
    scope: str = "window",      # window | focus
    max_chars: int = 4000,
    timeout: float = 4.0,
) -> dict:
    """-> {"title", "process", "pid", "text", "chars", "truncated", "how"}

    `how` is "text_pattern", "tree" or "wm_gettext", so a disappointing answer
    can be explained rather than guessed at.
    """
```

```python
# commands/desktop.py
registry.register(
    "desktop.read_window",
    _read_window,
    "Read the text of the window in the foreground, without taking a picture of it.",
    {"scope": "'window' (default), or 'focus' for just the box the caret is in",
     "max_chars": "cap on what comes back (default from config)"},
    needs_desktop=True,
)
```

Handler order, each raising `CommandError` with a speakable sentence:
`voice.screen_text` off gives "Reading the screen is switched off on this PC.";
a deny-list hit gives "I would rather not read that window."; `available()`
false gives `INSTALL_HINT`; a `UiaError` gives its own message. Empty text is a
*successful* result whose speech is "There's no text I can read in <title>."
Success speech is the title, a dash, and the first 300 characters, with the whole
thing in `result["text"]` so the model reads that rather than the speech line.

## Tests (D) - `tests/test_screen_text.py`

`comtypes` will not be installed in CI, so every test drives the command with
`platform_win.uia` monkeypatched. The point of these is the guard rails, not COM.

- `test_it_is_marked_as_needing_the_desktop` - over SSH it must be forwarded.
- `test_it_is_offered_to_the_voice_session` - present in `PC_COMMANDS`.
- `test_off_in_config_is_refused_with_a_sentence`.
- `test_a_password_manager_is_refused_before_anything_is_read` - the fake
  `read_active_window` records that it was never called.
- `test_the_deny_list_matches_the_title_too` - a 1Password vault under chrome.exe.
- `test_missing_comtypes_explains_the_install` - the error contains `.[uia]`.
- `test_text_is_capped_and_marked_truncated` - 10 000 characters in, 4 000 plus
  the marker out, `result["truncated"] is True`.
- `test_a_hung_window_is_an_error_not_a_hang` - the fake raises `UiaError`;
  `result.ok is False` and the sentence is speakable.
- `test_an_empty_window_says_so_rather_than_returning_nothing`.
- `test_it_does_not_log_the_text` - `caplog` at DEBUG holds the character count
  and not the secret string.
- `test_it_works_with_vision_off` - `config.voice.vision = False`;
  `build_tools(config)` has no `view_screen`, and `desktop.read_window` still
  dispatches.
- `test_the_pc_agent_description_tells_the_model_when_to_use_each` - it mentions
  both `desktop.read_window` and `view_screen`.

---

## Risks / open questions, each with a recommendation

1. **Should `speech.route` default to `auto`?** It means the PC starts talking out
   loud on a machine where it never did. Against: `notices.py:20-23` is explicit
   that an assistant which starts talking in your house is not a thing to switch
   on unseen. For: `auto` speaks locally *only when the Pi could not be reached*,
   so with the Pi up nothing changes at all, and the alternative is the documented
   current failure - silence. Proactive remarks stay separately gated behind
   `proactive.speak: false`, so what `auto` unmutes is alerts, work notifications
   and reminders, all of which someone explicitly asked for.
   **Recommendation: default `auto`, with exactly that reasoning in the config
   comment.**
2. **`Voice` wraps `JarvisClient` rather than replacing it,** so `tell_jarvis`
   (`tools.py:396-405`) and `jarvis.check()` keep their exact meaning.
   **Recommendation: keep `ctx.jarvis`, add `ctx.speech`; do not overload one
   object with two meanings.**
3. **Face lip-sync for agent speech.** Real lip-sync means running `MouthAnalyser`
   over the TTS PCM in the agent and pacing `eq` messages to playback - genuine
   work, and useless when the Pi is off, because the broker is on the Pi. But
   `face/state.py:363-377` already synthesises a talking envelope from a bare
   `state: speaking`. **Recommendation: v1 publishes `state` only (two retained
   messages per utterance, `publish_face_state: true`) and no `eq`. Full lip-sync
   is out of scope and cheap to add later on the same seam.**
4. **Not talking over a conversation.** MQTT is unusable for this (broker on the
   Pi). **Recommendation: a `voice.json` marker beside the state file, written by
   the voice runner and read through `state.read_state`'s existing staleness
   rule.** It also solves the real problem underneath, which is two processes
   calling `sd.play` on the same output device. If the marker turns out fiddly,
   the honest fallback is to defer the feature and accept the overlap - say so
   rather than pretending MQTT covers it.
5. **`voice/__init__` drags in the whole voice stack**, and numpy is not a core
   dependency. If the lazy import proves awkward, the alternative is moving
   `openai_tts`/`tts` up to `src/arnold/tts/`, which touches four call
   sites. **Recommendation: lazy import now, note the move as a follow-up.**
6. **`comtypes` on Python 3.14 is unverified** - it is not installed and there was
   no network during planning. It is pure Python over ctypes, so it ought to be
   fine, but `comtypes.client.GetModule("UIAutomationCore.dll")` writes generated
   code into `comtypes/gen`, which may be slow on first use or read-only under a
   scheduled task. **Recommendation: set `comtypes.client.gen_dir` to a writable
   directory beside the config before `GetModule`, treat any failure as
   "unavailable" with the install hint, and confirm with
   `uv pip install -e ".[uia]"` before writing the tree walk.**
7. **Timer accuracy is bounded by the tick** (`telemetry.interval_seconds`,
   default 10 s), so a twenty-minute timer can ring up to ten seconds late.
   **Recommendation: accept and document it. A second clock thread is exactly what
   `scheduler.py:9-11` argues against, and nobody times pasta to the second. If it
   ever matters, shorten the tick.**
8. **`schedule.cancel all`** currently drops timers too (`scheduler.py:311-314`).
   **Recommendation: keep that - one "cancel everything" should mean everything -
   but make `timer.cancel all` kind-scoped, and have the speech say which it did.**
9. **`long_session` is the observer most likely to be resented.**
   **Recommendation: ship it off by default (`break_after_hours: 0`) and at
   priority 3, below `min_priority: 4`, so even switching it on only records the
   remark until the operator also lowers the floor.**
10. **`drive_vanished` against a USB stick.** Guarded by requiring a day of hourly
    buckets, still imperfect for an external drive plugged in nightly.
    **Recommendation: ship it; `repeat_after_hours` caps it at one remark a day
    and `announce_missing_drives: false` turns it off.**
11. **`pi_unreachable` needs A's `Voice.pi_down_since`.** If A slips, it can fall
    back to `links["mqtt_connected"]` alone. **Recommendation: write it last.**
12. **None of the new config is live-reloaded.** `IdentityWatcher` covers identity
    only. Changing `speech.*`, `schedule.timers` or `voice.screen_text` needs
    `Stop-ScheduledTask Arnold; Start-ScheduledTask Arnold`.
    **Recommendation: leave it that way and document it - widening the watcher to
    a whole-config reload is a larger change with a real risk of half-reloaded
    state.**
13. **`config.yaml` is gitignored**, so these edits cannot be diffed with git.
    Copy it before editing and compare with `fc.exe`.

---

## Verification

```powershell
cd C:\Users\geogo\Downloads\Projects\ARNOLD
.venv\Scripts\python -m pytest -q                    # everything, incl. the subprocess AST scan
.venv\Scripts\python -m pytest -q tests/test_speech.py tests/test_timers.py `
    tests/test_scheduler.py tests/test_notices.py tests/test_screen_text.py

# --- A --------------------------------------------------------------------
arnold diag                              # config OK; prints the resolved speech route
arnold say --route local "Testing, one two three."
arnold say --route both  "Testing both rooms."
# with the Pi off (or jarvis.home_assistant.url pointed at a dead host):
arnold say "The Pi is down and you can still hear me."
Get-Content logs\agent.log -Tail 20                  # ONE warning about the Pi, not one per line
# with the agent and the face running: the face should mouth an alert.

# --- B --------------------------------------------------------------------
arnold exec timer.set --arg minutes=1 --arg name=pasta
arnold exec timer.list                   # "the pasta timer, less than a minute left"
# wait: ring, then the line, then a toast that stays in the notification centre
arnold exec timer.set --arg for="twenty minutes" --arg name=render
arnold exec timer.cancel --arg which=render
arnold exec schedule.list                # counts the timer and the reminders apart
# by voice, with the Pi off: "set a timer for two minutes for the pasta"
#   -> must NOT reach for ask_jarvis; check logs\agent.log for timer.set

# --- C --------------------------------------------------------------------
# drop proactive.interval_seconds to 30 temporarily rather than waiting five minutes
Get-Content logs\agent.log -Tail 40 | Select-String "saying unprompted|keeping quiet"
arnold exec query.metric --arg path=uptime_seconds
# then read the dashboard's Noticed panel: every new observer should appear there
# as recorded-but-not-spoken before any of them is ever heard aloud.

# --- D --------------------------------------------------------------------
uv pip install -e ".[uia]"
arnold exec desktop.read_window --pretty            # Notepad, then a browser
arnold exec desktop.read_window --arg scope=focus --pretty
# focus a password manager and repeat -> refused, and nothing is read
# set voice.vision: false, then by voice: "read me what's on screen"
```

## Out of scope

- Full lip-sync (an `eq` stream) for agent speech, and a local MQTT broker on the
  PC.
- Escalating or snoozable alarms, and any timer UI beyond the toast and the
  dashboard's existing jobs panel.
- Anything on the Pi (`pi/`), including Jarvis's own timers - he keeps the house,
  and a timer set here is not mirrored there.
- Whole-config live reload; extending `IdentityWatcher` past identity.
- SMART or drive-health probing, and any observer that needs a child process.
- OCR, continuous screen watching, and clicking or typing through UI Automation -
  this reads, it does not act.
- Replacing `voice.vision`: screenshots stay, for layout, images and charts.
