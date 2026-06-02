---
name: test-claude-honeyhive-trace
description: Run a real Claude Code session through honeyhive-daemon, verify HoneyHive export, and triage failures into daemon/skill fixes. Use when validating telemetry, regressions, or blog-launch readiness.
---

# Test Claude Code → HoneyHive trace

**Purpose:** exercise the real path (daemon + `claude -p`), compare logs and API export to expectations, and **fix what breaks** — in `honeyhive-daemon`, hooks, or this skill. A green checklist is not the goal; surfacing and resolving gaps is.

- **Human manual test:** daemon in a **foreground terminal** (second tab/window for `claude -p`). No tmux required.
- **Agent automation:** detached **tmux** only (one-shot shells must not use `run &`).

No wrapper scripts. Claude via **`claude -p`** (not `--bare`).

## Devin Secrets Needed

| Secret | Purpose |
|--------|---------|
| `CODING_AGENT_HH_API_KEY` | Production DP1 export key |
| `ANTHROPIC_API_KEY` | Claude Code CLI auth |

The API URL for production DP1 is `https://api.dp1.us.prod.honeyhive.ai`.

## Prerequisites

- Claude Code CLI (`claude`) authenticated
- `HH_API_KEY` (required). `HH_API_URL` only if not using the daemon default (`https://api.dp1.us.prod.honeyhive.ai`)
- `tmux` — agents only (humans use two terminal tabs)
- `honeyhive-daemon` + `honeyhive` in the **same** Python as the `honeyhive-daemon` on PATH
- HoneyHive CLI (`honeyhive`) on PATH — [install guide](https://honeyhiveai.github.io/honeyhive-cli/); not a Python shim named `honeyhive`

```bash
python -c "import honeyhive"
which honeyhive-daemon
which honeyhive
file "$(which honeyhive-daemon)" "$(which honeyhive)"
```

## Gotchas

- **Hooks use PATH** — install daemon into the interpreter that owns `honeyhive-daemon`.
- **Daemon before `claude -p`** — otherwise `session_name=(unknown)`.
- **Humans:** leave `honeyhive-daemon run` in the foreground in tab 1; run `claude -p` in tab 2. Stop with Ctrl-C in tab 1 or `honeyhive-daemon stop` from tab 2.
- **Agents:** never `run &` in a one-shot shell — use detached tmux (§3b).
- **No `--bare`** — skips hooks.
- **Let `claude -p` finish** — do not Ctrl+C; abrupt exit drops `session.end`.
- **`SessionEnd hook ... Hook cancelled` on stderr** — common in print mode; wait `sleep 12` and trust `daemon.log` + API, not stderr alone.
- **Artifact flush is async** — daemon loop ~5s; wait 12s before declaring failure.
- **Re-run `run` after upgrading** — refreshes absolute hook path in `~/.claude/settings.json`.
- **CLI stderr corrupts JSON** — when piping `honeyhive events search` to a file, use `2>/dev/null` to prevent the `HH_API_URL` deprecation warning from mixing into JSON output.
- **Spool reason race** — daemon background flush loop may modify spool entries between `status` calls. Capture `status` output in a single invocation.

## Workflow

### 1. Install daemon

```bash
cd <honeyhive-daemon-repo>
pip install -e .    # or: uv pip install -e .
```

### 2. Load credentials

```bash
set -a && source /path/to/.env && set +a
```

Optional: `honeyhive-daemon init` once per repo (not required if you only use env vars).

### 3a. Start daemon (human — foreground)

**Terminal 1** — leave this running:

```bash
cd /path/to/workdir
set -a && source /path/to/.env && set +a
honeyhive-daemon run
# Non-default plane only: honeyhive-daemon run --url "$HH_API_URL"
```

Optional isolated state: `export HH_DAEMON_HOME=/tmp/hh-smoke-$(whoami) && mkdir -p "$HH_DAEMON_HOME"` before `run`.

**Terminal 2** — steps 4–7 below. When done, stop the daemon: Ctrl-C in terminal 1, or `honeyhive-daemon stop` from terminal 2.

### 3b. Start daemon (agent — detached tmux)

```bash
WORKDIR=/path/to/workdir
tmux kill-session -t honeyhive 2>/dev/null || true
tmux new-session -d -s honeyhive bash -lc "
  set -a && source /path/to/.env && set +a
  cd \"$WORKDIR\"
  exec honeyhive-daemon run
  # Non-default plane: exec honeyhive-daemon run --url \"\$HH_API_URL\"
"
sleep 2
honeyhive-daemon status
honeyhive-daemon doctor
```

Optional: `HH_DAEMON_HOME` in the tmux block for isolated state.

Cleanup: `honeyhive-daemon stop` or `tmux kill-session -t honeyhive`.

### 4. Run Claude (print mode)

Use a prompt that forces **Read** and **Bash** so pre/post tool hooks export `tool.*` events (not just turns).

```bash
cd "$WORKDIR"
claude -p "$(cat <<'PROMPT'
Telemetry smoke test. Do these steps in order, then stop:

1. Read README.md in the current directory (first 10 lines is enough).
2. Run a Bash command to print the current UTC time, e.g. date -u +"%Y-%m-%dT%H:%M:%SZ".
3. In your final message, include: the first markdown heading from README, the date output, and this exact line on its own:
   honeyhive smoke ok
PROMPT
)"
```

If Claude blocks tool use, approve Read/Bash when prompted, or re-run with your usual non-interactive flags (e.g. permission-skipping) — the smoke test is only meaningful when at least one `tool.Read` and one `tool.Bash` (or equivalent) appear in the export.

### 5. Flush and capture session id

```bash
sleep 12
LOG="${HH_DAEMON_HOME:-$HOME/.honeyhive/daemon}/daemon.log"

# Prefer the session from this run's artifact line (tail -5 on session_id= hits stale failures)
SESSION=$(grep 'updated session artifact' "$LOG" | grep 'reason=session_end' | tail -1 \
  | sed -n 's/.*session_id=\([^ ]*\).*/\1/p')
# Fallback only if primary is empty — on a busy machine this tail is often an *older*
# session, not the smoke run you just did. Do not use fallback when primary matched.
[ -z "$SESSION" ] && SESSION=$(grep 'exported claude event' "$LOG" | grep 'event_name=session.start' | tail -1 \
  | sed -n 's/.*session_id=\([^ ]*\).*/\1/p')
echo "SESSION=$SESSION"
```

### 6. Verify logs

```bash
honeyhive-daemon status
grep "$SESSION" "$LOG" | grep -E 'exported|artifact|session\.end|spooled|fail'
```

Expect: `exported claude event` for `turn.*` and `tool.*` (e.g. `tool.Read`, `tool.Bash`); `session ended session_id=...` (logged in exporter immediately after API create — may still be missing if the hook process is killed mid-flight, but `export attempt` + API `session.end` is enough); `updated session artifact` with `session_end`; no `spooled claude event` for `$SESSION`.

```bash
grep "$SESSION" "$LOG" | grep 'exported claude event' | grep -E 'tool\.(Read|Bash)|turn\.'
```

### 7. Fetch events (HoneyHive CLI)

Use `--data-plane-url` (not deprecated `--base-url`). If `.env` sets `HH_API_URL`, the CLI may still print a **deprecation warning** on stderr — ignore it when exit code is 0.

```bash
honeyhive events search \
  --data-plane-url "$HH_API_URL" \
  --filters "[{\"field\":\"session_id\",\"value\":\"$SESSION\",\"operator\":\"is\",\"type\":\"string\"}]" \
  --limit 50 \
  2>/dev/null > session_export.json

python3 -c "
import json
d=json.load(open('session_export.json'))
events = sorted(d.get('events',[]), key=lambda x: x.get('start_time',0))
tools = [e for e in events if str(e.get('event_name','')).startswith('tool.')]
for e in events:
    print(e.get('event_name'), e.get('event_type'), f'duration={e.get(\"duration\")}')
print('count:', len(events), 'tool_events:', len(tools))
"
```

### 8. Triage — fix issues, don’t only report PASS

For each checklist miss or log/API anomaly:

1. **Reproduce** — note `SESSION`, relevant `daemon.log` lines, spool file, CLI export snippet.
2. **Classify** — export/spool, hooks/PATH, `session.end`/artifact, metrics/UI, skill doc wrong.
3. **Fix in repo** when the bug is in daemon/mappings/exporter (add or extend tests if the fix is non-trivial).
4. **Update this skill** if the workflow was wrong (commands, waits, session-id extraction).
5. **Re-run** steps 3–7 until checklist passes or file a tracked issue with evidence.

**Known gaps to watch for** (from prior runs; fix when reproduced):

| Gap | Where to look | Status |
|-----|---------------|--------|
| `session.end` missing after `claude -p` | Hook timeout, daemon not running | Fixed in HHAI-5521 (synthetic session.end) |
| Spool never drains | `grep -E 'spool|validation|error' "$LOG"` | Fixed in HHAI-5521 (retry cap) |
| Artifact 400 retries | Stale `state/sessions.json`; cap retries in daemon | Fixed in HHAI-5521 (ARTIFACT_MAX_RETRIES=3) |
| `total_tokens` / cost 0 in UI | `metadata.prompt_tokens` on turns | Pre-existing: Claude transcript lacks `usage` data |
| `coding_agent.model_count` 0 | `_compute_session_metrics` / transcript parser | Fixed in HHAI-5521 (count "assistant" records) |

Record fixes: commit on a branch, or short note in the task (issue id + session uuid).

## Pass checklist

- [ ] Spool empty (`status` + `spool/events.jsonl`)
- [ ] `session.start`, `turn.user`, `turn.agent`
- [ ] At least one `tool.*` event (e.g. `tool.Read`, `tool.Bash`) from the smoke prompt
- [ ] `session.end` in CLI export
- [ ] "session ended session_id=..." log line in daemon.log
- [ ] `coding_agent.model_count` ≥ 1 on session.start metrics
- [ ] `chat_history` on turn events has ≥ 1 entry (not empty `[]`)
- [ ] All events have `duration` > 0
- [ ] Transcript: `session.end` → `outputs.artifact` and/or `session.start` → `chat_history`

If anything fails, **do not stop at FAIL** — follow §8 and land a fix or a filed issue with reproduction.

## Common failures

| Symptom | Likely cause | What to do |
|--------|----------------|------------|
| `No module named 'honeyhive'` in hook | Wrong `honeyhive-daemon` on PATH | `pip install -e .`; restart `run` |
| Events stuck in spool | SDK/API errors | Fix exporter; grep `daemon.log` |
| `session.end` missing | Daemon not running, killed `claude`, or insufficient wait | `run` in foreground (or tmux); `claude -p`; `sleep 12`; check artifact line |
| `session ended` log missing but API has `session.end` | SessionEnd hook cancelled after `export attempt` | PASS if API + artifact OK; re-run with latest daemon (logs success inside exporter) |
| stderr `SessionEnd hook ... cancelled` | Print-mode race | Wait 12s; verify artifact + API anyway |
| `failed session artifact update` for old uuid | Stale state | Use SESSION from latest `session_end` artifact line |
| CLI URL flag error | Deprecated flag | `--data-plane-url "$HH_API_URL"` |
| CLI stderr: `HH_API_URL` deprecated | Env name legacy; flag is correct | Non-zero exit only is failure; search can still succeed |
| Wrong events in export | Used fallback session id on busy log | Prefer primary `session_end` artifact line; verify `SESSION` matches this run’s log lines |
| UI 0 total tokens | Missing rollup fields | Inspect event metadata; fix daemon or document backend gap |
| JSON parse error on CLI output | stderr deprecation warning mixed into redirected stdout | Use `2>/dev/null` when redirecting CLI output to file |
| Spool reason changes between status calls | Daemon flush loop modifies spool in background | Capture status in single call; race is in test harness, not code |
| No `tool.*` in export | Prompt too short or tools denied | Use §4 prompt; approve Read/Bash; check `daemon.log` for `tool.` exports |
