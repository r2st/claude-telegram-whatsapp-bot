"""
Behavior tests for smart model routing (telechat_pkg.smart_router).

Covers classify_complexity across the simple/moderate/complex decision tree,
route_model name mapping, and route_model_api identifier resolution.

Run:
    pytest tests/test_smart_router.py -v
"""

import os

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")

from telechat_pkg import smart_router as sr
from telechat_pkg.smart_router import classify_complexity, route_model, route_model_api


# ══════════════════════════════════════════════════════════════════════════════
# 1. classify_complexity — simple
# ══════════════════════════════════════════════════════════════════════════════


class TestSimple:
    def test_very_short_is_simple(self):
        assert classify_complexity("What time is it?") == "simple"

    def test_single_word(self):
        assert classify_complexity("hello") == "simple"

    def test_five_words_is_simple(self):
        assert classify_complexity("one two three four five") == "simple"

    def test_greeting_pattern(self):
        # >5 words so it bypasses the length shortcut, but matches a simple pattern
        assert classify_complexity("hello there how are you doing today") == "simple"

    def test_what_is_factual(self):
        assert classify_complexity("what is the capital of France today please") == "simple"

    def test_translate_pattern(self):
        assert classify_complexity("please translate this sentence into Spanish for me now") == "simple"

    def test_medium_length_no_signals_is_simple(self):
        # >5 words, no complex/simple patterns, under HAIKU_MAX → falls to simple
        text = "the quick brown fox jumped over lazy dog again"
        assert classify_complexity(text) == "simple"


# ══════════════════════════════════════════════════════════════════════════════
# 2. classify_complexity — moderate
# ══════════════════════════════════════════════════════════════════════════════


class TestModerate:
    def test_single_complex_pattern_is_moderate(self):
        text = "please refactor the user login function to be cleaner"
        assert classify_complexity(text) == "moderate"

    def test_long_query_without_signals_is_moderate(self):
        # > HAIKU_MAX_TOKENS (50) words, no opus/complex triggers
        text = " ".join(["word"] * 60)
        assert classify_complexity(text) == "moderate"

    def test_two_complex_patterns_still_moderate(self):
        # 2 complex matches (< 3) → moderate
        text = "build a system that stores data reliably for users"
        assert classify_complexity(text) == "moderate"


# ══════════════════════════════════════════════════════════════════════════════
# 3. classify_complexity — complex
# ══════════════════════════════════════════════════════════════════════════════


class TestComplex:
    def test_three_complex_patterns_is_complex(self):
        text = "design and implement a distributed system framework with security audit"
        assert classify_complexity(text) == "complex"

    def test_two_opus_patterns_is_complex(self):
        text = "write a research paper with formal verification and a mathematical proof of correctness"
        assert classify_complexity(text) == "complex"

    def test_one_opus_pattern_with_long_text_is_complex(self):
        # one opus pattern AND word_count > OPUS_MIN_TOKENS (200)
        text = "reasoning " + " ".join(["filler"] * 250)
        assert classify_complexity(text) == "complex"

    def test_one_opus_pattern_short_not_complex(self):
        # one opus pattern but short → not complex via opus path
        text = "let us do some reasoning about this short prompt here"
        assert classify_complexity(text) != "complex"


# ══════════════════════════════════════════════════════════════════════════════
# 4. route_model
# ══════════════════════════════════════════════════════════════════════════════


class TestRouteModel:
    def test_simple_routes_to_haiku(self):
        assert route_model("hi there") == "haiku"

    def test_moderate_routes_to_sonnet(self):
        assert route_model("please refactor the login handler for clarity now") == "sonnet"

    def test_complex_routes_to_opus(self):
        text = "design and implement a distributed system framework with security audit"
        assert route_model(text) == "opus"


# ══════════════════════════════════════════════════════════════════════════════
# 5. route_model_api
# ══════════════════════════════════════════════════════════════════════════════


class TestRouteModelApi:
    def test_simple_maps_to_haiku_api(self):
        assert "haiku" in route_model_api("hi")

    def test_complex_maps_to_opus_api(self):
        text = "design and implement a distributed system framework with security audit"
        assert "opus" in route_model_api(text)

    def test_moderate_maps_to_sonnet_api(self):
        assert "sonnet" in route_model_api("please refactor the login handler for clarity now")

    def test_env_override_for_api_id(self, monkeypatch):
        monkeypatch.setenv("SMART_ROUTE_HAIKU_API", "custom-haiku-id")
        # route_model_api reads env at call time
        assert route_model_api("hi") == "custom-haiku-id"
