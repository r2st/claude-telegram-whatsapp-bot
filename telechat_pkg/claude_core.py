"""
Claude invocation layer — CLI, API, and SDK clients.

Config constants and all store/session functions are re-exported here
so existing ``import claude_core as cc`` keeps working.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from typing import Awaitable, Callable, Optional

from . import models
from . import store as _store

from .store import (  # noqa: F401 — re-export for backward compat
    DB_PATH,
    RATE_LIMIT_REQUESTS,
    RATE_LIMIT_WINDOW,
    UserSession,
    SessionManager,
    _session_mgr,
    _get_conn,
    _db_writer,
    _ensure_writer,
    _enqueue_write,
    _cache_key,
    _invalidate_history,
    check_rate_limit,
    init_db,
    load_history,
    save_turn,
    clear_history,
    track_usage,
    get_usage,
    track_tool_usage,
    track_cost,
    get_session_id,
    set_session_id,
    clear_session,
    get_history,
)


def __getattr__(name):
    """Delegate lookups for mutable store globals (e.g. _write_queue, _writer_thread, _history_cache)."""
    try:
        return getattr(_store, name)
    except AttributeError as exc:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        ) from exc

log = logging.getLogger(__name__)

# ─── Config ──────────────────────────────────────────────────────────────────────

CLAUDE_MODEL      = os.getenv("CLAUDE_MODEL", "sonnet")

# Two names each, because `.env.example` shipped the wrong one for both settings
# and the code silently ignored it: a user who set SYSTEM_PROMPT (as the
# template told them to) got the stock prompt, and CLAUDE_CLI_ADD_DIRS granted
# access to nothing. The template now names the canonical variable; these
# fallbacks mean the .env files already out there start working rather than
# staying quietly broken.
CLAUDE_SYSTEM     = os.getenv("CLAUDE_SYSTEM_PROMPT") or os.getenv(
    "SYSTEM_PROMPT",
    "You are a helpful AI assistant. Be concise unless asked for detail.",
)
CLAUDE_ADD_DIRS   = os.getenv("CLAUDE_ADD_DIRS") or os.getenv("CLAUDE_CLI_ADD_DIRS", "")
CLAUDE_WORK_DIR   = os.getenv("CLAUDE_CLI_WORK_DIR", os.path.expanduser("~"))
CLAUDE_TIMEOUT    = int(os.getenv("CLAUDE_TIMEOUT", "180"))
CLAUDE_MODE       = os.getenv("CLAUDE_MODE", "cli")   # cli | api | sdk
CLAUDE_API_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_API_MODEL  = os.getenv("CLAUDE_API_MODEL", models.DEFAULT_API_MODEL)
CLAUDE_MAX_TOKENS = int(os.getenv("MAX_TOKENS", "4096"))
CLAUDE_PERM_MODE  = os.getenv("CLAUDE_CLI_PERMISSION_MODE", "auto")


# ─── Claude CLI (sync) ──────────────────────────────────────────────────────────

def ask_claude_sync(
    user_text: str,
    history: list[dict],
    *,
    model: str = CLAUDE_MODEL,
    system: str = CLAUDE_SYSTEM,
    add_dirs: str = CLAUDE_ADD_DIRS,
    perm_mode: str = CLAUDE_PERM_MODE,
    timeout: int = CLAUDE_TIMEOUT,
) -> tuple[str, dict]:
    """Blocking Claude CLI call. Returns (reply_text, stats_dict)."""
    full_prompt = _build_prompt(user_text, history)

    cmd = [
        "claude",
        "--model", model,
        "-p", full_prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", perm_mode,
    ]
    for d in [x.strip() for x in add_dirs.split(",") if x.strip()]:
        cmd += ["--add-dir", d]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=CLAUDE_WORK_DIR,
        )
        return _parse_cli_output(result.stdout, result.stderr, result.returncode, timeout)
    except subprocess.TimeoutExpired:
        return f"[Timeout] Claude took more than {timeout}s. Try a shorter prompt.", {}
    except FileNotFoundError:
        return "[Error] `claude` CLI not found. Ensure Claude Code is installed and in PATH.", {}


# ─── Claude CLI (async) ─────────────────────────────────────────────────────────

async def ask_claude_async(
    user_text: str,
    history: list[dict],
    *,
    model: str = CLAUDE_MODEL,
    system: str = CLAUDE_SYSTEM,
    add_dirs: str = CLAUDE_ADD_DIRS,
    perm_mode: str = CLAUDE_PERM_MODE,
    timeout: int = CLAUDE_TIMEOUT,
    on_progress: Optional[Callable[[str, str], Awaitable[None]]] = None,
    on_text: Optional[Callable[[str], Awaitable[None]]] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
    platform: str = "",
    user_id: str = "",
    resume_session_id: str = "",
    work_dir: str = "",
) -> tuple[str, dict]:
    """Async Claude CLI call with streaming progress."""
    session_id = resume_session_id or (get_session_id(platform, user_id) if platform else None)
    full_prompt = _build_prompt(user_text, history) if history else user_text
    cwd = work_dir or CLAUDE_WORK_DIR

    if session_id:
        cmd = [
            "claude",
            "--model", model,
            "-p", user_text,
            "--resume", session_id,
            "--output-format", "stream-json",
            "--verbose",
            "--permission-mode", perm_mode,
        ]
    else:
        cmd = [
            "claude",
            "--model", model,
            "-p", full_prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--permission-mode", perm_mode,
        ]

    for d in [x.strip() for x in add_dirs.split(",") if x.strip()]:
        cmd += ["--add-dir", d]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        limit=10 * 1024 * 1024,
    )

    stdout_lines: list[str] = []
    try:
        async def _read_stream():
            while True:
                if is_cancelled and is_cancelled():
                    proc.kill()
                    return

                line = await proc.stdout.readline()
                if not line:
                    break
                decoded = line.decode().strip()
                if not decoded:
                    continue
                stdout_lines.append(decoded)

                if on_progress or on_text:
                    try:
                        event = json.loads(decoded)
                        etype = event.get("type", "")

                        if etype == "assistant":
                            msg = event.get("message", {})
                            if isinstance(msg, dict):
                                for block in msg.get("content", []):
                                    if block.get("type") == "tool_use" and on_progress:
                                        detail = _extract_tool_detail(block)
                                        await on_progress(block.get("name", "tool"), detail)
                                    elif block.get("type") == "text" and on_text:
                                        await on_text(block.get("text", ""))

                        elif etype == "content_block_start":
                            cb = event.get("content_block", {})
                            if cb.get("type") == "tool_use" and on_progress:
                                detail = _extract_tool_detail(cb)
                                await on_progress(cb.get("name", "tool"), detail)

                        elif etype == "content_block_delta" and on_text:
                            delta = event.get("delta", {})
                            if delta.get("type") == "text_delta":
                                await on_text(delta.get("text", ""))

                    except (json.JSONDecodeError, Exception):
                        pass

        await asyncio.wait_for(_read_stream(), timeout=timeout)
        await proc.wait()
        stderr_data = await proc.stderr.read()
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return f"[Timeout] Claude took more than {timeout}s.", {}

    stdout_text = "\n".join(stdout_lines)
    result = _parse_cli_output(
        stdout_text, stderr_data.decode(), proc.returncode, timeout
    )

    # Retry with full history if session resume failed
    if session_id and (proc.returncode != 0 or not result[0] or result[0].startswith("[Claude error]")):
        log.warning("Session resume failed (rc=%d), retrying with full history", proc.returncode)
        if platform:
            active_sess = _session_mgr.get_or_create_active(platform, user_id)
            active_sess.claude_session_id = None

        retry_cmd = [
            "claude",
            "--model", model,
            "-p", full_prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--permission-mode", perm_mode,
        ]
        for d in [x.strip() for x in add_dirs.split(",") if x.strip()]:
            retry_cmd += ["--add-dir", d]

        proc2 = await asyncio.create_subprocess_exec(
            *retry_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=CLAUDE_WORK_DIR,
            limit=10 * 1024 * 1024,
        )
        retry_lines: list[str] = []
        try:
            async def _read_retry():
                while True:
                    if is_cancelled and is_cancelled():
                        proc2.kill()
                        return
                    line = await proc2.stdout.readline()
                    if not line:
                        break
                    decoded = line.decode().strip()
                    if decoded:
                        retry_lines.append(decoded)
                        if on_progress or on_text:
                            try:
                                event = json.loads(decoded)
                                etype = event.get("type", "")
                                if etype == "content_block_delta" and on_text:
                                    delta = event.get("delta", {})
                                    if delta.get("type") == "text_delta":
                                        await on_text(delta.get("text", ""))
                            except Exception:
                                pass

            await asyncio.wait_for(_read_retry(), timeout=timeout)
            await proc2.wait()
            stderr2 = await proc2.stderr.read()
        except asyncio.TimeoutError:
            proc2.kill()
            await proc2.wait()
            return f"[Timeout] Claude took more than {timeout}s.", {}

        result = _parse_cli_output(
            "\n".join(retry_lines), stderr2.decode(), proc2.returncode, timeout
        )

    return result


# ─── Claude API client (reused across calls) ────────────────────────────────────

_api_client = None
_async_api_client = None


def _get_api_client():
    global _api_client
    if _api_client is None:
        try:
            import anthropic
        except ImportError:
            return None
        _api_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    return _api_client


def _get_async_api_client():
    global _async_api_client
    if _async_api_client is None:
        try:
            import anthropic
        except ImportError:
            return None
        _async_api_client = anthropic.AsyncAnthropic(api_key=CLAUDE_API_KEY)
    return _async_api_client


# ─── Claude API (sync) ──────────────────────────────────────────────────────────

def _add_estimated_cost(stats: dict, model: str, usage) -> None:
    """Fill in ``cost_usd`` from token counts, because the API doesn't report it.

    Only the CLI path returns a real cost (the stream's ``total_cost_usd``). The
    API path returns tokens and nothing else, so before this every API-mode turn
    was recorded at $0.00: `/usage` showed a false cost and `BudgetManager`
    summed a column of zeroes, meaning daily and monthly caps never tripped no
    matter how much was spent.

    ``cost_estimated`` marks the figure as computed rather than reported, so the
    two can be told apart wherever cost is displayed.
    """
    def _tokens(name: str) -> int:
        # `usage` field names and types vary across anthropic SDK versions and
        # across the Bedrock/Vertex client wrappers. A cost *estimate* must never
        # be the thing that fails a user's message, so anything non-numeric
        # counts as zero rather than raising out of the chat path.
        value = getattr(usage, name, 0)
        return value if isinstance(value, int) and value > 0 else 0

    cache_read = _tokens("cache_read_input_tokens")
    stats["cost_usd"] = models.estimate_cost(
        model,
        input_tokens=_tokens("input_tokens"),
        output_tokens=_tokens("output_tokens"),
        cache_read_tokens=cache_read,
    )
    stats["cost_estimated"] = True
    if cache_read:
        stats["cache_read_tokens"] = cache_read


def ask_claude_api(
    user_text: str,
    history: list[dict],
    *,
    model: str = CLAUDE_API_MODEL,
    system: str = CLAUDE_SYSTEM,
    max_tokens: int = CLAUDE_MAX_TOKENS,
) -> tuple[str, dict]:
    client = _get_api_client()
    if client is None:
        return "[Error] anthropic package not installed. Run: pip install anthropic", {}

    messages = history + [{"role": "user", "content": user_text}]
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
    )
    text = resp.content[0].text
    stats = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "tools_used": [],
    }
    _add_estimated_cost(stats, model, resp.usage)
    return text, stats


# ─── Claude API (async with streaming) ──────────────────────────────────────────

async def ask_claude_api_async(
    user_text: str,
    history: list[dict],
    *,
    model: str = CLAUDE_API_MODEL,
    system: str = CLAUDE_SYSTEM,
    max_tokens: int = CLAUDE_MAX_TOKENS,
    on_text: Optional[Callable[[str], Awaitable[None]]] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> tuple[str, dict]:
    """Async Claude API call with streaming."""
    client = _get_async_api_client()
    if client is None:
        return "[Error] anthropic package not installed. Run: pip install anthropic", {}

    messages = history + [{"role": "user", "content": user_text}]
    result_parts: list[str] = []
    stats = {"tools_used": []}

    async with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
    ) as stream:
        async for text_chunk in stream.text_stream:
            if is_cancelled and is_cancelled():
                break
            result_parts.append(text_chunk)
            if on_text:
                try:
                    await on_text(text_chunk)
                except Exception:
                    pass
        final = await stream.get_final_message()
        stats["input_tokens"] = final.usage.input_tokens
        stats["output_tokens"] = final.usage.output_tokens
        _add_estimated_cost(stats, model, final.usage)

    result_text = "".join(result_parts)
    return result_text or "(no response)", stats


# ─── Claude Code SDK (async) ────────────────────────────────────────────────────

async def ask_claude_sdk(
    user_text: str,
    history: list[dict],
    *,
    model: str = CLAUDE_MODEL,
    system: str = CLAUDE_SYSTEM,
    add_dirs: str = CLAUDE_ADD_DIRS,
    timeout: int = CLAUDE_TIMEOUT,
    perm_mode: str = CLAUDE_PERM_MODE,
    on_progress: Optional[Callable[[str, str], Awaitable[None]]] = None,
    on_text: Optional[Callable[[str], Awaitable[None]]] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> tuple[str, dict]:
    """Async Claude Code SDK call with streaming progress."""
    try:
        from claude_code_sdk import (
            query,
            ClaudeCodeOptions,
            AssistantMessage,
            ResultMessage,
            TextBlock,
            ToolUseBlock,
        )
    except ImportError:
        return "[Error] claude-code-sdk not installed. Run: pip install claude-code-sdk", {}

    full_prompt = _build_prompt(user_text, history)

    # Map our CLI-style perm_mode values onto the SDK's accepted values.
    # The SDK historically accepts: "default", "acceptEdits", "bypassPermissions".
    # We translate "auto" → "default" so a user setting CLAUDE_CLI_PERMISSION_MODE=auto
    # (the CLI's default) doesn't silently get escalated to bypassPermissions.
    sdk_perm_mode = {
        "auto": "default",
        "default": "default",
        "acceptEdits": "acceptEdits",
        "plan": "default",
        "bypassPermissions": "bypassPermissions",
    }.get(perm_mode, "default")

    opts = ClaudeCodeOptions(
        model=model,
        system_prompt=system,
        cwd=CLAUDE_WORK_DIR,
        permission_mode=sdk_perm_mode,
        max_turns=50,
    )
    if add_dirs:
        opts.add_dirs = [d.strip() for d in add_dirs.split(",") if d.strip()]

    result_text = ""
    tools_used: list[str] = []
    stats: dict = {}

    try:
        async for message in query(prompt=full_prompt, options=opts):
            if is_cancelled and is_cancelled():
                break

            if isinstance(message, AssistantMessage):
                for block in getattr(message, "content", []):
                    if isinstance(block, ToolUseBlock):
                        tools_used.append(block.name)
                        if on_progress:
                            try:
                                inp = getattr(block, "input", {}) or {}
                                detail = _extract_tool_detail({"input": inp})
                                await on_progress(block.name, detail)
                            except Exception:
                                pass
                    elif isinstance(block, TextBlock):
                        result_text = block.text
                        if on_text:
                            try:
                                await on_text(block.text)
                            except Exception:
                                pass

            elif isinstance(message, ResultMessage):
                if message.result:
                    result_text = message.result
                usage = message.usage or {}
                stats = {
                    "input_tokens": usage.get("input_tokens", 0)
                                    + usage.get("cache_read_input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "cost_usd": message.total_cost_usd or 0,
                    "session_id": message.session_id,
                    "num_turns": message.num_turns,
                    "duration_ms": message.duration_ms,
                }

    except asyncio.TimeoutError:
        return f"[Timeout] Claude took more than {timeout}s.", {}
    except Exception as exc:
        return f"[SDK Error] {exc}", {}

    stats["tools_used"] = tools_used
    return result_text or "(no response)", stats


# ─── Internal helpers ───────────────────────────────────────────────────────────

def _extract_tool_detail(block: dict) -> str:
    inp = block.get("input", {})
    if not isinstance(inp, dict):
        return ""
    fp = inp.get("file_path", "")
    if fp:
        parts = fp.rsplit("/", 2)
        return "/".join(parts[-2:]) if len(parts) > 2 else fp
    cmd = inp.get("command", "")
    if cmd:
        return cmd[:50]
    pattern = inp.get("pattern", "")
    if pattern:
        return f"/{pattern[:30]}/"
    return ""


def _build_prompt(user_text: str, history: list[dict]) -> str:
    parts = []
    for msg in history:
        role = "User" if msg["role"] == "user" else "Assistant"
        parts.append(f"{role}: {msg['content']}")
    parts.append(f"User: {user_text}")
    return "\n\n".join(parts)


def _parse_cli_output(stdout: str, stderr: str, returncode: int, timeout: int) -> tuple[str, dict]:
    output = stdout.strip()
    if returncode != 0 and not output:
        err = stderr.strip()[:500]
        return f"[Claude error] {err}", {}

    result_text = ""
    tools_used: list[str] = []
    stats: dict = {}

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            result_text = result_text or line
            continue

        etype = event.get("type", "")
        if etype == "result":
            result_text = event.get("result", result_text)
            usage = event.get("usage", {})
            stats = {
                "input_tokens": usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cost_usd": event.get("total_cost_usd", 0),
                "session_id": event.get("session_id", ""),
            }
        elif etype == "assistant":
            msg = event.get("message", {})
            if isinstance(msg, dict):
                for block in msg.get("content", []):
                    if block.get("type") == "text":
                        result_text = block["text"]
                    elif block.get("type") == "tool_use":
                        tools_used.append(block.get("name", "tool"))
        elif etype == "content_block_start":
            cb = event.get("content_block", {})
            if cb.get("type") == "tool_use":
                tools_used.append(cb.get("name", "tool"))

    stats["tools_used"] = tools_used
    return result_text or output or "(no response)", stats
