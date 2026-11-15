# Changelog

All notable changes to AC Dash. Versions follow [Semantic Versioning](https://semver.org/):
a major bump means you may need to change something on your side, a minor bump adds features,
a patch bump only fixes things.

Docker images: `ghcr.io/ajankuv/acdash:<version>` (for example `:1.0.0`), plus `:1.0` and `:1`,
which always point at the newest release in that line. `:latest` follows `main`.

## [1.0.0] - 2026-11-15

First versioned release. Earlier builds were published only as `:latest` and commit tags.

**What AC Dash does**
- Live temperature, humidity, VPD and port states for all your AC Infinity UIS controllers,
  with trend arrows, fan speed bars, grow stage labels and a fullscreen/TV mode.
- Local history: one reading per controller every minute in SQLite, kept indefinitely, with
  gaps filled from AC Infinity's cloud history (up to 30 days back by default).
- Port control for every mode (Manual, Auto, VPD, Cycle, Schedule, Timer, Off). Every change
  is checked against the controller's live state before it is reported as done.
- Automations: list, turn on/off, delete and create time-window programs. Every change is
  confirmed against AC Infinity, and one change runs at a time.
- `/health` for liveness and `/status` for data freshness (503 when data is stale).

**New in this release**
- The app shows its version and build in the page footer and in `/status`
  (`"version"` and `"revision"`).
- Releases now get version tags, Docker image tags (`:1.0.0`, `:1.0`, `:1`) and GitHub Release
  notes taken from this file.
- Updated dependencies: FastAPI 0.162.5, Uvicorn 0.54.0, python-dotenv 1.2.4,
  python-multipart 0.0.33 and newer pinned GitHub Actions.
- The CI test server now runs on the same Python 3.12 Alpine base as the app.

**Upgrading:** nothing to change. Settings, credentials and history in `/app/data` carry over.
