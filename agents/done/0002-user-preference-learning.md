---
id: 0002
title: User preference learning
role: builder
priority: P2
owner: opus-features
started: 2026-06-03
status: done
depends_on: []
touches:
  - telechat_pkg/preferences.py
  - telechat_pkg/claude_core.py
  - telechat_pkg/feedback.py
  - telechat_pkg/telegram_bot.py
  - telechat_pkg/whatsapp_bot.py
  - tests/test_preferences.py
---

## Goal

Track per-user style preferences (response length, format, tone) over
time and bias future responses accordingly.

## Why it matters

Users who consistently ask for "short answers" or "with code blocks"
shouldn't have to repeat themselves. Closes the loop between feedback
and behavior.

## Acceptance criteria

- [ ] New `telechat_pkg/preferences.py` exposing `get_user_prefs(uid)`
      and `record_signal(uid, signal)`.
- [ ] Schema: `user_preferences` table (uid, dimension, value,
      confidence, updated_at).
- [ ] Signals captured from: explicit feedback (/feedback text),
      `/rate` ratings combined with response shape, and explicit
      `/prefer` command.
- [ ] System prompt assembly reads prefs and injects style hints.
- [ ] Tests: signal capture, decay/aggregation, prompt injection.
- [ ] `pytest -q` green.
- [ ] `docs/implementation-tracker.md` row flipped to Done.

## Likely files / surfaces touched

- `telechat_pkg/preferences.py` (new)
- `telechat_pkg/claude_core.py` (schema)
- `telechat_pkg/feedback.py` (signal hook)
- `telechat_pkg/telegram_bot.py` and `whatsapp_bot.py` (`/prefer`)
- `tests/test_preferences.py` (new)

## Notes

Keep dimensions small at first: `length` (short/medium/long), `format`
(plain/markdown/code-heavy), `tone` (formal/casual). Decay confidence
over time so stale prefs don't ossify.

## Outcome — 2026-06-03

Shipped `telechat_pkg/preferences.py`: confidence-weighted prefs across
length/format/tone with exponential decay (30-day half-life) and a
`_MIN_CONFIDENCE` floor; `record_signal`, `get_user_prefs`, `prompt_hint`,
`/prefer` parsing, and free-text signal mining (`infer_signals_from_text`).
Table `user_preferences` owned by the module (lazy create, like commitments.py).
Wired into Telegram: `/prefer` (explicit, strong weight) and `/feedback` (mines
text) commands; `_select_system_prompt` injects the style hint into the system
prompt for all engines. `tests/test_preferences.py` 22 tests, 100% coverage.
Deferred (noted, not done): WhatsApp `/prefer` parity and the `/rate`+response-
shape signal source — the current bot has no `/rate` pipeline to hook.
