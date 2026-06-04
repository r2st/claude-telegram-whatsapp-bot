"""
Behavior tests for the two-agent planner/executor (telechat_pkg.two_agent).

The Claude boundary (_call_claude) is monkeypatched so no network/API calls
are made. Covers: complexity gating, plan parsing (+ fallback), sequential
execution with step callbacks, model selection per step complexity, error
handling, the no-API-key path, and the formatting helpers.

Run:
    pytest tests/test_two_agent.py -v
"""

import json
import os

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")

from telechat_pkg import two_agent as ta
from telechat_pkg.two_agent import Step, TaskPlan, TwoAgentExecutor, should_use_two_agent


# ══════════════════════════════════════════════════════════════════════════════
# 1. should_use_two_agent gating
# ══════════════════════════════════════════════════════════════════════════════


class TestShouldUseTwoAgent:
    def test_disabled_returns_false(self, monkeypatch):
        monkeypatch.setattr(ta, "TWO_AGENT_ENABLED", False)
        assert should_use_two_agent("build and create and implement a thing step by step") is False

    def test_too_short_returns_false(self, monkeypatch):
        monkeypatch.setattr(ta, "TWO_AGENT_ENABLED", True)
        # below COMPLEXITY_THRESHOLD//5 words
        assert should_use_two_agent("build this") is False

    def test_two_signals_triggers(self, monkeypatch):
        monkeypatch.setattr(ta, "TWO_AGENT_ENABLED", True)
        monkeypatch.setattr(ta, "COMPLEXITY_THRESHOLD", 50)  # min 10 words
        text = "first build the api and then create the database schema for it"
        assert should_use_two_agent(text) is True

    def test_long_text_triggers(self, monkeypatch):
        monkeypatch.setattr(ta, "TWO_AGENT_ENABLED", True)
        monkeypatch.setattr(ta, "COMPLEXITY_THRESHOLD", 100)
        text = " ".join(["word"] * 150)
        assert should_use_two_agent(text) is True

    def test_medium_no_signals_false(self, monkeypatch):
        monkeypatch.setattr(ta, "TWO_AGENT_ENABLED", True)
        monkeypatch.setattr(ta, "COMPLEXITY_THRESHOLD", 100)
        # 30 words, no multi-step signals
        text = " ".join(["lorem"] * 30)
        assert should_use_two_agent(text) is False


# ══════════════════════════════════════════════════════════════════════════════
# 2. _call_claude boundary
# ══════════════════════════════════════════════════════════════════════════════


class TestCallClaude:
    @pytest.mark.asyncio
    async def test_no_api_key_returns_error_json(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        ex = TwoAgentExecutor()
        result = await ex._call_claude("prompt", "system", "model")
        assert json.loads(result) == {"error": "No API key set"}

    @pytest.mark.asyncio
    async def test_call_claude_posts_and_extracts_text(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key-123")
        ex = TwoAgentExecutor()

        captured = {}

        class FakeResp:
            def raise_for_status(self):
                captured["raised"] = True

            def json(self):
                return {"content": [{"text": "hello from claude"}]}

        class FakeClient:
            async def post(self, url, headers=None, json=None):
                captured["url"] = url
                captured["headers"] = headers
                captured["json"] = json
                return FakeResp()

        monkeypatch.setattr(ex, "_get_client", lambda: FakeClient())
        out = await ex._call_claude("the prompt", "the system", "the-model")
        assert out == "hello from claude"
        assert captured["json"]["model"] == "the-model"
        assert captured["json"]["system"] == "the system"
        assert captured["headers"]["x-api-key"] == "key-123"

    def test_get_client_lazy_init(self, monkeypatch):
        ex = TwoAgentExecutor()
        sentinel = object()
        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda timeout=None: sentinel)
        client = ex._get_client()
        assert client is sentinel
        # cached on second call
        assert ex._get_client() is sentinel


# ══════════════════════════════════════════════════════════════════════════════
# 3. plan()
# ══════════════════════════════════════════════════════════════════════════════


class TestPlan:
    @pytest.mark.asyncio
    async def test_parses_valid_plan(self, monkeypatch):
        ex = TwoAgentExecutor()
        plan_json = json.dumps({
            "task_summary": "Build API",
            "steps": [
                {"id": 1, "action": "design schema", "context": "db", "complexity": "moderate"},
                {"id": 2, "action": "write handlers", "context": "http", "complexity": "complex"},
            ],
        })

        async def fake_call(prompt, system, model):
            return plan_json

        monkeypatch.setattr(ex, "_call_claude", fake_call)
        plan = await ex.plan("build an api")
        assert plan.task_summary == "Build API"
        assert len(plan.steps) == 2
        assert plan.steps[1].complexity == "complex"
        assert plan.created_at > 0

    @pytest.mark.asyncio
    async def test_invalid_json_falls_back_to_single_step(self, monkeypatch):
        ex = TwoAgentExecutor()

        async def fake_call(prompt, system, model):
            return "this is not json"

        monkeypatch.setattr(ex, "_call_claude", fake_call)
        plan = await ex.plan("just do this task")
        assert len(plan.steps) == 1
        assert plan.steps[0].action == "just do this task"
        assert plan.steps[0].context == "Execute as single task"

    @pytest.mark.asyncio
    async def test_missing_action_key_falls_back(self, monkeypatch):
        ex = TwoAgentExecutor()
        # steps missing required "action" key → KeyError → fallback
        bad = json.dumps({"task_summary": "x", "steps": [{"id": 1, "context": "c"}]})

        async def fake_call(prompt, system, model):
            return bad

        monkeypatch.setattr(ex, "_call_claude", fake_call)
        plan = await ex.plan("do the thing")
        assert len(plan.steps) == 1
        assert plan.steps[0].context == "Execute as single task"

    @pytest.mark.asyncio
    async def test_steps_capped_at_max(self, monkeypatch):
        monkeypatch.setattr(ta, "MAX_STEPS", 3)
        ex = TwoAgentExecutor()
        steps = [{"id": i, "action": f"step {i}"} for i in range(10)]
        payload = json.dumps({"task_summary": "many", "steps": steps})

        async def fake_call(prompt, system, model):
            return payload

        monkeypatch.setattr(ex, "_call_claude", fake_call)
        plan = await ex.plan("lots of steps")
        assert len(plan.steps) == 3

    @pytest.mark.asyncio
    async def test_default_id_when_absent(self, monkeypatch):
        ex = TwoAgentExecutor()
        payload = json.dumps({"task_summary": "t", "steps": [{"action": "a"}, {"action": "b"}]})

        async def fake_call(prompt, system, model):
            return payload

        monkeypatch.setattr(ex, "_call_claude", fake_call)
        plan = await ex.plan("task")
        assert [s.id for s in plan.steps] == [1, 2]


# ══════════════════════════════════════════════════════════════════════════════
# 4. execute()
# ══════════════════════════════════════════════════════════════════════════════


class TestExecute:
    def _plan(self):
        return TaskPlan(
            task_summary="Test task",
            steps=[
                Step(id=1, action="step one", context="c1", complexity="moderate"),
                Step(id=2, action="step two", context="c2", complexity="simple"),
            ],
            created_at=1.0,
        )

    @pytest.mark.asyncio
    async def test_all_steps_done(self, monkeypatch):
        ex = TwoAgentExecutor()

        async def fake_call(prompt, system, model):
            return f"result for: {prompt}"

        monkeypatch.setattr(ex, "_call_claude", fake_call)
        plan = await ex.execute(self._plan())
        assert plan.status == "done"
        assert all(s.status == "done" for s in plan.steps)
        assert plan.completed_at > 0
        assert plan.steps[0].duration >= 0

    @pytest.mark.asyncio
    async def test_simple_step_uses_planner_model(self, monkeypatch):
        ex = TwoAgentExecutor()
        models_used = []

        async def fake_call(prompt, system, model):
            models_used.append(model)
            return "ok"

        monkeypatch.setattr(ex, "_call_claude", fake_call)
        await ex.execute(self._plan())
        # step 1 moderate → EXECUTOR_MODEL; step 2 simple → PLANNER_MODEL
        assert models_used[0] == ta.EXECUTOR_MODEL
        assert models_used[1] == ta.PLANNER_MODEL

    @pytest.mark.asyncio
    async def test_step_failure_marks_plan_failed(self, monkeypatch):
        ex = TwoAgentExecutor()

        async def fake_call(prompt, system, model):
            raise RuntimeError("api down")

        monkeypatch.setattr(ex, "_call_claude", fake_call)
        plan = await ex.execute(self._plan())
        assert plan.status == "failed"
        assert all(s.status == "failed" for s in plan.steps)
        assert plan.steps[0].result.startswith("Error:")

    @pytest.mark.asyncio
    async def test_step_callbacks_fire(self, monkeypatch):
        ex = TwoAgentExecutor()

        async def fake_call(prompt, system, model):
            return "done"

        monkeypatch.setattr(ex, "_call_claude", fake_call)
        starts, dones = [], []

        async def on_start(step):
            starts.append(step.id)

        async def on_done(step):
            dones.append(step.id)

        await ex.execute(self._plan(), on_step_start=on_start, on_step_done=on_done)
        assert starts == [1, 2]
        assert dones == [1, 2]

    @pytest.mark.asyncio
    async def test_previous_results_passed_forward(self, monkeypatch):
        ex = TwoAgentExecutor()
        systems = []

        async def fake_call(prompt, system, model):
            systems.append(system)
            return f"output-{prompt}"

        monkeypatch.setattr(ex, "_call_claude", fake_call)
        await ex.execute(self._plan())
        # The second step's system prompt should reference step 1's result.
        assert "None yet." in systems[0]
        assert "Step 1" in systems[1]


# ══════════════════════════════════════════════════════════════════════════════
# 5. run() end-to-end
# ══════════════════════════════════════════════════════════════════════════════


class TestRun:
    @pytest.mark.asyncio
    async def test_run_plans_then_executes(self, monkeypatch):
        ex = TwoAgentExecutor()
        plan_json = json.dumps({
            "task_summary": "Two-step task",
            "steps": [{"id": 1, "action": "a", "complexity": "simple"}],
        })
        calls = {"n": 0}

        async def fake_call(prompt, system, model):
            calls["n"] += 1
            if calls["n"] == 1:
                return plan_json  # planning call
            return "executed"

        monkeypatch.setattr(ex, "_call_claude", fake_call)
        plan = await ex.run("do a two step task")
        assert plan.status == "done"
        assert plan.steps[0].result == "executed"


# ══════════════════════════════════════════════════════════════════════════════
# 6. Formatting helpers
# ══════════════════════════════════════════════════════════════════════════════


class TestFormatting:
    def test_format_plan_lists_steps(self):
        ex = TwoAgentExecutor()
        plan = TaskPlan(
            task_summary="My Task",
            steps=[
                Step(id=1, action="first", context="", status="done", duration=1.2),
                Step(id=2, action="second", context="", status="running"),
                Step(id=3, action="third", context="", status="pending"),
                Step(id=4, action="fourth", context="", status="failed"),
            ],
        )
        out = ex.format_plan(plan)
        assert "My Task" in out
        assert "Step 1: first" in out
        assert "(1.2s)" in out
        assert "Step 4: fourth" in out

    def test_format_plan_unknown_status_uses_default_icon(self):
        ex = TwoAgentExecutor()
        plan = TaskPlan(task_summary="t", steps=[Step(id=1, action="a", context="", status="weird")])
        out = ex.format_plan(plan)
        assert "Step 1: a" in out

    def test_format_result_includes_step_output(self):
        ex = TwoAgentExecutor()
        plan = TaskPlan(
            task_summary="Done task",
            steps=[Step(id=1, action="build", context="", status="done", result="the output")],
            created_at=1.0,
            completed_at=5.0,
        )
        out = ex.format_result(plan)
        assert "the output" in out
        assert "Completed 1 steps" in out

    def test_format_result_no_completed_at_zero_time(self):
        ex = TwoAgentExecutor()
        plan = TaskPlan(
            task_summary="t",
            steps=[Step(id=1, action="a", context="", status="done", result="r")],
            created_at=1.0,
            completed_at=0.0,
        )
        out = ex.format_result(plan)
        assert "0.0s" in out

    def test_format_result_skips_empty_results(self):
        ex = TwoAgentExecutor()
        plan = TaskPlan(
            task_summary="t",
            steps=[Step(id=1, action="a", context="", status="pending", result="")],
            created_at=1.0,
        )
        out = ex.format_result(plan)
        # no per-step body section emitted for empty result
        assert "**Step 1:** a" not in out
