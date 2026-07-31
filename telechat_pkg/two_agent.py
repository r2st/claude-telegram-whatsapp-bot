"""
Two-Agent Pattern (Feature 5) — planner + executor for complex tasks.

Inspired by the Claude Agent SDK autonomous coding agent that uses a two-agent
pattern (initializer + coding agent) to decompose and execute complex tasks.

The planner uses haiku to quickly decompose tasks into sub-steps.
The executor uses sonnet/opus to execute each step, reporting progress.

Usage:
    from telechat_pkg.two_agent import TwoAgentExecutor
    executor = TwoAgentExecutor()
    async for update in executor.run("Build a REST API with auth"):
        print(update)  # {"type": "plan", "steps": [...]} or {"type": "step_result", ...}
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

from . import models

log = logging.getLogger(__name__)

PLANNER_MODEL = os.getenv("PLANNER_MODEL", models.HAIKU)
EXECUTOR_MODEL = os.getenv("EXECUTOR_MODEL", models.SONNET)
MAX_STEPS = int(os.getenv("TWO_AGENT_MAX_STEPS", "10"))
TWO_AGENT_ENABLED = os.getenv("TWO_AGENT_ENABLED", "true").lower() in ("1", "true", "yes")
COMPLEXITY_THRESHOLD = int(os.getenv("TWO_AGENT_THRESHOLD", "100"))

PLANNER_SYSTEM = """You are a task planner. Given a complex task, decompose it into
clear, actionable sub-steps that can be executed sequentially.

Rules:
- Output 2-{max_steps} concrete steps
- Each step should be independently executable
- Include enough context in each step for a separate agent to execute it
- Return ONLY valid JSON, no prose

Format:
{{"task_summary": "Brief summary", "steps": [
  {{"id": 1, "action": "What to do", "context": "Why and how", "complexity": "simple|moderate|complex"}},
  ...
]}}"""

EXECUTOR_SYSTEM = """You are executing step {step_id} of a multi-step task plan.

Overall task: {task_summary}
Current step: {step_action}
Context: {step_context}

Previous steps completed:
{previous_results}

Execute this step thoroughly. If it involves code, provide complete implementations.
If it involves analysis, be detailed and specific."""


@dataclass
class Step:
    id: int
    action: str
    context: str
    complexity: str = "moderate"
    status: str = "pending"  # pending | running | done | failed
    result: str = ""
    duration: float = 0.0


@dataclass
class TaskPlan:
    task_summary: str
    steps: list[Step]
    created_at: float = 0.0
    completed_at: float = 0.0
    status: str = "planned"  # planned | running | done | failed


def should_use_two_agent(text: str) -> bool:
    """Determine if a query is complex enough to benefit from two-agent pattern."""
    if not TWO_AGENT_ENABLED:
        return False
    words = text.split()
    if len(words) < COMPLEXITY_THRESHOLD // 5:
        return False
    # Look for signals of multi-step tasks
    multi_step_signals = [
        "and then", "after that", "first", "second", "finally",
        "step by step", "multiple", "several", "build", "create",
        "implement", "design", "refactor", "migrate", "full",
        "complete", "comprehensive", "end to end", "e2e",
    ]
    signal_count = sum(1 for s in multi_step_signals if s in text.lower())
    return signal_count >= 2 or len(words) > COMPLEXITY_THRESHOLD


class TwoAgentExecutor:
    def __init__(self):
        self._httpx_client = None

    def _get_client(self):
        if self._httpx_client is None:
            import httpx
            self._httpx_client = httpx.AsyncClient(timeout=60)
        return self._httpx_client

    async def _call_claude(self, prompt: str, system: str, model: str) -> str:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return '{"error": "No API key set"}'

        client = self._get_client()
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 4096,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"]

    async def plan(self, task: str) -> TaskPlan:
        """Use the planner agent to decompose a task into steps."""
        system = PLANNER_SYSTEM.format(max_steps=MAX_STEPS)
        result = await self._call_claude(task, system, PLANNER_MODEL)

        try:
            parsed = json.loads(result)
            steps = [
                Step(
                    id=s.get("id", i + 1),
                    action=s["action"],
                    context=s.get("context", ""),
                    complexity=s.get("complexity", "moderate"),
                )
                for i, s in enumerate(parsed.get("steps", []))
            ]
            return TaskPlan(
                task_summary=parsed.get("task_summary", task[:200]),
                steps=steps[:MAX_STEPS],
                created_at=time.time(),
            )
        except (json.JSONDecodeError, KeyError) as e:
            log.warning("Failed to parse plan: %s", e)
            # Fallback: single-step plan
            return TaskPlan(
                task_summary=task[:200],
                steps=[Step(id=1, action=task, context="Execute as single task")],
                created_at=time.time(),
            )

    async def execute(
        self,
        plan: TaskPlan,
        *,
        on_step_start: Optional[callable] = None,
        on_step_done: Optional[callable] = None,
    ) -> TaskPlan:
        """Execute each step in the plan sequentially."""
        plan.status = "running"
        previous_results = []

        for step in plan.steps:
            step.status = "running"
            if on_step_start:
                await on_step_start(step)

            prev_text = "\n".join(
                f"Step {r['id']}: {r['action']} -> {r['result'][:200]}"
                for r in previous_results
            ) or "None yet."

            system = EXECUTOR_SYSTEM.format(
                step_id=step.id,
                task_summary=plan.task_summary,
                step_action=step.action,
                step_context=step.context,
                previous_results=prev_text,
            )

            # Choose model based on step complexity
            model = EXECUTOR_MODEL
            if step.complexity == "simple":
                model = PLANNER_MODEL  # haiku for simple steps

            start = time.time()
            try:
                result = await self._call_claude(step.action, system, model)
                step.result = result
                step.status = "done"
                step.duration = time.time() - start
            except Exception as e:
                step.result = f"Error: {e}"
                step.status = "failed"
                step.duration = time.time() - start
                log.warning("Step %d failed: %s", step.id, e)

            previous_results.append({
                "id": step.id,
                "action": step.action,
                "result": step.result,
            })

            if on_step_done:
                await on_step_done(step)

        plan.status = "done" if all(s.status == "done" for s in plan.steps) else "failed"
        plan.completed_at = time.time()
        return plan

    async def run(
        self,
        task: str,
        *,
        on_step_start: Optional[callable] = None,
        on_step_done: Optional[callable] = None,
    ) -> TaskPlan:
        """Plan and execute a complex task end-to-end."""
        plan = await self.plan(task)
        return await self.execute(plan, on_step_start=on_step_start, on_step_done=on_step_done)

    def format_plan(self, plan: TaskPlan) -> str:
        """Format a plan for display in chat."""
        lines = [f"**Task Plan:** {plan.task_summary}\n"]
        for step in plan.steps:
            icon = {"pending": "⬜", "running": "🔄", "done": "✅", "failed": "❌"}.get(step.status, "⬜")
            dur = f" ({step.duration:.1f}s)" if step.duration else ""
            lines.append(f"{icon} Step {step.id}: {step.action}{dur}")
        return "\n".join(lines)

    def format_result(self, plan: TaskPlan) -> str:
        """Format completed plan results for display."""
        lines = [self.format_plan(plan), "\n---\n"]
        for step in plan.steps:
            if step.result:
                lines.append(f"**Step {step.id}:** {step.action}")
                lines.append(step.result[:2000])
                lines.append("")
        total_time = plan.completed_at - plan.created_at if plan.completed_at else 0
        lines.append(f"\n_Completed {len(plan.steps)} steps in {total_time:.1f}s_")
        return "\n".join(lines)
