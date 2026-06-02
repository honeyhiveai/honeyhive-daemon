---
name: test-claude-honeyhive-trace
description: Run a real Claude Code session through honeyhive-daemon, verify HoneyHive export, and triage failures into daemon/skill fixes. Use when validating telemetry, regressions, or blog-launch readiness.
---

# Test Claude Code → HoneyHive trace

**Purpose:** exercise the real path (daemon + `claude -p`), compare logs and API export to expectations, and **fix what breaks** — in `honeyhive-daemon`, hooks, or this skill. A green checklist is not the goal; surfacing and resolving gaps is.

- **Human manual test:** daemon in a **foreground terminal** (second tab/window for `claude -p`). No tmux required.
- **Agent automation:** detached **tmux** only (one-shot shells must not use `run &`).

No wrapper scripts. Claude via **`claude -p`** (not `--bare`).

## Credentials (`.env` in repo root)

Use **only** the `.env` in the `honeyhive-daemon` repo root (same directory as `pyproject.toml`). It is **gitignored** — glob/search tools will not find it; check explicitly with `test -f .env` or `ls -la .env`.

Do **not** source env files from other monorepo directories (e.g. `pm-honeyhive/envs/`). A stale `HH_API_KEY` from elsewhere overrides hook config and causes 401/spool failures.

Expected variables:

| Variable | Purpose |
|----------|---------|
| `HH_API_KEY` | Production DP1 export key |
| `HH_API_URL` | Optional; default `https://api.dp1.us.prod.honeyhive.ai` |
| `HH_PROJECT` | Project name for export |

**Claude Code auth (OAuth first, API key fallback):**

1. Check: `claude auth status` — if `loggedIn: true` (e.g. `authMethod: claude.ai`), use subscription OAuth. **Unset `ANTHROPIC_API_KEY`** before `claude -p` so a stale env var does not override OAuth.
2. If not logged in, use `ANTHROPIC_API_KEY` from `.env` (or export one) and run `claude -p`.

Optional in `.env` only when OAuth is not set up: `ANTHROPIC_API_KEY`.

## Prerequisites

- Repo-root `.env` with `HH_API_KEY` (see above)
- Claude Code CLI (`claude`) — subscription OAuth **or** `ANTHROPIC_API_KEY` when `claude auth status` shows not logged in
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

- **Repo `.env` only** — source `./.env` from the `honeyhive-daemon` checkout. It is gitignored; do not hunt other monorepo env files.
- **Same `HH_API_KEY` everywhere** — daemon, hooks, and CLI must all see the key from that `.env`. Mismatch (e.g. saved daemon config vs stale env) causes partial 401 exports.
- **Claude auth: OAuth before API key** — if `claude auth status` shows logged in, unset `ANTHROPIC_API_KEY` before `claude -p`. Only rely on `ANTHROPIC_API_KEY` when OAuth is not configured.
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
uv pip install -e .
```

### 2. Load credentials

From the repo root (where `.env` lives — gitignored, not visible to repo search):

```bash
cd <honeyhive-daemon-repo>
test -f .env || { echo "Missing .env in repo root"; exit 1; }
set -a && source .env && set +a
```

Optional: `honeyhive-daemon init` once per repo (not required if `.env` supplies `HH_API_KEY`).

### 3a. Start daemon (human — foreground)

**Terminal 1** — leave this running:

```bash
cd <honeyhive-daemon-repo>
set -a && source .env && set +a
honeyhive-daemon run
# Non-default plane only: honeyhive-daemon run --url "$HH_API_URL"
```

Optional isolated state: `export HH_DAEMON_HOME=/tmp/hh-smoke-$(whoami) && mkdir -p "$HH_DAEMON_HOME"` before `run`.

**Terminal 2** — steps 4–7 below. When done, stop the daemon: Ctrl-C in terminal 1, or `honeyhive-daemon stop` from terminal 2.

### 3b. Start daemon (agent — detached tmux)

```bash
REPO=/path/to/honeyhive-daemon-repo
tmux kill-session -t honeyhive 2>/dev/null || true
tmux new-session -d -s honeyhive bash -lc "
  cd \"$REPO\"
  set -a && source .env && set +a
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

**Project skill (not `.claude/rules`):** the repo ships `hh-daemon-smoke` at `.claude/skills/hh-daemon-smoke/`. Claude Code discovers project skills from `.claude/skills/*` automatically when running from this repo. If testing another checkout, copy that skill into its `.claude/skills/`.

```bash
cd <honeyhive-daemon-repo>
set -a && source .env && set +a

# OAuth if already logged in; otherwise keep ANTHROPIC_API_KEY from .env
if claude auth status 2>/dev/null | grep -q '"loggedIn": true'; then
  unset ANTHROPIC_API_KEY
else
  test -n "${ANTHROPIC_API_KEY:-}" || { echo "Not logged in — run 'claude login' or set ANTHROPIC_API_KEY in .env"; exit 1; }
fi

claude -p "$(cat <<'PROMPT'
Telemetry smoke test. Do these steps in order, then stop:

1. Read README.md in the current directory (first 10 lines is enough).
2. Run a Bash command to print the current UTC time, e.g. date -u +"%Y-%m-%dT%H:%M:%SZ".
3. Follow the hh-daemon-smoke project skill (.claude/skills/hh-daemon-smoke/) — include its marker line in your final reply.
4. In your final message, include: the first markdown heading from README, the date output, these exact lines each on its own line:
   honeyhive smoke ok
   hh-daemon-claude-md-loaded
   hh-daemon-skill-loaded
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

### 7b. Concurrent sessions smoke

After the single-session smoke passes, run two `claude -p` sessions at the same time against the same daemon home. This catches state-file races, cross-session pending-tool collisions, duplicate `session.start` synthesis, and artifact updates that only work when one session is active.

Agents should use tmux sessions and `tmux wait-for` rather than shell background jobs:

```bash
cd <honeyhive-daemon-repo>
export HH_DAEMON_HOME=/tmp/hh-concurrent-$(whoami)-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$HH_DAEMON_HOME"

tmux kill-session -t honeyhive-concurrent-daemon 2>/dev/null || true
tmux new-session -d -s honeyhive-concurrent-daemon bash -lc "
  cd \"$PWD\"
  export HH_DAEMON_HOME=\"$HH_DAEMON_HOME\"
  set -a && source .env && set +a
  exec honeyhive-daemon run
"
sleep 2
honeyhive-daemon status

python3 - <<'PY'
import os
import shlex
import subprocess
import textwrap

repo = os.getcwd()
home = os.environ["HH_DAEMON_HOME"]
labels = ["a", "b"]

for label in labels:
    prompt = f"""Concurrent telemetry smoke test {label}. Do these steps in order, then stop:

1. Read README.md in the current directory (first 10 lines is enough).
2. Run a Bash command to print the current UTC time and the label, e.g. printf '{label} ' && date -u +"%Y-%m-%dT%H:%M:%SZ".
3. Follow the hh-daemon-smoke project skill (.claude/skills/hh-daemon-smoke/) — include its marker line in your final reply.
4. In your final message, include: the first markdown heading from README, the date output, these exact lines each on its own line:
   honeyhive concurrent smoke {label} ok
   hh-daemon-claude-md-loaded
   hh-daemon-skill-loaded
"""
    command = textwrap.dedent(f"""
        cd {shlex.quote(repo)}
        export HH_DAEMON_HOME={shlex.quote(home)}
        set -a
        source .env
        set +a
        if claude auth status 2>/dev/null | rg -q '"loggedIn": true'; then
          unset ANTHROPIC_API_KEY
        else
          test -n "${{ANTHROPIC_API_KEY:-}}" || {{ echo "Not logged in and ANTHROPIC_API_KEY missing"; tmux wait-for -S hh-concurrent-{label}; exit 1; }}
        fi
        claude -p {shlex.quote(prompt)}
        status=$?
        tmux wait-for -S hh-concurrent-{label}
        exit $status
    """).strip()
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", f"honeyhive-concurrent-{label}", "bash", "-lc", command],
        check=True,
    )

for label in labels:
    subprocess.run(["tmux", "wait-for", f"hh-concurrent-{label}"], check=True, timeout=300)
PY

sleep 12
```

Verify two session ids from `state/sessions.json` or the latest two `updated session artifact ... reason=session_end` log lines. For each concurrent session, fetch events as in §7 and expect:

- Exactly one `session.start` and one `session.end`
- One `turn.user`, one `turn.agent`, and at least one `tool.*`
- `chain.skills.listed` exactly once
- `session.start.outputs.chat_history` present
- `session.end.outputs.artifact` present
- `session.end.outputs.chat_history` present
- `session.start.metrics["coding_agent.event_count"] >= 1`
- `session.start.metrics["coding_agent.total_tokens"] >= 1`
- `session.start.metadata.total_tokens`, `prompt_tokens`, and `completion_tokens` are present when token usage exists
- Tool event `duration` uses Claude's reported tool runtime when available; `metadata.tool.wall_duration_ms` keeps pre-hook to post-hook wall time
- `turn.user.inputs.chat_history` and `turn.agent.inputs.chat_history` are non-empty
- The `a` session contains `honeyhive concurrent smoke a ok` and not the `b` marker; the `b` session contains `honeyhive concurrent smoke b ok` and not the `a` marker
- `daemon.log` has no `malformed`, `failed`, `spooled`, or `synthesized session.start` lines for either session

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
| `total_tokens` / cost 0 in UI | UI/session aggregates read flat `metadata.total_tokens`, `metadata.prompt_tokens`, `metadata.completion_tokens`, and `metadata.cost`; custom metrics alone are not enough | Fixed: read `message.usage`, dedupe by `requestId`, update standard token metadata on `session.start` |
| `tool_count` 0 with tools present | Metrics looked for legacy `tool_use` records only | Fixed: count nested `tool_use` blocks in assistant messages |
| Duplicate tokens on tool events | Same `requestId` on batched tools | Fixed: attach usage to first tool event per `requestId` only |
| `coding_agent.model_count` 0 | `_compute_session_metrics` / transcript parser | Fixed in HHAI-5521 (count "assistant" records) |
| `chain.instructions.loaded` empty outputs | Hook has path only; daemon must read file | Fixed: `_enrich_instructions_loaded` reads disk |
| `chain.skills.listed` missing | Skills are transcript attachments, not InstructionsLoaded | Fixed: export from `skill_listing` attachment once per session |
| Duplicate `chain.skills.listed` | Concurrent hooks race before claim | Fixed: `claim_skills_listed_export` with file lock |
| Duplicate `session.start` during startup | `SessionStart` and `InstructionsLoaded` hooks race on session state | Fixed: locked session state writes; `InstructionsLoaded` does not synthesize `session.start` |
| Cross-session leakage under concurrent `claude -p` | State keyed too broadly or pending tools not keyed by session | Run §7b; fix any mixed markers, tool merges, or artifacts |
| Server-side evaluator errors in metrics | Stale/invalid evaluator definitions registered in HoneyHive | Fix `evaluators/definitions.py`, run tests, then update registered metrics with `uv run python -m evaluators.register` |
| Repo/private evaluator names in public exports | Local/private evaluator definitions leaked into repo | Remove from `evaluators/definitions.py`; keep public evaluator set agent-agnostic |
| Inferred nested tool tree | Tool event interval used pre-to-post wall time and overlapped with sibling tools | Use reported tool runtime for event `duration`; keep wall time in `metadata.tool.wall_duration_ms` |

Record fixes: commit on a branch, or short note in the task (issue id + session uuid).

## Pass checklist

- [ ] Spool empty (`status` + `spool/events.jsonl`)
- [ ] `session.start`, `turn.user`, `turn.agent`
- [ ] At least one `tool.*` event (e.g. `tool.Read`, `tool.Bash`) from the smoke prompt
- [ ] `session.end` in CLI export
- [ ] "session ended session_id=..." log line in daemon.log
- [ ] `coding_agent.model_count` ≥ 1 on session.start metrics
- [ ] `coding_agent.event_count` ≥ 1 on session.start metrics
- [ ] `coding_agent.total_tokens` ≥ 1 on session.start metrics (after session end artifact push)
- [ ] `session.start.metadata.total_tokens`, `metadata.prompt_tokens`, and `metadata.completion_tokens` are present when token usage exists
- [ ] `chat_history` on turn events has ≥ 1 entry (not empty `[]`)
- [ ] `chain.instructions.loaded` for repo `CLAUDE.md` with non-empty `outputs.content`
- [ ] `chain.skills.listed` present exactly once
- [ ] `chain.skills.listed` → `outputs.names` includes `hh-daemon-smoke` (from `.claude/skills/hh-daemon-smoke/`)
- [ ] `turn.agent` includes marker `hh-daemon-skill-loaded` when smoke prompt references the project skill
- [ ] All events have `duration` > 0
- [ ] Tool events with raw `duration_ms` have matching event `duration` and retain `metadata.tool.wall_duration_ms`
- [ ] No server-side evaluator metric values contain `ERROR:`
- [ ] Transcript: `session.start` → `outputs.chat_history`; `session.end` → `outputs.artifact` and `outputs.chat_history`
- [ ] Concurrent smoke (§7b): two simultaneous sessions each have exactly one `session.start`, one `session.end`, one artifact, metrics, non-empty turn history, and no marker leakage

If anything fails, **do not stop at FAIL** — follow §8 and land a fix or a filed issue with reproduction.

## Common failures

| Symptom | Likely cause | What to do |
|--------|----------------|------------|
| `No module named 'honeyhive'` in hook | Wrong `honeyhive-daemon` on PATH | `pip install -e .`; restart `run` |
| `401 createEventLegacy` / spool buildup | Wrong `HH_API_KEY` (often sourced from another repo’s env) | Source **only** `<repo>/.env`; restart `run` with same env |
| `Invalid API key` from Claude | Stale `ANTHROPIC_API_KEY` while OAuth is active, or missing key when not logged in | If logged in: `unset ANTHROPIC_API_KEY`. If not: add valid `ANTHROPIC_API_KEY` to `.env` or run `claude login` |
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
