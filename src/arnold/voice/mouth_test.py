"""Drive the face's mouth from a WAV file.

Tuning lip-sync by saying "hey jarvis" and hoping the reply contains the sound
you wanted to look at is miserable. This plays a file instead, publishing the
same messages a real conversation would, so the face on screen animates
exactly as it would in use and the constants in `arnold.mouth` can
be adjusted against the same three seconds of audio over and over.

The envelope is precomputed here in one pass, which the live path cannot do -
audio arrives from the model as a stream. The publishing, though, goes through
the same clock: each frame is sent when its audio is actually audible.
"""

from __future__ import annotations

import json
import logging
import time
import wave
from pathlib import Path

from ..config import Config
from ..mouth import TUNING, MouthTuning, envelope_from_wav

log = logging.getLogger(__name__)


def read_wav(path: Path):
    """Mono int16 samples and the sample rate."""
    import numpy as np

    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2:
            raise ValueError("only 16-bit WAV files are supported")
        rate = handle.getframerate()
        channels = handle.getnchannels()
        pcm = np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16)

    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1).astype(np.int16)
    return pcm, rate


def plot(frames: list[dict], width: int = 96) -> str:
    """An ASCII view of the envelope, so tuning does not need the face."""
    if not frames:
        return "(no frames)"
    ramp = " .:-=+*#@"
    step = max(1, len(frames) // width)
    sampled = frames[::step]
    rows = []
    for key, label in (("open", "open"), ("wide", "wide")):
        bar = "".join(ramp[min(len(ramp) - 1, int(f[key] * (len(ramp) - 0.01)))] for f in sampled)
        rows.append(f"{label}: {bar}")
    return "\n".join(rows)


def run(
    config: Config,
    wav_path: Path,
    *,
    play: bool = True,
    loop: int = 1,
    tuning: MouthTuning = TUNING,
) -> int:
    pcm, rate = read_wav(wav_path)
    duration = len(pcm) / rate
    frames = envelope_from_wav(pcm, rate, tuning)

    peak_open = max((f["open"] for f in frames), default=0.0)
    peak_wide = max((f["wide"] for f in frames), default=0.0)
    print(f"{wav_path.name}: {duration:.2f}s at {rate} Hz, {len(frames)} envelope frames")
    print(f"hop {tuning.hop_ms:.0f}ms | attack {tuning.attack_ms:.0f}ms | "
          f"release {tuning.release_ms:.0f}ms | floor {tuning.min_openness}")
    print(f"crossovers: low <{tuning.low_crossover_hz:.0f}Hz  high >{tuning.high_crossover_hz:.0f}Hz")
    print(f"peak open {peak_open:.2f}, peak wide {peak_wide:.2f}")
    print()
    print(plot(frames))
    print()

    client = _connect(config)
    prefix = config.assistant_topic_prefix()

    stream = None
    if play:
        try:
            import sounddevice as sd

            stream = sd.OutputStream(samplerate=rate, channels=1, dtype="int16")
            stream.start()
        except Exception as exc:
            log.warning("no audio output (%s); animating silently", exc)
            stream = None

    try:
        for pass_number in range(max(1, loop)):
            if loop > 1:
                print(f"pass {pass_number + 1} of {loop}")
            _publish(client, f"{prefix}/state", "speaking", retain=True)
            started = time.monotonic()

            if stream is not None:
                # Playback runs in the background so the publishing loop keeps
                # its own clock, exactly as the live pacer thread does.
                import threading

                threading.Thread(
                    target=stream.write, args=(pcm,), daemon=True, name="wav-play"
                ).start()

            for frame in frames:
                wait = started + frame["at"] - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                _publish(
                    client,
                    f"{prefix}/eq",
                    {"bands": frame["bands"], "open": frame["open"], "wide": frame["wide"]},
                )

            time.sleep(max(0.0, started + duration - time.monotonic()))
            _publish(client, f"{prefix}/state", "idle", retain=True)
            _publish(
                client,
                f"{prefix}/eq",
                {"bands": [0.0] * 16, "open": 0.0, "wide": tuning.rest_width},
            )
            if pass_number + 1 < loop:
                time.sleep(0.8)
    except KeyboardInterrupt:
        _publish(client, f"{prefix}/state", "idle", retain=True)
    finally:
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        if client is not None:
            client.loop_stop()
            client.disconnect()

    print("done")
    return 0


def _connect(config: Config):
    if not config.mqtt.enabled:
        log.warning("mqtt is disabled; the face will not see this")
        return None
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        log.warning("paho-mqtt is not installed; the face will not see this")
        return None

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ca-mouth-test")
    if config.mqtt.username:
        client.username_pw_set(config.mqtt.username, config.mqtt.password)
    try:
        client.connect(config.mqtt.host, config.mqtt.port, config.mqtt.keepalive)
        client.loop_start()
    except Exception as exc:
        log.warning("could not reach the broker: %s", exc)
        return None
    return client


def _publish(client, topic: str, payload, retain: bool = False) -> None:
    if client is None:
        return
    if not isinstance(payload, str):
        payload = json.dumps(payload)
    try:
        client.publish(topic, payload, qos=0, retain=retain)
    except Exception:
        pass
