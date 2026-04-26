# RFC: Improve `stop` and add a `disable` lifecycle

Status: Draft (discussion)
Author: @dhruv-hhai
Related: `sunny/start-restart-cli` (#TBD) — adds `start`/`restart`, complements but does not address this RFC

## Problem (real incident)

A user (me) noticed `honeyhive-daemon` actively exporting traces from a running Claude Code session and tried to stop it. The path they followed:

1. `ps aux | grep honeyhive-daemon` → no results.
2. `pgrep -fl honeyhive` → no results.
3. `launchctl list | grep -i honeyhive`, `docker ps`, LaunchAgents/LaunchDaemons → no results.
4. Inspected `~/.honeyhive/daemon/daemon.log` → events were being written every few seconds, including from the *current* session.
5. Checked `~/.honeyhive/daemon/daemon.pid` → file did not exist.

The user concluded — correctly — that there was no resident process to kill, but exports were happening regardless. The actual export traffic comes from short-lived `honeyhive-daemon ingest claude-hook` subprocesses spawned by Claude Code per hook event. Each one writes a log line, exports synchronously, and exits in <500ms.

`honeyhive-daemon stop` does not stop these. It only stops the optional spool flush-loop process started by `honeyhive-daemon start`. If `start` was never run, `stop` exits 1 with "No daemon PID file found" — even though telemetry is actively flowing.

The user reasonably believed `stop` would stop telemetry. It didn't. To actually stop ingestion they had to:

- Hand-edit `~/.claude/settings.json` to remove ~17 hook entries, **and**
- `pip uninstall honeyhive-daemon` so any remaining hook entries fail fast.

This is not a discoverable workflow.

## Root cause

The CLI exposes commands that match an internal model (`start` = spool flush loop, `stop` = kill that loop) rather than the user's mental model (`start` = "begin sending telemetry", `stop` = "stop sending telemetry"). Two specific gaps:

### Gap 1: `stop` doesn't stop the hot path

The flush loop handles spooled retries and late artifact pushes. The hot path — Claude Code hook → `ingest claude-hook` subprocess → synchronous export — bypasses the daemon process entirely. So killing the daemon doesn't kill exports.

### Gap 2: No symmetric `disable`

`start` *adds* hook entries to `~/.claude/settings.json` (via `install_claude_hooks`) and a git post-commit hook (via `install_post_commit_hook`). There is no command that removes them. Users must:

- Manually edit settings.json (error-prone — must preserve unrelated hooks, valid JSON, exact structure).
- Manually remove the post-commit hook.
- Optionally uninstall the package so any leftover hook lines fail loudly instead of silently re-installing on next `start`.

### Gap 3: `stop` is silent about what it doesn't do

When `stop` succeeds, it prints "Sent SIGTERM to daemon (PID N)." A reasonable user reads this as "telemetry is now off." It isn't.

### Gap 4: PID-file-only liveness is fragile

- File can be deleted while process keeps running (manual `rm`, daemon-home wipe) — `stop` won't find it.
- File can survive a hard kill / OS reboot — `stop` errors on a non-existent PID, then cleans up.
- Sunny's branch handles stale-cleanup but doesn't add a fallback (e.g. `pgrep -f` against a known argv marker).

## Proposal

### 1. Add `honeyhive-daemon disable`

The "off switch" users actually want.

```
honeyhive-daemon disable [--keep-state] [--keep-config]
```

Removes:
- Claude Code hook entries from `~/.claude/settings.json` whose command is `honeyhive-daemon ingest claude-hook` (matches what `install_claude_hooks` added — preserves all other hooks, preserves event-type entries that have other hooks).
- Git post-commit hook line installed by `install_post_commit_hook`.

Preserves by default:
- `~/.honeyhive/daemon/state/`, `spool/`, `sessions/`, `config.json` — so re-enabling restores prior state.

Flags:
- `--keep-state`: explicit; default is to keep.
- `--purge`: opposite — also removes `~/.honeyhive/daemon/` entirely.
- Calling without args is idempotent (no-op if already disabled).

After running, exporting halts immediately on next session start. Existing Claude sessions cache hooks at session-start time, so currently-open sessions continue exporting until they close. We should call this out in the disable output.

### 2. Tighten `honeyhive-daemon stop`

Keep semantics (stop the long-running flush-loop process) but:

- **Fallback to argv match** when PID file is missing/stale: `pgrep -f 'honeyhive-daemon run'`. Use a unique argv marker (`honeyhive-daemon run --internal-daemon-loop` or set a process title via `setproctitle`) so we don't accidentally kill `ingest claude-hook` invocations.
- **Idempotent**: exit 0 with "Daemon is not running" when nothing is found, instead of exit 1.
- **Always print a footer** explaining what stop does *not* do:
  ```
  Note: this stops the spool flush loop only. Per-hook telemetry export
  continues for any open Claude Code sessions. Run 'honeyhive-daemon
  disable' to remove hooks and fully stop ingestion.
  ```
- **`--force` flag**: SIGKILL after grace period; also kills any `ingest claude-hook` processes currently running (best-effort).

### 3. Add `honeyhive-daemon doctor`

Surfaces every active telemetry surface so users can see *why* exports are still flowing:

```
$ honeyhive-daemon doctor
Daemon process:        not running
Claude hooks:          17 installed in ~/.claude/settings.json
Git post-commit hook:  installed in <repo>/.git/hooks/post-commit
Spool:                 0 pending events
Recent log activity:   12 events in the last 60s  ← exports ARE active
Config:                project=<name>, base_url=<url>

Telemetry is currently ACTIVE via Claude Code hooks.
To stop: honeyhive-daemon disable
```

This is the diagnostic users need when their first instinct ("stop the daemon") doesn't match observed behavior.

### 4. Documentation: rename the user-facing model

`stop` currently sounds like a kill switch. Either:

- (a) Rename it to something narrower (`stop-flush-loop`?) and surface `disable` as the primary off-switch; or
- (b) Keep `stop` as an alias that warns and points to `disable` when hooks are still installed.

Lean toward (b) — less breakage, clearer migration path.

## Out of scope for this RFC

- Whether the spool flush loop should exist at all (could be replaced by per-hook spool flushing).
- Whether ingestion should run in-process under Claude Code rather than via subprocess-per-hook (would solve the lifecycle issue at the root, but is a much larger change).

## Open questions

1. Should `disable` require confirmation (`--yes`)? Hook removal is reversible (`start` re-installs), so I lean no.
2. Should `start` warn / refuse if hooks were previously `disable`d, or always re-install silently? Current `install_claude_hooks` is idempotent; I'd keep that.
3. Do we need a per-repo disable that only removes the post-commit hook, leaving Claude hooks intact?
