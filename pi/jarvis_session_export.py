"""Export Jarvis's effective realtime settings as JSON.

The PC opens its own Realtime session so it can answer with the same voice and
manner as the Pi. Hard-coding the model, voice and prompt on the PC would work
until the day one of them is changed on the Pi and the two quietly diverge -
so the PC asks for them instead, over the SSH access it already has:

    ssh parzival@192.168.1.171 \\
        "cd ~/voiceassistant && python3 jarvis_session_export.py"

Deliberately does NOT import assistant.py: that module is a 6,800-line script
whose import has side effects (audio devices, threads, API prewarming). The
system prompt comes from config.py, and the accent addendum is lifted out of
the source text with ast.literal_eval - no execution.
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def extract_addendum() -> str:
    """Pull REALTIME_INSTRUCTIONS_ADDENDUM out of assistant.py without running it."""
    source = (HERE / "assistant.py").read_text(encoding="utf-8")
    match = re.search(
        r"^REALTIME_INSTRUCTIONS_ADDENDUM\s*=\s*\((.*?)^\)\s*$",
        source,
        re.S | re.M,
    )
    if not match:
        return ""
    try:
        # The value is a run of adjacent string literals; literal_eval joins
        # them without executing anything.
        return ast.literal_eval("(" + match.group(1) + ")")
    except (ValueError, SyntaxError):
        return ""


def main() -> int:
    try:
        from config import load_config
    except Exception as exc:
        print(json.dumps({"error": f"could not load config: {exc}"}))
        return 1

    cfg = load_config()
    conv = cfg.conversation

    payload = {
        "model": getattr(conv, "realtime_model", "gpt-realtime-2"),
        "voice": getattr(conv, "realtime_voice", "cedar"),
        "realtime_enabled": bool(getattr(conv, "realtime_enabled", False)),
        "instructions": (getattr(conv, "system_prompt", "") or "") + extract_addendum(),
        # The legacy pipeline's voice, so the PC's non-realtime fallback
        # matches what the Pi would have used in the same situation.
        "tts_model": getattr(cfg.tts, "model", "gpt-4o-mini-tts"),
        "tts_voice": getattr(cfg.tts, "voice", "fable"),
        "turn_detection": {
            "type": "server_vad",
            "threshold": 0.5,
            "prefix_padding_ms": 300,
            "silence_duration_ms": 700,
        },
        "transcription_model": "gpt-4o-mini-transcribe",
        "has_api_key": bool(os.getenv("OPENAI_API_KEY")),
    }
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
