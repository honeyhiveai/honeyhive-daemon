---
name: status
description: Check whether Claude Code session telemetry is actually reaching HoneyHive. Use when the user asks if HoneyHive observability is working, why events or sessions are missing from HoneyHive, or to verify the honeyhive-daemon setup after installing the honeyhive-observability plugin.
allowed-tools: Bash, Read
---

# HoneyHive export status

Diagnose the HoneyHive telemetry pipeline for this machine. Work through the
checks in order and stop reporting at the first one that fails — each check
depends on the ones above it.

The pipeline is: Claude Code hook → `honeyhive-daemon ingest claude-hook` →
local spool → the daemon's background loop → HoneyHive API. The plugin only
supplies the first arrow. Everything after it is the `honeyhive-daemon` Python
package, which must be installed **and** running.

## 1. Is the daemon installed?

```bash
command -v honeyhive-daemon && honeyhive-daemon --version
```

Not found means nothing is being exported. Fix: `pip install honeyhive-daemon`.
If it is installed inside a virtualenv that Claude Code's shell does not see,
either put it on PATH or set the plugin's **Daemon executable** option with
`/plugin configure honeyhive-observability`.

## 2. Is it configured?

```bash
cat "${HH_DAEMON_HOME:-$HOME/.honeyhive/daemon}/state/config.json"
```

This file is written by `honeyhive-daemon run`. If it is missing, every hook
call is dropped on the floor without an error. Fix:

```bash
export HH_API_KEY=your-key
honeyhive-daemon run
```

Report the `base_url` back to the user, but **never print the `api_key`** —
report only whether it is present and non-empty.

## 3. Is it running?

```bash
DAEMON_HOME="${HH_DAEMON_HOME:-$HOME/.honeyhive/daemon}"
PID=$(cat "$DAEMON_HOME/daemon.pid" 2>/dev/null) && kill -0 "$PID" 2>/dev/null && echo "alive: $PID" || echo "not running"
```

The background loop runs every 5s and is what retries failed exports and
uploads session artifacts after a session ends. Without it, hook events still
spool to disk but nothing is delivered.

## 4. Are hooks firing?

```bash
tail -50 "${HH_DAEMON_HOME:-$HOME/.honeyhive/daemon}/daemon.log"
```

Look for `received claude hook` lines. Their absence while a session is active
means the hook is not reaching the daemon — check that the plugin is enabled
with `claude plugin list`, and that a stale user-level hook in
`~/.claude/settings.json` is not conflicting with it.

## 5. Is the spool draining?

```bash
wc -l "${HH_DAEMON_HOME:-$HOME/.honeyhive/daemon}/spool/events.jsonl" 2>/dev/null
```

A line count that keeps growing means exports are failing. Inspect the
`spool_reason` field on the most recent entries for the underlying error
(usually a bad API key or an unreachable `base_url`).

## Reporting

Summarize as a short checklist with a pass/fail per step, then give the single
most useful next command. Redact any API key you encounter.
