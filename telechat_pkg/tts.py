"""
Text-to-speech — generate voice audio from text using OpenAI's TTS API.

"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass

log = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
TTS_MODEL = os.getenv("TTS_MODEL", "tts-1")
TTS_VOICE = os.getenv("TTS_VOICE", "alloy")
TTS_MAX_LENGTH = int(os.getenv("TTS_MAX_LENGTH", "4096"))
TTS_ENABLED = os.getenv("TTS_ENABLED", "false").lower() in ("1", "true", "yes")

VOICES = ("alloy", "echo", "fable", "onyx", "nova", "shimmer")


@dataclass
class TtsResult:
    audio_path: str
    voice: str
    model: str
    text_length: int
    error: str | None = None


def is_available() -> bool:
    return TTS_ENABLED and bool(OPENAI_API_KEY)


async def synthesize(
    text: str,
    voice: str = TTS_VOICE,
    model: str = TTS_MODEL,
) -> TtsResult:
    if not OPENAI_API_KEY:
        return TtsResult(audio_path="", voice=voice, model=model,
                         text_length=len(text), error="OPENAI_API_KEY not set")
    if voice not in VOICES:
        voice = TTS_VOICE
    if len(text) > TTS_MAX_LENGTH:
        text = text[:TTS_MAX_LENGTH]

    try:
        import aiohttp
    except ImportError:
        return TtsResult(audio_path="", voice=voice, model=model,
                         text_length=len(text), error="aiohttp not installed")

    url = "https://api.openai.com/v1/audio/speech"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "input": text,
        "voice": voice,
        "response_format": "opus",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return TtsResult(audio_path="", voice=voice, model=model,
                                     text_length=len(text),
                                     error=f"OpenAI TTS API error {resp.status}: {body[:200]}")
                audio_data = await resp.read()
                tmp = tempfile.NamedTemporaryFile(suffix=".opus", delete=False)
                tmp.write(audio_data)
                tmp.close()
                return TtsResult(
                    audio_path=tmp.name, voice=voice, model=model,
                    text_length=len(text),
                )
    except asyncio.TimeoutError:
        return TtsResult(audio_path="", voice=voice, model=model,
                         text_length=len(text), error="TTS request timed out")
    except Exception as e:
        return TtsResult(audio_path="", voice=voice, model=model,
                         text_length=len(text), error=str(e)[:200])
