"""Local voice: the PC listens, Jarvis thinks, the PC answers.

The PC handles audio because it has the better microphone position and a GPU
that transcribes in a fraction of a second. Reasoning stays on the Pi so there
is one brain, one personality, and one set of tools - the transcript is posted
to Jarvis and its reply is spoken here.

When this is running it claims the wake word over MQTT so Jarvis stands down;
when the PC sleeps or shuts down the claim disappears with the agent's MQTT
last-will and the Pi resumes answering on its own.
"""

from .pipeline import VoiceAssistant, VoiceState

__all__ = ["VoiceAssistant", "VoiceState"]
