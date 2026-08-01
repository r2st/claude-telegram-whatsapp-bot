"""
Voice transcription — turn an inbound voice message into text.

Voice notes are how a great many people prefer to talk to a bot, but this
feature was effectively dormant: it required a paid ``OPENAI_API_KEY``, so
the answer to "can I just talk to it?" was "first go buy an OpenAI key".

Groq serves Whisper on a free tier and speaks the same OpenAI-shaped
multipart API, so the only real difference is a base URL, a key and a model
name. That is what this module abstracts. Set ``GROQ_API_KEY`` and voice
messages work at no cost; an existing OpenAI setup keeps working untouched.

Configuration is read per call rather than at import, so a key added to
``.env`` takes effect on the next message instead of the next restart —
and so ``telechat doctor`` and the tests see the current environment.

Usage:
    from telechat_pkg import voice_transcription as vt

    if vt.is_available():
        result = await vt.transcribe("/tmp/voice.ogg")
        result.text        # 'what is the weather in Oslo'
        result.provider    # 'groq'
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Provider:
    name: str
    url: str
    key_env: str
    default_model: str
    #: Reads the key. A callable holding a literal ``os.getenv`` rather than a
    #: lookup through ``key_env``, so the settings scanner behind
    #: ``telechat doctor`` and docs/configuration.md can still see the name.
    read_key: Callable[[], str]
    #: Where to send someone who has no key yet. Free tiers first.
    signup: str = ""


PROVIDERS: dict[str, Provider] = {
    "groq": Provider(
        name="groq",
        url="https://api.groq.com/openai/v1/audio/transcriptions",
        key_env="GROQ_API_KEY",
        default_model="whisper-large-v3-turbo",
        read_key=lambda: os.getenv("GROQ_API_KEY", ""),
        signup="https://console.groq.com/keys",
    ),
    "openai": Provider(
        name="openai",
        url="https://api.openai.com/v1/audio/transcriptions",
        key_env="OPENAI_API_KEY",
        default_model="whisper-1",
        read_key=lambda: os.getenv("OPENAI_API_KEY", ""),
    ),
}

#: Provider preference when ``TRANSCRIPTION_PROVIDER`` is unset. Groq leads
#: because it is the one that costs nothing.
_AUTO_ORDER = ("groq", "openai")

#: Model names that only exist on one provider. Shipping
#: ``WHISPER_MODEL=whisper-1`` in .env.example means a lot of installs carry
#: that line; sending it to Groq is a guaranteed 404, and the operator would
#: have no idea why voice stopped working the moment they added a Groq key.
_PROVIDER_ONLY_MODELS = {"whisper-1": "openai"}

DEFAULT_MAX_SIZE_MB = 25
_TIMEOUT_SECONDS = 90


@dataclass
class TranscriptionResult:
    text: str
    language: str
    duration_seconds: float
    error: Optional[str] = None
    provider: str = ""


# ─── Configuration ────────────────────────────────────────────────────────────


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _key_for(provider: Provider) -> str:
    return _clean(provider.read_key())


def configured_providers() -> list[Provider]:
    """Every provider that has a key, in preference order."""
    return [PROVIDERS[n] for n in _AUTO_ORDER if _key_for(PROVIDERS[n])]


def select_provider() -> Optional[Provider]:
    """The provider to use, or None when nothing is configured.

    ``TRANSCRIPTION_PROVIDER`` pins one explicitly; otherwise the first
    configured provider in preference order wins.
    """
    requested = _clean(os.getenv("TRANSCRIPTION_PROVIDER", "")).lower()
    if requested and requested != "auto":
        provider = PROVIDERS.get(requested)
        if provider is None:
            log.warning(
                "TRANSCRIPTION_PROVIDER=%r is not one of %s — falling back to auto",
                requested, ", ".join(PROVIDERS),
            )
        elif _key_for(provider):
            return provider
        else:
            # Pinned but keyless. Falling back to a different provider would
            # silently ignore what the operator asked for, so refuse instead.
            return None
    available = configured_providers()
    return available[0] if available else None


def model_for(provider: Provider) -> str:
    """The model to request, honouring ``WHISPER_MODEL`` when it can apply."""
    override = _clean(os.getenv("WHISPER_MODEL", ""))
    if not override:
        return provider.default_model
    owner = _PROVIDER_ONLY_MODELS.get(override.lower())
    if owner and owner != provider.name:
        log.debug(
            "WHISPER_MODEL=%r belongs to %s, not %s — using %s instead",
            override, owner, provider.name, provider.default_model,
        )
        return provider.default_model
    return override


def max_audio_size() -> int:
    """Largest accepted audio file, in bytes."""
    raw = _clean(os.getenv("TRANSCRIPTION_MAX_SIZE_MB", ""))
    try:
        mb = int(float(raw)) if raw else DEFAULT_MAX_SIZE_MB
    except ValueError:
        mb = DEFAULT_MAX_SIZE_MB
    return max(1, mb) * 1024 * 1024


def is_available() -> bool:
    """Whether an inbound voice message will be transcribed.

    ``TRANSCRIPTION_ENABLED`` still decides when it is set either way. Left
    unset, a Groq key turns it on by itself: that key has no other use here,
    so setting it *is* the request. An OpenAI key is not treated as consent,
    because TTS and image generation share it — someone who set it to make
    pictures did not thereby ask to send their voice notes to OpenAI.
    """
    explicit = _clean(os.getenv("TRANSCRIPTION_ENABLED", "")).lower()
    provider = select_provider()
    if explicit in ("1", "true", "yes", "on"):
        return provider is not None
    if explicit in ("0", "false", "no", "off"):
        return False
    return provider is not None and provider.name == "groq"


def availability_hint() -> str:
    """One line telling the operator how to switch voice messages on."""
    if is_available():
        provider = select_provider()
        return f"Voice transcription is on via {provider.name}." if provider else ""
    groq = PROVIDERS["groq"]
    if not configured_providers():
        return (
            f"Voice transcription needs a key. {groq.key_env} is free — "
            f"get one at {groq.signup} and add it to .env."
        )
    return "Voice transcription is configured but TRANSCRIPTION_ENABLED=false."


# ─── Transcription ────────────────────────────────────────────────────────────


def _content_type(path: str) -> str:
    """Best-effort MIME type. Telegram voice notes are Ogg/Opus."""
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "audio/ogg"


def _explain(status: int, body: str, provider: Provider) -> str:
    """Turn an API failure into something the operator can act on."""
    if status in (401, 403):
        return f"{provider.key_env} was rejected by {provider.name}."
    if status == 429:
        return f"{provider.name} rate limit reached — try again shortly."
    if status == 413:
        return "That audio file is too large for the transcription service."
    if status == 404 and provider.name == "groq":
        # Almost always a model name that does not exist on Groq.
        return (
            f"{provider.name} does not know the model "
            f"{model_for(provider)!r}. Unset WHISPER_MODEL to use the default."
        )
    detail = (body or "").strip().replace("\n", " ")
    return f"{provider.name} transcription failed ({status}): {detail[:160]}"


async def transcribe(
    audio_path: str,
    language: str | None = None,
) -> TranscriptionResult:
    """Transcribe an audio file with whichever provider is configured."""
    provider = select_provider()
    if provider is None:
        requested = _clean(os.getenv("TRANSCRIPTION_PROVIDER", "")).lower()
        if requested and requested != "auto" and requested in PROVIDERS:
            missing = PROVIDERS[requested].key_env
            return TranscriptionResult(
                "", "", 0, error=f"TRANSCRIPTION_PROVIDER={requested} but {missing} is not set"
            )
        groq = PROVIDERS["groq"]
        return TranscriptionResult(
            "", "", 0,
            error=f"No transcription key set. {groq.key_env} is free: {groq.signup}",
        )

    try:
        file_size = os.path.getsize(audio_path)
    except OSError as exc:
        return TranscriptionResult("", "", 0, error=f"Could not read the audio file: {exc}",
                                   provider=provider.name)

    limit = max_audio_size()
    if file_size > limit:
        return TranscriptionResult(
            "", "", 0, provider=provider.name,
            error=f"Audio file too large ({file_size // 1024 // 1024}MB, max {limit // 1024 // 1024}MB)",
        )

    try:
        import aiohttp
        from aiohttp import FormData
    except ImportError:
        return TranscriptionResult("", "", 0, error="aiohttp not installed",
                                   provider=provider.name)

    headers = {"Authorization": f"Bearer {_key_for(provider)}"}
    model = model_for(provider)

    try:
        with open(audio_path, "rb") as audio_file:
            data = FormData()
            data.add_field("file", audio_file,
                           filename=os.path.basename(audio_path),
                           content_type=_content_type(audio_path))
            data.add_field("model", model)
            data.add_field("response_format", "verbose_json")
            if language:
                data.add_field("language", language)

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    provider.url, headers=headers, data=data,
                    timeout=aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        return TranscriptionResult(
                            "", "", 0, provider=provider.name,
                            error=_explain(resp.status, body, provider),
                        )
                    result = await resp.json()
                    return TranscriptionResult(
                        text=result.get("text", "") or "",
                        language=result.get("language", "") or "",
                        duration_seconds=result.get("duration", 0) or 0,
                        provider=provider.name,
                    )
    except asyncio.TimeoutError:
        return TranscriptionResult("", "", 0, error="Transcription timed out",
                                   provider=provider.name)
    except Exception as e:
        return TranscriptionResult("", "", 0, error=str(e)[:200], provider=provider.name)
