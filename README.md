# Coding Agent Exporters

Export sessions from coding agent platforms into HoneyHive for observability and evaluation.

This repo contains two independent exporters:

| Exporter | Install | Agents | How it works |
|---|---|---|---|
| **Claude Code Daemon** | `pip install honeyhive-daemon` | Claude Code | Local daemon process, captures via Claude Code hooks in real-time |
| **Devin Exporter** | `pip install -r devin/requirements.txt` | Devin | Standalone script, polls Devin's API and batch-syncs sessions |

The daemon is published to PyPI as `honeyhive-daemon`. The Devin exporter is **not** included in the pip package — it lives in `devin/` and is run directly from this repo.

## Claude Code Daemon

A local daemon that captures Claude Code activity via hooks and exports structured events to HoneyHive for observability and evaluation.

- Installs Claude Code hooks automatically at the user level
- Merges pre-hook and post-hook into single tool events with accurate duration
- Uses real tool names for event names (e.g. `tool.Bash`, `tool.Edit`, `tool.Grep`)
- Emits turn events (`turn.user`, `turn.agent`) as `model` type with `chat_history`
- Enriches tool events with thinking/reasoning context from the session transcript
- Uploads session artifacts via the daemon's background loop (every 5s) to avoid hook timeouts
- Optionally emits lightweight git `chain.commit_link` events on `post-commit`
- Resilient event spooling — failed exports are retried every 5s

### Quickstart

**Per-project setup (recommended):**

```bash
pip install honeyhive-daemon

# In your project repo
honeyhive-daemon init --project my-project
# Creates .honeyhive/config.json and .honeyhive/config.local.json
# Edit .honeyhive/config.local.json to set your api_key_env

HH_API_KEY=your-key honeyhive-daemon run
```

**Single-project (legacy):**

```bash
honeyhive-daemon run --key $HH_API_KEY --project my-project
```

The daemon stores local state in `~/.honeyhive/daemon/` and installs Claude hooks in `~/.claude/settings.json`.

### Running in the background

```bash
honeyhive-daemon run --key $HH_API_KEY --url $HH_API_URL &
```

To stop it:

```bash
honeyhive-daemon stop
```

`stop` sends SIGTERM to the running daemon via its PID file (`~/.honeyhive/daemon/daemon.pid`). Only one instance can run at a time — attempting to start a second will print an error and exit.

### Events

Each Claude Code session produces a tree of events in HoneyHive:

| Event name | Type | Description |
|------------|------|-------------|
| `session.start` | `session` | Root event. All other events are children of this. |
| `turn.user` | `model` | User prompt. `inputs.chat_history` accumulates the full conversation so far; `outputs.content` is the new message. |
| `turn.agent` | `model` | Assistant response. `inputs.chat_history` accumulates the full conversation so far; `outputs.content` is the new message. |
| `tool.{ToolName}` | `tool` | Tool use (e.g. `tool.Bash`, `tool.Edit`, `tool.Read`, `tool.Grep`). Pre and post hooks are merged into a single event with `start_time`/`end_time` duration. |
| `session.end` | `chain` | Marks session completion. |
| `chain.commit_link` | `chain` | Git commit metadata (requires `--repo`). |

Tool events include `inputs.thinking` when a reasoning block precedes the tool call in the transcript. Pre-hook events that never receive a matching post-hook are exported as orphans after 60s.

#### Session artifacts

When a session ends (or goes idle), the daemon pushes two different views of the conversation:

- **`session.start`** receives `outputs.chat_history` — this is the user-facing conversation: the back-and-forth of user messages and assistant responses, basically what you'd see in the chat UI. Useful for reviewing what was said and evaluating response quality.
- **`session.end`** receives `outputs.artifact` containing the full session transcript — this is the complete trajectory of everything that happened under the hood, including tool calls, reasoning/thinking blocks, and internal processing steps. Think of it as the "behind the scenes" view of how the agent actually worked through the task.

This split lets you look at the same session from two angles: the conversation-level view for understanding what the user experienced, and the trajectory-level view for debugging agent behavior and understanding how it got there.

### State directory

All daemon state lives under `~/.honeyhive/daemon/` (override with `HH_DAEMON_HOME`):

| File | Purpose |
|------|---------|
| `state/config.json` | Persisted daemon configuration |
| `state/sessions.json` | Session index (transcript paths, timestamps, artifact status) |
| `state/pending_tools.json` | Buffered pre-hook tool events awaiting their post-hook |
| `state/chat_histories.json` | Accumulated chat history per session for turn events |
| `spool/events.jsonl` | Retry queue for failed exports |
| `daemon.log` | Timestamped daemon log |
| `daemon.pid` | Process ID file |

### CLI reference

| Command | Description |
|---------|-------------|
| `honeyhive-daemon run` | Start the daemon, install hooks, and flush queued events. |
| `honeyhive-daemon stop` | Stop the running daemon. |
| `honeyhive-daemon status` | Show config and pending spool event count. |
| `honeyhive-daemon doctor` | Check that hooks and config are correctly installed. |

#### `run` options

| Flag | Env var | Description |
|------|---------|-------------|
| `--key` | `HH_API_KEY` | HoneyHive API key (required). |
| `--url` | `HH_API_URL` | HoneyHive base URL (default: `https://api.honeyhive.ai`). |
| `--project` | `HH_PROJECT` | HoneyHive project name (default: repo/directory name). |
| `--repo PATH` | | Git repo to attach commit events to. |
| `--ci` | | Enable CI mode. |

### Troubleshooting

If events aren't showing up in HoneyHive, work through these checks in order:

1. **Is the daemon running?** Check `~/.honeyhive/daemon/daemon.pid` and verify the process is alive with `ps`.
2. **Check the log.** `tail -100 ~/.honeyhive/daemon/daemon.log` — look for `spooled` (export failures) or missing `received claude hook` entries.
3. **Verify config.** `cat ~/.honeyhive/daemon/state/config.json` — confirm API key, project, and base URL are correct.
4. **Hooks installed?** Run `honeyhive-daemon doctor` or inspect `~/.claude/settings.json` for the hook command.
5. **Spool buildup?** `wc -l ~/.honeyhive/daemon/spool/events.jsonl` — if events are piling up, check the `spool_reason` field for error details.
6. **PATH issues.** Ensure `honeyhive-daemon` is on PATH in the shell context Claude Code uses (`which honeyhive-daemon`). Virtualenv installations may not be visible to hooks.

A detailed troubleshooting guide is available in [`skills/honeyhive-daemon-debug/SKILL.md`](skills/honeyhive-daemon-debug/SKILL.md).

### Evaluators

The repo includes a suite of server-side evaluators that run automatically on session events in HoneyHive. These are agent-agnostic — they work on both Claude Code and Devin sessions.

**Python evaluators** (run on every `session.end` event):

| Evaluator | What it measures | Threshold |
|---|---|---|
| Bash Ratio | % of tool calls that are bash/shell (neutral metric) | — |
| Bash Edit Misuse | % of file edits via `sed -i`/`awk -i` instead of native Edit | 50% |
| File Search Spam | % of tool calls that are file_search/glob | 40% |
| Permission Bottleneck | % of events that are permission prompts | 15% |
| Subagent Lifecycle | Whether subagent starts match stops | boolean |
| Session Event Count | Total events in session | 500 |
| Tool-to-Model Ratio | Tool calls per reasoning step | 30 |

**LLM evaluators** (disabled by default, 20% sampling):

| Evaluator | What it measures |
|---|---|
| Task Completion | 1-5 rating of whether the agent completed the user's task |
| Approach Efficiency | 1-5 rating of whether the agent's approach was efficient |

**Client-side metrics** (computed by the daemon and attached to session events):

`coding_agent.total_events`, `coding_agent.tool_count`, `coding_agent.model_count`, `coding_agent.unique_tools`, `coding_agent.bash_ratio`, `coding_agent.search_ratio`, `coding_agent.tool_model_ratio`, `coding_agent.permission_ratio`, `coding_agent.has_errors`, `coding_agent.subagent_balanced`

#### Managing evaluators

```bash
# Register/update evaluators in HoneyHive
python -m evaluators.register

# Preview without making changes
python -m evaluators.register --dry-run

# List existing evaluators
python -m evaluators.register --list

# Batch evaluate existing sessions (retroactive)
python -m evaluators.batch_evaluate --limit 100 --pages 5 --verbose
```

### Enforcement hooks

The `enforcement/` directory contains ready-to-install Claude Code hooks and configuration templates:

| File | Purpose |
|---|---|
| `enforcement/hooks/prefer-dedicated-tools.sh` | PreToolUse hook: nudges agent toward native Edit for `sed -i`/`awk -i` |
| `enforcement/hooks/session-budget.sh` | PreToolUse hook: blocks after N tool calls (default 500), warns at 80% |
| `enforcement/CLAUDE.md.example` | Behavioral guidelines encouraging CLI tools for discovery, Edit for modifications |
| `enforcement/settings.json.ci-example` | Full CI settings template with permissions, hooks, and env vars |

To install the hooks, copy to `.claude/hooks/` and add to your `.claude/settings.json` (see `enforcement/settings.json.ci-example` for a complete example).

## Devin Exporter

A standalone Python script that polls Devin's API and batch-syncs sessions into HoneyHive. **Not included in the `honeyhive-daemon` pip package** — run directly from this repo.

Unlike the Claude Code daemon (which captures events in real-time via hooks), the Devin exporter works by polling Devin's REST API on an interval and syncing any new or updated sessions. It supports incremental sync, automatic upserts, and both one-shot and continuous daemon modes.

### How it works

1. Fetches sessions from Devin using `updated_after` for incremental sync
2. Maps each Devin session to a HoneyHive session event via `POST /session/start`
3. Fetches session messages and creates child events for each user/agent message
4. Fetches internal processing events (shell commands, git operations, browser actions, file edits) and exports them as tool/model child events
5. Updates the session event with `outputs.chat_history` (the full user↔agent conversation) and `outputs.structured_output` when available
6. Emits a `session.end` chain event with `outputs.artifact` for completed sessions (status: finished/stopped/failed) — this enables server-side evaluators to run on Devin sessions
7. On subsequent syncs, updates previously-synced sessions via `PUT /events` and incrementally syncs new messages and internal events
8. Tracks sync state (last sync timestamp + session ID mapping + message/event counts) in a local JSON file

Auto-detects Devin API version from key prefix: `apk_*` uses v1, `cog_*` uses v3. For v3 keys, messages are fetched from the dedicated paginated `/v3/.../messages` endpoint.

### Differences from Claude Code Daemon

| | Claude Code Daemon | Devin Exporter |
|---|---|---|
| **Capture method** | Real-time hooks (stdin JSON per event) | Polling REST API |
| **Install** | `pip install honeyhive-daemon` | `pip install -r devin/requirements.txt` |
| **Runs as** | Background daemon process | Standalone script or cron job |
| **Event granularity** | Individual tool calls with pre/post merge, thinking blocks | Messages + internal events (shell, git, browser, file) |
| **Idempotency** | State-based (spool + session index) | State-based (sync_state.json) + deterministic UUID5 event IDs |
| **Session artifacts** | Full transcript JSONL from Claude Code | Reconstructed from messages |

### Required Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DEVIN_API_KEY` | Yes | Devin API key. Prefix `apk_*` for v1 API, `cog_*` for v3 API. |
| `DEVIN_ORG_ID` | For v3 keys | Devin organization ID. Required when using `cog_*` keys. The script attempts auto-discovery via `/enterprise/self` but this requires enterprise-level access. |
| `HH_API_KEY` | Yes | HoneyHive API key. |
| `HH_API_URL` | Yes | HoneyHive data plane URL (e.g. `https://api.honeyhive.ai`). |
| `HH_PROJECT` | Yes | HoneyHive project name to export sessions into. |

### Optional Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `STATE_FILE_PATH` | `./sync_state.json` | Path to the JSON file that tracks sync state between runs. |
| `SYNC_INTERVAL_SECONDS` | `60` | Default polling interval for daemon mode (can also be set via `--interval`). |

### Usage

```bash
pip install -r devin/requirements.txt
```

**One-shot sync** (fetch and export, then exit):

```bash
python devin/devin_to_honeyhive.py
```

**Daemon mode** (continuous polling):

```bash
python devin/devin_to_honeyhive.py --daemon --interval 30
```

**Verbose logging:**

```bash
python devin/devin_to_honeyhive.py --verbose
```

### CLI Options

| Flag | Description |
|------|-------------|
| `--daemon` | Run continuously, polling at the configured interval. |
| `--interval SECONDS` | Polling interval in seconds (default: 60). |
| `--state-file PATH` | Path to sync state file (default: `./sync_state.json`). |
| `--verbose`, `-v` | Enable debug-level logging. |

### Data Mapping

Each Devin session is mapped to a HoneyHive session event:

| Devin Field | HoneyHive Field |
|-------------|-----------------|
| `session_id` | `user_properties.devin_session_id` |
| `title` | `session_name` |
| `status` | `metadata.devin_status` |
| `tags` | `metadata.devin_tags` |
| `url` | `metadata.devin_url` |
| `pull_requests` | `metadata.devin_pull_requests` |
| `acus_consumed` | `metrics.acus_consumed` |
| `created_at` | `start_time` |
| `updated_at` | `end_time` |

Session IDs are deterministically mapped using UUID5 so re-syncs are idempotent.

#### Events

Each session produces three types of child events:

**Messages** (user ↔ agent conversation):

| Message source | Event type | Event name |
|----------------|------------|------------|
| User message | `tool` | `user_message` |
| Agent (Devin) response | `model` | `agent_message` |

User messages store content in `inputs.message`; agent messages store content in `outputs.message`.

**Internal events** (Devin's behind-the-scenes processing, v3 API only):

| Category | Event type | Example event names |
|----------|------------|---------------------|
| `shell` | `tool` | `shell/shell_command`, `shell/shell_output` |
| `git` | `tool` | `git/git_commit`, `git/git_push` |
| `browser` | `tool` | `browser/browser_navigate`, `browser/browser_click` |
| `file` | `tool` | `file/file_edit`, `file/file_read` |
| `message` | `model` | `message/agent_message` |

Noisy internal events (checkpoints, activity updates, terminal updates) are filtered out automatically.

**Session end** (`event_type: "chain"`, `event_name: "session.end"`):

Emitted when a Devin session reaches `finished`, `stopped`, or `failed` status. Carries `outputs.artifact` with the full conversation as structured content, enabling server-side evaluators to analyze the session.

The session event also receives `outputs.chat_history` — a list of `{"role": "user"|"assistant", "content": "..."}` entries representing the full conversation.

### GitHub Actions Workflow

The included workflow (`.github/workflows/devin-export-sync.yml`) runs the exporter automatically:

- **On push/PR** to `main` touching `devin/**` files: runs a one-shot sync
- **Manual dispatch** via Actions tab: choose oneshot or daemon mode with configurable interval

Required GitHub repo secrets (same as the environment variables above):

- `DEVIN_API_KEY`
- `DEVIN_ORG_ID`
- `HH_API_KEY`
- `HH_API_URL`
- `HH_PROJECT`

### Sync Frequency

The script supports polling intervals as low as 30 seconds. The practical minimum depends on Devin API rate limits and the number of sessions in your org. For typical usage, 30-60 second intervals work well.

### State Management

Sync state is stored in a JSON file (`sync_state.json` by default) containing:
- `last_sync_epoch`: timestamp of the last successful sync (used for incremental filtering)
- `synced_sessions`: mapping of Devin session IDs to HoneyHive event IDs, last-updated timestamps, message counts, and internal event counts

If the state file is lost or corrupted, the next run performs a full re-sync. Session IDs and event IDs are deterministic (UUID5), so the sync produces the same IDs on re-run. However, the HoneyHive API does not enforce event ID uniqueness — losing the state file will create duplicate events with the same IDs rather than upserting.
