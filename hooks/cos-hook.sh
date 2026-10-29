#!/bin/sh
# cos hook: append one JSON line per agent event to ~/.cos/events.jsonl.
# Usage: cos-hook.sh <Event> [agent]     agent: claude (default) | gemini
# Must be fast, print nothing (stdout of some hooks is fed to the model), and never fail the session.
EV="${1:-unknown}"
AGENT="${2:-claude}"
P=$(cat | tr -d '\n\r')
[ -n "${COS_SKIP-}" ] && exit 0   # cos's own `claude -p` brief runs
DIR="${COS_HOME:-$HOME/.cos}"
mkdir -p "$DIR" 2>/dev/null

# normalise other agents' event names to Claude's
case "$AGENT:$EV" in
  gemini:BeforeAgent) EV=UserPromptSubmit ;;
  gemini:AfterAgent)  EV=Stop ;;
  gemini:AfterTool)   EV=PostToolUse ;;
esac

if [ "$EV" = "PostToolUse" ]; then
  # tool payloads can be huge (file contents); keep only the session id
  P="{\"session_id\":\"$(printf '%s' "$P" | grep -o '"session_id" *: *"[^"]*"' | head -1 | sed 's/.*: *"//; s/"$//')\"}"
fi
[ -z "$P" ] && P='{}'

# where the session lives, so `cos focus` can jump back to it (only on events that are rare)
TTY="" ; TMUXP=""
if [ "$EV" = "SessionStart" ] || [ "$EV" = "UserPromptSubmit" ]; then
  TTY=$(ps -o tty= -p "${CLAUDE_PID:-$PPID}" 2>/dev/null | tr -d ' ')
  [ -n "${TMUX-}" ] && TMUXP=$(tmux display-message -p -t "${TMUX_PANE-}" '#S:#I.#P' 2>/dev/null)
fi
CFG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
[ "$AGENT" = "claude" ] || CFG="$AGENT"
LINE=$(printf '{"ts":%s,"ev":"%s","agent":"%s","host":"%s","profile":"%s","cpid":"%s","term":"%s","iterm":"%s","orca":"%s","tty":"%s","tmux":"%s","p":%s}' \
  "$(date +%s)" "$EV" "$AGENT" "$(hostname -s)" "${CFG##*/}" "${CLAUDE_PID-}" "${TERM_PROGRAM-}" \
  "${ITERM_SESSION_ID-}" "${ORCA_TERMINAL_HANDLE-}" "$TTY" "$TMUXP" "$P")
printf '%s\n' "$LINE" >> "$DIR/events.jsonl" 2>/dev/null
if { [ "$EV" = "Notification" ] || [ "$EV" = "Stop" ]; } && [ "$(uname)" = "Darwin" ]; then
  PY=$(command -v python3)
  [ -n "$PY" ] && (printf '%s' "$LINE" | "$PY" "$(dirname "$0")/../cos.py" notify >/dev/null 2>&1 &)
fi
exit 0
