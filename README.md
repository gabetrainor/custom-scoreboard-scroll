# custom-scoreboard-scroll

A live-controlled two-team scoreboard for a Glance LED panel: scores, a
start/stop game clock, and a configurable period (type + counter, or a
one-click override like OT/Halftime/Final), driven from a phone.

- `GET /render.png` — the current state as a 128x32 PNG. Point a Glance
  panel's Private App at this URL.
- `GET /render64.png` — the same state as a 64x32 PNG, for a second panel
  (Custom Scoreboard Classic) running the same game.
- `GET /` — the control panel (buttons). Protect with `CONTROL_PASSWORD`.
- `GET /api/state` — current state as JSON (unauthenticated, read-only).
- `POST /api/score`, `/api/score/set`, `/api/period/type`,
  `/api/period/number`, `/api/period/override`,
  `/api/clock/start|stop|set|adjust`, `/api/teams`, `/api/reset` —
  mutating actions, require basic auth once `CONTROL_PASSWORD` is set.

Run locally: `python control_server.py [port]` (defaults to 8787, no auth).

Deploy: `gunicorn control_server:app --bind 0.0.0.0:$PORT --workers 1`,
with `CONTROL_PASSWORD` set as an environment variable. Must stay at 1
worker — game state lives in that process's memory.

No dependency on any Glance SDK code — self-contained (Flask + Pillow).
