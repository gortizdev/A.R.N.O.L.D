"""The face's loopback feed: this PC's voice reaches its face without the broker."""

import threading
import time

from arnold.config import Config
from arnold.face.local import LocalFaceListener, LocalFaceSender, listeners_dir
from arnold.face.sources import FaceFeed
from arnold.face.state import FaceMode


def _config(tmp_path) -> Config:
    config = Config()
    config.state_file = str(tmp_path / "state.json")
    config.mqtt.enabled = False
    return config


def _wait(predicate, timeout=3.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_every_listener_gets_each_message(tmp_path):
    config = _config(tmp_path)
    got: list[list] = [[], []]
    listeners = [
        LocalFaceListener(config, lambda t, r, box=box: box.append((t, r))) for box in got
    ]
    for listener in listeners:
        assert listener.start()
    try:
        LocalFaceSender(config).send("arnold/hud/state", "speaking")
        assert _wait(lambda: all(got))
        assert got[0] == got[1] == [("arnold/hud/state", "speaking")]
    finally:
        for listener in listeners:
            listener.stop()
    assert not list(listeners_dir(config).glob("*.port"))


def test_sending_with_nobody_listening_is_harmless(tmp_path):
    LocalFaceSender(_config(tmp_path)).send("arnold/hud/eq", {"bands": [0.5]})


def test_the_face_reacts_without_the_broker(tmp_path):
    config = _config(tmp_path)
    feed = FaceFeed(config)
    feed._agent_running = True
    local_only = threading.Event()
    feed._poll_local = lambda: local_only.wait()  # no state file in this test
    feed.start()
    try:
        prefix = config.assistant_topic_prefix()
        LocalFaceSender(config).send(f"{prefix}/state", "speaking")
        assert _wait(lambda: feed.current()[0] is FaceMode.SPEAKING)
        # Another assistant's topic on the loopback is not ours to show.
        LocalFaceSender(config).send("jarvis/hud/state", "listening")
        time.sleep(0.2)
        assert feed.current()[0] is FaceMode.SPEAKING
    finally:
        local_only.set()
        feed.stop()
