---
id: 0004
title: Auto-update mechanism (check PyPI / npm for new versions)
role: builder
priority: P2
owner:
started:
status: inbox
depends_on: []
---

## Goal

On boot and on a cron, check whether a newer telechat (PyPI) or npm
package is published. If yes, notify the bot operator and optionally
auto-upgrade in a controlled way.

## Why it matters

Operators currently have no signal that they're behind. With the
self-improving architecture pushing improvements often, this matters.

## Acceptance criteria

- [ ] New `telechat_pkg/updater.py` with `check_for_updates()` and
      optional `apply_update()`.
- [ ] Reads current version from `telechatai.egg-info` / `pyproject`.
- [ ] Queries PyPI JSON API and npm registry for latest.
- [ ] Logs result and emits an event on `event_bus` so other channels
      (Telegram admin DM, health endpoint) can surface it.
- [ ] Config: `UPDATE_CHECK_INTERVAL` env (default 24h), opt-in
      `UPDATE_AUTO_APPLY` (default false).
- [ ] Tests: version compare logic, registry response parsing (mocked),
      no auto-apply unless flag set.
- [ ] `pytest -q` green.
- [ ] `docs/implementation-tracker.md` row flipped to Done.

## Likely files / surfaces touched

- `telechat_pkg/updater.py` (new)
- `telechat_pkg/health.py` (surface update status in /health)
- `telechat_pkg/event_bus.py` (publish update event)
- `tests/test_updater.py` (new)

## Notes

Don't auto-restart in the same process — emit an event, let the
operator (or watchdog policy) decide. Respect existing circuit-breaker
patterns; don't hammer registries on retry.
