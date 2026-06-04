"""
Behavior tests for video generation (telechat_pkg.video_gen).

All network I/O is mocked: a fake ``aiohttp`` module is injected into
``sys.modules`` so the Replicate REST flow (create → poll → download) runs
entirely in-process. No real API calls are made.

Run:
    pytest tests/test_video_gen.py -v
"""

import os
import sys
import types

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")

from telechat_pkg import video_gen as vg


# ── Fake aiohttp scaffolding ─────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, *, status=200, json_data=None, text_data="", read_data=b""):
        self.status = status
        self._json = json_data or {}
        self._text = text_data
        self._read = read_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._json

    async def text(self):
        return self._text

    async def read(self):
        return self._read


class _FakeSession:
    """Returns queued responses for post() then get() calls in order."""

    def __init__(self, post_responses, get_responses):
        self._posts = list(post_responses)
        self._gets = list(get_responses)
        self.post_calls = []
        self.get_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self._posts.pop(0)

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self._gets.pop(0)


def _install_fake_aiohttp(monkeypatch, session):
    fake = types.ModuleType("aiohttp")

    def ClientSession(*a, **k):
        return session

    class ClientTimeout:
        def __init__(self, total=None):
            self.total = total

    fake.ClientSession = ClientSession
    fake.ClientTimeout = ClientTimeout
    monkeypatch.setitem(sys.modules, "aiohttp", fake)
    # No real sleeping during polling.
    monkeypatch.setattr(vg.asyncio, "sleep", _noop_sleep)


async def _noop_sleep(*a, **k):
    return None


# ══════════════════════════════════════════════════════════════════════════════
# 1. Availability / config
# ══════════════════════════════════════════════════════════════════════════════


class TestAvailability:
    def test_is_available_true(self, monkeypatch):
        monkeypatch.setattr(vg, "VIDEO_GEN_ENABLED", True)
        monkeypatch.setattr(vg, "REPLICATE_API_TOKEN", "tok")
        assert vg.is_available() is True

    def test_is_available_false_when_disabled(self, monkeypatch):
        monkeypatch.setattr(vg, "VIDEO_GEN_ENABLED", False)
        monkeypatch.setattr(vg, "REPLICATE_API_TOKEN", "tok")
        assert vg.is_available() is False

    def test_is_available_false_without_token(self, monkeypatch):
        monkeypatch.setattr(vg, "VIDEO_GEN_ENABLED", True)
        monkeypatch.setattr(vg, "REPLICATE_API_TOKEN", "")
        assert vg.is_available() is False


# ══════════════════════════════════════════════════════════════════════════════
# 2. Guard clauses
# ══════════════════════════════════════════════════════════════════════════════


class TestGuards:
    @pytest.mark.asyncio
    async def test_no_token_returns_error(self, monkeypatch):
        monkeypatch.setattr(vg, "REPLICATE_API_TOKEN", "")
        result = await vg.generate("a cat")
        assert result.error == "REPLICATE_API_TOKEN not set"
        assert result.video_path == ""

    @pytest.mark.asyncio
    async def test_missing_aiohttp_returns_error(self, monkeypatch):
        monkeypatch.setattr(vg, "REPLICATE_API_TOKEN", "tok")
        # Force the local `import aiohttp` to fail.
        monkeypatch.setitem(sys.modules, "aiohttp", None)
        result = await vg.generate("a cat")
        assert result.error == "aiohttp not installed"


# ══════════════════════════════════════════════════════════════════════════════
# 3. Happy path
# ══════════════════════════════════════════════════════════════════════════════


class TestSuccess:
    @pytest.mark.asyncio
    async def test_immediate_success_string_output(self, monkeypatch):
        monkeypatch.setattr(vg, "REPLICATE_API_TOKEN", "tok")
        create = _FakeResp(status=201, json_data={
            "status": "succeeded",
            "output": "https://cdn/video.mp4",
        })
        download = _FakeResp(status=200, read_data=b"VIDEODATA")
        session = _FakeSession([create], [download])
        _install_fake_aiohttp(monkeypatch, session)

        result = await vg.generate("a dancing robot")
        assert result.error is None
        assert result.video_url == "https://cdn/video.mp4"
        assert os.path.exists(result.video_path)
        with open(result.video_path, "rb") as f:
            assert f.read() == b"VIDEODATA"
        os.unlink(result.video_path)

    @pytest.mark.asyncio
    async def test_polling_then_success_list_output(self, monkeypatch):
        monkeypatch.setattr(vg, "REPLICATE_API_TOKEN", "tok")
        create = _FakeResp(status=200, json_data={
            "status": "processing",
            "urls": {"get": "https://api/poll"},
        })
        poll1 = _FakeResp(status=200, json_data={"status": "processing"})
        poll2 = _FakeResp(status=200, json_data={
            "status": "succeeded",
            "output": ["https://cdn/out.mp4"],
        })
        download = _FakeResp(status=200, read_data=b"MP4")
        session = _FakeSession([create], [poll1, poll2, download])
        _install_fake_aiohttp(monkeypatch, session)

        result = await vg.generate("waves")
        assert result.error is None
        assert result.video_url == "https://cdn/out.mp4"
        os.unlink(result.video_path)


# ══════════════════════════════════════════════════════════════════════════════
# 4. Error paths
# ══════════════════════════════════════════════════════════════════════════════


class TestErrors:
    @pytest.mark.asyncio
    async def test_api_error_status(self, monkeypatch):
        monkeypatch.setattr(vg, "REPLICATE_API_TOKEN", "tok")
        create = _FakeResp(status=500, text_data="server boom")
        session = _FakeSession([create], [])
        _install_fake_aiohttp(monkeypatch, session)
        result = await vg.generate("x")
        assert "Replicate API error 500" in result.error

    @pytest.mark.asyncio
    async def test_generation_failed_status(self, monkeypatch):
        monkeypatch.setattr(vg, "REPLICATE_API_TOKEN", "tok")
        create = _FakeResp(status=201, json_data={"status": "failed", "error": "bad prompt"})
        session = _FakeSession([create], [])
        _install_fake_aiohttp(monkeypatch, session)
        result = await vg.generate("x")
        assert result.error == "bad prompt"

    @pytest.mark.asyncio
    async def test_timeout_status_after_polls(self, monkeypatch):
        monkeypatch.setattr(vg, "REPLICATE_API_TOKEN", "tok")
        create = _FakeResp(status=201, json_data={
            "status": "processing",
            "urls": {"get": "https://api/poll"},
        })
        # 90 poll responses all "processing" → loop hits poll_count limit
        polls = [_FakeResp(status=200, json_data={"status": "processing"}) for _ in range(95)]
        session = _FakeSession([create], polls)
        _install_fake_aiohttp(monkeypatch, session)
        result = await vg.generate("x")
        assert "timed out" in result.error

    @pytest.mark.asyncio
    async def test_no_url_in_output(self, monkeypatch):
        monkeypatch.setattr(vg, "REPLICATE_API_TOKEN", "tok")
        create = _FakeResp(status=201, json_data={"status": "succeeded", "output": None})
        session = _FakeSession([create], [])
        _install_fake_aiohttp(monkeypatch, session)
        result = await vg.generate("x")
        assert result.error == "No video URL in output"

    @pytest.mark.asyncio
    async def test_download_failure(self, monkeypatch):
        monkeypatch.setattr(vg, "REPLICATE_API_TOKEN", "tok")
        create = _FakeResp(status=201, json_data={
            "status": "succeeded", "output": "https://cdn/v.mp4",
        })
        download = _FakeResp(status=404)
        session = _FakeSession([create], [download])
        _install_fake_aiohttp(monkeypatch, session)
        result = await vg.generate("x")
        assert "Download failed: HTTP 404" in result.error
        assert result.video_url == "https://cdn/v.mp4"

    @pytest.mark.asyncio
    async def test_unexpected_exception_caught(self, monkeypatch):
        monkeypatch.setattr(vg, "REPLICATE_API_TOKEN", "tok")

        class _BoomSession:
            async def __aenter__(self):
                raise RuntimeError("network exploded")

            async def __aexit__(self, *exc):
                return False

        _install_fake_aiohttp(monkeypatch, _BoomSession())
        result = await vg.generate("x")
        assert "network exploded" in result.error

    @pytest.mark.asyncio
    async def test_timeout_error_caught(self, monkeypatch):
        monkeypatch.setattr(vg, "REPLICATE_API_TOKEN", "tok")
        import asyncio

        class _TimeoutSession:
            async def __aenter__(self):
                raise asyncio.TimeoutError()

            async def __aexit__(self, *exc):
                return False

        _install_fake_aiohttp(monkeypatch, _TimeoutSession())
        result = await vg.generate("x")
        assert result.error == "Video generation timed out"
