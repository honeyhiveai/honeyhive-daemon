# honeyhive-daemon

Project instructions for Claude Code sessions in this repo.

## Conventions

- Source credentials from repo-root `.env` only (gitignored).
- Run `honeyhive-daemon run` before `claude -p` so hooks export traces.
- Use subscription OAuth for Claude when logged in; API key only as fallback.
