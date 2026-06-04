"""End-to-end Anthropic API tests using recorded HTTP cassettes (vcrpy).

These tests exercise the real Anthropic SDK + real httpx wire format against
recorded responses. No mocks of `_get_api_client` or `messages.create` — we
patch HTTP at the socket layer with vcrpy.

WORKFLOW
========

1. Record (one-time, requires a real ANTHROPIC_API_KEY):

       ANTHROPIC_API_KEY=sk-ant-... \
       pytest tests/test_anthropic_e2e_cassettes.py \
              --record-mode=once -v

   Cassettes land in tests/cassettes/test_anthropic_e2e_cassettes/.

2. Replay (CI, no key required — that's the whole point):

       pytest tests/test_anthropic_e2e_cassettes.py

   The conftest below injects a fake key when ANTHROPIC_API_KEY isn't set,
   so replay-only contributors don't need an Anthropic account.

3. Re-record if the wire format changes:

       rm tests/cassettes/test_anthropic_e2e_cassettes/<name>.yaml
       ANTHROPIC_API_KEY=sk-... pytest ... --record-mode=once

SECURITY
========

before_record_response in this file strips the ``x-api-key``,
``anthropic-organization-id``, request-id, set-cookie, and any other
identifying headers before the cassette is written. The fixture also rejects
recording any URL that isn't ``api.anthropic.com``. If you ever see a real
key string in a committed cassette, treat it as compromised and rotate it.
"""
from __future__ import annotations

import os

import pytest

# Skip the whole module gracefully if the optional deps aren't installed.
pytest.importorskip("vcr")
pytest.importorskip("anthropic")

# vcrpy 8.x's aiohttp stub subclasses ``aiohttp.streams.AsyncStreamReaderMixin``,
# which aiohttp 3.14 removed. These tests drive the Anthropic SDK over httpx, so
# vcr's aiohttp patcher is never exercised — but it is imported when vcr patches,
# and the missing attribute makes that import explode. A no-op shim lets the
# unused aiohttp stub import cleanly without pinning either dependency.
try:
    import aiohttp.streams as _aiohttp_streams

    if not hasattr(_aiohttp_streams, "AsyncStreamReaderMixin"):
        class AsyncStreamReaderMixin:  # pragma: no cover - compatibility shim
            pass

        _aiohttp_streams.AsyncStreamReaderMixin = AsyncStreamReaderMixin
except ImportError:  # pragma: no cover - aiohttp not installed → vcr skips it
    pass


# ─── VCR configuration shared by every test in this file ───────────────────


_SENSITIVE_REQUEST_HEADERS = {
    "x-api-key",
    "authorization",
    "anthropic-organization-id",
    "x-stainless-os",
    "x-stainless-lang",
    "x-stainless-package-version",
    "x-stainless-runtime",
    "x-stainless-runtime-version",
    "x-stainless-arch",
    "user-agent",
    "cookie",
}

_SENSITIVE_RESPONSE_HEADERS = {
    "set-cookie",
    "request-id",
    "anthropic-organization-id",
    "cf-ray",
    "x-cloud-trace-context",
    "traceresponse",
    "via",
    "server",
}


def _scrub_request(request):
    """vcrpy before_record_request hook — refuse unknown hosts, drop secrets."""
    if "api.anthropic.com" not in (request.host or ""):
        # Don't even record other hosts. This is paranoia: if a test
        # accidentally hits a different service we don't want its traffic
        # bottled up and replayed.
        return None
    return request


def _scrub_response(response):
    """vcrpy before_record_response hook — strip identifying headers."""
    headers = response.get("headers") or {}
    for h in list(headers):
        if h.lower() in _SENSITIVE_RESPONSE_HEADERS:
            headers[h] = ["[REDACTED]"]
    return response


@pytest.fixture(scope="module")
def vcr_config():
    """pytest-recording reads this fixture to configure vcr.VCR."""
    return {
        "filter_headers": [(h, "[REDACTED]") for h in _SENSITIVE_REQUEST_HEADERS],
        # The Anthropic SDK uses httpx; vcrpy 6+ ships an httpx stub.
        "decode_compressed_response": True,
        "before_record_request": _scrub_request,
        "before_record_response": _scrub_response,
        # Match on method, URL, and body — body matching catches accidental
        # prompt changes that would otherwise silently reuse a stale cassette.
        "match_on": ["method", "scheme", "host", "path", "query", "body"],
    }


@pytest.fixture(autouse=True)
def _skip_if_no_cassette_and_no_key(request, monkeypatch):
    """Skip cassette tests when neither a cassette nor a real API key is available.

    pytest-recording's default behaviour when a cassette is missing is to let
    the request fall through to real HTTP. We override that here: if there's
    no recorded cassette AND no real ANTHROPIC_API_KEY in the environment,
    skip the test with a clear message. That keeps CI deterministic —
    replay-only when keys aren't configured, real recording when they are.

    "Real" key means one that looks like an Anthropic key. Other test files
    (notably test_init_e2e.py) inject fake values like ``"test-api-key"`` at
    import time, and we don't want those to fool us into attempting a real
    HTTP call (which would then hang on DNS or fail with 401).
    """
    cassette_dir = os.path.join(
        os.path.dirname(__file__), "cassettes", "test_anthropic_e2e_cassettes",
    )
    cassette_path = os.path.join(cassette_dir, f"{request.node.name}.yaml")
    has_cassette = os.path.exists(cassette_path)
    key = os.getenv("ANTHROPIC_API_KEY", "")
    has_real_key = key.startswith("sk-ant-") and len(key) > 20

    if not has_cassette and not has_real_key:
        pytest.skip(
            f"No cassette at {cassette_path} and no real ANTHROPIC_API_KEY set. "
            "Run with --record-mode=once and a real key to create it; "
            "see tests/README_E2E.md."
        )


@pytest.fixture(autouse=True)
def _fake_key_when_replaying(monkeypatch):
    """Inject a dummy ANTHROPIC_API_KEY when none is set.

    During record mode the real key from the environment is used (vcrpy
    redacts it before writing). During replay, the SDK still requires *some*
    string to construct the client even though no real HTTP happens.
    """
    if not os.getenv("ANTHROPIC_API_KEY"):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fake-key-for-replay")
    # Force-reimport claude_core so its module-level CLAUDE_API_KEY constant
    # picks up the env we just set. Done by clearing the lazy client globals.
    from telechat_pkg import claude_core
    claude_core._api_client = None
    claude_core._async_api_client = None
    claude_core.CLAUDE_API_KEY = os.environ["ANTHROPIC_API_KEY"]


# ─── Tests ─────────────────────────────────────────────────────────────────


@pytest.mark.vcr
def test_ask_claude_api_basic_round_trip():
    """End-to-end: real Anthropic SDK → httpx → recorded HTTP → parsed reply."""
    from telechat_pkg.claude_core import ask_claude_api

    text, stats = ask_claude_api(
        "Reply with the single word HELLO and nothing else.",
        history=[],
        model="claude-haiku-4-5-20251001",
        system="You are a terse test bot. Reply with exactly what the user asks for.",
        max_tokens=20,
    )

    assert isinstance(text, str) and text.strip(), "Expected non-empty reply"
    assert "HELLO" in text.upper(), f"Expected reply to contain HELLO, got {text!r}"
    # Stats shape is part of the public contract — assert it survives the round-trip.
    assert "input_tokens" in stats and stats["input_tokens"] > 0
    assert "output_tokens" in stats and stats["output_tokens"] > 0
    assert stats["tools_used"] == []


@pytest.mark.vcr
def test_ask_claude_api_respects_history():
    """The wire body must include prior turns from `history`."""
    from telechat_pkg.claude_core import ask_claude_api

    history = [
        {"role": "user", "content": "My name is Ada."},
        {"role": "assistant", "content": "Nice to meet you, Ada."},
    ]
    text, _ = ask_claude_api(
        "What is my name? Answer with just the name.",
        history=history,
        model="claude-haiku-4-5-20251001",
        system="Answer concisely.",
        max_tokens=20,
    )
    assert "Ada" in text, f"Expected the model to recall 'Ada', got {text!r}"


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_ask_claude_api_async_streams_text():
    """The async streaming path yields chunks and aggregates a final reply."""
    from telechat_pkg.claude_core import ask_claude_api_async

    chunks: list[str] = []

    async def on_text(chunk: str) -> None:
        chunks.append(chunk)

    text, stats = await ask_claude_api_async(
        "Count from 1 to 3 separated by commas.",
        history=[],
        model="claude-haiku-4-5-20251001",
        system="Be terse.",
        max_tokens=30,
        on_text=on_text,
    )
    assert text and text.strip()
    # At least one streamed chunk must have been delivered to on_text.
    assert chunks, "on_text callback was never invoked during streaming"
    # Final aggregated text equals the concatenation of streamed chunks.
    assert "".join(chunks).strip() == text.strip()
    assert stats.get("input_tokens", 0) > 0
    assert stats.get("output_tokens", 0) > 0
