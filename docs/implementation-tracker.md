# Self-Improving System — Implementation Tracker

## Phase 1: Foundation (Current)

| Feature | Status | Module | Notes |
|---------|--------|--------|-------|
| Feedback collection (/rate + /feedback) | Done | `feedback.py` | Telegram + WhatsApp, /rate 1-5, text feedback |
| Binary quality evaluators | Done | `feedback.py` | 5 evaluators: length, error-free, content, truncation, cost |
| Health check endpoint | Done | `health.py` | HTTP :8484/health with component status |
| Watchdog with auto-restart | Done | `health.py` | Async monitor loop, tiered recovery, state persistence |
| Circuit breaker | Done | `health.py` | CLOSED→OPEN→HALF_OPEN states for claude + db |
| New DB tables (feedback, quality_scores) | Done | `claude_core.py` | Added to init_db() |
| Learnings accumulation (learnings.md) | Done | `feedback.py` | Auto-append insights from high-rated responses |
| Quality metrics command (/quality) | Done | `telegram_bot.py` | Shows ratings, trends, health status |
| Auto quality evaluation on every response | Done | `telegram_bot.py`, `whatsapp_bot.py` | Non-blocking composite scoring |

## Phase 2: Intelligence — Inspired by Top Claude Projects

| Feature | Status | Module | Notes | Inspired By |
|---------|--------|--------|-------|-------------|
| Auto Memory Extraction | **Done** | `memory.py`, `telegram_bot.py` | Background extraction after each turn using Haiku | Wisedocs cross-session memory |
| Cost Budget System | **Done** | `cost_budget.py` | Per-user daily/monthly caps, /budget command, warnings | Financial Data Analyst |
| Smart Model Routing | **Done** | `smart_router.py` | Auto-select haiku/sonnet/opus by query complexity | Customer Support Agent multi-model |
| Session Resume/Fork | **Done** | `session_manager.py` | /resume and /fork commands, session browsing | Agent SDK session persistence |
| Two-Agent Pattern | **Done** | `two_agent.py` | /plan command: planner (haiku) + executor (sonnet) | Agent SDK two-agent pattern |
| Event Bus / Webhooks | **Done** | `event_bus.py` | Typed async pub/sub, GitHub/generic webhook receiver | Claude Quickstarts architecture |
| Scheduled Autonomous Tasks | **Done** | `auto_scheduler.py` | Natural language scheduling, /schedule command | Managed Agents + Dreaming |
| MCP Integration | **Done** | `mcp_client.py`, `mcp_tools.py` | MCP client for external tool servers, plus the tool-use loop that makes those tools callable on the API path | Blender MCP / Playwright MCP |
| Knowledge Base / RAG | **Done** | `knowledge_base.py` | Document ingestion, FTS chunked search, /kb command | Customer Support Agent (Bedrock KB) |
| Browser Automation | **Done** | `browser_automation.py` | Playwright-based /web command (screenshot, extract) | Browser Automation Agent |

## Phase 2b: Intelligence (Next)

| Feature | Status | Module | Notes |
|---------|--------|--------|-------|
| LLM-as-judge evaluator | Done | `evaluator.py` | Sample 10% of responses, judge on helpfulness/accuracy/tone, surface in `/quality` |
| User preference learning | Done | `preferences.py` | Confidence-weighted length/format/tone prefs, decay, `/prefer` + `/feedback`, injected into system prompt |
| Prompt self-optimization | Done | `prompt_optimizer.py` | Weighted A/B variants, sticky per-conversation routing, score aggregation + auto-promote, `/prompts` (opt-in via `PROMPT_OPTIMIZER_ENABLED`) |
| Auto-update mechanism | Done | `updater.py` | PyPI+npm version check on boot/cron, event + `/health` surface, opt-in auto-apply |

## Phase 3: Resilience (Later)

| Feature | Status | Module | Notes |
|---------|--------|--------|-------|
| Circuit breaker pattern | Done | `health.py` | Prevent cascade failures |
| 4-tier recovery (restart → rollback → degrade → alert) | Planned | `health.py` | Escalating recovery |
| Observability & telemetry | Planned | `telemetry.py` | Structured logging, metrics export |

## Phase 4: Ecosystem (Future)

| Feature | Status | Module | Notes |
|---------|--------|--------|-------|
| Plugin architecture | Planned | `plugins/` | Dynamic loading, sandboxed execution |
| Knowledge graph integration | Done | `knowledge_base.py` | FTS-based RAG replaces planned knowledge graph |
| Multi-instance coordination | Planned | `coordinator.py` | Share learnings across deployments |

---

## Changelog

- **2026-05-18**: 10 features from top Claude projects implemented — auto memory extraction, cost budgets, smart model routing, session resume/fork, two-agent pattern, event bus, auto scheduler, MCP integration, knowledge base RAG, browser automation. 9 new modules added.
- **2026-05-16**: Phase 1 complete — feedback.py, health.py, DB schema, quality evaluators, circuit breaker, watchdog, /rate + /feedback + /quality commands on Telegram & WhatsApp. Version bumped to 1.1.0.
