"""The listen -> transcribe -> answer -> speak loop.

While running, this publishes its state to MQTT under the same topics the
Pi-side bridge uses for Jarvis. That does two jobs at once: the on-screen face
reacts to the PC's own listening and speaking, and Jarvis can see that this PC
has claimed the wake word and stay quiet so you do not get two assistants
answering the same question.

The claim is published retained, so the Pi sees it immediately on connect. It
is cleared on a clean exit, and by the agent's MQTT last-will if the PC sleeps
or loses power - which is what makes the handoff back to the Pi automatic.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from enum import Enum

import numpy as np

from ..alerts import AlertEngine
from ..commands import CommandContext
from ..config import Config
from ..face.local import LocalFaceSender
from ..jarvis import JarvisClient
from ..monitors.collector import Collector
from .audio import (
    FRAME_SAMPLES,
    SAMPLE_RATE,
    AudioError,
    Microphone,
    UtteranceRecorder,
    WakeWordDetector,
    resolve_device,
)
from .brain import BrainError, build_brain
from .stt import Transcriber
from .tts import Speaker
from .wake_ack import WakeAck

log = logging.getLogger(__name__)


class VoiceState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


class VoiceAssistant:
    def __init__(self, config: Config) -> None:
        self.config = config
        voice = config.voice

        self._stop = threading.Event()
        self._state = VoiceState.IDLE
        self._mqtt = None
        self._local_face = LocalFaceSender(config)

        input_device = resolve_device(voice.input_device, input=True)
        self._output_device = resolve_device(voice.output_device, input=False)

        log.info("loading wake word %r", voice.wake_word)
        self.detector = WakeWordDetector(
            voice.wake_word,
            voice.wake_threshold,
            patience=voice.wake_patience,
            vad_threshold=voice.wake_vad_threshold,
            wake_dir=voice.wake_word_dir,
        )

        log.info("loading speech recognition (%s)", voice.stt_model)
        self.transcriber = Transcriber(
            voice.stt_model, voice.stt_device, voice.stt_compute_type
        )

        log.info("loading voice (%s)", voice.piper_voice)
        self.speaker = Speaker(voice.piper_voice, voice.piper_dir, self._output_device)

        # Shares the Speaker, so the "Yes?" sounds like whatever answers next.
        self.ack = WakeAck(config, self._output_device, synth=self.speaker)
        self.ack.prepare()

        self.recorder = UtteranceRecorder(
            silence_threshold=voice.silence_threshold,
            silence_timeout=voice.silence_timeout_seconds,
            max_seconds=voice.max_utterance_seconds,
            min_seconds=voice.min_utterance_seconds,
        )

        context = CommandContext(
            config=config,
            collector=Collector(config.monitors, notifications=config.notifications, claude=config.claude),
            alerts=AlertEngine(config.alerts, config.device.friendly_name),
            jarvis=JarvisClient(config.jarvis),
        )
        self.brain = build_brain(config, context)
        self._input_device = input_device

    # -- state broadcasting -------------------------------------------------

    def _connect_mqtt(self) -> None:
        if not self.config.mqtt.enabled:
            return
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            return

        cfg = self.config.mqtt
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=f"{cfg.client_id}-voice"
        )
        if cfg.username:
            client.username_pw_set(cfg.username, cfg.password)

        prefix = self.config.assistant_topic_prefix()
        self._claim_topic = f"{self.config.face.jarvis_topic_prefix.rstrip('/')}/wake_owner"
        # If this process dies, the claim must not outlive it or Jarvis would
        # stay muted forever.
        if self.config.voice.claim_wake_word:
            client.will_set(self._claim_topic, "", qos=1, retain=True)

        client.reconnect_delay_set(min_delay=2, max_delay=60)
        try:
            client.connect_async(cfg.host, cfg.port, cfg.keepalive)
            client.loop_start()
            self._mqtt = client
            self._topic_prefix = prefix
        except Exception as exc:
            log.warning("voice could not reach the broker: %s", exc)

    def _local_prefix(self) -> str:
        return self.config.assistant_topic_prefix() + "/"

    def _publish(self, topic: str, payload, retain: bool = False, qos: int = 0) -> None:
        # The face on this desktop hears it directly, broker or no broker.
        if topic.startswith(self._local_prefix()):
            self._local_face.send(topic, payload)
        if self._mqtt is None:
            return
        if not isinstance(payload, (str, bytes)):
            payload = json.dumps(payload)
        try:
            self._mqtt.publish(topic, payload, qos=qos, retain=retain)
        except Exception:
            pass

    def _set_state(self, state: VoiceState) -> None:
        if state == self._state:
            return
        self._state = state
        log.debug("voice state -> %s", state.value)
        self._publish(f"{self._topic_prefix}/state", state.value, retain=True, qos=1)

    def _publish_levels(self, audio: np.ndarray, rate: int) -> None:
        """Push a coarse spectrum so the face's mouth tracks the real speech."""
        if self._mqtt is None or len(audio) == 0:
            return

        samples = audio.astype(np.float32) / 32768.0
        hop = max(1, int(rate * 0.05))  # 20 updates a second
        bands = 16

        def pump() -> None:
            for start in range(0, len(samples), hop):
                if self._stop.is_set():
                    return
                window = samples[start : start + hop]
                if len(window) < 8:
                    break
                spectrum = np.abs(np.fft.rfft(window * np.hanning(len(window))))
                # Log-spaced bins: speech energy is bunched at the low end, and
                # linear bins would leave most bars dead.
                edges = np.geomspace(1, len(spectrum) - 1, bands + 1).astype(int)
                levels = [
                    float(np.mean(spectrum[edges[i] : max(edges[i] + 1, edges[i + 1])]))
                    for i in range(bands)
                ]
                peak = max(levels) or 1.0
                self._publish(
                    f"{self._topic_prefix}/eq",
                    [round(min(1.0, v / peak), 3) for v in levels],
                )
                time.sleep(0.05)

        threading.Thread(target=pump, daemon=True).start()

    # -- the loop -----------------------------------------------------------

    def run(self) -> int:
        from .. import runtime

        # A voice session is here for the evening, so commands that start work
        # outliving the call can run here rather than being handed to the agent.
        runtime.mark_resident()

        voice = self.config.voice
        self._topic_prefix = self.config.assistant_topic_prefix()
        self._claim_topic = f"{self.config.face.jarvis_topic_prefix.rstrip('/')}/wake_owner"
        self._connect_mqtt()

        if voice.claim_wake_word:
            # Tells Jarvis on the Pi to stand down while this PC is listening.
            self._publish(self._claim_topic, self.config.device.id, retain=True, qos=1)
            log.info("claimed the wake word as %r", self.config.device.id)

        self._set_state(VoiceState.IDLE)
        log.info(
            "listening for %r on %s",
            voice.wake_word,
            voice.input_device or "the default microphone",
        )

        cooldown_until = 0.0
        try:
            with Microphone(self._input_device) as mic:
                for frame in mic.frames(timeout=1.0):
                    if self._stop.is_set():
                        break
                    if frame is None:
                        continue

                    self.recorder.observe(frame)

                    if time.monotonic() < cooldown_until:
                        continue
                    if not self.detector.triggered(frame):
                        continue

                    log.info("wake word detected (score %.2f)", self.detector.last_score)
                    # Light the face up now, not after the acknowledgement.
                    self._set_state(VoiceState.LISTENING)
                    if self.ack.enabled:
                        # Say something before listening, so the pause that
                        # follows reads as attention rather than a dead mic.
                        self.ack.play()
                        # Our own voice is in the buffer; recording it would
                        # only give Whisper something extra to mis-hear.
                        mic.drain()
                    self._handle_utterance(mic)
                    self.detector.reset()
                    mic.drain()
                    cooldown_until = time.monotonic() + voice.wake_cooldown_seconds
        except AudioError as exc:
            log.error("%s", exc)
            return 1
        finally:
            self._shutdown()
        return 0

    def _handle_utterance(self, mic: Microphone) -> None:
        self._set_state(VoiceState.LISTENING)
        audio = self.recorder.record(mic, cancel=self._stop)

        if audio is None:
            log.info("no speech after the wake word")
            self._set_state(VoiceState.IDLE)
            return

        self._set_state(VoiceState.THINKING)
        started = time.time()
        text = self.transcriber.transcribe(audio)
        log.info(
            "heard %r  (%.1fs audio, %.2fs transcribe)",
            text, len(audio) / SAMPLE_RATE, time.time() - started,
        )

        if not text:
            self._set_state(VoiceState.IDLE)
            return

        try:
            reply = self.brain.ask(text)
        except BrainError as exc:
            reply = str(exc)
        except Exception:
            log.exception("brain failed")
            reply = "Something went wrong handling that."

        if not reply:
            reply = "I'm not sure how to help with that."

        self._speak(reply)
        self._set_state(VoiceState.IDLE)

    def _speak(self, text: str) -> None:
        log.info("saying %r", text[:120])
        self._set_state(VoiceState.SPEAKING)
        try:
            audio, rate = self.speaker.synthesize(text)
            self._publish_levels(audio, rate)
            import sounddevice as sd

            sd.play(audio, rate, device=self._output_device)
            sd.wait()
        except Exception as exc:
            log.error("could not speak: %s", exc)
        finally:
            self._publish(f"{self._topic_prefix}/eq", [0.0] * 16)

    def stop(self) -> None:
        self._stop.set()

    def _shutdown(self) -> None:
        self._set_state(VoiceState.IDLE)
        if self._mqtt is not None:
            if self.config.voice.claim_wake_word:
                # Release the claim so the Pi takes over again.
                self._publish(self._claim_topic, "", retain=True, qos=1)
                time.sleep(0.4)
            try:
                self._mqtt.loop_stop()
                self._mqtt.disconnect()
            except Exception:
                pass
        log.info("voice stopped")
