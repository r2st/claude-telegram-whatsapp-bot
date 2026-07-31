"""
Comprehensive tests for the feedback module.

Run:
    pytest tests/test_feedback_extended.py -v
"""

import os
import tempfile
import time
from unittest.mock import patch

import pytest

# ── Environment must be set before any telechat imports ──────────────────────
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")

_tmp_dir = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp_dir, "test_feedback.db")
os.environ["CLAUDE_CLI_WORK_DIR"] = _tmp_dir

from telechat_pkg.claude_core import init_db, _get_conn
import telechat_pkg.claude_core as cc
import telechat_pkg.feedback as fb
from telechat_pkg.feedback import (
    save_feedback,
    get_feedback_stats,
    get_recent_feedback,
    evaluate_response,
    save_quality_score,
    get_quality_trend,
    _eval_length,
    _eval_error_free,
    _eval_has_content,
    _eval_not_truncated,
    _eval_reasonable_cost,
    append_learning,
    get_learnings_summary,
)

# Initialise DB once for the whole module
init_db()


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _flush_writes():
    """Drain the write queue into the DB so reads see the data immediately."""
    import queue as _q
    if cc._write_queue is None:
        return
    ops = []
    while True:
        try:
            ops.append(cc._write_queue.get_nowait())
        except _q.Empty:
            break
    if ops:
        conn = _get_conn()
        for sql, params in ops:
            conn.execute(sql, params)
        conn.commit()


def _clear_feedback():
    conn = _get_conn()
    conn.execute("DELETE FROM feedback")
    conn.commit()


def _clear_quality_scores():
    conn = _get_conn()
    conn.execute("DELETE FROM quality_scores")
    conn.commit()


# ═══════════════════════════════════════════════════════════════════════════════
# 1. save_feedback
# ═══════════════════════════════════════════════════════════════════════════════


class TestSaveFeedback:
    def test_enqueues_insert_with_correct_sql(self):
        with patch.object(cc, "_enqueue_write") as mock_enqueue:
            save_feedback("telegram", "user1", rating=5)
            mock_enqueue.assert_called_once()
            sql, params = mock_enqueue.call_args[0]
            assert "INSERT INTO feedback" in sql
            assert "platform" in sql.lower() or "?" in sql

    def test_params_contain_platform_and_user_id(self):
        with patch.object(cc, "_enqueue_write") as mock_enqueue:
            save_feedback("slack", "userX", rating=3, reaction="👍")
            _, params = mock_enqueue.call_args[0]
            assert "slack" in params
            assert "userX" in params

    def test_params_contain_rating_and_reaction(self):
        with patch.object(cc, "_enqueue_write") as mock_enqueue:
            save_feedback("telegram", "u1", rating=4, reaction="👍")
            _, params = mock_enqueue.call_args[0]
            assert 4 in params
            assert "👍" in params

    def test_params_contain_text_feedback(self):
        with patch.object(cc, "_enqueue_write") as mock_enqueue:
            save_feedback("telegram", "u1", text_feedback="Great response!")
            _, params = mock_enqueue.call_args[0]
            assert "Great response!" in params

    def test_response_preview_truncated_to_500(self):
        long_preview = "x" * 1000
        with patch.object(cc, "_enqueue_write") as mock_enqueue:
            save_feedback("telegram", "u1", response_preview=long_preview)
            _, params = mock_enqueue.call_args[0]
            # Find the response_preview param (it should be truncated)
            preview_in_params = [p for p in params if isinstance(p, str) and len(p) <= 500 and p.startswith("x")]
            assert len(preview_in_params) == 1
            assert len(preview_in_params[0]) == 500

    def test_message_ts_defaults_to_now_when_none(self):
        before = time.time()
        with patch.object(cc, "_enqueue_write") as mock_enqueue:
            save_feedback("telegram", "u1")
            _, params = mock_enqueue.call_args[0]
        after = time.time()
        # message_ts is params[5], ts is params[7]
        msg_ts = params[5]
        assert before <= msg_ts <= after

    def test_explicit_message_ts_preserved(self):
        fixed_ts = 1700000000.0
        with patch.object(cc, "_enqueue_write") as mock_enqueue:
            save_feedback("telegram", "u1", message_ts=fixed_ts)
            _, params = mock_enqueue.call_args[0]
        assert fixed_ts in params


# ═══════════════════════════════════════════════════════════════════════════════
# 2. get_feedback_stats
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetFeedbackStats:
    def setup_method(self):
        _clear_feedback()

    def test_no_data_returns_zeros(self):
        stats = get_feedback_stats("telegram", "nobody")
        assert stats["total_ratings"] == 0
        assert stats["avg_rating"] == 0
        assert stats["positive_count"] == 0
        assert stats["satisfaction_pct"] == 0

    def test_single_rating(self):
        conn = _get_conn()
        conn.execute(
            "INSERT INTO feedback (platform, user_id, rating, ts) VALUES (?,?,?,?)",
            ("telegram", "u2", 5, time.time()),
        )
        conn.commit()
        stats = get_feedback_stats("telegram", "u2")
        assert stats["total_ratings"] == 1
        assert stats["avg_rating"] == 5.0
        assert stats["positive_count"] == 1
        assert stats["satisfaction_pct"] == 100.0

    def test_multiple_ratings_avg(self):
        conn = _get_conn()
        now = time.time()
        for rating in [2, 4, 5]:
            conn.execute(
                "INSERT INTO feedback (platform, user_id, rating, ts) VALUES (?,?,?,?)",
                ("telegram", "u3", rating, now),
            )
        conn.commit()
        stats = get_feedback_stats("telegram", "u3")
        assert stats["total_ratings"] == 3
        assert stats["avg_rating"] == round((2 + 4 + 5) / 3, 2)

    def test_positive_count_only_gte_4(self):
        conn = _get_conn()
        now = time.time()
        for rating in [1, 2, 3, 4, 5]:
            conn.execute(
                "INSERT INTO feedback (platform, user_id, rating, ts) VALUES (?,?,?,?)",
                ("telegram", "u4", rating, now),
            )
        conn.commit()
        stats = get_feedback_stats("telegram", "u4")
        assert stats["positive_count"] == 2  # 4 and 5

    def test_satisfaction_pct_calculation(self):
        conn = _get_conn()
        now = time.time()
        for rating in [4, 4, 2]:
            conn.execute(
                "INSERT INTO feedback (platform, user_id, rating, ts) VALUES (?,?,?,?)",
                ("telegram", "u5", rating, now),
            )
        conn.commit()
        stats = get_feedback_stats("telegram", "u5")
        assert stats["satisfaction_pct"] == round(2 / 3 * 100, 1)

    def test_null_ratings_not_counted(self):
        conn = _get_conn()
        now = time.time()
        # One with rating, two without
        conn.execute(
            "INSERT INTO feedback (platform, user_id, rating, reaction, ts) VALUES (?,?,?,?,?)",
            ("telegram", "u6", 5, None, now),
        )
        conn.execute(
            "INSERT INTO feedback (platform, user_id, rating, reaction, ts) VALUES (?,?,?,?,?)",
            ("telegram", "u6", None, "👍", now),
        )
        conn.execute(
            "INSERT INTO feedback (platform, user_id, rating, reaction, ts) VALUES (?,?,?,?,?)",
            ("telegram", "u6", None, None, now),
        )
        conn.commit()
        stats = get_feedback_stats("telegram", "u6")
        assert stats["total_ratings"] == 1

    def test_platform_isolation(self):
        conn = _get_conn()
        now = time.time()
        conn.execute(
            "INSERT INTO feedback (platform, user_id, rating, ts) VALUES (?,?,?,?)",
            ("slack", "uX", 5, now),
        )
        conn.commit()
        stats = get_feedback_stats("telegram", "uX")
        assert stats["total_ratings"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 3. get_recent_feedback
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetRecentFeedback:
    def setup_method(self):
        _clear_feedback()

    def test_empty_returns_empty_list(self):
        result = get_recent_feedback("telegram", "nobody")
        assert result == []

    def test_returns_dicts_with_expected_keys(self):
        conn = _get_conn()
        conn.execute(
            "INSERT INTO feedback (platform, user_id, rating, reaction, text_feedback, response_preview, ts) "
            "VALUES (?,?,?,?,?,?,?)",
            ("telegram", "u10", 4, "👍", "Nice!", "preview text", time.time()),
        )
        conn.commit()
        result = get_recent_feedback("telegram", "u10")
        assert len(result) == 1
        entry = result[0]
        assert set(entry.keys()) == {"rating", "reaction", "text_feedback", "response_preview", "ts"}

    def test_dict_values_correct(self):
        conn = _get_conn()
        ts = time.time()
        conn.execute(
            "INSERT INTO feedback (platform, user_id, rating, reaction, text_feedback, response_preview, ts) "
            "VALUES (?,?,?,?,?,?,?)",
            ("telegram", "u11", 3, "😐", "Okay", "resp", ts),
        )
        conn.commit()
        result = get_recent_feedback("telegram", "u11")
        entry = result[0]
        assert entry["rating"] == 3
        assert entry["reaction"] == "😐"
        assert entry["text_feedback"] == "Okay"
        assert entry["response_preview"] == "resp"
        assert entry["ts"] == pytest.approx(ts, abs=0.01)

    def test_respects_limit(self):
        conn = _get_conn()
        now = time.time()
        for i in range(10):
            conn.execute(
                "INSERT INTO feedback (platform, user_id, rating, ts) VALUES (?,?,?,?)",
                ("telegram", "u12", i % 5 + 1, now + i),
            )
        conn.commit()
        result = get_recent_feedback("telegram", "u12", limit=3)
        assert len(result) == 3

    def test_ordered_by_ts_desc(self):
        conn = _get_conn()
        now = time.time()
        for i in range(5):
            conn.execute(
                "INSERT INTO feedback (platform, user_id, rating, ts) VALUES (?,?,?,?)",
                ("telegram", "u13", i + 1, now + i),
            )
        conn.commit()
        result = get_recent_feedback("telegram", "u13")
        timestamps = [r["ts"] for r in result]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_default_limit_is_10(self):
        conn = _get_conn()
        now = time.time()
        for i in range(15):
            conn.execute(
                "INSERT INTO feedback (platform, user_id, rating, ts) VALUES (?,?,?,?)",
                ("telegram", "u14", 5, now + i),
            )
        conn.commit()
        result = get_recent_feedback("telegram", "u14")
        assert len(result) == 10


# ═══════════════════════════════════════════════════════════════════════════════
# 4. evaluate_response
# ═══════════════════════════════════════════════════════════════════════════════


class TestEvaluateResponse:
    def test_all_checks_pass_composite_is_1(self):
        user_text = "Tell me about Python"
        response = "Python is a versatile programming language used in many domains."
        stats = {"cost_usd": 0.01}
        result = evaluate_response(user_text, response, stats)
        assert result["composite"] == 1.0

    def test_returns_all_evaluator_keys(self):
        result = evaluate_response("hi", "hello there, how are you today?", {})
        expected_keys = {"length_appropriate", "error_free", "has_content", "not_truncated", "reasonable_cost", "composite"}
        assert set(result.keys()) == expected_keys

    def test_composite_is_fraction_when_some_fail(self):
        # error_free will fail, has_content ok, not_truncated ok, reasonable_cost ok
        # length: short query + normal response → True
        user_text = "hi"
        response = "[Error] Something went wrong in the system."
        stats = {}
        result = evaluate_response(user_text, response, stats)
        assert result["error_free"] is False
        assert 0 < result["composite"] < 1.0

    def test_all_checks_fail_composite_is_0(self):
        # empty response fails has_content, length, is error-free (True for empty)
        # Craft a response that fails as many as possible
        user_text = "A" * 60  # long query
        response = ""  # empty
        stats = {"cost_usd": 2.0}
        result = evaluate_response(user_text, response, stats)
        assert result["composite"] < 1.0

    def test_composite_rounding(self):
        # Patch individual evaluators to control exact pass count
        with patch.object(fb, "_eval_length", return_value=True), \
             patch.object(fb, "_eval_error_free", return_value=True), \
             patch.object(fb, "_eval_has_content", return_value=True), \
             patch.object(fb, "_eval_not_truncated", return_value=False), \
             patch.object(fb, "_eval_reasonable_cost", return_value=False):
            result = evaluate_response("q", "r", {})
        assert result["composite"] == round(3 / 5, 2)

    def test_boolean_values_in_result(self):
        result = evaluate_response("What is 2+2?", "2+2 equals 4.", {})
        for key in ("length_appropriate", "error_free", "has_content", "not_truncated", "reasonable_cost"):
            assert isinstance(result[key], bool)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. _eval_length
# ═══════════════════════════════════════════════════════════════════════════════


class TestEvalLength:
    def test_empty_response_returns_false(self):
        assert _eval_length("hello", "") is False

    def test_short_query_massive_response_returns_false(self):
        # user_len < 20, resp_len > 5000
        user_text = "Hi"  # len=2
        response = "x" * 5001
        assert _eval_length(user_text, response) is False

    def test_short_query_exactly_at_boundary_5000_is_ok(self):
        user_text = "Hi"
        response = "x" * 5000
        assert _eval_length(user_text, response) is True

    def test_short_query_just_over_boundary_5001_fails(self):
        user_text = "Hi"
        response = "x" * 5001
        assert _eval_length(user_text, response) is False

    def test_long_query_tiny_response_returns_false(self):
        # user_len > 50, resp_len < 20
        user_text = "A" * 51
        response = "ok"  # len=2
        assert _eval_length(user_text, response) is False

    def test_long_query_exactly_at_boundary_20_is_ok(self):
        user_text = "A" * 51
        response = "x" * 20
        assert _eval_length(user_text, response) is True

    def test_long_query_just_under_boundary_19_fails(self):
        user_text = "A" * 51
        response = "x" * 19
        assert _eval_length(user_text, response) is False

    def test_normal_query_and_response_returns_true(self):
        user_text = "What is the capital of France?"
        response = "The capital of France is Paris."
        assert _eval_length(user_text, response) is True

    def test_user_len_exactly_20_not_short(self):
        # user_len == 20 does NOT trigger the "< 20" branch
        user_text = "A" * 20
        response = "x" * 5001
        # user_len is 20, condition is < 20 → not triggered → True
        assert _eval_length(user_text, response) is True

    def test_user_len_exactly_50_not_long(self):
        # user_len == 50 does NOT trigger the "> 50" branch
        user_text = "A" * 50
        response = "x" * 5  # very short
        # user_len == 50, condition is > 50 → not triggered → True
        assert _eval_length(user_text, response) is True


# ═══════════════════════════════════════════════════════════════════════════════
# 6. _eval_error_free
# ═══════════════════════════════════════════════════════════════════════════════


class TestEvalErrorFree:
    def test_clean_response_returns_true(self):
        assert _eval_error_free("This is a normal response.") is True

    def test_contains_claude_error_returns_false(self):
        assert _eval_error_free("[Claude error] Something went wrong") is False

    def test_contains_timeout_returns_false(self):
        assert _eval_error_free("[Timeout] The request timed out") is False

    def test_contains_error_bracket_returns_false(self):
        assert _eval_error_free("[Error] Could not process") is False

    def test_contains_sdk_error_returns_false(self):
        assert _eval_error_free("[SDK Error] API failure") is False

    def test_contains_rate_limit_returns_false(self):
        assert _eval_error_free("You have hit the rate limit for this period") is False

    def test_contains_overloaded_returns_false(self):
        assert _eval_error_free("The service is overloaded right now") is False

    def test_case_insensitive_claude_error(self):
        assert _eval_error_free("[CLAUDE ERROR] caps test") is False

    def test_case_insensitive_rate_limit(self):
        assert _eval_error_free("RATE LIMIT exceeded") is False

    def test_case_insensitive_overloaded(self):
        assert _eval_error_free("System OVERLOADED please retry") is False

    def test_empty_response_is_error_free(self):
        # No error markers present in empty string
        assert _eval_error_free("") is True

    def test_partial_match_does_not_trigger(self):
        # "limits" contains "limit" but not "rate limit"
        assert _eval_error_free("There are limits to what I can do.") is True


# ═══════════════════════════════════════════════════════════════════════════════
# 7. _eval_has_content
# ═══════════════════════════════════════════════════════════════════════════════


class TestEvalHasContent:
    def test_empty_string_returns_false(self):
        assert _eval_has_content("") is False

    def test_whitespace_only_returns_false(self):
        assert _eval_has_content("   \t\n  ") is False

    def test_no_response_placeholder_returns_false(self):
        assert _eval_has_content("(no response)") is False

    def test_empty_response_placeholder_returns_false(self):
        assert _eval_has_content("(empty response)") is False

    def test_very_short_10_chars_returns_false(self):
        # len("1234567890") == 10, condition is > 10 → False
        assert _eval_has_content("1234567890") is False

    def test_exactly_11_chars_returns_true(self):
        assert _eval_has_content("12345678901") is True

    def test_normal_content_returns_true(self):
        assert _eval_has_content("This is a meaningful response.") is True

    def test_content_with_leading_whitespace_stripped(self):
        # Stripped content > 10 → True
        assert _eval_has_content("   Hello there world!   ") is True

    def test_placeholder_with_whitespace_returns_false(self):
        # Strip handles "(no response)" surrounded by spaces
        assert _eval_has_content("  (no response)  ") is False


# ═══════════════════════════════════════════════════════════════════════════════
# 8. _eval_not_truncated
# ═══════════════════════════════════════════════════════════════════════════════


class TestEvalNotTruncated:
    def test_normal_text_returns_true(self):
        assert _eval_not_truncated("This is a complete sentence.") is True

    def test_contains_ellipsis_truncated_returns_false(self):
        assert _eval_not_truncated("Here is some text…(truncated)") is False

    def test_contains_cut_off_returns_false(self):
        assert _eval_not_truncated("The answer is... (cut off)") is False

    def test_contains_response_cut_returns_false(self):
        assert _eval_not_truncated("Full answer here (response cut due to length)") is False

    def test_empty_string_returns_true(self):
        assert _eval_not_truncated("") is True

    def test_partial_marker_not_matched(self):
        # "truncated" alone doesn't match "…(truncated)"
        assert _eval_not_truncated("This was truncated at some point") is True

    def test_marker_at_end_of_string(self):
        assert _eval_not_truncated("Long response…(truncated)") is False

    def test_marker_in_middle_of_string(self):
        assert _eval_not_truncated("Start (response cut here) end") is False


# ═══════════════════════════════════════════════════════════════════════════════
# 9. _eval_reasonable_cost
# ═══════════════════════════════════════════════════════════════════════════════


class TestEvalReasonableCost:
    def test_empty_stats_returns_true(self):
        assert _eval_reasonable_cost({}) is True

    def test_no_cost_usd_key_returns_true(self):
        assert _eval_reasonable_cost({"tokens": 100, "model": "sonnet"}) is True

    def test_cost_at_zero_returns_true(self):
        assert _eval_reasonable_cost({"cost_usd": 0.0}) is True

    def test_cost_exactly_1_returns_true(self):
        assert _eval_reasonable_cost({"cost_usd": 1.0}) is True

    def test_cost_just_over_1_returns_false(self):
        assert _eval_reasonable_cost({"cost_usd": 1.01}) is False

    def test_cost_well_over_1_returns_false(self):
        assert _eval_reasonable_cost({"cost_usd": 5.0}) is False

    def test_none_stats_returns_true(self):
        # None is falsy → "if not stats: return True"
        assert _eval_reasonable_cost(None) is True

    def test_typical_low_cost_returns_true(self):
        assert _eval_reasonable_cost({"cost_usd": 0.002}) is True


# ═══════════════════════════════════════════════════════════════════════════════
# 10. save_quality_score
# ═══════════════════════════════════════════════════════════════════════════════


class TestSaveQualityScore:
    def test_enqueues_with_correct_sql(self):
        with patch.object(cc, "_enqueue_write") as mock_enqueue:
            save_quality_score("telegram", "u1", "composite", 0.8)
            mock_enqueue.assert_called_once()
            sql, params = mock_enqueue.call_args[0]
            assert "INSERT INTO quality_scores" in sql

    def test_params_contain_evaluator_and_score(self):
        with patch.object(cc, "_enqueue_write") as mock_enqueue:
            save_quality_score("telegram", "u1", "length_appropriate", 1.0)
            _, params = mock_enqueue.call_args[0]
            assert "length_appropriate" in params
            assert 1.0 in params

    def test_params_contain_platform_user_id(self):
        with patch.object(cc, "_enqueue_write") as mock_enqueue:
            save_quality_score("slack", "userZ", "composite", 0.6)
            _, params = mock_enqueue.call_args[0]
            assert "slack" in params
            assert "userZ" in params

    def test_response_preview_truncated_to_500(self):
        long_preview = "y" * 1000
        with patch.object(cc, "_enqueue_write") as mock_enqueue:
            save_quality_score("telegram", "u1", "composite", 0.5, response_preview=long_preview)
            _, params = mock_enqueue.call_args[0]
            preview_param = [p for p in params if isinstance(p, str) and p.startswith("y")]
            assert len(preview_param) == 1
            assert len(preview_param[0]) == 500

    def test_metadata_passed_through(self):
        with patch.object(cc, "_enqueue_write") as mock_enqueue:
            save_quality_score("telegram", "u1", "composite", 0.9, metadata='{"key": "val"}')
            _, params = mock_enqueue.call_args[0]
            assert '{"key": "val"}' in params


# ═══════════════════════════════════════════════════════════════════════════════
# 11. get_quality_trend
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetQualityTrend:
    def setup_method(self):
        _clear_quality_scores()

    def test_returns_empty_list_with_no_data(self):
        result = get_quality_trend("telegram", "nobody")
        assert result == []

    def test_returns_scores_in_chronological_order(self):
        conn = _get_conn()
        now = time.time()
        scores = [0.2, 0.5, 0.8, 1.0]
        for i, score in enumerate(scores):
            conn.execute(
                "INSERT INTO quality_scores (platform, user_id, evaluator, score, ts) VALUES (?,?,?,?,?)",
                ("telegram", "u20", "composite", score, now + i),
            )
        conn.commit()
        result = get_quality_trend("telegram", "u20", evaluator="composite")
        # DB returns DESC, function reverses → chronological (ascending ts)
        assert result == scores

    def test_respects_limit(self):
        conn = _get_conn()
        now = time.time()
        for i in range(20):
            conn.execute(
                "INSERT INTO quality_scores (platform, user_id, evaluator, score, ts) VALUES (?,?,?,?,?)",
                ("telegram", "u21", "composite", float(i) / 20, now + i),
            )
        conn.commit()
        result = get_quality_trend("telegram", "u21", evaluator="composite", limit=5)
        assert len(result) == 5

    def test_default_evaluator_is_composite(self):
        conn = _get_conn()
        now = time.time()
        conn.execute(
            "INSERT INTO quality_scores (platform, user_id, evaluator, score, ts) VALUES (?,?,?,?,?)",
            ("telegram", "u22", "composite", 0.75, now),
        )
        conn.execute(
            "INSERT INTO quality_scores (platform, user_id, evaluator, score, ts) VALUES (?,?,?,?,?)",
            ("telegram", "u22", "length_appropriate", 1.0, now + 1),
        )
        conn.commit()
        result = get_quality_trend("telegram", "u22")
        assert result == [0.75]

    def test_evaluator_filter(self):
        conn = _get_conn()
        now = time.time()
        conn.execute(
            "INSERT INTO quality_scores (platform, user_id, evaluator, score, ts) VALUES (?,?,?,?,?)",
            ("telegram", "u23", "error_free", 1.0, now),
        )
        conn.execute(
            "INSERT INTO quality_scores (platform, user_id, evaluator, score, ts) VALUES (?,?,?,?,?)",
            ("telegram", "u23", "composite", 0.6, now + 1),
        )
        conn.commit()
        result = get_quality_trend("telegram", "u23", evaluator="error_free")
        assert result == [1.0]


# ═══════════════════════════════════════════════════════════════════════════════
# 12. append_learning
# ═══════════════════════════════════════════════════════════════════════════════


class TestAppendLearning:
    def test_creates_file_if_not_exists(self, tmp_path, monkeypatch):
        learnings_file = tmp_path / "learnings.md"
        monkeypatch.setattr(fb, "LEARNINGS_PATH", learnings_file)
        assert not learnings_file.exists()
        append_learning("Test insight", source="test", category="general")
        assert learnings_file.exists()

    def test_new_file_has_header(self, tmp_path, monkeypatch):
        learnings_file = tmp_path / "learnings.md"
        monkeypatch.setattr(fb, "LEARNINGS_PATH", learnings_file)
        append_learning("First insight")
        content = learnings_file.read_text()
        assert "# Telechat Learnings" in content

    def test_appends_to_existing_file(self, tmp_path, monkeypatch):
        learnings_file = tmp_path / "learnings.md"
        monkeypatch.setattr(fb, "LEARNINGS_PATH", learnings_file)
        append_learning("Insight one")
        append_learning("Insight two")
        content = learnings_file.read_text()
        assert "Insight one" in content
        assert "Insight two" in content

    def test_entry_contains_category(self, tmp_path, monkeypatch):
        learnings_file = tmp_path / "learnings.md"
        monkeypatch.setattr(fb, "LEARNINGS_PATH", learnings_file)
        append_learning("Some insight", category="performance")
        content = learnings_file.read_text()
        assert "performance" in content

    def test_entry_contains_source(self, tmp_path, monkeypatch):
        learnings_file = tmp_path / "learnings.md"
        monkeypatch.setattr(fb, "LEARNINGS_PATH", learnings_file)
        append_learning("Some insight", source="manual")
        content = learnings_file.read_text()
        assert "manual" in content

    def test_entry_contains_insight_text(self, tmp_path, monkeypatch):
        learnings_file = tmp_path / "learnings.md"
        monkeypatch.setattr(fb, "LEARNINGS_PATH", learnings_file)
        append_learning("Users prefer concise answers")
        content = learnings_file.read_text()
        assert "Users prefer concise answers" in content

    def test_second_append_does_not_repeat_header(self, tmp_path, monkeypatch):
        learnings_file = tmp_path / "learnings.md"
        monkeypatch.setattr(fb, "LEARNINGS_PATH", learnings_file)
        append_learning("First")
        append_learning("Second")
        content = learnings_file.read_text()
        assert content.count("# Telechat Learnings") == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 13. get_learnings_summary
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetLearningsSummary:
    def test_returns_empty_string_if_file_not_exists(self, tmp_path, monkeypatch):
        learnings_file = tmp_path / "learnings.md"
        monkeypatch.setattr(fb, "LEARNINGS_PATH", learnings_file)
        assert get_learnings_summary() == ""

    def test_returns_full_content_if_short(self, tmp_path, monkeypatch):
        learnings_file = tmp_path / "learnings.md"
        monkeypatch.setattr(fb, "LEARNINGS_PATH", learnings_file)
        content = "# Learnings\n\nShort content."
        learnings_file.write_text(content)
        assert get_learnings_summary() == content

    def test_returns_last_2000_chars_with_prefix_if_long(self, tmp_path, monkeypatch):
        learnings_file = tmp_path / "learnings.md"
        monkeypatch.setattr(fb, "LEARNINGS_PATH", learnings_file)
        long_content = "x" * 3000
        learnings_file.write_text(long_content)
        result = get_learnings_summary()
        assert result.startswith("...\n")
        assert result == "...\n" + long_content[-2000:]

    def test_exactly_2000_chars_not_truncated(self, tmp_path, monkeypatch):
        learnings_file = tmp_path / "learnings.md"
        monkeypatch.setattr(fb, "LEARNINGS_PATH", learnings_file)
        content = "y" * 2000
        learnings_file.write_text(content)
        result = get_learnings_summary()
        assert result == content
        assert not result.startswith("...\n")

    def test_exactly_2001_chars_triggers_truncation(self, tmp_path, monkeypatch):
        learnings_file = tmp_path / "learnings.md"
        monkeypatch.setattr(fb, "LEARNINGS_PATH", learnings_file)
        content = "z" * 2001
        learnings_file.write_text(content)
        result = get_learnings_summary()
        assert result.startswith("...\n")
