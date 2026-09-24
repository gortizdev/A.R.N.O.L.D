"""The log must not go quiet at the size cap because another process has
the file open, which on Windows makes the rotation's rename fail."""

from __future__ import annotations

import logging
import os

import pytest

from arnold.logging_setup import TolerantRotatingFileHandler


@pytest.mark.skipif(os.name != "nt", reason="only Windows refuses to rename an open file")
def test_keeps_writing_when_it_cannot_rotate(tmp_path):
    path = tmp_path / "agent.log"
    handler = TolerantRotatingFileHandler(path, maxBytes=200, backupCount=2, encoding="utf-8")
    logger = logging.getLogger("test.rotation")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        # Another process's handle, from the log's point of view.
        with open(path, "a", encoding="utf-8"):
            for n in range(20):
                logger.info("line %d %s", n, "x" * 40)
        handler.flush()
    finally:
        logger.removeHandler(handler)
        handler.close()
    text = path.read_text(encoding="utf-8")
    assert "line 19" in text  # past the cap, and still there
    assert handler._rotate_failed_at > 0


def test_rotates_when_it_can(tmp_path):
    path = tmp_path / "agent.log"
    handler = TolerantRotatingFileHandler(path, maxBytes=200, backupCount=2, encoding="utf-8")
    logger = logging.getLogger("test.rotation.ok")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        for n in range(20):
            logger.info("line %d %s", n, "x" * 40)
    finally:
        logger.removeHandler(handler)
        handler.close()
    assert (tmp_path / "agent.log.1").exists()
    assert handler._rotate_failed_at == 0.0
