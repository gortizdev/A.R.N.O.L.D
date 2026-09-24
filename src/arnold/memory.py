"""What the assistant remembers between conversations.

A realtime session closes after twenty-five seconds of silence, so without
this the assistant is introduced to the user afresh every time it is woken.
Two different things are kept, because they decay at different rates:

* **Facts** - things worth knowing next week. The user's name, which drive
  the games are on, how they take their coffee. Written deliberately, either
  because they said "remember that" or because the assistant judged it
  durable. Kept until forgotten.
* **Conversations** - the tail of what was just said. Worth carrying across
  a wake-word gap so "do that again" still means something a minute later,
  and worthless a day after, so it is only injected while it is fresh.

Both live in flat files rather than a database because two processes touch
them: the resident agent and the one-shot `exec` calls Jarvis makes over SSH.
Every read reloads from disk and every write is atomic, so the loser of a race
loses one fact rather than the file.

Nothing here is encrypted. It is a plain-text record of things said out loud
in the user's own house, on their own machine - but it is worth knowing that
is what it is, and `memory.forget` exists because of it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

DEFAULT_MEMORY_FILE = "logs/memory.json"
DEFAULT_CONVERSATION_FILE = "logs/conversations.jsonl"

_WORD_RE = re.compile(r"[a-z0-9']+")

# Dropped before matching. Without this "what do you know about my car" scores
# every fact that happens to contain "my".
_STOPWORDS = frozenset(
    """a an and are as at be been but by do does for from had has have he her
    his how i if in is it its me my of on or our she that the their them then
    there these they this to was we were what when where which who will with
    you your am not about into over under just very can could would should here
    im ive id ill dont doesnt isnt wasnt cant wont thats theres youre lets""".split()
)


def _tokens(text: str) -> set[str]:
    """Meaningful words, singularised crudely so 'dogs' matches 'dog'.

    Apostrophes go first, so "I'm allergic" and "I am allergic" reduce to the
    same thing - people rephrase themselves constantly when speaking, and two
    copies of one fact is the failure this is here to prevent. Single
    characters are kept: "drive C" and "monitor 2" are the whole point of the
    sentences they appear in.
    """
    words = set()
    for word in _WORD_RE.findall(text.lower().replace("'", "")):
        if word in _STOPWORDS:
            continue
        words.add(word[:-1] if len(word) > 3 and word.endswith("s") else word)
    return words


# -- facts -------------------------------------------------------------------


@dataclass
class Fact:
    text: str
    tags: list[str] = field(default_factory=list)
    source: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    uses: int = 0

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "tags": self.tags,
            "age": ago(self.created),
            "created": self.created,
        }


class MemoryStore:
    """Facts on disk, searched by word overlap.

    Word overlap rather than embeddings on purpose: a few hundred sentences
    written by one person do not need a vector index, and this has to answer
    inside a one-shot process that starts, prints and exits.
    """

    def __init__(self, path: Path | str, max_facts: int = 500) -> None:
        self.path = Path(path)
        self.max_facts = max(1, int(max_facts))

    # -- storage ------------------------------------------------------------

    def facts(self) -> list[Fact]:
        """Everything on disk, newest first. Never raises."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        entries = raw.get("facts") if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            return []

        facts: list[Fact] = []
        for entry in entries:
            if not isinstance(entry, dict) or not str(entry.get("text") or "").strip():
                continue
            try:
                facts.append(
                    Fact(
                        text=str(entry["text"]).strip(),
                        tags=[str(t) for t in entry.get("tags") or []],
                        source=str(entry.get("source") or ""),
                        id=str(entry.get("id") or uuid.uuid4().hex[:8]),
                        created=float(entry.get("created") or 0.0),
                        updated=float(entry.get("updated") or entry.get("created") or 0.0),
                        uses=int(entry.get("uses") or 0),
                    )
                )
            except (TypeError, ValueError):
                continue
        facts.sort(key=lambda f: f.updated, reverse=True)
        return facts

    def _write(self, facts: list[Fact]) -> None:
        facts = sorted(facts, key=lambda f: f.updated, reverse=True)[: self.max_facts]
        payload = json.dumps({"facts": [asdict(f) for f in facts]}, indent=1)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".memory-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                os.replace(tmp, self.path)  # atomic on Windows and POSIX alike
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError as exc:
            log.warning("could not write %s: %s", self.path, exc)

    # -- writing ------------------------------------------------------------

    def remember(
        self, text: str, tags: Iterable[str] = (), source: str = ""
    ) -> tuple[Fact, bool]:
        """Store a fact. Returns it and whether it was new.

        The same thing said twice - "remember I'm allergic to walnuts", a week
        apart - is one fact with a fresher timestamp, not two. Matching on the
        word set rather than the string means the rephrasing that comes of
        speaking, not typing, still collapses.
        """
        text = " ".join(str(text or "").split())
        if not text:
            raise ValueError("nothing to remember")

        clean_tags = sorted({str(t).strip().lower() for t in tags if str(t).strip()})
        facts = self.facts()
        words = _tokens(text)

        for existing in facts:
            if _tokens(existing.text) == words and words:
                existing.text = text
                existing.tags = sorted(set(existing.tags) | set(clean_tags))
                existing.updated = time.time()
                self._write(facts)
                return existing, False

        fact = Fact(text=text, tags=clean_tags, source=source)
        facts.append(fact)
        self._write(facts)
        return fact, True

    def forget(self, query: str = "", fact_id: str = "") -> list[Fact]:
        """Drop facts by id, or the best match for a query. Returns what went."""
        facts = self.facts()
        if fact_id:
            doomed = [f for f in facts if f.id == fact_id.strip().lower()]
        elif query.strip().lower() in ("everything", "all", "all of it"):
            doomed = facts
        else:
            matches = self.recall(query, limit=1, count_use=False)
            doomed = matches[:1]
        if not doomed:
            return []
        gone = {f.id for f in doomed}
        self._write([f for f in facts if f.id not in gone])
        return doomed

    # -- reading ------------------------------------------------------------

    def recall(self, query: str = "", limit: int = 5, count_use: bool = True) -> list[Fact]:
        """Facts matching `query`, best first. No query means the most recent."""
        facts = self.facts()
        if not facts:
            return []

        wanted = _tokens(query)
        if not wanted:
            return facts[:limit]

        phrase = " ".join(query.lower().split())
        scored: list[tuple[float, float, Fact]] = []
        for fact in facts:
            haystack = _tokens(fact.text) | {t for tag in fact.tags for t in _tokens(tag)}
            hits = wanted & haystack
            if not hits:
                continue
            score = len(hits) / len(wanted)
            if phrase and phrase in fact.text.lower():
                score += 0.5
            if wanted & {t for tag in fact.tags for t in _tokens(tag)}:
                score += 0.2
            scored.append((score, fact.updated, fact))

        scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
        found = [row[2] for row in scored[:limit]]
        if found and count_use:
            self._note_use(found)
        return found

    def _note_use(self, used: list[Fact]) -> None:
        """Record that these were recalled - a cheap signal of what matters."""
        ids = {f.id for f in used}
        facts = self.facts()
        for fact in facts:
            if fact.id in ids:
                fact.uses += 1
        self._write(facts)

    def recent(self, limit: int = 20) -> list[Fact]:
        return self.facts()[:limit]

    def count(self) -> int:
        return len(self.facts())


# -- conversations -----------------------------------------------------------


class ConversationLog:
    """The tail of recent conversations, one JSON object per line.

    Append-only until it is trimmed, so a crash mid-write costs the last line
    rather than the history.
    """

    def __init__(self, path: Path | str, keep: int = 200) -> None:
        self.path = Path(path)
        self.keep = max(1, int(keep))

    def append(self, turns: list[dict[str, str]]) -> None:
        turns = [
            {"who": str(t.get("who") or "user"), "text": " ".join(str(t.get("text") or "").split())}
            for t in turns
            if str(t.get("text") or "").strip()
        ]
        if not turns:
            return
        record = {"ended": time.time(), "turns": turns}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError as exc:
            log.debug("could not append to %s: %s", self.path, exc)
            return
        self._trim()

    def _trim(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        if len(lines) <= self.keep:
            return
        try:
            self.path.write_text("\n".join(lines[-self.keep :]) + "\n", encoding="utf-8")
        except OSError:
            pass

    def all(self) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        records = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("turns"):
                records.append(record)
        return records

    def last(self, max_age_seconds: float | None = None) -> dict[str, Any] | None:
        records = self.all()
        if not records:
            return None
        record = records[-1]
        if max_age_seconds is not None:
            if time.time() - float(record.get("ended") or 0) > max_age_seconds:
                return None
        return record


# -- wiring ------------------------------------------------------------------


def resolve(config, path: str) -> Path:
    """Absolute path for a configured file, anchored on the config file.

    `load_config` already does this, but a `Config()` built in a test or a
    library caller has not been through it, and an SSH-invoked command starts
    in the user's home directory where a relative path means somewhere else.
    """
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    base = config.source_path.resolve().parent if config.source_path else Path.cwd()
    return base / candidate


def store_for(config) -> MemoryStore:
    return MemoryStore(resolve(config, config.memory.file), config.memory.max_facts)


def conversations_for(config) -> ConversationLog:
    return ConversationLog(
        resolve(config, config.memory.conversation_file), config.memory.max_conversations
    )


def ago(when: float | None) -> str:
    """'11 minutes ago', for a timestamp that may be missing or in the future."""
    if not when:
        return "at some point"
    seconds = max(0.0, time.time() - float(when))
    if seconds < 90:
        return "just now"
    from .humanize import duration_speech

    return f"{duration_speech(seconds)} ago"


def instruction_block(config) -> str:
    """The memory the voice session should start with, as prompt text.

    Prepending this to the personality prompt is what makes the difference
    between an assistant and a stranger: it arrives at the conversation
    already knowing who it is talking to, without having to spend a tool call
    to find out.
    """
    if not config.memory.enabled:
        return ""

    parts: list[str] = []

    facts = store_for(config).recent(config.memory.inject_facts)
    if facts:
        lines = "\n".join(f"- {f.text}" for f in facts)
        parts.append(
            "WHAT YOU ALREADY KNOW\n"
            "Things this user has told you before. Use them the way a person "
            "who remembers would - naturally, and only when relevant. Never "
            "recite the list.\n"
            f"{lines}"
        )

    if config.memory.carry_conversation:
        last = conversations_for(config).last(config.memory.carry_max_age_minutes * 60)
        if last:
            turns = last["turns"][-config.memory.carry_turns :]
            spoken = "\n".join(
                f"  {'you' if t['who'] == 'assistant' else 'the user'}: {t['text']}"
                for t in turns
            )
            parts.append(
                f"EARLIER, {ago(last.get('ended')).upper()}\n"
                "The end of your last conversation. Treat it as continuous - a "
                "follow-up like 'do that again' refers to it - but do not bring "
                "it up unprompted.\n"
                f"{spoken}"
            )

    if not parts:
        return ""

    parts.append(
        "When the user tells you something durable - about themselves, this "
        "machine, the household, or how they like things done - store it with "
        "pc_agent {\"command\":\"memory.remember\",\"args\":{\"text\":\"...\"}}. "
        "Store the fact, not the sentence, and say nothing about having done "
        "it unless asked. Look things up with memory.recall when you are not "
        "sure, and drop them with memory.forget when you are corrected."
    )
    return "\n\n" + "\n\n".join(parts)
