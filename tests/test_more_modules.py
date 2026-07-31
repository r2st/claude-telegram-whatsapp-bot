"""Tests for error_classifier, music_gen, resource_limiter, scheduled_tasks,
text_chunking, video_gen, voice_transcription, web_fetch modules."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ═══════════════════════════════════════════════════════════════════════════════
# error_classifier.py
# ═══════════════════════════════════════════════════════════════════════════════
from telechat_pkg.error_classifier import (
    ErrorCategory, RecoveryStrategy, ErrorClassification, ConvergenceDetector,
    classify_error, _fingerprint, format_classification,
)


class TestClassifyError:
    def test_syntax_error(self):
        r = classify_error("SyntaxError: invalid syntax at line 5")
        assert r.category == ErrorCategory.SYNTAX_ERROR
        assert r.strategy == RecoveryStrategy.DIRECT_FIX
        assert r.confidence == 0.95

    def test_type_error(self):
        r = classify_error("TypeError: cannot add int and str")
        assert r.category == ErrorCategory.TYPE_ERROR
        assert r.strategy == RecoveryStrategy.INCLUDE_TYPES

    def test_import_error(self):
        r = classify_error("ModuleNotFoundError: No module named 'foo'")
        assert r.category == ErrorCategory.IMPORT_ERROR

    def test_logic_error(self):
        r = classify_error("AssertionError: Expected 5 but got 3")
        assert r.category == ErrorCategory.LOGIC_ERROR
        assert r.strategy == RecoveryStrategy.INCLUDE_BROAD_CONTEXT

    def test_environment_error(self):
        r = classify_error("FileNotFoundError: [Errno 2] No such file")
        assert r.category == ErrorCategory.ENVIRONMENT_ERROR

    def test_flaky_error(self):
        r = classify_error("connection refused timeout after 30s")
        assert r.category == ErrorCategory.FLAKY_ERROR
        assert r.strategy == RecoveryStrategy.RERUN

    def test_integration_error(self):
        r = classify_error("database error: connection pool exhausted")
        assert r.category == ErrorCategory.INTEGRATION_ERROR

    def test_architectural_error(self):
        r = classify_error("circular dependency detected between modules")
        assert r.category == ErrorCategory.ARCHITECTURAL_ERROR
        assert r.strategy == RecoveryStrategy.REPLAN

    def test_unknown_error(self):
        r = classify_error("something weird happened xyz123")
        assert r.category == ErrorCategory.UNKNOWN
        assert r.confidence == 0.3

    def test_fingerprint_stable(self):
        f1 = _fingerprint("Error at line 42 in /foo/bar.py")
        f2 = _fingerprint("Error at line 99 in /baz/qux.py")
        assert f1 == f2  # line numbers and paths stripped

    def test_fingerprint_strips_urls_and_hashes(self):
        f = _fingerprint("error at https://example.com/abc with hash abcdef01234567890")
        assert len(f) == 16


class TestFormatClassification:
    def test_format(self):
        cls = ErrorClassification(
            category=ErrorCategory.SYNTAX_ERROR,
            strategy=RecoveryStrategy.DIRECT_FIX,
            fingerprint="abc123",
        )
        out = format_classification(cls)
        assert "syntax" in out
        assert "direct_fix" in out


class TestConvergenceDetector:
    def test_empty_history(self):
        d = ConvergenceDetector()
        r = d.check()
        assert r.status == "progressing"

    def test_progressing(self):
        d = ConvergenceDetector()
        d.record("")
        d.record("fp1")
        d.record("")
        assert d.check().status == "progressing"

    def test_oscillating(self):
        d = ConvergenceDetector(window_size=4)
        d.record("fp1")
        d.record("fp1")
        r = d.check()
        assert r.status == "oscillating"
        assert r.action == "enrich_context"

    def test_stuck(self):
        d = ConvergenceDetector(stuck_threshold=3)
        d.record("fp1")
        d.record("fp2")
        d.record("fp3")
        r = d.check()
        assert r.status == "stuck"
        assert r.action == "replan"

    def test_diverging(self):
        d = ConvergenceDetector(window_size=4)
        d.record("")
        d.record("")
        d.record("fp1")
        d.record("fp2")
        r = d.check()
        assert r.status == "diverging"
        assert r.action == "escalate"

    def test_reset(self):
        d = ConvergenceDetector()
        d.record("fp1")
        d.reset()
        assert d.check().status == "progressing"

    def test_bounded_history(self):
        d = ConvergenceDetector()
        for i in range(25):
            d.record(f"fp{i}")
        assert len(d._history) == 20


# ═══════════════════════════════════════════════════════════════════════════════
# text_chunking.py
# ═══════════════════════════════════════════════════════════════════════════════
from telechat_pkg.text_chunking import chunk_text, _find_fence_spans, _is_inside_fence, _find_best_break


class TestChunkText:
    def test_short_text_single_chunk(self):
        chunks = chunk_text("hello", limit=100)
        assert len(chunks) == 1
        assert chunks[0].text == "hello"
        assert chunks[0].index == 0
        assert chunks[0].total == 1

    def test_length_mode(self):
        text = "A" * 100 + "\n" + "B" * 100
        chunks = chunk_text(text, limit=120, mode="length")
        assert len(chunks) == 2
        assert chunks[0].total == 2

    def test_length_mode_no_newline(self):
        text = "A" * 200
        chunks = chunk_text(text, limit=100, mode="length")
        assert len(chunks) == 2

    def test_smart_mode_paragraph_break(self):
        text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
        chunks = chunk_text(text, limit=30, mode="smart")
        assert len(chunks) >= 2

    def test_smart_mode_code_fence(self):
        text = "Before\n```\ncode line 1\ncode line 2\ncode line 3\n```\nAfter more text here."
        chunks = chunk_text(text, limit=30, mode="smart")
        assert len(chunks) >= 1

    def test_smart_mode_emergency_break(self):
        text = "A" * 200  # no newlines, no breaks
        chunks = chunk_text(text, limit=100, mode="smart")
        assert len(chunks) >= 2


class TestFindFenceSpans:
    def test_paired_fences(self):
        text = "before\n```\ncode\n```\nafter"
        spans = _find_fence_spans(text)
        assert len(spans) == 1
        assert spans[0][0] < spans[0][1]

    def test_unclosed_fence(self):
        text = "before\n```\ncode without closing"
        spans = _find_fence_spans(text)
        assert len(spans) == 1
        assert spans[0][1] == len(text)

    def test_no_fences(self):
        assert _find_fence_spans("no fences here") == []

    def test_tilde_fences(self):
        text = "~~~\ncode\n~~~"
        spans = _find_fence_spans(text)
        assert len(spans) == 1


class TestIsInsideFence:
    def test_inside(self):
        spans = [(10, 50)]
        assert _is_inside_fence(5, 10, spans) is True

    def test_outside(self):
        spans = [(10, 50)]
        assert _is_inside_fence(0, 5, spans) is False


class TestFindBestBreak:
    def test_paragraph_break(self):
        text = "AAAA\n\nBBBB\n\nCCCC"
        pos = _find_best_break(text, 0, [])
        assert pos > 0

    def test_no_good_break(self):
        text = "AAAAAAAAA"
        pos = _find_best_break(text, 0, [])
        assert pos == 0

    def test_sentence_break(self):
        text = "Short. " + "A" * 50 + ". rest"
        pos = _find_best_break(text, 0, [])
        assert pos > 0


# ═══════════════════════════════════════════════════════════════════════════════
# music_gen.py
# ═══════════════════════════════════════════════════════════════════════════════
from telechat_pkg import music_gen


class TestMusicGen:
    def test_not_available(self, monkeypatch):
        monkeypatch.setattr(music_gen, "MUSIC_GEN_ENABLED", False)
        assert music_gen.is_available() is False

    def test_available(self, monkeypatch):
        monkeypatch.setattr(music_gen, "MUSIC_GEN_ENABLED", True)
        monkeypatch.setattr(music_gen, "REPLICATE_API_TOKEN", "tok")
        assert music_gen.is_available() is True

    @pytest.mark.asyncio
    async def test_no_token(self, monkeypatch):
        monkeypatch.setattr(music_gen, "REPLICATE_API_TOKEN", "")
        r = await music_gen.generate("jazz")
        assert "REPLICATE_API_TOKEN" in r.error

    @pytest.mark.asyncio
    async def test_import_error(self, monkeypatch):
        monkeypatch.setattr(music_gen, "REPLICATE_API_TOKEN", "tok")
        real_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__
        def fail(name, *a, **kw):
            if name == "aiohttp":
                raise ImportError("no")
            return real_import(name, *a, **kw)
        with patch("builtins.__import__", side_effect=fail):
            r = await music_gen.generate("jazz")
        assert "aiohttp" in r.error

    @pytest.mark.asyncio
    async def test_api_error(self, monkeypatch):
        monkeypatch.setattr(music_gen, "REPLICATE_API_TOKEN", "tok")
        mock_resp = AsyncMock(status=500)
        mock_resp.text = AsyncMock(return_value="fail")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_sess = AsyncMock()
        mock_sess.post = MagicMock(return_value=mock_resp)
        mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_sess.__aexit__ = AsyncMock(return_value=False)
        with patch("aiohttp.ClientSession", return_value=mock_sess), patch("aiohttp.ClientTimeout"):
            r = await music_gen.generate("jazz")
        assert "500" in r.error

    @pytest.mark.asyncio
    async def test_success_immediate(self, monkeypatch):
        monkeypatch.setattr(music_gen, "REPLICATE_API_TOKEN", "tok")
        # First resp: prediction succeeded
        mock_resp1 = AsyncMock(status=201)
        mock_resp1.json = AsyncMock(return_value={
            "status": "succeeded", "output": "https://audio.mp3"
        })
        mock_resp1.__aenter__ = AsyncMock(return_value=mock_resp1)
        mock_resp1.__aexit__ = AsyncMock(return_value=False)
        # Download resp
        mock_dl = AsyncMock(status=200)
        mock_dl.read = AsyncMock(return_value=b"mp3data")
        mock_dl.__aenter__ = AsyncMock(return_value=mock_dl)
        mock_dl.__aexit__ = AsyncMock(return_value=False)

        mock_sess = AsyncMock()
        mock_sess.post = MagicMock(return_value=mock_resp1)
        mock_sess.get = MagicMock(return_value=mock_dl)
        mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_sess.__aexit__ = AsyncMock(return_value=False)
        with patch("aiohttp.ClientSession", return_value=mock_sess), patch("aiohttp.ClientTimeout"):
            r = await music_gen.generate("jazz")
        assert r.error is None
        assert r.audio_path

    @pytest.mark.asyncio
    async def test_failed_prediction(self, monkeypatch):
        monkeypatch.setattr(music_gen, "REPLICATE_API_TOKEN", "tok")
        mock_resp = AsyncMock(status=200)
        mock_resp.json = AsyncMock(return_value={"status": "failed", "error": "bad prompt"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_sess = AsyncMock()
        mock_sess.post = MagicMock(return_value=mock_resp)
        mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_sess.__aexit__ = AsyncMock(return_value=False)
        with patch("aiohttp.ClientSession", return_value=mock_sess), patch("aiohttp.ClientTimeout"):
            r = await music_gen.generate("jazz")
        assert "bad prompt" in r.error

    @pytest.mark.asyncio
    async def test_no_output(self, monkeypatch):
        monkeypatch.setattr(music_gen, "REPLICATE_API_TOKEN", "tok")
        mock_resp = AsyncMock(status=200)
        mock_resp.json = AsyncMock(return_value={"status": "succeeded", "output": None})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_sess = AsyncMock()
        mock_sess.post = MagicMock(return_value=mock_resp)
        mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_sess.__aexit__ = AsyncMock(return_value=False)
        with patch("aiohttp.ClientSession", return_value=mock_sess), patch("aiohttp.ClientTimeout"):
            r = await music_gen.generate("jazz")
        assert "No output" in r.error

    @pytest.mark.asyncio
    async def test_timeout(self, monkeypatch):
        monkeypatch.setattr(music_gen, "REPLICATE_API_TOKEN", "tok")
        mock_sess = AsyncMock()
        mock_sess.post = MagicMock(side_effect=asyncio.TimeoutError())
        mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_sess.__aexit__ = AsyncMock(return_value=False)
        with patch("aiohttp.ClientSession", return_value=mock_sess), patch("aiohttp.ClientTimeout"):
            r = await music_gen.generate("jazz")
        assert "timed out" in r.error.lower()

    @pytest.mark.asyncio
    async def test_generic_exception(self, monkeypatch):
        monkeypatch.setattr(music_gen, "REPLICATE_API_TOKEN", "tok")
        mock_sess = AsyncMock()
        mock_sess.post = MagicMock(side_effect=RuntimeError("net"))
        mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_sess.__aexit__ = AsyncMock(return_value=False)
        with patch("aiohttp.ClientSession", return_value=mock_sess), patch("aiohttp.ClientTimeout"):
            r = await music_gen.generate("jazz")
        assert "net" in r.error

    @pytest.mark.asyncio
    async def test_prediction_timed_out_status(self, monkeypatch):
        monkeypatch.setattr(music_gen, "REPLICATE_API_TOKEN", "tok")
        mock_resp = AsyncMock(status=200)
        mock_resp.json = AsyncMock(return_value={"status": "processing", "urls": {}})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_sess = AsyncMock()
        mock_sess.post = MagicMock(return_value=mock_resp)
        mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_sess.__aexit__ = AsyncMock(return_value=False)
        with patch("aiohttp.ClientSession", return_value=mock_sess), patch("aiohttp.ClientTimeout"):
            r = await music_gen.generate("jazz")
        assert "timed out" in r.error.lower() or "status" in r.error.lower()

    @pytest.mark.asyncio
    async def test_output_list(self, monkeypatch):
        monkeypatch.setattr(music_gen, "REPLICATE_API_TOKEN", "tok")
        mock_resp = AsyncMock(status=200)
        mock_resp.json = AsyncMock(return_value={"status": "succeeded", "output": ["https://a.mp3"]})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_dl = AsyncMock(status=200)
        mock_dl.read = AsyncMock(return_value=b"data")
        mock_dl.__aenter__ = AsyncMock(return_value=mock_dl)
        mock_dl.__aexit__ = AsyncMock(return_value=False)
        mock_sess = AsyncMock()
        mock_sess.post = MagicMock(return_value=mock_resp)
        mock_sess.get = MagicMock(return_value=mock_dl)
        mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_sess.__aexit__ = AsyncMock(return_value=False)
        with patch("aiohttp.ClientSession", return_value=mock_sess), patch("aiohttp.ClientTimeout"):
            r = await music_gen.generate("jazz")
        assert r.error is None

    @pytest.mark.asyncio
    async def test_download_fails(self, monkeypatch):
        monkeypatch.setattr(music_gen, "REPLICATE_API_TOKEN", "tok")
        mock_resp = AsyncMock(status=200)
        mock_resp.json = AsyncMock(return_value={"status": "succeeded", "output": "https://a.mp3"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_dl = AsyncMock(status=404)
        mock_dl.__aenter__ = AsyncMock(return_value=mock_dl)
        mock_dl.__aexit__ = AsyncMock(return_value=False)
        mock_sess = AsyncMock()
        mock_sess.post = MagicMock(return_value=mock_resp)
        mock_sess.get = MagicMock(return_value=mock_dl)
        mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_sess.__aexit__ = AsyncMock(return_value=False)
        with patch("aiohttp.ClientSession", return_value=mock_sess), patch("aiohttp.ClientTimeout"):
            r = await music_gen.generate("jazz")
        assert "Download" in r.error or "404" in r.error

    @pytest.mark.asyncio
    async def test_empty_audio_url(self, monkeypatch):
        monkeypatch.setattr(music_gen, "REPLICATE_API_TOKEN", "tok")
        mock_resp = AsyncMock(status=200)
        mock_resp.json = AsyncMock(return_value={"status": "succeeded", "output": {"not": "str"}})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_sess = AsyncMock()
        mock_sess.post = MagicMock(return_value=mock_resp)
        mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_sess.__aexit__ = AsyncMock(return_value=False)
        with patch("aiohttp.ClientSession", return_value=mock_sess), patch("aiohttp.ClientTimeout"):
            r = await music_gen.generate("jazz")
        assert "No audio URL" in r.error


# ═══════════════════════════════════════════════════════════════════════════════
# video_gen.py (same pattern as music_gen)
# ═══════════════════════════════════════════════════════════════════════════════
from telechat_pkg import video_gen


class TestVideoGen:
    def test_available(self, monkeypatch):
        monkeypatch.setattr(video_gen, "VIDEO_GEN_ENABLED", True)
        monkeypatch.setattr(video_gen, "REPLICATE_API_TOKEN", "tok")
        assert video_gen.is_available() is True

    def test_not_available(self, monkeypatch):
        monkeypatch.setattr(video_gen, "VIDEO_GEN_ENABLED", False)
        assert video_gen.is_available() is False

    @pytest.mark.asyncio
    async def test_no_token(self, monkeypatch):
        monkeypatch.setattr(video_gen, "REPLICATE_API_TOKEN", "")
        r = await video_gen.generate("cat")
        assert "REPLICATE_API_TOKEN" in r.error

    @pytest.mark.asyncio
    async def test_import_error(self, monkeypatch):
        monkeypatch.setattr(video_gen, "REPLICATE_API_TOKEN", "tok")
        real_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__
        def fail(name, *a, **kw):
            if name == "aiohttp":
                raise ImportError("no")
            return real_import(name, *a, **kw)
        with patch("builtins.__import__", side_effect=fail):
            r = await video_gen.generate("cat")
        assert "aiohttp" in r.error

    @pytest.mark.asyncio
    async def test_api_error(self, monkeypatch):
        monkeypatch.setattr(video_gen, "REPLICATE_API_TOKEN", "tok")
        mock_resp = AsyncMock(status=500)
        mock_resp.text = AsyncMock(return_value="fail")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_sess = AsyncMock()
        mock_sess.post = MagicMock(return_value=mock_resp)
        mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_sess.__aexit__ = AsyncMock(return_value=False)
        with patch("aiohttp.ClientSession", return_value=mock_sess), patch("aiohttp.ClientTimeout"):
            r = await video_gen.generate("cat")
        assert "500" in r.error

    @pytest.mark.asyncio
    async def test_success(self, monkeypatch):
        monkeypatch.setattr(video_gen, "REPLICATE_API_TOKEN", "tok")
        mock_resp = AsyncMock(status=201)
        mock_resp.json = AsyncMock(return_value={"status": "succeeded", "output": "https://v.mp4"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_dl = AsyncMock(status=200)
        mock_dl.read = AsyncMock(return_value=b"video")
        mock_dl.__aenter__ = AsyncMock(return_value=mock_dl)
        mock_dl.__aexit__ = AsyncMock(return_value=False)
        mock_sess = AsyncMock()
        mock_sess.post = MagicMock(return_value=mock_resp)
        mock_sess.get = MagicMock(return_value=mock_dl)
        mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_sess.__aexit__ = AsyncMock(return_value=False)
        with patch("aiohttp.ClientSession", return_value=mock_sess), patch("aiohttp.ClientTimeout"):
            r = await video_gen.generate("cat")
        assert r.error is None
        assert r.video_path

    @pytest.mark.asyncio
    async def test_failed(self, monkeypatch):
        monkeypatch.setattr(video_gen, "REPLICATE_API_TOKEN", "tok")
        mock_resp = AsyncMock(status=200)
        mock_resp.json = AsyncMock(return_value={"status": "failed", "error": "bad"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_sess = AsyncMock()
        mock_sess.post = MagicMock(return_value=mock_resp)
        mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_sess.__aexit__ = AsyncMock(return_value=False)
        with patch("aiohttp.ClientSession", return_value=mock_sess), patch("aiohttp.ClientTimeout"):
            r = await video_gen.generate("cat")
        assert "bad" in r.error

    @pytest.mark.asyncio
    async def test_no_video_url(self, monkeypatch):
        monkeypatch.setattr(video_gen, "REPLICATE_API_TOKEN", "tok")
        mock_resp = AsyncMock(status=200)
        mock_resp.json = AsyncMock(return_value={"status": "succeeded", "output": None})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_sess = AsyncMock()
        mock_sess.post = MagicMock(return_value=mock_resp)
        mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_sess.__aexit__ = AsyncMock(return_value=False)
        with patch("aiohttp.ClientSession", return_value=mock_sess), patch("aiohttp.ClientTimeout"):
            r = await video_gen.generate("cat")
        assert "No video URL" in r.error

    @pytest.mark.asyncio
    async def test_timeout(self, monkeypatch):
        monkeypatch.setattr(video_gen, "REPLICATE_API_TOKEN", "tok")
        mock_sess = AsyncMock()
        mock_sess.post = MagicMock(side_effect=asyncio.TimeoutError())
        mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_sess.__aexit__ = AsyncMock(return_value=False)
        with patch("aiohttp.ClientSession", return_value=mock_sess), patch("aiohttp.ClientTimeout"):
            r = await video_gen.generate("cat")
        assert "timed out" in r.error.lower()

    @pytest.mark.asyncio
    async def test_download_fail(self, monkeypatch):
        monkeypatch.setattr(video_gen, "REPLICATE_API_TOKEN", "tok")
        mock_resp = AsyncMock(status=200)
        mock_resp.json = AsyncMock(return_value={"status": "succeeded", "output": "https://v.mp4"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_dl = AsyncMock(status=500)
        mock_dl.__aenter__ = AsyncMock(return_value=mock_dl)
        mock_dl.__aexit__ = AsyncMock(return_value=False)
        mock_sess = AsyncMock()
        mock_sess.post = MagicMock(return_value=mock_resp)
        mock_sess.get = MagicMock(return_value=mock_dl)
        mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_sess.__aexit__ = AsyncMock(return_value=False)
        with patch("aiohttp.ClientSession", return_value=mock_sess), patch("aiohttp.ClientTimeout"):
            r = await video_gen.generate("cat")
        assert "Download" in r.error or "500" in r.error


# ═══════════════════════════════════════════════════════════════════════════════
# voice_transcription.py
# ═══════════════════════════════════════════════════════════════════════════════
from telechat_pkg import voice_transcription as vt


class TestVoiceTranscription:
    def test_available(self, monkeypatch):
        monkeypatch.setattr(vt, "TRANSCRIPTION_ENABLED", True)
        monkeypatch.setattr(vt, "OPENAI_API_KEY", "k")
        assert vt.is_available() is True

    def test_not_available(self, monkeypatch):
        monkeypatch.setattr(vt, "TRANSCRIPTION_ENABLED", False)
        assert vt.is_available() is False

    @pytest.mark.asyncio
    async def test_no_api_key(self, monkeypatch):
        monkeypatch.setattr(vt, "OPENAI_API_KEY", "")
        r = await vt.transcribe("/tmp/test.ogg")
        assert "OPENAI_API_KEY" in r.error

    @pytest.mark.asyncio
    async def test_file_too_large(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vt, "OPENAI_API_KEY", "key")
        monkeypatch.setattr(vt, "MAX_AUDIO_SIZE", 10)
        f = tmp_path / "big.ogg"
        f.write_bytes(b"x" * 100)
        r = await vt.transcribe(str(f))
        assert "too large" in r.error

    @pytest.mark.asyncio
    async def test_import_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vt, "OPENAI_API_KEY", "key")
        monkeypatch.setattr(vt, "MAX_AUDIO_SIZE", 1000)
        f = tmp_path / "audio.ogg"
        f.write_bytes(b"data")
        real_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__
        def fail(name, *a, **kw):
            if name == "aiohttp":
                raise ImportError("no")
            return real_import(name, *a, **kw)
        with patch("builtins.__import__", side_effect=fail):
            r = await vt.transcribe(str(f))
        assert "aiohttp" in r.error

    @pytest.mark.asyncio
    async def test_success(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vt, "OPENAI_API_KEY", "key")
        monkeypatch.setattr(vt, "MAX_AUDIO_SIZE", 10000)
        f = tmp_path / "audio.ogg"
        f.write_bytes(b"audiodata")
        mock_resp = AsyncMock(status=200)
        mock_resp.json = AsyncMock(return_value={"text": "hello world", "language": "en", "duration": 5.0})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_sess = AsyncMock()
        mock_sess.post = MagicMock(return_value=mock_resp)
        mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_sess.__aexit__ = AsyncMock(return_value=False)
        with patch("aiohttp.ClientSession", return_value=mock_sess), \
             patch("aiohttp.ClientTimeout"), \
             patch("aiohttp.FormData"):
            r = await vt.transcribe(str(f))
        assert r.text == "hello world"

    @pytest.mark.asyncio
    async def test_http_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vt, "OPENAI_API_KEY", "key")
        monkeypatch.setattr(vt, "MAX_AUDIO_SIZE", 10000)
        f = tmp_path / "audio.ogg"
        f.write_bytes(b"data")
        mock_resp = AsyncMock(status=401)
        mock_resp.text = AsyncMock(return_value="Unauthorized")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_sess = AsyncMock()
        mock_sess.post = MagicMock(return_value=mock_resp)
        mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_sess.__aexit__ = AsyncMock(return_value=False)
        with patch("aiohttp.ClientSession", return_value=mock_sess), \
             patch("aiohttp.ClientTimeout"), \
             patch("aiohttp.FormData"):
            r = await vt.transcribe(str(f))
        assert "401" in r.error

    @pytest.mark.asyncio
    async def test_timeout(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vt, "OPENAI_API_KEY", "key")
        monkeypatch.setattr(vt, "MAX_AUDIO_SIZE", 10000)
        f = tmp_path / "audio.ogg"
        f.write_bytes(b"data")
        mock_sess = AsyncMock()
        mock_sess.post = MagicMock(side_effect=asyncio.TimeoutError())
        mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_sess.__aexit__ = AsyncMock(return_value=False)
        with patch("aiohttp.ClientSession", return_value=mock_sess), \
             patch("aiohttp.ClientTimeout"), \
             patch("aiohttp.FormData"):
            r = await vt.transcribe(str(f))
        assert "timed out" in r.error.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# web_fetch.py
# ═══════════════════════════════════════════════════════════════════════════════
from telechat_pkg import web_fetch


class TestWebFetch:
    def test_available(self, monkeypatch):
        monkeypatch.setattr(web_fetch, "WEB_FETCH_ENABLED", True)
        assert web_fetch.is_available() is True

    def test_not_available(self, monkeypatch):
        monkeypatch.setattr(web_fetch, "WEB_FETCH_ENABLED", False)
        assert web_fetch.is_available() is False

    @pytest.mark.asyncio
    async def test_jina_success(self, monkeypatch):
        monkeypatch.setattr(web_fetch, "JINA_API_KEY", "key")
        monkeypatch.setattr(web_fetch, "MAX_CONTENT_LENGTH", 10000)
        mock_resp = AsyncMock(status=200)
        mock_resp.text = AsyncMock(return_value="Page Title\nActual content here")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_sess = MagicMock()
        mock_sess.get = MagicMock(return_value=mock_resp)
        with patch("telechat_pkg.web_fetch._get_session", return_value=mock_sess), patch("aiohttp.ClientTimeout"):
            r = await web_fetch.fetch_readable("https://example.com")
        assert r.title == "Page Title"
        assert r.content == "Actual content here"

    @pytest.mark.asyncio
    async def test_jina_error(self, monkeypatch):
        monkeypatch.setattr(web_fetch, "JINA_API_KEY", "key")
        mock_resp = AsyncMock(status=429)
        mock_resp.text = AsyncMock(return_value="Rate limited")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_sess = MagicMock()
        mock_sess.get = MagicMock(return_value=mock_resp)
        with patch("telechat_pkg.web_fetch._get_session", return_value=mock_sess), patch("aiohttp.ClientTimeout"):
            r = await web_fetch.fetch_readable("https://example.com")
        assert "429" in r.error

    @pytest.mark.asyncio
    async def test_jina_timeout(self, monkeypatch):
        monkeypatch.setattr(web_fetch, "JINA_API_KEY", "key")
        mock_sess = MagicMock()
        mock_sess.get = MagicMock(side_effect=asyncio.TimeoutError())
        with patch("telechat_pkg.web_fetch._get_session", return_value=mock_sess), patch("aiohttp.ClientTimeout"):
            r = await web_fetch.fetch_readable("https://example.com")
        assert "timed out" in r.error.lower()

    @pytest.mark.asyncio
    async def test_jina_truncation(self, monkeypatch):
        monkeypatch.setattr(web_fetch, "JINA_API_KEY", "key")
        monkeypatch.setattr(web_fetch, "MAX_CONTENT_LENGTH", 20)
        mock_resp = AsyncMock(status=200)
        mock_resp.text = AsyncMock(return_value="T\n" + "A" * 100)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_sess = MagicMock()
        mock_sess.get = MagicMock(return_value=mock_resp)
        with patch("telechat_pkg.web_fetch._get_session", return_value=mock_sess), patch("aiohttp.ClientTimeout"):
            r = await web_fetch.fetch_readable("https://example.com")
        assert "truncated" in r.content.lower()

    @pytest.mark.asyncio
    async def test_jina_generic_exception(self, monkeypatch):
        monkeypatch.setattr(web_fetch, "JINA_API_KEY", "key")
        mock_sess = MagicMock()
        mock_sess.get = MagicMock(side_effect=RuntimeError("fail"))
        with patch("telechat_pkg.web_fetch._get_session", return_value=mock_sess), patch("aiohttp.ClientTimeout"):
            r = await web_fetch._fetch_jina("https://example.com")
        assert "fail" in r.error

    @pytest.mark.asyncio
    async def test_raw_success(self, monkeypatch):
        monkeypatch.setattr(web_fetch, "JINA_API_KEY", "")
        monkeypatch.setattr(web_fetch, "MAX_CONTENT_LENGTH", 10000)
        html = "<html><title>My Page</title><body><p>Hello world</p></body></html>"
        mock_resp = AsyncMock(status=200)
        mock_resp.text = AsyncMock(return_value=html)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_sess = MagicMock()
        mock_sess.get = MagicMock(return_value=mock_resp)
        with patch("telechat_pkg.web_fetch._get_session", return_value=mock_sess), patch("aiohttp.ClientTimeout"):
            r = await web_fetch.fetch_readable("https://example.com")
        assert r.title == "My Page"
        assert "Hello world" in r.content

    @pytest.mark.asyncio
    async def test_raw_http_error(self, monkeypatch):
        monkeypatch.setattr(web_fetch, "JINA_API_KEY", "")
        mock_resp = AsyncMock(status=404)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_sess = MagicMock()
        mock_sess.get = MagicMock(return_value=mock_resp)
        with patch("telechat_pkg.web_fetch._get_session", return_value=mock_sess), patch("aiohttp.ClientTimeout"):
            r = await web_fetch.fetch_readable("https://example.com")
        assert "404" in r.error

    @pytest.mark.asyncio
    async def test_raw_generic_exception(self, monkeypatch):
        monkeypatch.setattr(web_fetch, "JINA_API_KEY", "")
        mock_sess = MagicMock()
        mock_sess.get = MagicMock(side_effect=RuntimeError("raw fail"))
        with patch("telechat_pkg.web_fetch._get_session", return_value=mock_sess), patch("aiohttp.ClientTimeout"):
            r = await web_fetch._fetch_raw("https://example.com")
        assert "raw fail" in r.error

    @pytest.mark.asyncio
    async def test_raw_timeout(self, monkeypatch):
        monkeypatch.setattr(web_fetch, "JINA_API_KEY", "")
        mock_sess = MagicMock()
        mock_sess.get = MagicMock(side_effect=asyncio.TimeoutError())
        with patch("telechat_pkg.web_fetch._get_session", return_value=mock_sess), patch("aiohttp.ClientTimeout"):
            r = await web_fetch._fetch_raw("https://example.com")
        assert "timed out" in r.error.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# resource_limiter.py
# ═══════════════════════════════════════════════════════════════════════════════
from telechat_pkg.resource_limiter import ResourceLimiter, ResourceLimits, ResourceUsage, TEMPLATES, format_usage


class TestResourceLimiter:
    def test_defaults(self):
        rl = ResourceLimiter()
        assert rl.limits.cpu_seconds > 0

    def test_from_template(self):
        rl = ResourceLimiter.from_template("strict")
        assert rl.limits.cpu_seconds == 60

    def test_from_template_unknown(self):
        with pytest.raises(ValueError):
            ResourceLimiter.from_template("nonexistent")

    def test_preexec_fn_not_linux(self):
        rl = ResourceLimiter()
        rl._is_linux = False
        assert rl._get_preexec_fn() is None

    @pytest.mark.asyncio
    async def test_execute_success(self):
        rl = ResourceLimiter()
        rl._is_linux = False
        rc, stdout, stderr, usage = await rl.execute("echo hello", limits=ResourceLimits(wall_time_seconds=10))
        assert rc == 0
        assert "hello" in stdout

    @pytest.mark.asyncio
    async def test_execute_timeout(self):
        rl = ResourceLimiter()
        rl._is_linux = False
        rc, stdout, stderr, usage = await rl.execute("sleep 60", limits=ResourceLimits(wall_time_seconds=1))
        assert "wall_time" in usage.limits_hit or "Wall-time" in stderr

    @pytest.mark.asyncio
    async def test_execute_error(self):
        rl = ResourceLimiter()
        rl._is_linux = False
        with patch("asyncio.create_subprocess_exec", side_effect=RuntimeError("bad")):
            rc, stdout, stderr, usage = await rl.execute("echo hi")
        assert rc == 1
        assert "bad" in stderr


class TestFormatUsage:
    def test_basic(self):
        u = ResourceUsage(wall_time_seconds=5.0)
        assert "5.0s" in format_usage(u)

    def test_with_cpu_and_memory(self):
        u = ResourceUsage(wall_time_seconds=5.0, cpu_time_seconds=2.0, memory_peak_bytes=1024*1024*100)
        out = format_usage(u)
        assert "CPU" in out
        assert "Mem" in out

    def test_with_limits_hit(self):
        u = ResourceUsage(limits_hit=["cpu", "memory"])
        assert "limits hit" in format_usage(u)


class TestTemplates:
    def test_all_templates_exist(self):
        for name in ("strict", "standard", "relaxed", "test"):
            assert name in TEMPLATES


# ═══════════════════════════════════════════════════════════════════════════════
# scheduled_tasks.py
# ═══════════════════════════════════════════════════════════════════════════════
from telechat_pkg.scheduled_tasks import ScheduledTask, Scheduler


class TestScheduledTask:
    def test_is_due(self):
        t = ScheduledTask(id="1", name="test", interval_seconds=60, callback_name="cb")
        t.last_run = time.time() - 120
        assert t.is_due is True

    def test_not_due(self):
        t = ScheduledTask(id="1", name="test", interval_seconds=60, callback_name="cb")
        t.last_run = time.time()
        assert t.is_due is False

    def test_next_run(self):
        t = ScheduledTask(id="1", name="test", interval_seconds=60, callback_name="cb")
        t.last_run = 1000
        assert t.next_run == 1060

    def test_to_dict_and_from_dict(self):
        t = ScheduledTask(id="1", name="test", interval_seconds=60, callback_name="cb",
                         platform="tg", user_id="42", extra={"key": "val"})
        d = t.to_dict()
        t2 = ScheduledTask.from_dict(d)
        assert t2.id == "1"
        assert t2.platform == "tg"
        assert t2.extra == {"key": "val"}


class TestScheduler:
    def test_add_and_list(self, tmp_path):
        s = Scheduler(tasks_file=str(tmp_path / "tasks.json"))
        t = ScheduledTask(id="1", name="test", interval_seconds=60, callback_name="cb")
        s.add_task(t)
        assert len(s.list_tasks()) == 1

    def test_remove_task(self, tmp_path):
        s = Scheduler(tasks_file=str(tmp_path / "tasks.json"))
        t = ScheduledTask(id="1", name="test", interval_seconds=60, callback_name="cb")
        s.add_task(t)
        assert s.remove_task("1") is True
        assert s.remove_task("1") is False
        assert len(s.list_tasks()) == 0

    def test_get_task(self, tmp_path):
        s = Scheduler(tasks_file=str(tmp_path / "tasks.json"))
        t = ScheduledTask(id="1", name="test", interval_seconds=60, callback_name="cb")
        s.add_task(t)
        assert s.get_task("1") is not None
        assert s.get_task("2") is None

    def test_list_user_tasks(self, tmp_path):
        s = Scheduler(tasks_file=str(tmp_path / "tasks.json"))
        s.add_task(ScheduledTask(id="1", name="t1", interval_seconds=60, callback_name="cb",
                                 platform="tg", user_id="42"))
        s.add_task(ScheduledTask(id="2", name="t2", interval_seconds=60, callback_name="cb",
                                 platform="tg", user_id="99"))
        assert len(s.list_user_tasks("tg", "42")) == 1

    def test_register_callback(self):
        s = Scheduler()
        async def cb(task): return True
        s.register_callback("cb", cb)
        assert "cb" in s._callbacks

    def test_save_and_load(self, tmp_path):
        f = str(tmp_path / "tasks.json")
        s = Scheduler(tasks_file=f)
        s.add_task(ScheduledTask(id="1", name="test", interval_seconds=60, callback_name="cb"))
        s._save()

        s2 = Scheduler(tasks_file=f)
        s2._load()
        assert len(s2.list_tasks()) == 1

    def test_load_missing_file(self, tmp_path):
        s = Scheduler(tasks_file=str(tmp_path / "nonexistent.json"))
        s._load()  # should not raise
        assert len(s.list_tasks()) == 0

    def test_save_no_file(self):
        s = Scheduler(tasks_file="")
        s._save()  # should not raise

    def test_load_no_file(self):
        s = Scheduler(tasks_file="")
        s._load()  # should not raise

    def test_stop(self):
        s = Scheduler()
        s._running = True
        mock_task = MagicMock()
        s._loop_task = mock_task
        s.stop()
        assert s._running is False
        mock_task.cancel.assert_called_once()
        assert s._loop_task is None

    @pytest.mark.asyncio
    async def test_run_loop_executes_due_task(self, tmp_path):
        s = Scheduler(tasks_file=str(tmp_path / "tasks.json"))
        results = []
        async def cb(task):
            results.append(task.id)
            return True
        s.register_callback("cb", cb)
        t = ScheduledTask(id="1", name="test", interval_seconds=1, callback_name="cb")
        t.last_run = 0
        s.add_task(t)
        s._running = True

        call_count = [0]
        orig_sleep = asyncio.sleep
        async def fake_sleep(d):
            call_count[0] += 1
            if call_count[0] >= 2:
                s._running = False

        with patch("asyncio.sleep", side_effect=fake_sleep):
            await s._run_loop()

        assert "1" in results

    @pytest.mark.asyncio
    async def test_run_loop_handles_missing_callback(self, tmp_path):
        s = Scheduler(tasks_file=str(tmp_path / "tasks.json"))
        t = ScheduledTask(id="1", name="test", interval_seconds=1, callback_name="missing")
        t.last_run = 0
        s.add_task(t)
        s._running = True

        call_count = [0]
        async def fake_sleep(d):
            call_count[0] += 1
            if call_count[0] >= 2:
                s._running = False
        with patch("asyncio.sleep", side_effect=fake_sleep):
            await s._run_loop()
        # No crash

    @pytest.mark.asyncio
    async def test_run_loop_handles_callback_failure(self, tmp_path):
        s = Scheduler(tasks_file=str(tmp_path / "tasks.json"))
        async def bad_cb(task):
            raise RuntimeError("fail")
        s.register_callback("cb", bad_cb)
        t = ScheduledTask(id="1", name="test", interval_seconds=1, callback_name="cb")
        t.last_run = 0
        s.add_task(t)
        s._running = True

        call_count = [0]
        async def fake_sleep(d):
            call_count[0] += 1
            if call_count[0] >= 2:
                s._running = False
        with patch("asyncio.sleep", side_effect=fake_sleep):
            await s._run_loop()
        assert t.run_count >= 1

    @pytest.mark.asyncio
    async def test_run_loop_callback_returns_false(self, tmp_path):
        s = Scheduler(tasks_file=str(tmp_path / "tasks.json"))
        async def fail_cb(task):
            return False
        s.register_callback("cb", fail_cb)
        t = ScheduledTask(id="1", name="test", interval_seconds=1, callback_name="cb")
        t.last_run = 0
        s.add_task(t)
        s._running = True

        call_count = [0]
        async def fake_sleep(d):
            call_count[0] += 1
            if call_count[0] >= 2:
                s._running = False
        with patch("asyncio.sleep", side_effect=fake_sleep):
            await s._run_loop()
        assert t.run_count >= 1

    def test_start_already_running(self, tmp_path):
        s = Scheduler(tasks_file=str(tmp_path / "tasks.json"))
        s._running = True
        s.start()  # should not create a new task
