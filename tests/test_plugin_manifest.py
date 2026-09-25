"""Tests for the honeyhive-observability Claude Code plugin."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MANIFEST = REPO_ROOT / ".claude-plugin" / "plugin.json"
HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"
INGEST_HOOK = REPO_ROOT / "hooks" / "honeyhive-ingest.sh"
PREFLIGHT_HOOK = REPO_ROOT / "hooks" / "honeyhive-preflight.sh"
GENERATOR = REPO_ROOT / "scripts" / "generate_plugin_hooks.py"


@pytest.fixture(scope="module")
def manifest() -> dict:
    """Parsed plugin manifest."""
    return json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def hooks_config() -> dict:
    """Parsed plugin hooks config."""
    return json.loads(HOOKS_JSON.read_text(encoding="utf-8"))


def test_manifest_name_and_metadata(manifest: dict) -> None:
    """Manifest declares the fields a marketplace listing needs."""
    assert manifest["name"] == "honeyhive-observability"
    for field in ("description", "version", "author", "homepage", "repository",
                  "license", "keywords"):
        assert manifest[field], f"missing plugin.json field: {field}"
    assert isinstance(manifest["keywords"], list)


def test_manifest_version_matches_package_version(manifest: dict) -> None:
    """The plugin version tracks the PyPI package it depends on."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for line in pyproject.splitlines():
        if line.startswith("version = "):
            package_version = line.split("=", 1)[1].strip().strip('"')
            break
    else:  # pragma: no cover - pyproject always has a version
        pytest.fail("no version found in pyproject.toml")

    assert manifest["version"] == package_version, (
        "plugin.json version must be bumped alongside pyproject.toml — the "
        "plugin only wires hooks to the installed honeyhive-daemon package"
    )


def test_hooks_cover_every_mapped_event(hooks_config: dict) -> None:
    """Plugin registers exactly the events the daemon can normalize."""
    from honeyhive_daemon.mappings import load_claude_code_mapping

    mapping = load_claude_code_mapping()
    expected = {r["hook_event_name"] for r in mapping["hook_registrations"]}
    assert set(hooks_config["hooks"]) == expected


def test_hooks_json_is_generated_from_the_mapping() -> None:
    """hooks.json has not drifted from claude_code.yaml."""
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_hook_commands_use_plugin_root(hooks_config: dict) -> None:
    """Every hook command resolves through ${CLAUDE_PLUGIN_ROOT}."""
    for event, entries in hooks_config["hooks"].items():
        for entry in entries:
            for hook in entry["hooks"]:
                assert hook["type"] == "command"
                assert "${CLAUDE_PLUGIN_ROOT}" in hook["command"], event
                assert hook["timeout"] > 0, event


def test_session_start_runs_preflight_first(hooks_config: dict) -> None:
    """The loud daemon check runs before the SessionStart ingest."""
    commands = [
        hook["command"]
        for entry in hooks_config["hooks"]["SessionStart"]
        for hook in entry["hooks"]
    ]
    assert "honeyhive-preflight.sh" in commands[0]
    assert any("honeyhive-ingest.sh" in c for c in commands)


def test_hook_scripts_are_executable() -> None:
    """Claude Code invokes the scripts directly, so the bits must be set."""
    for script in (INGEST_HOOK, PREFLIGHT_HOOK):
        assert os.access(script, os.X_OK), f"{script} is not executable"


BASH = shutil.which("bash") or "/bin/bash"


def _run_hook(script: Path, payload: str, env: dict) -> subprocess.CompletedProcess:
    """Run a hook script with a controlled environment.

    ``bash`` is invoked by absolute path so tests can scrub ``PATH`` down to a
    directory that does not contain ``honeyhive-daemon`` — the point of several
    of these cases — without also losing the shell itself.
    """
    merged = {**os.environ, "HONEYHIVE_DAEMON_BIN": "", **env}
    return subprocess.run(
        [BASH, str(script)],
        input=payload,
        capture_output=True,
        text=True,
        env=merged,
    )


def test_ingest_is_silent_and_non_blocking_without_the_daemon(tmp_path: Path) -> None:
    """A missing daemon must never turn into a hook error on every tool call."""
    result = _run_hook(
        INGEST_HOOK,
        '{"hook_event_name": "PreToolUse"}',
        {"PATH": str(tmp_path), "CLAUDE_PLUGIN_OPTION_HONEYHIVE_DAEMON_BIN": ""},
    )
    assert result.returncode == 0
    assert result.stderr == ""


def test_preflight_fails_loudly_without_the_daemon(tmp_path: Path) -> None:
    """The one place a missing daemon is reported: once, at SessionStart."""
    result = _run_hook(
        PREFLIGHT_HOOK,
        '{"hook_event_name": "SessionStart"}',
        {
            "PATH": str(tmp_path),
            "CLAUDE_PLUGIN_OPTION_HONEYHIVE_DAEMON_BIN": "",
            "HH_DAEMON_HOME": str(tmp_path / "home"),
        },
    )
    assert result.returncode != 0
    assert "honeyhive-daemon" in result.stderr
    assert "pip install honeyhive-daemon" in result.stderr


def test_preflight_reports_unconfigured_daemon(tmp_path: Path) -> None:
    """An installed-but-never-started daemon drops every event silently."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    stub = fake_bin / "honeyhive-daemon"
    stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)

    result = _run_hook(
        PREFLIGHT_HOOK,
        '{"hook_event_name": "SessionStart"}',
        {
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "CLAUDE_PLUGIN_OPTION_HONEYHIVE_DAEMON_BIN": "",
            "HH_DAEMON_HOME": str(tmp_path / "home"),
        },
    )
    assert result.returncode != 0
    assert "honeyhive-daemon run" in result.stderr


def test_preflight_passes_when_daemon_is_configured_and_running(
    tmp_path: Path,
) -> None:
    """A healthy setup stays quiet."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    stub = fake_bin / "honeyhive-daemon"
    stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)

    daemon_home = tmp_path / "home"
    (daemon_home / "state").mkdir(parents=True)
    (daemon_home / "state" / "config.json").write_text("{}", encoding="utf-8")
    # This process is alive by definition.
    (daemon_home / "daemon.pid").write_text(str(os.getpid()), encoding="utf-8")

    result = _run_hook(
        PREFLIGHT_HOOK,
        '{"hook_event_name": "SessionStart"}',
        {
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "CLAUDE_PLUGIN_OPTION_HONEYHIVE_DAEMON_BIN": "",
            "HH_DAEMON_HOME": str(daemon_home),
        },
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_ingest_forwards_payload_to_the_daemon(tmp_path: Path) -> None:
    """The wrapper pipes stdin straight into `ingest claude-hook`."""
    stub = tmp_path / "honeyhive-daemon"
    captured = tmp_path / "captured.txt"
    stub.write_text(
        f'#!/bin/sh\necho "$@" > {captured}\ncat >> {captured}\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)

    payload = '{"hook_event_name": "PostToolUse", "session_id": "abc"}'
    result = _run_hook(
        INGEST_HOOK,
        payload,
        {"CLAUDE_PLUGIN_OPTION_HONEYHIVE_DAEMON_BIN": str(stub)},
    )
    assert result.returncode == 0, result.stderr

    written = captured.read_text(encoding="utf-8")
    assert written.splitlines()[0] == "ingest claude-hook"
    assert payload in written


def test_ingest_exports_plugin_config_without_clobbering_the_shell(
    tmp_path: Path,
) -> None:
    """Plugin config fills in the API key; an exported one still wins."""
    stub = tmp_path / "honeyhive-daemon"
    captured = tmp_path / "env.txt"
    stub.write_text(
        f'#!/bin/sh\ncat > /dev/null\nprintenv HH_API_KEY > {captured}\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)

    base = {"CLAUDE_PLUGIN_OPTION_HONEYHIVE_DAEMON_BIN": str(stub)}

    _run_hook(INGEST_HOOK, "{}", {**base, "CLAUDE_PLUGIN_OPTION_HH_API_KEY": "from-plugin"})
    assert captured.read_text(encoding="utf-8").strip() == "from-plugin"

    _run_hook(
        INGEST_HOOK,
        "{}",
        {
            **base,
            "CLAUDE_PLUGIN_OPTION_HH_API_KEY": "from-plugin",
            "HH_API_KEY": "from-shell",
        },
    )
    assert captured.read_text(encoding="utf-8").strip() == "from-shell"


def test_status_skill_is_discoverable() -> None:
    """The plugin ships the setup/verify skill with usable frontmatter."""
    skill = REPO_ROOT / "skills" / "status" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    frontmatter = text.split("---", 2)[1]
    assert "name: status" in frontmatter
    assert "description:" in frontmatter
