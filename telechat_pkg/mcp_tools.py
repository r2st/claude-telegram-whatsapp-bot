"""
Make MCP tools callable during a conversation.

`mcp_client` can connect to MCP servers and list their tools, but nothing ever
handed those tools to Claude — the only bridge was `get_tools_for_prompt`,
which pastes the tool names into the system prompt as prose. A model told
"you have a tool called filesystem.read_file" and given no way to call it can
only describe the tool it cannot use.

This module closes that gap: it converts discovered MCP tools into Anthropic
tool definitions, runs the tool-use loop against the Messages API, dispatches
each `tool_use` block to the owning MCP server, and feeds the results back.

    from telechat_pkg import mcp_tools
    text, stats = await mcp_tools.run_tool_loop(
        client, model=..., system=..., messages=[...], manager=mgr)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger(__name__)

#: How many model turns a single question may take before we stop. Each turn is
#: one API call, so an unbounded loop is an unbounded bill; a server that keeps
#: returning "try again" would otherwise spin until the user gives up.
MCP_MAX_TOOL_TURNS = int(os.getenv("MCP_MAX_TOOL_TURNS", "8"))

#: Ceiling on the characters of any one tool result fed back to the model. A
#: filesystem tool asked for a large file will happily return all of it, and
#: the context window is the bot's, not the server's, to spend.
MCP_MAX_RESULT_CHARS = int(os.getenv("MCP_MAX_RESULT_CHARS", "20000"))

#: The Anthropic API accepts tool names matching ^[a-zA-Z0-9_-]{1,128}$.
_NAME_OK = re.compile(r"[^a-zA-Z0-9_-]")
_MAX_NAME_LEN = 128


def _sanitize(part: str) -> str:
    """Replace every character the API's tool-name charset rejects."""
    return _NAME_OK.sub("_", part)


@dataclass
class ToolRegistry:
    """Maps the names Claude sees back to the MCP server that owns them.

    The exposed name is ``server__tool``, but the mapping is stored rather than
    re-parsed on the way back: a server or tool whose own name contains ``__``
    (or any character that had to be sanitized) makes splitting on a separator
    ambiguous, and dispatching a tool call to the wrong server is worse than
    refusing it. Lookup is exact or it fails.
    """

    #: exposed name -> (server name, tool name as the server knows it)
    routes: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: Tool definitions in Anthropic wire format, in registration order.
    schemas: list[dict] = field(default_factory=list)

    def add(self, server: str, tool_name: str, description: str, input_schema: dict) -> str:
        """Register one tool and return the name Claude will call it by."""
        base = f"{_sanitize(server)}__{_sanitize(tool_name)}"[:_MAX_NAME_LEN]
        name = base
        # Sanitizing can collide two distinct tools onto one name (``a.b`` and
        # ``a-b`` both become ``a_b``). Suffix until unique so the second tool
        # stays reachable instead of silently shadowing the first.
        suffix = 2
        while name in self.routes:
            tail = f"_{suffix}"
            name = base[: _MAX_NAME_LEN - len(tail)] + tail
            suffix += 1
        self.routes[name] = (server, tool_name)
        self.schemas.append({
            "name": name,
            "description": description or f"{tool_name} (via MCP server {server})",
            # The API requires an object schema. A server that advertises no
            # schema still gets a valid empty one rather than being dropped.
            "input_schema": input_schema or {"type": "object", "properties": {}},
        })
        return name

    def resolve(self, name: str) -> Optional[tuple[str, str]]:
        return self.routes.get(name)

    def __len__(self) -> int:
        return len(self.routes)


def build_registry(tools) -> ToolRegistry:
    """Convert discovered :class:`~telechat_pkg.mcp_client.MCPTool` objects."""
    registry = ToolRegistry()
    for t in tools:
        registry.add(t.server, t.name, t.description, t.input_schema)
    return registry


def render_result(result: Any) -> tuple[str, bool]:
    """Flatten an MCP tool result into ``(text, is_error)`` for a tool_result.

    MCP returns content as a list of typed blocks; the Messages API wants text
    it can put in front of the model. Non-text blocks (images, embedded
    resources) are described rather than dropped, so the model knows something
    came back and can ask differently instead of assuming the tool was empty.
    """
    if not isinstance(result, dict):
        return _truncate(str(result)), False

    # Our own transport-level failures arrive as {"error": ...}; a server's
    # own failure arrives as isError with the message in content. Both are
    # errors as far as the model is concerned.
    if "error" in result:
        return _truncate(str(result["error"])), True

    is_error = bool(result.get("isError"))
    content = result.get("content")

    if content is None:
        # No content key at all — hand back the raw payload rather than an
        # empty string, which would read as "the tool returned nothing".
        return _truncate(json.dumps(result, default=str)), is_error

    if isinstance(content, str):
        return _truncate(content), is_error

    parts: list[str] = []
    for block in content if isinstance(content, list) else [content]:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            btype = block.get("type")
            if btype == "text":
                parts.append(str(block.get("text", "")))
            elif btype == "resource":
                res = block.get("resource", {})
                text = res.get("text") if isinstance(res, dict) else None
                uri = res.get("uri", "?") if isinstance(res, dict) else "?"
                parts.append(str(text) if text else f"[resource: {uri}]")
            else:
                parts.append(f"[{btype or 'unknown'} content]")
        else:
            parts.append(str(block))

    text = "\n".join(p for p in parts if p)
    return _truncate(text or "(the tool returned no content)"), is_error


def _truncate(text: str) -> str:
    if len(text) <= MCP_MAX_RESULT_CHARS:
        return text
    dropped = len(text) - MCP_MAX_RESULT_CHARS
    return text[:MCP_MAX_RESULT_CHARS] + f"\n… [truncated {dropped} characters]"


async def dispatch(manager, registry: ToolRegistry, name: str, arguments: dict) -> tuple[str, bool]:
    """Route one tool call to its MCP server and render the reply."""
    route = registry.resolve(name)
    if route is None:
        # The model invented a tool name. Say so plainly — it can recover from
        # a clear error, but not from a silent empty result.
        return f"No such tool: {name}", True
    server, tool_name = route
    result = await manager.call_tool(server, tool_name, arguments or {})
    return render_result(result)


def _accumulate(stats: dict, usage) -> None:
    """Sum usage across loop turns — the caller bills for all of them."""
    stats["input_tokens"] = stats.get("input_tokens", 0) + getattr(usage, "input_tokens", 0)
    stats["output_tokens"] = stats.get("output_tokens", 0) + getattr(usage, "output_tokens", 0)


def _text_of(content) -> str:
    return "\n".join(
        b.text for b in content
        if getattr(b, "type", None) == "text" and getattr(b, "text", "")
    )


async def run_tool_loop(
    client,
    *,
    model: str,
    system: str,
    messages: list[dict],
    max_tokens: int,
    manager,
    registry: Optional[ToolRegistry] = None,
    max_turns: int = MCP_MAX_TOOL_TURNS,
    on_tool: Optional[Callable[[str, str], Awaitable[None]]] = None,
) -> tuple[str, dict]:
    """Run the agentic tool-use loop until Claude stops calling tools.

    ``messages`` is not mutated — the loop works on its own copy, so a caller
    that keeps conversation history does not end up with tool plumbing in it.

    Returns ``(text, stats)``. ``stats["tools_used"]`` lists the tools actually
    invoked, in call order, which is what the platform adapters surface to the
    user.
    """
    registry = registry if registry is not None else build_registry(manager.list_tools())
    convo = list(messages)
    stats: dict = {"tools_used": []}
    text = ""

    for _turn in range(max_turns):
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=convo,
            tools=registry.schemas,
        )
        _accumulate(stats, response.usage)

        if response.stop_reason != "tool_use":
            # Keep the earlier narration if this turn adds nothing: after a
            # tool call the model sometimes stops without restating anything,
            # and "(no response)" is a worse answer than what it already said.
            text = _text_of(response.content) or text
            break

        # Any text alongside the tool calls is the model narrating what it is
        # about to do. Keep it: on the final turn it may be all the user gets.
        text = _text_of(response.content) or text
        convo.append({"role": "assistant", "content": response.content})

        calls = [b for b in response.content if getattr(b, "type", None) == "tool_use"]

        async def _run(block):
            if on_tool:
                try:
                    await on_tool(block.name, _summarize_input(block.input))
                except Exception:
                    # The progress callback belongs to the caller; its failure
                    # must not abort a turn that is otherwise working.
                    log.debug("on_tool callback raised", exc_info=True)
            body, is_error = await dispatch(manager, registry, block.name, block.input)
            return {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": body,
                "is_error": is_error,
            }

        # Claude may ask for several tools at once; running them concurrently
        # is the whole point of it doing so. Results must go back in ONE user
        # message — splitting them across messages teaches the model to stop
        # asking for parallel calls.
        results = await asyncio.gather(*(_run(b) for b in calls))
        stats["tools_used"].extend(b.name for b in calls)
        convo.append({"role": "user", "content": list(results)})
    else:
        # Fell out of the loop still wanting tools. Report it rather than
        # returning a half-finished answer as if it were complete.
        log.warning("MCP tool loop hit the %d-turn limit", max_turns)
        text = (text or "").rstrip()
        note = f"[Stopped after {max_turns} tool turns without finishing.]"
        text = f"{text}\n\n{note}" if text else note
        stats["hit_turn_limit"] = True

    return text or "(no response)", stats


def _summarize_input(inp: Any) -> str:
    """A short, loggable description of a tool call's arguments."""
    if not isinstance(inp, dict) or not inp:
        return ""
    for key in ("path", "file_path", "query", "url", "command", "name"):
        if key in inp:
            return str(inp[key])[:80]
    return ", ".join(list(inp)[:3])[:80]
