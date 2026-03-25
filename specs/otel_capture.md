# OTel Capture Spec (Deferred)

## Overview
Capture OTel logs/events from Claude Code by running an embedded OTLP HTTP receiver in the daemon.

Reference: https://code.claude.com/docs/en/monitoring-usage

## Standard attributes (all events)
- `session.id` — unique session identifier
- `prompt.id` — UUID v4 linking all events from a single user prompt
- `app.version` — Claude Code version
- `organization.id` — org UUID (when authenticated)
- `user.account_uuid` — account UUID (when authenticated)
- `user.id` — anonymous device/installation identifier
- `user.email` — user email (when authenticated via OAuth)
- `terminal.type` — terminal type (iTerm.app, vscode, cursor, tmux)
- `event.timestamp` — ISO 8601 timestamp
- `event.sequence` — monotonic counter for ordering events within a session

## OTel events from Claude Code

### `claude_code.api_request`
Logged for each API request to Claude.
- `model` — model identifier (e.g. "claude-sonnet-4-6")
- `cost_usd` — estimated cost in USD
- `duration_ms` — request duration in milliseconds
- `input_tokens` — number of input tokens
- `output_tokens` — number of output tokens
- `cache_read_tokens` — tokens read from cache
- `cache_creation_tokens` — tokens used for cache creation
- `speed` — "fast" or "normal" (fast mode)

### `claude_code.tool_result`
Logged when a tool completes execution.
- `tool_name` — name of the tool
- `success` — "true" or "false"
- `duration_ms` — execution time in milliseconds
- `error` — error message (if failed)
- `decision_type` — "accept" or "reject"
- `decision_source` — "config", "hook", "user_permanent", "user_temporary", "user_abort", "user_reject"
- `tool_result_size_bytes` — size of tool result in bytes
- `mcp_server_scope` — MCP server scope (for MCP tools)
- `tool_parameters` — JSON string with tool-specific parameters:
  - Bash: `bash_command`, `full_command`, `timeout`, `description`, `dangerouslyDisableSandbox`, `git_commit_id`
  - MCP tools (OTEL_LOG_TOOL_DETAILS=1): `mcp_server_name`, `mcp_tool_name`
  - Skill tool (OTEL_LOG_TOOL_DETAILS=1): `skill_name`

### `claude_code.user_prompt`
Logged when a user submits a prompt.
- `prompt_length` — length of the prompt
- `prompt` — prompt content (redacted by default, enable with OTEL_LOG_USER_PROMPTS=1)

### `claude_code.api_error`
Logged when an API request fails.
- `model` — model identifier
- `error` — error message
- `status_code` — HTTP status code as string, or "undefined"
- `duration_ms` — request duration in milliseconds
- `attempt` — attempt number (for retried requests)
- `speed` — "fast" or "normal"

### `claude_code.tool_decision`
Logged when a tool permission decision is made.
- `tool_name` — name of the tool
- `decision` — "accept" or "reject"
- `source` — "config", "hook", "user_permanent", "user_temporary", "user_abort", "user_reject"

## What OTel provides that hooks/transcript don't
- `cost_usd` — no cost data in hooks or transcript
- `speed` — fast vs normal mode
- `prompt.id` — cross-event correlation for a single user prompt
- `event.sequence` — monotonic ordering counter
- `tool_result_size_bytes` — size of tool response
- `api_error` events — error details with retry attempt count
- `tool_decision` events — standalone accept/reject with source

## Environment variables to configure
```
CLAUDE_CODE_ENABLE_TELEMETRY=1
OTEL_LOGS_EXPORTER=otlp
OTEL_EXPORTER_OTLP_LOGS_PROTOCOL=http/json
OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://localhost:4318/v1/logs
```

Can be set in Claude managed settings via `env` key or as shell env vars.

## Approach
- Add an OTLP HTTP receiver in the daemon (lightweight HTTP server on a local port, e.g., `localhost:4318`)
- Accept `POST /v1/logs` with JSON or protobuf payloads
- Parse incoming OTel log records, extract event name + attributes
- For `api_request` events: create new HoneyHive events of type `model` with token/cost data, correlated to session via `session.id`
- For `tool_result` events: enrich existing hook-based tool events with `duration_ms` (use `session.id` + `tool_name` + `event.sequence` for matching)
- For `api_error` events: create HoneyHive events with error metadata
- On `honeyhive-daemon run`, auto-configure Claude Code env vars by writing to managed settings

## Files to modify/create
- **New**: `honeyhive_daemon/otel_receiver.py` — OTLP HTTP server + log record parser
- **Modify**: `honeyhive_daemon/main.py` — start OTLP receiver thread in `run` command, configure env vars in Claude settings
- **Modify**: `honeyhive_daemon/claude_hooks.py` — add `install_claude_otel_env()` to write OTel env config
- **Modify**: `honeyhive_daemon/mappings/claude_code.yaml` — add event mappings for OTel-sourced events
