"""Git helpers for lightweight daemon provenance."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, Optional


HOOK_MARKER_START = "# >>> honeyhive-daemon post-commit >>>"
HOOK_MARKER_END = "# <<< honeyhive-daemon post-commit <<<"


def find_git_root(start_path: Path) -> Optional[Path]:
    """Return the repo root for a path, if any."""
    try:
        result = subprocess.run(
            ["git", "-C", str(start_path), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return Path(result.stdout.strip())


def get_git_revision(repo_root: Path) -> Optional[str]:
    """Return the current commit SHA for a repo if available."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return result.stdout.strip() or None


def get_commit_link_payload(repo_root: Path) -> Optional[Dict[str, Optional[str]]]:
    """Return minimal commit metadata for the current HEAD."""
    commit_sha = get_git_revision(repo_root)
    if not commit_sha:
        return None

    parent_sha: Optional[str]
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD^"],
            check=True,
            capture_output=True,
            text=True,
        )
        parent_sha = result.stdout.strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        parent_sha = None

    return {
        "repo_path": str(repo_root),
        "git.commit_sha": commit_sha,
        "git.parent_sha": parent_sha,
    }


def _get_post_commit_hook_path(repo_root: Path) -> Optional[Path]:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "rev-parse",
                "--git-path",
                "hooks/post-commit",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return repo_root / result.stdout.strip()


def install_post_commit_hook(repo_root: Path, command: str) -> bool:
    """Install a repo-local post-commit hook if not already present."""
    hook_path = _get_post_commit_hook_path(repo_root)
    if hook_path is None:
        return False

    hook_path.parent.mkdir(parents=True, exist_ok=True)
    existing = hook_path.read_text(encoding="utf-8") if hook_path.exists() else ""
    block = "\n".join(
        [
            HOOK_MARKER_START,
            f"{command} >/dev/null 2>&1 || true",
            HOOK_MARKER_END,
            "",
        ]
    )

    if HOOK_MARKER_START in existing:
        return False

    if existing.startswith("#!"):
        new_content = existing.rstrip() + "\n\n" + block
    elif existing.strip():
        new_content = "#!/bin/sh\n\n" + existing.rstrip() + "\n\n" + block
    else:
        new_content = "#!/bin/sh\n\n" + block

    hook_path.write_text(new_content, encoding="utf-8")
    hook_path.chmod(0o755)
    return True
