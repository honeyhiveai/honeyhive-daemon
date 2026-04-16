"""Per-repo error category configuration for honeyhive-daemon analyze.

Default rules are defined here. Each repo can override or extend them by
creating `.honeyhive/error-categories.json`. The CI workflow's Claude step
auto-discovers new categories from recurring ``unknown_error`` patterns and
appends them to the ``discovered`` section of that file, creating a PR for
human review. Once merged the rule becomes active for future runs.

Schema for .honeyhive/error-categories.json
--------------------------------------------
{
  "version": 1,
  "extends": "defaults",       // "defaults" | "none" — whether to merge with built-ins
  "categories": [              // overrides / additions on top of defaults
    {
      "id": "promote_exit_2",
      "pattern": "already promoted",
      "fix": "Exit code 2 from promote-plan.sh is a no-op — handle silently"
    }
  ],
  "skip_patterns": [           // exact lowercased error strings to ignore
    "exit code 1"
  ],
  "discovered": [              // auto-discovered by CI — review and promote to categories
    {
      "id": "promote_exit_2",
      "pattern": "already promoted",
      "fix": "...",
      "discovered_at": "2026-04-16T18:00:00Z",
      "occurrences": 296,
      "sample": "Exit code 2\\nPlan WAG-252 already promoted — skipping"
    }
  ]
}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Built-in defaults
# ---------------------------------------------------------------------------

DEFAULT_CATEGORIES: list[dict] = [
    {"id": "permission_denied",  "pattern": "permission denied",       "fix": "Add the command to allowedTools in .claude/settings.json"},
    {"id": "permission_denied",  "pattern": "operation not permitted", "fix": "Add the command to allowedTools in .claude/settings.json"},
    {"id": "missing_command",    "pattern": "command not found",       "fix": "Install the missing tool or document it in CLAUDE.md"},
    {"id": "missing_command",    "pattern": "exit code 127",           "fix": "Install the missing tool or add it to PATH in CLAUDE.md"},
    {"id": "permission_denied",  "pattern": "exit code 126",           "fix": "Check file permissions; add execution steps to CLAUDE.md"},
    {"id": "git_error",          "pattern": "exit code 128",           "fix": "Add git setup steps to CLAUDE.md (fetch, base branch, worktree)"},
    {"id": "git_error",          "pattern": "fatal: ",                 "fix": "Add git setup steps to CLAUDE.md (fetch, base branch, worktree)"},
    {"id": "git_error",          "pattern": "no merge base",           "fix": "Ensure worktrees fetch origin before diffing; add to CLAUDE.md"},
    {"id": "missing_file",       "pattern": "no such file",            "fix": "Add path guidance or setup steps to CLAUDE.md"},
    {"id": "missing_module",     "pattern": "cannot find module",      "fix": "Add the install step to your setup docs"},
    {"id": "missing_module",     "pattern": "module not found",        "fix": "Add the install step to your setup docs"},
    {"id": "auth_failure",       "pattern": "authentication",          "fix": "Verify API credentials and document rotation in CLAUDE.md"},
    {"id": "auth_failure",       "pattern": "unauthorized",            "fix": "Verify API credentials and document rotation in CLAUDE.md"},
    {"id": "auth_failure",       "pattern": "http 401",                "fix": "Verify API credentials and document rotation in CLAUDE.md"},
    {"id": "auth_failure",       "pattern": "401 unauthorized",        "fix": "Verify API credentials and document rotation in CLAUDE.md"},
    {"id": "rate_limit",         "pattern": "rate limit",              "fix": "Add retry logic or caching for this operation"},
    {"id": "rate_limit",         "pattern": "too many requests",       "fix": "Add retry logic or caching for this operation"},
    {"id": "timeout",            "pattern": "timed out",               "fix": "Break into smaller operations or add a timeout handler"},
    {"id": "timeout",            "pattern": "search timed out",        "fix": "Use narrower glob patterns or break search into smaller scopes"},
    {"id": "connection_refused", "pattern": "connection refused",      "fix": "Check service availability and add health-check guidance to CLAUDE.md"},
    {"id": "syntax_error",       "pattern": "syntax error",            "fix": "Review code-generation patterns; add examples to CLAUDE.md"},
    {"id": "python_exception",   "pattern": "traceback",               "fix": "Add error handling guidance or a helper script"},
]

# Exact lowercased strings (after stripping) that are too generic to be actionable.
DEFAULT_SKIP_PATTERNS: list[str] = [
    "exit code 1",
    "exit code 2",
    "exit code 1\n",
]

# Prefixes to strip before measuring if remaining content is worth keeping.
_EXIT_PREFIXES = ("exit code 1\n", "exit code 2\n", "exit code 1 ", "exit code 3\n")


# ---------------------------------------------------------------------------
# Config file location
# ---------------------------------------------------------------------------

_CONFIG_FILENAME = "error-categories.json"


def _config_path(root: str) -> Path:
    return Path(root) / ".honeyhive" / _CONFIG_FILENAME


# ---------------------------------------------------------------------------
# Load / merge
# ---------------------------------------------------------------------------

def load_rules(cwd: Optional[str] = None) -> tuple[list[dict], list[str]]:
    """Return (categories, skip_patterns) for the given working directory.

    Looks for ``.honeyhive/error-categories.json`` starting at *cwd* and
    walking up. If not found, returns the built-in defaults unchanged.

    When ``extends: "defaults"`` (the default), the repo's ``categories``
    list is *appended* to the built-ins so repo-specific rules have lower
    priority than defaults (which is usually what you want — defaults handle
    common patterns first, repo-specific rules catch the remainder). Set
    ``extends: "none"`` to replace defaults entirely.
    """
    from .config import find_project_root  # avoid circular at module level

    root: Optional[str] = None
    if cwd:
        root = find_project_root(cwd)

    cfg: dict = {}
    if root:
        p = _config_path(root)
        if p.exists():
            try:
                cfg = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

    extends = cfg.get("extends", "defaults")
    repo_cats: list[dict] = cfg.get("categories", [])
    # Also include discovered categories that have been validated
    # (CI auto-discovery writes to "discovered"; humans promote them to "categories" on PR merge)
    discovered: list[dict] = cfg.get("discovered", [])

    if extends == "none":
        categories = repo_cats
    else:
        # Defaults first, then repo overrides (more specific rules last loses in first-match,
        # so put repo rules BEFORE defaults to let them override).
        categories = repo_cats + discovered + DEFAULT_CATEGORIES

    repo_skip: list[str] = cfg.get("skip_patterns", [])
    skip_patterns = list({*repo_skip, *DEFAULT_SKIP_PATTERNS})

    return categories, skip_patterns


def init_config(root: str) -> Path:
    """Scaffold .honeyhive/error-categories.json with an empty repo section.

    Safe to call if the file already exists — returns path without modifying.
    """
    p = _config_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        scaffold = {
            "version": 1,
            "extends": "defaults",
            "categories": [],
            "skip_patterns": [],
            "discovered": [],
        }
        p.write_text(json.dumps(scaffold, indent=2) + "\n", encoding="utf-8")
    return p


def append_discovered(root: str, entries: list[dict]) -> None:
    """Append new auto-discovered categories to the 'discovered' list.

    Called by the CI Claude step when it finds recurring ``unknown_error``
    patterns that don't match any existing rule. Existing entries (by id or
    pattern) are not duplicated.
    """
    p = _config_path(root)
    try:
        cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (json.JSONDecodeError, OSError):
        cfg = {}

    existing_ids = {e.get("id") for e in cfg.get("discovered", [])}
    existing_patterns = {e.get("pattern", "").lower() for e in cfg.get("discovered", [])}

    new_entries = [
        e for e in entries
        if e.get("id") not in existing_ids
        and e.get("pattern", "").lower() not in existing_patterns
    ]
    if not new_entries:
        return

    cfg.setdefault("discovered", []).extend(new_entries)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Categorization
# ---------------------------------------------------------------------------

def categorize(error: str, categories: list[dict], skip_patterns: list[str]) -> tuple[str, str]:
    """Return (category_id, fix) for an error string, or ('', '') to skip.

    Checks skip_patterns first, then iterates categories in order (first match wins).
    Falls back to ``unknown_error`` for non-trivial errors that don't match any rule.
    """
    lower = error.lower().strip()

    # Skip exact matches for generic-only content.
    if lower in skip_patterns:
        return "", ""

    # Try all rules (first match wins).
    for rule in categories:
        pattern = rule.get("pattern", "")
        if pattern and pattern.lower() in lower:
            return rule["id"], rule.get("fix", "Review the recurring error and add guidance to CLAUDE.md")

    # Strip exit-code prefix and check if there's meaningful content left.
    content = lower
    for prefix in _EXIT_PREFIXES:
        if content.startswith(prefix):
            content = content[len(prefix):]
            break
    if len(content.strip()) < 10:
        return "", ""

    return "unknown_error", "Review the recurring error and add guidance to CLAUDE.md"
