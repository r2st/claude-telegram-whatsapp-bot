---
id: 0027
title: Voice notes & Claude slash-commands passthrough to the bound session
role: builder
priority: P2
owner:
started:
status: inbox
depends_on: []
touches:
  - telechat_pkg/desktop_bridge.py
  - telechat_pkg/telegram_bot.py
  - tests/test_desktop_bridge.py
---

## Goal

When a session is bound, a typed message routes to it via
`try_handle_text_message`, but two natural inputs don't: voice notes and Claude
Code slash-commands. Route a transcribed voice note to the bound session, and pass
recognised slash-commands (`/clear`, `/compact`, and skill invocations like
`/review`) through to the session instead of treating them as bot commands.

## Why it matters

Talking to your running agent ("add error handling and run the tests") while away
from the keyboard is the most natural mobile interaction there is. And being able
to drive the session's own slash-commands from Telegram closes the gap between the
phone and sitting at the desktop.

## Acceptance criteria

- [ ] A voice note received while a session is bound is transcribed (reuse
      `voice_transcription`) and dispatched to the session as a prompt; the user
      gets a confirmation of the transcribed text
- [ ] A configurable allowlist of passthrough slash-commands is forwarded to the
      bound session verbatim (sent as the `-p` prompt) rather than handled by the
      bot's own `CommandHandler`s
- [ ] Slash-commands NOT on the allowlist keep their current bot behavior (no
      regression to existing `/desktop`, `/follow`, etc.)
- [ ] When no session is bound, voice and slash behavior are unchanged
- [ ] Tests: voice→bound-session dispatch (mocked transcription), allowlisted slash
      passthrough, non-allowlisted slash unaffected, no-binding fallthrough.
      `pytest -q` green

## Likely files / surfaces touched

See `touches:`. The voice handler and command dispatch live in `telegram_bot.py`;
the routing decision and send live in `desktop_bridge.py`
(`try_handle_text_message` and a sibling for voice).

## Notes

Order matters: the bridge must get first refusal on a slash-command before the
bot's `CommandHandler` consumes it — telegram-python-bot runs CommandHandlers
ahead of the generic message handler, so passthrough likely needs a small
group-priority handler or an explicit allowlist check at the top of the relevant
handlers. Document the chosen mechanism.
