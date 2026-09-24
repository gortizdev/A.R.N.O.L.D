# Patching assistant.py for PC-side voice

Two additions to `/home/parzival/voiceassistant/assistant.py`. Neither changes
how Jarvis behaves on its own — both are additive.

1. **`POST /ask`** — accept text, return Jarvis's reply. This is what lets the
   PC do the listening while Jarvis keeps doing the thinking.
2. **Wake-word deference** — stay quiet while the PC has claimed the wake word,
   so you don't get two assistants answering the same question.

Send me the output of the greps below if you'd like these written precisely
against your code — the sketches here are deliberately generic because I
haven't seen `assistant.py`.

---

## 1. The `/ask` route

`HudBroadcaster`'s handler already routes `/say`, `/alarm` and `/volume`
(assistant.py:384-406). Add a fourth branch alongside them.

Find how `/say` is handled:

```bash
grep -n "'/say'\|\"/say\"" -A12 assistant.py
```

The new branch, in the same style:

```python
elif self.path == "/ask":
    # Text from the PC's speech recognition. Reason over it exactly as if it
    # had come from the Pi's own microphone, and return the reply so the PC
    # can speak it - do NOT speak it here, or it comes out of both rooms.
    text = (payload.get("text") or "").strip()[:1000]
    if not text:
        self._json({"ok": False, "error": "no text"})
        return
    try:
        reply = handle_user_text(text, speak=False)      # <-- your function
        self._json({"ok": True, "reply": reply or ""})
    except Exception as exc:
        self._json({"ok": False, "error": str(exc)})
```

The one thing to get right: **`speak=False`**. Jarvis must return the text
rather than speak it, otherwise the answer plays on the Pi *and* the PC.

To find the right function to call, look for whatever the transcription path
hands its text to:

```bash
grep -n "def .*\(transcri\|utterance\|user_text\|handle_\|respond\|process_\)" assistant.py | head -30
```

You want the function that takes a user string and returns the assistant's
reply — the one the Pi's own STT feeds. If it speaks internally with no way to
suppress that, the smallest change is to split it in two: one that produces the
reply text, one that speaks it.

---

## 2. Wake-word deference

While `arnold listen` runs on the PC, it publishes its device id
retained to `jarvis/hud/wake_owner`. It clears that on exit, and the MQTT
last-will clears it if the PC sleeps or loses power — so the Pi automatically
resumes when the PC goes away.

Jarvis doesn't use MQTT (per JARVIS-INTEGRATION.md §5), so the simplest option
is to let the existing state bridge own it. In `jarvis_state_bridge.py` the
subscription already exists; expose the claim as a file Jarvis can check:

```python
# in jarvis_state_bridge.py, alongside the SSE publishing
CLAIM_FILE = "/run/user/1000/jarvis_wake_owner"

def on_message(client, userdata, msg):
    if msg.topic.endswith("/wake_owner"):
        owner = msg.payload.decode().strip()
        if owner:
            open(CLAIM_FILE, "w").write(owner)
        elif os.path.exists(CLAIM_FILE):
            os.remove(CLAIM_FILE)
```

Then in `assistant.py`, at the top of the wake-word handler:

```python
import os
WAKE_CLAIM = "/run/user/1000/jarvis_wake_owner"

def wake_word_detected():
    if os.path.exists(WAKE_CLAIM):
        # The desktop is awake and listening; let it answer this one.
        return
    ...existing behaviour...
```

A file check is deliberate: it's a single `stat`, costs nothing in the audio
path, and fails safe. If the bridge dies the file goes stale — so have the
bridge remove it on startup, and consider ignoring a claim older than a few
minutes.

Find the wake-word handler with:

```bash
grep -n "wake\|porcupine\|openwakeword\|oww\|detect" assistant.py | head -30
```

---

## Testing

Without any of this, the PC still answers questions about itself — those are
matched locally and never reach the Pi. Confirm that first:

```powershell
arnold listen --brain local
```

Say *"hey jarvis, how much disk space is left"*. Once `/ask` exists, switch to
`--brain jarvis` and anything that isn't a PC question is forwarded to the Pi.
