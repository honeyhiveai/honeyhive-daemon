# honeyhive-daemon

Project instructions for Claude Code sessions in this repo.

## Smoke test markers

When validating HoneyHive telemetry, include these exact lines in your final reply (each on its own line when applicable):

- `hh-daemon-claude-md-loaded` — from this CLAUDE.md
- `hh-daemon-skill-loaded` — when the `hh-daemon-smoke` skill (`.claude/skills/hh-daemon-smoke/`) is listed for the session

## Conventions

- Source credentials from repo-root `.env` only (gitignored).
- Run `honeyhive-daemon run` before `claude -p` so hooks export traces.
- Use subscription OAuth for Claude when logged in; API key only as fallback.
