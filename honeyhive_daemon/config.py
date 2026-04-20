"""Configuration helpers for the HoneyHive daemon."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


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


def get_routes_path() -> Path:
    """Return the project routing config path."""
    return get_state_dir() / "routes.json"


def load_routes() -> list:
    """Load cwd-based project routing rules.

    Routes file schema::

        {
          "routes": [
            {"cwd_prefix": "/path/to/project", "project": "my_project", "api_key_env": "MY_KEY"},
            ...
          ]
        }

    Longer prefixes are matched first. If no route matches, the default
    daemon config is used.
    """
    path = get_routes_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        routes = data.get("routes", [])
        # Sort by prefix length descending (longest match wins)
        routes.sort(key=lambda r: len(r.get("cwd_prefix", "")), reverse=True)
        return routes
    except (json.JSONDecodeError, OSError):
        return []


def resolve_config_for_cwd(default_config: DaemonConfig, cwd: Optional[str]) -> DaemonConfig:
    """Resolve the DaemonConfig for a given cwd using routing rules.

    If a route matches, override project and api_key from the route.
    Falls back to default_config if no route matches.
    """
    if not cwd:
        return default_config
    routes = load_routes()
    for route in routes:
        prefix = route.get("cwd_prefix", "")
        if cwd.startswith(prefix):
            api_key = default_config.api_key
            api_key_env = route.get("api_key_env")
            if api_key_env:
                env_val = os.getenv(api_key_env)
                if env_val:
                    api_key = env_val
            return DaemonConfig(
                api_key=api_key,
                base_url=route.get("api_url", default_config.base_url),
                project=route.get("project", default_config.project),
                repo_path=default_config.repo_path,
                ci=default_config.ci,
            )
    return default_config


# ---------------------------------------------------------------------------
# Hierarchical config resolution
# ---------------------------------------------------------------------------


def find_project_root(cwd: str) -> Optional[Path]:
    """Walk up from *cwd* to find a directory containing a ``.honeyhive/`` subdirectory.

    Returns the project root ``Path`` or ``None`` if the filesystem root is
    reached without finding one.
    """
    current = Path(cwd).resolve()
    while True:
        if (current / ".honeyhive").is_dir():
            return current
        parent = current.parent
        if parent == current:
            # Reached filesystem root
            return None
        current = parent


def _get_user_config_path() -> Path:
    """Return the user-level HoneyHive config path."""
    return Path.home() / ".honeyhive" / "config.json"


def load_user_config() -> dict:
    """Load user-level defaults from ``~/.honeyhive/config.json``.

    Expected schema::

        {"api_key_env": "HH_API_KEY", "base_url": "https://api.honeyhive.ai"}

    Returns an empty dict on missing file or parse error.
    """
    path = _get_user_config_path()
    try:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, PermissionError) as exc:
        from .state import log_message

        log_message(f"load_user_config: skipping {path} ({exc})")
        return {}


def save_user_config(data: dict) -> Path:
    """Write user-level config to ``~/.honeyhive/config.json``.

    Merges *data* into any existing user config so that callers can
    update individual fields without clobbering the rest.

    Returns the path that was written.
    """
    path = _get_user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_user_config()
    existing.update(data)
    path.write_text(
        json.dumps(existing, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def load_project_config(project_root: Path) -> dict:
    """Load project-level config from ``{project_root}/.honeyhive/config.json``.

    Expected schema::

        {"project": "my-project"}

    Returns an empty dict on missing file or parse error.
    """
    path = project_root / ".honeyhive" / "config.json"
    try:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, PermissionError) as exc:
        from .state import log_message

        log_message(f"load_project_config: skipping {path} ({exc})")
        return {}


def load_project_local_config(project_root: Path) -> dict:
    """Load project-local overrides from ``{project_root}/.honeyhive/config.local.json``.

    For the ``api_key_env`` field, the value is treated as an environment
    variable *name* and resolved via :func:`os.getenv`.  If the variable is
    not set a warning is logged and the field is omitted from the result.

    Returns an empty dict on missing file or parse error.
    """
    path = project_root / ".honeyhive" / "config.local.json"
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, PermissionError) as exc:
        from .state import log_message

        log_message(f"load_project_local_config: skipping {path} ({exc})")
        return {}

    # Resolve api_key_env -> actual value
    api_key_env = data.get("api_key_env")
    if api_key_env:
        env_val = os.getenv(api_key_env)
        if env_val:
            data["_resolved_api_key"] = env_val
        else:
            from .state import log_message

            log_message(f"load_project_local_config: env var {api_key_env!r} is not set; skipping api_key resolution")
    return data


def _load_session_sidecar(session_name: str) -> dict:
    """Load session-level config from the daemon sessions directory.

    Reads ``~/.honeyhive/daemon/sessions/{session_name}.json`` and returns
    the ``config`` sub-dict, or an empty dict on failure.
    """
    path = get_daemon_home() / "sessions" / f"{session_name}.json"
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return dict(data.get("config", {}))
    except (json.JSONDecodeError, OSError, PermissionError):
        return {}


def _merge_to_daemon_config(merged: dict, cli_defaults: Optional[DaemonConfig]) -> DaemonConfig:
    """Build a :class:`DaemonConfig` from a merged config dict.

    Resolves ``api_key_env`` from the environment if no ``_resolved_api_key``
    has already been set by :func:`load_project_local_config`.  Falls back to
    *cli_defaults* for any missing required fields.
    """
    # Determine API key: _resolved_api_key > api_key_env resolution > cli_defaults
    api_key = merged.get("_resolved_api_key")
    if not api_key:
        api_key_env = merged.get("api_key_env")
        if api_key_env:
            api_key = os.getenv(api_key_env, "")
    if not api_key and cli_defaults:
        api_key = cli_defaults.api_key
    if not api_key:
        api_key = ""

    base_url = merged.get("base_url") or (cli_defaults.base_url if cli_defaults else DEFAULT_BASE_URL)
    project = merged.get("project") or (cli_defaults.project if cli_defaults else "")
    repo_path = merged.get("repo_path") or (cli_defaults.repo_path if cli_defaults else None)
    ci = merged.get("ci", cli_defaults.ci if cli_defaults else False)

    return DaemonConfig(
        api_key=api_key,
        base_url=base_url,
        project=project,
        repo_path=repo_path,
        ci=ci,
    )


def _config_source_files(
    cwd: Optional[str],
    session_name: Optional[str],
) -> List[Path]:
    """Return all config source file paths that *could* contribute layers.

    Used for mtime-based cache invalidation.
    """
    paths: List[Path] = [_get_user_config_path()]
    if cwd:
        project_root = find_project_root(cwd)
        if project_root:
            paths.append(project_root / ".honeyhive" / "config.json")
            paths.append(project_root / ".honeyhive" / "config.local.json")
        # routes.json is the fallback when no .honeyhive/ is found
        paths.append(get_routes_path())
    if session_name:
        paths.append(get_daemon_home() / "sessions" / f"{session_name}.json")
    return paths


# ---- mtime-based config cache --------------------------------------------

_config_cache_lock = threading.Lock()
_config_cache: Dict[
    Tuple[Optional[str], Optional[str]],  # (cwd, session_name)
    Tuple[DaemonConfig, Dict[str, float]],  # (resolved, {path: mtime})
] = {}


def _snapshot_mtimes(paths: List[Path]) -> Dict[str, float]:
    """Return a dict mapping each path (as str) to its mtime, or 0.0 if missing."""
    result: Dict[str, float] = {}
    for p in paths:
        try:
            result[str(p)] = p.stat().st_mtime
        except (OSError, PermissionError):
            result[str(p)] = 0.0
    return result


def _cache_is_valid(
    key: Tuple[Optional[str], Optional[str]],
    current_mtimes: Dict[str, float],
) -> bool:
    """Check whether the cached entry for *key* is still valid."""
    entry = _config_cache.get(key)
    if entry is None:
        return False
    _, cached_mtimes = entry
    return cached_mtimes == current_mtimes


def resolve_config(
    cwd: Optional[str] = None,
    session_name: Optional[str] = None,
    cli_defaults: Optional[DaemonConfig] = None,
) -> DaemonConfig:
    """Resolve a :class:`DaemonConfig` by merging four layers.

    Layer priority (lowest to highest):

    1. **User config** — ``~/.honeyhive/config.json``
    2. **Project config** — ``{project_root}/.honeyhive/config.json``
    3. **Project local config** — ``{project_root}/.honeyhive/config.local.json``
    4. **Session sidecar** — ``~/.honeyhive/daemon/sessions/{session_name}.json``

    If no ``.honeyhive/`` directory is found walking up from *cwd*, the
    function falls back to ``routes.json`` (via :func:`resolve_config_for_cwd`)
    and then to *cli_defaults*.

    Results are cached and invalidated when any source file's mtime changes.
    """
    cache_key: Tuple[Optional[str], Optional[str]] = (cwd, session_name)
    source_paths = _config_source_files(cwd, session_name)
    current_mtimes = _snapshot_mtimes(source_paths)

    with _config_cache_lock:
        if _cache_is_valid(cache_key, current_mtimes):
            return _config_cache[cache_key][0]

    # -- Build merged dict from layers (lowest to highest priority) ----------
    merged: dict = {}

    # Layer 1: user config
    merged.update(load_user_config())

    # Layer 2 & 3: project config (only if .honeyhive/ found)
    project_root: Optional[Path] = None
    if cwd:
        project_root = find_project_root(cwd)

    if project_root:
        merged.update(load_project_config(project_root))
        merged.update(load_project_local_config(project_root))
    elif cwd:
        # No .honeyhive/ found — fall back to routes.json
        fallback_base = cli_defaults or DaemonConfig(api_key="", base_url=DEFAULT_BASE_URL, project="")
        routes_config = resolve_config_for_cwd(fallback_base, cwd)
        if routes_config is not fallback_base:
            # routes.json matched — use it as the base and overlay session
            if session_name:
                session_layer = _load_session_sidecar(session_name)
                if session_layer:
                    result = DaemonConfig(
                        api_key=routes_config.api_key,
                        base_url=session_layer.get("base_url", routes_config.base_url),
                        project=session_layer.get("project", routes_config.project),
                        repo_path=routes_config.repo_path,
                        ci=routes_config.ci,
                    )
                    with _config_cache_lock:
                        _config_cache[cache_key] = (result, current_mtimes)
                    return result
            with _config_cache_lock:
                _config_cache[cache_key] = (routes_config, current_mtimes)
            return routes_config

    # Layer 4: session sidecar
    if session_name:
        session_layer = _load_session_sidecar(session_name)
        if session_layer:
            merged.update(session_layer)

    result = _merge_to_daemon_config(merged, cli_defaults)

    with _config_cache_lock:
        _config_cache[cache_key] = (result, current_mtimes)

    return result


def invalidate_config_cache() -> None:
    """Clear the entire config resolution cache.

    Useful in tests or when a config file is known to have changed.
    """
    with _config_cache_lock:
        _config_cache.clear()
