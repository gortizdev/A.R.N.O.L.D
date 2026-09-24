"""The face's feed from this PC's own voice, without the broker.

The broker lives on the Pi, so when the Pi is down every face message the
voice session sends goes nowhere - and the face sitting a few centimetres
away on the same desktop stops reacting. This carries the same messages over
loopback UDP instead, alongside MQTT.

More than one feed listens (the face window, and the dashboard inside the
agent), and a UDP port has one owner, so each listener binds a port of its
own and leaves it in a small directory beside the state file. Senders read
that directory every couple of seconds and send each datagram to every port
in it. A port whose listener has gone just drops what it is sent.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
# How often a sender looks for listeners that came or went.
RESCAN_SECONDS = 2.0
MAX_DATAGRAM = 60_000


def listeners_dir(config) -> Path:
    state = config.state_file or "logs/state.json"
    return Path(state).with_name("face-listeners")


def _pid_alive(pid: int) -> bool:
    try:
        import psutil

        return psutil.pid_exists(pid)
    except Exception:
        return True


class LocalFaceSender:
    """Sends face messages to every listening feed on this machine."""

    def __init__(self, config) -> None:
        self._dir = listeners_dir(config)
        self._sock: socket.socket | None = None
        self._ports: list[int] = []
        self._scanned = 0.0
        self._lock = threading.Lock()

    def send(self, topic: str, payload: Any) -> None:
        """Best effort, never raises: the face is decoration, not plumbing."""
        try:
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8", "replace")
            data = json.dumps({"t": topic, "p": payload}).encode("utf-8")
            if len(data) > MAX_DATAGRAM:
                return
            with self._lock:
                ports = self._current_ports()
                if not ports:
                    return
                if self._sock is None:
                    self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    self._sock.setblocking(False)
                for port in ports:
                    try:
                        self._sock.sendto(data, (HOST, port))
                    except OSError:
                        pass
        except Exception as exc:
            log.debug("local face send failed: %s", exc)

    def _current_ports(self) -> list[int]:
        now = time.monotonic()
        if now - self._scanned < RESCAN_SECONDS:
            return self._ports
        self._scanned = now
        ports = []
        try:
            for entry in self._dir.glob("*.port"):
                try:
                    ports.append(int(entry.read_text().strip()))
                except (OSError, ValueError):
                    continue
        except OSError:
            pass
        self._ports = ports
        return ports

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                self._sock.close()
                self._sock = None


class LocalFaceListener:
    """Receives face messages and hands each (topic, raw payload) on."""

    def __init__(self, config, handle: Callable[[str, str], None]) -> None:
        self._dir = listeners_dir(config)
        self._handle = handle
        self._sock: socket.socket | None = None
        self._file: Path | None = None
        self._stop = threading.Event()

    def start(self) -> bool:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind((HOST, 0))
            sock.settimeout(1.0)
            port = sock.getsockname()[1]
            self._dir.mkdir(parents=True, exist_ok=True)
            self._prune()
            self._file = self._dir / f"{os.getpid()}-{port}.port"
            self._file.write_text(str(port))
        except OSError as exc:
            log.warning("face has no local feed: %s", exc)
            return False
        self._sock = sock
        threading.Thread(target=self._run, name="face-udp", daemon=True).start()
        log.info("face listening locally on udp %s:%d", HOST, port)
        return True

    def _prune(self) -> None:
        """Forget listeners whose process has gone."""
        for entry in self._dir.glob("*.port"):
            try:
                pid = int(entry.name.split("-", 1)[0])
            except ValueError:
                continue
            if pid != os.getpid() and not _pid_alive(pid):
                try:
                    entry.unlink()
                except OSError:
                    pass

    def _run(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                data, _ = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                # Windows reports an earlier send to a closed port here; it
                # says nothing about this socket.
                if self._stop.is_set():
                    break
                continue
            try:
                message = json.loads(data.decode("utf-8"))
                topic, payload = str(message["t"]), message["p"]
            except (ValueError, KeyError, TypeError):
                continue
            raw = payload if isinstance(payload, str) else json.dumps(payload)
            try:
                self._handle(topic, raw)
            except Exception as exc:
                log.debug("local face message dropped: %s", exc)

    def stop(self) -> None:
        self._stop.set()
        if self._file is not None:
            try:
                self._file.unlink()
            except OSError:
                pass
        if self._sock is not None:
            self._sock.close()
