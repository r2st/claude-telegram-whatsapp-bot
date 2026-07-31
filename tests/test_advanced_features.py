"""Tests for advanced features: commitments, context_compaction,
doctor, conversation_export, document_extract."""
import os
import tempfile
import time
import unittest

# ─── Commitments ────────────────────────────────────────────────────────────

class TestCommitmentsTimeParsing(unittest.TestCase):
    def test_parse_minutes(self):
        from telechat_pkg.commitments import parse_due_time
        ts = parse_due_time("in 30 minutes")
        self.assertIsNotNone(ts)
        self.assertAlmostEqual(ts, time.time() + 1800, delta=5)

    def test_parse_hours(self):
        from telechat_pkg.commitments import parse_due_time
        ts = parse_due_time("in 2 hours")
        self.assertIsNotNone(ts)
        self.assertAlmostEqual(ts, time.time() + 7200, delta=5)

    def test_parse_tomorrow(self):
        from telechat_pkg.commitments import parse_due_time
        ts = parse_due_time("tomorrow")
        self.assertIsNotNone(ts)
        self.assertAlmostEqual(ts, time.time() + 86400, delta=5)

    def test_parse_next_week(self):
        from telechat_pkg.commitments import parse_due_time
        ts = parse_due_time("next week")
        self.assertIsNotNone(ts)
        self.assertAlmostEqual(ts, time.time() + 7 * 86400, delta=5)

    def test_parse_no_match(self):
        from telechat_pkg.commitments import parse_due_time
        self.assertIsNone(parse_due_time("whenever"))

    def test_parse_days(self):
        from telechat_pkg.commitments import parse_due_time
        ts = parse_due_time("in 3 days")
        self.assertIsNotNone(ts)
        self.assertAlmostEqual(ts, time.time() + 3 * 86400, delta=5)

    def test_parse_weeks(self):
        from telechat_pkg.commitments import parse_due_time
        ts = parse_due_time("in 2 weeks")
        self.assertIsNotNone(ts)
        self.assertAlmostEqual(ts, time.time() + 14 * 86400, delta=5)


class TestCommitmentsExtraction(unittest.TestCase):
    def test_remind_me(self):
        from telechat_pkg.commitments import extract_commitments
        results = extract_commitments("remind me to buy groceries tomorrow")
        self.assertLessEqual(len(results), 2)
        self.assertEqual(results[0]["kind"], "reminder")
        self.assertIn("buy groceries", results[0]["reason"])

    def test_dont_forget(self):
        from telechat_pkg.commitments import extract_commitments
        results = extract_commitments("don't forget to call mom")
        self.assertLessEqual(len(results), 2)
        self.assertEqual(results[0]["kind"], "reminder")

    def test_follow_up_from_assistant(self):
        from telechat_pkg.commitments import extract_commitments
        results = extract_commitments("", "I'll check on that issue for you.")
        self.assertLessEqual(len(results), 2)
        self.assertEqual(results[0]["kind"], "follow_up")

    def test_no_commitment(self):
        from telechat_pkg.commitments import extract_commitments
        results = extract_commitments("hello how are you")
        self.assertEqual(len(results), 0)

    def test_short_reason_filtered(self):
        from telechat_pkg.commitments import extract_commitments
        results = extract_commitments("remind me to x")
        self.assertEqual(len(results), 0)

    def test_dedup(self):
        from telechat_pkg.commitments import extract_commitments
        results = extract_commitments(
            "remind me to buy milk. remind me to buy milk tomorrow."
        )
        self.assertLessEqual(len(results), 2)

    def test_deadline_extraction(self):
        from telechat_pkg.commitments import extract_commitments
        results = extract_commitments("deadline is next week for the report.")
        self.assertLessEqual(len(results), 2)
        self.assertEqual(results[0]["kind"], "deadline")


class TestCommitmentsFormatPending(unittest.TestCase):
    def test_empty(self):
        from telechat_pkg.commitments import format_pending
        self.assertIn("No pending", format_pending([]))

    def test_with_items(self):
        from telechat_pkg.commitments import format_pending, CommitmentRecord
        records = [
            CommitmentRecord(
                id="abc", platform="telegram", user_id="123",
                kind="reminder", status="pending", reason="buy milk",
                due_at=time.time() + 3600, created_at=time.time(),
            )
        ]
        text = format_pending(records)
        self.assertIn("buy milk", text)
        self.assertIn("Pending", text)

    def test_overdue_item(self):
        from telechat_pkg.commitments import format_pending, CommitmentRecord
        records = [
            CommitmentRecord(
                id="xyz", platform="telegram", user_id="123",
                kind="reminder", status="pending", reason="overdue task",
                due_at=time.time() - 3600, created_at=time.time() - 7200,
            )
        ]
        text = format_pending(records)
        self.assertIn("overdue", text)


# ─── Context Compaction ─────────────────────────────────────────────────────

class TestContextCompaction(unittest.TestCase):
    def test_estimate_tokens(self):
        from telechat_pkg.context_compaction import estimate_tokens
        self.assertEqual(estimate_tokens("hello"), 1)
        self.assertEqual(estimate_tokens("a" * 400), 100)

    def test_needs_compaction_small(self):
        from telechat_pkg.context_compaction import needs_compaction
        msgs = [{"role": "user", "content": "hi"}] * 5
        self.assertFalse(needs_compaction(msgs))

    def test_needs_compaction_large(self):
        from telechat_pkg.context_compaction import needs_compaction
        msgs = [{"role": "user", "content": "x" * 10000}] * 50
        self.assertTrue(needs_compaction(msgs))

    def test_compact_noop(self):
        from telechat_pkg.context_compaction import compact_history_sync
        msgs = [{"role": "user", "content": "hi"}] * 5
        result = compact_history_sync(msgs)
        self.assertEqual(result.messages_compacted, 0)
        self.assertEqual(result.history, msgs)

    def test_compact_large(self):
        from telechat_pkg.context_compaction import compact_history_sync
        msgs = [{"role": "user", "content": f"Message {i} " + "x" * 50000} for i in range(30)]
        result = compact_history_sync(msgs)
        self.assertGreater(result.messages_compacted, 0)
        self.assertLess(result.messages_after, result.messages_before)
        self.assertEqual(result.history[0]["role"], "system")
        self.assertIn("compacted", result.history[0]["content"])

    def test_estimate_history_tokens(self):
        from telechat_pkg.context_compaction import estimate_history_tokens
        msgs = [{"role": "user", "content": "a" * 100}] * 10
        self.assertEqual(estimate_history_tokens(msgs), 250)

    def test_build_summary_prompt(self):
        from telechat_pkg.context_compaction import build_summary_prompt
        msgs = [{"role": "user", "content": "hello"}]
        prompt = build_summary_prompt(msgs)
        self.assertIn("Summarize", prompt)
        self.assertIn("hello", prompt)


# ─── Doctor ─────────────────────────────────────────────────────────────────

class TestDoctor(unittest.TestCase):
    def test_check_python_version(self):
        from telechat_pkg.doctor import check_python_version
        result = check_python_version()
        self.assertTrue(result.passed)

    def test_check_disk_space(self):
        from telechat_pkg.doctor import check_disk_space
        result = check_disk_space()
        self.assertTrue(result.passed)

    def test_report_format(self):
        from telechat_pkg.doctor import DoctorReport, CheckResult
        report = DoctorReport()
        report.add(CheckResult("Test", True, "OK"))
        report.add(CheckResult("Warn", False, "Hmm", severity="warning"))
        text = report.format()
        self.assertIn("Test", text)
        self.assertIn("Warn", text)
        self.assertIn("Passed: 1", text)
        self.assertIn("Warnings: 1", text)

    def test_report_healthy(self):
        from telechat_pkg.doctor import DoctorReport, CheckResult
        report = DoctorReport()
        report.add(CheckResult("Test", True, "OK"))
        self.assertTrue(report.healthy)

    def test_report_unhealthy(self):
        from telechat_pkg.doctor import DoctorReport, CheckResult
        report = DoctorReport()
        report.add(CheckResult("Fail", False, "Bad", severity="error"))
        self.assertFalse(report.healthy)

    def test_run_doctor_sync(self):
        from telechat_pkg.doctor import run_doctor_sync
        report = run_doctor_sync()
        self.assertGreater(len(report.checks), 5)

    def test_check_dependencies(self):
        from telechat_pkg.doctor import check_dependencies
        result = check_dependencies()
        self.assertIn("name", dir(result))

    def test_check_rate_limits(self):
        from telechat_pkg.doctor import check_rate_limits
        result = check_rate_limits()
        self.assertIn("name", dir(result))


# ─── Conversation Export ────────────────────────────────────────────────────

class TestConversationExport(unittest.TestCase):
    def setUp(self):
        self.messages = [
            {"role": "user", "content": "Hello!", "timestamp": 1700000000},
            {"role": "assistant", "content": "Hi there!", "timestamp": 1700000010},
        ]

    def test_export_text(self):
        from telechat_pkg.conversation_export import export_text
        result = export_text(self.messages)
        self.assertEqual(result.format, "text")
        self.assertEqual(result.message_count, 2)
        self.assertIn("Hello!", result.content)
        self.assertTrue(result.filename.endswith(".txt"))

    def test_export_markdown(self):
        from telechat_pkg.conversation_export import export_markdown
        result = export_markdown(self.messages)
        self.assertEqual(result.format, "markdown")
        self.assertIn("###", result.content)
        self.assertTrue(result.filename.endswith(".md"))

    def test_export_html(self):
        from telechat_pkg.conversation_export import export_html
        result = export_html(self.messages)
        self.assertEqual(result.format, "html")
        self.assertIn("<html", result.content)
        self.assertIn("Hello!", result.content)
        self.assertTrue(result.filename.endswith(".html"))

    def test_export_json(self):
        from telechat_pkg.conversation_export import export_json
        import json
        result = export_json(self.messages)
        self.assertEqual(result.format, "json")
        data = json.loads(result.content)
        self.assertEqual(data["message_count"], 2)
        self.assertEqual(len(data["messages"]), 2)

    def test_export_convenience(self):
        from telechat_pkg.conversation_export import export_conversation
        for fmt in ("text", "txt", "markdown", "md", "html", "json"):
            result = export_conversation(self.messages, fmt)
            self.assertEqual(result.message_count, 2)

    def test_export_unknown_format(self):
        from telechat_pkg.conversation_export import export_conversation
        with self.assertRaises(ValueError):
            export_conversation(self.messages, "pdf")

    def test_html_escaping(self):
        from telechat_pkg.conversation_export import export_html
        msgs = [{"role": "user", "content": "<script>alert('xss')</script>"}]
        result = export_html(msgs)
        self.assertNotIn("<script>", result.content)
        self.assertIn("&lt;script&gt;", result.content)

    def test_no_timestamps(self):
        from telechat_pkg.conversation_export import export_text
        msgs = [{"role": "user", "content": "test"}]
        result = export_text(msgs, include_timestamps=False)
        self.assertIn("User:", result.content)


# ─── Document Extract ──────────────────────────────────────────────────────

class TestDocumentExtract(unittest.TestCase):
    def test_extract_text_file(self):
        from telechat_pkg.document_extract import extract
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("Hello world\nLine two")
            f.flush()
            result = extract(f.name)
        self.assertIn("Hello world", result.text)
        self.assertIsNone(result.error)
        os.unlink(f.name)

    def test_extract_csv(self):
        from telechat_pkg.document_extract import extract
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write("name,age\nAlice,30\nBob,25")
            f.flush()
            result = extract(f.name)
        self.assertIn("Alice", result.text)
        self.assertEqual(result.format, "csv")
        os.unlink(f.name)

    def test_extract_python_file(self):
        from telechat_pkg.document_extract import extract
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("def hello():\n    print('hi')\n")
            f.flush()
            result = extract(f.name)
        self.assertIn("def hello", result.text)
        os.unlink(f.name)

    def test_extract_missing_file(self):
        from telechat_pkg.document_extract import extract
        result = extract("/nonexistent/file.txt")
        self.assertIsNotNone(result.error)

    def test_extract_empty_file(self):
        from telechat_pkg.document_extract import extract
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            pass
        result = extract(f.name)
        self.assertIsNotNone(result.error)
        os.unlink(f.name)

    def test_available_formats(self):
        from telechat_pkg.document_extract import available_formats
        fmts = available_formats()
        self.assertIn("txt", fmts)
        self.assertIn("csv", fmts)

    def test_summarize_extraction(self):
        from telechat_pkg.document_extract import summarize_extraction, ExtractResult
        result = ExtractResult(text="Hello world", pages=1, format="txt")
        summary = summarize_extraction(result)
        self.assertIn("TXT", summary)

    def test_summarize_error(self):
        from telechat_pkg.document_extract import summarize_extraction, ExtractResult
        result = ExtractResult(text="", pages=0, format="pdf", error="not installed")
        summary = summarize_extraction(result)
        self.assertIn("not installed", summary)


# ─── Store replace_history ──────────────────────────────────────────────────

class TestStoreReplaceHistory(unittest.TestCase):
    def test_replace_history(self):
        from telechat_pkg import store
        store.init_db()
        platform = "test_replace"
        uid = "test_user_999"
        store.save_turn(platform, uid, "hello", "hi")
        store.save_turn(platform, uid, "how are you", "fine")
        new_history = [
            {"role": "system", "content": "Summary of conversation"},
            {"role": "user", "content": "latest question"},
            {"role": "assistant", "content": "latest answer"},
        ]
        store.replace_history(platform, uid, new_history)
        loaded = store.load_history(platform, uid, limit=100)
        self.assertEqual(len(loaded), 3)
        self.assertEqual(loaded[0]["role"], "system")
        self.assertIn("Summary", loaded[0]["content"])
        store.clear_history(platform, uid)


if __name__ == "__main__":
    unittest.main()
