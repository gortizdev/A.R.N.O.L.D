"""One wake-to-idle conversation over the OpenAI Realtime API.

Modelled directly on Jarvis's `run_realtime_conversation` (assistant.py:5160),
whose protocol comment records it as verified live against gpt-realtime-2 - so
the event names, the 24 kHz PCM formats and the server-VAD settings here match
what is known to work rather than what the docs imply.

Speech never becomes text on this machine: audio goes up, audio comes back.
That is the whole point - it is what gives the Pi's assistant its timing and
character, and a transcribe-then-synthesise pipeline cannot reproduce it.

The microphone is gated while audio is playing, so the assistant does not hear
and answer itself. Barge-in (see TurnTaking) is the exception: sustained speech
clearly louder than that echo stops the reply, because an assistant that cannot
be interrupted is one you end up talking over.
"""

from __future__ import annotations

import base64
import json
import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from ..mouth import TUNING, MouthAnalyser

log = logging.getLogger(__name__)

RATE = 24000  # both directions, per Jarvis's session config
# Silence after the last audio before the session closes itself.
DEFAULT_IDLE_SECONDS = 25.0
# Hard cap, so a stuck session cannot bill indefinitely.
DEFAULT_MAX_SECONDS = 900.0


class RealtimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class TurnTaking:
    """When the microphone is live during a reply, and what it takes to cut in.

    Gating the mic for the whole of a reply is the safe reading for a webcam mic
    sitting in front of a pair of speakers - nothing the assistant says can come
    back round and be mistaken for an instruction. It is also why interrupting
    it does not work: you talk, it hears none of it, and you both carry on.

    Barge-in keeps measuring the microphone through the reply and yields when
    what it hears is loud enough for long enough to be a person rather than the
    room. `threshold` is the level that matters: it has to clear the echo of the
    assistant's own voice, which RealtimeConversation logs at the end of every
    conversation so it can be set against a measurement rather than a guess.
    """

    barge_in: bool = True
    threshold: float = 0.055
    hold_seconds: float = 0.25
    grace_seconds: float = 0.5
    echo_guard_seconds: float = 0.25

    @classmethod
    def from_config(cls, voice) -> "TurnTaking":
        return cls(
            barge_in=bool(voice.barge_in),
            threshold=float(voice.barge_in_threshold),
            hold_seconds=max(0.0, voice.barge_in_hold_ms / 1000.0),
            grace_seconds=max(0.0, voice.barge_in_grace_ms / 1000.0),
            echo_guard_seconds=max(0.0, voice.echo_guard_ms / 1000.0),
        )


def decimate_to_24k(pcm: np.ndarray, rate: int) -> np.ndarray:
    """48k -> 24k by averaging sample pairs (cheap anti-aliased 2:1)."""
    if rate == RATE:
        return pcm
    if rate == 48000:
        n = len(pcm) & ~1
        x = pcm[:n].astype(np.int32)
        return ((x[0::2] + x[1::2]) // 2).astype(np.int16)
    step = max(1, int(round(rate / RATE)))
    return pcm[::step]


class RealtimeConversation:
    def __init__(
        self,
        api_key: str,
        session: Any,
        tools: list[dict],
        dispatch_tool: Callable[[str, dict], Any],
        *,
        output_device: int | None = None,
        on_state: Callable[[str], None] | None = None,
        on_levels: Callable[[list[float]], None] | None = None,
        on_transcript: Callable[[str, str], None] | None = None,
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
        max_seconds: float = DEFAULT_MAX_SECONDS,
        turn: TurnTaking | None = None,
    ) -> None:
        self.api_key = api_key
        self.session = session
        self.tools = tools
        self.dispatch_tool = dispatch_tool
        self.output_device = output_device
        self.on_state = on_state or (lambda s: None)
        self.on_levels = on_levels or (lambda levels: None)
        self.on_transcript = on_transcript or (lambda who, text: None)
        self.idle_seconds = idle_seconds
        self.max_seconds = max_seconds
        self.turn = turn or TurnTaking()

        self._ws = None
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._play_queue: queue.Queue = queue.Queue(maxsize=256)
        # Monotonic time until which queued audio is still audible; the mic is
        # gated until then so the assistant cannot transcribe its own voice.
        self._play_head = 0.0
        self._pending_tools: set[str] = set()
        self._pending_lock = threading.Lock()
        self._last_activity = time.monotonic()
        self._ended = False

        # Barge-in bookkeeping. The active item and how much of it has actually
        # reached the speaker are needed to tell the model what was heard: it
        # streams a reply in a second or two, so "what it said" and "what you
        # got to hear before cutting in" are rarely the same sentence.
        self._active_item: str | None = None
        self._active_content_index = 0
        self._item_audible_at = 0.0
        self._item_ms = 0.0
        self._cancelled_items: set[str] = set()
        # Whether there is still a response being generated. Audio arrives
        # around six times faster than it plays, so by the time you cut in the
        # reply is usually finished and only the speaker is behind - cancelling
        # then is an error from the server, not a stop.
        self._response_active = False
        self._loud_since = 0.0
        # Frames since the noise started, so the word you cut in with is not
        # eaten by the hold that proves you meant it.
        self._barge_buffer: deque = deque(maxlen=16)
        self._echo_peak = 0.0

        # Mouth envelope: analysed as audio arrives, published as it is heard.
        self.tuning = TUNING
        self.analyser = MouthAnalyser(TUNING)
        self._pending_levels: deque = deque()
        self._pending_lock = threading.Lock()
        self._level_wake = threading.Event()

    # -- socket -------------------------------------------------------------

    def _send(self, payload: dict) -> None:
        if self._ws is None:
            return
        with self._send_lock:
            try:
                self._ws.send(json.dumps(payload))
            except Exception as exc:
                log.debug("send failed: %s", exc)

    def _connect(self) -> bool:
        import websocket

        url = f"wss://api.openai.com/v1/realtime?model={self.session.model}"
        try:
            self._ws = websocket.create_connection(
                url, header=[f"Authorization: Bearer {self.api_key}"], timeout=10
            )
        except Exception as exc:
            log.warning("realtime connect failed: %s", exc)
            return False

        try:
            first = json.loads(self._ws.recv())
            if first.get("type") != "session.created":
                log.warning("unexpected first event: %s", first.get("type"))
                self._close()
                return False
        except Exception as exc:
            log.warning("realtime handshake failed: %s", exc)
            self._close()
            return False

        self._send(
            {
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "instructions": self.session.instructions,
                    "tools": self.tools,
                    "tool_choice": "auto",
                    "audio": {
                        "input": {
                            "format": {"type": "audio/pcm", "rate": RATE},
                            "transcription": {"model": self.session.transcription_model},
                            "turn_detection": {
                                **self.session.turn_detection,
                                "create_response": True,
                                # Only ever reached by audio sent after we have
                                # already decided you are cutting in, since the
                                # mic is gated until then - so this is a
                                # backstop for the reply we did not manage to
                                # cancel, not the thing that detects barge-in.
                                "interrupt_response": self.turn.barge_in,
                            },
                        },
                        "output": {
                            "format": {"type": "audio/pcm", "rate": RATE},
                            "voice": self.session.voice,
                            "speed": self.session.speed,
                        },
                    },
                },
            }
        )
        log.info(
            "realtime session open (%s, voice=%s)", self.session.model, self.session.voice
        )
        return True

    def _close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    # -- audio --------------------------------------------------------------

    def _speaking(self) -> bool:
        return time.monotonic() < self._play_head + self.turn.echo_guard_seconds

    def _playback_worker(self) -> None:
        import sounddevice as sd

        stream = None
        try:
            stream = sd.OutputStream(
                samplerate=RATE, channels=1, dtype="int16", device=self.output_device
            )
            stream.start()
            while not self._stop.is_set():
                try:
                    chunk = self._play_queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                if chunk is None:
                    break
                stream.write(chunk)
        except Exception as exc:
            log.error("playback failed: %s", exc)
        finally:
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass

    def _enqueue_audio(self, pcm: np.ndarray) -> float:
        now = time.monotonic()
        duration = len(pcm) / RATE
        # When this chunk will actually reach the speaker - everything already
        # queued has to play first.
        audible_at = max(self._play_head, now)
        self._play_head = audible_at + duration
        try:
            self._play_queue.put_nowait(pcm)
        except queue.Full:
            log.debug("playback queue full; dropping a chunk")
        self._schedule_levels(pcm, audible_at, duration)
        return audible_at

    def _schedule_levels(self, pcm: np.ndarray, audible_at: float, duration: float) -> None:
        """Analyse now, publish when the audio is heard.

        The model streams audio far faster than real time - measured at about
        six times - so a whole reply arrives in a second or two. Publishing the
        envelope on arrival makes the face perform the entire sentence before
        the speaker has finished the first few words, and then sit there while
        the voice carries on. The DSP still happens here, once, off the render
        thread; only the delivery waits.
        """
        hop = max(32, int(RATE * self.tuning.hop_seconds))
        with self._pending_lock:
            for offset in range(0, max(1, len(pcm) - hop + 1), hop):
                frame = self.analyser.analyse(pcm[offset : offset + hop], RATE)
                self._pending_levels.append((audible_at + offset / RATE, frame))
        self._level_wake.set()

    def _level_pacer(self) -> None:
        """Publish each analysed frame at the moment its audio is audible."""
        while not self._stop.is_set():
            with self._pending_lock:
                due = self._pending_levels[0][0] if self._pending_levels else None
            if due is None:
                self._level_wake.wait(0.2)
                self._level_wake.clear()
                continue

            wait = due - time.monotonic()
            if wait > 0:
                # Woken early if something more urgent arrives.
                self._level_wake.wait(min(wait, 0.2))
                self._level_wake.clear()
                continue

            with self._pending_lock:
                if not self._pending_levels:
                    continue
                _, frame = self._pending_levels.popleft()
            self.on_levels(frame)

    def _drop_pending_levels(self) -> None:
        """Forget queued frames - the audio they described is not going to play."""
        with self._pending_lock:
            self._pending_levels.clear()


    def _mic_worker(self, mic_frames, mic_rate: int) -> None:
        for frame in mic_frames:
            if self._stop.is_set():
                return
            if frame is None:
                continue
            # Gate while speaking, or the model hears its own output and the
            # server VAD starts a turn against it.
            if self._speaking():
                if self._consider_barge_in(frame):
                    # This frame is already in the buffer, along with the rest
                    # of the run that earned the interruption.
                    self._flush_barge_buffer(mic_rate)
                continue
            self._send_frame(frame, mic_rate)

    def _send_frame(self, frame: np.ndarray, mic_rate: int) -> None:
        pcm = decimate_to_24k(frame, mic_rate)
        self._send(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm.tobytes()).decode("ascii"),
            }
        )

    def _consider_barge_in(self, frame: np.ndarray) -> bool:
        """Decide whether this frame is you cutting in. True means we yielded.

        Loudness alone is not enough: the assistant's own voice arrives back
        through the microphone, and so does a cough, a door and the keyboard.
        So it takes a level above `threshold` held for `hold_seconds`, and only
        once the reply has been running long enough that the tail of your own
        last sentence cannot be what is being heard.
        """
        if not self.turn.barge_in:
            return False

        rms = float(np.sqrt(np.mean((frame.astype(np.float32) / 32768.0) ** 2)))
        self._echo_peak = max(self._echo_peak, rms)

        now = time.monotonic()
        if now - self._item_audible_at < self.turn.grace_seconds:
            self._loud_since = 0.0
            self._barge_buffer.clear()
            return False

        if rms < self.turn.threshold:
            self._loud_since = 0.0
            self._barge_buffer.clear()
            return False

        self._barge_buffer.append(frame)
        if self._loud_since == 0.0:
            self._loud_since = now
        if now - self._loud_since < self.turn.hold_seconds:
            return False

        self._barge_in()
        return True

    def _barge_in(self) -> None:
        """Stop talking. You have the floor."""
        played_ms = int(
            min(self._item_ms, max(0.0, (time.monotonic() - self._item_audible_at) * 1000.0))
        )
        log.info("barge-in after %d ms of the reply", played_ms)

        # Ungate the mic and bin everything that has not been heard yet - both
        # the audio and the mouth movements that described it.
        self._play_head = 0.0
        self._loud_since = 0.0
        self._drain_playback()
        self._drop_pending_levels()
        self.analyser.reset()
        self.on_levels({"bands": [0.0] * 16, "open": 0.0, "wide": TUNING.rest_width})

        # Truncate before cancelling: the model's record of its own turn has to
        # end where you actually stopped hearing it, or it will follow up on
        # half a sentence you never got.
        if self._active_item is not None:
            self._cancelled_items.add(self._active_item)
            self._send(
                {
                    "type": "conversation.item.truncate",
                    "item_id": self._active_item,
                    "content_index": self._active_content_index,
                    "audio_end_ms": played_ms,
                }
            )
        if self._response_active:
            self._send({"type": "response.cancel"})
            self._response_active = False

        self.on_state("listening")
        self._last_activity = time.monotonic()

    def _drain_playback(self) -> None:
        """Discard queued audio. A chunk already handed to the sound card still
        plays, which is a short fade rather than a hard cut - no bad thing."""
        while True:
            try:
                self._play_queue.get_nowait()
            except queue.Empty:
                return

    def _flush_barge_buffer(self, mic_rate: int) -> None:
        """Send the speech that proved you were interrupting."""
        while self._barge_buffer:
            self._send_frame(self._barge_buffer.popleft(), mic_rate)

    # -- tools --------------------------------------------------------------

    def _run_tool(self, call_id: str, name: str, arguments: str) -> None:
        try:
            parsed = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            parsed = {}

        try:
            result = self.dispatch_tool(name, parsed)
        except Exception as exc:
            log.exception("tool %s failed", name)
            result = {"error": str(exc)}

        # A function_call_output is a plain string, so an image cannot ride in
        # it. Tools that captured one hand it over under __screen_b64__ and it
        # goes in as its own user message - the same shape, and the same
        # ordering, that Jarvis uses on the Pi (assistant.py:5412).
        image = result.pop("__screen_b64__", None) if isinstance(result, dict) else None
        if image:
            self._send(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_image",
                                "image_url": f"data:image/jpeg;base64,{image}",
                                # `auto` already means high, but on-screen text
                                # is the whole point here, so be explicit.
                                "detail": "high",
                            }
                        ],
                    },
                }
            )

        self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(result, default=str)[:8000],
                },
            }
        )
        self._send({"type": "response.create"})
        with self._pending_lock:
            self._pending_tools.discard(call_id)
        self._last_activity = time.monotonic()

    # -- main loop ----------------------------------------------------------

    def run(self, mic_frames, mic_rate: int) -> bool:
        """Hold one conversation. Returns False if the session never opened."""
        import websocket

        if not self._connect():
            return False

        self.on_state("listening")
        started = time.monotonic()
        self._last_activity = started

        threading.Thread(target=self._playback_worker, daemon=True, name="rt-play").start()
        threading.Thread(
            target=self._mic_worker, args=(mic_frames, mic_rate), daemon=True, name="rt-mic"
        ).start()
        threading.Thread(target=self._level_pacer, daemon=True, name="rt-mouth").start()

        try:
            while not self._stop.is_set():
                if time.monotonic() - started > self.max_seconds:
                    log.info("realtime session hit its time cap")
                    break

                with self._pending_lock:
                    busy = bool(self._pending_tools)
                if (
                    not busy
                    and not self._speaking()
                    and time.monotonic() - self._last_activity > self.idle_seconds
                ):
                    log.info("realtime session idle; closing")
                    break

                try:
                    self._ws.settimeout(0.5)
                    raw = self._ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                except Exception as exc:
                    log.info("realtime socket closed: %s", exc)
                    break

                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if self._handle_event(event):
                    break
        finally:
            self._stop.set()
            self._drop_pending_levels()
            self._level_wake.set()
            try:
                self._play_queue.put_nowait(None)
            except queue.Full:
                pass
            self._close()
            self.on_levels({"bands": [0.0] * 16, "open": 0.0, "wide": TUNING.rest_width})
            self.on_state("idle")
            self._report_echo()

        return True

    def _report_echo(self) -> None:
        """Say what the microphone heard while the assistant was talking.

        This is the number voice.barge_in_threshold has to clear, and it is
        specific to the room, the mic and how loud the speakers are - so it is
        printed rather than guessed at. Everything above it is your voice;
        everything below it is the assistant hearing itself.
        """
        if not self.turn.barge_in or self._echo_peak <= 0.0:
            return
        log.info(
            "mic peaked at RMS %.3f during playback; barge-in fires at %.3f",
            self._echo_peak,
            self.turn.threshold,
        )
        if self.turn.threshold < self._echo_peak * 1.3:
            log.warning(
                "voice.barge_in_threshold (%.3f) is close to the echo of its own voice "
                "(%.3f) - it may cut itself off. Raise it to about %.3f.",
                self.turn.threshold,
                self._echo_peak,
                round(self._echo_peak * 1.6, 3),
            )

    def _handle_event(self, event: dict) -> bool:
        """Handle one event. Returns True to end the conversation."""
        kind = event.get("type", "")

        if kind == "response.created":
            self._response_active = True

        elif kind == "response.output_audio.delta":
            data = event.get("delta") or ""
            item_id = event.get("item_id") or ""
            # Deltas keep arriving for a moment after a cancel; playing them
            # would re-gate the mic in the middle of the interruption.
            if data and item_id not in self._cancelled_items:
                self.on_state("speaking")
                pcm = np.frombuffer(base64.b64decode(data), dtype=np.int16)
                audible_at = self._enqueue_audio(pcm)
                if item_id != self._active_item:
                    self._active_item = item_id
                    self._active_content_index = int(event.get("content_index") or 0)
                    self._item_audible_at = audible_at
                    self._item_ms = 0.0
                self._item_ms += len(pcm) / RATE * 1000.0
                self._last_activity = time.monotonic()

        elif kind == "input_audio_buffer.speech_started":
            # The user cut in. Whatever was queued for the mouth describes
            # audio that will not now be heard.
            self._drop_pending_levels()
            self.analyser.reset()
            self.on_state("listening")
            self._last_activity = time.monotonic()

        elif kind == "input_audio_buffer.speech_stopped":
            self.on_state("thinking")
            self._last_activity = time.monotonic()

        elif kind == "conversation.item.input_audio_transcription.completed":
            text = (event.get("transcript") or "").strip()
            if text:
                log.info("you: %s", text)
                self.on_transcript("user", text)

        elif kind == "response.output_audio_transcript.done":
            text = (event.get("transcript") or "").strip()
            if text:
                log.info("%s: %s", self.session.name.lower(), text)
                self.on_transcript("assistant", text)

        elif kind == "response.function_call_arguments.done":
            name = event.get("name") or ""
            call_id = event.get("call_id") or ""
            if name == "end_conversation":
                log.info("assistant ended the conversation")
                self._ended = True
                # Let the sign-off finish playing before tearing down.
                remaining = max(0.0, self._play_head - time.monotonic())
                time.sleep(min(6.0, remaining + 0.3))
                return True
            self.on_state("thinking")
            with self._pending_lock:
                self._pending_tools.add(call_id)
            threading.Thread(
                target=self._run_tool,
                args=(call_id, name, event.get("arguments") or "{}"),
                daemon=True,
                name=f"rt-tool-{name}",
            ).start()

        elif kind == "response.done":
            self._response_active = False
            self._last_activity = time.monotonic()

        elif kind == "error":
            detail = event.get("error") or {}
            log.error("realtime error: %s", detail.get("message") or detail)

        return False

    def stop(self) -> None:
        self._stop.set()
