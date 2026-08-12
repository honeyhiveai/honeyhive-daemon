#!/usr/bin/env bash
# Forward one Claude Code hook payload to the HoneyHive daemon.
#
# Registered by the honeyhive-observability plugin for every hook event listed
# in honeyhive_daemon/mappings/claude_code.yaml. The payload arrives on stdin
# and is piped straight through to `honeyhive-daemon ingest claude-hook`, which
# is the same command the daemon writes into ~/.claude/settings.json when you
# run `honeyhive-daemon run`.
#
# This script never blocks or slows a session: if the daemon is not installed
# it drains stdin and exits 0. The loud, actionable message about a missing or
# unconfigured daemon is emitted once per session by honeyhive-preflight.sh.

# Deliberately no `set -e`: a telemetry failure must never surface as a hook
# error on every tool call.
set -uo pipefail

# --- Resolve the daemon executable ------------------------------------------
# Priority: plugin config > environment override > PATH.
BIN="${CLAUDE_PLUGIN_OPTION_HONEYHIVE_DAEMON_BIN:-${HONEYHIVE_DAEMON_BIN:-}}"
if [ -z "$BIN" ]; then
    BIN="$(command -v honeyhive-daemon 2>/dev/null || true)"
fi

if [ -z "$BIN" ] || [ ! -x "$BIN" ]; then
    # Drain stdin so Claude Code never writes into a closed pipe, then get out
    # of the way. See honeyhive-preflight.sh for the user-facing diagnosis.
    cat >/dev/null 2>&1
    exit 0
fi

# --- Plugin config -> daemon environment ------------------------------------
# Values already exported in the shell win, so an existing HH_API_KEY keeps
# behaving exactly as it does for a plain `pip install honeyhive-daemon`.
# Sensitive plugin config cannot be substituted into shell-form hook commands,
# so it is read here from CLAUDE_PLUGIN_OPTION_* instead.
if [ -z "${HH_API_KEY:-}" ] && [ -n "${CLAUDE_PLUGIN_OPTION_HH_API_KEY:-}" ]; then
    export HH_API_KEY="$CLAUDE_PLUGIN_OPTION_HH_API_KEY"
fi
if [ -z "${HH_API_URL:-}" ] && [ -n "${CLAUDE_PLUGIN_OPTION_HH_API_URL:-}" ]; then
    export HH_API_URL="$CLAUDE_PLUGIN_OPTION_HH_API_URL"
fi

exec "$BIN" ingest claude-hook
