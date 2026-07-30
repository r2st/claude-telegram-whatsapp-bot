---
id: 0026
title: Granular approval cards (approve once / always-tool / always-session / deny+reason)
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

Approval mode currently gates Bash/Write/Edit on a binary approve/deny decision
card, and approval is all-or-nothing per project. Make the decision card granular:
`✅ Approve once`, `✅ Always (this tool)`, `✅ Always (this session)`, `⛔ Deny`,
`⛔ Deny with reason`. Persist the always-rules so matching later tool calls
auto-approve without another round-trip.

## Why it matters

Real interactive control over an agent means saying "yes to reads, ask me about
writes" or "stop asking about this one tool." Binary approve/deny forces a phone
tap on every single tool call, which makes approval mode too tedious to leave on.

## Acceptance criteria

- [ ] Decision cards render the five granular options
- [ ] `Always (this tool)` and `Always (this session)` persist allow-rules (new
      bridge table, created lazily like the other bridge schema) keyed by
      session/tool; the approve hook consults them and auto-approves matches
- [ ] `Deny with reason` prompts for a short reason and feeds it back to the
      session as the denial message
- [ ] Auto-approved calls are logged (and optionally surfaced) so "always" rules
      are auditable, not silent
- [ ] A command to list/clear active always-rules (e.g. `/desktop_rules`,
      `/desktop_rules_clear`)
- [ ] `try_handle_callback` routes the new approval callbacks
- [ ] Tests: each decision path, rule persistence + auto-approve match, reason
      passthrough, rule listing/clearing. `pytest -q` green
- [ ] `docs/advanced-telegram-features.md` updated

## Likely files / surfaces touched

See `touches:`. Hook entrypoints (`hook_approve`/the approve CLI dispatch) and the
existing `appr:`/approve callback flow.

## Notes

Keep rules scoped and expirable — a session-scoped "always" rule should disappear
when that session ends (reuse lifecycle signals). Never persist an always-approve
for a tool across *all* sessions implicitly; require an explicit, clearly-labelled
choice. Security-sensitive surface — be conservative about defaults.
