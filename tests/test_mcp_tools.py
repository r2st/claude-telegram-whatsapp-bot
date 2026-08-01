"""
Behavior tests for telechat_pkg.mcp_tools — the bridge that makes MCP tools
callable during a conversation rather than merely described in the prompt.

Covered:
  * tool names Claude sees are valid for the API, unique, and route back to
    exactly the server that owns them,
  * MCP result blocks are flattened into tool_result content, with server
    errors and transport errors both marked is_error,
  * the loop terminates: on end_turn, and on the turn cap,
  * parallel tool calls come back in a SINGLE user message (splitting them
    trains the model out of asking for parallel calls),
  * caller-supplied history is not mutated.

No network and no subprocesses: the Anthropic client and the MCP manager are
both fakes.

Run:
    pytest tests/test_mcp_tools.py -v
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")

from telechat_pkg import mcp_tools
from telechat_pkg.mcp_client import MCPTool


# ══════════════════════════════════════════════════════════════════════════════
# Fakes
# ══════════════════════════════════════════════════════════════════════════════


class Block:
    """Stand-in for an SDK content block (TextBlock / ToolUseBlock)."""

    def __init__(self, type, **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


class Usage:
    def __init__(self, i=10, o=5):
        self.input_tokens = i
        self.output_tokens = o


class Response:
    def __init__(self, content, stop_reason="end_turn", usage=None):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage or Usage()


class FakeMessages:
    """Replays a scripted list of responses and records each request."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests: list[dict] = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        if not self._responses:
            return Response([Block("text", text="done")])
        return self._responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


class FakeManager:
    """Minimal MCPManager surface: list_tools + call_tool."""

    def __init__(self, tools=None, results=None):
        self._tools = tools or []
        #: (server, tool) -> result payload
        self._results = results or {}
        self.calls: list[tuple[str, str, dict]] = []
        self.concurrent = 0
        self.max_concurrent = 0

    def list_tools(self):
        return list(self._tools)

    async def call_tool(self, server, tool, arguments):
        self.calls.append((server, tool, arguments))
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        await asyncio.sleep(0)  # yield so overlap is observable
        self.concurrent -= 1
        return self._results.get((server, tool), {"content": [{"type": "text", "text": "ok"}]})


def text_result(s):
    return {"content": [{"type": "text", "text": s}]}


# ══════════════════════════════════════════════════════════════════════════════
# 1. Tool naming and routing
# ══════════════════════════════════════════════════════════════════════════════


class TestRegistry:
    def test_exposed_name_joins_server_and_tool(self):
        r = mcp_tools.ToolRegistry()
        assert r.add("files", "read_file", "d", {}) == "files__read_file"

    def test_name_matches_the_apis_charset(self):
        # The API accepts ^[a-zA-Z0-9_-]{1,128}$ — dots and spaces must go.
        r = mcp_tools.ToolRegistry()
        name = r.add("my.server v2", "read file!", "d", {})
        assert __import__("re").fullmatch(r"[a-zA-Z0-9_-]{1,128}", name), name

    def test_name_is_capped_at_128_characters(self):
        r = mcp_tools.ToolRegistry()
        name = r.add("s" * 200, "t" * 200, "d", {})
        assert len(name) <= 128

    def test_routes_back_to_the_owning_server(self):
        r = mcp_tools.ToolRegistry()
        name = r.add("files", "read", "d", {})
        assert r.resolve(name) == ("files", "read")

    def test_unknown_name_resolves_to_none(self):
        assert mcp_tools.ToolRegistry().resolve("nope") is None

    def test_sanitization_collision_does_not_shadow_a_tool(self):
        # "a.b" and "a-b" both sanitize toward the same name; the second tool
        # must stay reachable rather than silently replacing the first.
        r = mcp_tools.ToolRegistry()
        first = r.add("a.b", "run", "d", {})
        second = r.add("a b", "run", "d", {})
        assert first != second
        assert r.resolve(first) == ("a.b", "run")
        assert r.resolve(second) == ("a b", "run")

    def test_original_names_survive_the_round_trip(self):
        # The dispatch must use the server's own name, not the sanitized one.
        r = mcp_tools.ToolRegistry()
        name = r.add("srv.one", "do.thing", "d", {})
        assert r.resolve(name) == ("srv.one", "do.thing")

    def test_schema_carries_name_description_and_input_schema(self):
        r = mcp_tools.ToolRegistry()
        r.add("s", "t", "Reads a file", {"type": "object", "properties": {"p": {}}})
        schema = r.schemas[0]
        assert schema["name"] == "s__t"
        assert schema["description"] == "Reads a file"
        assert schema["input_schema"]["properties"] == {"p": {}}

    def test_missing_input_schema_becomes_a_valid_empty_object(self):
        # The API rejects a tool with no object schema — a server that omits
        # one should still be usable rather than dropped.
        r = mcp_tools.ToolRegistry()
        r.add("s", "t", "d", {})
        assert r.schemas[0]["input_schema"] == {"type": "object", "properties": {}}

    def test_missing_description_gets_a_generated_one(self):
        r = mcp_tools.ToolRegistry()
        r.add("srv", "tool", "", {})
        assert "srv" in r.schemas[0]["description"]

    def test_build_registry_from_mcptools(self):
        reg = mcp_tools.build_registry([
            MCPTool("read", "Read it", "files", {"type": "object"}),
            MCPTool("write", "Write it", "files", {"type": "object"}),
        ])
        assert len(reg) == 2
        assert reg.resolve("files__read") == ("files", "read")


# ══════════════════════════════════════════════════════════════════════════════
# 2. Rendering MCP results into tool_result content
# ══════════════════════════════════════════════════════════════════════════════


class TestRenderResult:
    def test_text_blocks_are_joined(self):
        text, err = mcp_tools.render_result(
            {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]})
        assert text == "a\nb"
        assert err is False

    def test_transport_error_is_flagged(self):
        text, err = mcp_tools.render_result({"error": "server not connected"})
        assert err is True
        assert "not connected" in text

    def test_server_reported_error_is_flagged(self):
        text, err = mcp_tools.render_result(
            {"isError": True, "content": [{"type": "text", "text": "path required"}]})
        assert err is True
        assert text == "path required"

    def test_non_text_block_is_described_not_dropped(self):
        # Returning "" would read as "the tool succeeded and said nothing".
        text, _ = mcp_tools.render_result({"content": [{"type": "image", "data": "..."}]})
        assert "image" in text

    def test_resource_block_prefers_its_text(self):
        text, _ = mcp_tools.render_result({
            "content": [{"type": "resource",
                         "resource": {"uri": "file:///x", "text": "contents"}}]})
        assert text == "contents"

    def test_resource_block_without_text_names_the_uri(self):
        text, _ = mcp_tools.render_result({
            "content": [{"type": "resource", "resource": {"uri": "file:///x"}}]})
        assert "file:///x" in text

    def test_string_content_passes_through(self):
        assert mcp_tools.render_result({"content": "plain"})[0] == "plain"

    def test_payload_without_content_is_serialized(self):
        text, err = mcp_tools.render_result({"rows": 3})
        assert json.loads(text) == {"rows": 3}
        assert err is False

    def test_empty_content_says_so_explicitly(self):
        text, _ = mcp_tools.render_result({"content": []})
        assert "no content" in text

    def test_non_dict_result_is_stringified(self):
        assert mcp_tools.render_result("bare")[0] == "bare"

    def test_oversized_result_is_truncated(self, monkeypatch):
        monkeypatch.setattr(mcp_tools, "MCP_MAX_RESULT_CHARS", 50)
        text, _ = mcp_tools.render_result(text_result("x" * 500))
        assert len(text) < 200
        assert "truncated" in text

    def test_result_at_the_limit_is_left_alone(self, monkeypatch):
        monkeypatch.setattr(mcp_tools, "MCP_MAX_RESULT_CHARS", 50)
        text, _ = mcp_tools.render_result(text_result("x" * 50))
        assert text == "x" * 50


# ══════════════════════════════════════════════════════════════════════════════
# 3. Dispatch
# ══════════════════════════════════════════════════════════════════════════════


class TestDispatch:
    @pytest.mark.asyncio
    async def test_calls_the_owning_server_with_the_servers_own_tool_name(self):
        mgr = FakeManager(results={("srv.one", "do.thing"): text_result("hi")})
        reg = mcp_tools.ToolRegistry()
        name = reg.add("srv.one", "do.thing", "d", {})
        text, err = await mcp_tools.dispatch(mgr, reg, name, {"a": 1})
        assert mgr.calls == [("srv.one", "do.thing", {"a": 1})]
        assert (text, err) == ("hi", False)

    @pytest.mark.asyncio
    async def test_hallucinated_tool_name_returns_an_error_not_a_crash(self):
        mgr = FakeManager()
        text, err = await mcp_tools.dispatch(mgr, mcp_tools.ToolRegistry(), "ghost", {})
        assert err is True
        assert "No such tool" in text
        assert mgr.calls == []

    @pytest.mark.asyncio
    async def test_none_arguments_become_an_empty_dict(self):
        mgr = FakeManager()
        reg = mcp_tools.ToolRegistry()
        name = reg.add("s", "t", "d", {})
        await mcp_tools.dispatch(mgr, reg, name, None)
        assert mgr.calls[0][2] == {}


# ══════════════════════════════════════════════════════════════════════════════
# 4. The tool-use loop
# ══════════════════════════════════════════════════════════════════════════════


def tool_use_response(*calls, text=""):
    content = [Block("text", text=text)] if text else []
    content += [
        Block("tool_use", id=f"toolu_{i}", name=name, input=inp)
        for i, (name, inp) in enumerate(calls)
    ]
    return Response(content, stop_reason="tool_use")


class TestToolLoop:
    @pytest.fixture
    def mgr(self):
        return FakeManager(
            tools=[MCPTool("read", "Read", "files", {"type": "object"})],
            results={("files", "read"): text_result("file contents")},
        )

    @pytest.mark.asyncio
    async def test_no_tool_call_returns_the_text_directly(self, mgr):
        client = FakeClient([Response([Block("text", text="just an answer")])])
        text, stats = await mcp_tools.run_tool_loop(
            client, model="m", system="s", max_tokens=100,
            messages=[{"role": "user", "content": "hi"}], manager=mgr)
        assert text == "just an answer"
        assert stats["tools_used"] == []
        assert mgr.calls == []

    @pytest.mark.asyncio
    async def test_tools_are_offered_to_the_model(self, mgr):
        client = FakeClient([Response([Block("text", text="hi")])])
        await mcp_tools.run_tool_loop(
            client, model="m", system="s", max_tokens=100,
            messages=[{"role": "user", "content": "hi"}], manager=mgr)
        assert client.messages.requests[0]["tools"][0]["name"] == "files__read"

    @pytest.mark.asyncio
    async def test_a_tool_call_runs_and_the_result_goes_back(self, mgr):
        client = FakeClient([
            tool_use_response(("files__read", {"path": "/x"})),
            Response([Block("text", text="the file says hello")]),
        ])
        text, stats = await mcp_tools.run_tool_loop(
            client, model="m", system="s", max_tokens=100,
            messages=[{"role": "user", "content": "read /x"}], manager=mgr)

        assert mgr.calls == [("files", "read", {"path": "/x"})]
        assert text == "the file says hello"
        assert stats["tools_used"] == ["files__read"]

        # Second request carries assistant tool_use then the user tool_result.
        convo = client.messages.requests[1]["messages"]
        assert convo[1]["role"] == "assistant"
        result_msg = convo[2]
        assert result_msg["role"] == "user"
        block = result_msg["content"][0]
        assert block["type"] == "tool_result"
        assert block["tool_use_id"] == "toolu_0"
        assert block["content"] == "file contents"
        assert block["is_error"] is False

    @pytest.mark.asyncio
    async def test_parallel_calls_return_in_one_user_message(self, mgr):
        # Splitting tool_results across messages teaches the model to stop
        # asking for parallel calls — they must all ride in a single message.
        client = FakeClient([
            tool_use_response(("files__read", {"path": "/a"}), ("files__read", {"path": "/b"})),
            Response([Block("text", text="both read")]),
        ])
        await mcp_tools.run_tool_loop(
            client, model="m", system="s", max_tokens=100,
            messages=[{"role": "user", "content": "read both"}], manager=mgr)

        convo = client.messages.requests[1]["messages"]
        user_msgs = [m for m in convo if m["role"] == "user"]
        result_blocks = [
            b for m in user_msgs if isinstance(m["content"], list)
            for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        assert len(result_blocks) == 2
        # ...and in exactly one message.
        msgs_with_results = [
            m for m in user_msgs if isinstance(m["content"], list)
            and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"])
        ]
        assert len(msgs_with_results) == 1

    @pytest.mark.asyncio
    async def test_parallel_calls_actually_overlap(self, mgr):
        client = FakeClient([
            tool_use_response(("files__read", {"path": "/a"}), ("files__read", {"path": "/b"})),
            Response([Block("text", text="ok")]),
        ])
        await mcp_tools.run_tool_loop(
            client, model="m", system="s", max_tokens=100,
            messages=[{"role": "user", "content": "x"}], manager=mgr)
        assert mgr.max_concurrent == 2

    @pytest.mark.asyncio
    async def test_tool_ids_are_matched_per_call(self, mgr):
        client = FakeClient([
            tool_use_response(("files__read", {"p": 1}), ("files__read", {"p": 2})),
            Response([Block("text", text="ok")]),
        ])
        await mcp_tools.run_tool_loop(
            client, model="m", system="s", max_tokens=100,
            messages=[{"role": "user", "content": "x"}], manager=mgr)
        results = client.messages.requests[1]["messages"][2]["content"]
        assert sorted(b["tool_use_id"] for b in results) == ["toolu_0", "toolu_1"]

    @pytest.mark.asyncio
    async def test_a_failing_tool_is_reported_as_is_error(self):
        mgr = FakeManager(
            tools=[MCPTool("read", "Read", "files", {})],
            results={("files", "read"): {"error": "boom"}},
        )
        client = FakeClient([
            tool_use_response(("files__read", {})),
            Response([Block("text", text="that failed")]),
        ])
        await mcp_tools.run_tool_loop(
            client, model="m", system="s", max_tokens=100,
            messages=[{"role": "user", "content": "x"}], manager=mgr)
        block = client.messages.requests[1]["messages"][2]["content"][0]
        assert block["is_error"] is True
        assert "boom" in block["content"]

    @pytest.mark.asyncio
    async def test_multiple_rounds_of_tool_use(self, mgr):
        client = FakeClient([
            tool_use_response(("files__read", {"path": "/a"})),
            tool_use_response(("files__read", {"path": "/b"})),
            Response([Block("text", text="finished")]),
        ])
        text, stats = await mcp_tools.run_tool_loop(
            client, model="m", system="s", max_tokens=100,
            messages=[{"role": "user", "content": "x"}], manager=mgr)
        assert text == "finished"
        assert len(mgr.calls) == 2
        assert stats["tools_used"] == ["files__read", "files__read"]

    @pytest.mark.asyncio
    async def test_turn_limit_stops_the_loop_and_says_so(self, mgr):
        # A server that keeps prompting more tool use must not spin forever.
        client = FakeClient([tool_use_response(("files__read", {})) for _ in range(20)])
        text, stats = await mcp_tools.run_tool_loop(
            client, model="m", system="s", max_tokens=100,
            messages=[{"role": "user", "content": "x"}], manager=mgr, max_turns=3)
        assert len(client.messages.requests) == 3
        assert stats["hit_turn_limit"] is True
        assert "Stopped after 3 tool turns" in text

    @pytest.mark.asyncio
    async def test_usage_is_summed_across_turns(self, mgr):
        client = FakeClient([
            Response(
                [Block("tool_use", id="t0", name="files__read", input={})],
                stop_reason="tool_use", usage=Usage(100, 20),
            ),
            Response([Block("text", text="done")], usage=Usage(300, 40)),
        ])
        _, stats = await mcp_tools.run_tool_loop(
            client, model="m", system="s", max_tokens=100,
            messages=[{"role": "user", "content": "x"}], manager=mgr)
        assert stats["input_tokens"] == 400
        assert stats["output_tokens"] == 60

    @pytest.mark.asyncio
    async def test_caller_history_is_not_mutated(self, mgr):
        history = [{"role": "user", "content": "x"}]
        client = FakeClient([
            tool_use_response(("files__read", {})),
            Response([Block("text", text="done")]),
        ])
        await mcp_tools.run_tool_loop(
            client, model="m", system="s", max_tokens=100,
            messages=history, manager=mgr)
        assert history == [{"role": "user", "content": "x"}]

    @pytest.mark.asyncio
    async def test_narration_before_a_tool_call_survives_an_empty_final_turn(self, mgr):
        client = FakeClient([
            tool_use_response(("files__read", {}), text="Let me look."),
            Response([]),  # final turn with no text block
        ])
        text, _ = await mcp_tools.run_tool_loop(
            client, model="m", system="s", max_tokens=100,
            messages=[{"role": "user", "content": "x"}], manager=mgr)
        assert text == "Let me look."

    @pytest.mark.asyncio
    async def test_completely_empty_response_has_a_placeholder(self, mgr):
        client = FakeClient([Response([])])
        text, _ = await mcp_tools.run_tool_loop(
            client, model="m", system="s", max_tokens=100,
            messages=[{"role": "user", "content": "x"}], manager=mgr)
        assert text == "(no response)"

    @pytest.mark.asyncio
    async def test_on_tool_callback_is_notified(self, mgr):
        seen = []

        async def on_tool(name, detail):
            seen.append((name, detail))

        client = FakeClient([
            tool_use_response(("files__read", {"path": "/etc/hosts"})),
            Response([Block("text", text="ok")]),
        ])
        await mcp_tools.run_tool_loop(
            client, model="m", system="s", max_tokens=100,
            messages=[{"role": "user", "content": "x"}], manager=mgr, on_tool=on_tool)
        assert seen == [("files__read", "/etc/hosts")]

    @pytest.mark.asyncio
    async def test_a_raising_callback_does_not_break_the_turn(self, mgr):
        async def on_tool(name, detail):
            raise RuntimeError("callback is the caller's problem")

        client = FakeClient([
            tool_use_response(("files__read", {})),
            Response([Block("text", text="still fine")]),
        ])
        text, _ = await mcp_tools.run_tool_loop(
            client, model="m", system="s", max_tokens=100,
            messages=[{"role": "user", "content": "x"}], manager=mgr, on_tool=on_tool)
        assert text == "still fine"


class TestInputSummary:
    def test_prefers_a_recognizable_field(self):
        assert mcp_tools._summarize_input({"path": "/tmp/x", "mode": "r"}) == "/tmp/x"

    def test_falls_back_to_key_names(self):
        assert "alpha" in mcp_tools._summarize_input({"alpha": 1, "beta": 2})

    def test_empty_input_summarizes_to_nothing(self):
        assert mcp_tools._summarize_input({}) == ""
        assert mcp_tools._summarize_input(None) == ""

    def test_long_values_are_clipped(self):
        assert len(mcp_tools._summarize_input({"query": "q" * 500})) <= 80
