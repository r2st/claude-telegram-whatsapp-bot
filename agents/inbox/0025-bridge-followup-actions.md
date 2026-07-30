---
id: 0025
title: Context-aware follow-up action buttons after a Desktop/CLI turn
role: builder
priority: P2
owner:
started:
status: inbox
depends_on: []
touches:
  - telechat_pkg/desktop_bridge.py
  - tests/test_desktop_bridge.py
---

## Goal

After a bound-session turn completes, the reply card is read-only. Add a row of
one-tap follow-up buttons that send a canned prompt or Claude slash-command back to
the same session: `▶️ Continue`, `🧪 Run tests`, `❓ Explain`, `🗜 /compact`,
`↩️ Undo last edit`. Make the set context-aware (e.g. offer "Run tests" only when
the turn touched code, "Undo" only when it ran an Edit/Write).

## Why it matters

The most common follow-ups become one tap instead of typing on a phone keyboard.
This is where the bridge starts to feel like an interactive remote control rather
than a notification feed.

## Acceptance criteria

- [ ] Completed-turn cards render a context-aware action-button row
- [ ] Buttons use a `bridge:act:<verb>:<sid8>` callback that dispatches the mapped
      prompt/slash-command to the session (via the 0023 send path, or the existing
      `_run_resume_background` if 0023 isn't merged yet)
- [ ] Context detection derives the offered buttons from the turn's tool actions
      (e.g. saw `Edit`/`Write` → show Undo; saw test-y Bash → show Run tests)
- [ ] Each verb maps to a documented prompt/command in one table that's easy to
      extend
- [ ] `try_handle_callback` routes `bridge:act:`
- [ ] Tests: button set for a code-editing turn vs a plain Q&A turn, callback
      dispatches the right prompt, unknown verb is a safe no-op. `pytest -q` green

## Likely files / surfaces touched

See `touches:`. Builds on the digest/notify card builders.

## Notes

Keep the verb→prompt table data-driven so new actions are a one-line add. "Undo
last edit" should send a natural-language instruction (e.g. "revert your last file
edit"), not attempt git operations itself — let the session decide.
