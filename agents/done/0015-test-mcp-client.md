---
id: 0015
title: Write behavior-organized tests for telechat_pkg/mcp_client.py
role: builder
priority: P1
owner: claude-opus-4-8
started: 2026-06-03
status: done
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

## Outcome — 2026-06-03

- **File:** `tests/test_mcp_client.py` (new, 49 tests, behavior-organized into 12 classes).
- **Coverage:** `telechat_pkg/mcp_client.py` 61% → **100%** (143/143 stmts; target was ≥95%).
- **Run:** `COVERAGE_FILE=/tmp/.cov_0015 python -m pytest -q tests/test_mcp_client.py --cov=telechat_pkg.mcp_client --cov-report=term-missing` → 49 passed, 0 warnings.

### What's covered
- **Lifecycle:** connect (unknown server, success + tool discovery, init/list message ordering, connect_all), call_tool (not-connected, unknown server, success, missing-result, malformed response, timeout), disconnect / disconnect_all (terminate + wait, noop on missing/process-less), list_tools / list_servers / get_tools_for_prompt, dataclasses, get_mcp_manager singleton.
- **Security invariants:** command allowlist (default runtimes allowed; `rm`/`/bin/sh`/`curl` rejected; empty rejected; basename match for absolute paths; `MCP_ALLOW_ANY_COMMAND` override via env and module constant); malformed/attacker config rejected at load and at `add_server`; missing `mcpServers` key; broken JSON swallowed not raised; subprocess env composition; subprocess termination on disconnect; bounded stdout reads (timeout → error, not hang).

### Caveats / untestable lines
- No source lines left uncovered. The `_telechat_version` `except → "unknown"` fallback is marked `# pragma: no cover` in source and is intentionally not exercised (telechat_pkg is always importable mid-suite); coverage is 100% without it.
- No real subprocesses spawned — `asyncio.create_subprocess_exec` is monkeypatched with a `FakeProcess` speaking JSON-RPC over fake streams. `jsonschema` is not imported by this module, so no optional-dep handling was needed.
- Tests that assert the allowlist-rejection path explicitly `monkeypatch.delenv("MCP_ALLOW_ANY_COMMAND")` + set the module constant `False`, because `tests/conftest.py` sets `MCP_ALLOW_ANY_COMMAND=1` session-wide.

### Security observations (baseline asserted — NOT fixed here)
1. **PATH/env not scrubbed (confirms CODE_REVIEW HIGH §3).** `connect()` builds `env = {**os.environ, **server.env}`. `test_env_inherits_os_environ_merged_with_server_env` asserts the CURRENT unsafe behavior: a parent-process secret (`PARENT_SECRET`) IS passed to the child, and `PATH` is inherited. The allowlist (`MCP_ALLOWED_COMMANDS`) only gates the *executable name*, not its resolution — a poisoned `PATH` could still point `npx`/`python3` at an attacker binary. Recommend a follow-up ticket: scrub/whitelist the child env and/or resolve commands to absolute paths before exec (per P1 #12 `MCP_ALLOW_EXEC` flag).
2. **Allowlist is basename-only.** `_is_command_allowed("/usr/local/bin/npx")` passes purely on basename `npx`; an attacker controlling `PATH` or planting a file named `npx` is not caught. Documented via `test_basename_match_for_absolute_path`. Same follow-up applies.
3. **`MCP_ALLOW_ANY_COMMAND` fully disables the allowlist** and is re-read from env on every `_is_command_allowed` call (runtime-flippable). Behavior intended per source docstring; noted for completeness.
