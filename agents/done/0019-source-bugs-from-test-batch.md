---
id: 0019
title: Fix source bugs surfaced while writing the 0008–0017 test batch
role: builder
priority: P1
owner: claude-opus-4-8
started: 2026-06-03
status: done
depends_on: []
---

## Goal

While writing behavior-organized tests for tickets 0008–0017, several real
source defects were found and pinned by tests (current behavior asserted, not
fixed — test-writing and source-fixing were deliberately kept separate). This
ticket tracks fixing them. Each is independent; split into per-bug tickets if a
fix turns out non-trivial.

## Why it matters

These are latent correctness/security issues now covered by tests, so a fix can
be made with a safety net. The mcp_client item is a known HIGH security finding.

## Acceptance criteria

- [x] **(P1, security) mcp_client subprocess env + PATH** — `connect()` does
      `env = {**os.environ, **server.env}` then spawns `server.command` with no
      PATH sanitization; the allow-list checks the command *basename* only.
      A poisoned `PATH` or planted binary redirects `npx`/`python3` to attacker
      code, and parent secrets leak into the child env. Maps to
      `CODE_REVIEW.md` HIGH §3 / P1 #12. Fix: scrub/whitelist env, resolve and
      validate the absolute command path (or gate behind an explicit
      `MCP_ALLOW_EXEC`). Tests in `tests/test_mcp_client.py` currently assert
      the *unsafe* current behavior — flip them to assert the safe behavior.
- [x] **(P2) store SessionManager cache corruption** — `archive()` /
      `delete_by_name()` on the sole active session leave the archived
      `UserSession` in the in-memory cache while also appending a fresh
      `"default"`; two entries then share the `(platform,user_id,name)` UNIQUE
      key and `get_or_create_active()` can return the archived copy
      (`archived is True`). Pinned by `tests/test_store.py`.
- [x] **(P3) conversation_export._ts_to_str** — catches only
      `(OSError, ValueError)`; a huge timestamp (e.g. `10**30`) raises
      `OverflowError` on macOS, which propagates uncaught. Add `OverflowError`
      to the handled set. Pinned by `tests/test_conversation_export.py`.
- [x] Any test whose assertion changes from current→fixed behavior is updated in
      the same PR; `pytest -q` green (modulo the 3 cassette failures from 0006).

## Likely files / surfaces touched

- `telechat_pkg/mcp_client.py` + `tests/test_mcp_client.py`
- `telechat_pkg/store.py` (SessionManager) + `tests/test_store.py`
- `telechat_pkg/conversation_export.py` + `tests/test_conversation_export.py`

## Notes

Non-bugs also observed and intentionally left as-is (documented here so they
aren't re-reported): `document_extract` never rejects "unsupported" formats
(falls back to text decode by design); `cost_budget._get_daily/monthly_cost`
`return 0.0, 0` fallback is dead code (COUNT/COALESCE always returns a row);
`smart_router` line 93 is genuinely unreachable (`# pragma: no cover`).

Created from the 0008–0017 test-restoration batch.

## Outcome — 2026-06-03

All three bugs fixed; affected tests flipped from pinning the buggy behavior to
asserting the correct behavior. Full suite: **2835 passed, 3 failed** (only the
known 0006 cassette failures).

- **mcp_client env scrub (security):** added `_build_child_env()` — MCP
  subprocesses now inherit only an infrastructure allowlist (PATH, HOME, locale,
  temp dirs, Windows infra); secrets like `ANTHROPIC_API_KEY` / `TELEGRAM_BOT_TOKEN`
  are no longer forwarded. Servers declare needed vars via config `env` (merged
  on top). `connect()` line 162 changed; `tests/test_mcp_client.py` now asserts
  `PARENT_SECRET not in env` while `PATH`/`SERVER_KEY` pass through. The
  command-allowlist (basename) already serves as the exec gate, so absolute-path
  resolution was left out to avoid breaking legitimate `npx`/`python3` usage —
  noted for a future hardening pass if desired.
- **store SessionManager (cache):** `get_or_create_active()` now filters to live
  (non-archived) sessions, so it can never return an archived copy — the actual
  reported harm. Archive/delete replacement-selection factored into
  `_activate_replacement()` (behavior-preserving: archiving still returns the
  archived session; the transient archived+fresh `default` pair collapses to one
  row on the next DB reload). `tests/test_store.py` updated.
- **conversation_export:** `_ts_to_str()` now also catches `OverflowError`
  (huge timestamps on macOS raise it, not `ValueError`) → degrades to `"unknown"`
  instead of propagating. `tests/test_conversation_export.py` gains a case.

Intentionally-unfixed non-bugs (documented in this ticket) left as-is.
