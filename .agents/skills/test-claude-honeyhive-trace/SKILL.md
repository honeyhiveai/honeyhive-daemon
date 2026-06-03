---
name: test-claude-honeyhive-trace
description: Run a real Claude Code session through honeyhive-daemon, verify HoneyHive export, and triage failures into daemon/skill fixes. Use when validating telemetry, regressions, or blog-launch readiness.
---

# Test Claude Code → HoneyHive trace

Exercise daemon + `claude -p`, verify logs + API export, **fix** daemon/hooks/skill gaps — not just PASS.

- **Human:** foreground `honeyhive-daemon run` in tab 1; `claude -p` in tab 2.
- **Agent:** detached **tmux** only (no `run &` in one-shot shells).

No wrapper scripts. Use `claude -p` (not `--bare`).

## Setup

**`.env` only** at repo root (gitignored — `test -f .env`)


| Variable            | Purpose                                                  |
| ------------------- | -------------------------------------------------------- |
| `HH_API_KEY`        | DP1 export                                               |
| `HH_API_URL`        | Optional; default `https://api.dp1.us.prod.honeyhive.ai` |
| `ANTHROPIC_API_KEY` | Only if `claude auth status` is not logged in            |


**Claude auth:** OAuth first — if `loggedIn: true`, **unset `ANTHROPIC_API_KEY`** before `claude -p`.

**Prereqs:** `uv pip install -e .`; same Python for `honeyhive-daemon` + `import honeyhive`; HoneyHive CLI on PATH ([install](https://honeyhiveai.github.io/honeyhive-cli/)); `tmux` for agents.

```bash
python -c "import honeyhive"
which honeyhive-daemon honeyhive
```

**Rules:** daemon before `claude -p`; let `claude -p` finish (no Ctrl-C); `sleep 12` before judging export; re-run `honeyhive-daemon run` after upgrade; pipe CLI with `2>/dev/null`; capture `status` once (spool race).

## Workflow

### 1–2. Install + env

```bash
cd <honeyhive-daemon-repo>
uv pip install -e .
test -f .env || exit 1
set -a && source .env && set +a
```

### 3a. Daemon (human)

Tab 1: `honeyhive-daemon run` (optional: `HH_DAEMON_HOME=/tmp/hh-smoke-$(whoami)`). Tab 2: steps 4–7. Stop: Ctrl-C or `honeyhive-daemon stop`.

### 3b. Daemon (agent)

```bash
REPO=/path/to/honeyhive-daemon-repo
tmux kill-session -t honeyhive 2>/dev/null || true
tmux new-session -d -s honeyhive bash -lc "
  cd \"$REPO\" && set -a && source .env && set +a && exec honeyhive-daemon run
"
sleep 2 && honeyhive-daemon status && honeyhive-daemon doctor
```

Cleanup: `honeyhive-daemon stop` or `tmux kill-session -t honeyhive`.

### 4. Claude smoke

Prompt must trigger **Read** + **Bash**.

```bash
cd <honeyhive-daemon-repo>
set -a && source .env && set +a
if claude auth status 2>/dev/null | grep -q '"loggedIn": true'; then
  unset ANTHROPIC_API_KEY
else
  test -n "${ANTHROPIC_API_KEY:-}" || exit 1
fi

claude -p "$(cat <<'PROMPT'
Telemetry smoke test. Do these steps in order, then stop:

1. Read README.md in the current directory (first 10 lines is enough).
2. Run a Bash command to print the current UTC time, e.g. date -u +"%Y-%m-%dT%H:%M:%SZ".
3. In your final message, include: the first markdown heading from README, the date output, these exact lines each on its own line:
   honeyhive smoke ok
   hh-daemon-claude-md-loaded
PROMPT
)"
```

Approve Read/Bash if prompted. `SessionEnd hook ... cancelled` on stderr is often OK — trust log + API after wait.

### 5–6. Session id + logs

```bash
sleep 12
LOG="${HH_DAEMON_HOME:-$HOME/.honeyhive/daemon}/daemon.log"
SESSION=$(grep 'updated session artifact' "$LOG" | grep 'reason=session_end' | tail -1 \
  | sed -n 's/.*session_id=\([^ ]*\).*/\1/p')
[ -z "$SESSION" ] && SESSION=$(grep 'exported claude event' "$LOG" | grep 'event_name=session.start' | tail -1 \
  | sed -n 's/.*session_id=\([^ ]*\).*/\1/p')
echo "SESSION=$SESSION"

honeyhive-daemon status
grep "$SESSION" "$LOG" | grep -E 'exported|artifact|session\.end|spooled|fail'
grep "$SESSION" "$LOG" | grep 'exported claude event' | grep -E 'tool\.(Read|Bash)|turn\.'
```

Expect `turn.*`, `tool.*`, `session_end` artifact; no spool for `$SESSION`.

### 7. API export

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

### 7b. Concurrent (optional)

Isolated home; daemon in tmux; two `claude -p` sessions via tmux + `wait-for` (not `&`):

```bash
cd <honeyhive-daemon-repo>
export HH_DAEMON_HOME=/tmp/hh-concurrent-$(whoami)-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$HH_DAEMON_HOME"
tmux kill-session -t honeyhive-concurrent-daemon 2>/dev/null || true
tmux new-session -d -s honeyhive-concurrent-daemon bash -lc "
  cd \"$PWD\" && export HH_DAEMON_HOME=\"$HH_DAEMON_HOME\"
  set -a && source .env && set +a && exec honeyhive-daemon run
"
sleep 2 && honeyhive-daemon status
```

For each label in `a` `b`: new tmux session running §4-style prompt with `Concurrent telemetry smoke test {label}`, marker `honeyhive concurrent smoke {label} ok`, then `tmux wait-for -S hh-concurrent-{label}`; parent `tmux wait-for hh-concurrent-{label}`. Reuse §4 auth unset logic. `sleep 12`; verify two session ids; per §7 + no cross-marker leakage; log free of `malformed`/`spooled`/`synthesized session.start`.

### 8. Triage

On miss: capture `SESSION`, log snippet, spool → classify (export/hooks/artifact/metrics) → fix daemon/tests or update this skill → re-run 3–7. Do not stop at FAIL.

## Pass checklist

All events are correctly there in Session export

Each event in session has inputs and outputs correctly. 



