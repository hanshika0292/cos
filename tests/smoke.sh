#!/bin/sh
# Smoke test: hook -> ingest -> status/todos/export/codex, in a throwaway COS_HOME. Touches nothing else.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export COS_HOME="$(mktemp -d)"
trap 'rm -rf "$COS_HOME"' EXIT
H="$ROOT/hooks/cos-hook.sh"
cos() { python3 "$ROOT/cos.py" "$@"; }
fail() { echo "FAIL: $1"; exit 1; }

echo '{"session_id":"s1","cwd":"/tmp/proj","prompt":"fix the \"login\" bug"}' | sh "$H" UserPromptSubmit
echo '{"session_id":"s1","tool_response":"huge"}' | sh "$H" PostToolUse
echo '{"session_id":"s2","cwd":"/tmp/other","message":"needs permission","notification_type":"permission_prompt"}' | sh "$H" Notification
echo '{"session_id":"g1","cwd":"/tmp/gem","prompt":"gemini task"}' | sh "$H" BeforeAgent gemini
echo '{"session_id":"g1","cwd":"/tmp/gem","prompt_response":"gemini done"}' | sh "$H" AfterAgent gemini
COS_SKIP=1 sh -c "echo '{\"session_id\":\"x\"}' | sh '$H' Stop"

grep -q '"tool_response"' "$COS_HOME/events.jsonl" && fail "PostToolUse payload not trimmed"
grep -q '"session_id":"x"' "$COS_HOME/events.jsonl" && fail "COS_SKIP not honoured"
cos >/dev/null || fail "bare cos"
out="$(cos status -a)"
echo "$out" | grep -q "Needs you" || fail "permission wait not shown"
echo "$out" | grep -q "gemini task" || fail "gemini session missing"
cos brief --dry-run --no-pull | grep -q "gemini done" || fail "gemini reply missing from digest"

cos add "write tests" >/dev/null && cos add "tmr: ship it" >/dev/null
cos today | grep -q "write tests" || fail "todo today"
cos tomorrow | grep -q "ship it" || fail "todo tomorrow"
cos done 1 && cos today | grep -q nothing || fail "done"

cos export 0 | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['pos']>0 and d['data']" || fail "export"
python3 "$ROOT/cos.py" codex-notify '{"type":"agent-turn-complete","thread-id":"c1","cwd":"/tmp/cx","input-messages":["codex job"],"last-assistant-message":"codex done"}'
cos status | grep -q "codex job" || fail "codex notify"
python3 - "$ROOT" <<'PY'
import sys, importlib.util
spec = importlib.util.spec_from_file_location("cos", sys.argv[1] + "/cos.py"); c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c)
assert c.remote_argv("gcloud:vm:us-central1-a:proj", "ls")[:4] == ["gcloud", "compute", "ssh", "vm"]
assert c.remote_argv("me@host", "ls")[-2:] == ["me@host", "ls"]
assert c.doc_kind("https://docs.google.com/spreadsheets/d/abc/edit") == "Sheets"
assert "<h2>" in c.md_to_html("## Hi\n- [ ] x", "t")
PY
echo "smoke test passed"
