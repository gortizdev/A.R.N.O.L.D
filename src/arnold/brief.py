"""Turning a short request into a precise one, by asking.

"An owl" is enough to sculpt from, but the image model then decides
everything the words left open - cartoon or realistic, perched or flying,
on a base or not - and a second go decides it all differently. A few
questions first pin those down. Each round sends the request and every
answer so far; what comes back is the description as it now stands, and
the questions still worth asking. When nothing important is left open, no
questions come back, and a request that was specific to begin with is
never asked any.

Only shape is asked about. The part is drawn as grey clay and printed in
one colour, so colour and material are questions whose answers go nowhere.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

CHAT_URL = "https://api.openai.com/v1/chat/completions"
MAX_QUESTIONS = 4

INSTRUCTIONS = (
    "You help someone commission a small 3D-printed sculpture. It will be drawn as "
    "a matte grey clay model from the front, side and back, turned into a mesh and "
    "printed in one colour, so only its SHAPE matters. From their request and the "
    "answers so far, reply with JSON only:\n"
    '{"title": "...", "description": "...", "height_mm": null, '
    '"questions": [{"question": "...", "options": ["...", "..."]}]}\n'
    "- description: one to three sentences saying exactly what it looks like - the "
    "subject, pose, proportions, style, distinctive features and what it stands on. "
    "Include every answer given. Describe; do not mention printing, clay or views.\n"
    "- title: two or three words naming it.\n"
    "- height_mm: its height in millimetres if a size was given, otherwise null.\n"
    "- questions: up to {n} short questions about whatever is still open that "
    "would change the shape most - style, pose, expression, key features, "
    "accessories, a base or stand, size. Never ask about colour, paint or "
    "material, and never ask again what has been answered. If it is a working "
    "object - a box, pouch, case, holder, anything that opens, holds or fits - ask "
    "instead about what it must hold, its size, how it opens and closes, and how it "
    "attaches, and put those sizes and mechanisms in the description. Each question has two "
    "to four options of a few words each, the likeliest first. If the description "
    "is already specific enough to sculpt, return no questions."
)


class BriefError(Exception):
    """The questions could not be had. The message is spoken."""


@dataclass(slots=True)
class Brief:
    title: str
    description: str
    height_mm: float | None = None
    questions: list[dict[str, Any]] = field(default_factory=list)


def ask(request: str, answers: list[dict[str, str]] | None = None, *, model: str,
        questions: int = MAX_QUESTIONS, timeout: float = 60.0) -> Brief:
    """The description so far and what is still worth asking. `answers` is
    [{"question": ..., "answer": ...}] from earlier rounds; questions=0 asks
    for the final description only."""
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise BriefError("I need an OpenAI key to ask about it, and OPENAI_API_KEY isn't set.")
    lines = [f"Request: {request.strip()}"]
    answered = [a for a in answers or [] if str(a.get("answer") or "").strip()]
    if answered:
        lines.append("Answers so far:")
        lines += [f"- {a.get('question', '').strip()} {str(a['answer']).strip()}" for a in answered]
    if questions <= 0:
        lines.append("Ask nothing more: return no questions.")
    body: dict[str, Any] = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": INSTRUCTIONS.replace("{n}", str(max(questions, 1)))},
            {"role": "user", "content": "\n".join(lines)},
        ],
    }
    if model.startswith(("gpt-5", "o")):
        body["reasoning_effort"] = "low"
    http = urllib.request.Request(
        CHAT_URL, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(http, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        reply = json.loads(payload["choices"][0]["message"]["content"] or "{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))["error"]["message"]
        except Exception:
            detail = f"error {exc.code}"
        raise BriefError(f"The model turned that down: {detail}") from None
    except (urllib.error.URLError, OSError, KeyError, IndexError, ValueError) as exc:
        raise BriefError(f"I couldn't think of questions to ask: {exc}") from None
    return read(reply, request, answered, questions)


def read(reply: Any, request: str, answered: list[dict[str, str]], limit: int) -> Brief:
    """The model's JSON, held to what was asked for: anything malformed is
    dropped rather than shown, and a question already answered is not
    asked twice."""
    if not isinstance(reply, dict):
        reply = {}
    description = str(reply.get("description") or "").strip() or request.strip()
    title = str(reply.get("title") or "").strip()[:60]
    try:
        height = float(reply.get("height_mm")) if reply.get("height_mm") not in (None, "") else None
    except (TypeError, ValueError):
        height = None
    if height is not None and not 5 <= height <= 1000:
        height = None
    seen = {str(a.get("question") or "").strip().lower() for a in answered}
    questions = []
    for item in reply.get("questions") or []:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        if not question or question.lower() in seen:
            continue
        options = [str(o).strip()[:60] for o in item.get("options") or [] if str(o).strip()][:4]
        questions.append({"question": question[:200], "options": options})
        seen.add(question.lower())
    return Brief(title=title, description=description, height_mm=height,
                 questions=questions[:max(limit, 0)])
