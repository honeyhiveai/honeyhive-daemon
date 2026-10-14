#!/usr/bin/env bash
# SessionStart preflight for the honeyhive-observability plugin.
#
# Installing the plugin wires the hooks, but the export itself is done by the
# `honeyhive-daemon` Python package: the hooks pipe payloads to it, and its
# background loop flushes spooled events and pushes session artifacts. If that
# package is missing or was never started, every hook silently no-ops and
# nothing reaches HoneyHive.
#
# This runs once per session and reports that situation loudly rather than
# letting the session look instrumented when it isn't. It never blocks the
# session: a non-zero exit here is a non-blocking hook error whose stderr is
# shown to the user.

set -uo pipefail

# Claude Code writes the SessionStart payload to stdin. Drain it so it never
# writes into a closed pipe.
cat >/dev/null 2>&1

PROBLEMS=()

# --- 1. Is the daemon executable resolvable? --------------------------------
BIN="${CLAUDE_PLUGIN_OPTION_HONEYHIVE_DAEMON_BIN:-${HONEYHIVE_DAEMON_BIN:-}}"
if [ -z "$BIN" ]; then
    BIN="$(command -v honeyhive-daemon 2>/dev/null || true)"
fi

if [ -z "$BIN" ] || [ ! -x "$BIN" ]; then
    PROBLEMS+=(
        "The 'honeyhive-daemon' executable was not found on PATH."
        "  Install it:  pip install honeyhive-daemon"
        "  Already installed in a virtualenv? Point the plugin at it:"
        "    /plugin configure honeyhive-observability   ->  Daemon executable"
    )
    printf 'HoneyHive observability is NOT capturing this session.\n\n' >&2
    printf '%s\n' "${PROBLEMS[@]}" >&2
    printf '\nDocs: https://github.com/honeyhiveai/honeyhive-daemon\n' >&2
    exit 1
fi

# --- 2. Has the daemon ever been configured? --------------------------------
DAEMON_HOME="${HH_DAEMON_HOME:-$HOME/.honeyhive/daemon}"
if [ ! -f "$DAEMON_HOME/state/config.json" ]; then
    PROBLEMS+=(
        "The daemon has no config at $DAEMON_HOME/state/config.json."
        "  Hooks will fire but every event is dropped until you start it once:"
        "    export HH_API_KEY=your-key"
        "    honeyhive-daemon run"
    )
fi

# --- 3. Is the daemon process alive? ----------------------------------------
# The background loop (every 5s) retries failed exports and pushes session
# artifacts after session.end. Without it, artifacts and retries are lost.
PID_FILE="$DAEMON_HOME/daemon.pid"
DAEMON_ALIVE=0
if [ -f "$PID_FILE" ]; then
    PID="$(tr -d '[:space:]' <"$PID_FILE" 2>/dev/null || true)"
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        DAEMON_ALIVE=1
    fi
fi

if [ "$DAEMON_ALIVE" -eq 0 ]; then
    PROBLEMS+=(
        "The daemon does not appear to be running (no live PID at $PID_FILE)."
        "  Session artifacts and export retries need it alive alongside Claude Code:"
        "    honeyhive-daemon run"
    )
fi

if [ "${#PROBLEMS[@]}" -eq 0 ]; then
    exit 0
fi

printf 'HoneyHive observability is installed but not exporting.\n\n' >&2
printf '%s\n' "${PROBLEMS[@]}" >&2
printf '\nRun /honeyhive-observability:status for a full check.\n' >&2
exit 1
