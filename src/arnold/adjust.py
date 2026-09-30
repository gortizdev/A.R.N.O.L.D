"""Rewriting a designed part's OpenSCAD to make one change.

printer.make's code is written by whichever model the user was talking to,
and that model can edit its own code while the conversation lasts. This is
for everything after: the Workshop tab, which has no conversation, and a
part made yesterday. A small, quick model is enough - the change is one
sentence, the file is short, and OpenSCAD itself checks the answer. When it
does not render, the error goes back for another try.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

CHAT_URL = "https://api.openai.com/v1/chat/completions"

INSTRUCTIONS = (
    "You edit OpenSCAD parts for a 3D printer. Make the change asked for and "
    "nothing else: keep every other dimension, name and comment, keep units in "
    "millimetres and the part on z=0, and keep it printable - walls at least "
    "1.2 mm, about 0.2 mm clearance where parts fit together. Reply with the "
    "complete updated OpenSCAD file only: no markdown fences, no commentary."
)


class AdjustError(Exception):
    """The rewrite could not be had. The message is spoken."""


def rewrite(code: str, change: str, *, model: str, error: str = "", previous: str = "",
            timeout: float = 120.0) -> str:
    """The file with the change made. `error` and `previous` are the last
    attempt and what OpenSCAD said about it, when there was one."""
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise AdjustError("I need an OpenAI key to change the code, and OPENAI_API_KEY isn't set.")
    messages = [
        {"role": "system", "content": INSTRUCTIONS},
        {"role": "user", "content": f"Change: {change.strip()}\n\nCurrent file:\n{code}"},
    ]
    if error:
        messages += [
            {"role": "assistant", "content": previous},
            {"role": "user", "content": f"OpenSCAD could not use that: {error}\n"
                                        "Fix it and reply with the whole file again."},
        ]
    body: dict = {"model": model, "messages": messages}
    if model.startswith(("gpt-5", "o")):
        body["reasoning_effort"] = "low"
    request = urllib.request.Request(
        CHAT_URL, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        text = payload["choices"][0]["message"]["content"] or ""
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))["error"]["message"]
        except Exception:
            detail = f"error {exc.code}"
        raise AdjustError(f"The code model turned that down: {detail}") from None
    except (urllib.error.URLError, OSError, KeyError, IndexError, ValueError) as exc:
        raise AdjustError(f"I couldn't get the code changed: {exc}") from None
    code = unfence(text)
    if not code.strip():
        raise AdjustError("The code model sent back nothing.")
    return code


def unfence(text: str) -> str:
    fenced = re.search(r"```[\w-]*[ \t]*\n(.*?)\n?```", text, re.S)
    return (fenced.group(1) if fenced else text).strip() + "\n"
