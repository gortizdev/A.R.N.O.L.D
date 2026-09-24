# Pi-side setup

Everything here goes on the Raspberry Pi (`192.168.1.171`), not the Windows PC.

## What's here

| File | Where it goes | Why |
|---|---|---|
| `homeassistant/configuration.snippet.yaml` | append to HA's `configuration.yaml` | Lets HA relay text to Jarvis's loopback-only `/say`. **Required** for `speech_route: home_assistant`. |
| `homeassistant/automations.snippet.yaml` | append to HA's `automations.yaml` | Optional: react to the PC's MQTT sensors from HA. |
| `jarvis_pc.py` | next to `assistant.py` | Lets Jarvis query and control the PC. |
| `jarvis_state_bridge.py` + `.service` | next to `assistant.py`, unit to `~/.config/systemd/user/` | Optional: relays Jarvis's state to MQTT so the PC's animated face can listen, think and lip-sync along. |

## 1. The Home Assistant relay (required)

Jarvis's HTTP API binds to `127.0.0.1:8765` (`assistant.py:460`), so the PC
cannot reach it. Home Assistant runs on the same Pi and can. Append the
`rest_command:` block from `homeassistant/configuration.snippet.yaml`:

```bash
ssh parzival@192.168.1.171
nano /home/parzival/homeassistant/config/configuration.yaml
```

`assistant.py:374` already refers to `rest_command.jarvis_say` by name, so check
whether it exists before adding it — YAML will reject a duplicate `rest_command:`
key. If one is already there, merge the entries instead of adding a second block.

Restart HA, then confirm from the Pi:

```bash
curl -s -X POST \
  -H "Authorization: Bearer $HA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "the relay works"}' \
  http://127.0.0.1:8123/api/services/rest_command/jarvis_say
```

If Jarvis speaks, the PC's `arnold say` will work too.

## 2. An MQTT user for the PC

Mosquitto has `allow_anonymous false`, so the agent needs an account:

```bash
docker exec -it $(docker ps --filter name=mosquitto -q) \
    mosquitto_passwd /mosquitto/config/passwords pc-agent
docker restart $(docker ps --filter name=mosquitto -q)
```

Put that username and password in the PC's `config.yaml` under `mqtt:`.

## 3. Letting Jarvis query the PC

Copy `jarvis_pc.py` to `/home/parzival/voiceassistant/`, then from inside
Jarvis:

```python
from jarvis_pc import SshPcClient, handle_intent

pc = SshPcClient()
pc.say_answer("query.system")
# 'the desktop is at CPU 12 percent, memory 46 percent, 91 gigabytes free on drive C...'

# Or route a recognised phrase straight through:
reply = handle_intent("pc disk")
if reply:
    speak(reply)
```

Test it standalone first:

```bash
python3 jarvis_pc.py query.system
python3 jarvis_pc.py query.disk drive=C
```

### Which transport?

`SshPcClient` is the default and the one to start with — it reuses the
`~/.ssh/jarvis_pc` key Jarvis already uses in `_pc_ssh()` (`assistant.py:3014`),
so there is nothing new to provision, and it keeps working if Mosquitto is down.
Cost is an SSH handshake per call (~200-400ms on this LAN).

`MqttPcClient` avoids that handshake and is worth switching to if you end up
polling the PC frequently. It needs `CA_SHARED_SECRET` set to the same value as
`security.shared_secret` on the PC, plus MQTT credentials.

## 3b. The state bridge (optional, powers the animated face)

Jarvis's SSE stream on `127.0.0.1:8765` is loopback-only, so the PC cannot read
it. This relays the interesting events onto MQTT, which the PC already reaches:

```bash
cp jarvis_state_bridge.py      /home/parzival/voiceassistant/
cp jarvis-state-bridge.service /home/parzival/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now jarvis-state-bridge
journalctl --user -u jarvis-state-bridge -f
```

Test it by hand first — it prints each state change:

```bash
python3 jarvis_state_bridge.py --verbose --mqtt-user pc-agent --mqtt-password '...'
```

Say the wake word; you should see `jarvis state -> listening`. It publishes to
`jarvis/hud/state`, `jarvis/hud/eq` (16 audio bands, drives the mouth) and
`jarvis/hud/bridge` (online/offline).

Without this the face still shows alerts and offline state, and still animates
while speaking — just from a synthesised envelope rather than Jarvis's real
audio.

## 4. Configuration

`jarvis_pc.py` reads environment variables, so nothing is hardcoded into
Jarvis. Add to `/home/parzival/voiceassistant/.env`:

```ini
PC_HOST=192.168.1.103
PC_USER=geogo
PC_SSH_KEY=/home/parzival/.ssh/jarvis_pc
CA_SHARED_SECRET=<the value from `arnold gen-secret`>
CA_DEVICE_ID=desktop_rssbqj2

# only needed for MqttPcClient
MQTT_HOST=127.0.0.1
MQTT_USER=jarvis
MQTT_PASSWORD=<mosquitto password>
```

## Troubleshooting

**`Permission denied (publickey)` from `jarvis_pc.py`** — the SSH direction that
already works is Pi → PC, which is what this uses, so this usually means the PC
has no OpenSSH server running rather than a key problem. On the PC:

```powershell
Get-Service sshd
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic
```

**`'arnold' is not recognized`** — the PC's SSH shell is PowerShell,
and non-login sessions don't have the venv on PATH. `jarvis_pc.py` already uses
the full path; override it with `PC_AGENT_EXE` if your install lives elsewhere.

**`no config file found`** — older builds only looked in the working directory,
and SSH sessions start in `C:\Users\geogo`. The agent now also searches its
install root, so this should not recur. If it does, set
`ARNOLD_CONFIG` on the PC to the full path of `config.yaml`.

**HA returns 400 on `rest_command.jarvis_say`** — the rest_command isn't defined,
or `configuration.yaml` has two `rest_command:` keys and the later one won.

**Jarvis speaks nothing but HA returns 200** — the relay worked and Jarvis
accepted it; check Jarvis's own output volume (`POST /volume`) and that
`jarvis-assistant.service` is running.

## 5. Two assistants: Jarvis here, Mycroft on the PC

The PC no longer pretends to be Jarvis. It answers to *hey Mycroft*, speaks
in a different voice, and shows a steel-blue face instead of the gold core
(see the `assistant` section of the PC's `config.yaml`; `mirror_jarvis: true`
puts it back the old way). Nothing on this side has to change for that:

- **Jarvis reaching the PC** is exactly as in section 3 - `pc_agent_tool.py`
  over SSH. Only the wording is worth a tweak: tell Jarvis in his prompt that
  the desktop has an assistant of its own called Mycroft, so "ask Mycroft
  what is on my screen" resolves to the PC tool rather than confusion.
- **Mycroft reaching Jarvis** uses the `rest_command.jarvis_ask` and
  `rest_command.jarvis_say` relays from section 1. Both are installed
  (2026-09-06), and `assistant.py` has the matching `POST /ask` route: one
  fresh chat-completions turn with Jarvis's prompt, memory and tools, whose
  reply comes back as text and is never spoken on the Pi. Jarvis's system
  prompt in `config.py` also has a MYCROFT rule, so he knows the desktop has
  an assistant of its own and never claims to be it. Backups of all three
  files sit beside them as `*.bak-mycroft-<stamp>`.
- **The wake-word claim** (`pc_wake_claim`, section 3) is now unused unless
  both machines are set to the same wake word. `wake_claimed()` is harmless
  to leave in place; it simply never sees a claim.
- **The state bridge** (section 3b) still publishes to `jarvis/hud`. The
  PC's face no longer follows it by default - it follows `arnold/hud`, the
  PC's own session - so the desktop stops lighting up when Jarvis is talking
  in another room. `face.follow_jarvis: true` on the PC restores that.
