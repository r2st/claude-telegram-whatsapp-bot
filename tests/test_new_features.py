"""
Tests for the 10 new features inspired by top Claude projects.

Covers: cost_budget, smart_router, session_manager, two_agent,
event_bus, auto_scheduler, mcp_client, knowledge_base, browser_automation,
and the auto-memory/budget/routing hooks in telegram_bot.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# ─── Ensure test env ────────────────────────────────────────────────────────────
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")
os.environ.setdefault("ANTHROPIC_API_KEY", "")


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 2: Cost Budget System
# ═══════════════════════════════════════════════════════════════════════════════

class TestCostBudget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()
        # Create cost_tracking table that budget reads from
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS cost_tracking (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT,
                user_id TEXT,
                date TEXT DEFAULT (date('now')),
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0,
                requests INTEGER DEFAULT 1
            );
        """)
        conn.commit()
        conn.close()
        from telechat_pkg.cost_budget import BudgetManager
        self.mgr = BudgetManager(db_path=self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def _add_cost(self, platform, user_id, cost, date="date('now')"):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            f"INSERT INTO cost_tracking (platform, user_id, cost_usd, date) VALUES (?, ?, ?, {date})",
            (platform, user_id, cost),
        )
        conn.commit()
        conn.close()

    def test_check_no_usage(self):
        result = self.mgr.check("telegram", "123")
        self.assertIsNone(result)

    def test_check_within_budget(self):
        self._add_cost("telegram", "123", 1.0)
        result = self.mgr.check("telegram", "123")
        self.assertIsNone(result)

    def test_check_daily_exceeded(self):
        self.mgr.set_budget("telegram", "123", daily=2.0, monthly=50.0)
        self._add_cost("telegram", "123", 2.5)
        result = self.mgr.check("telegram", "123")
        self.assertIsNotNone(result)
        self.assertIn("Daily budget exceeded", result)

    def test_check_monthly_exceeded(self):
        self.mgr.set_budget("telegram", "123", daily=100.0, monthly=3.0)
        self._add_cost("telegram", "123", 3.5)
        result = self.mgr.check("telegram", "123")
        self.assertIsNotNone(result)
        self.assertIn("Monthly budget exceeded", result)

    def test_check_warning_threshold(self):
        self.mgr.set_budget("telegram", "123", daily=10.0, monthly=100.0)
        self._add_cost("telegram", "123", 8.5)  # 85% of daily
        result = self.mgr.check("telegram", "123")
        self.assertIsNotNone(result)
        self.assertIn("Budget warning", result)

    def test_warning_not_repeated(self):
        """Alert should only fire once until reset."""
        self.mgr.set_budget("telegram", "123", daily=10.0, monthly=100.0)
        self._add_cost("telegram", "123", 8.5)
        r1 = self.mgr.check("telegram", "123")
        self.assertIsNotNone(r1)
        r2 = self.mgr.check("telegram", "123")
        # Second check should not warn again (alert already sent)
        self.assertIsNone(r2)

    def test_set_budget(self):
        self.mgr.set_budget("telegram", "123", daily=7.0, monthly=70.0)
        budget = self.mgr._get_budget("telegram", "123")
        self.assertEqual(budget.daily_limit, 7.0)
        self.assertEqual(budget.monthly_limit, 70.0)

    def test_set_budget_partial(self):
        """Setting only daily should keep monthly default."""
        self.mgr.set_budget("telegram", "123", daily=3.0)
        budget = self.mgr._get_budget("telegram", "123")
        self.assertEqual(budget.daily_limit, 3.0)

    def test_usage_report(self):
        self._add_cost("telegram", "123", 2.0)
        self._add_cost("telegram", "123", 1.5)
        report = self.mgr.usage_report("telegram", "123")
        self.assertAlmostEqual(report.daily_cost, 3.5, places=1)
        self.assertEqual(report.daily_requests, 2)
        self.assertGreater(report.daily_pct, 0)

    def test_reset_daily_alerts(self):
        self.mgr.set_budget("telegram", "123", daily=10.0)
        self.mgr._mark_alert("telegram", "123", "daily")
        budget = self.mgr._get_budget("telegram", "123")
        self.assertTrue(budget.alert_sent_daily)
        self.mgr.reset_daily_alerts()
        budget = self.mgr._get_budget("telegram", "123")
        self.assertFalse(budget.alert_sent_daily)

    def test_default_budget_used(self):
        budget = self.mgr._get_budget("telegram", "unknown_user")
        from telechat_pkg.cost_budget import DEFAULT_DAILY_BUDGET, DEFAULT_MONTHLY_BUDGET
        self.assertEqual(budget.daily_limit, DEFAULT_DAILY_BUDGET)
        self.assertEqual(budget.monthly_limit, DEFAULT_MONTHLY_BUDGET)

    def test_check_handles_missing_table_gracefully(self):
        """If cost_tracking table doesn't exist, check should return None (not crash)."""
        tmp2 = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        path2 = tmp2.name
        tmp2.close()
        from telechat_pkg.cost_budget import BudgetManager
        mgr2 = BudgetManager(db_path=path2)
        # No cost_tracking table → should handle gracefully
        result = mgr2.check("telegram", "x")
        # Returns None because exception is caught
        self.assertIsNone(result)
        os.unlink(path2)


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 3: Smart Model Routing
# ═══════════════════════════════════════════════════════════════════════════════

class TestSmartRouter(unittest.TestCase):
    def test_simple_greeting(self):
        from telechat_pkg.smart_router import classify_complexity, route_model
        self.assertEqual(classify_complexity("hello"), "simple")
        self.assertEqual(route_model("hello"), "haiku")

    def test_simple_question(self):
        from telechat_pkg.smart_router import classify_complexity, route_model
        self.assertEqual(classify_complexity("What is Python?"), "simple")
        self.assertEqual(route_model("What is Python?"), "haiku")

    def test_moderate_task(self):
        from telechat_pkg.smart_router import classify_complexity
        result = classify_complexity(
            "Can you refactor this function to use async/await and handle errors properly?"
        )
        self.assertIn(result, ("moderate", "complex"))

    def test_complex_task(self):
        from telechat_pkg.smart_router import classify_complexity, route_model
        text = (
            "Design a distributed system with fault tolerance, implement the architecture "
            "with load balancing, analyze the trade-offs between consistency and availability, "
            "and build a comprehensive monitoring framework with alerting. Compare different "
            "approaches and evaluate their security implications."
        )
        result = classify_complexity(text)
        self.assertEqual(result, "complex")
        self.assertEqual(route_model(text), "opus")

    def test_very_short_query(self):
        from telechat_pkg.smart_router import classify_complexity
        self.assertEqual(classify_complexity("yes"), "simple")
        self.assertEqual(classify_complexity("ok"), "simple")

    def test_code_block_detection(self):
        from telechat_pkg.smart_router import classify_complexity
        text = "Fix this code:\n```python\ndef foo(): pass\n```"
        result = classify_complexity(text)
        self.assertIn(result, ("moderate", "complex"))

    def test_translate_is_simple(self):
        from telechat_pkg.smart_router import classify_complexity
        result = classify_complexity("translate hello to French")
        self.assertEqual(result, "simple")

    def test_route_model_api(self):
        from telechat_pkg.smart_router import route_model_api
        result = route_model_api("hello")
        self.assertIn("haiku", result)

    def test_route_model_api_complex(self):
        from telechat_pkg.smart_router import route_model_api
        result = route_model_api(
            "Design a distributed system with fault tolerance, implement the architecture "
            "with load balancing, analyze the trade-offs between consistency and availability, "
            "and build a comprehensive monitoring framework with alerting. Compare different "
            "approaches and evaluate their security implications."
        )
        self.assertIn("opus", result)

    def test_empty_string(self):
        from telechat_pkg.smart_router import classify_complexity
        self.assertEqual(classify_complexity(""), "simple")

    def test_moderate_word_count(self):
        from telechat_pkg.smart_router import classify_complexity
        # A longer query without strong complexity signals
        text = " ".join(["word"] * 60)
        result = classify_complexity(text)
        self.assertEqual(result, "moderate")

    def test_debug_pattern(self):
        from telechat_pkg.smart_router import classify_complexity
        text = "debug this issue with the authentication system and troubleshoot the login flow"
        result = classify_complexity(text)
        self.assertIn(result, ("moderate", "complex"))


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 4: Session Resume/Fork
# ═══════════════════════════════════════════════════════════════════════════════

class TestSessionBrowser(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT,
                user_id TEXT,
                user_text TEXT,
                bot_reply TEXT,
                timestamp REAL,
                session_name TEXT,
                cost_usd REAL
            );
        """)
        # Insert test data
        now = time.time()
        for i in range(5):
            conn.execute(
                "INSERT INTO history (platform, user_id, user_text, bot_reply, timestamp, session_name) VALUES (?,?,?,?,?,?)",
                ("telegram", "123", f"Question {i}", f"Answer {i}", now - (5 - i) * 60, "project-alpha"),
            )
        for i in range(3):
            conn.execute(
                "INSERT INTO history (platform, user_id, user_text, bot_reply, timestamp, session_name) VALUES (?,?,?,?,?,?)",
                ("telegram", "123", f"Bug report {i}", f"Fix {i}", now - (3 - i) * 30, "bugfix-session"),
            )
        conn.commit()
        conn.close()

        from telechat_pkg.session_manager import SessionBrowser
        self.browser = SessionBrowser(db_path=self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def test_list_sessions(self):
        sessions = self.browser.list_sessions("telegram", "123")
        self.assertEqual(len(sessions), 2)
        # Most recent first
        self.assertEqual(sessions[0].name, "bugfix-session")
        self.assertEqual(sessions[1].name, "project-alpha")

    def test_list_sessions_with_preview(self):
        sessions = self.browser.list_sessions("telegram", "123", include_preview=True)
        self.assertTrue(any(s.preview for s in sessions))

    def test_list_sessions_without_preview(self):
        sessions = self.browser.list_sessions("telegram", "123", include_preview=False)
        self.assertTrue(all(s.preview == "" for s in sessions))

    def test_session_message_counts(self):
        sessions = self.browser.list_sessions("telegram", "123")
        alpha = next(s for s in sessions if s.name == "project-alpha")
        bugfix = next(s for s in sessions if s.name == "bugfix-session")
        # Each legacy history row migrates to a user+assistant pair, so
        # 5 exchanges → 10 message rows (matches test_get_session_history below).
        self.assertEqual(alpha.message_count, 10)
        self.assertEqual(bugfix.message_count, 6)

    def test_get_session_history(self):
        history = self.browser.get_session_history("telegram", "123", "project-alpha")
        self.assertEqual(len(history), 10)  # 5 user + 5 assistant
        self.assertEqual(history[0]["role"], "user")
        self.assertEqual(history[1]["role"], "assistant")

    def test_fork_session(self):
        result = self.browser.fork_session("telegram", "123", "project-alpha", "alpha-v2")
        self.assertTrue(result.success)
        # 5 legacy exchanges → 10 conversation rows copied.
        self.assertEqual(result.messages_copied, 10)
        self.assertEqual(result.new_session_name, "alpha-v2")
        # Verify fork exists
        sessions = self.browser.list_sessions("telegram", "123")
        names = [s.name for s in sessions]
        self.assertIn("alpha-v2", names)

    def test_fork_auto_name(self):
        result = self.browser.fork_session("telegram", "123", "project-alpha")
        self.assertTrue(result.success)
        self.assertTrue(result.new_session_name.startswith("project-alpha-fork-"))

    def test_fork_nonexistent_session(self):
        result = self.browser.fork_session("telegram", "123", "nonexistent")
        self.assertFalse(result.success)
        self.assertIn("not found", result.error)

    def test_search_sessions(self):
        results = self.browser.search_sessions("telegram", "123", "Bug report")
        self.assertTrue(any(s.name == "bugfix-session" for s in results))

    def test_search_sessions_no_match(self):
        results = self.browser.search_sessions("telegram", "123", "zzzznonexistent")
        self.assertEqual(len(results), 0)

    def test_list_no_sessions(self):
        sessions = self.browser.list_sessions("telegram", "999")
        self.assertEqual(len(sessions), 0)

    def test_list_sessions_limit(self):
        sessions = self.browser.list_sessions("telegram", "123", limit=1)
        self.assertEqual(len(sessions), 1)


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 5: Two-Agent Pattern
# ═══════════════════════════════════════════════════════════════════════════════

class TestTwoAgent(unittest.TestCase):
    def test_should_use_two_agent_simple(self):
        from telechat_pkg.two_agent import should_use_two_agent
        self.assertFalse(should_use_two_agent("What is Python?"))

    def test_should_use_two_agent_complex(self):
        from telechat_pkg.two_agent import should_use_two_agent
        text = (
            "First, build a REST API with authentication. After that, create "
            "a frontend with React. Then implement the database layer. Finally, "
            "deploy the complete application to AWS with CI/CD."
        )
        self.assertTrue(should_use_two_agent(text))

    def test_step_dataclass(self):
        from telechat_pkg.two_agent import Step
        step = Step(id=1, action="Build API", context="REST endpoints")
        self.assertEqual(step.status, "pending")
        self.assertEqual(step.id, 1)

    def test_task_plan_dataclass(self):
        from telechat_pkg.two_agent import TaskPlan, Step
        plan = TaskPlan(
            task_summary="Build app",
            steps=[Step(id=1, action="Setup", context="Init project")],
        )
        self.assertEqual(plan.status, "planned")
        self.assertEqual(len(plan.steps), 1)

    def test_format_plan(self):
        from telechat_pkg.two_agent import TwoAgentExecutor, TaskPlan, Step
        executor = TwoAgentExecutor()
        plan = TaskPlan(
            task_summary="Build REST API",
            steps=[
                Step(id=1, action="Set up project", context="", status="done", duration=2.5),
                Step(id=2, action="Add endpoints", context="", status="running"),
                Step(id=3, action="Test", context="", status="pending"),
            ],
        )
        formatted = executor.format_plan(plan)
        self.assertIn("Build REST API", formatted)
        self.assertIn("Set up project", formatted)

    def test_format_result(self):
        from telechat_pkg.two_agent import TwoAgentExecutor, TaskPlan, Step
        executor = TwoAgentExecutor()
        plan = TaskPlan(
            task_summary="Build app",
            steps=[Step(id=1, action="Init", context="", status="done", result="Project initialized")],
            created_at=time.time() - 5,
            completed_at=time.time(),
        )
        formatted = executor.format_result(plan)
        self.assertIn("Project initialized", formatted)
        self.assertIn("Completed 1 steps", formatted)

    @patch("telechat_pkg.two_agent.TwoAgentExecutor._call_claude")
    def test_plan_parses_json(self, mock_call):
        from telechat_pkg.two_agent import TwoAgentExecutor
        mock_call.return_value = json.dumps({
            "task_summary": "Build API",
            "steps": [
                {"id": 1, "action": "Setup", "context": "Init", "complexity": "simple"},
                {"id": 2, "action": "Build", "context": "Code", "complexity": "moderate"},
            ]
        })
        executor = TwoAgentExecutor()
        plan = asyncio.run(executor.plan("Build an API"))
        self.assertEqual(len(plan.steps), 2)
        self.assertEqual(plan.task_summary, "Build API")

    @patch("telechat_pkg.two_agent.TwoAgentExecutor._call_claude")
    def test_plan_handles_invalid_json(self, mock_call):
        from telechat_pkg.two_agent import TwoAgentExecutor
        mock_call.return_value = "This is not JSON"
        executor = TwoAgentExecutor()
        plan = asyncio.run(executor.plan("Do something"))
        # Should fallback to single step
        self.assertEqual(len(plan.steps), 1)

    @patch("telechat_pkg.two_agent.TwoAgentExecutor._call_claude")
    def test_execute_steps(self, mock_call):
        from telechat_pkg.two_agent import TwoAgentExecutor, TaskPlan, Step
        mock_call.return_value = "Step completed successfully"
        executor = TwoAgentExecutor()
        plan = TaskPlan(
            task_summary="Test task",
            steps=[
                Step(id=1, action="Step one", context="", complexity="simple"),
                Step(id=2, action="Step two", context="", complexity="moderate"),
            ],
            created_at=time.time(),
        )
        result = asyncio.run(executor.execute(plan))
        self.assertEqual(result.status, "done")
        self.assertTrue(all(s.status == "done" for s in result.steps))

    @patch("telechat_pkg.two_agent.TwoAgentExecutor._call_claude")
    def test_execute_with_callbacks(self, mock_call):
        from telechat_pkg.two_agent import TwoAgentExecutor, TaskPlan, Step
        mock_call.return_value = "OK"
        executor = TwoAgentExecutor()
        plan = TaskPlan(
            task_summary="Test",
            steps=[Step(id=1, action="Do it", context="")],
            created_at=time.time(),
        )
        started = []
        done = []
        result = asyncio.run(executor.execute(
            plan,
            on_step_start=AsyncMock(side_effect=lambda s: started.append(s.id)),
            on_step_done=AsyncMock(side_effect=lambda s: done.append(s.id)),
        ))
        self.assertEqual(started, [1])
        self.assertEqual(done, [1])

    @patch("telechat_pkg.two_agent.TwoAgentExecutor._call_claude")
    def test_execute_handles_failure(self, mock_call):
        from telechat_pkg.two_agent import TwoAgentExecutor, TaskPlan, Step
        mock_call.side_effect = Exception("API error")
        executor = TwoAgentExecutor()
        plan = TaskPlan(
            task_summary="Test",
            steps=[Step(id=1, action="Fail", context="")],
            created_at=time.time(),
        )
        result = asyncio.run(executor.execute(plan))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.steps[0].status, "failed")

    @patch("telechat_pkg.two_agent.TwoAgentExecutor._call_claude")
    def test_run_end_to_end(self, mock_call):
        from telechat_pkg.two_agent import TwoAgentExecutor
        # First call is plan, rest are execution
        mock_call.side_effect = [
            json.dumps({"task_summary": "T", "steps": [{"id": 1, "action": "A", "context": "C"}]}),
            "Result of step 1",
        ]
        executor = TwoAgentExecutor()
        plan = asyncio.run(executor.run("Do something complex"))
        self.assertEqual(plan.status, "done")

    def test_should_use_two_agent_disabled(self):
        from telechat_pkg.two_agent import should_use_two_agent
        with patch("telechat_pkg.two_agent.TWO_AGENT_ENABLED", False):
            self.assertFalse(should_use_two_agent("First build, then test, finally deploy"))


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 6: Event Bus
# ═══════════════════════════════════════════════════════════════════════════════

class TestEventBus(unittest.TestCase):
    def test_publish_and_subscribe(self):
        from telechat_pkg.event_bus import EventBus, Event
        bus = EventBus()
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe("test.event", handler)
        asyncio.run(bus.publish(Event(type="test.event", data={"key": "value"})))
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].data["key"], "value")

    def test_wildcard_subscriber(self):
        from telechat_pkg.event_bus import EventBus, Event
        bus = EventBus()
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe("*", handler)
        asyncio.run(bus.publish(Event(type="any.event")))
        self.assertEqual(len(received), 1)

    def test_prefix_matching(self):
        from telechat_pkg.event_bus import EventBus, Event
        bus = EventBus()
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe("webhook.*", handler)
        asyncio.run(bus.publish(Event(type="webhook.github")))
        asyncio.run(bus.publish(Event(type="chat.message")))
        self.assertEqual(len(received), 1)

    def test_unsubscribe(self):
        from telechat_pkg.event_bus import EventBus, Event
        bus = EventBus()
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe("test", handler)
        bus.unsubscribe("test", handler)
        asyncio.run(bus.publish(Event(type="test")))
        self.assertEqual(len(received), 0)

    def test_event_auto_fields(self):
        from telechat_pkg.event_bus import Event
        e = Event(type="test")
        self.assertGreater(e.timestamp, 0)
        self.assertTrue(e.id.startswith("test:"))

    def test_handler_exception_doesnt_break_others(self):
        from telechat_pkg.event_bus import EventBus, Event
        bus = EventBus()
        received = []

        async def bad_handler(event):
            raise ValueError("oops")

        async def good_handler(event):
            received.append(event)

        bus.subscribe("test", bad_handler)
        bus.subscribe("test", good_handler)
        asyncio.run(bus.publish(Event(type="test")))
        self.assertEqual(len(received), 1)

    def test_recent_events(self):
        from telechat_pkg.event_bus import EventBus, Event
        bus = EventBus()
        for i in range(5):
            asyncio.run(bus.publish(Event(type="test", data={"i": i})))
        recent = bus.recent_events("test", limit=3)
        self.assertEqual(len(recent), 3)

    def test_recent_events_all_types(self):
        from telechat_pkg.event_bus import EventBus, Event
        bus = EventBus()
        asyncio.run(bus.publish(Event(type="a")))
        asyncio.run(bus.publish(Event(type="b")))
        recent = bus.recent_events(limit=10)
        self.assertEqual(len(recent), 2)

    def test_event_types_constants(self):
        from telechat_pkg.event_bus import EventTypes
        self.assertEqual(EventTypes.MESSAGE_RECEIVED, "chat.message_received")
        self.assertEqual(EventTypes.WEBHOOK_GITHUB, "webhook.github")

    def test_webhook_receiver_github(self):
        from telechat_pkg.event_bus import EventBus, WebhookReceiver
        bus = EventBus()
        receiver = WebhookReceiver(bus)
        event = asyncio.run(receiver.handle_github({"ref": "main"}, "push"))
        self.assertEqual(event.type, "webhook.github")
        self.assertEqual(event.source, "github")

    def test_webhook_receiver_generic(self):
        from telechat_pkg.event_bus import EventBus, WebhookReceiver
        bus = EventBus()
        receiver = WebhookReceiver(bus)
        event = asyncio.run(receiver.handle_generic({"foo": "bar"}, "ci"))
        self.assertEqual(event.type, "webhook.generic")
        self.assertEqual(event.source, "ci")

    def test_webhook_verify_bearer(self):
        from telechat_pkg.event_bus import EventBus, WebhookReceiver
        bus = EventBus()
        receiver = WebhookReceiver(bus, bearer_token="secret123")
        self.assertTrue(receiver.verify_bearer("Bearer secret123"))
        self.assertFalse(receiver.verify_bearer("Bearer wrong"))

    def test_webhook_verify_bearer_no_token(self):
        from telechat_pkg.event_bus import EventBus, WebhookReceiver
        bus = EventBus()
        receiver = WebhookReceiver(bus)
        self.assertTrue(receiver.verify_bearer("anything"))

    def test_get_event_bus_singleton(self):
        from telechat_pkg.event_bus import get_event_bus
        bus1 = get_event_bus()
        bus2 = get_event_bus()
        self.assertIs(bus1, bus2)

    def test_publish_async_queue(self):
        from telechat_pkg.event_bus import EventBus, Event
        bus = EventBus(max_queue=5)
        asyncio.run(bus.publish_async(Event(type="queued")))
        self.assertEqual(bus._queue.qsize(), 1)

    def test_publish_async_full_queue(self):
        from telechat_pkg.event_bus import EventBus, Event
        bus = EventBus(max_queue=1)
        asyncio.run(bus.publish_async(Event(type="a")))
        # This should not raise — it drops silently
        asyncio.run(bus.publish_async(Event(type="b")))

    def test_start_stop(self):
        from telechat_pkg.event_bus import EventBus
        bus = EventBus()
        asyncio.run(bus.start())
        self.assertTrue(bus._running)
        asyncio.run(bus.stop())
        self.assertFalse(bus._running)


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 7: Auto Scheduler
# ═══════════════════════════════════════════════════════════════════════════════

class TestAutoScheduler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()
        from telechat_pkg.auto_scheduler import AutoScheduler
        self.sched = AutoScheduler(db_path=self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def test_parse_interval_minutes(self):
        from telechat_pkg.auto_scheduler import parse_interval
        self.assertEqual(parse_interval("every 5 minutes"), 300)
        self.assertEqual(parse_interval("every 30 min"), 1800)

    def test_parse_interval_hours(self):
        from telechat_pkg.auto_scheduler import parse_interval
        self.assertEqual(parse_interval("every 2 hours"), 7200)
        self.assertEqual(parse_interval("hourly"), 3600)

    def test_parse_interval_daily(self):
        from telechat_pkg.auto_scheduler import parse_interval
        self.assertEqual(parse_interval("daily"), 86400)
        self.assertEqual(parse_interval("every day"), 86400)

    def test_parse_interval_weekly(self):
        from telechat_pkg.auto_scheduler import parse_interval
        self.assertEqual(parse_interval("weekly"), 604800)

    def test_parse_interval_half_hour(self):
        from telechat_pkg.auto_scheduler import parse_interval
        self.assertEqual(parse_interval("every half hour"), 1800)

    def test_parse_interval_twice_a_day(self):
        from telechat_pkg.auto_scheduler import parse_interval
        self.assertEqual(parse_interval("twice a day"), 43200)

    def test_parse_interval_seconds(self):
        from telechat_pkg.auto_scheduler import parse_interval
        self.assertEqual(parse_interval("every 30 seconds"), 30)

    def test_parse_interval_no_match(self):
        from telechat_pkg.auto_scheduler import parse_interval
        self.assertIsNone(parse_interval("do something"))

    def test_parse_interval_one_shot(self):
        from telechat_pkg.auto_scheduler import parse_interval
        self.assertEqual(parse_interval("in 10 minutes"), 600)
        self.assertEqual(parse_interval("once in 2 hours"), 7200)

    def test_parse_schedule_request(self):
        from telechat_pkg.auto_scheduler import parse_schedule_request
        result = parse_schedule_request("remind me to check deploys every 2 hours")
        self.assertIsNotNone(result)
        self.assertEqual(result["interval"], 7200)
        self.assertIn("check deploys", result["description"])

    def test_parse_schedule_one_shot(self):
        from telechat_pkg.auto_scheduler import parse_schedule_request
        result = parse_schedule_request("remind me once in 30 minutes to take a break")
        self.assertIsNotNone(result)
        self.assertEqual(result["max_runs"], 1)

    def test_parse_schedule_no_match(self):
        from telechat_pkg.auto_scheduler import parse_schedule_request
        result = parse_schedule_request("just do something")
        self.assertIsNone(result)

    def test_create_task(self):
        task = self.sched.create_task("telegram", "123", "test", "check status", 3600)
        self.assertGreater(task.id, 0)
        self.assertEqual(task.interval_seconds, 3600)

    def test_list_tasks(self):
        self.sched.create_task("telegram", "123", "task1", "p1", 3600)
        self.sched.create_task("telegram", "123", "task2", "p2", 7200)
        tasks = self.sched.list_tasks("telegram", "123")
        self.assertEqual(len(tasks), 2)

    def test_delete_task(self):
        task = self.sched.create_task("telegram", "123", "test", "p", 3600)
        self.assertTrue(self.sched.delete_task(task.id, "telegram", "123"))
        tasks = self.sched.list_tasks("telegram", "123")
        self.assertEqual(len(tasks), 0)

    def test_delete_wrong_user(self):
        task = self.sched.create_task("telegram", "123", "test", "p", 3600)
        self.assertFalse(self.sched.delete_task(task.id, "telegram", "999"))

    def test_parse_and_create(self):
        task = self.sched.parse_and_create("telegram", "123", "check status every 5 minutes")
        self.assertIsNotNone(task)
        self.assertEqual(task.interval_seconds, 300)

    def test_parse_and_create_no_match(self):
        task = self.sched.parse_and_create("telegram", "123", "hello world")
        self.assertIsNone(task)

    def test_get_due_tasks(self):
        task = self.sched.create_task("telegram", "123", "due", "p", 1)
        # Task next_run is ~now+1s, wait or manually set
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE auto_scheduled_tasks SET next_run = ? WHERE id = ?", (time.time() - 1, task.id))
        conn.commit()
        conn.close()
        # Refresh sched connection
        self.sched._local = __import__("threading").local()
        due = self.sched.get_due_tasks()
        self.assertGreaterEqual(len(due), 1)

    def test_mark_run(self):
        task = self.sched.create_task("telegram", "123", "test", "p", 3600)
        self.sched.mark_run(task.id)
        tasks = self.sched.list_tasks("telegram", "123")
        self.assertEqual(tasks[0].run_count, 1)

    def test_mark_run_exhausted(self):
        task = self.sched.create_task("telegram", "123", "test", "p", 3600, max_runs=1)
        self.sched.mark_run(task.id)
        tasks = self.sched.list_tasks("telegram", "123")
        self.assertEqual(len(tasks), 0)  # disabled after max_runs

    def test_mark_run_nonexistent(self):
        # Should not raise
        self.sched.mark_run(99999)

    def test_format_task_list_empty(self):
        result = self.sched.format_task_list([])
        self.assertIn("No scheduled tasks", result)

    def test_format_task_list(self):
        self.sched.create_task("telegram", "123", "Check deploys", "check", 7200)
        tasks = self.sched.list_tasks("telegram", "123")
        result = self.sched.format_task_list(tasks)
        self.assertIn("Check deploys", result)
        self.assertIn("2h", result)

    def test_format_interval(self):
        from telechat_pkg.auto_scheduler import _format_interval
        self.assertEqual(_format_interval(30), "30s")
        self.assertEqual(_format_interval(300), "5m")
        self.assertEqual(_format_interval(7200), "2h")
        self.assertEqual(_format_interval(172800), "2d")

    def test_auto_task_is_due(self):
        from telechat_pkg.auto_scheduler import AutoTask
        t = AutoTask(id=1, platform="t", user_id="1", description="x", prompt="x",
                     interval_seconds=60, next_run=time.time() - 1)
        self.assertTrue(t.is_due)

    def test_auto_task_not_due(self):
        from telechat_pkg.auto_scheduler import AutoTask
        t = AutoTask(id=1, platform="t", user_id="1", description="x", prompt="x",
                     interval_seconds=60, next_run=time.time() + 1000)
        self.assertFalse(t.is_due)

    def test_auto_task_is_exhausted(self):
        from telechat_pkg.auto_scheduler import AutoTask
        t = AutoTask(id=1, platform="t", user_id="1", description="x", prompt="x",
                     interval_seconds=60, max_runs=3, run_count=3)
        self.assertTrue(t.is_exhausted)

    def test_auto_task_not_exhausted_unlimited(self):
        from telechat_pkg.auto_scheduler import AutoTask
        t = AutoTask(id=1, platform="t", user_id="1", description="x", prompt="x",
                     interval_seconds=60, max_runs=0, run_count=100)
        self.assertFalse(t.is_exhausted)

    def test_start_stop(self):
        asyncio.run(self.sched.start())
        self.assertTrue(self.sched._running)
        asyncio.run(self.sched.stop())
        self.assertFalse(self.sched._running)

    def test_start_disabled(self):
        with patch("telechat_pkg.auto_scheduler.AUTO_SCHEDULER_ENABLED", False):
            from telechat_pkg.auto_scheduler import AutoScheduler
            s = AutoScheduler(db_path=self.db_path)
            asyncio.run(s.start())
            self.assertFalse(s._running)


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 8: MCP Client
# ═══════════════════════════════════════════════════════════════════════════════

class TestMCPClient(unittest.TestCase):
    def test_add_server(self):
        from telechat_pkg.mcp_client import MCPManager
        mgr = MCPManager()
        mgr.add_server("test", {"command": "echo", "args": ["hello"]})
        servers = mgr.list_servers()
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]["name"], "test")
        self.assertEqual(servers[0]["status"], "disconnected")

    def test_remove_server(self):
        from telechat_pkg.mcp_client import MCPManager
        mgr = MCPManager()
        mgr.add_server("test", {"command": "echo"})
        mgr.remove_server("test")
        self.assertEqual(len(mgr.list_servers()), 0)

    def test_list_tools_empty(self):
        from telechat_pkg.mcp_client import MCPManager
        mgr = MCPManager()
        self.assertEqual(mgr.list_tools(), [])

    def test_get_tools_for_prompt_empty(self):
        from telechat_pkg.mcp_client import MCPManager
        mgr = MCPManager()
        self.assertEqual(mgr.get_tools_for_prompt(), "")

    def test_get_tools_for_prompt_with_tools(self):
        from telechat_pkg.mcp_client import MCPManager, MCPTool
        mgr = MCPManager()
        mgr._tools_cache["fs.read"] = MCPTool(name="read", description="Read a file", server="fs")
        result = mgr.get_tools_for_prompt()
        self.assertIn("fs.read", result)
        self.assertIn("Read a file", result)

    def test_mcp_tool_dataclass(self):
        from telechat_pkg.mcp_client import MCPTool
        tool = MCPTool(name="read", description="Read file", server="fs", input_schema={"type": "object"})
        self.assertEqual(tool.name, "read")
        self.assertEqual(tool.server, "fs")

    def test_mcp_server_dataclass(self):
        from telechat_pkg.mcp_client import MCPServer
        server = MCPServer(name="fs", command="npx", args=["-y", "mcp-fs"])
        self.assertEqual(server.status, "disconnected")
        self.assertEqual(server.tools, [])

    def test_call_tool_not_connected(self):
        from telechat_pkg.mcp_client import MCPManager
        mgr = MCPManager()
        mgr.add_server("test", {"command": "echo"})
        result = asyncio.run(mgr.call_tool("test", "read", {}))
        self.assertIn("error", result)

    def test_call_tool_unknown_server(self):
        from telechat_pkg.mcp_client import MCPManager
        mgr = MCPManager()
        result = asyncio.run(mgr.call_tool("nonexistent", "read", {}))
        self.assertIn("error", result)

    def test_get_mcp_manager_singleton(self):
        from telechat_pkg.mcp_client import get_mcp_manager
        m1 = get_mcp_manager()
        m2 = get_mcp_manager()
        self.assertIs(m1, m2)

    def test_load_config_no_file(self):
        from telechat_pkg.mcp_client import MCPManager
        mgr = MCPManager()
        # Should not crash when no config file
        self.assertEqual(len(mgr._servers), 0)

    def test_load_config_from_file(self):
        from telechat_pkg.mcp_client import MCPManager
        config = {"mcpServers": {"fs": {"command": "npx", "args": ["-y", "mcp-fs"]}}}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config, f)
            f.flush()
            with patch("telechat_pkg.mcp_client.MCP_CONFIG_FILE", f.name):
                mgr = MCPManager()
                mgr._load_config()
        servers = mgr.list_servers()
        self.assertTrue(any(s["name"] == "fs" for s in servers))
        os.unlink(f.name)

    def test_disconnect_not_connected(self):
        from telechat_pkg.mcp_client import MCPManager
        mgr = MCPManager()
        mgr.add_server("test", {"command": "echo"})
        # Should not raise
        asyncio.run(mgr.disconnect("test"))

    def test_disconnect_all(self):
        from telechat_pkg.mcp_client import MCPManager
        mgr = MCPManager()
        mgr.add_server("a", {"command": "echo"})
        mgr.add_server("b", {"command": "echo"})
        asyncio.run(mgr.disconnect_all())


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 9: Knowledge Base / RAG
# ═══════════════════════════════════════════════════════════════════════════════

class TestKnowledgeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()
        from telechat_pkg.knowledge_base import KnowledgeBase
        self.kb = KnowledgeBase(db_path=self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def test_chunk_text_short(self):
        chunks = self.kb.chunk_text("Hello world", chunk_size=1000)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], "Hello world")

    def test_chunk_text_long(self):
        text = "First sentence. " * 200
        chunks = self.kb.chunk_text(text, chunk_size=200, overlap=50)
        self.assertGreater(len(chunks), 1)
        # Each chunk should be reasonable size
        for c in chunks:
            self.assertLessEqual(len(c), 250)  # some slack for sentence boundaries

    def test_ingest_text(self):
        doc = self.kb.ingest_text("telegram", "123", "Test Doc", "This is test content about Python programming.")
        self.assertEqual(doc.title, "Test Doc")
        self.assertGreater(doc.chunk_count, 0)

    def test_ingest_text_dedup(self):
        content = "Exact same content"
        d1 = self.kb.ingest_text("telegram", "123", "Doc 1", content)
        d2 = self.kb.ingest_text("telegram", "123", "Doc 2", content)
        self.assertEqual(d1.id, d2.id)  # Same content hash → same doc

    def test_ingest_text_with_tags(self):
        doc = self.kb.ingest_text("telegram", "123", "API Docs", "Content", tags=["api", "docs"])
        self.assertEqual(doc.tags, ["api", "docs"])

    def test_search_fts(self):
        self.kb.ingest_text("telegram", "123", "Python Guide", "Python is a programming language used for web development and data science.")
        self.kb.ingest_text("telegram", "123", "Java Guide", "Java is a programming language used for enterprise applications.")
        results = self.kb.search("telegram", "123", "Python web development")
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0].document.title, "Python Guide")

    def test_search_no_results(self):
        results = self.kb.search("telegram", "123", "nonexistent topic xyz")
        self.assertEqual(len(results), 0)

    def test_build_context(self):
        self.kb.ingest_text("telegram", "123", "Auth Docs", "To authenticate, use Bearer tokens in the Authorization header.")
        context = self.kb.build_context("telegram", "123", "authenticate Bearer")
        self.assertIn("Knowledge Base Context", context)
        self.assertIn("Bearer tokens", context)

    def test_build_context_empty(self):
        context = self.kb.build_context("telegram", "123", "random query")
        self.assertEqual(context, "")

    def test_build_context_disabled(self):
        self.kb.ingest_text("telegram", "123", "Doc", "Content about authentication")
        with patch("telechat_pkg.knowledge_base.KB_ENABLED", False):
            context = self.kb.build_context("telegram", "123", "authentication")
            self.assertEqual(context, "")

    def test_list_documents(self):
        self.kb.ingest_text("telegram", "123", "Doc 1", "Content 1")
        self.kb.ingest_text("telegram", "123", "Doc 2", "Content 2 different")
        docs = self.kb.list_documents("telegram", "123")
        self.assertEqual(len(docs), 2)

    def test_delete_document(self):
        doc = self.kb.ingest_text("telegram", "123", "To Delete", "Temporary content")
        self.assertTrue(self.kb.delete_document("telegram", "123", doc.id))
        docs = self.kb.list_documents("telegram", "123")
        self.assertEqual(len(docs), 0)

    def test_delete_nonexistent(self):
        self.assertFalse(self.kb.delete_document("telegram", "123", "fake-id"))

    def test_stats(self):
        self.kb.ingest_text("telegram", "123", "Doc", "Some content for stats testing purposes")
        s = self.kb.stats("telegram", "123")
        self.assertEqual(s["documents"], 1)
        self.assertGreater(s["chunks"], 0)

    def test_stats_empty(self):
        s = self.kb.stats("telegram", "123")
        self.assertEqual(s["documents"], 0)
        self.assertEqual(s["chunks"], 0)

    def test_ingest_file_txt(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("# Test Document\n\nThis is a markdown document for testing the knowledge base.")
            f.flush()
            doc = self.kb.ingest_file("telegram", "123", f.name)
        self.assertIsNotNone(doc)
        self.assertIn(".md", doc.title)
        os.unlink(f.name)

    def test_ingest_file_nonexistent(self):
        doc = self.kb.ingest_file("telegram", "123", "/nonexistent/file.txt")
        self.assertIsNone(doc)

    def test_ingest_file_unsupported(self):
        with tempfile.NamedTemporaryFile(suffix=".xyz", delete=False) as f:
            f.write(b"binary data")
            f.flush()
            doc = self.kb.ingest_file("telegram", "123", f.name)
        self.assertIsNone(doc)
        os.unlink(f.name)

    def test_ingest_file_empty(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("")
            f.flush()
            doc = self.kb.ingest_file("telegram", "123", f.name)
        self.assertIsNone(doc)
        os.unlink(f.name)

    def test_search_like_fallback(self):
        """When FTS is somehow unavailable, LIKE fallback should work."""
        self.kb.ingest_text("telegram", "123", "Guide", "Python programming language guide for beginners")
        # Force no FTS
        self.kb._fts_ok = False
        results = self.kb.search("telegram", "123", "Python")
        self.assertGreater(len(results), 0)

    def test_extract_pdf_no_pypdf(self):
        from telechat_pkg.knowledge_base import KnowledgeBase
        with patch.dict("sys.modules", {"pypdf": None}):
            result = KnowledgeBase._extract_pdf(Path("/fake/file.pdf"))
            # Should return empty string when pypdf not available
            self.assertEqual(result, "")


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 10: Browser Automation
# ═══════════════════════════════════════════════════════════════════════════════

class TestBrowserAutomation(unittest.TestCase):
    def test_browser_result_dataclass(self):
        from telechat_pkg.browser_automation import BrowserResult
        r = BrowserResult(success=True, url="https://example.com", title="Example", duration=1.5)
        self.assertTrue(r.success)
        self.assertEqual(r.url, "https://example.com")

    def test_browser_result_error(self):
        from telechat_pkg.browser_automation import BrowserResult
        r = BrowserResult(success=False, error="Timeout", duration=30.0)
        self.assertFalse(r.success)
        self.assertEqual(r.error, "Timeout")

    def test_page_info_dataclass(self):
        from telechat_pkg.browser_automation import PageInfo
        p = PageInfo(url="https://example.com", title="Example", text_content="Hello")
        self.assertEqual(p.url, "https://example.com")
        self.assertEqual(p.links, [])

    def test_get_browser_agent_singleton(self):
        from telechat_pkg.browser_automation import get_browser_agent
        a1 = get_browser_agent()
        a2 = get_browser_agent()
        self.assertIs(a1, a2)

    def test_agent_not_started(self):
        from telechat_pkg.browser_automation import BrowserAgent
        agent = BrowserAgent()
        self.assertFalse(agent._started)

    def test_screenshot_no_playwright(self):
        from telechat_pkg.browser_automation import BrowserAgent
        agent = BrowserAgent()
        with patch.dict("sys.modules", {"playwright": None, "playwright.async_api": None}):
            with self.assertRaises(Exception):
                asyncio.run(agent.screenshot("https://example.com"))

    @patch("telechat_pkg.browser_automation.BrowserAgent._ensure_started")
    def test_screenshot_mock(self, mock_start):
        from telechat_pkg.browser_automation import BrowserAgent
        agent = BrowserAgent()
        agent._started = True
        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.wait_for_load_state = AsyncMock()
        mock_page.screenshot = AsyncMock()
        mock_page.title = AsyncMock(return_value="Test Page")
        mock_page.close = AsyncMock()

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        agent._context = mock_context

        result = asyncio.run(agent.screenshot("https://example.com"))
        self.assertTrue(result.success)
        self.assertEqual(result.title, "Test Page")

    @patch("telechat_pkg.browser_automation.BrowserAgent._ensure_started")
    def test_extract_text_mock(self, mock_start):
        from telechat_pkg.browser_automation import BrowserAgent
        agent = BrowserAgent()
        agent._started = True
        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.wait_for_load_state = AsyncMock()
        mock_page.text_content = AsyncMock(return_value="Page content here")
        mock_page.title = AsyncMock(return_value="Title")
        mock_page.url = "https://example.com"
        mock_page.eval_on_selector_all = AsyncMock(return_value=[])
        mock_page.close = AsyncMock()

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        agent._context = mock_context

        result = asyncio.run(agent.extract_text("https://example.com"))
        self.assertTrue(result.success)
        self.assertEqual(result.data.text_content, "Page content here")

    @patch("telechat_pkg.browser_automation.BrowserAgent._ensure_started")
    def test_fill_form_mock(self, mock_start):
        from telechat_pkg.browser_automation import BrowserAgent
        agent = BrowserAgent()
        agent._started = True
        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.wait_for_load_state = AsyncMock()
        mock_page.fill = AsyncMock()
        mock_page.screenshot = AsyncMock()
        mock_page.title = AsyncMock(return_value="Form")
        mock_page.close = AsyncMock()

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        agent._context = mock_context

        result = asyncio.run(agent.fill_form("https://example.com", {"#name": "John"}))
        self.assertTrue(result.success)
        self.assertEqual(result.data["filled"], ["#name"])

    @patch("telechat_pkg.browser_automation.BrowserAgent._ensure_started")
    def test_fill_form_with_submit(self, mock_start):
        from telechat_pkg.browser_automation import BrowserAgent
        agent = BrowserAgent()
        agent._started = True
        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.wait_for_load_state = AsyncMock()
        mock_page.fill = AsyncMock()
        mock_page.click = AsyncMock()
        mock_page.screenshot = AsyncMock()
        mock_page.title = AsyncMock(return_value="Form")
        mock_page.close = AsyncMock()

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        agent._context = mock_context

        result = asyncio.run(agent.fill_form("https://example.com", {"#name": "J"}, submit=True))
        self.assertTrue(result.success)
        mock_page.click.assert_called_once()

    @patch("telechat_pkg.browser_automation.BrowserAgent._ensure_started")
    def test_run_script_mock(self, mock_start):
        from telechat_pkg.browser_automation import BrowserAgent
        agent = BrowserAgent()
        agent._started = True
        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.wait_for_load_state = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value=42)
        mock_page.title = AsyncMock(return_value="Test")
        mock_page.close = AsyncMock()

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        agent._context = mock_context

        result = asyncio.run(agent.run_script("https://example.com", "1 + 1"))
        self.assertTrue(result.success)
        self.assertEqual(result.data, 42)

    @patch("telechat_pkg.browser_automation.BrowserAgent._ensure_started")
    def test_get_page_info_mock(self, mock_start):
        from telechat_pkg.browser_automation import BrowserAgent
        agent = BrowserAgent()
        agent._started = True
        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.wait_for_load_state = AsyncMock()
        mock_page.title = AsyncMock(return_value="Info Page")
        mock_page.text_content = AsyncMock(return_value="Body text")
        mock_page.evaluate = AsyncMock(return_value={"description": "A page"})
        mock_page.close = AsyncMock()

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        agent._context = mock_context

        result = asyncio.run(agent.get_page_info("https://example.com"))
        self.assertTrue(result.success)
        self.assertEqual(result.data["title"], "Info Page")

    @patch("telechat_pkg.browser_automation.BrowserAgent._ensure_started")
    def test_screenshot_error(self, mock_start):
        from telechat_pkg.browser_automation import BrowserAgent
        agent = BrowserAgent()
        agent._started = True
        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(side_effect=Exception("Browser crashed"))
        agent._context = mock_context

        result = asyncio.run(agent.screenshot("https://example.com"))
        self.assertFalse(result.success)
        self.assertIn("Browser crashed", result.error)

    @patch("telechat_pkg.browser_automation.BrowserAgent._ensure_started")
    def test_extract_text_error(self, mock_start):
        from telechat_pkg.browser_automation import BrowserAgent
        agent = BrowserAgent()
        agent._started = True
        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(side_effect=Exception("fail"))
        agent._context = mock_context

        result = asyncio.run(agent.extract_text("https://x.com"))
        self.assertFalse(result.success)

    @patch("telechat_pkg.browser_automation.BrowserAgent._ensure_started")
    def test_run_script_error(self, mock_start):
        from telechat_pkg.browser_automation import BrowserAgent
        agent = BrowserAgent()
        agent._started = True
        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(side_effect=Exception("fail"))
        agent._context = mock_context

        result = asyncio.run(agent.run_script("https://x.com", "x"))
        self.assertFalse(result.success)

    @patch("telechat_pkg.browser_automation.BrowserAgent._ensure_started")
    def test_get_page_info_error(self, mock_start):
        from telechat_pkg.browser_automation import BrowserAgent
        agent = BrowserAgent()
        agent._started = True
        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(side_effect=Exception("fail"))
        agent._context = mock_context

        result = asyncio.run(agent.get_page_info("https://x.com"))
        self.assertFalse(result.success)

    @patch("telechat_pkg.browser_automation.BrowserAgent._ensure_started")
    def test_fill_form_error(self, mock_start):
        from telechat_pkg.browser_automation import BrowserAgent
        agent = BrowserAgent()
        agent._started = True
        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(side_effect=Exception("fail"))
        agent._context = mock_context

        result = asyncio.run(agent.fill_form("https://x.com", {}))
        self.assertFalse(result.success)

    def test_stop_not_started(self):
        from telechat_pkg.browser_automation import BrowserAgent
        agent = BrowserAgent()
        # Should not raise
        asyncio.run(agent.stop())
        self.assertFalse(agent._started)


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 1: Auto Memory Extraction hook
# ═══════════════════════════════════════════════════════════════════════════════

class TestAutoMemoryExtraction(unittest.TestCase):
    @patch("telechat_pkg.telegram_bot.extract_memories", new_callable=AsyncMock)
    @patch("telechat_pkg.telegram_bot._memory")
    def test_auto_extract_stores_memories(self, mock_memory, mock_extract):
        from telechat_pkg.telegram_bot import _auto_extract_memories
        mock_extract.return_value = [
            {"content": "User prefers Python", "tags": ["preference"], "importance": 0.8},
        ]
        mock_memory.recall.return_value = []
        mock_memory.remember.return_value = MagicMock()

        asyncio.run(_auto_extract_memories(123, "I love Python " * 20, "Great choice! " * 20))
        mock_memory.remember.assert_called_once()

    @patch("telechat_pkg.telegram_bot.extract_memories", new_callable=AsyncMock)
    def test_auto_extract_skips_short(self, mock_extract):
        from telechat_pkg.telegram_bot import _auto_extract_memories
        asyncio.run(_auto_extract_memories(123, "hi", "hello"))
        mock_extract.assert_not_called()

    @patch("telechat_pkg.telegram_bot.extract_memories", new_callable=AsyncMock)
    def test_auto_extract_handles_errors(self, mock_extract):
        from telechat_pkg.telegram_bot import _auto_extract_memories
        mock_extract.side_effect = Exception("API error")
        # Should not raise
        asyncio.run(_auto_extract_memories(123, "x" * 200, "y" * 200))


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 3: Smart model in telegram_bot
# ═══════════════════════════════════════════════════════════════════════════════

class TestSmartModelIntegration(unittest.TestCase):
    def test_smart_model_with_override(self):
        from telechat_pkg.telegram_bot import _smart_model, _user_model
        _user_model[999] = "opus"
        result = _smart_model(999, "hello")
        self.assertEqual(result, "opus")
        del _user_model[999]

    def test_smart_model_auto(self):
        from telechat_pkg.telegram_bot import _smart_model
        result = _smart_model(998, "hello")
        self.assertEqual(result, "haiku")

    def test_smart_model_disabled(self):
        from telechat_pkg.telegram_bot import _smart_model
        with patch("telechat_pkg.telegram_bot.SMART_ROUTING_ENABLED", False):
            result = _smart_model(997, "hello")
            # Should return default model, not "haiku"
            from telechat_pkg.telegram_bot import _DEFAULT_MODEL
            self.assertEqual(result, _DEFAULT_MODEL)


if __name__ == "__main__":
    unittest.main()
