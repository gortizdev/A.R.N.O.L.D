# A.R.N.O.L.D.

**A** **R**ather **N**ice, **O**rdinary, **L**oyal **D**aemon.

A Windows agent that monitors this PC, speaks for it, and bridges it to the
J.A.R.V.I.S. voice assistant on the Raspberry Pi at `192.168.1.171`. Where
Jarvis is *just a rather very intelligent system*, Arnold is the quieter
sibling on the desktop: it watches, reports, and does what it is told.

It does four things:

- **Monitors** CPU, memory, disks, network, GPU, battery, processes, and the
  active window, publishing a snapshot over MQTT every few seconds.
- **Alerts** on threshold rules with duration, hysteresis, and cooldown, and
  speaks them through Jarvis.
- **Answers questions** — "how much disk is left?" returns a ready-to-speak
  sentence, not JSON for the Pi to summarise.
- **Takes orders** — launch apps, lock/sleep/shutdown, volume and media keys,
  toasts, clipboard, screenshots.

## Two assistants

The PC is not Jarvis. Out of the box it is **Arnold**: wake word *hey
Arnold*, a different voice (`marin` against Jarvis's `cedar`), its own
persona, and a cool steel-blue face where Jarvis's core is gold. The point is
that you always know which machine answered, and each can be addressed
without the other waking.

They talk to each other:

| Direction | How |
|---|---|
| Arnold asks Jarvis | the `ask_jarvis` tool - "ask Jarvis what time the oven timer is" goes to the Pi over the same Home Assistant relay the alerts use, and the reply is read back, attributed |
| Arnold tells Jarvis | the `tell_jarvis` tool - Jarvis says it aloud through the Pi's speaker |
| Jarvis asks the PC | unchanged: `pi/pc_agent_tool.py` over SSH, so "Jarvis, how much disk is left on the desktop" still works |
| Alerts | still spoken by Jarvis, since the Pi is where the speaker in the room is |

Everything about the identity lives in the `assistant` section of
`config.yaml`: `name`, `voice`, `persona`, `delivery`, and `mirror_jarvis`,
which turns all of it off and makes the PC Jarvis-in-another-room again (his
voice and prompt fetched from the Pi, gold face, shared topics). The wake
word is `voice.wake_word`; it is a trained model rather than free text. *Hey
Arnold* ships in `models/wake/hey_arnold.onnx`, trained with the tooling
below; any other name needs a custom openWakeWord model - see
[Wake words](#wake-words).

### Profiles

Those settings are also bundled into **profiles**, so an identity is one
name rather than six fields. `arnold`, `mycroft` and `jarvis` are built in; add your
own under `assistant.profiles`, setting only the fields you want to differ:

```yaml
assistant:
  profile: arnold            # who it is right now
  profiles:
    athena:
      name: Athena
      voice: sage
      wake_word: hey_athena  # a model you trained - see below
      palette: steel
      design: lattice
```

```powershell
arnold profile               # everyone it can be, and who it is
arnold profile use jarvis    # become Jarvis
```

`profile use` rewrites exactly one line of `config.yaml` - the `profile:`
key, comments and all else untouched - and the voice session, the face and
the agent notice the file change within a couple of seconds and become the
new assistant without a restart: new wake word, voice, persona and colours.
Say *"be Jarvis for a while"* and the same thing happens through the
`switch_profile` tool; a voice cannot change mid-call, so it signs off and
the next wake word gets the new one. Jarvis can do it over SSH too
(`profile.use`), as can the dashboard.

Because the two answer different words, `voice.claim_wake_word` is off - the
claim only ever existed to stop both answering "hey Jarvis" at once.

## How it connects to Jarvis

Jarvis's own HTTP API (`HudBroadcaster`, `assistant.py:319-469`) binds to
`127.0.0.1:8765` and **cannot be reached from this PC** — confirmed by both the
bind call and a port scan from here. Jarvis also has no MQTT integration of its
own. So the bridge routes around that:

```
                          ┌──────────────────── Raspberry Pi (192.168.1.171) ─────────────┐
                          │                                                               │
  ┌──────────────┐  MQTT  │  ┌────────────┐   rest_command    ┌─────────────────────────┐ │
  │ Windows PC   │───────►│  │    Home    │──────────────────►│ Jarvis :8765 (loopback) │ │
  │ this agent   │  1883  │  │  Assistant │  http://127.0.0.1 │  /say  /alarm  /volume  │ │
  │              │◄───────│  │   :8123    │                   └─────────────────────────┘ │
  └──────────────┘  cmds  │  └────────────┘                                               │
         ▲                └───────────────────────────────────────────────────────────────┘
         │  SSH + PowerShell (Jarvis's existing _pc_ssh, key ~/.ssh/jarvis_pc)
         └────────────────────────────────────────────────────────────────────
```

Home Assistant runs *on the Pi*, so it can reach Jarvis's loopback endpoint even
though this PC cannot. Mosquitto was already provisioned for exactly this shape
of traffic — its config comment reads *"HASS.Agent (Windows desktop) ↔ Home
Assistant"*.

| Direction | Transport | Needs |
|---|---|---|
| Telemetry & alerts out | MQTT → Mosquitto → HA | MQTT credentials |
| Speech out | HA REST → `rest_command.jarvis_say` → `:8765/say` | HA long-lived token |
| Commands in (from Jarvis) | SSH → `arnold exec` | Jarvis's existing PC key |
| Commands in (from HA) | MQTT command topic | shared secret for signing |

Speech can instead go over SSH directly (`speech_route: ssh`), which skips Home
Assistant but needs a **new** SSH key from this PC to the Pi — the existing
`~/.ssh/jarvis_pc` key runs the other direction.

## Install

```powershell
cd C:\Users\geogo\Downloads\Projects\ARNOLD
uv venv
uv pip install -e .
# optional: absolute volume control ("set volume to 40%") instead of stepping
uv pip install -e ".[audio]"
# optional: mail and meetings, read from the Outlook desktop client
uv pip install -e ".[outlook]"
```

## Set up

**1. Copy the config and fill it in.**

```powershell
copy config.example.yaml config.yaml
arnold gen-secret     # paste into security.shared_secret
```

**2. Create an MQTT user on the Pi.** Mosquitto has `allow_anonymous false`, so
anonymous connections are refused:

```bash
ssh parzival@192.168.1.171
docker exec -it <mosquitto-container> \
    mosquitto_passwd /mosquitto/config/passwords pc-agent
docker restart <mosquitto-container>
```

**3. Create a Home Assistant token** from your HA profile page
(`http://192.168.1.171:8123/profile/security`) and put it in
`jarvis.home_assistant.token`. Make a new one — don't reuse Jarvis's.

**4. Add the relay to Home Assistant.** Append
[pi/homeassistant/configuration.snippet.yaml](pi/homeassistant/configuration.snippet.yaml)
to the Pi's `configuration.yaml` and restart HA. This is the piece that lets HA
reach Jarvis's loopback `/say`.

**5. Check it.**

```powershell
arnold diag
```

`diag` validates the config, tests the MQTT connection and the Jarvis route, and
prints a live snapshot. Fix anything it flags before running the agent.

Config is looked up in this order, so the agent works no matter which directory
it is invoked from — SSH sessions start in the user's home, not the project:

1. `--config <path>`
2. `$ARNOLD_CONFIG`
3. `./config.yaml`
4. `<install root>/config.yaml` (derived from the virtualenv location)
5. `~/.config/arnold/config.yaml`, then `%APPDATA%\arnold\config.yaml`

Relative `log_file` and `state_file` paths resolve against the config file's
directory, not the working directory.

**6. Run it.**

```powershell
arnold run
```

The PC now appears in Home Assistant as a device with sensors, no HA YAML
required — MQTT Discovery handles it.

## Everyday use

```powershell
arnold ui                          # the dashboard in a browser
arnold diag                        # health check + snapshot
arnold commands                    # list every command
arnold exec query.system           # ask a question, get JSON + speech
arnold exec query.disk --arg drive=C --pretty
arnold say "the build finished"    # test the Jarvis path
arnold send query.system           # round-trip over MQTT
arnold discovery --clear           # remove the device from HA
arnold profile                     # who it can be, and who it is
arnold profile use jarvis          # switch identity; running processes follow
arnold wake list                   # wake-word models, bundled and custom
arnold wake test                   # say the wake word, watch the score
arnold say --local "it works"      # out of this PC's own speakers
arnold exec timer.set --arg minutes=20 --arg name=pasta
arnold exec desktop.read_window    # what is on screen, as text
```

## Letting Jarvis drive

Jarvis already SSHes into this PC (`_pc_ssh()`, `assistant.py:3014`). The
cleanest integration reuses that — no new credentials, no new listener:

```python
# On the Pi, inside Jarvis
import json, subprocess

def ask_pc(command, **args):
    argv = ["ssh", "-i", "/home/parzival/.ssh/jarvis_pc", "geogo@192.168.1.103",
            "arnold", "exec", command]
    for key, value in args.items():
        argv += ["--arg", f"{key}={value}"]
    return json.loads(subprocess.run(argv, capture_output=True, text=True).stdout)

ask_pc("query.disk", drive="C")["speech"]
# -> "Drive C has 143.2 gigabytes free of 931 gigabytes, 84 percent used."
```

[pi/jarvis_pc.py](pi/jarvis_pc.py) is a ready-made version of this with both the
SSH and MQTT paths, including request signing.

Every query returns a `speech` field written to be read aloud verbatim.

## Acting without being asked

Three pieces turn the agent from something that answers into something that
notices. They are separate on purpose: the first is just data, the second
decides whether to speak, and the third acts at a time.

### History

The agent keeps a series of everything it monitors — fine samples for the last
half hour, hourly buckets for three months. Nothing else here has a memory of
time, and without one the assistant can only ever tell you what a number *is*:

```powershell
arnold exec query.trend --arg path=disks.C.free_bytes
# "Drive C is losing about 12 gigabytes a day, measured over the last 9 days.
#  At that rate it's empty in about 7 days."
```

Trends are measured by least squares over the hourly buckets, not first-to-last,
so one big delete doesn't have the assistant announcing that the disk empties on
Tuesday. It refuses to answer at all until it has a day or so of history — a
projection made from four samples is a guess said in a confident voice.

### Noticing

Every few minutes the agent asks a different question from the alert rules: not
*has a number crossed a line*, but *is anything going on that I'd want to be
told about before I thought to ask*. Observers produce candidates and a policy
decides whether now is a moment to say any of it.

| Observer | What it watches for |
|---|---|
| `disk_filling` | a drive on course to run out, judged by the trend rather than the percentage |
| `memory_creeping` | memory climbing all day, with a process to blame |
| `gpu_running_hot` | a GPU that has drifted warmer over days, not one that spiked |
| `log_errors` | the agent's own log filling with errors it handled and never mentioned |
| `pending_reboot` | a Windows update waiting, on a machine that has been up long enough to be ignoring it |
| `watched_process_ended` | a watched process closing — *"Steam has closed, after four hours"* |
| `battery_low` | on battery and getting short, by percentage or by time left |
| `pi_unreachable` | the Pi silent long enough that everything is now coming out of these speakers |
| `drive_vanished` | a drive that was there an hour ago and is not now |
| `long_session` | hours at the keyboard with no real break. Off by default |

The last six are about a *moment* rather than a level, which is the difference
between an observer and an alert rule: a rule says "Steam should be running",
and the observer says "Steam has just closed". Both are useful and they are not
the same sentence.

**It won't speak until you let it.** `proactive.enabled` runs the observers and
writes down what it *would* have said; `proactive.speak` is what lets it out
loud. The dashboard's **Noticed** panel shows both, so you can read a week of
its judgement before trusting any of it.

| Guard | What it stops |
|---|---|
| `quiet_hours` | Anything at all overnight |
| `min_gap_minutes` / `max_per_day` | A chatty afternoon |
| `repeat_after_hours` | The same remark twice |
| an alert firing | Two voices about one machine at once |

`judge: model` hands the final choice to a small model, which sees the time, the
window you're actually in, and what it has already said today — a rules engine
can tell you the disk is filling, but not that it isn't worth interrupting a
film to say so. Any failure falls back to the rules, so proactive speech never
depends on the internet being up.

`long_session` sits at priority 3, under the default `min_priority` of 4, so
switching it on only starts recording it — being told as well needs the floor
lowered too. Two switches for the one feature here that can nag.

### Speaking out loud

Everything the agent volunteers used to go to Jarvis on the Pi and nowhere
else, so with the Pi off an alert was written to the log and lost. The startup
banner said so: *nothing will be spoken*. The PC now has a mouth of its own.

```yaml
speech:
  route: auto      # jarvis | local | both | auto | none
```

| Route | Where it comes out |
|---|---|
| `jarvis` | the Pi, exactly as before |
| `local` | this PC's speakers |
| `both` | said in both rooms |
| `auto` | try the Pi; say it here only if he could not be reached |
| `none` | stay quiet |

`auto` is the default because it changes nothing while the Pi is up and loses
nothing when it is down. Leaving `speech.route` blank keeps the older
`jarvis.speech_route` behaviour, so upgrading does not by itself make a quiet
machine start talking.

```powershell
arnold say --local "the build finished"
arnold diag                 # which routes are actually usable
```

Speaking happens on one worker thread, so the agent's tick never waits for a
sentence. A dead Pi is asked once and then left alone for a minute, doubling
up to ten, rather than costing every line a Home Assistant timeout. Anything
older than ninety seconds is dropped rather than read out late. A live voice
conversation holds the queue rather than being talked over, and the on-screen
face moves its mouth for an alert just as it does for a conversation.

None of it can take the agent down: no speakers, no API key, no sound card and
no Pi are each a log line and silence.

### Reminders, timers and alarms

*"Remind me to check the render in twenty minutes."* Reminders and standing jobs
live in a file the agent's tick reads, so they survive the conversation, the
voice session and a reboot. Times are parsed from speech — `in twenty minutes`,
`at half four`, `every day at 9`, `every monday at 10` — and anything it cannot
parse is refused with a sentence rather than guessed at.

```powershell
arnold exec schedule.add --arg text="check the render" --arg when="in 20 minutes"
arnold exec schedule.list
arnold exec schedule.cancel --arg which=render
```

Jobs can run commands too, but only ones named in `schedule.allow_commands`,
which is empty by default — checked again when the job fires, so a standing
order stops working the moment you withdraw permission. A job whose time passed
while the PC was asleep fires if it's only a few minutes late and is dropped
otherwise; a repeating one catches up rather than firing a burst.

**A timer is the same machinery that rings.** *"Set a timer for twenty
minutes"* used to be handed to Jarvis, which meant it failed outright whenever
the Pi was off — so timers are now this machine's own, and they sound on these
speakers.

```powershell
arnold exec timer.set --arg minutes=20 --arg name=pasta
arnold exec timer.list      # "the pasta timer, nine minutes left"
arnold exec timer.cancel --arg which=pasta
```

Lengths are understood the way people say them: `twenty minutes`, `an hour and
a half`, `90 seconds`, `1:30`. A bare number is refused rather than guessed at,
because "set a timer for twenty" is twenty of something and choosing wrong
burns the dinner. Give a clock time instead (`--arg when="at 7"`) and it is an
alarm, which reads back as a time rather than as a countdown.

A timer differs from a reminder in exactly two ways: it plays a sound before it
speaks, and it describes itself as what is left. `schedule.list` still reports
both, because "what have I got on" is one question. There is no escalating
alarm — there would be nothing to dismiss it with — but a fired timer also
lands in the Windows notification centre, which is somewhere it can wait.

## The dashboard

```powershell
arnold ui
```

Or just ask for it — *"open the dashboard"* — which runs `desktop.dashboard`,
starts the server if nothing is serving yet, and brings the browser forward.

A page at `http://localhost:8770`, six views behind one strip of tabs. The
header carries a small orb driven by the same feed as the desktop core — in the
dashboard's own palette rather than the core's gold — so a glance tells you
whether it is idle, listening, thinking or firing an alert. Alt+1 to Alt+6
switch views, and `#machine` on the URL opens one directly.

| View | Shows |
|---|---|
| Today | the to-do list (below), what is scheduled, what the assistant noticed, and pages it has made |
| Machine | CPU, memory, each disk, GPU, network and battery with sparklines; the active window and busiest processes; which alert rules are firing |
| Claude Code | every Claude Code session lately, what it is doing, and a line to send it something |
| Talk | something for Jarvis to say aloud, a question for him, and the voice transcript while `arnold listen` runs |
| Commands | any of the commands with its arguments as fields, and what came back |
| Log | the tail of `agent.log` |

### The to-do list

The Today view is a to-do list: add a line, tick it off, remove it. It is also
what `todo.list`, `todo.add`, `todo.done` and `todo.remove` read and write, so
*"what's on my list"* and *"tick off the dentist"* work by voice, from the
shell and from the Pi, and the page shows the same file.

Once a week it fills itself from a document that arrives by email, and it
reads that email straight from the inbox through Microsoft Graph, the same API
Outlook itself uses. Sign in once:

```powershell
arnold mail login      # or the "Sign in to Outlook" button on the Today tab
```

It shows a code; you enter it at microsoft.com/devicelogin in any browser and
approve *Mail.Read*, which is the only permission asked for — nothing here can
send, move or delete. What comes back is a refresh token, kept in
`%LOCALAPPDATA%\arnold\graph-token.bin` and encrypted to your
Windows account with DPAPI. From then on the agent asks the inbox every five
minutes (`todo.mail.poll_seconds`) for the newest message whose subject
mentions `todo.match` (*"geo update"*), downloads its document to `logs/mail`
and imports it. The strip above the list says who is signed in, when the inbox
was last checked and which email the list came from. `arnold mail`
prints the same; `mail logout` forgets the sign-in.

**If your organisation refuses the sign-in.** This is a public client with no
secret of its own, and by default it borrows the identity of Microsoft's own
Graph PowerShell app, which every tenant already knows. Some tenants block that
for anything but PowerShell, or require an administrator to approve any app
that reads mail; the error says which. Either ask for *Mail.Read* to be
granted, or register an app yourself (Azure portal › App registrations › New;
*Accounts in this organizational directory*; under Authentication, add the
*Mobile and desktop* platform and turn *Allow public client flows* on; under
API permissions add Microsoft Graph › Delegated › `Mail.Read`) and put its
application id in `todo.mail.client_id`.

**Each group is a project, and a project can be a Claude Code project.** The
groups on the list are the document's projects, and the Claude Code tab
already knows the projects on this PC (every working directory a session has
run in, plus `code.projects`). A chip on each group's heading names its
Claude Code project, with the session's state as its dot: guessed from the
name where the words match (a dashed chip with a question mark), or chosen by
hand - click the chip and pick from the list, or say *"Data Migration is
Ellipse Data"* (`todo.link`). Once a group has a project, every task in it has
a *to Claude* button that hands the task to that project's session
(`todo.send`, the same path as *"tell the ROI session to..."*), and the
session's block on the Claude Code tab lists what it is owed. `todo.projects`
reads the whole map back.

**Without the inbox** the list still fills, because the attachment touches the
disk in two places that are watched as a fallback (`todo.watch_folders`):

* **Outlook's attachment cache** (`%LOCALAPPDATA%\Microsoft\Olk\Attachments`)
  gets a copy the moment you open or preview the attachment in Outlook. One
  click on the email, and the list has it within two minutes.
* **A Power Automate flow** files it into OneDrive with no click at all. Flows
  run with your own mailbox rights, so they need no consent from anyone. In
  [make.powerautomate.com](https://make.powerautomate.com) pick the template
  *Save Office 365 email attachments to your OneDrive for Business*, and in the
  trigger's advanced options set *Subject Filter* to `geo update`; leave the
  folder as *Email attachments from Power Automate*. The one catch: that is
  your **work** OneDrive, so it has to be synced on this PC for the file to
  land here - in the OneDrive tray icon, *Settings › Account › Add an account*,
  sign in with the work account, and sync at least that folder. Every
  `OneDrive*` folder under your profile is watched.

And there is always the Open dialog: **Import a file** on the Today tab (or
dropping a document anywhere on the tab) uploads it to the agent and imports
it as this week's document. `todo.import` does the same from a path.

Whichever way it arrives, the newest Word, PDF or text document whose path
mentions `todo.match` becomes this week's list. It is read the way the status
update is written: the date on the title line dates everything under it, a
top-level bullet is a project and becomes the group, its sub-bullets are the
tasks, and a project with its status on the same line (*"ROI Calculator -
waiting on Matt"*) is one task under that project. A document that grows a
dated section each week is read **one week at a time**: the weekly import
takes only the section dated within the current Monday-to-Sunday week and
otherwise leaves the list alone, saying on the strip which date the document
does have; a manual import takes the newest section. Checkboxes and table
rows work too, and a checked box or a row marked *Done* arrives already ticked. Last
week's items from the same document go; a tick on one that is still in the new
document survives; anything added by hand is never touched. The notification
listener also sees the email land, so the strip can say *the email arrived at
9:05* before the document does. `todo.sync` looks now rather than on the next
scan (PDFs need `pip install pypdf`).

The agent serves the same dashboard while it runs (`ui.enabled`), so once
`arnold run` is going the page is already there — `arnold
ui` is for when it isn't. Only one of them can hold the port; the second says so
and exits.

**It binds to loopback, and that is the security model.** The page can run
everything `arnold exec` can, which makes it exactly as privileged
as a terminal window on this PC — no more, but no less. Three doors keep it that
way: the `Host` header must be one we bound to (so a hostile site cannot resolve
its own name to 127.0.0.1 and talk to us), a cross-origin `Origin` is refused,
and writes need an `X-CA-UI` header that a drive-by form post cannot set.
Destructive commands stay behind `security.allow_destructive` and ask for a
second click.

To reach it from a phone, give it a token first — without one it refuses to
leave loopback:

```yaml
ui:
  host: 0.0.0.0
  token: ${CA_UI_TOKEN}    # arnold gen-secret
```

The token travels in the dashboard URL and then as a header, never as a cookie,
so nothing another tab does is ever sent with your credentials attached.

## The face

```powershell
arnold face
```

A frameless J.A.R.V.I.S. core that floats on the desktop and shows what the
assistant is doing: a sphere woven out of gold filament, turning slowly, which
flares and bristles in time with the voice. Drag to move, right-click for a
menu, double-click to have Jarvis speak the PC's status, Esc to close.

Colours below are the `gold` palette (Jarvis). Arnold's `steel` palette is
cyan at rest, violet-white listening, teal thinking and ice-blue speaking;
`face.palette` picks. The shape is separate: `face.design` is `core` (the
J.A.R.V.I.S. sphere: a busy scatter of short traces, a knot at the centre) or
`lattice` (Arnold: an evenly ruled globe with a few long traces, densely
studded, a three-ring gyroscope at the centre, turning the other way).

| Mode | Looks like | When |
|---|---|---|
| idle | cool amber, slow spin, breathing rim | nothing happening |
| listening | ice-white, rings drawing inward, a beating rim | Jarvis heard its wake word; the mic is open |
| thinking | deep orange, spun up, a sweeping arc | Jarvis is working |
| speaking | pulsing — every band drives its own spike | Jarvis is talking, in time with real audio |
| alert | red, urgent throb | an alert rule is firing |
| offline | grey, barely turning | the agent isn't running |

The sphere is genuinely 3-D: the far side passes behind the near side, and the
whole thing leans towards the mouse pointer. `style: orb` in config.yaml (or
`--style orb`) brings back the older cartoon face with eyes and a lip-synced
mouth instead.

Alert and offline work with no extra setup — they come from the agent's state
file. **Listening, thinking and speaking need the Pi-side bridge**, because
Jarvis's state lives behind its loopback-only API:

```bash
# on the Pi
cp pi/jarvis_state_bridge.py      /home/parzival/voiceassistant/
cp pi/jarvis-state-bridge.service /home/parzival/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now jarvis-state-bridge
```

It reads Jarvis's SSE stream locally and republishes to MQTT, including the 16
audio bands that drive the pulse — each band lengthens its own spikes in the
corona. Without it the core still pulses while speaking, just from a
synthesised envelope rather than real audio.

Tune it under `face:` in config.yaml, or override per-run:

```powershell
arnold face --size 260 --position top-right --opacity 0.85
```

Set `hide_when_idle: true` to make it appear only when something is happening.

## Wake words

`voice.wake_word` takes three forms:

| Form | Example | Meaning |
|---|---|---|
| bundled name | `hey_mycroft` | one of openWakeWord's own: `hey_mycroft`, `hey_jarvis`, `alexa`, `hey_rhasspy` |
| custom name | `hey_athena` | `<voice.wake_word_dir>/hey_athena.onnx`, where `wake install` and `wake train` put things (default `models/wake`) |
| path | `D:/models/hey_athena.onnx` | any openWakeWord model file |

A custom model wins over a bundled one of the same name; whoever put it in
that folder meant it.

```powershell
arnold wake list                       # every model it can see, the active one marked
arnold wake test                       # say it; a live score bar, TRIGGERED on a hit
arnold wake test hey_athena            # try a model without switching to it
arnold wake install C:\dl\hey_athena.onnx   # copy into models/wake, checking it loads
arnold listen --wake-word hey_athena
```

### Training one

A new name needs a new model, and openWakeWord trains one from synthetic
speech: a few thousand clips of the phrase in many voices, the same again of
near-misses, mixed over room echoes and background noise, then a small
classifier on top of its shared embedding. The whole thing is driven by
openWakeWord's own `train.py`; `wake train` writes its config, runs the
clip generation and augmentation stages through it, and runs the training
stage in-process (Windows cannot fork the trainer's data loader workers, so
it runs single-threaded here and skips the tflite export nobody needs).

```powershell
arnold wake train "hey athena"         # -> models/wake/hey_athena.onnx
arnold wake train "hey athena" --steps 20000 --samples 2000
arnold wake train "hey athena" --stage augment,train   # clips already made
```

It needs two things first, and says so exactly rather than crashing:

1. **The `wake-train` extra.** PyTorch and the augmentation stack. For the
   GPU install torch from the CUDA index before the extra:

   ```powershell
   uv pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
   uv pip install -e ".[wake-train]"
   ```

2. **The training assets**, under `models/wake/train/` (or `--data-dir`).
   They are large and `wake train` will not fetch multi-gigabyte files on its
   own; `wake train` lists whichever are missing with where to get each:

   | Path | What | From |
   |---|---|---|
   | `piper-sample-generator/` | the synthetic-speech generator, plus its `models/en_US-libritts_r-medium.pt` checkpoint | github.com/rhasspy/piper-sample-generator |
   | `features/acav100m.npy` | ~2000 h of pre-computed negative features | HF `davidscripka/openwakeword_features` |
   | `features/validation.npy` | 11 h of validation features, for the false-positive rate | same |
   | `rir/` | room impulse responses, WAV | HF `davidscripka/MIT_environmental_impulse_responses` |
   | `background/` | any folder of 16 kHz WAV noise; the notebook uses AudioSet and FMA subsets | |

**The dependable route is the notebook.** openWakeWord's
[automatic_model_training.ipynb](https://github.com/dscripka/openWakeWord/blob/main/notebooks/automatic_model_training.ipynb)
runs on a free Colab GPU in about an hour, downloads everything itself, and
hands back an `.onnx`; `wake train` prints the link when it cannot run
locally. Then:

```powershell
arnold wake install ~\Downloads\hey_athena.onnx
arnold wake test hey_athena
```

and either set `voice.wake_word: hey_athena` or put it in a profile. Bear in
mind that PyTorch wheels for the newest Python, and the sample generator's
phonemizer on Windows, are the two things most likely to be missing on a
fresh machine; the notebook has neither problem.

### What it took on Windows with Python 3.14

`hey_arnold.onnx` was trained locally this way (2000 clips per class, 20000
steps, about ten minutes on an RTX 5060 Ti). Four things needed a hand, all
in the venv rather than the project, so a fresh `uv pip install` loses them:

- **piper-sample-generator** at tag `v2.0.0` (the last one with
  `generate_samples.py` at the root) imports `piper_phonemize`, which has no
  wheel for this Python. A ten-line `piper_phonemize.py` dropped into the
  checkout forwards to `piper.phonemize_espeak.EspeakPhonemizer` from the
  `piper-tts` already installed for the voice extra. Its `torch.load` also
  needs `weights_only=False` on PyTorch 2.6+.
- **acoustics** imports `scipy.special.sph_harm`, gone in SciPy 1.17; wrap
  the import to fall back on `sph_harm_y`.
- **torchaudio 2.9+** hands file reading to torchcodec, which wants FFmpeg
  DLLs. `openwakeword/data.py` and `torch_audiomentations/utils/io.py` read
  WAVs through `soundfile` instead (same tensor shape and rate).
- **Windows cannot delete a mapped file**: `openwakeword`'s `trim_mmap` and
  its caller drop their memmaps before the delete.

`wake train` itself sets nothing up for these; `torch.onnx` also needs
`onnxscript` and a UTF-8 console (`PYTHONUTF8=1`), and its exporter parks
the weights in a `.onnx.data` sidecar that `wake train` now folds back into
the single file it installs.

## Reading the screen

*"What am I looking at"* and *"read this to me"* used to be answered by
photographing every monitor and sending the picture to a model. Windows already
knows what is on screen, so there is a cheaper and narrower way round:

```powershell
arnold exec desktop.read_window
arnold exec desktop.read_window --arg scope=focus   # just the caret's box
```

It reads the window in front as **text**, through UI Automation — the same
accessibility layer screen readers use, which is why it reaches inside Chrome,
Electron, WPF and UWP alike. Three tiers, best first: a document's text in one
piece, then the control tree's names and values, then plain `WM_GETTEXT`. The
result says which tier answered, so a thin answer can be explained rather than
guessed at.

```powershell
uv pip install -e ".[uia]"     # comtypes; without it only classic windows answer
```

Two switches govern the screen, and they are independent:

| Setting | What it allows |
|---|---|
| `voice.vision` | taking a screenshot and sending it as an image |
| `voice.screen_text` | reading the text of one window, with no picture taken |

With `vision: false, screen_text: true` the assistant can read what you are
looking at and no image of your screen ever leaves the machine. With both off it
cannot see the screen at all. The text still goes to the model when a voice
session asks for it, so this is *narrower* than a screenshot rather than
private — and `voice.screen_text_deny` refuses outright before anything is read:
password managers and private windows by default, matched on both the process
name and the title. The character count is logged; the text never is.

The voice session is told to prefer this for words and `view_screen` for
pixels — layout, images, charts, and where a thing sits on screen.

## What it remembers

A realtime session closes after a few seconds of silence, so without a memory
the assistant meets you again at every wake word. Two things are kept, because
they go stale at different rates:

- **Facts** — "the good coffee is in the left cupboard", "the games live on D".
  Stored when you say *remember that*, or when the assistant decides something
  is durable. They are put in front of the model at the start of every
  conversation, so it simply knows them rather than having to go and look.
- **The last conversation** — carried into the next one for three hours, so
  "do that again" still means something after a wake-word gap, and forgotten
  after that.

```powershell
arnold memory list                    # everything it is holding
arnold memory add "the bins go out on Tuesday" --tags house
arnold memory search bins
arnold memory forget bins             # or: forget everything
```

It is a plain-text file (`logs/memory.json`) of things said out loud in your
house — worth knowing, which is why `forget` is one word. Tune it under
`memory:` in config.yaml, or set `enabled: false` to keep nothing at all.
`inject_facts` is the number that costs money: every fact listed there is
prompt on every wake word.

The commands are `memory.remember` / `recall` / `forget` / `list`, so Jarvis
can reach them over SSH like any other.

## What it knows about your day

Off by default. Switched on, the assistant notices mail arriving and warns you
before a meeting starts:

```yaml
outlook:
  enabled: true
```

```powershell
arnold exec query.mail
# -> "You have 3 unread messages, 1 flagged important. The newest is from
#     Jane Doe, the quarterly numbers, 4 minutes ago."
arnold exec query.next_meeting
# -> "Next up is the design review at 2:30 PM, on Teams, 25 minutes away."
arnold exec query.agenda --arg hours=8
arnold exec web.join_meeting
```

**Teams meetings need no Teams API.** An invitation lands in the Outlook calendar
as an appointment carrying its own join URL, so the calendar answers "what's
coming up" and `web.join_meeting` opens the link the Teams client is already
registered to catch. Zoom, Google Meet and Webex links are recognised too, which
matters when the invite came from somebody else's organisation.

### Teams and Outlook notifications

The new Outlook and Teams expose nothing to read - no COM, no Graph without an
app registration and a tenant admin's consent. But both raise Windows toasts,
and Windows keeps those in the notification centre where an allowed app can
read them. So:

```yaml
notifications:
  enabled: true
  speak: false     # true = say each one aloud as it lands
```

```powershell
arnold exec query.notifications --arg hours=2
# -> "In the last 2 hours: 3 Teams messages and 1 mail. Jane Doe on Teams,
#     can you look at the deck before 3, 12 minutes ago; ..."
```

The assistant sees exactly what the toast said - sender, subject, first line -
and no more, which is the same as the lock screen shows. Each one lands in the
console's Noticed feed and on the notice topic; with `speak` on it is read
aloud as it arrives, honouring quiet hours, and a burst is summarised as a
count. "Any Teams messages?" and "what did I miss?" ask the same thing by
voice. Windows must allow it: Settings > Privacy > Notifications > "Let apps
access your notifications". Install the extra:
`pip install 'arnold[notifications]'`.

### Claude Code sessions

The Claude desktop app, the VS Code extension and the terminal all run the
same Claude Code, and it writes every session to a transcript under
`~/.claude/projects`. The agent tails those, so the console has a panel per
session - what it was asked, the step it is on, what it last said, how long
the turn has run - and the dev servers each one started, matched to it by
folder and probed, so `localhost:5173` is a link with a green or red dot
beside it. A running session also registers itself with its status and, when
it takes messages from other sessions, the named pipe it listens on; that is
how a prompt gets in.

```powershell
arnold claude                       # every session and its state
arnold claude status ellipse hub    # one, in a sentence
arnold claude apps                  # the dev servers, up or not
arnold claude prompt --which "ellipse hub" run the tests and fix what fails
```

By voice, "what are my Claude sessions doing", "how's the ROI session
going", and "tell the Ellipse Hub session to run the tests" do the same
through `claude.list`, `claude.status` and `claude.prompt`. A prompt goes
down the session's own pipe, as one Claude Code session's SendMessage reaches
another, wrapped the same way - the session sees it came from Arnold - and
attesting the session's own permission class, because a bare message into a
session that bypasses permission prompts is parked for a review nobody
unattended can give. It arrives in that conversation as a message from a
teammate, delivered after the step it is on, and the reply shows up in the
app like any other. The desktop app only keeps a session's process alive
while it is working, so a conversation sitting idle there has no pipe, and
the assistant says so: open it and send it anything, then ask again. It will
not run the prompt through the CLI behind the app's back, because the app
forks a fresh transcript for each turn it starts and would never show that
one. A terminal or VS Code session that has closed is different - with
`claude.resume_fallback` on (the default) the prompt runs through the CLI
against the same transcript, and is there on the next `--resume`. Either way
the agent says when the reply lands.

It speaks unprompted for two things: a turn finishing after it ran longer
than `speak_after_seconds`, and a session stopping to ask something or wait
on a permission. Everything else - a turn starting, a dev server coming up -
goes to the console's Noticed feed and the notice topic. The permission case
needs a hook, because Claude Code does not write the prompt to the
transcript until it is answered:

```powershell
arnold claude hooks install   # adds a hook to ~/.claude/settings.json
arnold claude hooks remove    # takes exactly that out again
```

The hook is `claude_hook.py`: one stdlib-only script that appends a line to
`logs/claude-events.jsonl` on session start, prompt, notification, stop and
session end. Sessions already open pick it up when they next start.

### Why COM and not Microsoft Graph

Graph wants an Azure AD app registration and, on a work tenant, an
administrator's consent for `Mail.Read` and `Calendars.Read`. Outlook is already
signed in as you on this machine and MAPI hands over the same mailbox — no token
to store, nobody to ask, nothing to renew. The trade:

| | Outlook COM | Microsoft Graph |
|---|---|---|
| Setup | none | app registration + tenant consent |
| Works when Outlook is closed | no | yes |
| Works off this PC | no | yes |
| Credentials on disk | none | a refresh token |

**It needs the *classic* Outlook desktop client, signed in to an account.** Not
the year — `Office16` is the folder every Click-to-Run Office has used since
2016, including current Microsoft 365 — but the *client*: the new Outlook
(`olk.exe`, the Store app) exposes no COM automation at all, and neither does
Teams. If `arnold diag` reports Outlook unreadable, that is almost
always why. Open classic Outlook once, add your account, and leave it running.

### What the rules watch

Two metrics reach the alert engine, and they are shaped to fit the machinery that
is already there rather than needing new plumbing:

| Metric | Reads |
|---|---|
| `calendar.minutes_until_next` | minutes to the next meeting that has not started |
| `calendar.in_progress` | whether one is running right now |
| `mail.unread` / `mail.unread_important` | Outlook's own unread counts |
| `mail.seconds_since_latest` | how long ago the newest message landed |

New mail is watched as *time since arrival* rather than as an unread count: a
count crosses its threshold once and then sits there, while elapsed time crosses
back over the line by itself, so each new message is a fresh event and one you
have not got round to reading is not announced twice.

```yaml
- name: meeting_soon
  metric: calendar.minutes_until_next
  op: "<"
  threshold: 5
  cooldown_seconds: 900
  notify: true
  message: "{calendar.next.subject} starts in {value_speech} minutes."
```

That `{calendar.next.subject}` is a dotted path into the snapshot, which any
alert message can now use. A bare metric rarely makes a sentence worth hearing —
"something starts in 5 minutes" is not a reminder — and the subject lives
somewhere else in the same snapshot from the number that fired.

Timing does not depend on the poll interval. Outlook is read once a minute, but
the cache holds absolute start times and the minutes-until figure is recomputed
on every tick, so the reminder lands on the minute it is due rather than on
whichever poll happened to notice. Polling is off the tick thread entirely: a
COM call is far too slow to sit in the telemetry loop, and a stalled Outlook
slows the mailbox down, not the machine's vitals.

Read-aloud subjects and senders are also published to MQTT and appear in Home
Assistant as `Unread mail`, `Newest mail`, `Next meeting`, `Next meeting in` and
an `In a meeting` binary sensor. Set `include_subjects: false` to keep the fact
that mail arrived without saying what it was about, or `ignore_senders` /
`ignore_subjects` to stop a newsletter getting a spoken announcement.

## What it puts on screen

`artifact.create` writes a self-contained HTML page and opens it — a chart, a
table, a checklist, a small tool. Pages are dressed in the assistant's own
styling, so what lands on screen looks like it came from the same place as the
face on the desktop. As Arnold that is a memorandum: steel-blue, ruled, a
serif title over monospace figures, nothing that glows. With
`assistant.mirror_jarvis` on it is his HUD instead: gridded, gold-lit,
corner-bracketed. The page carries the assistant's name and this PC's.

The model supplies a fragment and the shell supplies the look, with parts
already styled for it: a `.grid` of `.card`s, `.stat` with a `.label` and a
`.value`, `.bar`, `.badge` with `.ok` / `.warn` / `.crit`, and the palette as
CSS variables. Tables, headings, code, inputs and buttons need no classes at
all. A model that ignores all of that and returns a whole white-background
document is not refused — its styles and scripts are lifted out, its
`body` rules are re-pointed at the panel, and it is folded into the shell
anyway. `--arg raw=true` opts out entirely, for a page that really does want
the whole window.

## Commands

| Command | What it does |
|---|---|
| `system.ping` / `system.info` / `system.capabilities` | Liveness, machine facts, command list |
| `query.system` | One-sentence health summary |
| `query.cpu` / `query.memory` / `query.disk` / `query.gpu` | Resource readings |
| `query.network` / `query.battery` / `query.uptime` | More readings |
| `query.processes` / `query.process` | Top consumers; is X running |
| `query.active_window` / `query.alerts` / `query.volume` | Foreground app, firing rules, volume |
| `query.metric --arg path=disks.C.percent` | Any raw telemetry value |
| `query.trend --arg path=disks.C.free_bytes` | Which way it is heading, and when it runs out |
| `schedule.add` / `schedule.list` / `schedule.cancel` | Reminders and standing jobs |
| `todo.list` / `todo.add` / `todo.done` / `todo.remove` | The to-do list on the dashboard |
| `todo.sync` / `todo.import --arg path=...` | Take in this week's emailed document now; take in any document |
| `todo.mail_login` / `todo.mail_status` / `todo.mail_logout` | The inbox sign-in behind it |
| `todo.projects` / `todo.link` / `todo.send` | Which Claude Code project each group is; tie one; hand a task to its session |
| `control.launch` / `control.kill_process` † | Start an allowlisted app; close one |
| `control.lock` / `sleep` † / `hibernate` † / `shutdown` † / `restart` † / `logoff` † | Power |
| `control.cancel_shutdown` | Call off a pending shutdown |
| `control.volume_set` / `volume_adjust` / `mute` / `media` | Audio |
| `control.run_script` † | Run an allowlisted script |
| `desktop.notify` / `clipboard_get` / `clipboard_set` / `screenshot` | Desktop |
| `desktop.dashboard` | Put the dashboard on screen, starting it if needed |
| `artifact.create` / `artifact.open` / `artifact.list` | Build a page and show it |
| `memory.remember` / `recall` / `forget` / `list` | What it keeps between conversations |
| `query.mail` | Unread count and the newest thing in the inbox |
| `query.next_meeting` / `query.agenda` | The meeting in progress or due, and what's coming up |
| `web.join_meeting` | Open the join link for the current or next meeting |
| `code.task` / `code.status` / `code.projects` | Hand Claude Code a job in an allowlisted project |
| `claude.list` / `claude.status` | The Claude Code sessions open here, and what each is doing |
| `claude.prompt --arg which=ellipse --arg text=...` | Say something into a running session |
| `claude.apps` / `claude.open` | The dev servers those sessions started; open one |

† Requires `security.allow_destructive: true`.

## Security

The threat model is a home LAN where anything holding the broker password can
publish, and Jarvis's `:8765` has no authentication at all. So:

- **Commands are HMAC-SHA256 signed** over a canonical encoding of the whole
  envelope — including `reply_to` and `speak`, so neither can be tampered with.
- **Replays are rejected** via a timestamp window (default 120s) plus a nonce
  cache.
- **Destructive commands are off by default** and need
  `security.allow_destructive: true` in addition to a valid signature.
- **Launching and scripts are allowlist-only.** A mis-transcribed voice command
  cannot become arbitrary process execution.
- **No shell interpolation of spoken text.** Messages travel over stdin, so a
  message containing quotes or `$(...)` cannot inject.
- **Secrets are redacted** from logs.

Home Assistant automations cannot compute HMACs, so if you want HA to publish
commands directly, either route them through [pi/jarvis_pc.py](pi/jarvis_pc.py)
(which signs) or set `require_signature: false` and accept broker auth as the
only boundary.

## Run it at startup

```powershell
$action  = New-ScheduledTaskAction -Execute "C:\Users\geogo\Downloads\Projects\ARNOLD\.venv\Scripts\arnold.exe" `
                                   -Argument "run" `
                                   -WorkingDirectory "C:\Users\geogo\Downloads\Projects\ARNOLD"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "Arnold" -Action $action -Trigger $trigger -Settings $settings
```

Run at logon rather than as a service: toasts, clipboard, screenshots, and media
keys all need an interactive desktop session.

## Troubleshooting

| Symptom | Cause |
|---|---|
| HA entity names look wrong | HA builds entity ids from the **device name**, so `friendly_name: "the desktop"` gives `sensor.the_desktop_cpu`, not `sensor.<device.id>_cpu` |
| GPU temp shows °F in HA | HA converts `device_class: temperature` to your locale's units. Alert rules still evaluate raw Celsius on the PC, so a `threshold: 83` stays Celsius |
| `query.alerts` always says "none" | The agent isn't running, so there's no fresh `state_file` for one-shot `exec` calls to read |
| `no CONNACK` | Wrong MQTT credentials — Mosquitto has `allow_anonymous false` |
| HA returns 400 on say | `rest_command.jarvis_say` missing from HA config — see step 4 |
| HA returns 401 | Bad or expired long-lived token |
| Speech works, nothing audible | Jarvis's own volume — `POST /volume`, or check `jarvis-assistant.service` |
| `ssh` route: permission denied | This PC has no key on the Pi; `~/.ssh/jarvis_pc` runs Pi→PC |
| Toasts don't appear | Agent isn't in an interactive session, or Focus Assist is on |
| Volume reads `null` | Install the `[audio]` extra for pycaw |
| GPU sensors missing | `nvidia-smi` not on PATH |
| Commands rejected: timestamp | Clock drift between Pi and PC beyond 120s |
| It answers before you've finished a sentence | `voice.turn_detection: server` treats any pause as your turn ending. `semantic` reads the words instead; on `server`, raise `turn_silence_ms` |
| Talking over it does nothing | `voice.barge_in: false`, or `barge_in_threshold` is above your speaking level. Every conversation logs `mic peaked at RMS …` — set it a little above that |
| It cuts itself off mid-reply | Its own voice is reaching the mic above `barge_in_threshold`. Raise the threshold, or turn the speakers down; the log line above says by how much |
| First word after it stops talking is missing | Lower `echo_guard_ms` (the mic stays gated that long for speaker decay) |

## Layout

```
src/arnold/
  service.py       long-running agent: tick loop, alert dispatch, command handling
  cli.py           run / ui / exec / say / diag / commands / send / discovery / gen-secret / profile / wake
  config.py        YAML + ${ENV} config with validation; profiles; the identity watcher
  security.py      HMAC signing, replay protection, destructive-command gating
  alerts.py        rules engine: duration, hysteresis, cooldown
  history.py       telemetry series: fine ring + hourly buckets, trends
  notices.py       observers, policy and judge for speaking unprompted
  scheduler.py     reminders and standing jobs, parsed from speech
  jarvis.py        speech routing to the Pi (Home Assistant or SSH)
  speech.py        this PC's own mouth: route policy, worker thread, the timer ring
  humanize.py      byte/duration formatting for screen and for speech
  memory.py        facts and recent conversations kept between sessions
  monitors/        collector.py (snapshot), gpu.py (nvidia-smi), outlook.py (mail/calendar over COM)
  commands/        registry.py + system / query / control / desktop / profile / timer handlers
  voice/           wake word (audio.py), realtime session, tools, wake_tools.py (list/install/test/train)
  models/wake/     custom wake-word models (.onnx), from `wake install` or `wake train`
  transport/       mqtt.py, topics.py, discovery.py (HA MQTT Discovery)
  platform_win/    toast, clipboard, screenshot, audio, power, window, uia (screen text)
  face/            state.py (animation), holo.py (the core), render.py (the old face), app.py (tkinter)
  ui/              server.py (http.server + guards), index.html (the dashboard)
  state.py         runtime state shared between the agent and one-shot calls
pi/                Pi-side helpers, Home Assistant snippets, state bridge
```

## MQTT topics

Everything hangs off `arnold/<device_id>`:

| Topic | Direction |
|---|---|
| `.../status` | out, retained — `online` / `offline` (last will) |
| `.../telemetry` | out, retained — full snapshot; every HA sensor reads this |
| `.../alert` | out — alert fired or cleared |
| `.../active_window` | out — foreground window changed |
| `.../capabilities` | out, retained — the command list |
| `.../cmd` | **in** — signed command envelopes |
| `.../cmd/result` | out — command replies |
