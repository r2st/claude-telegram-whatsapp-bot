"""
MCP Integration (Feature 8) — connect telechat to external MCP servers.

Inspired by the Blender MCP connector and Playwright MCP integration. Allows
telechat to extend its capabilities by connecting to any MCP-compatible server
(filesystem, web tools, databases, custom APIs).

The MCP client discovers available tools from connected servers and makes them
available to Claude during conversations.

Usage:
    from telechat_pkg.mcp_client import MCPManager
    mgr = MCPManager()
    mgr.add_server("filesystem", {"command": "npx", "args": ["-y", "@anthropic-ai/mcp-filesystem"]})
    tools = await mgr.list_tools()
    result = await mgr.call_tool("filesystem", "read_file", {"path": "/tmp/test.txt"})
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import stat
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger(__name__)

MCP_ENABLED = os.getenv("MCP_ENABLED", "false").lower() in ("1", "true", "yes")
MCP_CONFIG_FILE = os.getenv("MCP_CONFIG_FILE", "")

#: How long to wait for a single JSON-RPC response, in seconds. The handshake
#: is short because a server that cannot introduce itself promptly is broken;
#: a tool call gets longer because it may be doing real work.
MCP_HANDSHAKE_TIMEOUT = float(os.getenv("MCP_HANDSHAKE_TIMEOUT", "10"))
MCP_CALL_TIMEOUT = float(os.getenv("MCP_CALL_TIMEOUT", "30"))

#: Upper bound on lines skipped while looking for a matching response id.
#: The deadline alone is not enough: a server spewing junk faster than the
#: timeout would keep the loop spinning without ever advancing.
_MCP_MAX_SKIPPED_LINES = 100

# Defense in depth: even though MCP_CONFIG_FILE is typically operator-controlled,
# treat its contents as untrusted (it may be checked into a shared repo, written
# by another tool, or fetched from an LLM). Reject server commands not on the
# allowlist unless MCP_ALLOW_ANY_COMMAND=1 is explicitly set.
#
# Defaults cover the runtimes Anthropic's published MCP servers use today.
_MCP_DEFAULT_ALLOWED = {"npx", "node", "python", "python3", "uvx", "uv", "deno"}
_user_allowed = {
    s.strip() for s in os.getenv("MCP_ALLOWED_COMMANDS", "").split(",") if s.strip()
}
MCP_ALLOWED_COMMANDS = _MCP_DEFAULT_ALLOWED | _user_allowed
MCP_ALLOW_ANY_COMMAND = os.getenv("MCP_ALLOW_ANY_COMMAND", "0").lower() in ("1", "true", "yes", "on")

#: Optional strict mode: absolute directory prefixes the resolved executable
#: must live under. Empty (the default) means "anywhere that isn't obviously
#: attacker-writable" — see :func:`_untrusted_location_reason`.
MCP_ALLOWED_COMMAND_PATHS = tuple(
    os.path.abspath(p.strip())
    for p in os.getenv("MCP_ALLOWED_COMMAND_PATHS", "").split(os.pathsep)
    if p.strip()
)


def _allow_any_command() -> bool:
    """True when the allowlist is disabled, re-reading the env var each call."""
    return (
        MCP_ALLOW_ANY_COMMAND
        or os.getenv("MCP_ALLOW_ANY_COMMAND", "0").lower() in ("1", "true", "yes", "on")
    )


def _is_command_allowed(command: str) -> bool:
    """True if ``command``'s *name* is on the allowlist (or any-command mode is on).

    Reads the override env var each call so tests (and operators flipping it
    at runtime) get the current value, not whatever was set at import time.

    The check compares the executable's basename so absolute paths like
    ``/usr/local/bin/npx`` match the entry ``npx``. A basename match alone is
    **not** sufficient to execute anything — a name is trivially forgeable by
    planting a binary called ``npx`` on ``PATH`` — so :func:`resolve_allowed_command`
    layers path resolution and a location check on top. This function remains the
    name half of that decision.
    """
    if _allow_any_command():
        return True
    if not command:
        return False
    base = os.path.basename(command)
    return base in MCP_ALLOWED_COMMANDS


def _resolve_command_path(command: str) -> Optional[str]:
    """Resolve ``command`` to an absolute, symlink-free path, or None.

    A bare name is looked up on ``PATH`` exactly as the subprocess machinery
    would; anything containing a separator is treated as a path. Returns None
    when nothing executable is found, which is itself a refusal reason: a
    command that cannot be resolved now cannot be vetted either.
    """
    if not command:
        return None
    has_sep = os.sep in command or (os.altsep and os.altsep in command)
    if has_sep:
        path = os.path.abspath(command)
        if not (os.path.isfile(path) and os.access(path, os.X_OK)):
            return None
    else:
        path = shutil.which(command)
        if not path:
            return None
    return os.path.realpath(path)


def _untrusted_location_reason(path: str) -> Optional[str]:
    """Return why ``path`` is an unsafe place to exec from, or None if it is fine.

    The threat is a *planted* binary: an attacker who can write into a directory
    on ``PATH`` (``/tmp``, a shared cache, a checked-out repo) drops a file named
    ``npx`` and the basename allowlist waves it through. World-writable is the
    cheap, portable signal for "someone other than the operator can put a file
    here", and it is what catches the realistic version of this attack.

    When ``MCP_ALLOWED_COMMAND_PATHS`` is set the check is stricter still: the
    executable must sit under one of those prefixes.
    """
    if MCP_ALLOWED_COMMAND_PATHS:
        parent = os.path.dirname(path)
        under = any(
            parent == prefix or parent.startswith(prefix.rstrip(os.sep) + os.sep)
            for prefix in MCP_ALLOWED_COMMAND_PATHS
        )
        if not under:
            return (
                f"{path} is not under MCP_ALLOWED_COMMAND_PATHS "
                f"({os.pathsep.join(MCP_ALLOWED_COMMAND_PATHS)})"
            )
    for target in (os.path.dirname(path), path):
        try:
            mode = os.stat(target).st_mode
        except OSError:
            return f"cannot stat {target}"
        if mode & stat.S_IWOTH:
            return f"{target} is world-writable"
    return None


def resolve_allowed_command(command: str) -> tuple[Optional[str], Optional[str]]:
    """Vet ``command`` and return ``(resolved_path, refusal_reason)``.

    Exactly one of the two is non-None. The resolved path is what callers should
    actually exec: resolving once and spawning *that* closes the window where a
    ``PATH`` entry changes between the check and the call.

    Any-command mode short-circuits every check and hands the command back
    unchanged — operators who set it have opted out deliberately.
    """
    if _allow_any_command():
        return (command, None) if command else (None, "empty command")
    if not command:
        return None, "empty command"
    if not _is_command_allowed(command):
        return None, (
            f"command {command!r} not in allowlist ({', '.join(sorted(MCP_ALLOWED_COMMANDS))})"
        )
    resolved = _resolve_command_path(command)
    if not resolved:
        return None, f"command {command!r} not found on PATH (or not executable)"
    reason = _untrusted_location_reason(resolved)
    if reason:
        return None, f"refusing to exec {command!r}: {reason}"
    return resolved, None


# Environment variables safe to forward to an MCP subprocess. Everything else
# in the parent environment (ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, cloud creds,
# …) is withheld: an MCP server is a separate program and has no business seeing
# the bot's secrets. A server that genuinely needs a variable must declare it in
# its config `env` block, which is merged on top of this safe base.
_MCP_SAFE_ENV_PASSTHROUGH = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
    "TZ", "TMPDIR", "TEMP", "TMP", "TERM",
    # Windows infrastructure
    "SYSTEMROOT", "SYSTEMDRIVE", "PATHEXT", "WINDIR", "COMSPEC",
)


def _build_child_env(server_env: Optional[dict[str, str]]) -> dict[str, str]:
    """Build a minimal, scrubbed environment for an MCP subprocess.

    Only infrastructure variables (PATH, HOME, locale, temp dirs) are inherited
    from the parent process; secrets are NOT forwarded. The server-supplied
    ``env`` is merged on top, so a server can both add its own variables and
    override an inherited one.
    """
    env = {k: os.environ[k] for k in _MCP_SAFE_ENV_PASSTHROUGH if k in os.environ}
    env.update(server_env or {})
    return env


def _telechat_version() -> str:
    """Return the package version, falling back to ``unknown`` on import errors.

    Used for the MCP ``initialize`` handshake so the value tracks pyproject.toml
    automatically.
    """
    try:
        from . import __version__
        return __version__
    except Exception:  # pragma: no cover — defensive
        return "unknown"


class MCPProtocolError(Exception):
    """A server spoke JSON-RPC badly enough that the request cannot be answered."""


async def _read_response(
    stdout: Any,
    expect_id: int,
    timeout: float,
    *,
    server_name: str = "",
) -> dict:
    """Read stdout until the JSON-RPC response with ``expect_id`` arrives.

    A stdio MCP server's stdout is not a neat request/response queue. It may
    interleave notifications (``$/progress``, ``notifications/message``), which
    carry no ``id`` and are not anybody's answer; responses to *other* in-flight
    requests; and — from servers that log carelessly — plain text that is not
    JSON at all. Taking whatever line arrives next and calling it the answer is
    wrong in all three cases: the caller gets a notification's empty payload, or
    another call's result, and every later read is off by one for the life of
    the connection.

    So: read lines, skip anything that is not the response we asked for, and
    return the one that matches. Skipping is bounded twice over — by ``timeout``
    for a slow server and by :data:`_MCP_MAX_SKIPPED_LINES` for a chatty one —
    because either failure mode alone can starve the caller.

    Raises :class:`MCPProtocolError` on EOF or when the skip budget runs out,
    and :class:`asyncio.TimeoutError` when a single read exceeds ``timeout``.
    """
    for _ in range(_MCP_MAX_SKIPPED_LINES):
        line = await asyncio.wait_for(stdout.readline(), timeout=timeout)
        if not line:
            raise MCPProtocolError(
                f"server {server_name or '?'} closed its output stream "
                f"while awaiting response {expect_id}"
            )
        try:
            msg = json.loads(line.decode())
        except (ValueError, UnicodeDecodeError):
            # Not JSON — almost always a server logging to stdout instead of
            # stderr. Noise, not a protocol failure; keep looking.
            log.debug("MCP %s: skipping non-JSON stdout line", server_name)
            continue
        if not isinstance(msg, dict):
            continue
        if msg.get("id") == expect_id:
            return msg
        if "id" not in msg:
            log.debug("MCP %s: skipping notification %s", server_name, msg.get("method"))
        else:
            log.debug(
                "MCP %s: skipping response for id %r (awaiting %r)",
                server_name, msg.get("id"), expect_id,
            )
    raise MCPProtocolError(
        f"server {server_name or '?'} sent {_MCP_MAX_SKIPPED_LINES} messages "
        f"without answering request {expect_id}"
    )


@dataclass
class MCPTool:
    name: str
    description: str
    server: str
    input_schema: dict = field(default_factory=dict)


@dataclass
class MCPServer:
    name: str
    command: str
    #: Absolute path the allowlist actually vetted. ``connect`` spawns this
    #: rather than ``command`` so a PATH change after registration cannot
    #: swap in a different binary.
    resolved_command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    status: str = "disconnected"  # disconnected | connecting | connected | error
    tools: list[MCPTool] = field(default_factory=list)
    process: Optional[Any] = None
    #: Monotonic JSON-RPC request counter. Reusing an id (the old code sent
    #: ``3`` for every tools/call) makes concurrent calls indistinguishable —
    #: two in-flight requests would both match the first reply.
    _next_id: int = 0
    #: Serializes access to the one stdin/stdout pipe pair. Two coroutines
    #: writing requests and racing on readline() would hand each other's
    #: responses back; a tool loop that dispatches calls in parallel makes
    #: that a routine occurrence rather than a corner case.
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def next_id(self) -> int:
        self._next_id += 1
        return self._next_id


class MCPManager:
    """Manages connections to MCP servers and routes tool calls."""

    def __init__(self):
        self._servers: dict[str, MCPServer] = {}
        self._tools_cache: dict[str, MCPTool] = {}
        self._load_config()

    def _load_config(self):
        """Load MCP server config from file or environment."""
        if MCP_CONFIG_FILE and os.path.exists(MCP_CONFIG_FILE):
            try:
                with open(MCP_CONFIG_FILE) as f:
                    config = json.load(f)
                for name, cfg in config.get("mcpServers", {}).items():
                    self.add_server(name, cfg)
                log.info("Loaded %d MCP servers from config", len(self._servers))
            except Exception as e:
                log.error("Failed to load MCP config: %s", e)

    def add_server(self, name: str, config: dict):
        """Register an MCP server configuration.

        Refuses to register a server whose ``command`` fails
        :func:`resolve_allowed_command` — it must be on the MCP_ALLOWED_COMMANDS
        allowlist *and* resolve to an executable in a location the operator
        controls. This is a defense-in-depth check: an attacker who can write to
        MCP_CONFIG_FILE (or to a directory on PATH) should not also gain
        arbitrary local code execution at MCP-connect time.
        """
        command = config.get("command", "")
        resolved, reason = resolve_allowed_command(command)
        if reason or not resolved:
            log.error(
                "Refusing to register MCP server %r: %s. "
                "Set MCP_ALLOWED_COMMANDS / MCP_ALLOWED_COMMAND_PATHS, or "
                "MCP_ALLOW_ANY_COMMAND=1 to override.",
                name, reason,
            )
            return
        server = MCPServer(
            name=name,
            command=command,
            resolved_command=resolved,
            args=config.get("args", []),
            env=config.get("env", {}),
        )
        self._servers[name] = server
        log.info("Added MCP server: %s (%s)", name, server.command)

    def remove_server(self, name: str):
        if name in self._servers:
            del self._servers[name]
            # Clear cached tools from this server
            self._tools_cache = {
                k: v for k, v in self._tools_cache.items() if v.server != name
            }

    async def connect(self, server_name: str) -> bool:
        """Connect to an MCP server and discover its tools."""
        server = self._servers.get(server_name)
        if not server:
            return False

        server.status = "connecting"
        try:
            # Use stdio transport to connect to MCP server. The child env is
            # scrubbed to infrastructure vars only — the bot's secrets are not
            # forwarded to a third-party MCP server (see _build_child_env).
            env = _build_child_env(server.env)
            proc = await asyncio.create_subprocess_exec(
                server.resolved_command or server.command, *server.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            server.process = proc
            server.status = "connected"

            # Send initialize request
            init_id = server.next_id()
            init_msg = json.dumps({
                "jsonrpc": "2.0",
                "id": init_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "telechat", "version": _telechat_version()},
                },
            }) + "\n"
            proc.stdin.write(init_msg.encode())
            await proc.stdin.drain()

            # Drain the initialize response. Nothing in it is used yet, but it
            # must be consumed before tools/list or the reads fall out of step.
            await _read_response(
                proc.stdout, init_id, MCP_HANDSHAKE_TIMEOUT, server_name=server_name)

            # List tools
            list_id = server.next_id()
            list_msg = json.dumps({
                "jsonrpc": "2.0",
                "id": list_id,
                "method": "tools/list",
                "params": {},
            }) + "\n"
            proc.stdin.write(list_msg.encode())
            await proc.stdin.drain()

            tools_result = await _read_response(
                proc.stdout, list_id, MCP_HANDSHAKE_TIMEOUT, server_name=server_name)

            for tool_data in tools_result.get("result", {}).get("tools", []):
                tool = MCPTool(
                    name=tool_data["name"],
                    description=tool_data.get("description", ""),
                    server=server_name,
                    input_schema=tool_data.get("inputSchema", {}),
                )
                server.tools.append(tool)
                self._tools_cache[f"{server_name}.{tool.name}"] = tool

            log.info("Connected to MCP server %s, found %d tools", server_name, len(server.tools))
            return True

        except Exception as e:
            server.status = "error"
            log.error("Failed to connect to MCP server %s: %s", server_name, e)
            return False

    async def connect_all(self):
        """Connect to all configured servers."""
        for name in self._servers:
            await self.connect(name)

    async def disconnect(self, server_name: str):
        server = self._servers.get(server_name)
        if server and server.process:
            server.process.terminate()
            await server.process.wait()
            server.status = "disconnected"
            server.tools = []

    async def disconnect_all(self):
        for name in list(self._servers):
            await self.disconnect(name)

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> dict:
        """Call a tool on a specific MCP server."""
        server = self._servers.get(server_name)
        if not server or server.status != "connected" or not server.process:
            return {"error": f"Server {server_name} not connected"}

        try:
            # One pipe pair per server, so a request and its response must stay
            # adjacent: hold the lock across write-then-read.
            async with server._lock:
                call_id = server.next_id()
                msg = json.dumps({
                    "jsonrpc": "2.0",
                    "id": call_id,
                    "method": "tools/call",
                    "params": {
                        "name": tool_name,
                        "arguments": arguments,
                    },
                }) + "\n"
                server.process.stdin.write(msg.encode())
                await server.process.stdin.drain()

                response = await _read_response(
                    server.process.stdout, call_id, MCP_CALL_TIMEOUT,
                    server_name=server_name,
                )

            # A JSON-RPC error is a failed call, not an empty result. Reporting
            # `{}` for it (as `result.get("result", {})` alone would) tells the
            # model the tool succeeded and returned nothing.
            if "error" in response:
                err = response["error"]
                detail = err.get("message", err) if isinstance(err, dict) else err
                log.warning("MCP tool error: %s.%s: %s", server_name, tool_name, detail)
                return {"error": str(detail)}
            return response.get("result", {})

        except asyncio.TimeoutError:
            log.error(
                "MCP tool call timed out after %ss: %s.%s",
                MCP_CALL_TIMEOUT, server_name, tool_name,
            )
            return {"error": f"{server_name}.{tool_name} timed out after {MCP_CALL_TIMEOUT}s"}
        except Exception as e:
            log.error("MCP tool call failed: %s.%s: %s", server_name, tool_name, e)
            return {"error": str(e)}

    def list_tools(self) -> list[MCPTool]:
        """List all available tools from connected servers."""
        return list(self._tools_cache.values())

    def list_servers(self) -> list[dict]:
        """List all servers with their status."""
        return [
            {
                "name": s.name,
                "command": s.command,
                # The vetted absolute path actually spawned — an operator
                # debugging "which npx is this?" should not have to guess.
                "resolved_command": s.resolved_command,
                "status": s.status,
                "tools_count": len(s.tools),
                "tools": [t.name for t in s.tools],
            }
            for s in self._servers.values()
        ]

    def get_tools_for_prompt(self) -> str:
        """Format available MCP tools for inclusion in Claude's system prompt."""
        tools = self.list_tools()
        if not tools:
            return ""
        lines = ["\n\nAvailable MCP tools:"]
        for t in tools:
            lines.append(f"- {t.server}.{t.name}: {t.description}")
        return "\n".join(lines)


# Singleton
_mcp_manager: MCPManager | None = None


def get_mcp_manager() -> MCPManager:
    global _mcp_manager
    if _mcp_manager is None:
        _mcp_manager = MCPManager()
    return _mcp_manager
