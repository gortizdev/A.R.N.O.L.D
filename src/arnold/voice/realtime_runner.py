"""Wake word on the PC, conversation over the Realtime API.

The microphone is opened once at 48 kHz and decimated per consumer - by 3 for
the wake-word model's 16 kHz, by 2 for the session's 24 kHz. One stream avoids
the device contention and reopen latency of switching rates mid-conversation,
and matches how Jarvis drives its own mic (assistant.py: "Mic rate: 48000 Hz |
Wake rate: 16000 Hz").

While a conversation is live this claims the wake word over MQTT so the Pi
stands down; the claim is retained and cleared by the agent's last-will, so a
sleeping or unplugged PC hands control back automatically.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import queue
import threading
import time

import numpy as np

from ..alerts import AlertEngine
from ..commands import CommandContext
from ..commands.clock import now_sentence
from ..config import Config, IdentityWatcher
from ..face.local import LocalFaceSender
from ..jarvis import JarvisClient
from ..monitors.collector import Collector
from .. import memory, process
from ..speech import set_conversation_live
from .audio import AudioError, WakeWordDetector, resolve_device
from .realtime import RealtimeConversation, TurnTaking
from .session_config import load_session_config
from .tools import ToolDispatcher, build_tools
from .wake_ack import WakeAck

log = logging.getLogger(__name__)

CAPTURE_RATE = 48000
WAKE_RATE = 16000
# 80 ms at 48 kHz; decimates to exactly 1280 samples at 16 kHz, which is the
# frame size openWakeWord expects.
CAPTURE_FRAME = 3840

# Where the claim lives on the Pi; pc_agent_tool.wake_claimed() reads it.
PI_CLAIM_FILE = "/run/user/1000/pc_wake_claim"


def decimate_to_16k(pcm: np.ndarray) -> np.ndarray:
    """48k -> 16k by averaging each run of three samples."""
    n = len(pcm) - (len(pcm) % 3)
    x = pcm[:n].astype(np.int32).reshape(-1, 3)
    return (x.sum(axis=1) // 3).astype(np.int16)


class RealtimeVoiceAssistant:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._stop = threading.Event()
        self._mqtt = None

        self.api_key = os.environ.get("OPENAI_API_KEY", "")
        if not self.api_key:
            raise RealtimeSetupError(
                "OPENAI_API_KEY is not set. It can be copied from the Pi's "
                "voiceassistant/.env, or set with:\n"
                "  [Environment]::SetEnvironmentVariable('OPENAI_API_KEY','sk-...','User')"
            )

        self.input_device = resolve_device(config.voice.input_device, input=True)
        self.output_device = resolve_device(config.voice.output_device, input=False)

        log.info("loading wake word %r", config.voice.wake_word)
        self.detector = self._build_detector()

        # Fetched from the Pi so both machines share one personality.
        self.session = load_session_config(config)

        # Rendered in the background: the first wake word gets the chime if it
        # arrives before the phrases do, rather than waiting on the network.
        # Spoken in the session's own voice, so the "Yes?" and the answer that
        # follows it are not two different people.
        self.ack = WakeAck(config, self.output_device, tts_voice=self.session.voice)
        self.ack.prepare()

        context = CommandContext(
            config=config,
            collector=Collector(config.monitors, notifications=config.notifications, claude=config.claude),
            alerts=AlertEngine(config.alerts, config.device.friendly_name),
            jarvis=JarvisClient(config.jarvis),
        )
        self.tools = build_tools(config)
        self.dispatch = ToolDispatcher(config, context)
        log.info("%d tools available to the session", len(self.tools))

        self._frames: queue.Queue = queue.Queue(maxsize=120)
        self._in_conversation = threading.Event()
        # What has been said in the conversation currently running; flushed to
        # the conversation log when it ends.
        self._turns: list[dict[str, str]] = []

        # A profile switch edits config.yaml under us; between conversations
        # the wake loop asks whether the identity moved and rebuilds.
        self._identity = config.identity_key()
        self._watcher = IdentityWatcher(config)
        self._prefix = config.assistant_topic_prefix()
        self._local_face = LocalFaceSender(config)

    def _local_prefix(self) -> str:
        return self._prefix + "/"

    # -- identity -----------------------------------------------------------

    def _build_detector(self) -> WakeWordDetector:
        voice = self.config.voice
        return WakeWordDetector(
            voice.wake_word,
            voice.wake_threshold,
            patience=voice.wake_patience,
            vad_threshold=voice.wake_vad_threshold,
            wake_dir=voice.wake_word_dir,
        )

    def _identity_moved(self) -> Config | None:
        """Has the identity changed since the runner last rebuilt itself?

        Two ways it can: the file changed underneath us (another process,
        the CLI, Jarvis over SSH), or the `switch_profile` tool applied the
        profile to *this* config in place, in which case the file matches
        what we already hold and the watcher alone would see nothing.
        Returns the fresh Config to adopt, our own if the change is already
        in it, or None.
        """
        fresh = self._watcher.changed()
        if fresh is not None:
            return fresh
        if self.config.identity_key() != self._identity:
            return self.config
        return None

    def _reload_identity(self, fresh: Config) -> None:
        """Become whoever the config file now says, without a restart.

        Only what changed is rebuilt. The wake word model is swapped first,
        because that is what the next conversation is gated on; a model that
        will not load keeps the old one rather than killing the process over
        a typo. The session refresh may reach the Pi over SSH and take a few
        seconds - fine here, on a switch, between conversations.
        """
        wake_before = self._identity[5:7] if self._identity else None
        old_prefix = self._prefix
        if fresh is not self.config:
            self.config.adopt_identity(fresh)
        if self.config.identity_key() == self._identity:
            return

        wake_now = (self.config.voice.wake_word, self.config.voice.wake_word_dir)
        if wake_now != wake_before:
            try:
                self.detector = self._build_detector()
                log.info("now listening for %r", self.config.voice.wake_word)
            except Exception as exc:  # AudioError, or onnxruntime's own
                log.error("keeping the old wake word %r: %s", wake_before and wake_before[0], exc)
                if wake_before is not None:
                    self.config.voice.wake_word, self.config.voice.wake_word_dir = wake_before

        try:
            self.session = load_session_config(self.config)
        except Exception as exc:  # the Pi being odd must not stop the switch
            log.warning("could not refresh the session after the switch: %s", exc)
        try:
            self.ack = WakeAck(self.config, self.output_device, tts_voice=self.session.voice)
            self.ack.prepare()
        except Exception as exc:
            log.warning("keeping the old acknowledgement: %s", exc)
        self.tools = build_tools(self.config)

        prefix = self.config.assistant_topic_prefix()
        if prefix != old_prefix:
            # Leave the old face topic idle and light up the new one.
            self._on_state("idle")
            self._prefix = prefix
            self._on_state("idle")

        # After any revert above, so what we record is what we listen for.
        self._identity = self.config.identity_key()
        log.info(
            "%s listening for %r (voice=%s, profile=%s)",
            self.session.name,
            self.config.voice.wake_word,
            self.session.voice,
            self.config.active_profile or "-",
        )

    # -- mqtt state ---------------------------------------------------------

    def _connect_mqtt(self) -> None:
        if not self.config.mqtt.enabled:
            return
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            return
        cfg = self.config.mqtt
        # Our own state goes on our own prefix; the wake claim stays on
        # Jarvis's, because that is where the Pi looks for it.
        self._prefix = self.config.assistant_topic_prefix()
        self._claim_topic = f"{self.config.face.jarvis_topic_prefix.rstrip('/')}/wake_owner"

        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=f"{cfg.client_id}-rtvoice"
        )
        if cfg.username:
            client.username_pw_set(cfg.username, cfg.password)
        if self.config.voice.claim_wake_word:
            client.will_set(self._claim_topic, "", qos=1, retain=True)
        client.reconnect_delay_set(min_delay=2, max_delay=60)
        try:
            client.connect_async(cfg.host, cfg.port, cfg.keepalive)
            client.loop_start()
            self._mqtt = client
        except Exception as exc:
            log.warning("voice could not reach the broker: %s", exc)

    # -- wake-word claim on the Pi -----------------------------------------

    def _claim_ssh(self, command: str) -> bool:
        ssh = self.config.jarvis.ssh
        argv = [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=accept-new",
        ]
        if ssh.port != 22:
            argv += ["-p", str(ssh.port)]
        if ssh.key_path:
            argv += ["-i", ssh.key_path]
        argv += [f"{ssh.user}@{ssh.host}", command]
        try:
            return process.run(argv, timeout=12).returncode == 0
        except Exception as exc:
            log.debug("claim ssh failed: %s", exc)
            return False

    def _claim_heartbeat(self) -> None:
        """Hold the wake-word claim on the Pi, refreshing until we stop.

        Jarvis treats a claim older than 90s as expired, so if this PC sleeps or
        dies the Pi resumes answering by itself - no cleanup required, and no
        way to leave Jarvis permanently muted.
        """
        device = self.config.device.id
        command = f"mkdir -p /run/user/1000 && printf '%s' '{device}' > {PI_CLAIM_FILE}"
        while not self._stop.is_set():
            if not self._claim_ssh(command):
                log.debug("could not refresh the wake claim on the Pi")
            # Comfortably inside the 90s expiry, with room for one failure.
            self._stop.wait(35.0)

    def _release_claim(self) -> None:
        if self._claim_ssh(f"rm -f {PI_CLAIM_FILE}"):
            log.info("released the wake-word claim; the Pi is listening again")

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

    def _on_state(self, state: str) -> None:
        self._publish(f"{self._prefix}/state", state, retain=True, qos=1)
        # Refresh the marker as the conversation goes, not once when it opens:
        # it is read with a staleness rule, so a conversation that outlives
        # that window would otherwise read as finished and be talked over.
        if self._in_conversation.is_set():
            set_conversation_live(self.config, True)

    def _on_levels(self, levels: list[float]) -> None:
        self._publish(f"{self._prefix}/eq", levels)

    def _on_transcript(self, who: str, text: str) -> None:
        self._publish(f"{self._prefix}/transcript", {"who": who, "text": text})
        self._turns.append({"who": who, "text": text})

    # -- memory -------------------------------------------------------------

    def _remembering_session(self):
        """The session prompt with what this machine already knows folded in.

        Built per conversation rather than once at startup: facts stored during
        one exchange have to be there for the next wake word, and a session
        that began at boot would still be reciting yesterday's.
        """
        block = ""
        try:
            block = memory.instruction_block(self.config)
        except Exception as exc:  # memory must never cost us a conversation
            log.warning("could not load memory: %s", exc)
        # The model has no clock. This covers "morning!" and "happy Friday";
        # anything that needs the exact time still asks clock.now.
        if block:
            log.info("carrying %d characters of memory into the session", len(block))
        clock = (
            f"\n\nAs this conversation opens it is {now_sentence()}. For the time "
            "later on, use clock.now rather than counting from this."
        )
        return dataclasses.replace(
            self.session, instructions=self.session.instructions + clock + block
        )

    def _keep_conversation(self) -> None:
        """Save what was just said, so the next wake word is not a cold start."""
        turns, self._turns = self._turns, []
        if not turns or not self.config.memory.enabled:
            return
        if not self.config.memory.carry_conversation:
            return
        try:
            memory.conversations_for(self.config).append(turns)
        except Exception as exc:
            log.warning("could not record the conversation: %s", exc)

    # -- audio --------------------------------------------------------------

    def _mic_callback(self, indata, frames, time_info, status) -> None:
        try:
            self._frames.put_nowait(indata[:, 0].copy())
        except queue.Full:
            try:
                self._frames.get_nowait()
                self._frames.put_nowait(indata[:, 0].copy())
            except queue.Empty:
                pass

    def _drain_frames(self) -> None:
        """Throw away buffered audio - our own voice, or a stale wake word."""
        while True:
            try:
                self._frames.get_nowait()
            except queue.Empty:
                return

    def _session_frames(self):
        """Frames for the live session, at the capture rate."""
        while not self._stop.is_set() and self._in_conversation.is_set():
            try:
                yield self._frames.get(timeout=0.3)
            except queue.Empty:
                yield None

    # -- main loop ----------------------------------------------------------

    def run(self) -> int:
        import sounddevice as sd

        from .. import runtime

        # A voice session is here for the evening, so commands that start work
        # outliving the call can run here rather than being handed to the agent.
        runtime.mark_resident()

        # Our own state goes on our own prefix; the wake claim stays on
        # Jarvis's, because that is where the Pi looks for it.
        self._prefix = self.config.assistant_topic_prefix()
        self._claim_topic = f"{self.config.face.jarvis_topic_prefix.rstrip('/')}/wake_owner"
        self._connect_mqtt()

        if self.config.voice.claim_wake_word:
            self._publish(self._claim_topic, self.config.device.id, retain=True, qos=1)
            # Jarvis has no MQTT client, so the claim it actually reads is a
            # file on the Pi, refreshed by this thread.
            threading.Thread(
                target=self._claim_heartbeat, daemon=True, name="wake-claim"
            ).start()
            log.info("claiming the wake word as %r; the Pi will defer", self.config.device.id)
        self._on_state("idle")

        log.info(
            "%s listening for %r at %.2f, %d frame(s) in a row, vad %.2f (%s, voice=%s, from %s)",
            self.session.name,
            self.config.voice.wake_word,
            self.detector.threshold,
            self.detector.patience,
            self.detector.vad_threshold,
            self.session.model,
            self.session.voice,
            self.session.source,
        )

        try:
            stream = sd.InputStream(
                samplerate=CAPTURE_RATE,
                blocksize=CAPTURE_FRAME,
                channels=1,
                dtype="int16",
                device=self.input_device,
                callback=self._mic_callback,
            )
        except Exception as exc:
            raise AudioError(f"could not open the microphone: {exc}") from exc

        cooldown_until = 0.0
        with stream:
            while not self._stop.is_set():
                # Between conversations only, so a switch made mid-call lands
                # the moment it ends. Checked on the silent branch too: a
                # quiet room should not delay it.
                fresh = self._identity_moved()
                if fresh is not None:
                    try:
                        self._reload_identity(fresh)
                    except Exception:  # a switch must never take the voice down
                        log.exception("could not switch identity; carrying on as before")
                        self._identity = self.config.identity_key()
                    self._drain_frames()

                try:
                    frame = self._frames.get(timeout=0.5)
                except queue.Empty:
                    continue

                if time.monotonic() < cooldown_until:
                    continue

                wake_frame = decimate_to_16k(frame).astype(np.float32) / 32768.0
                if not self.detector.triggered(wake_frame):
                    continue

                log.info("wake word detected (score %.2f)", self.detector.last_score)
                # The face lights up on this beat, not when the session opens
                # a second or two later: the gap between hearing the wake word
                # and being ready to talk is exactly when you want to know it
                # heard you.
                self._on_state("listening")
                # Answer before the session is even dialled, so there is no
                # silent gap in which you wonder whether it heard you.
                if self.ack.enabled:
                    self.ack.play()
                    # The acknowledgement is in the mic buffer now; sending it
                    # up would have the model reply to its own voice.
                    self._drain_frames()
                self._converse()
                self.detector.reset()
                # Drain whatever accumulated during the conversation, or the
                # wake detector immediately re-triggers on stale audio.
                self._drain_frames()
                cooldown_until = time.monotonic() + self.config.voice.wake_cooldown_seconds

        self._shutdown()
        return 0

    def _converse(self) -> None:
        conversation = RealtimeConversation(
            self.api_key,
            self._remembering_session(),
            self.tools,
            self.dispatch,
            output_device=self.output_device,
            on_state=self._on_state,
            on_levels=self._on_levels,
            on_transcript=self._on_transcript,
            turn=TurnTaking.from_config(self.config.voice),
        )
        self._in_conversation.set()
        # The agent process holds its own alerts and reminders while this is
        # set, rather than talking over the conversation. A file rather than
        # MQTT, because the broker is on the Pi and may well be unreachable.
        set_conversation_live(self.config, True)
        try:
            opened = conversation.run(self._session_frames(), CAPTURE_RATE)
            if not opened:
                log.warning("realtime session could not be opened")
                self._on_state("idle")
        finally:
            self._in_conversation.clear()
            set_conversation_live(self.config, False)
            conversation.stop()
            self._keep_conversation()

    def stop(self) -> None:
        self._stop.set()
        self._in_conversation.clear()

    def _shutdown(self) -> None:
        self._on_state("idle")
        # Leaving this set would mute the agent until the marker went stale.
        set_conversation_live(self.config, False)
        if self.config.voice.claim_wake_word:
            self._release_claim()
        if self._mqtt is not None:
            if self.config.voice.claim_wake_word:
                self._publish(self._claim_topic, "", retain=True, qos=1)
                time.sleep(0.4)
            try:
                self._mqtt.loop_stop()
                self._mqtt.disconnect()
            except Exception:
                pass
        log.info("voice stopped")


class RealtimeSetupError(RuntimeError):
    pass
