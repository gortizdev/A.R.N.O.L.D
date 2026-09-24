"""Which process am I?

Two questions, and they are not the same one.

* **IS_AGENT** - am I `arnold run`? Some commands only work there,
  because they belong to the process that holds the MQTT connection and the
  alert history. A one-shot `exec` forwards those to it.
* **IS_RESIDENT** - will I still be here in a minute? The agent, a voice
  session and the dashboard server all stay up; `exec` prints its reply and
  exits. Anything that starts work outliving the call needs to know, and for
  those the answer is not "only the agent" - any resident process will do.
"""

from __future__ import annotations

# True only inside `arnold run`.
IS_AGENT = False
# True in the agent, a voice session, and the dashboard server.
IS_RESIDENT = False


def mark_agent() -> None:
    global IS_AGENT
    IS_AGENT = True
    mark_resident()


def mark_resident() -> None:
    global IS_RESIDENT
    IS_RESIDENT = True
