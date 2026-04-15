# Changelog

## 0.5.1 (2026-04-14)

### Bug Fixes

- **fix: synthesize session.start when daemon starts after session** — When the daemon starts (or restarts) while Claude Code sessions are already running, it now creates a synthetic `session.start` event on first contact with that session. Previously, the daemon would receive tool events for mid-flight sessions but fail to push artifacts with a 400 error because no session root event existed in HoneyHive. This caused silent data loss for any session that started before the daemon. The synthetic event carries all available metadata (`session_name`, `cwd`, `repo.path`, etc.) and is tagged `metadata.synthetic = true` for auditability.

## 0.5.0 (2026-04-14)

### Features

- **feat: extract session_name from Claude Code transcripts** — Sessions started with `claude --name X` now have `session_name` in trace metadata and as a top-level field on session events. Reads `customTitle` from the first line of the transcript JSONL, cached per path via LRU. Forward-compatible: if the hook payload ever includes `session_name` directly, it takes precedence.

## 0.4.1

- Initial public release
