"""Live control server for the `scoreboard` GDN app.

Runs alongside `gdn studio`/`gdn preview` (a separate process, separate port).
It owns the game state — scores, period, and a start/stop game clock — and
exposes:

  GET  /            a control panel (buttons) you drive from a browser tab
  GET  /api/state   current state as JSON (polled by app.star every render)
  GET  /render.png  the same state drawn as a 128x32 PNG, with its own tiny
                     pixel font (pixel_font.py) — no dependency on gdn/ or
                     the Starlark sandbox, so this file + pixel_font.py are
                     the entire deployment. Point a Glance panel's "Private
                     App" at this URL (see the module docstring in
                     pixel_font.py / DEPLOY.md) to show live scores on real
                     hardware.
  POST /api/...      the actions the control panel buttons call

Nothing but this process ever mutates state, so it stays consistent no
matter how many panels/browser tabs are reading it.

Run it with:  python apps/scoreboard/control_server.py [port]

Deploying this somewhere public (so a real panel's render service can reach
/api/state, and so you can drive the game from your phone) means anyone with
the URL could otherwise change your score. Set the CONTROL_PASSWORD
environment variable before starting the process to require HTTP basic auth
(any username, that password) on the control page and every state-changing
endpoint. GET /api/state stays open unauthenticated — the render service
fetches it with no credentials, and it's read-only score data anyway. With
CONTROL_PASSWORD unset (the local-dev default), no auth is required.
"""
from __future__ import annotations

import functools
import hmac
import io
import json
import os
import sys
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, request
from PIL import Image, ImageDraw

import pixel_font

STATE_PATH = Path(__file__).resolve().parent / "state.json"
PANEL_WIDTH = 128
PANEL_HEIGHT = 32

# Quick-select period *types* (the counter reads "{period_type} {period_number}",
# e.g. "QUARTER 3") and one-click *overrides* (a literal string that replaces the
# type+number display until the next type/number change resumes it). Both lists
# are just what the control panel buttons offer — /api/period/type and
# /api/period/override also take arbitrary custom text.
PERIOD_TYPE_PRESETS = ["QUARTER", "HALF", "PERIOD", "INNING"]
PERIOD_OVERRIDE_PRESETS = ["PREGAME", "HALFTIME", "OT", "OT2", "SHOOTOUT", "FINAL"]

DEFAULT_STATE = {
    "home": "HOME",
    "away": "AWAY",
    "home_score": 0,
    "away_score": 0,
    "period_type": "QUARTER",
    "period_number": 1,
    "period": "QUARTER 1",      # the literal text drawn on the panel
    "clock_seconds": 12 * 60,   # frozen remaining seconds when not running
    "running": False,
    "last_tick": 0.0,           # epoch time clock last started running
}

_lock = threading.Lock()


def _load() -> dict:
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return {**DEFAULT_STATE, **raw}
    except (OSError, ValueError):
        return dict(DEFAULT_STATE)


def _save(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass  # a read-only disk shouldn't break live control


_state = _load()


def _remaining_locked() -> int:
    """Seconds left right now, without mutating _state."""
    secs = _state["clock_seconds"]
    if _state["running"]:
        secs -= time.time() - _state["last_tick"]
    return max(0, int(round(secs)))


def _freeze_locked() -> None:
    """Fold elapsed running time into clock_seconds and stop. Caller holds _lock."""
    if _state["running"]:
        _state["clock_seconds"] = _remaining_locked()
        _state["running"] = False


def _public_state_locked() -> dict:
    remaining = _remaining_locked()
    if remaining <= 0 and _state["running"]:
        _freeze_locked()
        remaining = 0
    return {
        "home": _state["home"],
        "away": _state["away"],
        "home_score": _state["home_score"],
        "away_score": _state["away_score"],
        "period": _state["period"],
        "period_type": _state["period_type"],
        "period_number": _state["period_number"],
        "running": _state["running"],
        "clock_minutes": remaining // 60,
        "clock_seconds": remaining % 60,
        "clock_total_seconds": remaining,
    }


def _render_png(st: dict) -> bytes:
    """Draw `st` (a _public_state_locked() dict) as a 128x32 PNG, laid out to
    match the app.star Studio preview: team names + period on top, big
    scores flanking a center clock, LIVE/PAUSED at the bottom."""
    img = Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), (0, 0, 0))
    d = ImageDraw.Draw(img)

    home = str(st["home"]).upper()[:5]
    away = str(st["away"]).upper()[:5]
    period = str(st["period"]).upper()[:10]
    clock_str = "%d:%s" % (st["clock_minutes"], str(st["clock_seconds"]).zfill(2))
    running = bool(st["running"])

    pixel_font.draw_text(d, home, 2, 1, scale=1, color=(255, 204, 51))
    pixel_font.draw_text(d, away, PANEL_WIDTH - 2, 1, scale=1,
                         color=(51, 204, 255), align="right")
    if period:
        pixel_font.draw_text(d, period, PANEL_WIDTH // 2, 1, scale=1,
                             color=(150, 150, 150), align="center")

    pixel_font.draw_text(d, str(st["home_score"]), 2, 11, scale=2, color=(255, 255, 255))
    pixel_font.draw_text(d, str(st["away_score"]), PANEL_WIDTH - 2, 11, scale=2,
                         color=(255, 255, 255), align="right")

    clock_color = (76, 175, 80) if running else (229, 115, 115)
    pixel_font.draw_text(d, clock_str, PANEL_WIDTH // 2, 13, scale=1,
                         color=clock_color, align="center")

    dot_color = (76, 175, 80) if running else (229, 115, 115)
    d.ellipse([4, 24, 8, 28], fill=dot_color)
    pixel_font.draw_text(d, "LIVE" if running else "PAUSED", 11, 25, scale=1,
                         color=dot_color)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


app = Flask(__name__)

CONTROL_PASSWORD = os.environ.get("CONTROL_PASSWORD", "")


def require_auth(view):
    """HTTP basic auth, enforced only when CONTROL_PASSWORD is set. Any
    username is accepted — only the password is checked, with a
    constant-time comparison so response timing can't leak it."""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not CONTROL_PASSWORD:
            return view(*args, **kwargs)
        auth = request.authorization
        if not auth or not hmac.compare_digest(auth.password or "", CONTROL_PASSWORD):
            return Response(
                "Authentication required", 401,
                {"WWW-Authenticate": 'Basic realm="Scoreboard control"'})
        return view(*args, **kwargs)
    return wrapped


@app.get("/api/state")
def api_state():
    with _lock:
        return jsonify(_public_state_locked())


@app.get("/render.png")
def render_png():
    with _lock:
        png = _render_png(_public_state_locked())
    return Response(png, mimetype="image/png",
                    headers={"Cache-Control": "no-store"})


@app.post("/api/score")
@require_auth
def api_score():
    body = request.get_json(silent=True) or {}
    team = body.get("team")
    delta = int(body.get("delta", 0))
    if team not in ("home", "away"):
        return jsonify(error="team must be 'home' or 'away'"), 400
    with _lock:
        key = f"{team}_score"
        _state[key] = max(0, _state[key] + delta)
        _save(_state)
        return jsonify(_public_state_locked())


@app.post("/api/period/type")
@require_auth
def api_period_type():
    """Set the period *type* word (QUARTER, HALF, PERIOD, INNING, or any
    custom text) and recompute the displayed period as "{type} {number}"."""
    body = request.get_json(silent=True) or {}
    value = str(body.get("value", "")).strip().upper()[:12]
    if not value:
        return jsonify(error="value required"), 400
    with _lock:
        _state["period_type"] = value
        _state["period"] = f"{value} {_state['period_number']}"
        _save(_state)
        return jsonify(_public_state_locked())


@app.post("/api/period/number")
@require_auth
def api_period_number():
    """+1/-1 the period counter (floor of 1) and recompute the displayed
    period as "{type} {number}" — this resumes normal play, replacing
    whatever override (halftime, OT, ...) may currently be showing."""
    body = request.get_json(silent=True) or {}
    delta = int(body.get("delta", 0))
    with _lock:
        _state["period_number"] = max(1, _state["period_number"] + delta)
        _state["period"] = f"{_state['period_type']} {_state['period_number']}"
        _save(_state)
        return jsonify(_public_state_locked())


@app.post("/api/period/override")
@require_auth
def api_period_override():
    """Set the displayed period to an arbitrary literal string (used by both
    the OT/HALFTIME/etc. preset buttons and the manual text field). Doesn't
    touch period_type/period_number, so +1/-1 later resumes the count from
    wherever it left off."""
    body = request.get_json(silent=True) or {}
    value = str(body.get("value", "")).strip().upper()[:16]
    if not value:
        return jsonify(error="value required"), 400
    with _lock:
        _state["period"] = value
        _save(_state)
        return jsonify(_public_state_locked())


@app.post("/api/clock/start")
@require_auth
def api_clock_start():
    with _lock:
        if not _state["running"]:
            _state["running"] = True
            _state["last_tick"] = time.time()
            _save(_state)
        return jsonify(_public_state_locked())


@app.post("/api/clock/stop")
@require_auth
def api_clock_stop():
    with _lock:
        _freeze_locked()
        _save(_state)
        return jsonify(_public_state_locked())


@app.post("/api/clock/set")
@require_auth
def api_clock_set():
    body = request.get_json(silent=True) or {}
    minutes = int(body.get("minutes", 0))
    seconds = int(body.get("seconds", 0))
    with _lock:
        _state["clock_seconds"] = max(0, minutes * 60 + seconds)
        _state["running"] = False
        _save(_state)
        return jsonify(_public_state_locked())


@app.post("/api/clock/adjust")
@require_auth
def api_clock_adjust():
    body = request.get_json(silent=True) or {}
    delta = int(body.get("seconds", 0))
    with _lock:
        remaining = _remaining_locked()
        new_remaining = max(0, remaining + delta)
        _state["clock_seconds"] = new_remaining
        _state["last_tick"] = time.time()
        _save(_state)
        return jsonify(_public_state_locked())


@app.post("/api/teams")
@require_auth
def api_teams():
    body = request.get_json(silent=True) or {}
    with _lock:
        if body.get("home"):
            _state["home"] = str(body["home"])[:5]
        if body.get("away"):
            _state["away"] = str(body["away"])[:5]
        _save(_state)
        return jsonify(_public_state_locked())


@app.post("/api/reset")
@require_auth
def api_reset():
    global _state
    with _lock:
        home, away = _state["home"], _state["away"]
        _state = dict(DEFAULT_STATE)
        _state["home"], _state["away"] = home, away
        _save(_state)
        return jsonify(_public_state_locked())


CONTROL_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Scoreboard Control</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { color-scheme: dark; }
  body { font-family: -apple-system, Segoe UI, sans-serif; background: #111;
         color: #eee; margin: 0; padding: 16px; }
  h1 { font-size: 18px; margin: 0 0 16px; color: #999; }
  .teams { display: flex; gap: 12px; }
  .team { flex: 1; background: #1b1b1b; border-radius: 10px; padding: 14px;
          text-align: center; }
  .team h2 { margin: 0 0 8px; font-size: 15px; }
  .team.home h2 { color: #ffcc33; }
  .team.away h2 { color: #33ccff; }
  .score { font-size: 48px; font-weight: 700; margin: 4px 0 10px; }
  .row { display: flex; gap: 8px; justify-content: center; }
  button { font-size: 18px; padding: 10px 16px; border-radius: 8px; border: none;
           background: #333; color: #eee; cursor: pointer; }
  button:active { background: #555; }
  button.big { font-size: 22px; padding: 14px 22px; }
  .clock-panel { margin-top: 18px; background: #1b1b1b; border-radius: 10px;
                 padding: 14px; text-align: center; }
  .clock { font-size: 56px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .clock.running { color: #4caf50; }
  .clock.stopped { color: #e57373; }
  .status { font-size: 13px; color: #999; margin-top: 4px; }
  .period-panel { margin-top: 18px; background: #1b1b1b; border-radius: 10px;
                  padding: 14px; }
  .period-panel h3 { margin: 0 0 10px; font-size: 13px; color: #999;
                      text-transform: uppercase; letter-spacing: .05em; }
  .presets { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; }
  .presets button.active { background: #4caf50; color: #111; }
  .period-display { font-size: 22px; font-weight: 700; min-width: 140px;
                     text-align: center; }
  .textrow { margin-top: 10px; display: flex; gap: 8px; }
  .textrow input { flex: 1; background: #222; border: 1px solid #444; color: #eee;
                   border-radius: 6px; padding: 8px; min-width: 0; }
  .numinput { width: 56px; background: #222; border: 1px solid #444; color: #eee;
              border-radius: 6px; padding: 8px; text-align: center; font-size: 16px; }
  .rename { margin-top: 18px; display: flex; gap: 8px; }
  .rename input { flex: 1; background: #222; border: 1px solid #444; color: #eee;
                  border-radius: 6px; padding: 8px; }
  .footer { margin-top: 20px; color: #666; font-size: 12px; }
</style>
</head>
<body>
<h1>Scoreboard Control</h1>

<div class="teams">
  <div class="team home">
    <h2 id="home-name">HOME</h2>
    <div class="score" id="home-score">0</div>
    <div class="row">
      <button class="big" onclick="score('home',-1)">-1</button>
      <button class="big" onclick="score('home',1)">+1</button>
      <button onclick="score('home',2)">+2</button>
      <button onclick="score('home',3)">+3</button>
    </div>
  </div>
  <div class="team away">
    <h2 id="away-name">AWAY</h2>
    <div class="score" id="away-score">0</div>
    <div class="row">
      <button class="big" onclick="score('away',-1)">-1</button>
      <button class="big" onclick="score('away',1)">+1</button>
      <button onclick="score('away',2)">+2</button>
      <button onclick="score('away',3)">+3</button>
    </div>
  </div>
</div>

<div class="clock-panel">
  <div class="clock" id="clock">12:00</div>
  <div class="status" id="clock-status">stopped</div>
  <div class="row" style="margin-top:12px">
    <button class="big" onclick="clockStart()">START</button>
    <button class="big" onclick="clockStop()">STOP</button>
    <button onclick="clockAdjust(-60)">-1:00</button>
    <button onclick="clockAdjust(-10)">-10s</button>
    <button onclick="clockAdjust(10)">+10s</button>
    <button onclick="clockAdjust(60)">+1:00</button>
  </div>
  <div class="row" style="margin-top:10px">
    <button onclick="clockSet(12,0)">Set 12:00</button>
    <button onclick="clockSet(10,0)">Set 10:00</button>
    <button onclick="clockSet(1,0)">Set 1:00</button>
    <button onclick="clockSet(0,0)">Set 0:00</button>
  </div>
  <div class="row" style="margin-top:10px; align-items:center; gap:6px">
    <input id="clock-min-input" class="numinput" type="number" min="0" max="99" placeholder="MM">
    <span>:</span>
    <input id="clock-sec-input" class="numinput" type="number" min="0" max="59" placeholder="SS">
    <button onclick="setCustomClock()">Set clock</button>
  </div>
</div>

<div class="period-panel">
  <h3>Period type</h3>
  <div class="presets" id="type-presets"></div>
  <div class="textrow">
    <input id="period-type-input" placeholder="Custom type, e.g. SET" maxlength="12">
    <button onclick="setCustomType()">Use</button>
  </div>

  <h3 style="margin-top:16px">Counter</h3>
  <div class="row" style="align-items:center; gap:16px">
    <button class="big" onclick="periodNumber(-1)">-1</button>
    <div class="period-display" id="period-display">QUARTER 1</div>
    <button class="big" onclick="periodNumber(1)">+1</button>
  </div>

  <h3 style="margin-top:16px">Overrides</h3>
  <div class="presets" id="override-presets"></div>
  <div class="textrow">
    <input id="period-override-input" placeholder="Custom period text..." maxlength="16">
    <button onclick="setCustomOverride()">Set</button>
  </div>
</div>

<div class="rename">
  <input id="home-input" placeholder="Home team name" maxlength="5">
  <input id="away-input" placeholder="Away team name" maxlength="5">
  <button onclick="renameTeams()">Rename</button>
</div>

<div class="row" style="margin-top:18px">
  <button onclick="resetAll()">Reset game</button>
</div>

<div class="footer">Polls /api/state every second · app.star reads the same
endpoint on its own refresh, so the LED preview follows this panel.</div>

<script>
const TYPE_PRESETS = ["QUARTER","HALF","PERIOD","INNING"];
const typePresetsEl = document.getElementById('type-presets');
TYPE_PRESETS.forEach(p => {
  const b = document.createElement('button');
  b.textContent = p[0] + p.slice(1).toLowerCase();
  b.onclick = () => periodType(p);
  b.dataset.periodType = p;
  typePresetsEl.appendChild(b);
});

const OVERRIDE_PRESETS = ["PREGAME","HALFTIME","OT","OT2","SHOOTOUT","FINAL"];
const overridePresetsEl = document.getElementById('override-presets');
OVERRIDE_PRESETS.forEach(p => {
  const b = document.createElement('button');
  b.textContent = p[0] + p.slice(1).toLowerCase();
  b.onclick = () => periodOverride(p);
  b.dataset.override = p;
  overridePresetsEl.appendChild(b);
});

async function post(url, body) {
  const r = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body || {}),
  });
  render(await r.json());
}

function score(team, delta) { post('/api/score', {team, delta}); }
function clockStart() { post('/api/clock/start'); }
function clockStop() { post('/api/clock/stop'); }
function clockAdjust(seconds) { post('/api/clock/adjust', {seconds}); }
function clockSet(minutes, seconds) { post('/api/clock/set', {minutes, seconds}); }
function setCustomClock() {
  const minutes = parseInt(document.getElementById('clock-min-input').value, 10) || 0;
  const seconds = parseInt(document.getElementById('clock-sec-input').value, 10) || 0;
  clockSet(minutes, seconds);
}
function periodType(value) { post('/api/period/type', {value}); }
function periodNumber(delta) { post('/api/period/number', {delta}); }
function periodOverride(value) { post('/api/period/override', {value}); }
function setCustomType() {
  const v = document.getElementById('period-type-input').value.trim();
  if (v) post('/api/period/type', {value: v});
}
function setCustomOverride() {
  const v = document.getElementById('period-override-input').value.trim();
  if (v) post('/api/period/override', {value: v});
}
function renameTeams() {
  const home = document.getElementById('home-input').value.trim();
  const away = document.getElementById('away-input').value.trim();
  post('/api/teams', {home, away});
}
function resetAll() {
  if (confirm('Reset scores, period, and clock?')) post('/api/reset');
}

function render(s) {
  document.getElementById('home-name').textContent = s.home;
  document.getElementById('away-name').textContent = s.away;
  document.getElementById('home-score').textContent = s.home_score;
  document.getElementById('away-score').textContent = s.away_score;
  document.getElementById('clock').textContent =
    s.clock_minutes + ':' + String(s.clock_seconds).padStart(2, '0');
  const clockEl = document.getElementById('clock');
  clockEl.classList.toggle('running', s.running);
  clockEl.classList.toggle('stopped', !s.running);
  document.getElementById('clock-status').textContent = s.running ? 'running' : 'stopped';
  document.getElementById('period-display').textContent = s.period;
  [...typePresetsEl.children].forEach(b => b.classList.toggle('active', b.dataset.periodType === s.period_type));
  [...overridePresetsEl.children].forEach(b => b.classList.toggle('active', b.dataset.override === s.period));
}

async function poll() {
  const r = await fetch('/api/state');
  render(await r.json());
}
poll();
setInterval(poll, 1000);
</script>
</body>
</html>
"""


@app.get("/")
@require_auth
def control_page():
    return CONTROL_PAGE


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 8787))
    # 127.0.0.1 for local dev (matches app.star's default `server` input);
    # deployments (Render etc.) set HOST=0.0.0.0 so the platform can reach it.
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"Scoreboard control panel: http://{host}:{port}/")
    print(f"State API for app.star:   http://{host}:{port}/api/state")
    if CONTROL_PASSWORD:
        print("CONTROL_PASSWORD is set — control page and POST endpoints require basic auth.")
    else:
        print("CONTROL_PASSWORD is not set — control endpoints are open. Set it before deploying publicly.")
    app.run(host=host, port=port, threaded=True)
