#!/usr/bin/env bash
#
# End-to-end FEATURE test for telechat.
#
# Exercises the actual runtime features (not just install/connectivity):
#
#   • Conversation persistence  save_turn → load_history → clear_history
#   • Rate limiting             check_rate_limit throttles after the cap
#   • Usage tracking            track_usage → get_usage totals
#   • Claude brain round-trip   ask_claude_sync returns a real reply
#   • Health endpoint           /health JSON shape + component statuses
#   • Service lifecycle         start → health up → status → restart → stop
#
# Safety / isolation:
#   - DB-backed tests use an isolated temp DB_PATH (never touches bot.db).
#   - The Claude round-trip is skipped if the `claude` CLI is absent.
#   - Health probe is read-only against whatever is already on :8484.
#   - The destructive start/stop/restart lifecycle is OPT-IN:
#         TELECHAT_E2E_LIFECYCLE=1 bash tests/e2e-features.sh
#     (off by default so it never disturbs your running bot).
#
# Usage:  bash tests/e2e-features.sh
# Exit:   0 = all run checks passed (skips don't fail), 1 = a check failed

set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLI="$REPO_ROOT/npm/bin/telechat.js"
PY="$(command -v python3 || command -v python)"

PASS=0; FAIL=0; SKIP=0
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; PASS=$((PASS + 1)); }
bad()  { printf "  \033[31m✗\033[0m %s\n" "$1"; FAIL=$((FAIL + 1)); }
skip() { printf "  \033[33m–\033[0m %s (skipped)\n" "$1"; SKIP=$((SKIP + 1)); }

TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

echo
echo "telechat e2e feature test"
echo

if [ -z "$PY" ] || ! PYTHONPATH="$REPO_ROOT" "$PY" -c "import telechat_pkg" 2>/dev/null; then
  echo "  telechat_pkg not importable — cannot run feature tests."
  echo "  Install deps: pip install -e . (or pip install telechatai)"
  exit 1
fi

# ── 1. Conversation persistence ──────────────────────────────────────────────
echo "[1] Conversation persistence (SQLite)"
out="$(DB_PATH="$TMP/p.db" PYTHONPATH="$REPO_ROOT" "$PY" - <<'PY' 2>&1
import time
import telechat_pkg.claude_core as cc
cc.init_db()
P, U = "telegram", "e2e_user_1"
cc.clear_history(P, U)
cc.save_turn(P, U, "hello bot", "hi there")
cc.save_turn(P, U, "second message", "second reply")
# Writes are async; poll until both turns are persisted (history cache
# has a short TTL so re-reads eventually reflect the DB).
texts = ""
for _ in range(100):  # up to ~10s
    cc._invalidate_history(P, U)
    h = cc.load_history(P, U, limit=10)
    texts = " ".join(str(t) for t in h)
    if "hello bot" in texts and "second reply" in texts:
        break
    time.sleep(0.1)
assert "hello bot" in texts and "second reply" in texts, f"history missing turns: {texts[:200]}"
cc.clear_history(P, U)
for _ in range(100):
    cc._invalidate_history(P, U)
    h2 = cc.load_history(P, U, limit=10)
    if len(h2) == 0:
        break
    time.sleep(0.1)
assert len(h2) == 0, f"clear_history failed: {h2}"
print("PASS")
PY
)"
printf '%s' "$out" | grep -q '^PASS' && ok "save → load → clear round-trip" || { bad "persistence round-trip"; printf '%s\n' "$out" | tail -4 | sed 's/^/      /'; }

# ── 2. Rate limiting ─────────────────────────────────────────────────────────
echo "[2] Rate limiting"
out="$(DB_PATH="$TMP/r.db" RATE_LIMIT_REQUESTS="3" RATE_LIMIT_WINDOW="60" PYTHONPATH="$REPO_ROOT" "$PY" - <<'PY' 2>&1
import telechat_pkg.claude_core as cc
cc.init_db()
key = "telegram:rate_user"
allowed = sum(1 for _ in range(10) if cc.check_rate_limit(key))
# With a cap of 3, far fewer than 10 calls should be allowed
assert allowed <= 5, f"rate limit not enforced: {allowed} allowed"
assert allowed >= 1, f"rate limit too aggressive: {allowed} allowed"
print(f"PASS allowed={allowed}")
PY
)"
printf '%s' "$out" | grep -q '^PASS' && ok "throttles after the configured cap ($(printf '%s' "$out" | grep -o 'allowed=[0-9]*'))" || { bad "rate limiting"; printf '%s\n' "$out" | tail -4 | sed 's/^/      /'; }

# ── 3. Usage tracking ────────────────────────────────────────────────────────
echo "[3] Usage tracking"
out="$(DB_PATH="$TMP/u.db" PYTHONPATH="$REPO_ROOT" "$PY" - <<'PY' 2>&1
import time
import telechat_pkg.claude_core as cc
cc.init_db()
P, U = "slack", "usage_user"
cc.track_usage(P, U, in_tok=100, out_tok=50)
cc.track_usage(P, U, in_tok=20, out_tok=10)
# Writes are async (background writer thread, same as the live bot).
# Poll for eventual consistency instead of reading immediately.
u = {}
for _ in range(100):  # up to ~10s
    u = cc.get_usage(P, U)
    if u.get("input") == 120 and u.get("output") == 60:
        break
    time.sleep(0.1)
assert u.get("input") == 120, f"input tokens not summed: {u}"
assert u.get("output") == 60, f"output tokens not summed: {u}"
print(f"PASS {u}")
PY
)"
printf '%s' "$out" | grep -q '^PASS' && ok "token totals accumulate per user" || { bad "usage tracking"; printf '%s\n' "$out" | tail -4 | sed 's/^/      /'; }

# ── 4. Claude brain round-trip ───────────────────────────────────────────────
echo "[4] Claude brain (ask_claude_sync)"
if ! command -v claude >/dev/null 2>&1; then
  skip "claude CLI not installed"
else
  out="$(DB_PATH="$TMP/c.db" CLAUDE_TIMEOUT="60" PYTHONPATH="$REPO_ROOT" "$PY" - <<'PY' 2>&1
import telechat_pkg.claude_core as cc
reply, stats = cc.ask_claude_sync(
    "Reply with exactly the single word: PONG",
    [],
    timeout=60,
)
assert reply, "empty reply"
low = reply.lower()
if "401" in reply or "invalid authentication" in low or "failed to authenticate" in low:
    # CLI present but not signed in / no valid credentials — not a code defect.
    print("SKIP: claude CLI not authenticated")
else:
    assert not reply.startswith("[Error]"), f"claude error: {reply[:120]}"
    assert not reply.startswith("[Timeout]"), "claude timed out"
    assert "PONG" in reply.upper(), f"unexpected reply: {reply[:120]!r}"
    print("PASS")
PY
)"
  if printf '%s' "$out" | grep -q '^PASS'; then
    ok "real Claude CLI round-trip returns expected reply"
  elif printf '%s' "$out" | grep -q '^SKIP'; then
    skip "Claude brain round-trip — claude CLI not authenticated"
  else
    bad "Claude brain round-trip"; printf '%s\n' "$out" | tail -5 | sed 's/^/      /'
  fi
fi

# ── 4b. Coding agent (project store + real end-to-end file creation) ─────────
echo "[4b] Coding agent"
out="$(DB_PATH="$TMP/cg.db" PYTHONPATH="$REPO_ROOT" "$PY" - <<PY 2>&1
from telechat_pkg import coder
ok, msg = coder.set_project("telegram", "cg_user", "$TMP")
assert ok, f"set_project failed: {msg}"
assert coder.get_project("telegram", "cg_user") == "$TMP", "get_project mismatch"
ok2, _ = coder.set_project("telegram", "cg_user", "/no/such/dir/xyz")
assert not ok2, "set_project should reject a non-directory"
p = coder.build_task_prompt("add a flag", "$TMP")
assert "$TMP" in p and "EXPLORE" in p, "task prompt malformed"
coder.clear_project("telegram", "cg_user")
assert coder.get_project("telegram", "cg_user") is None, "clear_project failed"
print("PASS")
PY
)"
printf '%s' "$out" | grep -q '^PASS' && ok "project store: set/get/reject/clear" || { bad "coding agent project store"; printf '%s\n' "$out" | tail -4 | sed 's/^/      /'; }

if ! command -v claude >/dev/null 2>&1; then
  skip "end-to-end coding run (claude CLI not installed)"
else
  PROJ="$TMP/proj"; mkdir -p "$PROJ"
  out="$(DB_PATH="$TMP/cg2.db" PYTHONPATH="$REPO_ROOT" "$PY" - <<PY 2>&1
import asyncio
import telechat_pkg.claude_core as cc
from telechat_pkg import coder
prompt = coder.build_task_prompt(
    "Create a file named DONE.txt containing exactly the text READY. "
    "No other files.",
    "$PROJ",
)
reply, stats = asyncio.run(
    cc.ask_claude_async(
        prompt, [],
        system=coder.CODER_SYSTEM,
        perm_mode="bypassPermissions",
        timeout=120,
        work_dir="$PROJ",
    )
)
reply = reply or ""
low = reply.lower()
if "401" in reply or "invalid authentication" in low or "failed to authenticate" in low:
    # CLI present but not signed in / no valid credentials — not a code defect.
    print("SKIP: claude CLI not authenticated")
else:
    print("reply:", reply[:80].replace(chr(10), " "))
PY
)"
  if printf '%s' "$out" | grep -q '^SKIP'; then
    skip "end-to-end coding run — claude CLI not authenticated"
  elif [ -f "$PROJ/DONE.txt" ] && grep -q "READY" "$PROJ/DONE.txt"; then
    ok "agent created DONE.txt with correct content in the project dir"
  else
    bad "coding agent did not produce the expected file"
    printf '%s\n' "$out" | tail -4 | sed 's/^/      /'
  fi
fi

# ── 4c. Memory store round-trip ──────────────────────────────────────────────
echo "[4c] Memory store (remember / recall / forget)"
out="$(DB_PATH="$TMP/mem.db" PYTHONPATH="$REPO_ROOT" "$PY" - <<PY 2>&1
from telechat_pkg.memory import MemoryStore
ms = MemoryStore("$TMP/mem.db")
m = ms.remember("tg", "e2e_u", "prefers dark mode", tags=["pref"], importance=0.8)
assert m.id and m.content == "prefers dark mode" and m.importance == 0.8
results = ms.recall("tg", "e2e_u", "dark mode")
assert any("dark" in r.content for r in results), f"recall failed: {results}"
ms.update("tg", "e2e_u", m.id, content="prefers light mode")
mems = ms.list_memories("tg", "e2e_u")
assert any("light" in x.content for x in mems), "update failed"
assert ms.forget("tg", "e2e_u", m.id)
assert ms.stats("tg", "e2e_u")["total"] == 0, "forget failed"
assert ms.recall("slack", "e2e_u", "light") == [], "platform isolation failed"
print("PASS")
PY
)"
printf '%s' "$out" | grep -q '^PASS' && ok "remember → recall → update → forget → isolation" || { bad "memory store round-trip"; printf '%s\n' "$out" | tail -4 | sed 's/^/      /'; }

# ── 4d. Session manager ─────────────────────────────────────────────────────
echo "[4d] Session manager"
out="$(DB_PATH="$TMP/sess.db" PYTHONPATH="$REPO_ROOT" "$PY" - <<'PY' 2>&1
import telechat_pkg.claude_core as cc
cc.init_db()
P, U = "telegram", "e2e_sess"
s = cc._session_mgr.get_or_create_active(P, U)
assert s.name == "default", f"expected default, got {s.name}"
s2 = cc._session_mgr.create(P, U, "work")
assert s2.name == "work"
all_s = cc._session_mgr.get_all(P, U)
assert len(all_s) == 2, f"expected 2 sessions, got {len(all_s)}"
switched = cc._session_mgr.switch_to(P, U, 1)
assert switched and switched.name == "work", f"switch failed: {switched}"
deleted = cc._session_mgr.delete(P, U, 1)
assert deleted, "delete failed"
remaining = cc._session_mgr.get_all(P, U)
assert len(remaining) == 1, f"expected 1 after delete, got {len(remaining)}"
print("PASS")
PY
)"
printf '%s' "$out" | grep -q '^PASS' && ok "create → list → switch → delete" || { bad "session manager"; printf '%s\n' "$out" | tail -4 | sed 's/^/      /'; }

# ── 5. Health endpoint (read-only probe of whatever is on :8484) ──────────────
echo "[5] Health endpoint"
hp="${HEALTH_PORT:-8484}"
hjson="$(curl -s --max-time 4 "http://localhost:${hp}/health" 2>/dev/null)"
if [ -z "$hjson" ]; then
  skip "no bot on :${hp} (start one to test /health live)"
else
  printf '%s' "$hjson" | grep -q '"status"' \
    && ok "/health returns JSON with status field" \
    || bad "/health missing status field"
  printf '%s' "$hjson" | grep -q '"components"' \
    && ok "/health reports components" \
    || bad "/health missing components"
  if printf '%s' "$hjson" | grep -q '"status": *"healthy"'; then
    ok "overall status: healthy"
  else
    bad "overall status not healthy: $(printf '%s' "$hjson" | head -c 120)"
  fi
fi

# ── 6. Service lifecycle (OPT-IN — destructive to a running instance) ─────────
echo "[6] Service lifecycle (start/status/restart/stop)"
if [ "${TELECHAT_E2E_LIFECYCLE:-0}" != "1" ]; then
  skip "set TELECHAT_E2E_LIFECYCLE=1 to run (restarts your bot)"
elif [ ! -f "${TELECHAT_HOME:-$HOME/.telechat}/.env" ]; then
  skip "no .env configured — run 'telechat init' first"
else
  node "$CLI" stop >/dev/null 2>&1
  sleep 2
  node "$CLI" start >/dev/null 2>&1
  # Wait up to 30s for the health server to come up
  up=""
  for _ in $(seq 1 30); do
    if curl -s --max-time 2 "http://localhost:${hp}/health" | grep -q '"status"'; then up=1; break; fi
    sleep 1
  done
  [ -n "$up" ] && ok "start → health endpoint comes up" || bad "bot did not become healthy within 30s"

  node "$CLI" status 2>&1 | grep -q "running" \
    && ok "status reports running" || bad "status does not report running"

  node "$CLI" restart >/dev/null 2>&1
  up=""
  for _ in $(seq 1 30); do
    if curl -s --max-time 2 "http://localhost:${hp}/health" | grep -q '"status"'; then up=1; break; fi
    sleep 1
  done
  [ -n "$up" ] && ok "restart → healthy again" || bad "bot unhealthy after restart"

  node "$CLI" stop >/dev/null 2>&1
  sleep 3
  if curl -s --max-time 2 "http://localhost:${hp}/health" | grep -q '"status"'; then
    bad "stop did not shut the bot down"
  else
    ok "stop → health endpoint gone"
  fi
fi

# ── 7. New productivity features (commitments, compaction, export, extract, doctor)
echo ""
echo "[7] Commitments / Reminders"
"$PY" -c "
import sys; sys.path.insert(0, '$REPO_ROOT')
from telechat_pkg import commitments, store
store.init_db()
commitments.init_db()
r = commitments.add_commitment('e2e', 'test_user', 'reminder', 'e2e test', 9999999999)
assert r.id
import time; time.sleep(0.5)
pending = commitments.get_pending('e2e', 'test_user')
assert any(p.id == r.id for p in pending)
text = commitments.format_pending(pending)
assert 'e2e test' in text
commitments.dismiss(r.id)
" && ok "commitments lifecycle works" || bad "commitments lifecycle"

echo "[8] Context Compaction"
"$PY" -c "
import sys; sys.path.insert(0, '$REPO_ROOT')
from telechat_pkg.context_compaction import compact_history_sync, estimate_tokens
small = [{'role': 'user', 'content': 'hi'}] * 5
r = compact_history_sync(small)
assert r.messages_compacted == 0
assert estimate_tokens('hello world') >= 1
" && ok "context compaction works" || bad "context compaction"

echo "[9] Conversation Export"
"$PY" -c "
import sys, json; sys.path.insert(0, '$REPO_ROOT')
from telechat_pkg.conversation_export import export_conversation
msgs = [{'role': 'user', 'content': 'Hello!', 'timestamp': 1700000000},
        {'role': 'assistant', 'content': 'Hi!', 'timestamp': 1700000010}]
for fmt in ('text', 'markdown', 'html', 'json'):
    r = export_conversation(msgs, fmt)
    assert r.message_count == 2
    assert len(r.content) > 0
jr = export_conversation(msgs, 'json')
data = json.loads(jr.content)
assert data['message_count'] == 2
hr = export_conversation([{'role': 'user', 'content': '<script>alert(1)</script>'}], 'html')
assert '<script>' not in hr.content
" && ok "conversation export all formats" || bad "conversation export"

echo "[10] Document Extraction"
"$PY" -c "
import sys, tempfile, os; sys.path.insert(0, '$REPO_ROOT')
from telechat_pkg.document_extract import extract, available_formats
fmts = available_formats()
assert 'txt' in fmts and 'csv' in fmts
with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
    f.write('Hello e2e test')
    tmp = f.name
r = extract(tmp)
os.unlink(tmp)
assert 'Hello e2e test' in r.text
assert r.error is None
r2 = extract('/nonexistent')
assert r2.error is not None
" && ok "document extraction works" || bad "document extraction"

echo "[11] Doctor Diagnostics"
"$PY" -c "
import sys; sys.path.insert(0, '$REPO_ROOT')
from telechat_pkg.doctor import run_doctor_sync
report = run_doctor_sync()
assert len(report.checks) >= 5
text = report.format()
assert 'Python version' in text
assert 'Passed:' in text
" && ok "doctor diagnostics ($(python3 -c "import sys;sys.path.insert(0,'$REPO_ROOT');from telechat_pkg.doctor import run_doctor_sync;print(len(run_doctor_sync().checks))" 2>/dev/null || echo '?') checks)" || bad "doctor diagnostics"

# ── Summary ──────────────────────────────────────────────────────────────────
echo
echo "──────────────────────────────────────────────"
printf "  Passed: %d   Failed: %d   Skipped: %d\n" "$PASS" "$FAIL" "$SKIP"
echo "──────────────────────────────────────────────"
[ "$FAIL" -eq 0 ]
