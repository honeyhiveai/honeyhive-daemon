#!/bin/bash
# PreToolUse hook: enforce a session tool-call budget.
# Blocks tool calls once a session exceeds MAX_TOOL_CALLS.
# Prevents runaway sessions (the 1,643-event mega-session problem).
#
# Install in .claude/settings.json under hooks.PreToolUse (no matcher = all tools).

set -euo pipefail

MAX_TOOL_CALLS="${CLAUDE_MAX_TOOL_CALLS:-500}"
COUNTER_DIR="${TMPDIR:-/tmp}/claude-session-budgets"
mkdir -p "$COUNTER_DIR"

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "unknown"')
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // "unknown"')

COUNTER_FILE="$COUNTER_DIR/$SESSION_ID"

# Increment counter
if [ -f "$COUNTER_FILE" ]; then
    COUNT=$(cat "$COUNTER_FILE")
    COUNT=$((COUNT + 1))
else
    COUNT=1
fi
echo "$COUNT" > "$COUNTER_FILE"

# Check budget
if [ "$COUNT" -gt "$MAX_TOOL_CALLS" ]; then
    echo "Session budget exceeded: $COUNT/$MAX_TOOL_CALLS tool calls. Summarize progress and stop." >&2
    exit 2
fi

# Warn at 80% budget
WARN_AT=$((MAX_TOOL_CALLS * 80 / 100))
if [ "$COUNT" -eq "$WARN_AT" ]; then
    jq -n --arg count "$COUNT" --arg max "$MAX_TOOL_CALLS" '{
        hookSpecificOutput: {
            hookEventName: "PreToolUse",
            permissionDecision: "allow",
            additionalContext: ("WARNING: You have used " + $count + "/" + $max + " of your tool call budget for this session. Start wrapping up — summarize your progress and complete the current task efficiently.")
        }
    }'
    exit 0
fi

# Under budget — allow silently
exit 0
