"""
Behavior tests for music generation (telechat_pkg.music_gen).

Network I/O is mocked via a fake ``aiohttp`` module injected into
``sys.modules``; the Replicate MusicGen flow (create → poll → download) runs
in-process with no real API calls.

Run:
    pytest tests/test_music_gen.py -v
"""

import asyncio
import os
import sys
import types

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")

from telechat_pkg import music_gen as mg


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
    def __init__(self, post_responses, get_responses):
        self._posts = list(post_responses)
        self._gets = list(get_responses)
        self.post_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self._posts.pop(0)

    def get(self, url, **kwargs):
        return self._gets.pop(0)


async def _noop_sleep(*a, **k):
    return None


def _install_fake_aiohttp(monkeypatch, session):
    fake = types.ModuleType("aiohttp")
    fake.ClientSession = lambda *a, **k: session

    class ClientTimeout:
        def __init__(self, total=None):
            self.total = total

    fake.ClientTimeout = ClientTimeout
    monkeypatch.setitem(sys.modules, "aiohttp", fake)
    monkeypatch.setattr(mg.asyncio, "sleep", _noop_sleep)


# ══════════════════════════════════════════════════════════════════════════════
# 1. Availability
# ══════════════════════════════════════════════════════════════════════════════


class TestAvailability:
    def test_available(self, monkeypatch):
        monkeypatch.setattr(mg, "MUSIC_GEN_ENABLED", True)
        monkeypatch.setattr(mg, "REPLICATE_API_TOKEN", "tok")
        assert mg.is_available() is True

    def test_unavailable_disabled(self, monkeypatch):
        monkeypatch.setattr(mg, "MUSIC_GEN_ENABLED", False)
        monkeypatch.setattr(mg, "REPLICATE_API_TOKEN", "tok")
        assert mg.is_available() is False

    def test_unavailable_no_token(self, monkeypatch):
        monkeypatch.setattr(mg, "MUSIC_GEN_ENABLED", True)
        monkeypatch.setattr(mg, "REPLICATE_API_TOKEN", "")
        assert mg.is_available() is False


# ══════════════════════════════════════════════════════════════════════════════
# 2. Guards
# ══════════════════════════════════════════════════════════════════════════════


class TestGuards:
    @pytest.mark.asyncio
    async def test_no_token(self, monkeypatch):
        monkeypatch.setattr(mg, "REPLICATE_API_TOKEN", "")
        result = await mg.generate("lofi")
        assert result.error == "REPLICATE_API_TOKEN not set"

    @pytest.mark.asyncio
    async def test_missing_aiohttp(self, monkeypatch):
        monkeypatch.setattr(mg, "REPLICATE_API_TOKEN", "tok")
        monkeypatch.setitem(sys.modules, "aiohttp", None)
        result = await mg.generate("lofi")
        assert result.error == "aiohttp not installed"


# ══════════════════════════════════════════════════════════════════════════════
# 3. Success
# ══════════════════════════════════════════════════════════════════════════════


class TestSuccess:
    @pytest.mark.asyncio
    async def test_immediate_success(self, monkeypatch):
        monkeypatch.setattr(mg, "REPLICATE_API_TOKEN", "tok")
        create = _FakeResp(status=201, json_data={
            "status": "succeeded", "output": "https://cdn/song.mp3",
        })
        download = _FakeResp(status=200, read_data=b"AUDIO")
        session = _FakeSession([create], [download])
        _install_fake_aiohttp(monkeypatch, session)
        result = await mg.generate("upbeat jazz", duration=15)
        assert result.error is None
        assert result.audio_url == "https://cdn/song.mp3"
        assert result.duration == 15
        assert os.path.exists(result.audio_path)
        os.unlink(result.audio_path)

    @pytest.mark.asyncio
    async def test_duration_clamped_in_payload(self, monkeypatch):
        monkeypatch.setattr(mg, "REPLICATE_API_TOKEN", "tok")
        create = _FakeResp(status=201, json_data={
            "status": "succeeded", "output": ["https://cdn/s.mp3"],
        })
        download = _FakeResp(status=200, read_data=b"A")
        session = _FakeSession([create], [download])
        _install_fake_aiohttp(monkeypatch, session)
        result = await mg.generate("x", duration=999)
        # payload duration clamped to 30
        assert session.post_calls[0][1]["json"]["input"]["duration"] == 30
        os.unlink(result.audio_path)

    @pytest.mark.asyncio
    async def test_version_split_from_model(self, monkeypatch):
        monkeypatch.setattr(mg, "REPLICATE_API_TOKEN", "tok")
        monkeypatch.setattr(mg, "MUSIC_GEN_MODEL", "meta/musicgen:abc123")
        create = _FakeResp(status=201, json_data={
            "status": "succeeded", "output": "https://cdn/s.mp3",
        })
        download = _FakeResp(status=200, read_data=b"A")
        session = _FakeSession([create], [download])
        _install_fake_aiohttp(monkeypatch, session)
        result = await mg.generate("x")
        assert session.post_calls[0][1]["json"]["version"] == "abc123"
        os.unlink(result.audio_path)

    @pytest.mark.asyncio
    async def test_model_without_colon_used_as_version(self, monkeypatch):
        monkeypatch.setattr(mg, "REPLICATE_API_TOKEN", "tok")
        monkeypatch.setattr(mg, "MUSIC_GEN_MODEL", "plainversion")
        create = _FakeResp(status=201, json_data={
            "status": "succeeded", "output": "https://cdn/s.mp3",
        })
        download = _FakeResp(status=200, read_data=b"A")
        session = _FakeSession([create], [download])
        _install_fake_aiohttp(monkeypatch, session)
        result = await mg.generate("x")
        assert session.post_calls[0][1]["json"]["version"] == "plainversion"
        os.unlink(result.audio_path)

    @pytest.mark.asyncio
    async def test_polling_then_success(self, monkeypatch):
        monkeypatch.setattr(mg, "REPLICATE_API_TOKEN", "tok")
        create = _FakeResp(status=200, json_data={
            "status": "starting", "urls": {"get": "https://api/poll"},
        })
        poll = _FakeResp(status=200, json_data={"status": "succeeded", "output": "https://cdn/s.mp3"})
        download = _FakeResp(status=200, read_data=b"A")
        session = _FakeSession([create], [poll, download])
        _install_fake_aiohttp(monkeypatch, session)
        result = await mg.generate("x")
        assert result.error is None
        os.unlink(result.audio_path)


# ══════════════════════════════════════════════════════════════════════════════
# 4. Errors
# ══════════════════════════════════════════════════════════════════════════════


class TestErrors:
    @pytest.mark.asyncio
    async def test_api_error(self, monkeypatch):
        monkeypatch.setattr(mg, "REPLICATE_API_TOKEN", "tok")
        create = _FakeResp(status=422, text_data="invalid")
        session = _FakeSession([create], [])
        _install_fake_aiohttp(monkeypatch, session)
        result = await mg.generate("x")
        assert "Replicate API error 422" in result.error

    @pytest.mark.asyncio
    async def test_failed_status(self, monkeypatch):
        monkeypatch.setattr(mg, "REPLICATE_API_TOKEN", "tok")
        create = _FakeResp(status=201, json_data={"status": "failed", "error": "model error"})
        session = _FakeSession([create], [])
        _install_fake_aiohttp(monkeypatch, session)
        result = await mg.generate("x")
        assert result.error == "model error"

    @pytest.mark.asyncio
    async def test_timeout_status(self, monkeypatch):
        monkeypatch.setattr(mg, "REPLICATE_API_TOKEN", "tok")
        create = _FakeResp(status=201, json_data={
            "status": "processing", "urls": {"get": "https://api/poll"},
        })
        polls = [_FakeResp(status=200, json_data={"status": "processing"}) for _ in range(65)]
        session = _FakeSession([create], polls)
        _install_fake_aiohttp(monkeypatch, session)
        result = await mg.generate("x")
        assert "timed out" in result.error

    @pytest.mark.asyncio
    async def test_no_output(self, monkeypatch):
        monkeypatch.setattr(mg, "REPLICATE_API_TOKEN", "tok")
        create = _FakeResp(status=201, json_data={"status": "succeeded", "output": None})
        session = _FakeSession([create], [])
        _install_fake_aiohttp(monkeypatch, session)
        result = await mg.generate("x")
        assert result.error == "No output from model"

    @pytest.mark.asyncio
    async def test_empty_url_in_nonempty_output(self, monkeypatch):
        monkeypatch.setattr(mg, "REPLICATE_API_TOKEN", "tok")
        # output is a truthy dict (not str/list) → audio_url resolves to ""
        create = _FakeResp(status=201, json_data={"status": "succeeded", "output": {"k": "v"}})
        session = _FakeSession([create], [])
        _install_fake_aiohttp(monkeypatch, session)
        result = await mg.generate("x")
        assert result.error == "No audio URL in output"

    @pytest.mark.asyncio
    async def test_download_failure(self, monkeypatch):
        monkeypatch.setattr(mg, "REPLICATE_API_TOKEN", "tok")
        create = _FakeResp(status=201, json_data={"status": "succeeded", "output": "https://cdn/s.mp3"})
        download = _FakeResp(status=500)
        session = _FakeSession([create], [download])
        _install_fake_aiohttp(monkeypatch, session)
        result = await mg.generate("x")
        assert "Failed to download audio: HTTP 500" in result.error

    @pytest.mark.asyncio
    async def test_unexpected_exception(self, monkeypatch):
        monkeypatch.setattr(mg, "REPLICATE_API_TOKEN", "tok")

        class _Boom:
            async def __aenter__(self):
                raise RuntimeError("kaboom")

            async def __aexit__(self, *exc):
                return False

        _install_fake_aiohttp(monkeypatch, _Boom())
        result = await mg.generate("x")
        assert "kaboom" in result.error

    @pytest.mark.asyncio
    async def test_timeout_error(self, monkeypatch):
        monkeypatch.setattr(mg, "REPLICATE_API_TOKEN", "tok")

        class _T:
            async def __aenter__(self):
                raise asyncio.TimeoutError()

            async def __aexit__(self, *exc):
                return False

        _install_fake_aiohttp(monkeypatch, _T())
        result = await mg.generate("x")
        assert result.error == "Music generation timed out"
