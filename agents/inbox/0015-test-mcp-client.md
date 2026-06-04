---
id: 0015
title: Write behavior-organized tests for telechat_pkg/mcp_client.py
role: builder
priority: P1
owner:
started:
status: inbox
depends_on: []
---

## Goal

Create `tests/test_mcp_client.py` covering the mcp_client module's real behaviors. Coverage dropped 96% → **61%** after ticket 0012 — 50 lines unexercised. Third-largest drop. Cross-references CODE_REVIEW.md HIGH severity finding (security): mcp_client `create_subprocess_exec` with no PATH sanitization is a known attack surface.

## Why it matters

mcp_client is where Claude reaches out to MCP servers — meaning subprocesses run with whatever command is in `MCP_CONFIG_FILE`. If the config is user-controlled or attacker-controlled, this is arbitrary local code execution. Behavior-organized tests around config parsing, command validation, subprocess lifecycle, and error reporting are *security-relevant*, not just coverage. The padding test class `TestMCPClientConnect` covered "connection establishes" but the *security* invariants — command allow-listing, environment scrubbing, timeout enforcement — need explicit assertions.

## Acceptance criteria

- [ ] `tests/test_mcp_client.py` created, organized by behavior
- [ ] Cover the connection lifecycle: connect, list-tools, call-tool, disconnect, cleanup-on-error
- [ ] Cover the SECURITY-relevant invariants: malformed config rejected, env vars not leaked, subprocess cleaned up on parent exit, timeout enforced
- [ ] Coverage of `telechat_pkg/mcp_client.py` returns to ≥95%
- [ ] `pytest -q tests/test_mcp_client.py` green
- [ ] Full `pytest -q` still green (modulo 3 pre-existing cassette failures from 0006)

## Likely files / surfaces touched

- `tests/test_mcp_client.py` (new)
- No source changes expected — but if a security-invariant test fails, file a separate bug-fix ticket for the underlying issue (do not change source code while writing tests)

## Notes

CODE_REVIEW.md HIGH (security §3): *"`env = {**os.environ, **server.env}` then `create_subprocess_exec(server.command, ...)` with no PATH sanitization. If MCP config is user-controlled, this is straight command exec."* Tests should establish a baseline of CURRENT behavior; the fix (allow-list / `MCP_ALLOW_EXEC` flag per P1 #12) is a separate ticket.

Created from ticket 0012.
