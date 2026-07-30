---
id: 0023
title: Live-stream bound Desktop/CLI session turns with tool-action progress
role: builder
priority: P1
owner:
started:
status: inbox
depends_on: []
touches:
  - telechat_pkg/desktop_bridge.py
  - tests/test_desktop_bridge.py
  - docs/advanced-telegram-features.md
---

## Goal

When you send a message to a bound Desktop/CLI session, `_run_resume_background`
spawns `claude --resume … -p … --output-format text`, blocks up to 15 minutes,
then posts a single digest card. The user sees nothing until it finishes. Stream
the turn instead: use `--output-format stream-json` and edit one Telegram card in
place as Claude emits text and tool actions, the way the normal chat path already
does with its `TaskSession` tracker.

## Why it matters

This is the single biggest interactivity gap. A long Desktop task currently looks
frozen from Telegram. Live progress ("🔧 Bash · pytest", streaming text) makes the
phone feel like a real terminal into the session and is the foundation interrupt
(0024) and follow-up actions (0025) build on.

## Acceptance criteria

- [ ] `_run_resume_background` (or a new `_stream_resume_background`) invokes
      `claude --resume <sid> -p <msg> --output-format stream-json --verbose` and
      parses the JSONL stream incrementally
- [ ] A single Telegram card is edited in place as the turn progresses: assistant
      text appended, tool_use rendered as `🔧 <tool> · <detail>` lines, rate-limited
      to avoid Telegram 429s (reuse the existing edit-throttle pattern, ~2–4s)
- [ ] Final state shows the completed reply (digest card behavior preserved for
      long output, with the [📄 Full output] button)
- [ ] Falls back to the current text-mode blocking path if `stream-json` parsing
      fails, so a CLI version without stream-json still works
- [ ] Tests: stream parsing (text + tool_use + result events from a fixture JSONL),
      throttled-edit logic, fallback path. `pytest -q` green
- [ ] `docs/advanced-telegram-features.md` updated

## Likely files / surfaces touched

See `touches:`. The JSONL event shapes mirror what `_follow_format_entry` already
parses for follow-mode — reuse that rendering. The edit-throttle pattern lives in
`telegram_bot.py`'s placeholder updater; port the idea, don't import the bot.

## Notes

Keep the subprocess handle accessible (instance/dict keyed by session) so 0024 can
terminate it. Do not block the asyncio loop — the subprocess + stream reader run in
the existing background thread; card edits go through `_tg_send`/edit HTTP calls.
