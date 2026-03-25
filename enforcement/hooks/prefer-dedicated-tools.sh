#!/bin/bash
# PreToolUse hook: nudge agent toward Edit for file modifications done via sed/awk.
# Install in .claude/settings.json under hooks.PreToolUse with matcher "Bash".
#
# Philosophy: CLI tools (rg, jq, yq, fd) are PREFERRED for discovery — they're
# more powerful than built-in Grep/Glob and chain better. The only thing that
# should use the native tool is file editing (Edit is more accurate and parallel).
#
# This hook ONLY catches in-place file edits via sed/awk. Everything else is allowed.

set -euo pipefail

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // ""')

# Detect sed -i / awk -i (in-place file editing) → should use Edit
if echo "$COMMAND" | grep -qE '^\s*(sed|awk)\s.*-i'; then
    jq -n '{
        hookSpecificOutput: {
            hookEventName: "PreToolUse",
            permissionDecision: "allow",
            additionalContext: "HINT: For file edits, prefer the native Edit tool over sed -i / awk -i. Edit is more accurate (exact string matching prevents wrong-location edits) and supports parallel calls across files."
        }
    }'
    exit 0
fi

# Everything else — rg, fd, jq, yq, grep, find, cat, etc. — is fine and encouraged.
exit 0
