"""
Behavior tests for voice transcription (telechat_pkg.voice_transcription).

Network I/O is mocked via a fake ``aiohttp`` (with ``FormData``) injected into
``sys.modules``; the Whisper API call runs in-process with no real requests.

Run:
    pytest tests/test_voice_transcription.py -v
"""

import asyncio
import os
import sys
import types

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")

from telechat_pkg import voice_transcription as vt


# ── Fake aiohttp scaffolding ─────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, *, status=200, json_data=None, text_data=""):
        self.status = status
        self._json = json_data or {}
        self._text = text_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._json

    async def text(self):
        return self._text


class _FakeSession:
    def __init__(self, response):
        self._response = response
        self.post_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self._response


class _FakeFormData:
    def __init__(self):
        self.fields = []

    def add_field(self, name, value, **kwargs):
        self.fields.append((name, value, kwargs))


def _install_fake_aiohttp(monkeypatch, session):
    fake = types.ModuleType("aiohttp")
    fake.ClientSession = lambda *a, **k: session
    fake.FormData = _FakeFormData

    class ClientTimeout:
        def __init__(self, total=None):
            self.total = total

    fake.ClientTimeout = ClientTimeout
    monkeypatch.setitem(sys.modules, "aiohttp", fake)


@pytest.fixture
def audio_file(tmp_path):
    p = tmp_path / "voice.ogg"
    p.write_bytes(b"FAKE_AUDIO_BYTES")
    return str(p)


# ══════════════════════════════════════════════════════════════════════════════
# 1. Availability
# ══════════════════════════════════════════════════════════════════════════════


class TestAvailability:
    def test_available(self, monkeypatch):
        monkeypatch.setattr(vt, "TRANSCRIPTION_ENABLED", True)
        monkeypatch.setattr(vt, "OPENAI_API_KEY", "sk-x")
        assert vt.is_available() is True

    def test_unavailable_disabled(self, monkeypatch):
        monkeypatch.setattr(vt, "TRANSCRIPTION_ENABLED", False)
        monkeypatch.setattr(vt, "OPENAI_API_KEY", "sk-x")
        assert vt.is_available() is False

    def test_unavailable_no_key(self, monkeypatch):
        monkeypatch.setattr(vt, "TRANSCRIPTION_ENABLED", True)
        monkeypatch.setattr(vt, "OPENAI_API_KEY", "")
        assert vt.is_available() is False


# ══════════════════════════════════════════════════════════════════════════════
# 2. Guards
# ══════════════════════════════════════════════════════════════════════════════


class TestGuards:
    @pytest.mark.asyncio
    async def test_no_api_key(self, monkeypatch, audio_file):
        monkeypatch.setattr(vt, "OPENAI_API_KEY", "")
        result = await vt.transcribe(audio_file)
        assert result.error == "OPENAI_API_KEY not set"

    @pytest.mark.asyncio
    async def test_file_too_large(self, monkeypatch, audio_file):
        monkeypatch.setattr(vt, "OPENAI_API_KEY", "sk-x")
        monkeypatch.setattr(vt, "MAX_AUDIO_SIZE", 1)  # 1 byte limit
        result = await vt.transcribe(audio_file)
        assert "too large" in result.error

    @pytest.mark.asyncio
    async def test_missing_aiohttp(self, monkeypatch, audio_file):
        monkeypatch.setattr(vt, "OPENAI_API_KEY", "sk-x")
        monkeypatch.setattr(vt, "MAX_AUDIO_SIZE", 10 * 1024 * 1024)
        monkeypatch.setitem(sys.modules, "aiohttp", None)
        result = await vt.transcribe(audio_file)
        assert result.error == "aiohttp not installed"


# ══════════════════════════════════════════════════════════════════════════════
# 3. Success
# ══════════════════════════════════════════════════════════════════════════════


class TestSuccess:
    @pytest.mark.asyncio
    async def test_transcribes_audio(self, monkeypatch, audio_file):
        monkeypatch.setattr(vt, "OPENAI_API_KEY", "sk-x")
        monkeypatch.setattr(vt, "MAX_AUDIO_SIZE", 10 * 1024 * 1024)
        resp = _FakeResp(status=200, json_data={
            "text": "hello world", "language": "english", "duration": 3.2,
        })
        session = _FakeSession(resp)
        _install_fake_aiohttp(monkeypatch, session)
        result = await vt.transcribe(audio_file)
        assert result.error is None
        assert result.text == "hello world"
        assert result.language == "english"
        assert result.duration_seconds == 3.2

    @pytest.mark.asyncio
    async def test_language_hint_added_to_form(self, monkeypatch, audio_file):
        monkeypatch.setattr(vt, "OPENAI_API_KEY", "sk-x")
        monkeypatch.setattr(vt, "MAX_AUDIO_SIZE", 10 * 1024 * 1024)
        resp = _FakeResp(status=200, json_data={"text": "bonjour", "language": "french", "duration": 1.0})
        session = _FakeSession(resp)
        _install_fake_aiohttp(monkeypatch, session)
        result = await vt.transcribe(audio_file, language="fr")
        assert result.text == "bonjour"

    @pytest.mark.asyncio
    async def test_missing_fields_default_empty(self, monkeypatch, audio_file):
        monkeypatch.setattr(vt, "OPENAI_API_KEY", "sk-x")
        monkeypatch.setattr(vt, "MAX_AUDIO_SIZE", 10 * 1024 * 1024)
        resp = _FakeResp(status=200, json_data={})
        session = _FakeSession(resp)
        _install_fake_aiohttp(monkeypatch, session)
        result = await vt.transcribe(audio_file)
        assert result.text == ""
        assert result.language == ""
        assert result.duration_seconds == 0


# ══════════════════════════════════════════════════════════════════════════════
# 4. Errors
# ══════════════════════════════════════════════════════════════════════════════


class TestErrors:
    @pytest.mark.asyncio
    async def test_api_error_status(self, monkeypatch, audio_file):
        monkeypatch.setattr(vt, "OPENAI_API_KEY", "sk-x")
        monkeypatch.setattr(vt, "MAX_AUDIO_SIZE", 10 * 1024 * 1024)
        resp = _FakeResp(status=401, text_data="unauthorized")
        session = _FakeSession(resp)
        _install_fake_aiohttp(monkeypatch, session)
        result = await vt.transcribe(audio_file)
        assert "Whisper API error 401" in result.error

    @pytest.mark.asyncio
    async def test_timeout_error(self, monkeypatch, audio_file):
        monkeypatch.setattr(vt, "OPENAI_API_KEY", "sk-x")
        monkeypatch.setattr(vt, "MAX_AUDIO_SIZE", 10 * 1024 * 1024)

        class _T:
            async def __aenter__(self):
                raise asyncio.TimeoutError()

            async def __aexit__(self, *exc):
                return False

        _install_fake_aiohttp(monkeypatch, _T())
        result = await vt.transcribe(audio_file)
        assert result.error == "Transcription timed out"

    @pytest.mark.asyncio
    async def test_unexpected_exception(self, monkeypatch, audio_file):
        monkeypatch.setattr(vt, "OPENAI_API_KEY", "sk-x")
        monkeypatch.setattr(vt, "MAX_AUDIO_SIZE", 10 * 1024 * 1024)

        class _Boom:
            async def __aenter__(self):
                raise RuntimeError("connection reset")

            async def __aexit__(self, *exc):
                return False

        _install_fake_aiohttp(monkeypatch, _Boom())
        result = await vt.transcribe(audio_file)
        assert "connection reset" in result.error
