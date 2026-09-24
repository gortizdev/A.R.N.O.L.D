"""Things the assistant makes and puts on screen.

Ask for a chart, a checklist, a summary of something it just read, and it
writes a self-contained HTML page and opens it. The browser is the renderer
because it is the one thing on the machine that can lay out a table, draw an
SVG and run a little JavaScript without any of it being installed first.

Artifacts are opened as `file:` URLs, which `web.open` deliberately refuses.
That is not an inconsistency: the rule there exists because the URL was
*dictated*, and a mis-heard one must not be able to reach the disk. Here the
file is one this process just wrote, to a directory it chose.

Pages are self-contained on purpose. There is no CDN to reach in an offline
house, and an artifact that renders as a blank page a month later because a
script host went away is worse than one that never had the script.

The model supplies content and this file supplies the look, which is why a
whole document arriving from it is unwrapped rather than used as-is: an
assistant whose pages each land in whatever default the model reached for is
not an assistant with a face, it is a browser with a wrapper around it.
"""

from __future__ import annotations

import html as html_escape
import logging
import re
import threading
from datetime import datetime
from pathlib import Path

from ..platform_win import foreground
from .registry import (
    CommandContext,
    CommandError,
    CommandResult,
    Registry,
    arg_bool,
    arg_str,
)
from .web import _default_browser_exe, _browser_path

log = logging.getLogger(__name__)

MAX_BYTES = 2_000_000
KEEP = 200  # artifacts kept on disk before the oldest are pruned

# The page takes the assistant's identity. Arnold's default is a memorandum:
# steel-blue, ruled, a serif title over monospace figures, and nothing that
# glows. When this PC mirrors Jarvis it is his HUD instead - gridded, gold,
# corner-bracketed - switched by `data-palette` on the body. Both use the same
# parts and variable names, so a fragment the model wrote for one renders in
# the other. Dark only, deliberately - a HUD in light mode is just a document.
_CSS = """
:root {
  color-scheme: dark;
  /* Arnold's steel: the face's idle tones laid flat. */
  --bg: #060c14; --panel: #0b1622; --code: #08111a;
  --ink: #dcefff; --dim: #8aa9c4; --faint: #5f7a92;
  --accent: #6ec4ee; --accent-2: #c9b4ff;
  --ok: #46d18d; --warn: #ffb545; --crit: #ff6f6f;
  --line: rgba(110,196,238,.3); --line-soft: rgba(140,180,215,.14);
  --glow: none;
  --mono: "Cascadia Code", Consolas, ui-monospace, monospace;
  --serif: Cambria, Georgia, "Times New Roman", serif;
  --ui: "Segoe UI", system-ui, -apple-system, sans-serif;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; padding: 2.4rem 1.25rem 3rem; min-height: 100vh;
  background:
    radial-gradient(1000px 560px at 0% 0%, rgba(110,196,238,.11), transparent 62%),
    radial-gradient(800px 500px at 100% 100%, rgba(201,180,255,.06), transparent 60%),
    linear-gradient(180deg, #08111b, var(--bg) 55%);
  color: var(--ink);
  font: 16px/1.65 var(--ui);
}
::selection { background: rgba(110,196,238,.28); }

/* Ruled like ledger paper, fading out down the page. The gold identity
   swaps this for its grid. */
.hud {
  position: fixed; inset: 0; z-index: 0; pointer-events: none;
  background: repeating-linear-gradient(180deg, rgba(110,196,238,.055) 0 1px, transparent 1px 28px);
  -webkit-mask-image: linear-gradient(180deg, #000 0%, rgba(0,0,0,.5) 45%, transparent 85%);
  mask-image: linear-gradient(180deg, #000 0%, rgba(0,0,0,.5) 45%, transparent 85%);
}
header, main, footer { position: relative; z-index: 1; max-width: 58rem; margin-inline: auto; }

/* The seal: the assistant's aperture in wire. It breathes; it does not turn. */
.seal { width: 3.4rem; height: 3.4rem; flex: none; fill: none; stroke: var(--accent); stroke-width: 1.4; }
.seal .rim { stroke: var(--line); stroke-width: 1.2; }
.seal .ticks { stroke: var(--dim); stroke-width: 1.6; }
.seal .far { stroke: var(--dim); opacity: .8; }
.seal .pupil { stroke: var(--accent-2); stroke-width: 1; }
.seal .core { fill: var(--accent); stroke: none; }
.seal .blades { transform-origin: 50% 50%; animation: breathe 7s ease-in-out infinite; }
.seal .core { animation: core 7s ease-in-out infinite; }
@keyframes breathe { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.06); } }
@keyframes core { 0%, 100% { opacity: .7; } 50% { opacity: 1; } }
.watermark {
  position: fixed; right: -6vw; bottom: -8vw; z-index: 0; pointer-events: none;
  width: 44vw; min-width: 22rem; opacity: .06;
}
.watermark .seal { width: 100%; height: auto; animation: none; stroke-width: .5; }
.watermark .seal * { animation: none; }

header {
  display: flex; align-items: center; gap: 1.2rem;
  padding: 0 .2rem 1rem; border-bottom: 1px solid var(--line);
  margin-bottom: 1.6rem;
}
.masthead { flex: 1; min-width: 0; }
.eyebrow {
  margin: 0; font-family: var(--mono); font-size: .66rem; letter-spacing: .24em;
  text-transform: uppercase; color: var(--accent);
}
header h1 {
  margin: .3rem 0 0; font-family: var(--serif); font-size: 1.9rem; font-weight: 400;
  line-height: 1.15; letter-spacing: 0; text-wrap: balance;
}
.stamp {
  display: flex; flex-direction: column; align-items: flex-end; gap: .35rem;
  font-family: var(--mono); font-size: .72rem; letter-spacing: .12em;
  color: var(--dim); white-space: nowrap; align-self: flex-end;
}
.ref {
  font-size: .62rem; letter-spacing: .2em; text-transform: uppercase; color: var(--faint);
  border: 1px solid var(--line-soft); padding: .2em .6em;
}
.live { display: none; }

main {
  position: relative;
  background: linear-gradient(180deg, rgba(255,255,255,.015), transparent 30%), var(--panel);
  border: 1px solid var(--line-soft); border-top: 2px solid var(--accent);
  outline: 1px solid var(--line-soft); outline-offset: 5px;
  padding: 2.1rem 2.4rem;
  box-shadow: 0 30px 70px rgba(0,0,0,.45);
  counter-reset: sec;
}
main > :first-child { margin-top: 0; }
main > :last-child { margin-bottom: 0; }

footer {
  display: flex; justify-content: space-between; gap: 1rem;
  padding: 1.2rem .2rem 0; font-family: var(--mono); font-size: .66rem;
  letter-spacing: .18em; text-transform: uppercase; color: var(--faint);
}

h1, h2, h3, h4 { line-height: 1.25; letter-spacing: 0; }
h2 {
  display: flex; align-items: baseline; gap: .8rem;
  font-family: var(--serif); font-size: 1.3rem; font-weight: 400; color: var(--ink);
  margin: 2.1rem 0 1rem; padding-bottom: .4rem; border-bottom: 1px solid var(--line-soft);
}
/* Sections are numbered, as a memorandum's are. */
h2::before {
  counter-increment: sec; content: "§ " counter(sec);
  font-family: var(--mono); font-size: .68rem; letter-spacing: .2em; color: var(--accent);
}
h3 { font-size: .95rem; color: var(--ink); margin: 1.5rem 0 .5rem; }
p, li { color: var(--ink); }
a { color: var(--accent); text-underline-offset: 3px; }
strong { color: #fff; }
small, .dim { color: var(--dim); }

code, pre, .mono { font-family: var(--mono); font-size: .88em; }
code { background: var(--code); border: 1px solid var(--line-soft); padding: .1em .38em; }
pre {
  background: var(--code); border: 1px solid var(--line-soft);
  padding: 1rem; overflow-x: auto;
}
pre code { background: none; border: 0; padding: 0; }

table { border-collapse: collapse; width: 100%; margin: 1.1rem 0; font-variant-numeric: tabular-nums; }
th, td { border-bottom: 1px solid var(--line-soft); padding: .55rem .7rem; text-align: left; }
th {
  font-family: var(--mono); font-size: .7rem; letter-spacing: .16em;
  text-transform: uppercase; color: var(--dim); border-bottom-color: var(--line);
}
tbody tr:nth-child(even) { background: rgba(110,196,238,.03); }
tbody tr:hover { background: rgba(110,196,238,.07); }

blockquote {
  position: relative; margin: 1.3rem 0; padding: .3rem 1rem .3rem 2.4rem; color: var(--dim);
  font-family: var(--serif); font-size: 1.1em; font-style: italic;
  border-left: 1px solid var(--accent-2);
}
blockquote::before {
  content: "“"; position: absolute; left: .7rem; top: -.35rem;
  font-size: 2.6rem; line-height: 1; color: var(--accent-2); opacity: .6; font-style: normal;
}
hr { border: none; border-top: 1px solid var(--line-soft); margin: 1.8rem 0; }
img, svg, canvas { max-width: 100%; height: auto; }

/* -- parts to build readouts out of ---------------------------------------- */
.grid {
  display: grid; gap: 1rem; margin: 1.25rem 0;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 13rem), 1fr));
}
.card {
  position: relative;
  background: linear-gradient(180deg, rgba(110,196,238,.06), rgba(110,196,238,.015));
  border: 1px solid var(--line-soft); border-top-color: var(--line);
  padding: 1rem 1.1rem;
}
.stat .label, .label {
  display: block; font-family: var(--mono); font-size: .66rem; letter-spacing: .2em;
  text-transform: uppercase; color: var(--dim);
}
.stat .value, .value {
  display: block; margin-top: .3rem; font-family: var(--mono); font-size: 1.9rem;
  font-variant-numeric: tabular-nums; color: var(--accent); text-shadow: var(--glow);
}
.bar {
  height: 5px; margin-top: .8rem; overflow: hidden;
  background: repeating-linear-gradient(135deg, rgba(255,255,255,.12) 0 2px, transparent 2px 5px);
}
.bar > * { display: block; height: 100%; background: var(--accent); }
.badge {
  display: inline-block; font-family: var(--mono); font-size: .66rem; letter-spacing: .16em;
  text-transform: uppercase; padding: .18em .55em;
  border: 1px solid var(--line); color: var(--accent);
}
.ok    { color: var(--ok);   border-color: rgba(70,209,141,.4); }
.warn  { color: var(--warn); border-color: rgba(255,181,69,.4); }
.crit  { color: var(--crit); border-color: rgba(255,111,111,.45); }
/* Specific enough to beat `.stat .value`, which would otherwise keep a
   reading in the accent when it has just been called critical. */
.value.ok, .value.warn, .value.crit { text-shadow: none; }
.value.ok { color: var(--ok); }
.value.warn { color: var(--warn); }
.value.crit { color: var(--crit); }

/* Controls, because half of what it makes is a small tool. */
label { display: inline-flex; flex-direction: column; gap: .4rem; font-size: .85rem; color: var(--dim); }
input, select, textarea {
  font: inherit; color: var(--ink); background: var(--code);
  border: 1px solid var(--line-soft); padding: .5rem .65rem;
}
input:focus, select:focus, textarea:focus {
  outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(110,196,238,.15);
}
button {
  font-family: var(--mono); font-size: .74rem; letter-spacing: .14em; text-transform: uppercase;
  color: var(--ink); background: transparent;
  border: 1px solid var(--line); padding: .6rem 1.1rem; cursor: pointer;
}
button:hover { border-color: var(--accent); color: var(--bg); background: var(--accent); box-shadow: var(--glow); }
output { font-family: var(--mono); font-variant-numeric: tabular-nums; color: var(--accent); }

/* -- the gold identity ----------------------------------------------------- */
/* When this PC is Jarvis the page is his HUD: gridded, bracketed, lit. Same
   parts, same variables, so a fragment written for one renders in the other. */
body[data-palette="gold"] {
  --bg: #0d0803; --panel: #17110a; --code: #0f0a05;
  --ink: #fff3dc; --dim: #c2a77a; --faint: #8a7350;
  --accent: #ffb545; --accent-2: #ffe6b0;
  --line: rgba(255,181,69,.3); --line-soft: rgba(255,210,122,.14);
  --glow: 0 0 18px rgba(255,181,69,.35);
  background:
    radial-gradient(1100px 620px at 10% -10%, rgba(255,181,69,.10), transparent 60%),
    radial-gradient(900px 520px at 100% 0%, rgba(255,230,176,.08), transparent 55%),
    var(--bg);
}
body[data-palette="gold"] .hud {
  display: block;
  background:
    repeating-linear-gradient(0deg, rgba(255,181,69,.05) 0 1px, transparent 1px 46px),
    repeating-linear-gradient(90deg, rgba(255,181,69,.05) 0 1px, transparent 1px 46px);
  -webkit-mask-image: radial-gradient(120% 90% at 50% 0%, #000 10%, transparent 78%);
  mask-image: radial-gradient(120% 90% at 50% 0%, #000 10%, transparent 78%);
}
body[data-palette="gold"] header { border-bottom: 0; margin-bottom: 0; padding-bottom: .8rem; align-items: flex-end; }
body[data-palette="gold"] .seal, body[data-palette="gold"] .watermark, body[data-palette="gold"] .ref { display: none; }
body[data-palette="gold"] .stamp { padding-bottom: .2rem; }
body[data-palette="gold"] main { outline: none; counter-reset: none; }
body[data-palette="gold"] h2::before { content: none; }
body[data-palette="gold"] .card { background: rgba(255,181,69,.045); }
body[data-palette="gold"] .bar { background: rgba(255,255,255,.08); }
body[data-palette="gold"] tbody tr:nth-child(even) { background: none; }
body[data-palette="gold"] blockquote::before { content: none; }
body[data-palette="gold"] button:hover { background: linear-gradient(180deg, rgba(255,181,69,.3), rgba(255,181,69,.1)); }
body[data-palette="gold"] .eyebrow { letter-spacing: .3em; opacity: .8; }
body[data-palette="gold"] header h1 { font-family: var(--ui); font-size: 1.2rem; font-weight: 600; letter-spacing: .01em; }
body[data-palette="gold"] .live {
  display: inline-block; width: 7px; height: 7px; border-radius: 50%;
  background: var(--accent); box-shadow: var(--glow); vertical-align: middle;
  margin-right: .5em; animation: live 2.4s ease-in-out infinite;
}
@keyframes live { 0%, 100% { opacity: .3; } 50% { opacity: 1; } }
body[data-palette="gold"] main {
  background: linear-gradient(180deg, rgba(40,28,12,.92), rgba(20,13,6,.94));
  border: 1px solid var(--line); border-top-color: rgba(255,181,69,.5); border-radius: 2px;
  box-shadow: 0 24px 60px rgba(0,0,0,.55), inset 0 1px 0 rgba(255,255,255,.04);
}
/* Corner brackets, drawn as eight hairlines rather than eight elements. */
body[data-palette="gold"] main::before {
  content: ""; position: absolute; inset: -1px; pointer-events: none;
  background:
    linear-gradient(var(--accent),var(--accent)) 0 0/18px 2px no-repeat,
    linear-gradient(var(--accent),var(--accent)) 0 0/2px 18px no-repeat,
    linear-gradient(var(--accent),var(--accent)) 100% 0/18px 2px no-repeat,
    linear-gradient(var(--accent),var(--accent)) 100% 0/2px 18px no-repeat,
    linear-gradient(var(--accent),var(--accent)) 0 100%/18px 2px no-repeat,
    linear-gradient(var(--accent),var(--accent)) 0 100%/2px 18px no-repeat,
    linear-gradient(var(--accent),var(--accent)) 100% 100%/18px 2px no-repeat,
    linear-gradient(var(--accent),var(--accent)) 100% 100%/2px 18px no-repeat;
}
body[data-palette="gold"] h2 {
  font-family: var(--mono); font-size: 1.05rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: .16em; color: var(--accent); border-bottom: 0; padding: 0 0 0 .7rem;
  border-left: 2px solid var(--accent);
}
body[data-palette="gold"] th { color: var(--accent); }
body[data-palette="gold"] .card { border-left: 2px solid var(--accent); border-top-color: var(--line-soft); border-radius: 2px; }
body[data-palette="gold"] .bar > * { background: linear-gradient(90deg, var(--accent), var(--accent-2)); }
body[data-palette="gold"] .bar, body[data-palette="gold"] .badge, body[data-palette="gold"] button,
body[data-palette="gold"] input, body[data-palette="gold"] select, body[data-palette="gold"] textarea,
body[data-palette="gold"] code, body[data-palette="gold"] pre { border-radius: 2px; }
body[data-palette="gold"] .badge { background: rgba(255,181,69,.08); }
body[data-palette="gold"] button { background: linear-gradient(180deg, rgba(255,181,69,.2), rgba(255,181,69,.06)); }
body[data-palette="gold"] button:hover { color: var(--ink); }
body[data-palette="gold"] blockquote { font-family: var(--ui); font-size: 1em; font-style: normal; border-left-width: 2px; background: rgba(255,230,176,.06); padding: .5rem 1rem; }

@media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }
@media (max-width: 34rem) { main { padding: 1.4rem 1.1rem; } }
"""

_SHELL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style>
</head>
<body data-palette="{palette}">
<div class="hud"></div>
<div class="watermark" aria-hidden="true"><svg class="seal" viewBox="0 0 100 100" aria-hidden="true"><circle class="rim" cx="50" cy="50" r="47"/><g class="ticks"><path d="M50 3v5M50 92v5M3 50h5M92 50h5"/></g><g class="blades"><path class="near" d="M69.0 51.0A19 19 0 0 1 64.1 62.7L75.2 81.1A40 40 0 0 0 89.1 58.3Z"/><path class="near" d="M62.7 64.1A19 19 0 0 1 51.0 69.0L45.8 89.8A40 40 0 0 0 71.8 83.5Z"/><path class="near" d="M49.0 69.0A19 19 0 0 1 37.3 64.1L18.9 75.2A40 40 0 0 0 41.7 89.1Z"/><path class="near" d="M35.9 62.7A19 19 0 0 1 31.0 51.0L10.2 45.8A40 40 0 0 0 16.5 71.8Z"/><path class="far" d="M31.0 49.0A19 19 0 0 1 35.9 37.3L24.8 18.9A40 40 0 0 0 10.9 41.7Z"/><path class="far" d="M37.3 35.9A19 19 0 0 1 49.0 31.0L54.2 10.2A40 40 0 0 0 28.2 16.5Z"/><path class="far" d="M51.0 31.0A19 19 0 0 1 62.7 35.9L81.1 24.8A40 40 0 0 0 58.3 10.9Z"/><path class="far" d="M64.1 37.3A19 19 0 0 1 69.0 49.0L89.8 54.2A40 40 0 0 0 83.5 28.2Z"/></g><circle class="pupil" cx="50" cy="50" r="9"/><circle class="core" cx="50" cy="50" r="3.5"/></svg></div>
<header>
  <svg class="seal" viewBox="0 0 100 100" aria-hidden="true"><circle class="rim" cx="50" cy="50" r="47"/><g class="ticks"><path d="M50 3v5M50 92v5M3 50h5M92 50h5"/></g><g class="blades"><path class="near" d="M69.0 51.0A19 19 0 0 1 64.1 62.7L75.2 81.1A40 40 0 0 0 89.1 58.3Z"/><path class="near" d="M62.7 64.1A19 19 0 0 1 51.0 69.0L45.8 89.8A40 40 0 0 0 71.8 83.5Z"/><path class="near" d="M49.0 69.0A19 19 0 0 1 37.3 64.1L18.9 75.2A40 40 0 0 0 41.7 89.1Z"/><path class="near" d="M35.9 62.7A19 19 0 0 1 31.0 51.0L10.2 45.8A40 40 0 0 0 16.5 71.8Z"/><path class="far" d="M31.0 49.0A19 19 0 0 1 35.9 37.3L24.8 18.9A40 40 0 0 0 10.9 41.7Z"/><path class="far" d="M37.3 35.9A19 19 0 0 1 49.0 31.0L54.2 10.2A40 40 0 0 0 28.2 16.5Z"/><path class="far" d="M51.0 31.0A19 19 0 0 1 62.7 35.9L81.1 24.8A40 40 0 0 0 58.3 10.9Z"/><path class="far" d="M64.1 37.3A19 19 0 0 1 69.0 49.0L89.8 54.2A40 40 0 0 0 83.5 28.2Z"/></g><circle class="pupil" cx="50" cy="50" r="9"/><circle class="core" cx="50" cy="50" r="3.5"/></svg>
  <div class="masthead"><p class="eyebrow">{eyebrow}</p><h1>{title}</h1></div>
  <div class="stamp"><span class="ref">Ref {ref}</span><time><span class="live"></span>{stamp}</time></div>
</header>
<main class="frag">
{body}
</main>
<footer><span>{footer}</span><span>self-contained</span></footer>
</body>
</html>
"""

# Applied after whatever the model wrote, so a page that styled `body` for a
# white document does not repaint the panel it is now sitting inside.
_GUARD = (
    "<style>.frag{color:var(--ink);background:none;font:inherit;"
    "margin:0;padding:0;max-width:none;min-height:0}</style>"
)


def register_all(registry: Registry) -> None:
    registry.register(
        "artifact.create",
        _create,
        "Build a page and show it on screen.",
        {
            "title": "what it is called",
            "html": "page body as HTML (preferred - tables, SVG, inline script)",
            "markdown": "or markdown, if plain prose is enough",
            "open": "false to write it without showing it",
            "raw": "true to skip the assistant's styling entirely",
        },
        needs_desktop=True,
    )
    registry.register(
        "artifact.open",
        _open_existing,
        "Show an artifact that was made earlier.",
        {"name": "file name, or omit for the most recent"},
        needs_desktop=True,
    )
    registry.register("artifact.list", _list, "List the artifacts made so far.")


# -- storage -----------------------------------------------------------------


def artifacts_dir(ctx: CommandContext) -> Path:
    """Where artifacts live: beside the config, so they survive a reinstall.

    Resolved absolutely, because the agent is started by Task Scheduler with a
    working directory of its own choosing - a relative path would scatter
    artifacts wherever each caller happened to be.
    """
    base = ctx.config.source_path.resolve().parent if ctx.config.source_path else Path.cwd()
    path = base / "artifacts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _slug(title: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return (text or "artifact")[:48]


def _prune(directory: Path) -> None:
    files = sorted(directory.glob("*.html"), key=made_at)
    for old in files[:-KEEP]:
        try:
            old.unlink()
        except OSError:
            pass


# -- rendering ---------------------------------------------------------------


def _render(
    title: str,
    body_html: str,
    device: str = "",
    assistant: str = "",
    palette: str = "steel",
) -> str:
    clean_title = html_escape.escape(title or "Artifact")
    who = " \u00b7 ".join(
        html_escape.escape(part) for part in (assistant, device) if part
    ) or "arnold"
    now = datetime.now()
    return _SHELL.format(
        css=_CSS,
        palette=html_escape.escape(palette if palette in ("steel", "gold") else "steel"),
        title=clean_title,
        eyebrow=who,
        stamp=now.strftime("%d %b %Y  %H:%M"),
        ref=now.strftime("%y%m%d.%H%M"),
        body=body_html,
        footer=who,
    )


def _identity(ctx: CommandContext) -> tuple[str, str]:
    """Whose page this is: the assistant's name and the palette it wears."""
    config = ctx.config
    assistant = getattr(config, "assistant", None)
    if assistant is None:
        return "", "steel"
    return config.assistant_title(), config.face_palette()


def _body_from(args: dict) -> str:
    """Turn whatever the model supplied into HTML for the page body."""
    raw_html = str(args.get("html") or "").strip()
    if raw_html:
        return _unwrap_document(raw_html) if _is_full_document(raw_html) else raw_html

    text = str(args.get("markdown") or args.get("text") or "").strip()
    if not text:
        raise CommandError("Tell me what to put in it - some HTML or markdown.")

    import markdown

    return markdown.markdown(
        text,
        extensions=["fenced_code", "tables", "sane_lists", "nl2br"],
        output_format="html",
    )


def _is_full_document(page: str) -> bool:
    return bool(re.match(r"(?is)^\s*(<!doctype|<html\b)", page))


_HEAD_RE = re.compile(r"(?is)<head[^>]*>(.*?)</head>")
_BODY_RE = re.compile(r"(?is)<body[^>]*>(.*?)</body>")
_STYLE_RE = re.compile(r"(?is)<style[^>]*>(.*?)</style>")
_SCRIPT_RE = re.compile(r"(?is)<script\b.*?</script>")
# A selector list starting with html/body, wherever a rule may start: the top
# level, inside a block (@media), or after a comma.
_PAGE_SELECTOR_RE = re.compile(r"(?i)(^|[{};,])(\s*)(?:html|body)(?=\s*[,{])")


def _unwrap_document(document: str) -> str:
    """Fold a complete page into a fragment the shell can style.

    The model regularly returns a whole document with a white background and
    black text, which then arrives on screen looking nothing like the rest of
    the assistant. Rather than refuse it, its head styles and scripts are
    lifted out, its body-level rules are rescoped to the panel, and the whole
    thing is dropped into the shell. What it wrote still wins over the theme -
    it is later in the cascade - but it inherits the palette it forgot to ask
    for.
    """
    head = _HEAD_RE.search(document)
    body = _BODY_RE.search(document)

    pieces: list[str] = []
    if head:
        inside = head.group(1)
        pieces += [
            f"<style>{_rescope_css(css)}</style>" for css in _STYLE_RE.findall(inside)
        ]
        pieces += _SCRIPT_RE.findall(inside)

    if body:
        pieces.append(body.group(1))
    else:
        # No <body> tag: strip the wrapper and any <head> and keep the rest.
        remainder = _HEAD_RE.sub("", document)
        remainder = re.sub(r"(?is)</?(?:!doctype[^>]*|html[^>]*)>", "", remainder)
        pieces.append(remainder)

    pieces.append(_GUARD)
    return "\n".join(part.strip() for part in pieces if part.strip())


def _rescope_css(style_block: str) -> str:
    """Point `html` / `body` rules at the panel instead of the page."""
    return _PAGE_SELECTOR_RE.sub(r"\1\2.frag", style_block)


# -- showing -----------------------------------------------------------------


def _show(ctx: CommandContext, path: Path) -> None:
    """Open the artifact in the browser and bring it forward."""
    import os

    browser = _browser_path(ctx, "")
    url = path.resolve().as_uri()  # file:///C:/...
    foreground.allow_foreground()

    from .. import process

    if browser is not None:
        process.launch([str(browser), url])
        target = browser.name.lower()
    else:
        target = _default_browser_exe()
        try:
            os.startfile(str(path))
        except AttributeError:  # not Windows
            import webbrowser

            webbrowser.open(url)

    if target:
        threading.Thread(
            target=foreground.raise_process_windows,
            args=({target},),
            daemon=True,
            name="raise-artifact",
        ).start()


# -- handlers ----------------------------------------------------------------


def _create(ctx: CommandContext, args: dict) -> CommandResult:
    title = str(args.get("title") or "").strip() or "Artifact"
    supplied = str(args.get("html") or "").strip()

    if arg_bool(args, "raw", False) and _is_full_document(supplied):
        page = supplied  # an explicit request for a page with no chrome at all
    else:
        name, palette = _identity(ctx)
        page = _render(title, _body_from(args), ctx.device_name, name, palette)

    if len(page.encode("utf-8")) > MAX_BYTES:
        raise CommandError("That page is too large to write out.")

    directory = artifacts_dir(ctx)
    name = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{_slug(title)}.html"
    path = directory / name
    path.write_text(page, encoding="utf-8")
    _prune(directory)
    log.info("wrote artifact %s (%d bytes)", name, len(page))

    shown = arg_bool(args, "open", True)
    if shown:
        _show(ctx, path)

    return CommandResult(
        speech=(
            f"{title} is on your screen."
            if shown
            else f"I've saved {title} on {ctx.device_name}."
        ),
        result={"name": name, "path": str(path), "bytes": len(page), "opened": shown},
    )


def _open_existing(ctx: CommandContext, args: dict) -> CommandResult:
    directory = artifacts_dir(ctx)
    wanted = str(args.get("name") or "").strip()

    if wanted:
        # Never let a name escape the artifacts directory.
        candidate = (directory / Path(wanted).name).resolve()
        if candidate.parent != directory.resolve() or not candidate.exists():
            raise CommandError(f"I can't find an artifact called {wanted}.")
        path = candidate
    else:
        files = sorted(directory.glob("*.html"), key=made_at)
        if not files:
            raise CommandError("I haven't made any artifacts yet.")
        path = files[-1]

    _show(ctx, path)
    return CommandResult(
        speech=f"Opening {path.stem.split('-', 2)[-1].replace('-', ' ')}.",
        result={"name": path.name, "path": str(path)},
    )


def made_at(path: Path) -> datetime:
    """When a page was made: the stamp in its name, which a copy or a move
    keeps, unlike the file's mtime. The mtime only for a name without one."""
    try:
        return datetime.strptime(path.name[:15], "%Y%m%d-%H%M%S")
    except ValueError:
        return datetime.fromtimestamp(path.stat().st_mtime)


def _list(ctx: CommandContext, args: dict) -> CommandResult:
    files = sorted(artifacts_dir(ctx).glob("*.html"), key=made_at, reverse=True)
    items = [
        {
            "name": p.name,
            "title": p.stem.split("-", 2)[-1].replace("-", " "),
            "modified": made_at(p).isoformat(timespec="seconds"),
        }
        for p in files[:40]
    ]
    if not items:
        return CommandResult(speech="I haven't made any artifacts yet.", result={"artifacts": []})
    return CommandResult(
        speech=f"There are {len(files)}, most recently {items[0]['title']}.",
        result={"artifacts": items, "count": len(files)},
    )
