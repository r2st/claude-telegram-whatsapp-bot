"""
Benchmarks for telechat core modules.

Run with:
    pytest tests/bench_telechat.py --benchmark-only
    pytest tests/bench_telechat.py --benchmark-only --benchmark-sort=mean
    pytest tests/bench_telechat.py --benchmark-only --benchmark-group-by=group
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from telechat_pkg.text_chunking import chunk_text, _find_fence_spans
from telechat_pkg.markdown_v2 import to_markdown_v2, escape_md2
from telechat_pkg.error_classifier import classify_error, _fingerprint, ConvergenceDetector
from telechat_pkg.store import (
    check_rate_limit,
    _cache_key,
    _rate_state,
    UserSession,
    SessionManager,
)
from telechat_pkg.memory import MemoryStore
from telechat_pkg.feedback import (
    evaluate_response,
    _eval_length,
    _eval_error_free,
    _eval_has_content,
    _eval_not_truncated,
    _eval_reasonable_cost,
)
from telechat_pkg.health import CircuitBreaker, register_component, report_healthy, get_health
from telechat_pkg.polls import parse_poll_command, extract_poll_from_response
from telechat_pkg.link_understanding import extract_links, _strip_html
from telechat_pkg.coder import PipelineTracker, build_task_prompt
from telechat_pkg.scheduled_tasks import ScheduledTask
from telechat_pkg.claude_core import _build_prompt, _parse_cli_output, _extract_tool_detail


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "bench.db")


@pytest.fixture
def memory_store(tmp_db):
    return MemoryStore(db_path=tmp_db)


@pytest.fixture
def populated_memory_store(tmp_db):
    store = MemoryStore(db_path=tmp_db)
    for i in range(200):
        store.remember(
            "telegram", "user1",
            f"Memory entry {i}: the user prefers {'dark' if i % 2 == 0 else 'light'} mode "
            f"and uses {'Python' if i % 3 == 0 else 'TypeScript'} for project-{i % 10}",
            tags=["preference", f"project-{i % 10}"],
            importance=0.5 + (i % 5) * 0.1,
        )
    return store


@pytest.fixture(autouse=True)
def _clean_rate_state():
    """Prevent rate limit state from leaking between benchmarks."""
    yield
    _rate_state.clear()


SHORT_TEXT = "Hello, this is a short response from Claude."

MEDIUM_TEXT = "\n\n".join(
    f"Paragraph {i}: " + "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 5
    for i in range(20)
)

LONG_TEXT_WITH_CODE = """Here's a detailed explanation with code examples.

## Overview

This is a complex response with multiple sections and code blocks.

```python
def fibonacci(n):
    if n <= 1:
        return n
    a, b = 0, 1
    for _ in range(2, n + 1):
        a, b = b, a + b
    return b

# Test it
for i in range(10):
    print(f"fib({i}) = {fibonacci(i)}")
```

The above implementation uses iterative approach for O(n) time complexity.

## Another Section

More text here with **bold**, *italic*, and `inline code`.

```javascript
const fetchData = async (url) => {
    const response = await fetch(url);
    if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
    }
    return response.json();
};
```

### Subsection

- Point one with details
- Point two with more info
- Point three with [a link](https://example.com)

> This is a blockquote from the documentation
> that spans multiple lines.

And here's a table-like structure:

| Column A | Column B | Column C |
|----------|----------|----------|
| Value 1  | Value 2  | Value 3  |

""" * 3

MARKDOWN_RICH = """# Main Heading

Here's some **bold text** and *italic text* with ~~strikethrough~~.

## Code Section

Check this `inline code` and this block:

```python
class Bot:
    def __init__(self, token):
        self.token = token

    async def send(self, chat_id, text):
        await self._api("sendMessage", chat_id=chat_id, text=text)
```

> Important: Always validate your inputs before processing.
> This is critical for security.

### Links and Lists

- First item with [a link](https://example.com/path?q=1&r=2)
- Second item with **bold** and `code`
- Third item

1. Numbered one
2. Numbered two
3. Numbered three

---

Final paragraph with mixed *italic* and **bold** and ~~strike~~ formatting.
"""

ERROR_SAMPLES = [
    "SyntaxError: invalid syntax at line 42 in /app/main.py",
    "TypeError: Cannot read property 'map' of undefined",
    "ImportError: No module named 'nonexistent_package'",
    "AssertionError: Expected 5 but got 3",
    "FileNotFoundError: [Errno 2] No such file or directory: '/tmp/data.csv'",
    "connection refused: ETIMEDOUT after 30000ms",
    "FAIL tests/test_api.py::test_create_user - AssertionError",
    "TS2322: Type 'string' is not assignable to type 'number'",
    "circular dependency detected between moduleA and moduleB",
    "database error: connection pool exhausted after 50 retries",
]


# ═══════════════════════════════════════════════════════════════════════════════
# Text Chunking Benchmarks
# ═══════════════════════════════════════════════════════════════════════════════


class TestChunkingBenchmarks:

    @pytest.mark.benchmark(group="chunking")
    def test_chunk_short_text(self, benchmark):
        benchmark(chunk_text, SHORT_TEXT)

    @pytest.mark.benchmark(group="chunking")
    def test_chunk_medium_text(self, benchmark):
        benchmark(chunk_text, MEDIUM_TEXT, 500)

    @pytest.mark.benchmark(group="chunking")
    def test_chunk_long_with_code(self, benchmark):
        benchmark(chunk_text, LONG_TEXT_WITH_CODE, 2000)

    @pytest.mark.benchmark(group="chunking")
    def test_chunk_by_length_fallback(self, benchmark):
        benchmark(chunk_text, LONG_TEXT_WITH_CODE, 2000, "length")

    @pytest.mark.benchmark(group="chunking")
    def test_find_fence_spans(self, benchmark):
        benchmark(_find_fence_spans, LONG_TEXT_WITH_CODE)

    @pytest.mark.benchmark(group="chunking")
    def test_chunk_very_long_text(self, benchmark):
        very_long = MEDIUM_TEXT * 20
        benchmark(chunk_text, very_long, 4000)


# ═══════════════════════════════════════════════════════════════════════════════
# MarkdownV2 Benchmarks
# ═══════════════════════════════════════════════════════════════════════════════


class TestMarkdownV2Benchmarks:

    @pytest.mark.benchmark(group="markdown")
    def test_escape_md2_short(self, benchmark):
        benchmark(escape_md2, "Hello world! How are you?")

    @pytest.mark.benchmark(group="markdown")
    def test_escape_md2_special_chars(self, benchmark):
        text = "Price: $100.00 (50% off!) [link](url) *bold* _italic_ ~strike~"
        benchmark(escape_md2, text)

    @pytest.mark.benchmark(group="markdown")
    def test_to_markdown_v2_plain(self, benchmark):
        benchmark(to_markdown_v2, SHORT_TEXT)

    @pytest.mark.benchmark(group="markdown")
    def test_to_markdown_v2_rich(self, benchmark):
        benchmark(to_markdown_v2, MARKDOWN_RICH)

    @pytest.mark.benchmark(group="markdown")
    def test_to_markdown_v2_with_code(self, benchmark):
        benchmark(to_markdown_v2, LONG_TEXT_WITH_CODE)

    @pytest.mark.benchmark(group="markdown")
    def test_to_markdown_v2_repeated(self, benchmark):
        big = MARKDOWN_RICH * 5
        benchmark(to_markdown_v2, big)


# ═══════════════════════════════════════════════════════════════════════════════
# Error Classifier Benchmarks
# ═══════════════════════════════════════════════════════════════════════════════


class TestErrorClassifierBenchmarks:

    @pytest.mark.benchmark(group="error_classifier")
    def test_classify_syntax_error(self, benchmark):
        benchmark(classify_error, ERROR_SAMPLES[0])

    @pytest.mark.benchmark(group="error_classifier")
    def test_classify_type_error(self, benchmark):
        benchmark(classify_error, ERROR_SAMPLES[1])

    @pytest.mark.benchmark(group="error_classifier")
    def test_classify_unknown_error(self, benchmark):
        benchmark(classify_error, "something completely unexpected happened in the system")

    @pytest.mark.benchmark(group="error_classifier")
    def test_classify_all_error_types(self, benchmark):
        def classify_all():
            for sample in ERROR_SAMPLES:
                classify_error(sample)
        benchmark(classify_all)

    @pytest.mark.benchmark(group="error_classifier")
    def test_fingerprint_stability(self, benchmark):
        benchmark(_fingerprint, ERROR_SAMPLES[0])

    @pytest.mark.benchmark(group="error_classifier")
    def test_fingerprint_long_error(self, benchmark):
        long_error = "\n".join(ERROR_SAMPLES) * 10
        benchmark(_fingerprint, long_error)

    @pytest.mark.benchmark(group="error_classifier")
    def test_convergence_check(self, benchmark):
        def run_convergence():
            detector = ConvergenceDetector()
            for i in range(10):
                fp = _fingerprint(ERROR_SAMPLES[i % len(ERROR_SAMPLES)])
                detector.record(fp)
                detector.check()
        benchmark(run_convergence)


# ═══════════════════════════════════════════════════════════════════════════════
# Rate Limiter Benchmarks
# ═══════════════════════════════════════════════════════════════════════════════


class TestRateLimitBenchmarks:

    @pytest.mark.benchmark(group="rate_limit")
    def test_rate_limit_check_fresh(self, benchmark):
        counter = [0]
        def check_fresh():
            counter[0] += 1
            check_rate_limit(f"bench_user_{counter[0]}")
        benchmark(check_fresh)

    @pytest.mark.benchmark(group="rate_limit")
    def test_rate_limit_check_warm(self, benchmark):
        key = "bench_warm_user"
        for _ in range(10):
            check_rate_limit(key)
        benchmark(check_rate_limit, key)

    @pytest.mark.benchmark(group="rate_limit")
    def test_rate_limit_check_at_limit(self, benchmark):
        key = "bench_limit_user"
        for _ in range(19):
            check_rate_limit(key)
        benchmark(check_rate_limit, key)


# ═══════════════════════════════════════════════════════════════════════════════
# Store / DB Benchmarks
# ═══════════════════════════════════════════════════════════════════════════════


class TestStoreBenchmarks:

    @pytest.mark.benchmark(group="store")
    def test_cache_key_generation(self, benchmark):
        benchmark(_cache_key, "telegram", "123456789")

    @pytest.mark.benchmark(group="store")
    def test_user_session_creation(self, benchmark):
        def create_session():
            return UserSession("test", "telegram", "123")
        benchmark(create_session)

    @pytest.mark.benchmark(group="store")
    def test_user_session_summary_line(self, benchmark):
        sess = UserSession("coding", "telegram", "123", title="My Project", message_count=42)
        benchmark(sess.summary_line)

    @pytest.mark.benchmark(group="store")
    def test_user_session_age_str(self, benchmark):
        sess = UserSession("test", "telegram", "123")
        sess.last_active = time.time() - 3700
        benchmark(sess.age_str)

    @pytest.mark.benchmark(group="store")
    def test_session_cli_valid_check(self, benchmark):
        sess = UserSession("test", "telegram", "123")
        sess.claude_session_id = "sess_abc123"
        sess.last_active = time.time() - 100
        benchmark(lambda: sess.cli_session_valid)

    @pytest.mark.benchmark(group="store")
    def test_sqlite_init_schema(self, benchmark, tmp_path):
        def init_fresh():
            db_path = str(tmp_path / f"bench_{time.monotonic_ns()}.db")
            conn = sqlite3.connect(db_path)
            SessionManager.init_schema(conn)
            conn.close()
        benchmark(init_fresh)

    @pytest.mark.benchmark(group="store")
    def test_sqlite_insert_conversation(self, benchmark, tmp_path):
        db_path = str(tmp_path / "conv_bench.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                platform TEXT NOT NULL, user_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL,
                ts REAL NOT NULL, PRIMARY KEY (platform, user_id, ts))
        """)
        conn.commit()
        counter = [0]

        def insert_turn():
            counter[0] += 1
            ts = time.time() + counter[0] * 0.001
            conn.execute(
                "INSERT OR IGNORE INTO conversations VALUES (?,?,?,?,?)",
                ("telegram", "bench_user", "user", f"message {counter[0]}", ts),
            )
            conn.commit()
        benchmark(insert_turn)
        conn.close()

    @pytest.mark.benchmark(group="store")
    def test_sqlite_load_history(self, benchmark, tmp_path):
        db_path = str(tmp_path / "hist_bench.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                platform TEXT NOT NULL, user_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL,
                ts REAL NOT NULL, PRIMARY KEY (platform, user_id, ts))
        """)
        for i in range(100):
            conn.execute(
                "INSERT INTO conversations VALUES (?,?,?,?,?)",
                ("telegram", "bench_user", "user" if i % 2 == 0 else "assistant",
                 f"message content {i} " * 20, time.time() + i * 0.001),
            )
        conn.commit()

        def load_20():
            rows = conn.execute(
                "SELECT role, content FROM conversations WHERE platform=? AND user_id=? ORDER BY ts DESC LIMIT 20",
                ("telegram", "bench_user"),
            ).fetchall()
            return [{"role": r[0], "content": r[1]} for r in reversed(rows)]
        benchmark(load_20)
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Memory Store Benchmarks
# ═══════════════════════════════════════════════════════════════════════════════


class TestMemoryBenchmarks:

    @pytest.mark.benchmark(group="memory")
    def test_remember_single(self, benchmark, memory_store):
        counter = [0]
        def remember_one():
            counter[0] += 1
            memory_store.remember(
                "telegram", "user1",
                f"Benchmark memory {counter[0]}: user prefers vim keybindings",
                tags=["preference"],
                importance=0.7,
            )
        benchmark(remember_one)

    @pytest.mark.benchmark(group="memory")
    def test_recall_fts_populated(self, benchmark, populated_memory_store):
        benchmark(populated_memory_store.recall, "telegram", "user1", "dark mode Python")

    @pytest.mark.benchmark(group="memory")
    def test_recall_fts_no_results(self, benchmark, populated_memory_store):
        benchmark(populated_memory_store.recall, "telegram", "user1", "xyznonexistent")

    @pytest.mark.benchmark(group="memory")
    def test_recall_empty_query(self, benchmark, populated_memory_store):
        benchmark(populated_memory_store.recall, "telegram", "user1", "")

    @pytest.mark.benchmark(group="memory")
    def test_recall_with_tags(self, benchmark, populated_memory_store):
        benchmark(
            populated_memory_store.recall,
            "telegram", "user1", "project",
            tags=["preference"],
        )

    @pytest.mark.benchmark(group="memory")
    def test_list_memories(self, benchmark, populated_memory_store):
        benchmark(populated_memory_store.list_memories, "telegram", "user1", limit=20)

    @pytest.mark.benchmark(group="memory")
    def test_stats(self, benchmark, populated_memory_store):
        benchmark(populated_memory_store.stats, "telegram", "user1")

    @pytest.mark.benchmark(group="memory")
    def test_forget_and_remember(self, benchmark, memory_store):
        def forget_remember():
            mem = memory_store.remember("telegram", "user1", "temp memory", importance=0.3)
            memory_store.forget("telegram", "user1", mem.id)
        benchmark(forget_remember)

    @pytest.mark.benchmark(group="memory")
    def test_update_memory(self, benchmark, memory_store):
        mem = memory_store.remember("telegram", "user1", "updatable memory", importance=0.5)

        counter = [0]
        def update_one():
            counter[0] += 1
            memory_store.update(
                "telegram", "user1", mem.id,
                content=f"updated content v{counter[0]}",
                importance=min(1.0, 0.5 + counter[0] * 0.01),
            )
        benchmark(update_one)

    @pytest.mark.benchmark(group="memory")
    def test_export_all(self, benchmark, populated_memory_store):
        benchmark(populated_memory_store.export_all, "telegram", "user1")


# ═══════════════════════════════════════════════════════════════════════════════
# Feedback / Quality Evaluator Benchmarks
# ═══════════════════════════════════════════════════════════════════════════════


class TestFeedbackBenchmarks:

    @pytest.mark.benchmark(group="feedback")
    def test_evaluate_response_good(self, benchmark):
        benchmark(
            evaluate_response,
            "Explain Python decorators",
            "Decorators are a powerful feature..." + " detail " * 50,
            {"cost_usd": 0.02},
        )

    @pytest.mark.benchmark(group="feedback")
    def test_evaluate_response_error(self, benchmark):
        benchmark(
            evaluate_response,
            "Fix the bug",
            "[Claude error] rate limit exceeded",
            {},
        )

    @pytest.mark.benchmark(group="feedback")
    def test_eval_length_various(self, benchmark):
        def check_all():
            _eval_length("hi", "short")
            _eval_length("hi", "x" * 6000)
            _eval_length("Explain the architecture of this system in detail", "ok")
            _eval_length("Explain the architecture of this system in detail", "x" * 500)
        benchmark(check_all)

    @pytest.mark.benchmark(group="feedback")
    def test_eval_error_free_clean(self, benchmark):
        benchmark(_eval_error_free, "Here's a perfectly normal response with no issues.")

    @pytest.mark.benchmark(group="feedback")
    def test_eval_error_free_dirty(self, benchmark):
        benchmark(_eval_error_free, "[Claude error] something went wrong, rate limit hit")

    @pytest.mark.benchmark(group="feedback")
    def test_eval_has_content(self, benchmark):
        def check_all():
            _eval_has_content("")
            _eval_has_content("(no response)")
            _eval_has_content("Here's a substantive answer to your question.")
        benchmark(check_all)

    @pytest.mark.benchmark(group="feedback")
    def test_eval_not_truncated(self, benchmark):
        benchmark(_eval_not_truncated, "Normal response without any truncation.")

    @pytest.mark.benchmark(group="feedback")
    def test_eval_reasonable_cost(self, benchmark):
        def check_all():
            _eval_reasonable_cost({})
            _eval_reasonable_cost({"cost_usd": 0.05})
            _eval_reasonable_cost({"cost_usd": 2.50})
        benchmark(check_all)


# ═══════════════════════════════════════════════════════════════════════════════
# Circuit Breaker Benchmarks
# ═══════════════════════════════════════════════════════════════════════════════


class TestCircuitBreakerBenchmarks:

    @pytest.mark.benchmark(group="circuit_breaker")
    def test_breaker_is_open_closed(self, benchmark):
        cb = CircuitBreaker("bench", failure_threshold=5, recovery_timeout=60)
        benchmark(lambda: cb.is_open)

    @pytest.mark.benchmark(group="circuit_breaker")
    def test_breaker_record_success(self, benchmark):
        cb = CircuitBreaker("bench", failure_threshold=5, recovery_timeout=60)
        benchmark(cb.record_success)

    @pytest.mark.benchmark(group="circuit_breaker")
    def test_breaker_record_failure(self, benchmark):
        cb = CircuitBreaker("bench", failure_threshold=100, recovery_timeout=60)
        benchmark(cb.record_failure)

    @pytest.mark.benchmark(group="circuit_breaker")
    def test_breaker_full_cycle(self, benchmark):
        def cycle():
            cb = CircuitBreaker("cycle", failure_threshold=3, recovery_timeout=0)
            cb.record_failure()
            cb.record_failure()
            cb.record_failure()
            _ = cb.is_open  # should be open
            _ = cb.is_open  # recovery_timeout=0 → half_open
            cb.record_success()
            cb.record_success()
            _ = cb.is_open  # should be closed
        benchmark(cycle)

    @pytest.mark.benchmark(group="circuit_breaker")
    def test_health_get_with_components(self, benchmark):
        for i in range(5):
            register_component(f"bench_comp_{i}")
            report_healthy(f"bench_comp_{i}")
        benchmark(get_health)


# ═══════════════════════════════════════════════════════════════════════════════
# Polls Benchmarks
# ═══════════════════════════════════════════════════════════════════════════════


class TestPollsBenchmarks:

    @pytest.mark.benchmark(group="polls")
    def test_parse_poll_pipe_format(self, benchmark):
        benchmark(parse_poll_command, "Best language? | Python | Rust | Go | TypeScript")

    @pytest.mark.benchmark(group="polls")
    def test_parse_poll_newline_format(self, benchmark):
        text = "What's for lunch?\nPizza\nSushi\nTacos\nSalad"
        benchmark(parse_poll_command, text)

    @pytest.mark.benchmark(group="polls")
    def test_parse_poll_with_flags(self, benchmark):
        benchmark(parse_poll_command, "--multi --public Best tools? | VS Code | Vim | Emacs")

    @pytest.mark.benchmark(group="polls")
    def test_parse_poll_invalid(self, benchmark):
        benchmark(parse_poll_command, "")

    @pytest.mark.benchmark(group="polls")
    def test_extract_poll_from_response_match(self, benchmark):
        text = """Here's what I'd suggest:

[POLL]
Q: Which framework should we use?
- React
- Vue
- Svelte
- Angular

Let me know what you think!"""
        benchmark(extract_poll_from_response, text)

    @pytest.mark.benchmark(group="polls")
    def test_extract_poll_from_response_no_match(self, benchmark):
        benchmark(extract_poll_from_response, MEDIUM_TEXT)


# ═══════════════════════════════════════════════════════════════════════════════
# Link Understanding Benchmarks
# ═══════════════════════════════════════════════════════════════════════════════


class TestLinkBenchmarks:

    @pytest.mark.benchmark(group="links")
    def test_extract_links_none(self, benchmark):
        benchmark(extract_links, "Just a regular message with no URLs at all.")

    @pytest.mark.benchmark(group="links")
    def test_extract_links_single(self, benchmark):
        benchmark(extract_links, "Check out https://example.com/path?q=search for details")

    @pytest.mark.benchmark(group="links")
    def test_extract_links_multiple(self, benchmark):
        text = (
            "See https://docs.python.org/3/library/asyncio.html and "
            "also https://github.com/python/cpython/issues/12345 and "
            "https://stackoverflow.com/questions/1234/something and "
            "another one https://reddit.com/r/python/comments/abc "
            "plus https://news.ycombinator.com/item?id=99999"
        )
        benchmark(extract_links, text)

    @pytest.mark.benchmark(group="links")
    def test_extract_links_with_markdown(self, benchmark):
        text = (
            "Check [this link](https://example.com/ignored) and also "
            "https://example.com/bare-url for more."
        )
        benchmark(extract_links, text)

    @pytest.mark.benchmark(group="links")
    def test_extract_links_blocked_hosts(self, benchmark):
        text = "Try http://localhost:3000 and http://0.0.0.0:8080 and http://127.0.0.1/admin"
        benchmark(extract_links, text)

    @pytest.mark.benchmark(group="links")
    def test_strip_html_small(self, benchmark):
        html = "<html><body><h1>Title</h1><p>Hello <b>world</b></p></body></html>"
        benchmark(_strip_html, html)

    @pytest.mark.benchmark(group="links")
    def test_strip_html_large(self, benchmark):
        html = (
            "<html><head><script>var x=1;</script><style>body{margin:0}</style></head>"
            "<body><nav>Nav</nav><main>" + "<p>Content paragraph.</p>\n" * 200 +
            "</main><footer>Footer</footer></body></html>"
        )
        benchmark(_strip_html, html)


# ═══════════════════════════════════════════════════════════════════════════════
# Coder / Pipeline Tracker Benchmarks
# ═══════════════════════════════════════════════════════════════════════════════


class TestCoderBenchmarks:

    @pytest.mark.benchmark(group="coder")
    def test_build_task_prompt(self, benchmark):
        benchmark(build_task_prompt, "Fix the broken test in test_main.py", "/home/user/project")

    @pytest.mark.benchmark(group="coder")
    def test_pipeline_on_tool_read(self, benchmark):
        tracker = PipelineTracker()
        benchmark(tracker.on_tool, "Read", "src/main.py")

    @pytest.mark.benchmark(group="coder")
    def test_pipeline_on_tool_bash_test(self, benchmark):
        tracker = PipelineTracker()
        tracker.on_tool("Write", "src/main.py")
        benchmark(tracker.on_tool, "Bash", "pytest tests/ -v")

    @pytest.mark.benchmark(group="coder")
    def test_pipeline_full_workflow(self, benchmark):
        def workflow():
            t = PipelineTracker()
            t.on_tool("Read", "src/main.py")
            t.on_tool("Grep", "def handle_message")
            t.on_tool("TodoWrite", "plan steps")
            t.on_tool("Edit", "src/main.py")
            t.on_tool("Write", "tests/test_new.py")
            t.on_tool("Bash", "pytest tests/ -v")
            t.on_success()
            t.on_tool("Bash", "ruff check src/")
            _ = t.pipeline_bar()
            _ = t.stage_summary()
        benchmark(workflow)

    @pytest.mark.benchmark(group="coder")
    def test_pipeline_fix_loop(self, benchmark):
        def fix_loop():
            t = PipelineTracker()
            t.on_tool("Edit", "src/main.py")
            for _ in range(5):
                t.on_tool("Bash", "pytest tests/ -v")
                t.on_error("AssertionError: Expected 5 but got 3")
                t.on_tool("Edit", "src/main.py")
            _ = t.get_convergence_warning()
        benchmark(fix_loop)

    @pytest.mark.benchmark(group="coder")
    def test_pipeline_bar(self, benchmark):
        tracker = PipelineTracker()
        tracker.on_tool("Read", "src/main.py")
        tracker.on_tool("TodoWrite", "plan")
        tracker.on_tool("Edit", "src/main.py")
        benchmark(tracker.pipeline_bar)

    @pytest.mark.benchmark(group="coder")
    def test_pipeline_stage_summary(self, benchmark):
        tracker = PipelineTracker()
        tracker.on_tool("Bash", "pytest tests/ -v")
        tracker.on_error("TypeError: something bad")
        benchmark(tracker.stage_summary)


# ═══════════════════════════════════════════════════════════════════════════════
# Scheduled Tasks Benchmarks
# ═══════════════════════════════════════════════════════════════════════════════


class TestScheduledTasksBenchmarks:

    @pytest.mark.benchmark(group="scheduled_tasks")
    def test_task_creation(self, benchmark):
        def create():
            return ScheduledTask(
                id="bench-1", name="Health Check",
                interval_seconds=300, callback_name="health_check",
                platform="telegram", user_id="123",
            )
        benchmark(create)

    @pytest.mark.benchmark(group="scheduled_tasks")
    def test_task_is_due(self, benchmark):
        task = ScheduledTask(
            id="bench-1", name="test",
            interval_seconds=60, callback_name="cb",
            last_run=time.time() - 120,
        )
        benchmark(lambda: task.is_due)

    @pytest.mark.benchmark(group="scheduled_tasks")
    def test_task_serialization_roundtrip(self, benchmark):
        task = ScheduledTask(
            id="bench-1", name="Health Check",
            interval_seconds=300, callback_name="health_check",
            platform="telegram", user_id="123",
            extra={"channel": "#ops", "retries": 3},
        )
        def roundtrip():
            d = task.to_dict()
            return ScheduledTask.from_dict(d)
        benchmark(roundtrip)

    @pytest.mark.benchmark(group="scheduled_tasks")
    def test_task_batch_serialization(self, benchmark):
        tasks = [
            ScheduledTask(
                id=f"t-{i}", name=f"Task {i}",
                interval_seconds=60 * (i + 1), callback_name="cb",
                platform="telegram", user_id="123",
                run_count=i * 10, last_run=time.time() - i * 100,
            )
            for i in range(20)
        ]
        def batch():
            data = [t.to_dict() for t in tasks]
            return [ScheduledTask.from_dict(d) for d in data]
        benchmark(batch)


# ═══════════════════════════════════════════════════════════════════════════════
# Claude Core Helper Benchmarks
# ═══════════════════════════════════════════════════════════════════════════════


class TestClaudeCoreBenchmarks:

    @pytest.mark.benchmark(group="claude_core")
    def test_build_prompt_short_history(self, benchmark):
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi! How can I help?"},
        ]
        benchmark(_build_prompt, "What's the weather?", history)

    @pytest.mark.benchmark(group="claude_core")
    def test_build_prompt_long_history(self, benchmark):
        history = [
            {"role": "user" if i % 2 == 0 else "assistant",
             "content": f"Message {i} with some content " * 10}
            for i in range(20)
        ]
        benchmark(_build_prompt, "Final question", history)

    @pytest.mark.benchmark(group="claude_core")
    def test_extract_tool_detail_file(self, benchmark):
        block = {"input": {"file_path": "/Users/dev/projects/telechat/src/main.py"}}
        benchmark(_extract_tool_detail, block)

    @pytest.mark.benchmark(group="claude_core")
    def test_extract_tool_detail_command(self, benchmark):
        block = {"input": {"command": "pytest tests/ -v --tb=short"}}
        benchmark(_extract_tool_detail, block)

    @pytest.mark.benchmark(group="claude_core")
    def test_extract_tool_detail_pattern(self, benchmark):
        block = {"input": {"pattern": "def handle_message.*async"}}
        benchmark(_extract_tool_detail, block)

    @pytest.mark.benchmark(group="claude_core")
    def test_extract_tool_detail_empty(self, benchmark):
        benchmark(_extract_tool_detail, {"input": {}})

    @pytest.mark.benchmark(group="claude_core")
    def test_parse_cli_output_result(self, benchmark):
        stdout = '{"type":"result","result":"Here is the answer.","usage":{"input_tokens":100,"output_tokens":50,"cache_read_input_tokens":10},"total_cost_usd":0.003,"session_id":"sess_abc"}'
        benchmark(_parse_cli_output, stdout, "", 0, 180)

    @pytest.mark.benchmark(group="claude_core")
    def test_parse_cli_output_streamed(self, benchmark):
        lines = []
        for i in range(10):
            lines.append(f'{{"type":"assistant","message":{{"content":[{{"type":"text","text":"chunk {i}"}}]}}}}')
        lines.append('{"type":"result","result":"Final answer.","usage":{"input_tokens":500,"output_tokens":200,"cache_read_input_tokens":50},"total_cost_usd":0.01,"session_id":"sess_xyz"}')
        stdout = "\n".join(lines)
        benchmark(_parse_cli_output, stdout, "", 0, 180)

    @pytest.mark.benchmark(group="claude_core")
    def test_parse_cli_output_with_tools(self, benchmark):
        lines = [
            '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{"file_path":"/src/main.py"}}]}}',
            '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Edit","input":{"file_path":"/src/main.py"}}]}}',
            '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"pytest"}}]}}',
            '{"type":"assistant","message":{"content":[{"type":"text","text":"Done! All tests pass."}]}}',
            '{"type":"result","result":"Done! All tests pass.","usage":{"input_tokens":1000,"output_tokens":400,"cache_read_input_tokens":200},"total_cost_usd":0.05,"session_id":"sess_123"}',
        ]
        stdout = "\n".join(lines)
        benchmark(_parse_cli_output, stdout, "", 0, 180)

    @pytest.mark.benchmark(group="claude_core")
    def test_parse_cli_output_error(self, benchmark):
        benchmark(_parse_cli_output, "", "Error: something went wrong", 1, 180)
