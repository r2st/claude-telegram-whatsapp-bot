"""
Voice transcription — convert audio messages to text using OpenAI Whisper API.

"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-1")
TRANSCRIPTION_ENABLED = os.getenv("TRANSCRIPTION_ENABLED", "false").lower() in ("1", "true", "yes")
MAX_AUDIO_SIZE = int(os.getenv("TRANSCRIPTION_MAX_SIZE_MB", "25")) * 1024 * 1024


@dataclass
class TranscriptionResult:
    text: str
    language: str
    duration_seconds: float
    error: str | None = None


def is_available() -> bool:
    return TRANSCRIPTION_ENABLED and bool(OPENAI_API_KEY)


async def transcribe(
    audio_path: str,
    language: str | None = None,
) -> TranscriptionResult:
    """Transcribe an audio file using OpenAI Whisper API."""
    if not OPENAI_API_KEY:
        return TranscriptionResult(text="", language="", duration_seconds=0,
                                   error="OPENAI_API_KEY not set")

    file_size = os.path.getsize(audio_path)
    if file_size > MAX_AUDIO_SIZE:
        return TranscriptionResult(text="", language="", duration_seconds=0,
                                   error=f"Audio file too large ({file_size // 1024 // 1024}MB, max {MAX_AUDIO_SIZE // 1024 // 1024}MB)")

    try:
        import aiohttp
        from aiohttp import FormData
    except ImportError:
        return TranscriptionResult(text="", language="", duration_seconds=0,
                                   error="aiohttp not installed")

    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}

    try:
        with open(audio_path, "rb") as audio_file:
            data = FormData()
            data.add_field("file", audio_file,
                           filename=os.path.basename(audio_path),
                           content_type="audio/ogg")
            data.add_field("model", WHISPER_MODEL)
            data.add_field("response_format", "verbose_json")
            if language:
                data.add_field("language", language)

            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, data=data,
                                        timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        return TranscriptionResult(
                            text="", language="", duration_seconds=0,
                            error=f"Whisper API error {resp.status}: {body[:200]}",
                        )
                    result = await resp.json()
                    return TranscriptionResult(
                        text=result.get("text", ""),
                        language=result.get("language", ""),
                        duration_seconds=result.get("duration", 0),
                    )
    except asyncio.TimeoutError:
        return TranscriptionResult(text="", language="", duration_seconds=0,
                                   error="Transcription timed out")
    except Exception as e:
        return TranscriptionResult(text="", language="", duration_seconds=0,
                                   error=str(e)[:200])
