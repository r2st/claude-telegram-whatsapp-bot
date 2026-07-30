---
id: 0024
title: Interrupt a running Desktop/CLI turn from Telegram (Stop button)
role: builder
priority: P1
owner:
started:
status: inbox
depends_on: [0023]
touches:
  - telechat_pkg/desktop_bridge.py
  - tests/test_desktop_bridge.py
---

## Goal

A bound-session turn runs as a background `claude --resume` subprocess with a
15-minute timeout and no way to stop it. Add a `⏹ Stop` button on the in-flight
card that terminates the subprocess (and its process group) and reports the turn
as cancelled.

## Why it matters

Interactivity means control, not just observation. If Claude heads down the wrong
path on a long task you should be able to kill it from your phone instead of
waiting out a 15-minute timeout. Pairs directly with the streaming card from 0023.

## Acceptance criteria

- [ ] In-flight cards carry a `⏹ Stop` inline button with a `bridge:stop:<token>`
      callback that maps to the running subprocess
- [ ] Tapping Stop terminates the process group (SIGTERM, then SIGKILL after a short
      grace) so child tool processes die too — not just the parent
- [ ] The card updates to a clear "⏹ Cancelled after Ns" final state; any partial
      streamed output is preserved
- [ ] Stopping an already-finished/unknown turn is a no-op with a friendly message
      (no traceback, no stale-handle crash)
- [ ] `try_handle_callback` routes the new `bridge:stop:` prefix
- [ ] Tests: stop terminates a fake long-running process, process-group kill is
      requested, double-stop / unknown-token is safe. `pytest -q` green

## Likely files / surfaces touched

See `touches:`. Reuses the subprocess-handle registry introduced in 0023.

## Notes

Spawn the subprocess with `start_new_session=True` (its own process group) so the
group kill works. Mirror the cancel UX from the normal-chat `TaskSession`
(`⏹ Task cancelled`) for consistency. Keep a small bounded registry of in-flight
turns keyed by an opaque token (don't put raw pids in callback_data).
