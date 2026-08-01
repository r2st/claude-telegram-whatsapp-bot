"""
Behavior tests for voice transcription (telechat_pkg.voice_transcription).

Network I/O is mocked via a fake ``aiohttp`` (with ``FormData``) injected into
``sys.modules``; the transcription call runs in-process with no real requests.

Configuration is read from the environment on every call, so these tests set
env vars rather than patching module constants. The autouse fixture clears
every relevant variable first — the real environment (or a developer's .env)
must not decide what these assert.

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

_CONFIG_VARS = (
    "GROQ_API_KEY", "OPENAI_API_KEY", "TRANSCRIPTION_ENABLED",
    "TRANSCRIPTION_PROVIDER", "TRANSCRIPTION_MAX_SIZE_MB", "WHISPER_MODEL",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _CONFIG_VARS:
        monkeypatch.delenv(name, raising=False)
    yield


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

    def field(self, name):
        for n, value, _ in self.fields:
            if n == name:
                return value
        return None


def _install_fake_aiohttp(monkeypatch, session):
    fake = types.ModuleType("aiohttp")
    fake.ClientSession = lambda *a, **k: session
    fake.FormData = _FakeFormData

    class ClientTimeout:
        def __init__(self, total=None):
            self.total = total

    fake.ClientTimeout = ClientTimeout
    monkeypatch.setitem(sys.modules, "aiohttp", fake)


def _ok_session(monkeypatch, **json_data):
    payload = {"text": "hello world", "language": "english", "duration": 3.2}
    payload.update(json_data)
    session = _FakeSession(_FakeResp(status=200, json_data=payload))
    _install_fake_aiohttp(monkeypatch, session)
    return session


def _sent_form(session):
    """The FormData the transcriber posted."""
    return session.post_calls[0][1]["data"]


@pytest.fixture
def audio_file(tmp_path):
    p = tmp_path / "voice.ogg"
    p.write_bytes(b"FAKE_AUDIO_BYTES")
    return str(p)


# ══════════════════════════════════════════════════════════════════════════════
# 1. Provider selection
# ══════════════════════════════════════════════════════════════════════════════


class TestProviderSelection:
    def test_nothing_configured_selects_nothing(self):
        assert vt.select_provider() is None

    def test_a_groq_key_alone_selects_groq(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
        assert vt.select_provider().name == "groq"

    def test_an_openai_key_alone_selects_openai(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
        assert vt.select_provider().name == "openai"

    def test_groq_wins_when_both_are_configured(self, monkeypatch):
        """The free one is the default; nobody should pay by accident."""
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
        assert vt.select_provider().name == "groq"

    def test_an_explicit_provider_overrides_preference(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
        monkeypatch.setenv("TRANSCRIPTION_PROVIDER", "openai")
        assert vt.select_provider().name == "openai"

    def test_auto_is_the_same_as_unset(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
        monkeypatch.setenv("TRANSCRIPTION_PROVIDER", "auto")
        assert vt.select_provider().name == "groq"

    def test_a_pinned_provider_with_no_key_selects_nothing(self, monkeypatch):
        """Silently using the other provider would ignore an explicit choice."""
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
        monkeypatch.setenv("TRANSCRIPTION_PROVIDER", "openai")
        assert vt.select_provider() is None

    def test_an_unknown_provider_name_falls_back_to_auto(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
        monkeypatch.setenv("TRANSCRIPTION_PROVIDER", "nonesuch")
        assert vt.select_provider().name == "groq"

    def test_provider_names_are_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
        monkeypatch.setenv("TRANSCRIPTION_PROVIDER", "OpenAI")
        assert vt.select_provider().name == "openai"

    def test_a_blank_key_does_not_count_as_configured(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "   ")
        assert vt.select_provider() is None


# ══════════════════════════════════════════════════════════════════════════════
# 2. Availability
# ══════════════════════════════════════════════════════════════════════════════


class TestAvailability:
    def test_a_groq_key_enables_it_on_its_own(self, monkeypatch):
        """GROQ_API_KEY has no other use here, so setting it is the request."""
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
        assert vt.is_available() is True

    def test_an_openai_key_alone_does_not_enable_it(self, monkeypatch):
        """TTS and image generation share that key; it is not consent."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
        assert vt.is_available() is False

    def test_openai_works_when_switched_on_explicitly(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
        monkeypatch.setenv("TRANSCRIPTION_ENABLED", "true")
        assert vt.is_available() is True

    def test_explicit_false_beats_a_groq_key(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
        monkeypatch.setenv("TRANSCRIPTION_ENABLED", "false")
        assert vt.is_available() is False

    def test_enabled_without_any_key_is_still_unavailable(self, monkeypatch):
        monkeypatch.setenv("TRANSCRIPTION_ENABLED", "true")
        assert vt.is_available() is False

    def test_nothing_configured_is_unavailable(self):
        assert vt.is_available() is False

    def test_the_hint_points_at_the_free_option(self):
        hint = vt.availability_hint()
        assert "GROQ_API_KEY" in hint and "console.groq.com" in hint

    def test_the_hint_names_the_provider_in_use(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
        assert "groq" in vt.availability_hint()

    def test_the_hint_calls_out_an_explicit_opt_out(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
        monkeypatch.setenv("TRANSCRIPTION_ENABLED", "false")
        assert "TRANSCRIPTION_ENABLED=false" in vt.availability_hint()


# ══════════════════════════════════════════════════════════════════════════════
# 3. Model choice
# ══════════════════════════════════════════════════════════════════════════════


class TestModelChoice:
    def test_each_provider_has_its_own_default(self):
        assert vt.model_for(vt.PROVIDERS["groq"]) == "whisper-large-v3-turbo"
        assert vt.model_for(vt.PROVIDERS["openai"]) == "whisper-1"

    def test_an_override_is_honoured(self, monkeypatch):
        monkeypatch.setenv("WHISPER_MODEL", "whisper-large-v3")
        assert vt.model_for(vt.PROVIDERS["groq"]) == "whisper-large-v3"

    def test_a_stale_openai_model_is_not_sent_to_groq(self, monkeypatch):
        """.env.example shipped WHISPER_MODEL=whisper-1 for a long time.

        Groq has no such model, so honouring the leftover line would 404 every
        voice message the moment someone added a Groq key — with nothing to
        connect the failure to a setting they configured months earlier.
        """
        monkeypatch.setenv("WHISPER_MODEL", "whisper-1")
        assert vt.model_for(vt.PROVIDERS["groq"]) == "whisper-large-v3-turbo"

    def test_that_same_model_is_still_used_for_openai(self, monkeypatch):
        monkeypatch.setenv("WHISPER_MODEL", "whisper-1")
        assert vt.model_for(vt.PROVIDERS["openai"]) == "whisper-1"

    def test_an_empty_override_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("WHISPER_MODEL", "  ")
        assert vt.model_for(vt.PROVIDERS["groq"]) == "whisper-large-v3-turbo"


# ══════════════════════════════════════════════════════════════════════════════
# 4. Size limit
# ══════════════════════════════════════════════════════════════════════════════


class TestSizeLimit:
    def test_default_limit(self):
        assert vt.max_audio_size() == 25 * 1024 * 1024

    def test_configured_limit(self, monkeypatch):
        monkeypatch.setenv("TRANSCRIPTION_MAX_SIZE_MB", "10")
        assert vt.max_audio_size() == 10 * 1024 * 1024

    def test_a_nonsense_limit_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("TRANSCRIPTION_MAX_SIZE_MB", "banana")
        assert vt.max_audio_size() == 25 * 1024 * 1024

    def test_zero_is_clamped_to_something_usable(self, monkeypatch):
        monkeypatch.setenv("TRANSCRIPTION_MAX_SIZE_MB", "0")
        assert vt.max_audio_size() == 1024 * 1024


# ══════════════════════════════════════════════════════════════════════════════
# 5. Guards
# ══════════════════════════════════════════════════════════════════════════════


class TestGuards:
    @pytest.mark.asyncio
    async def test_no_key_names_the_free_option(self, audio_file):
        result = await vt.transcribe(audio_file)
        assert "GROQ_API_KEY" in result.error

    @pytest.mark.asyncio
    async def test_a_pinned_provider_with_no_key_says_which_key(self, monkeypatch, audio_file):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
        monkeypatch.setenv("TRANSCRIPTION_PROVIDER", "openai")
        result = await vt.transcribe(audio_file)
        assert "OPENAI_API_KEY" in result.error

    @pytest.mark.asyncio
    async def test_file_too_large(self, monkeypatch, audio_file):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
        monkeypatch.setenv("TRANSCRIPTION_MAX_SIZE_MB", "0")
        monkeypatch.setattr(os.path, "getsize", lambda _p: 2 * 1024 * 1024)
        result = await vt.transcribe(audio_file)
        assert "too large" in result.error

    @pytest.mark.asyncio
    async def test_a_missing_file_is_reported_not_raised(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
        result = await vt.transcribe(str(tmp_path / "gone.ogg"))
        assert "could not read" in result.error.lower()

    @pytest.mark.asyncio
    async def test_missing_aiohttp(self, monkeypatch, audio_file):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
        monkeypatch.setitem(sys.modules, "aiohttp", None)
        result = await vt.transcribe(audio_file)
        assert result.error == "aiohttp not installed"


# ══════════════════════════════════════════════════════════════════════════════
# 6. Success
# ══════════════════════════════════════════════════════════════════════════════


class TestSuccess:
    @pytest.mark.asyncio
    async def test_transcribes_audio(self, monkeypatch, audio_file):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
        _ok_session(monkeypatch)
        result = await vt.transcribe(audio_file)
        assert result.error is None
        assert result.text == "hello world"
        assert result.language == "english"
        assert result.duration_seconds == 3.2
        assert result.provider == "groq"

    @pytest.mark.asyncio
    async def test_it_posts_to_the_selected_provider(self, monkeypatch, audio_file):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
        session = _ok_session(monkeypatch)
        await vt.transcribe(audio_file)
        url, kwargs = session.post_calls[0]
        assert url == vt.PROVIDERS["groq"].url
        assert kwargs["headers"]["Authorization"] == "Bearer gsk-x"

    @pytest.mark.asyncio
    async def test_openai_still_works(self, monkeypatch, audio_file):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
        monkeypatch.setenv("TRANSCRIPTION_ENABLED", "true")
        session = _ok_session(monkeypatch)
        result = await vt.transcribe(audio_file)
        assert result.provider == "openai"
        assert session.post_calls[0][0] == vt.PROVIDERS["openai"].url

    @pytest.mark.asyncio
    async def test_the_provider_default_model_is_sent(self, monkeypatch, audio_file):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
        session = _ok_session(monkeypatch)
        await vt.transcribe(audio_file)
        assert _sent_form(session).field("model") == "whisper-large-v3-turbo"

    @pytest.mark.asyncio
    async def test_language_hint_added_to_form(self, monkeypatch, audio_file):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
        session = _ok_session(monkeypatch, text="bonjour", language="french")
        result = await vt.transcribe(audio_file, language="fr")
        assert result.text == "bonjour"
        assert _sent_form(session).field("language") == "fr"

    @pytest.mark.asyncio
    async def test_no_language_field_without_a_hint(self, monkeypatch, audio_file):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
        session = _ok_session(monkeypatch)
        await vt.transcribe(audio_file)
        assert _sent_form(session).field("language") is None

    @pytest.mark.asyncio
    async def test_missing_fields_default_empty(self, monkeypatch, audio_file):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
        session = _FakeSession(_FakeResp(status=200, json_data={}))
        _install_fake_aiohttp(monkeypatch, session)
        result = await vt.transcribe(audio_file)
        assert result.text == ""
        assert result.language == ""
        assert result.duration_seconds == 0

    @pytest.mark.asyncio
    async def test_null_fields_do_not_become_none(self, monkeypatch, audio_file):
        """A provider returning explicit nulls must not poison the result."""
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
        session = _FakeSession(_FakeResp(
            status=200, json_data={"text": None, "language": None, "duration": None},
        ))
        _install_fake_aiohttp(monkeypatch, session)
        result = await vt.transcribe(audio_file)
        assert result.text == ""
        assert result.language == ""
        assert result.duration_seconds == 0


# ══════════════════════════════════════════════════════════════════════════════
# 7. Errors
# ══════════════════════════════════════════════════════════════════════════════


class TestErrors:
    @pytest.mark.asyncio
    async def test_a_rejected_key_names_the_variable_to_fix(self, monkeypatch, audio_file):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
        _install_fake_aiohttp(monkeypatch, _FakeSession(
            _FakeResp(status=401, text_data="unauthorized")))
        result = await vt.transcribe(audio_file)
        assert "GROQ_API_KEY" in result.error

    @pytest.mark.asyncio
    async def test_rate_limit_is_reported_as_temporary(self, monkeypatch, audio_file):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
        _install_fake_aiohttp(monkeypatch, _FakeSession(
            _FakeResp(status=429, text_data="slow down")))
        result = await vt.transcribe(audio_file)
        assert "rate limit" in result.error.lower()

    @pytest.mark.asyncio
    async def test_an_unknown_groq_model_explains_the_fix(self, monkeypatch, audio_file):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
        monkeypatch.setenv("WHISPER_MODEL", "whisper-nope")
        _install_fake_aiohttp(monkeypatch, _FakeSession(
            _FakeResp(status=404, text_data="model not found")))
        result = await vt.transcribe(audio_file)
        assert "WHISPER_MODEL" in result.error

    @pytest.mark.asyncio
    async def test_an_unmapped_status_still_reports_the_body(self, monkeypatch, audio_file):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
        _install_fake_aiohttp(monkeypatch, _FakeSession(
            _FakeResp(status=500, text_data="upstream exploded")))
        result = await vt.transcribe(audio_file)
        assert "500" in result.error and "upstream exploded" in result.error

    @pytest.mark.asyncio
    async def test_timeout_error(self, monkeypatch, audio_file):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")

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
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")

        class _Boom:
            async def __aenter__(self):
                raise RuntimeError("connection reset")

            async def __aexit__(self, *exc):
                return False

        _install_fake_aiohttp(monkeypatch, _Boom())
        result = await vt.transcribe(audio_file)
        assert "connection reset" in result.error
