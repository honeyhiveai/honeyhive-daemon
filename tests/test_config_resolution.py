"""Unit tests for hierarchical config resolution."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import pytest

from honeyhive_daemon.config import (
    DEFAULT_BASE_URL,
    DaemonConfig,
    _merge_to_daemon_config,
    find_project_root,
    invalidate_config_cache,
    load_project_config,
    load_project_local_config,
    load_user_config,
    resolve_config,
    resolve_config_for_cwd,
    save_user_config,
)


# ---------------------------------------------------------------------------
# find_project_root
# ---------------------------------------------------------------------------


class TestFindProjectRoot:
    def test_finds_honeyhive_dir_in_cwd(self, tmp_path: Path) -> None:
        (tmp_path / ".honeyhive").mkdir()
        assert find_project_root(str(tmp_path)) == tmp_path

    def test_walks_up_to_parent(self, tmp_path: Path) -> None:
        (tmp_path / ".honeyhive").mkdir()
        child = tmp_path / "src" / "app"
        child.mkdir(parents=True)
        assert find_project_root(str(child)) == tmp_path

    def test_returns_none_when_missing(self, tmp_path: Path) -> None:
        # tmp_path has no .honeyhive/ anywhere up the chain
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        assert find_project_root(str(deep)) is None

    def test_stops_at_filesystem_root(self) -> None:
        # Starting from root should not infinite-loop
        result = find_project_root("/")
        # /  may or may not have .honeyhive, but it must return quickly
        assert result is None or isinstance(result, Path)


# ---------------------------------------------------------------------------
# load_user_config
# ---------------------------------------------------------------------------


class TestLoadUserConfig:
    def test_returns_empty_when_missing(self, monkeypatch, tmp_path: Path) -> None:
        # Point HOME to a temp dir with no config
        monkeypatch.setenv("HOME", str(tmp_path))
        # Patch the helper to use our tmp home
        monkeypatch.setattr(
            "honeyhive_daemon.config._get_user_config_path",
            lambda: tmp_path / ".honeyhive" / "config.json",
        )
        assert load_user_config() == {}

    def test_reads_valid_config(self, monkeypatch, tmp_path: Path) -> None:
        config_path = tmp_path / ".honeyhive" / "config.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            json.dumps({"api_key_env": "MY_KEY", "base_url": "https://custom.api"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "honeyhive_daemon.config._get_user_config_path",
            lambda: config_path,
        )
        result = load_user_config()
        assert result["api_key_env"] == "MY_KEY"
        assert result["base_url"] == "https://custom.api"

    def test_returns_empty_on_malformed_json(self, monkeypatch, tmp_path: Path) -> None:
        config_path = tmp_path / ".honeyhive" / "config.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("{not valid json", encoding="utf-8")
        monkeypatch.setattr(
            "honeyhive_daemon.config._get_user_config_path",
            lambda: config_path,
        )
        assert load_user_config() == {}


# ---------------------------------------------------------------------------
# load_project_config
# ---------------------------------------------------------------------------


class TestLoadProjectConfig:
    def test_reads_project_config(self, tmp_path: Path) -> None:
        hh_dir = tmp_path / ".honeyhive"
        hh_dir.mkdir()
        (hh_dir / "config.json").write_text(
            json.dumps({"project": "my-project"}), encoding="utf-8"
        )
        result = load_project_config(tmp_path)
        assert result["project"] == "my-project"

    def test_returns_empty_when_missing(self, tmp_path: Path) -> None:
        assert load_project_config(tmp_path) == {}

    def test_returns_empty_on_malformed(self, tmp_path: Path) -> None:
        hh_dir = tmp_path / ".honeyhive"
        hh_dir.mkdir()
        (hh_dir / "config.json").write_text("broken!", encoding="utf-8")
        assert load_project_config(tmp_path) == {}


# ---------------------------------------------------------------------------
# load_project_local_config
# ---------------------------------------------------------------------------


class TestLoadProjectLocalConfig:
    def test_resolves_api_key_env(self, monkeypatch, tmp_path: Path) -> None:
        hh_dir = tmp_path / ".honeyhive"
        hh_dir.mkdir()
        (hh_dir / "config.local.json").write_text(
            json.dumps({"api_key_env": "TEST_HH_KEY"}), encoding="utf-8"
        )
        monkeypatch.setenv("TEST_HH_KEY", "secret-key-123")
        result = load_project_local_config(tmp_path)
        assert result["_resolved_api_key"] == "secret-key-123"
        assert result["api_key_env"] == "TEST_HH_KEY"

    def test_missing_env_var_omits_resolved_key(self, monkeypatch, tmp_path: Path) -> None:
        hh_dir = tmp_path / ".honeyhive"
        hh_dir.mkdir()
        (hh_dir / "config.local.json").write_text(
            json.dumps({"api_key_env": "NONEXISTENT_VAR"}), encoding="utf-8"
        )
        monkeypatch.delenv("NONEXISTENT_VAR", raising=False)
        result = load_project_local_config(tmp_path)
        assert "_resolved_api_key" not in result

    def test_returns_empty_when_missing(self, tmp_path: Path) -> None:
        assert load_project_local_config(tmp_path) == {}

    def test_returns_empty_on_malformed(self, tmp_path: Path) -> None:
        hh_dir = tmp_path / ".honeyhive"
        hh_dir.mkdir()
        (hh_dir / "config.local.json").write_text("{bad", encoding="utf-8")
        assert load_project_local_config(tmp_path) == {}


# ---------------------------------------------------------------------------
# resolve_config merge order
# ---------------------------------------------------------------------------


class TestResolveConfig:
    """Test the 4-layer merge: user < project < project.local < session."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self) -> None:
        """Clear config cache before every test."""
        invalidate_config_cache()
        yield  # type: ignore[misc]
        invalidate_config_cache()

    def _setup_layers(
        self,
        tmp_path: Path,
        monkeypatch,
        *,
        user: Optional[dict] = None,
        project: Optional[dict] = None,
        project_local: Optional[dict] = None,
        session: Optional[dict] = None,
        session_name: str = "test-session",
    ) -> Path:
        """Create config files for all four layers."""
        # User config
        user_config_path = tmp_path / "home" / ".honeyhive" / "config.json"
        user_config_path.parent.mkdir(parents=True)
        if user:
            user_config_path.write_text(json.dumps(user), encoding="utf-8")
        monkeypatch.setattr(
            "honeyhive_daemon.config._get_user_config_path",
            lambda: user_config_path,
        )

        # Project root with .honeyhive/
        project_root = tmp_path / "project"
        project_root.mkdir()
        hh_dir = project_root / ".honeyhive"
        hh_dir.mkdir()

        if project:
            (hh_dir / "config.json").write_text(
                json.dumps(project), encoding="utf-8"
            )
        if project_local:
            (hh_dir / "config.local.json").write_text(
                json.dumps(project_local), encoding="utf-8"
            )

        # Session sidecar
        daemon_home = tmp_path / "daemon"
        monkeypatch.setenv("HH_DAEMON_HOME", str(daemon_home))
        sessions_dir = daemon_home / "sessions"
        sessions_dir.mkdir(parents=True)
        if session:
            (sessions_dir / f"{session_name}.json").write_text(
                json.dumps({"config": session}), encoding="utf-8"
            )

        return project_root

    def test_user_config_is_lowest_priority(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        project_root = self._setup_layers(
            tmp_path,
            monkeypatch,
            user={"project": "user-proj", "base_url": "https://user.api"},
            project={"project": "proj-proj"},
        )
        result = resolve_config(cwd=str(project_root))
        # Project config overrides user config
        assert result.project == "proj-proj"
        # User base_url survives if project doesn't set it
        assert result.base_url == "https://user.api"

    def test_project_local_overrides_project(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("LOCAL_KEY", "local-secret")
        project_root = self._setup_layers(
            tmp_path,
            monkeypatch,
            user={"api_key_env": "WRONG_KEY"},
            project={"project": "proj-proj"},
            project_local={"api_key_env": "LOCAL_KEY", "project": "local-proj"},
        )
        result = resolve_config(cwd=str(project_root))
        assert result.project == "local-proj"
        assert result.api_key == "local-secret"

    def test_session_overrides_all(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("LOCAL_KEY", "local-secret")
        project_root = self._setup_layers(
            tmp_path,
            monkeypatch,
            user={"project": "user-proj"},
            project={"project": "proj-proj"},
            project_local={"project": "local-proj"},
            session={"project": "session-proj"},
        )
        result = resolve_config(
            cwd=str(project_root), session_name="test-session"
        )
        assert result.project == "session-proj"

    def test_cli_defaults_used_as_fallback(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When no config files provide api_key, cli_defaults are used."""
        project_root = self._setup_layers(
            tmp_path,
            monkeypatch,
            user={},
            project={"project": "my-proj"},
        )
        cli = DaemonConfig(
            api_key="cli-key",
            base_url="https://cli.api",
            project="cli-proj",
        )
        result = resolve_config(cwd=str(project_root), cli_defaults=cli)
        assert result.project == "my-proj"  # project config wins
        assert result.api_key == "cli-key"  # falls back to cli

    def test_missing_cwd_falls_back_to_cli_defaults(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # No user config
        user_config_path = tmp_path / "home" / ".honeyhive" / "config.json"
        user_config_path.parent.mkdir(parents=True)
        monkeypatch.setattr(
            "honeyhive_daemon.config._get_user_config_path",
            lambda: user_config_path,
        )
        monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon"))
        cli = DaemonConfig(
            api_key="fallback-key",
            base_url="https://fallback.api",
            project="fallback-proj",
        )
        result = resolve_config(cwd=None, cli_defaults=cli)
        assert result.api_key == "fallback-key"
        assert result.project == "fallback-proj"

    def test_empty_layers_produce_empty_config(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """All layers empty, no cli_defaults → empty DaemonConfig fields."""
        user_config_path = tmp_path / "home" / ".honeyhive" / "config.json"
        user_config_path.parent.mkdir(parents=True)
        monkeypatch.setattr(
            "honeyhive_daemon.config._get_user_config_path",
            lambda: user_config_path,
        )
        monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon"))
        result = resolve_config(cwd=None, cli_defaults=None)
        assert result.api_key == ""
        assert result.project == ""
        assert result.base_url == DEFAULT_BASE_URL


# ---------------------------------------------------------------------------
# routes.json backward compatibility
# ---------------------------------------------------------------------------


class TestRoutesJsonBackwardCompat:
    @pytest.fixture(autouse=True)
    def _clear_cache(self) -> None:
        invalidate_config_cache()
        yield  # type: ignore[misc]
        invalidate_config_cache()

    def test_routes_json_used_when_no_honeyhive_dir(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When cwd has no .honeyhive/, routes.json is used as fallback."""
        daemon_home = tmp_path / "daemon"
        monkeypatch.setenv("HH_DAEMON_HOME", str(daemon_home))
        state_dir = daemon_home / "state"
        state_dir.mkdir(parents=True)

        # User config
        user_config_path = tmp_path / "home" / ".honeyhive" / "config.json"
        user_config_path.parent.mkdir(parents=True)
        monkeypatch.setattr(
            "honeyhive_daemon.config._get_user_config_path",
            lambda: user_config_path,
        )

        # Write routes.json
        routes_path = state_dir / "routes.json"
        routes_path.write_text(
            json.dumps(
                {
                    "routes": [
                        {
                            "cwd_prefix": str(tmp_path / "myrepo"),
                            "project": "routed-project",
                            "api_key_env": "ROUTE_KEY",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("ROUTE_KEY", "route-secret")

        # A cwd inside the routed prefix but with no .honeyhive/
        work_dir = tmp_path / "myrepo" / "src"
        work_dir.mkdir(parents=True)

        cli = DaemonConfig(
            api_key="cli-key",
            base_url=DEFAULT_BASE_URL,
            project="cli-proj",
        )
        result = resolve_config(cwd=str(work_dir), cli_defaults=cli)
        assert result.project == "routed-project"
        assert result.api_key == "route-secret"

    def test_hierarchical_config_takes_precedence_over_routes(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When .honeyhive/ exists, routes.json is ignored."""
        daemon_home = tmp_path / "daemon"
        monkeypatch.setenv("HH_DAEMON_HOME", str(daemon_home))
        state_dir = daemon_home / "state"
        state_dir.mkdir(parents=True)

        user_config_path = tmp_path / "home" / ".honeyhive" / "config.json"
        user_config_path.parent.mkdir(parents=True)
        monkeypatch.setattr(
            "honeyhive_daemon.config._get_user_config_path",
            lambda: user_config_path,
        )

        # routes.json with a matching route
        routes_path = state_dir / "routes.json"
        routes_path.write_text(
            json.dumps(
                {
                    "routes": [
                        {
                            "cwd_prefix": str(tmp_path / "myrepo"),
                            "project": "routed-project",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        # Also create .honeyhive/ in the project
        project_root = tmp_path / "myrepo"
        project_root.mkdir(exist_ok=True)
        hh_dir = project_root / ".honeyhive"
        hh_dir.mkdir()
        (hh_dir / "config.json").write_text(
            json.dumps({"project": "hierarchical-project"}), encoding="utf-8"
        )

        cli = DaemonConfig(
            api_key="cli-key",
            base_url=DEFAULT_BASE_URL,
            project="cli-proj",
        )
        result = resolve_config(cwd=str(project_root), cli_defaults=cli)
        # .honeyhive/ wins over routes.json
        assert result.project == "hierarchical-project"


# ---------------------------------------------------------------------------
# Spool stamping: _resolved_config on spooled events
# ---------------------------------------------------------------------------


class TestSpoolStamping:
    def test_daemon_config_to_dict_roundtrip(self) -> None:
        config = DaemonConfig(
            api_key="key-1",
            base_url="https://api.example.com",
            project="my-project",
            repo_path="/tmp/repo",
            ci=True,
        )
        d = config.to_dict()
        restored = DaemonConfig.from_dict(d)
        assert restored.api_key == config.api_key
        assert restored.base_url == config.base_url
        assert restored.project == config.project
        assert restored.repo_path == config.repo_path
        assert restored.ci == config.ci

    def test_from_dict_uses_default_base_url(self) -> None:
        d = {"api_key": "k", "project": "p"}
        config = DaemonConfig.from_dict(d)
        assert config.base_url == DEFAULT_BASE_URL

    def test_from_dict_handles_none_base_url(self) -> None:
        d = {"api_key": "k", "project": "p", "base_url": None}
        config = DaemonConfig.from_dict(d)
        assert config.base_url == DEFAULT_BASE_URL


# ---------------------------------------------------------------------------
# Cache invalidation
# ---------------------------------------------------------------------------


class TestCacheInvalidation:
    def test_cache_returns_same_result_on_unchanged_files(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        invalidate_config_cache()

        user_config_path = tmp_path / "home" / ".honeyhive" / "config.json"
        user_config_path.parent.mkdir(parents=True)
        user_config_path.write_text(
            json.dumps({"project": "cached-proj"}), encoding="utf-8"
        )
        monkeypatch.setattr(
            "honeyhive_daemon.config._get_user_config_path",
            lambda: user_config_path,
        )
        monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon"))

        r1 = resolve_config(cwd=None)
        r2 = resolve_config(cwd=None)
        # Same object from cache
        assert r1 is r2

    def test_invalidate_clears_cache(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        invalidate_config_cache()

        user_config_path = tmp_path / "home" / ".honeyhive" / "config.json"
        user_config_path.parent.mkdir(parents=True)
        user_config_path.write_text(
            json.dumps({"project": "before"}), encoding="utf-8"
        )
        monkeypatch.setattr(
            "honeyhive_daemon.config._get_user_config_path",
            lambda: user_config_path,
        )
        monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon"))

        r1 = resolve_config(cwd=None)
        assert r1.project == "before"

        # Update file content (but mtime may be same within 1s)
        user_config_path.write_text(
            json.dumps({"project": "after"}), encoding="utf-8"
        )
        invalidate_config_cache()
        r2 = resolve_config(cwd=None)
        assert r2.project == "after"

    def test_mtime_change_invalidates_cache(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        invalidate_config_cache()

        user_config_path = tmp_path / "home" / ".honeyhive" / "config.json"
        user_config_path.parent.mkdir(parents=True)
        user_config_path.write_text(
            json.dumps({"project": "v1"}), encoding="utf-8"
        )
        monkeypatch.setattr(
            "honeyhive_daemon.config._get_user_config_path",
            lambda: user_config_path,
        )
        monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon"))

        r1 = resolve_config(cwd=None)
        assert r1.project == "v1"

        # Change content and force mtime to change
        import time

        user_config_path.write_text(
            json.dumps({"project": "v2"}), encoding="utf-8"
        )
        # Force different mtime by setting it to future
        future_mtime = time.time() + 100
        os.utime(user_config_path, (future_mtime, future_mtime))

        r2 = resolve_config(cwd=None)
        assert r2.project == "v2"
        assert r2 is not r1


# ---------------------------------------------------------------------------
# _merge_to_daemon_config
# ---------------------------------------------------------------------------


class TestMergeToDaemonConfig:
    def test_resolved_api_key_takes_precedence(self, monkeypatch) -> None:
        monkeypatch.setenv("SOME_ENV", "env-key")
        merged = {
            "api_key_env": "SOME_ENV",
            "_resolved_api_key": "resolved-key",
            "project": "p",
        }
        result = _merge_to_daemon_config(merged, None)
        assert result.api_key == "resolved-key"

    def test_api_key_env_fallback(self, monkeypatch) -> None:
        monkeypatch.setenv("SOME_ENV", "env-key")
        merged = {"api_key_env": "SOME_ENV", "project": "p"}
        result = _merge_to_daemon_config(merged, None)
        assert result.api_key == "env-key"

    def test_cli_defaults_fallback_for_api_key(self) -> None:
        cli = DaemonConfig(api_key="cli-key", base_url=DEFAULT_BASE_URL, project="p")
        merged = {"project": "p"}
        result = _merge_to_daemon_config(merged, cli)
        assert result.api_key == "cli-key"

    def test_empty_merged_no_defaults(self) -> None:
        result = _merge_to_daemon_config({}, None)
        assert result.api_key == ""
        assert result.project == ""
        assert result.base_url == DEFAULT_BASE_URL


# ---------------------------------------------------------------------------
# save_user_config merges
# ---------------------------------------------------------------------------


class TestSaveUserConfig:
    def test_merges_with_existing(self, monkeypatch, tmp_path: Path) -> None:
        config_path = tmp_path / ".honeyhive" / "config.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            json.dumps({"existing_field": "keep", "api_key_env": "OLD"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "honeyhive_daemon.config._get_user_config_path",
            lambda: config_path,
        )
        save_user_config({"api_key_env": "NEW", "base_url": "https://new.api"})
        result = json.loads(config_path.read_text(encoding="utf-8"))
        assert result["existing_field"] == "keep"
        assert result["api_key_env"] == "NEW"
        assert result["base_url"] == "https://new.api"
