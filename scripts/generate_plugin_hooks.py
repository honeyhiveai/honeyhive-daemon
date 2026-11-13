#!/usr/bin/env python3
"""Generate hooks/hooks.json for the honeyhive-observability plugin.

The plugin must register exactly the hook events the daemon knows how to
normalize. That list lives in ``honeyhive_daemon/mappings/claude_code.yaml``
under ``hook_registrations`` and is also what ``honeyhive-daemon run`` writes
into ``~/.claude/settings.json``. Generating the plugin manifest from the same
source keeps the two install paths from drifting apart.

Usage::

    python scripts/generate_plugin_hooks.py           # rewrite hooks/hooks.json
    python scripts/generate_plugin_hooks.py --check   # fail if out of date
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"
MAPPING_YAML = REPO_ROOT / "honeyhive_daemon" / "mappings" / "claude_code.yaml"

INGEST_COMMAND = '"${CLAUDE_PLUGIN_ROOT}"/hooks/honeyhive-ingest.sh'
PREFLIGHT_COMMAND = '"${CLAUDE_PLUGIN_ROOT}"/hooks/honeyhive-preflight.sh'

# Seconds. Ingest is a pipe into a short-lived process; preflight only stats a
# couple of files. Both are generously bounded so a wedged daemon can never
# stall a session.
INGEST_TIMEOUT = 10
PREFLIGHT_TIMEOUT = 5


def build_hooks_config() -> dict:
    """Build the plugin hooks config from the daemon's mapping file."""
    import yaml

    mapping = yaml.safe_load(MAPPING_YAML.read_text(encoding="utf-8"))

    hooks: dict[str, list] = {}
    for registration in mapping["hook_registrations"]:
        event_name = registration["hook_event_name"]
        entry: dict = {
            "hooks": [
                {
                    "type": "command",
                    "command": INGEST_COMMAND,
                    "timeout": INGEST_TIMEOUT,
                }
            ]
        }
        matcher = registration.get("matcher")
        if matcher is not None:
            entry["matcher"] = matcher
        hooks.setdefault(event_name, []).append(entry)

    # One loud, once-per-session check that the Python daemon is actually there
    # and running. Without it a missing daemon looks identical to a healthy one.
    hooks.setdefault("SessionStart", []).insert(
        0,
        {
            "hooks": [
                {
                    "type": "command",
                    "command": PREFLIGHT_COMMAND,
                    "timeout": PREFLIGHT_TIMEOUT,
                }
            ]
        },
    )

    return {"hooks": hooks}


def render() -> str:
    """Render hooks.json content."""
    return json.dumps(build_hooks_config(), indent=2) + "\n"


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if hooks/hooks.json is out of date",
    )
    args = parser.parse_args()

    rendered = render()
    if args.check:
        current = HOOKS_JSON.read_text(encoding="utf-8") if HOOKS_JSON.exists() else ""
        if current != rendered:
            print(
                "hooks/hooks.json is out of date with "
                "honeyhive_daemon/mappings/claude_code.yaml.\n"
                "Regenerate it: python scripts/generate_plugin_hooks.py",
                file=sys.stderr,
            )
            return 1
        return 0

    HOOKS_JSON.parent.mkdir(parents=True, exist_ok=True)
    HOOKS_JSON.write_text(rendered, encoding="utf-8")
    print(f"Wrote {HOOKS_JSON.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
