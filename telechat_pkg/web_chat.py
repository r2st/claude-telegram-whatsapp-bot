"""
Web chat adapter — serves a browser-based chat UI backed by Claude CLI/API/SDK.

Runs an aiohttp server with:
- GET  /         → chat UI (single-page app)
- GET  /health   → JSON health status
- WS   /ws       → WebSocket for real-time chat

Set WEB_CHAT_PORT in .env (default: 8585).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path

from aiohttp import web

from . import claude_core as cc
from .memory import MemoryStore

log = logging.getLogger(__name__)

PLATFORM = "web"

WEB_PORT = int(os.getenv("WEB_CHAT_PORT", "8585"))
# Default to loopback. Binding to 0.0.0.0 must be opt-in via WEB_CHAT_BIND
# so we never expose the chat UI on the LAN by accident.
WEB_BIND = os.getenv("WEB_CHAT_BIND", "127.0.0.1")
WEB_AUTH_TOKEN = os.getenv("WEB_CHAT_TOKEN", "")
# Only honour X-Forwarded-For when WEB_CHAT_TRUST_PROXY=1 — otherwise any
# client can spoof their IP and defeat the per-IP auth lockout.
WEB_TRUST_PROXY = os.getenv("WEB_CHAT_TRUST_PROXY", "0").lower() in ("1", "true", "yes", "on")

# Auth throttling. When the server is exposed beyond localhost, attackers
# could otherwise brute-force WEB_CHAT_TOKEN indefinitely. Tunable via env
# so operators can tighten/loosen for their environment.
WEB_AUTH_MAX_ATTEMPTS = int(os.getenv("WEB_CHAT_AUTH_MAX_ATTEMPTS", "5"))
WEB_AUTH_LOCKOUT_SEC = float(os.getenv("WEB_CHAT_AUTH_LOCKOUT_SEC", "300"))

# Per-IP rolling counters: (window_start, failure_count). Memory-only; resets
# on process restart, which is acceptable for a single-node web chat.
_auth_failures: dict[str, tuple[float, int]] = {}

# An entry was only ever removed on a successful auth, or when that same IP was
# checked again after its window expired. So a distributed brute force — one
# failed attempt each from many addresses — left one entry per address behind
# forever, and the defence against brute force became a way to grow the
# process's memory without bound. Expired entries are now pruned on write, and
# the table is hard-capped.
_AUTH_FAILURES_MAX = 10_000

_memory = MemoryStore()
_active_ws: dict[str, web.WebSocketResponse] = {}


_LOOPBACK_BINDS = {"127.0.0.1", "localhost", "::1", ""}


def _is_loopback_bind(bind: str) -> bool:
    return bind in _LOOPBACK_BINDS


def _client_ip(request: web.Request) -> str:
    """Best-effort client IP.

    Only consults X-Forwarded-For when WEB_CHAT_TRUST_PROXY is set; otherwise
    the header is attacker-controlled and bypasses the per-IP lockout.
    """
    if WEB_TRUST_PROXY:
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
    peer = request.transport.get_extra_info("peername") if request.transport else None
    if peer and isinstance(peer, tuple):
        return peer[0]
    return "unknown"


def _ip_is_locked(ip: str) -> bool:
    """True if this IP has exceeded WEB_AUTH_MAX_ATTEMPTS within the window."""
    rec = _auth_failures.get(ip)
    if not rec:
        return False
    window_start, count = rec
    if (time.time() - window_start) > WEB_AUTH_LOCKOUT_SEC:
        _auth_failures.pop(ip, None)
        return False
    return count >= WEB_AUTH_MAX_ATTEMPTS


def _prune_auth_failures(now: float, keep: str = "") -> None:
    """Drop entries whose lockout window has expired, then enforce the cap.

    Called on write rather than on a timer: failures are the only thing that
    grows this table, so that is exactly when it needs bounding.

    ``keep`` is never evicted. It is the IP whose failure triggered this call,
    and dropping it would hand an attacker a way to clear their own counter by
    filling the table from other addresses — turning the cap into a bypass for
    the lockout it exists to protect.
    """
    for ip, (window_start, _count) in list(_auth_failures.items()):
        if ip != keep and (now - window_start) > WEB_AUTH_LOCKOUT_SEC:
            _auth_failures.pop(ip, None)
    # Still over the cap means more distinct live attackers than we are willing
    # to track. Evict the oldest windows — they are closest to expiring anyway.
    if len(_auth_failures) > _AUTH_FAILURES_MAX:
        evictable = sorted(
            ((ip, rec) for ip, rec in _auth_failures.items() if ip != keep),
            key=lambda kv: kv[1][0],
        )
        for ip, _rec in evictable[: len(_auth_failures) - _AUTH_FAILURES_MAX]:
            _auth_failures.pop(ip, None)


def _record_auth_failure(ip: str) -> int:
    """Increment failure count for `ip`; return the new count."""
    now = time.time()
    rec = _auth_failures.get(ip)
    if rec and (now - rec[0]) <= WEB_AUTH_LOCKOUT_SEC:
        _auth_failures[ip] = (rec[0], rec[1] + 1)
    else:
        _auth_failures[ip] = (now, 1)
    count = _auth_failures[ip][1]
    # Prune after recording, and never evict this IP: its own failure must not
    # be what clears its counter.
    _prune_auth_failures(now, keep=ip)
    return count


def _clear_auth_failures(ip: str) -> None:
    _auth_failures.pop(ip, None)

_DEFAULT_MODEL = os.getenv("CLAUDE_CLI_MODEL", cc.CLAUDE_MODEL)
_DEFAULT_PERM = os.getenv("CLAUDE_CLI_PERMISSION_MODE", cc.CLAUDE_PERM_MODE)

_HTML_PATH = Path(__file__).parent / "web_chat_ui.html"


def _get_user_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


# Sentinel used when WEB_CHAT_TOKEN is not configured. Yields a single stable
# user_id across reconnects/refreshes — sufficient for single-user web chat,
# which is what the open-mode deployment effectively is. Multi-tenant deploys
# should set WEB_CHAT_TOKEN or front the server with their own auth proxy.
_ANONYMOUS_USER_KEY = "anonymous"


def _stable_user_id(token: str) -> str:
    """Derive a stable user_id from the auth token (or an anonymous sentinel).

    The returned id MUST NOT depend on per-connection randomness — browser
    refreshes and reconnects need to map back to the same history/session.
    """
    return _get_user_id(token or _ANONYMOUS_USER_KEY)


async def _index_handler(request: web.Request) -> web.Response:
    html = _HTML_PATH.read_text()
    return web.Response(text=html, content_type="text/html")


async def _health_handler(request: web.Request) -> web.Response:
    from . import health
    h = health.get_health()
    h["web_clients"] = len(_active_ws)
    status = 200 if h["status"] == "healthy" else 503
    return web.json_response(h, status=status)


async def _ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(max_msg_size=10 * 1024 * 1024)
    await ws.prepare(request)

    client_id = secrets.token_hex(8)
    authenticated = not WEB_AUTH_TOKEN
    # user_id is derived from a stable source (auth token, or an anonymous
    # sentinel when no token is configured) so it survives reconnects.
    # When WEB_AUTH_TOKEN is required, we recompute after a successful auth
    # message so the id is keyed on the validated token.
    user_id = _stable_user_id(WEB_AUTH_TOKEN)

    # Per-connection failure cap (independent of the per-IP window). Without
    # this, a single socket could try indefinitely as long as it stayed under
    # the per-IP rate.
    conn_fail_count = 0
    peer_ip = _client_ip(request)

    # Retain handles for chat tasks we spawn — bare asyncio.create_task() is
    # only weakly referenced by the event loop, so it can be garbage-collected
    # mid-flight (Python docs warn explicitly about this). Also bound the
    # number of concurrent in-flight chat tasks per connection so a malicious
    # client can't spawn unbounded work.
    chat_tasks: set[asyncio.Task] = set()
    MAX_INFLIGHT = 4

    async def send_json(data: dict):
        if not ws.closed:
            await ws.send_json(data)

    await send_json({
        "type": "connected",
        "auth_required": not authenticated,
    })

    _active_ws[client_id] = ws
    log.info("Web client connected: %s (auth_required=%s)", client_id, not authenticated)

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    await send_json({"type": "error", "text": "Invalid JSON"})
                    continue

                msg_type = data.get("type", "")

                if msg_type == "auth":
                    # Reject before doing any token comparison if the peer IP
                    # is already locked out, so attackers can't keep probing.
                    if WEB_AUTH_TOKEN and _ip_is_locked(peer_ip):
                        log.warning(
                            "Auth lockout: rejecting attempt from %s (client %s)",
                            peer_ip, client_id,
                        )
                        await send_json({
                            "type": "auth_fail",
                            "reason": "locked",
                        })
                        await ws.close(code=4429, message=b"auth locked")
                        break

                    token = data.get("token", "")
                    if not WEB_AUTH_TOKEN or hmac.compare_digest(token, WEB_AUTH_TOKEN):
                        authenticated = True
                        # Re-key user_id on the validated token so the same
                        # browser, on reconnect, lands on the same history.
                        user_id = _stable_user_id(token or WEB_AUTH_TOKEN)
                        conn_fail_count = 0
                        _clear_auth_failures(peer_ip)
                        await send_json({"type": "auth_ok"})
                    else:
                        conn_fail_count += 1
                        ip_count = _record_auth_failure(peer_ip)
                        log.warning(
                            "Auth failure from %s (client %s): conn=%d ip=%d",
                            peer_ip, client_id, conn_fail_count, ip_count,
                        )
                        await send_json({"type": "auth_fail"})
                        if conn_fail_count >= WEB_AUTH_MAX_ATTEMPTS:
                            await ws.close(
                                code=4429, message=b"too many auth failures",
                            )
                            break
                    continue

                if not authenticated:
                    await send_json({"type": "error", "text": "Not authenticated"})
                    continue

                if msg_type == "message":
                    text = (data.get("text") or "").strip()
                    if not text:
                        continue

                    if text.startswith("/"):
                        await _handle_command(ws, send_json, user_id, text)
                        continue

                    # Drop stale completed tasks before checking the in-flight cap.
                    chat_tasks.difference_update({t for t in chat_tasks if t.done()})
                    if len(chat_tasks) >= MAX_INFLIGHT:
                        await send_json({
                            "type": "error",
                            "text": (
                                "Too many in-flight messages on this connection — "
                                "wait for previous replies."
                            ),
                        })
                        continue

                    task = asyncio.create_task(
                        _handle_chat(ws, send_json, user_id, client_id, text)
                    )
                    chat_tasks.add(task)
                    task.add_done_callback(chat_tasks.discard)

                elif msg_type == "cancel":
                    pass

            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    finally:
        # Cancel any still-running chat tasks so they don't keep writing to
        # a closed WebSocket and don't leak memory after disconnect.
        for t in list(chat_tasks):
            if not t.done():
                t.cancel()
        _active_ws.pop(client_id, None)
        log.info("Web client disconnected: %s", client_id)

    return ws


async def _handle_command(
    ws: web.WebSocketResponse,
    send_json,
    user_id: str,
    text: str,
):
    cmd = text.split()[0].lower()
    args = text[len(cmd):].strip()

    if cmd == "/clear":
        sess = cc._session_mgr.get_or_create_active(PLATFORM, user_id)
        cc.clear_history(PLATFORM, user_id, session_name=sess.name)
        sess.claude_session_id = None
        await send_json({"type": "system", "text": "Conversation cleared."})

    elif cmd == "/new":
        cc._session_mgr.new_session(PLATFORM, user_id, args or None)
        await send_json({"type": "system", "text": f"New session started{': ' + args if args else ''}."})

    elif cmd == "/model":
        if args:
            await send_json({"type": "system", "text": f"Model set to: {args}"})
        else:
            await send_json({"type": "system", "text": f"Current model: {cc.CLAUDE_MODEL}"})

    elif cmd == "/export":
        await _handle_export(send_json, user_id, args)

    elif cmd == "/help":
        await send_json({
            "type": "system",
            "text": (
                "**Commands:**\n"
                "- `/clear` — clear conversation history\n"
                "- `/new [name]` — start a new session\n"
                "- `/model [name]` — show/set model\n"
                "- `/export [text|md|html|json]` — download this conversation\n"
                "- `/help` — show this help"
            ),
        })
    else:
        await send_json({"type": "system", "text": f"Unknown command: {cmd}. Type /help."})


_EXPORT_MIME = {
    "text": "text/plain",
    "markdown": "text/markdown",
    "html": "text/html",
    "json": "application/json",
}


async def _handle_export(send_json, user_id: str, fmt_arg: str) -> None:
    sess = cc._session_mgr.get_or_create_active(PLATFORM, user_id)
    history = cc.load_history(PLATFORM, user_id, session_name=sess.name)
    if not history:
        await send_json({"type": "system", "text": "No conversation to export."})
        return

    from . import conversation_export
    fmt = (fmt_arg or "text").strip().lower()
    try:
        result = conversation_export.export_conversation(
            history, fmt, title=f"TeleChat web session: {sess.name}"
        )
    except ValueError as exc:
        await send_json({"type": "system", "text": str(exc)})
        return

    await send_json({
        "type": "download",
        "filename": result.filename,
        "content": result.content,
        "mime": _EXPORT_MIME.get(result.format, "text/plain"),
        "message_count": result.message_count,
    })


async def _handle_chat(
    ws: web.WebSocketResponse,
    send_json,
    user_id: str,
    client_id: str,
    text: str,
):
    sess = cc._session_mgr.get_or_create_active(PLATFORM, user_id)
    history = cc.load_history(PLATFORM, user_id, session_name=sess.name)

    msg_id = secrets.token_hex(6)
    await send_json({"type": "thinking", "msg_id": msg_id})

    collected_text = []

    async def _on_progress(tool_name: str, detail: str = ""):
        await send_json({
            "type": "progress",
            "msg_id": msg_id,
            "tool": tool_name,
            "detail": detail,
        })

    async def _on_text(text_chunk: str):
        collected_text.append(text_chunk)
        await send_json({
            "type": "stream",
            "msg_id": msg_id,
            "text": text_chunk,
        })

    def _is_cancelled() -> bool:
        return ws.closed or client_id not in _active_ws

    try:
        engine = cc.CLAUDE_MODE

        if engine == "api":
            reply, stats = await cc.ask_claude_api_async(
                text, history,
                system=cc.CLAUDE_SYSTEM,
                on_text=_on_text,
                is_cancelled=_is_cancelled,
            )
        elif engine == "sdk":
            reply, stats = await cc.ask_claude_sdk(
                text, history,
                model=cc.CLAUDE_MODEL,
                system=cc.CLAUDE_SYSTEM,
                add_dirs=cc.CLAUDE_ADD_DIRS,
                timeout=cc.CLAUDE_TIMEOUT,
                on_progress=_on_progress,
                on_text=_on_text,
                is_cancelled=_is_cancelled,
            )
            if stats.get("session_id"):
                sess.claude_session_id = stats["session_id"]
                sess.touch()
        else:
            cli_sid = sess.claude_session_id if sess.cli_session_valid else ""
            reply, stats = await cc.ask_claude_async(
                text, history,
                model=cc.CLAUDE_MODEL,
                system=cc.CLAUDE_SYSTEM,
                add_dirs=cc.CLAUDE_ADD_DIRS,
                perm_mode=_DEFAULT_PERM,
                timeout=cc.CLAUDE_TIMEOUT,
                on_progress=_on_progress,
                on_text=_on_text,
                is_cancelled=_is_cancelled,
                platform=PLATFORM,
                user_id=user_id,
                resume_session_id=cli_sid or "",
            )
            if stats.get("session_id"):
                sess.claude_session_id = stats["session_id"]
                sess.touch()

        cc.save_turn(PLATFORM, user_id, text, reply, session_name=sess.name)

        if stats:
            cc.track_usage(
                PLATFORM, user_id,
                stats.get("input_tokens", 0),
                stats.get("output_tokens", 0),
            )
            # Record the turn whenever we have token counts, not only when the
            # backend reported a cost. Gating on a truthy cost_usd meant every
            # API-mode web turn was skipped entirely (the API returns tokens and
            # no cost), so those turns never reached cost_tracking and the budget
            # caps couldn't see them at all.
            if stats.get("input_tokens") or stats.get("output_tokens") or stats.get("cost_usd"):
                cc.track_cost(
                    PLATFORM, user_id,
                    stats.get("input_tokens", 0),
                    stats.get("output_tokens", 0),
                    stats.get("cost_usd", 0),
                )

        if not collected_text:
            await send_json({
                "type": "reply",
                "msg_id": msg_id,
                "text": reply,
                "stats": {
                    "input_tokens": stats.get("input_tokens", 0),
                    "output_tokens": stats.get("output_tokens", 0),
                    "cost_usd": stats.get("cost_usd", 0),
                    "tools_used": stats.get("tools_used", []),
                },
            })
        else:
            await send_json({
                "type": "done",
                "msg_id": msg_id,
                "stats": {
                    "input_tokens": stats.get("input_tokens", 0),
                    "output_tokens": stats.get("output_tokens", 0),
                    "cost_usd": stats.get("cost_usd", 0),
                    "tools_used": stats.get("tools_used", []),
                },
            })

    except Exception as exc:
        log.exception("Web chat error for client %s", client_id)
        await send_json({
            "type": "error",
            "msg_id": msg_id,
            "text": _friendly_error_text(exc),
        })


# Anthropic SDK exception class names, matched by name rather than imported so
# this module doesn't need `anthropic` installed just to classify an error
# that only occurs when it is (e.g. the CLI/SDK engines never raise these).
_RATE_LIMIT_ERRORS = {"RateLimitError"}
_AUTH_ERRORS = {"AuthenticationError", "PermissionDeniedError"}
_CONNECTION_ERRORS = {"APIConnectionError", "APITimeoutError"}


def _friendly_error_text(exc: Exception) -> str:
    """A message safe to show a browser client.

    `str(exc)` on an SDK/network failure can include request internals (host,
    status body, timeouts) that mean nothing to the person chatting and were
    previously shown verbatim as "Error: <that>". The real exception is still
    in the server log via `log.exception` above; this is only what the client
    sees.
    """
    name = type(exc).__name__
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "Claude took too long to respond. Please try again."
    if name in _RATE_LIMIT_ERRORS:
        return "Claude is receiving too many requests right now. Please wait a moment and try again."
    if name in _AUTH_ERRORS:
        return "The server's Claude credentials aren't working. Please contact the operator."
    if isinstance(exc, (ConnectionError, OSError)) or name in _CONNECTION_ERRORS:
        return "Lost connection to Claude. Please try again in a moment."
    return "Something went wrong processing your message. Please try again."


def _create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", _index_handler)
    app.router.add_get("/health", _health_handler)
    app.router.add_get("/ws", _ws_handler)
    return app


async def run_web_chat() -> None:
    # Refuse to expose the chat UI beyond loopback without an auth token,
    # unless the operator has explicitly opted in via WEB_CHAT_ALLOW_OPEN=1.
    if not _is_loopback_bind(WEB_BIND) and not WEB_AUTH_TOKEN:
        allow_open = os.getenv("WEB_CHAT_ALLOW_OPEN", "").lower() in ("1", "true", "yes", "on")
        if not allow_open:
            msg = (
                f"Refusing to start web chat: WEB_CHAT_BIND={WEB_BIND!r} exposes the UI "
                "with no WEB_CHAT_TOKEN set. Set WEB_CHAT_TOKEN, bind to 127.0.0.1, or "
                "set WEB_CHAT_ALLOW_OPEN=1 to override (NOT recommended)."
            )
            log.error(msg)
            print(f"❌ {msg}")
            return
        log.warning(
            "WEB_CHAT_ALLOW_OPEN=1 — running web chat on %s with no auth token. "
            "Anyone who can reach this host can use the bot.",
            WEB_BIND,
        )

    app = _create_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, WEB_BIND, WEB_PORT)
    await site.start()
    log.info("Web chat running on http://%s:%d", WEB_BIND, WEB_PORT)
    print(f"  Web chat: http://localhost:{WEB_PORT}")

    # Only offer the phone QR when a phone could actually reach it. On the
    # default loopback bind the LAN address in that QR refuses connections.
    if _is_loopback_bind(WEB_BIND):
        print("  Bound to 127.0.0.1 — reachable from this machine only.")
        print("  For your phone: set WEB_CHAT_BIND=0.0.0.0 and WEB_CHAT_TOKEN=<secret> in .env.")
    else:
        from .qr_util import print_web_qr
        print_web_qr(str(WEB_PORT))

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        await runner.cleanup()


def run_web_chat_sync() -> None:
    asyncio.run(run_web_chat())
