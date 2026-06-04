"""
Behavior-organized tests for telechat_pkg.mcp_client.

SECURITY-RELEVANT module: MCPManager spawns subprocesses for MCP servers
(``asyncio.create_subprocess_exec(server.command, ...)``). These tests cover
the connection lifecycle (connect / list-tools / call-tool / disconnect /
cleanup-on-error) AND the security invariants the CODE_REVIEW HIGH finding
calls out:

  * malformed / attacker-controlled config is rejected (command allowlist),
  * subprocess env is scrubbed to infrastructure vars only — the bot's
    secrets are NOT forwarded to a third-party MCP server (ticket 0019 fix
    of the CODE_REVIEW HIGH §3 finding); server-declared env is merged in,
  * the subprocess is terminated/awaited on disconnect (no orphan),
  * stdout reads are bounded by ``asyncio.wait_for`` timeouts.

No real subprocesses are spawned: ``asyncio.create_subprocess_exec`` is
monkeypatched with a fake process that speaks JSON-RPC over fake streams.

Run:
    pytest tests/test_mcp_client.py -v
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")

from telechat_pkg import mcp_client
from telechat_pkg.mcp_client import (
    MCPManager,
    MCPServer,
    MCPTool,
    _is_command_allowed,
    _telechat_version,
    get_mcp_manager,
)


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures / fakes
# ══════════════════════════════════════════════════════════════════════════════


class FakeStream:
    """Minimal stand-in for an asyncio StreamReader/StreamWriter pair."""

    def __init__(self, responses: list[bytes] | None = None):
        # Queue of byte-lines that readline() will hand back, in order.
        self._responses = list(responses or [])
        self.written: list[bytes] = []
        self.drained = 0

    # --- writer side (stdin) ---
    def write(self, data: bytes):
        self.written.append(data)

    async def drain(self):
        self.drained += 1

    # --- reader side (stdout) ---
    async def readline(self):
        if self._responses:
            return self._responses.pop(0)
        return b""


class FakeProcess:
    """Fake asyncio subprocess transport."""

    def __init__(self, responses: list[bytes] | None = None):
        self.stdin = FakeStream()
        self.stdout = FakeStream(responses)
        self.terminated = False
        self.waited = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    async def wait(self):
        self.waited = True
        return 0


def _jsonrpc(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def _init_and_tools(tools: list[dict] | None = None) -> list[bytes]:
    """Canned init-handshake + tools/list responses for a successful connect."""
    return [
        _jsonrpc({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}}),
        _jsonrpc({"jsonrpc": "2.0", "id": 2, "result": {"tools": tools or []}}),
    ]


@pytest.fixture
def patch_subprocess(monkeypatch):
    """Patch create_subprocess_exec to return a caller-supplied FakeProcess.

    Returns a recorder dict capturing the positional command/args and the
    ``env`` kwarg the manager passes, so tests can assert on the spawn.
    """
    recorder: dict = {"calls": []}

    def make(proc: FakeProcess):
        async def _fake_exec(*args, **kwargs):
            recorder["calls"].append({"args": args, "kwargs": kwargs})
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        return recorder

    recorder["install"] = make
    return recorder


@pytest.fixture
def mgr(monkeypatch):
    """A fresh MCPManager with no config file loaded.

    Force MCP_CONFIG_FILE empty so __init__'s _load_config is a no-op and the
    manager starts with zero servers regardless of the developer's env.
    """
    monkeypatch.setattr(mcp_client, "MCP_CONFIG_FILE", "")
    return MCPManager()


# ══════════════════════════════════════════════════════════════════════════════
# 1. Command allowlist (SECURITY: who may be exec'd)
# ══════════════════════════════════════════════════════════════════════════════


class TestCommandAllowlist:
    def test_default_runtimes_allowed(self, monkeypatch):
        # With any-command mode off, only the curated runtimes pass.
        monkeypatch.delenv("MCP_ALLOW_ANY_COMMAND", raising=False)
        monkeypatch.setattr(mcp_client, "MCP_ALLOW_ANY_COMMAND", False)
        for cmd in ("npx", "node", "python", "python3", "uvx", "uv", "deno"):
            assert _is_command_allowed(cmd) is True

    def test_arbitrary_command_rejected(self, monkeypatch):
        monkeypatch.delenv("MCP_ALLOW_ANY_COMMAND", raising=False)
        monkeypatch.setattr(mcp_client, "MCP_ALLOW_ANY_COMMAND", False)
        assert _is_command_allowed("rm") is False
        assert _is_command_allowed("/bin/sh") is False
        assert _is_command_allowed("curl") is False

    def test_empty_command_rejected(self, monkeypatch):
        monkeypatch.delenv("MCP_ALLOW_ANY_COMMAND", raising=False)
        monkeypatch.setattr(mcp_client, "MCP_ALLOW_ANY_COMMAND", False)
        assert _is_command_allowed("") is False

    def test_basename_match_for_absolute_path(self, monkeypatch):
        # An absolute path whose basename is allowlisted should pass...
        monkeypatch.delenv("MCP_ALLOW_ANY_COMMAND", raising=False)
        monkeypatch.setattr(mcp_client, "MCP_ALLOW_ANY_COMMAND", False)
        assert _is_command_allowed("/usr/local/bin/npx") is True
        # ...but a non-allowlisted basename does not.
        assert _is_command_allowed("/usr/local/bin/evil") is False

    def test_any_command_via_env_var(self, monkeypatch):
        # Env var is re-read each call; module constant stays False.
        monkeypatch.setattr(mcp_client, "MCP_ALLOW_ANY_COMMAND", False)
        monkeypatch.setenv("MCP_ALLOW_ANY_COMMAND", "1")
        assert _is_command_allowed("anything-goes") is True

    def test_any_command_via_module_constant(self, monkeypatch):
        monkeypatch.delenv("MCP_ALLOW_ANY_COMMAND", raising=False)
        monkeypatch.setattr(mcp_client, "MCP_ALLOW_ANY_COMMAND", True)
        assert _is_command_allowed("rm") is True


# ══════════════════════════════════════════════════════════════════════════════
# 2. Version helper
# ══════════════════════════════════════════════════════════════════════════════


class TestVersionHelper:
    def test_returns_package_version(self):
        from telechat_pkg import __version__
        assert _telechat_version() == __version__


# ══════════════════════════════════════════════════════════════════════════════
# 3. Config loading (SECURITY: malformed / attacker config)
# ══════════════════════════════════════════════════════════════════════════════


class TestLoadConfig:
    def test_no_config_file_means_no_servers(self, mgr):
        assert mgr.list_servers() == []

    def test_loads_servers_from_config_file(self, monkeypatch, tmp_path):
        cfg = tmp_path / "mcp.json"
        cfg.write_text(json.dumps({
            "mcpServers": {
                "fs": {"command": "npx", "args": ["-y", "@x/fs"]},
                "py": {"command": "python3", "args": ["-m", "srv"]},
            }
        }))
        monkeypatch.setattr(mcp_client, "MCP_CONFIG_FILE", str(cfg))
        m = MCPManager()
        names = {s["name"] for s in m.list_servers()}
        assert names == {"fs", "py"}

    def test_malformed_json_is_swallowed_not_raised(self, monkeypatch, tmp_path):
        cfg = tmp_path / "broken.json"
        cfg.write_text("{ this is not valid json ")
        monkeypatch.setattr(mcp_client, "MCP_CONFIG_FILE", str(cfg))
        # __init__ -> _load_config must not raise on broken config.
        m = MCPManager()
        assert m.list_servers() == []

    def test_config_with_disallowed_command_rejected(self, monkeypatch, tmp_path):
        # SECURITY: an attacker-written config naming a non-allowlisted command
        # must NOT register a server (and thus can't be exec'd later).
        monkeypatch.setattr(mcp_client, "MCP_ALLOW_ANY_COMMAND", False)
        monkeypatch.delenv("MCP_ALLOW_ANY_COMMAND", raising=False)
        cfg = tmp_path / "evil.json"
        cfg.write_text(json.dumps({
            "mcpServers": {"pwn": {"command": "/bin/sh", "args": ["-c", "rm -rf /"]}}
        }))
        monkeypatch.setattr(mcp_client, "MCP_CONFIG_FILE", str(cfg))
        m = MCPManager()
        assert m.list_servers() == []

    def test_missing_config_path_is_noop(self, monkeypatch):
        monkeypatch.setattr(mcp_client, "MCP_CONFIG_FILE", "/no/such/path/mcp.json")
        m = MCPManager()
        assert m.list_servers() == []

    def test_config_without_mcpservers_key(self, monkeypatch, tmp_path):
        cfg = tmp_path / "empty.json"
        cfg.write_text(json.dumps({"somethingElse": 1}))
        monkeypatch.setattr(mcp_client, "MCP_CONFIG_FILE", str(cfg))
        m = MCPManager()
        assert m.list_servers() == []


# ══════════════════════════════════════════════════════════════════════════════
# 4. add_server / remove_server (SECURITY: registration allowlist)
# ══════════════════════════════════════════════════════════════════════════════


class TestAddRemoveServer:
    def test_add_allowed_server(self, mgr, monkeypatch):
        monkeypatch.setenv("MCP_ALLOW_ANY_COMMAND", "1")
        mgr.add_server("svc", {"command": "npx", "args": ["a"], "env": {"K": "V"}})
        servers = mgr.list_servers()
        assert len(servers) == 1
        assert servers[0]["name"] == "svc"
        assert servers[0]["command"] == "npx"
        assert servers[0]["status"] == "disconnected"

    def test_add_stores_args_and_env(self, mgr, monkeypatch):
        monkeypatch.setenv("MCP_ALLOW_ANY_COMMAND", "1")
        mgr.add_server("svc", {"command": "node", "args": ["x", "y"], "env": {"A": "B"}})
        srv = mgr._servers["svc"]
        assert srv.args == ["x", "y"]
        assert srv.env == {"A": "B"}

    def test_add_defaults_when_args_env_missing(self, mgr, monkeypatch):
        monkeypatch.setenv("MCP_ALLOW_ANY_COMMAND", "1")
        mgr.add_server("svc", {"command": "npx"})
        srv = mgr._servers["svc"]
        assert srv.args == []
        assert srv.env == {}

    def test_add_disallowed_command_is_refused(self, mgr, monkeypatch):
        monkeypatch.setattr(mcp_client, "MCP_ALLOW_ANY_COMMAND", False)
        monkeypatch.delenv("MCP_ALLOW_ANY_COMMAND", raising=False)
        mgr.add_server("evil", {"command": "rm", "args": ["-rf", "/"]})
        assert "evil" not in mgr._servers

    def test_add_missing_command_refused(self, mgr, monkeypatch):
        monkeypatch.setattr(mcp_client, "MCP_ALLOW_ANY_COMMAND", False)
        monkeypatch.delenv("MCP_ALLOW_ANY_COMMAND", raising=False)
        mgr.add_server("nocmd", {"args": ["x"]})
        assert "nocmd" not in mgr._servers

    def test_remove_server(self, mgr, monkeypatch):
        monkeypatch.setenv("MCP_ALLOW_ANY_COMMAND", "1")
        mgr.add_server("svc", {"command": "npx"})
        mgr.remove_server("svc")
        assert "svc" not in mgr._servers

    def test_remove_nonexistent_is_noop(self, mgr):
        mgr.remove_server("ghost")  # must not raise
        assert mgr.list_servers() == []

    def test_remove_clears_cached_tools_for_that_server(self, mgr):
        # Seed the tool cache directly with tools from two servers.
        mgr._tools_cache = {
            "a.t1": MCPTool("t1", "", "a"),
            "a.t2": MCPTool("t2", "", "a"),
            "b.t3": MCPTool("t3", "", "b"),
        }
        mgr._servers["a"] = MCPServer("a", "npx")
        mgr.remove_server("a")
        assert set(mgr._tools_cache) == {"b.t3"}


# ══════════════════════════════════════════════════════════════════════════════
# 5. connect() lifecycle
# ══════════════════════════════════════════════════════════════════════════════


class TestConnect:
    @pytest.mark.asyncio
    async def test_connect_unknown_server_returns_false(self, mgr):
        assert await mgr.connect("missing") is False

    @pytest.mark.asyncio
    async def test_connect_success_discovers_tools(self, mgr, monkeypatch, patch_subprocess):
        monkeypatch.setenv("MCP_ALLOW_ANY_COMMAND", "1")
        mgr.add_server("svc", {"command": "npx"})
        proc = FakeProcess(_init_and_tools([
            {"name": "read", "description": "Read a file", "inputSchema": {"type": "object"}},
            {"name": "write"},
        ]))
        patch_subprocess["install"](proc)

        assert await mgr.connect("svc") is True
        srv = mgr._servers["svc"]
        assert srv.status == "connected"
        assert srv.process is proc
        assert [t.name for t in srv.tools] == ["read", "write"]
        # Tools cached under "server.tool" key.
        assert "svc.read" in mgr._tools_cache
        assert mgr._tools_cache["svc.read"].description == "Read a file"
        # Tool with no description defaults to empty string.
        assert mgr._tools_cache["svc.write"].description == ""

    @pytest.mark.asyncio
    async def test_connect_sends_initialize_and_list_messages(self, mgr, monkeypatch, patch_subprocess):
        monkeypatch.setenv("MCP_ALLOW_ANY_COMMAND", "1")
        mgr.add_server("svc", {"command": "npx"})
        proc = FakeProcess(_init_and_tools())
        patch_subprocess["install"](proc)

        await mgr.connect("svc")
        sent = [json.loads(b.decode()) for b in proc.stdin.written]
        methods = [m["method"] for m in sent]
        assert methods == ["initialize", "tools/list"]
        # Version comes from the package, surfaced in clientInfo.
        assert sent[0]["params"]["clientInfo"]["version"] == _telechat_version()

    @pytest.mark.asyncio
    async def test_connect_failure_sets_error_status_and_returns_false(self, mgr, monkeypatch):
        monkeypatch.setenv("MCP_ALLOW_ANY_COMMAND", "1")
        mgr.add_server("svc", {"command": "npx"})

        async def _boom(*a, **k):
            raise OSError("exec failed")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)
        assert await mgr.connect("svc") is False
        assert mgr._servers["svc"].status == "error"

    @pytest.mark.asyncio
    async def test_connect_malformed_response_returns_false(self, mgr, monkeypatch, patch_subprocess):
        # Garbage on stdout -> json.loads raises -> caught -> error status.
        monkeypatch.setenv("MCP_ALLOW_ANY_COMMAND", "1")
        mgr.add_server("svc", {"command": "npx"})
        proc = FakeProcess([b"not json\n"])
        patch_subprocess["install"](proc)
        assert await mgr.connect("svc") is False
        assert mgr._servers["svc"].status == "error"

    @pytest.mark.asyncio
    async def test_connect_all(self, mgr, monkeypatch, patch_subprocess):
        monkeypatch.setenv("MCP_ALLOW_ANY_COMMAND", "1")
        mgr.add_server("a", {"command": "npx"})
        mgr.add_server("b", {"command": "npx"})

        # Each connect pops two responses; give a fresh proc per call.
        procs = [FakeProcess(_init_and_tools()), FakeProcess(_init_and_tools())]

        async def _fake_exec(*a, **k):
            return procs.pop(0)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        await mgr.connect_all()
        assert mgr._servers["a"].status == "connected"
        assert mgr._servers["b"].status == "connected"


# ══════════════════════════════════════════════════════════════════════════════
# 6. Subprocess env (SECURITY: env merge / no scrubbing — CURRENT behavior)
# ══════════════════════════════════════════════════════════════════════════════


class TestSubprocessEnvironment:
    @pytest.mark.asyncio
    async def test_env_is_scrubbed_of_parent_secrets(self, mgr, monkeypatch, patch_subprocess):
        # SECURE behavior (ticket 0019 fix of CODE_REVIEW HIGH §3): the child env
        # is scrubbed to infrastructure vars only. Arbitrary parent secrets are
        # NOT forwarded to a third-party MCP server; PATH (needed to resolve the
        # command) is preserved; server-declared env is merged in.
        monkeypatch.setenv("MCP_ALLOW_ANY_COMMAND", "1")
        monkeypatch.setenv("PARENT_SECRET", "should-not-leak")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        mgr.add_server("svc", {"command": "npx", "env": {"SERVER_KEY": "v"}})
        proc = FakeProcess(_init_and_tools())
        rec = patch_subprocess["install"](proc)

        await mgr.connect("svc")
        env = rec["calls"][0]["kwargs"]["env"]
        # The parent secret is withheld from the child.
        assert "PARENT_SECRET" not in env
        # Infrastructure (PATH) is still passed so the command resolves.
        assert env.get("PATH") == "/usr/bin:/bin"
        # Server-supplied env is merged in.
        assert env.get("SERVER_KEY") == "v"

    @pytest.mark.asyncio
    async def test_server_env_overrides_parent(self, mgr, monkeypatch, patch_subprocess):
        monkeypatch.setenv("MCP_ALLOW_ANY_COMMAND", "1")
        monkeypatch.setenv("OVERRIDE_ME", "parent")
        mgr.add_server("svc", {"command": "npx", "env": {"OVERRIDE_ME": "child"}})
        proc = FakeProcess(_init_and_tools())
        rec = patch_subprocess["install"](proc)
        await mgr.connect("svc")
        assert rec["calls"][0]["kwargs"]["env"]["OVERRIDE_ME"] == "child"

    @pytest.mark.asyncio
    async def test_command_and_args_passed_positionally(self, mgr, monkeypatch, patch_subprocess):
        monkeypatch.setenv("MCP_ALLOW_ANY_COMMAND", "1")
        mgr.add_server("svc", {"command": "npx", "args": ["-y", "pkg"]})
        proc = FakeProcess(_init_and_tools())
        rec = patch_subprocess["install"](proc)
        await mgr.connect("svc")
        assert rec["calls"][0]["args"] == ("npx", "-y", "pkg")


# ══════════════════════════════════════════════════════════════════════════════
# 7. disconnect() / cleanup (SECURITY: no orphaned subprocess)
# ══════════════════════════════════════════════════════════════════════════════


class TestDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_terminates_and_waits(self, mgr, monkeypatch, patch_subprocess):
        monkeypatch.setenv("MCP_ALLOW_ANY_COMMAND", "1")
        mgr.add_server("svc", {"command": "npx"})
        proc = FakeProcess(_init_and_tools([{"name": "t"}]))
        patch_subprocess["install"](proc)
        await mgr.connect("svc")

        await mgr.disconnect("svc")
        assert proc.terminated is True
        assert proc.waited is True
        srv = mgr._servers["svc"]
        assert srv.status == "disconnected"
        assert srv.tools == []

    @pytest.mark.asyncio
    async def test_disconnect_unknown_server_is_noop(self, mgr):
        await mgr.disconnect("ghost")  # no raise

    @pytest.mark.asyncio
    async def test_disconnect_server_with_no_process_is_noop(self, mgr, monkeypatch):
        monkeypatch.setenv("MCP_ALLOW_ANY_COMMAND", "1")
        mgr.add_server("svc", {"command": "npx"})  # never connected, process=None
        await mgr.disconnect("svc")  # must not raise
        assert mgr._servers["svc"].process is None

    @pytest.mark.asyncio
    async def test_disconnect_all_terminates_every_process(self, mgr, monkeypatch):
        monkeypatch.setenv("MCP_ALLOW_ANY_COMMAND", "1")
        mgr.add_server("a", {"command": "npx"})
        mgr.add_server("b", {"command": "npx"})
        procs = [FakeProcess(_init_and_tools()), FakeProcess(_init_and_tools())]

        async def _fake_exec(*a, **k):
            return procs.pop(0)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        await mgr.connect_all()
        await mgr.disconnect_all()
        for srv in mgr._servers.values():
            assert srv.process.terminated is True
            assert srv.status == "disconnected"


# ══════════════════════════════════════════════════════════════════════════════
# 8. call_tool()
# ══════════════════════════════════════════════════════════════════════════════


class TestCallTool:
    @pytest.mark.asyncio
    async def test_call_tool_not_connected_returns_error(self, mgr, monkeypatch):
        monkeypatch.setenv("MCP_ALLOW_ANY_COMMAND", "1")
        mgr.add_server("svc", {"command": "npx"})  # status=disconnected
        result = await mgr.call_tool("svc", "read", {})
        assert "error" in result
        assert "not connected" in result["error"]

    @pytest.mark.asyncio
    async def test_call_tool_unknown_server_returns_error(self, mgr):
        result = await mgr.call_tool("ghost", "read", {})
        assert "not connected" in result["error"]

    @pytest.mark.asyncio
    async def test_call_tool_success_returns_result_payload(self, mgr, monkeypatch, patch_subprocess):
        monkeypatch.setenv("MCP_ALLOW_ANY_COMMAND", "1")
        mgr.add_server("svc", {"command": "npx"})
        proc = FakeProcess(_init_and_tools([{"name": "read"}]))
        patch_subprocess["install"](proc)
        await mgr.connect("svc")
        # Prime the next stdout line for the tools/call response.
        proc.stdout._responses.append(
            _jsonrpc({"jsonrpc": "2.0", "id": 3, "result": {"content": "hello"}})
        )

        result = await mgr.call_tool("svc", "read", {"path": "/tmp/x"})
        assert result == {"content": "hello"}
        # The request that went out names the tool and arguments.
        last = json.loads(proc.stdin.written[-1].decode())
        assert last["method"] == "tools/call"
        assert last["params"]["name"] == "read"
        assert last["params"]["arguments"] == {"path": "/tmp/x"}

    @pytest.mark.asyncio
    async def test_call_tool_missing_result_key_returns_empty(self, mgr, monkeypatch, patch_subprocess):
        monkeypatch.setenv("MCP_ALLOW_ANY_COMMAND", "1")
        mgr.add_server("svc", {"command": "npx"})
        proc = FakeProcess(_init_and_tools())
        patch_subprocess["install"](proc)
        await mgr.connect("svc")
        proc.stdout._responses.append(_jsonrpc({"jsonrpc": "2.0", "id": 3}))
        result = await mgr.call_tool("svc", "read", {})
        assert result == {}

    @pytest.mark.asyncio
    async def test_call_tool_malformed_response_returns_error(self, mgr, monkeypatch, patch_subprocess):
        monkeypatch.setenv("MCP_ALLOW_ANY_COMMAND", "1")
        mgr.add_server("svc", {"command": "npx"})
        proc = FakeProcess(_init_and_tools())
        patch_subprocess["install"](proc)
        await mgr.connect("svc")
        proc.stdout._responses.append(b"garbage not json\n")
        result = await mgr.call_tool("svc", "read", {})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_call_tool_timeout_returns_error(self, mgr, monkeypatch, patch_subprocess):
        # SECURITY: stdout reads are bounded. Force wait_for to time out and
        # assert the manager reports an error rather than hanging.
        monkeypatch.setenv("MCP_ALLOW_ANY_COMMAND", "1")
        mgr.add_server("svc", {"command": "npx"})
        proc = FakeProcess(_init_and_tools())
        patch_subprocess["install"](proc)
        await mgr.connect("svc")

        async def _timeout(aw, *a, **k):
            # Close the readline() coroutine we're "waiting" on so we don't
            # emit an un-awaited-coroutine RuntimeWarning, then time out.
            if asyncio.iscoroutine(aw):
                aw.close()
            raise asyncio.TimeoutError()

        monkeypatch.setattr(asyncio, "wait_for", _timeout)
        result = await mgr.call_tool("svc", "read", {})
        assert "error" in result


# ══════════════════════════════════════════════════════════════════════════════
# 9. Connect timeout enforcement (SECURITY: bounded reads on handshake)
# ══════════════════════════════════════════════════════════════════════════════


class TestConnectTimeout:
    @pytest.mark.asyncio
    async def test_connect_timeout_treated_as_failure(self, mgr, monkeypatch, patch_subprocess):
        monkeypatch.setenv("MCP_ALLOW_ANY_COMMAND", "1")
        mgr.add_server("svc", {"command": "npx"})
        proc = FakeProcess(_init_and_tools())
        patch_subprocess["install"](proc)

        async def _timeout(aw, *a, **k):
            if asyncio.iscoroutine(aw):
                aw.close()
            raise asyncio.TimeoutError()

        monkeypatch.setattr(asyncio, "wait_for", _timeout)
        assert await mgr.connect("svc") is False
        assert mgr._servers["svc"].status == "error"


# ══════════════════════════════════════════════════════════════════════════════
# 10. Tool listing / prompt formatting
# ══════════════════════════════════════════════════════════════════════════════


class TestToolListing:
    def test_list_tools_empty(self, mgr):
        assert mgr.list_tools() == []

    def test_list_tools_returns_cached(self, mgr):
        mgr._tools_cache = {"s.a": MCPTool("a", "d", "s")}
        tools = mgr.list_tools()
        assert len(tools) == 1
        assert tools[0].name == "a"

    def test_list_servers_reports_status_and_tool_count(self, mgr):
        srv = MCPServer("s", "npx", status="connected")
        srv.tools = [MCPTool("t1", "", "s"), MCPTool("t2", "", "s")]
        mgr._servers["s"] = srv
        info = mgr.list_servers()[0]
        assert info["status"] == "connected"
        assert info["tools_count"] == 2
        assert info["tools"] == ["t1", "t2"]

    def test_get_tools_for_prompt_empty(self, mgr):
        assert mgr.get_tools_for_prompt() == ""

    def test_get_tools_for_prompt_formats_each_tool(self, mgr):
        mgr._tools_cache = {
            "s.read": MCPTool("read", "Read a file", "s"),
        }
        prompt = mgr.get_tools_for_prompt()
        assert "Available MCP tools:" in prompt
        assert "- s.read: Read a file" in prompt


# ══════════════════════════════════════════════════════════════════════════════
# 11. Dataclasses
# ══════════════════════════════════════════════════════════════════════════════


class TestDataclasses:
    def test_mcptool_defaults(self):
        t = MCPTool("n", "d", "s")
        assert t.input_schema == {}

    def test_mcpserver_defaults(self):
        s = MCPServer("n", "npx")
        assert s.args == []
        assert s.env == {}
        assert s.status == "disconnected"
        assert s.tools == []
        assert s.process is None


# ══════════════════════════════════════════════════════════════════════════════
# 12. Singleton accessor
# ══════════════════════════════════════════════════════════════════════════════


class TestSingleton:
    def test_get_mcp_manager_returns_singleton(self, monkeypatch):
        # Reset the module global so we exercise the lazy-init branch, then the
        # cached branch.
        monkeypatch.setattr(mcp_client, "_mcp_manager", None)
        monkeypatch.setattr(mcp_client, "MCP_CONFIG_FILE", "")
        first = get_mcp_manager()
        second = get_mcp_manager()
        assert first is second
        assert isinstance(first, MCPManager)


# NOTE: the ``except Exception -> "unknown"`` fallback in _telechat_version is
# marked ``# pragma: no cover`` in the source (defensive — telechat_pkg is always
# importable once the suite is running). It is intentionally not exercised here;
# coverage already reports 100% without it.
