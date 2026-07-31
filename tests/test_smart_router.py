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


# ══════════════════════════════════════════════════════════════════════════════
# 6. Complexity signals outrank length and incidental keywords
# ══════════════════════════════════════════════════════════════════════════════


class TestComplexityWinsOverLengthAndKeywords:
    """A misroute is silent: you get a worse answer, never a reason.

    Two rules used to run before any complexity check — a "five words or fewer
    is simple" shortcut and a simple-keyword rule — so a short refactor request,
    or a long one that happened to contain the word "convert", went to Haiku.
    """

    @pytest.mark.parametrize("query", [
        "Refactor this codebase",
        "Debug this crash",
        "Architect the ingest pipeline",
        "Optimize this query",
    ])
    def test_a_short_request_with_a_complexity_signal_is_not_simple(self, query):
        assert len(query.split()) <= 5, "this case is meant to test the length shortcut"
        assert classify_complexity(query) != "simple"
        assert route_model(query) != "haiku"

    @pytest.mark.parametrize("query", [
        "Refactor the payment pipeline and convert it to async",
        "Debug this crash and translate the stack trace for me",
        "Analyze the auth module and list the security issues you find",
    ])
    def test_an_incidental_simple_keyword_does_not_win(self, query):
        # Each of these matches a _SIMPLE_PATTERNS keyword (convert / translate
        # / list) *and* a complexity pattern. The complexity signal wins.
        assert route_model(query) != "haiku", query

    @pytest.mark.parametrize("query", [
        "hi",
        "thanks!",
        "What time is it?",
        "What is the capital of France?",
        "translate this to french: bonjour",
    ])
    def test_genuinely_simple_queries_still_go_to_haiku(self, query):
        assert route_model(query) == "haiku", query

    def test_the_module_docstring_examples_are_true(self):
        """The docstring promised sonnet for a query that routed to haiku."""
        assert route_model("What time is it?") == "haiku"
        assert route_model("Refactor this codebase") == "sonnet"
        assert route_model(
            "Write a full comprehensive security audit of "
            "this distributed pipeline architecture"
        ) == "opus"

    def test_opus_still_needs_real_evidence(self):
        """Opus costs several times Sonnet, so one keyword must not reach it."""
        assert route_model("design the schema") != "opus"
        assert route_model("please refactor the login handler for clarity now") != "opus"

    def test_a_long_query_with_no_signal_is_moderate(self):
        assert classify_complexity(" ".join(["word"] * 80)) == "moderate"

    def test_empty_and_whitespace_are_handled(self):
        for text in ("", "   ", "\n\n"):
            assert classify_complexity(text) == "simple"
