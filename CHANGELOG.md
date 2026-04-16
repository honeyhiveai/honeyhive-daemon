# Changelog

## 0.7.0 (2026-04-16)

### Features

- **feat: `honeyhive-daemon analyze` command** — Queries HoneyHive traces for the configured project and detects recurring error patterns in Claude Code sessions. Groups failures by category (permission_denied, missing_command, missing_file, auth_failure, git_error, timeout, python_exception, etc.), counts occurrences, and outputs a JSON report with suggested fixes. Patterns with ≥3 occurrences are flagged as actionable.

- **feat: `honeyhive-daemon add-to-ci` command** — Generates a ready-to-use GitHub Actions workflow (`.github/workflows/hh-proactive-improvements.yml`) that runs `analyze` on a schedule and then invokes `claude --dangerously-skip-permissions` to automatically open PRs for recurring issues. Supports `--cadence hourly|daily|weekly` and `--project` overrides.

- **feat: dual-filter error detection** — `analyze` queries both `metadata.tool.status = failure` (daemon-stamped field, catches all tool failures even when stderr is inside `outputs.tool_response`) and `error is not null` (catches legacy/third-party events), then deduplicates by event_id for complete coverage.

- **feat: smart error extraction** — Extracts meaningful error text from `error`, `outputs.tool_response.stderr`, `outputs.tool_response.error`, and content-block arrays, so errors that don't surface in the top-level `error` field are still categorized correctly.

## 0.6.0 (2026-04-15)

### Features

- **feat: hierarchical config resolution with 4-layer merge** — The daemon now supports per-project configuration via a `.honeyhive/` directory convention. Config layers merge in priority order: CLI defaults < user config (`~/.honeyhive/config.json`) < project config (`.honeyhive/config.json`) < project-local config (`.honeyhive/config.local.json`) < session sidecar. Multiple repos on the same machine each trace to different HoneyHive projects through a single daemon process.

- **feat: `honeyhive-daemon init` command** — New command that scaffolds `.honeyhive/config.json` (project name) and `.honeyhive/config.local.json` (API key env var) in the current repo. Auto-appends `config.local.json` to `.gitignore` so API keys are never committed.

- **feat: routes.json backward compatibility** — The existing `routes.json` cwd-prefix routing continues to work alongside the new `.honeyhive/` convention.

### Deprecations

- `--key` and `--project` CLI flags on the `run` command now emit deprecation warnings. They remain fully functional as the lowest-priority config layer. Use `honeyhive-daemon init` for per-project setup.

### Internals

- Config is resolved per-event at ingest time and stamped on spooled events, so spool replay never re-resolves config.
- `state.py`: `record_session_activity()` now persists `cwd` and `session_name` for background loop config resolution.
- 42 new tests covering config resolution hierarchy, caching, init command, and backward compat.

## 0.5.1 (2026-04-14)

### Bug Fixes

- **fix: synthesize session.start when daemon starts after session** — When the daemon starts (or restarts) while Claude Code sessions are already running, it now creates a synthetic `session.start` event on first contact with that session. Previously, the daemon would receive tool events for mid-flight sessions but fail to push artifacts with a 400 error because no session root event existed in HoneyHive. This caused silent data loss for any session that started before the daemon. The synthetic event carries all available metadata (`session_name`, `cwd`, `repo.path`, etc.) and is tagged `metadata.synthetic = true` for auditability.

## 0.5.0 (2026-04-14)

### Features

- **feat: extract session_name from Claude Code transcripts** — Sessions started with `claude --name X` now have `session_name` in trace metadata and as a top-level field on session events. Reads `customTitle` from the first line of the transcript JSONL, cached per path via LRU. Forward-compatible: if the hook payload ever includes `session_name` directly, it takes precedence.

## 0.4.1

- Initial public release
