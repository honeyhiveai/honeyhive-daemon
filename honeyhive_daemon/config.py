"""Configuration helpers for the HoneyHive daemon."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional


DEFAULT_BASE_URL = "https://api.honeyhive.ai"


@dataclass
class DaemonConfig:
    """Persisted daemon configuration."""

    api_key: str
    base_url: str
    project: str
    repo_path: Optional[str] = None
    ci: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Convert config to a JSON-serializable dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DaemonConfig":
        """Build config from stored JSON data."""
        return cls(
            api_key=str(data["api_key"]),
            base_url=str(data.get("base_url") or DEFAULT_BASE_URL),
            project=str(data["project"]),
            repo_path=data.get("repo_path"),
            ci=bool(data.get("ci", False)),
        )


def get_daemon_home() -> Path:
    """Return the daemon state directory."""
    override = os.getenv("HH_DAEMON_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".honeyhive" / "daemon"


def get_state_dir() -> Path:
    """Return the config state directory."""
    return get_daemon_home() / "state"


def get_spool_dir() -> Path:
    """Return the local spool directory."""
    return get_daemon_home() / "spool"


def get_config_path() -> Path:
    """Return the config file path."""
    return get_state_dir() / "config.json"


def get_log_path() -> Path:
    """Return the daemon log path."""
    return get_daemon_home() / "daemon.log"


def get_spool_path() -> Path:
    """Return the event spool path."""
    return get_spool_dir() / "events.jsonl"


def get_sessions_path() -> Path:
    """Return the tracked session state path."""
    return get_state_dir() / "sessions.json"


def get_pid_path() -> Path:
    """Return the daemon PID file path."""
    return get_daemon_home() / "daemon.pid"


def get_pending_tools_path() -> Path:
    """Return the pending tool events state path."""
    return get_state_dir() / "pending_tools.json"


def get_chat_histories_path() -> Path:
    """Return the per-session chat history state path."""
    return get_state_dir() / "chat_histories.json"


def get_claude_settings_path() -> Path:
    """Return the Claude user settings path."""
    override = os.getenv("CLAUDE_SETTINGS_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "settings.json"


def ensure_state_layout() -> None:
    """Create required local directories."""
    get_state_dir().mkdir(parents=True, exist_ok=True)
    get_spool_dir().mkdir(parents=True, exist_ok=True)


def save_config(config: DaemonConfig) -> None:
    """Persist daemon config to disk."""
    ensure_state_layout()
    get_config_path().write_text(
        json.dumps(config.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_config() -> Optional[DaemonConfig]:
    """Load daemon config if present."""
    path = get_config_path()
    if not path.exists():
        return None
    return DaemonConfig.from_dict(json.loads(path.read_text(encoding="utf-8")))
