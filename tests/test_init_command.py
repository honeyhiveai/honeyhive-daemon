"""Integration tests for the ``honeyhive-daemon init`` CLI command."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from honeyhive_daemon.main import cli


class TestInitCommand:
    """Tests for ``honeyhive-daemon init``."""

    def test_creates_honeyhive_dir_and_configs(self, tmp_path: Path) -> None:
        """init creates .honeyhive/ with config.json + config.local.json."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path) as td:
            result = runner.invoke(cli, ["init", "--project", "my-project"])
            assert result.exit_code == 0, result.output

            hh_dir = Path(td) / ".honeyhive"
            assert hh_dir.is_dir()

            config = json.loads(
                (hh_dir / "config.json").read_text(encoding="utf-8")
            )
            assert config["project"] == "my-project"

            local_config = json.loads(
                (hh_dir / "config.local.json").read_text(encoding="utf-8")
            )
            assert local_config["api_key_env"] == "HH_API_KEY"

    def test_custom_api_key_env(self, tmp_path: Path) -> None:
        """init respects --api-key-env flag."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path) as td:
            result = runner.invoke(
                cli,
                ["init", "--project", "proj", "--api-key-env", "CUSTOM_KEY"],
            )
            assert result.exit_code == 0, result.output

            local_config = json.loads(
                (Path(td) / ".honeyhive" / "config.local.json").read_text(
                    encoding="utf-8"
                )
            )
            assert local_config["api_key_env"] == "CUSTOM_KEY"

    def test_creates_gitignore_with_local_pattern(self, tmp_path: Path) -> None:
        """init creates .gitignore with config.local.json entry."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path) as td:
            result = runner.invoke(cli, ["init", "--project", "proj"])
            assert result.exit_code == 0, result.output

            gitignore = Path(td) / ".gitignore"
            assert gitignore.exists()
            content = gitignore.read_text(encoding="utf-8")
            assert ".honeyhive/config.local.json" in content

    def test_appends_to_existing_gitignore(self, tmp_path: Path) -> None:
        """init appends to an existing .gitignore without duplicating."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path) as td:
            gitignore = Path(td) / ".gitignore"
            gitignore.write_text("node_modules/\n.env\n", encoding="utf-8")

            result = runner.invoke(cli, ["init", "--project", "proj"])
            assert result.exit_code == 0, result.output

            content = gitignore.read_text(encoding="utf-8")
            assert content.startswith("node_modules/\n.env\n")
            assert ".honeyhive/config.local.json" in content
            # Only one occurrence
            assert content.count(".honeyhive/config.local.json") == 1

    def test_no_duplicate_gitignore_entry_on_rerun(self, tmp_path: Path) -> None:
        """Running init twice does not duplicate the .gitignore entry."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path) as td:
            runner.invoke(cli, ["init", "--project", "proj"])
            runner.invoke(cli, ["init", "--project", "proj"])

            content = (Path(td) / ".gitignore").read_text(encoding="utf-8")
            assert content.count(".honeyhive/config.local.json") == 1

    def test_existing_honeyhive_dir_warns_and_updates(self, tmp_path: Path) -> None:
        """init with existing .honeyhive/ emits a warning and updates files."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path) as td:
            # Create existing .honeyhive/ with old config
            hh_dir = Path(td) / ".honeyhive"
            hh_dir.mkdir()
            (hh_dir / "config.json").write_text(
                json.dumps({"project": "old-project"}), encoding="utf-8"
            )

            result = runner.invoke(cli, ["init", "--project", "new-project"])
            assert result.exit_code == 0, result.output
            assert "Warning:" in result.output
            assert "already exists" in result.output

            config = json.loads(
                (hh_dir / "config.json").read_text(encoding="utf-8")
            )
            assert config["project"] == "new-project"

    def test_init_without_git_root_creates_in_cwd(self, tmp_path: Path) -> None:
        """init works in a directory without .git — creates .honeyhive/ in cwd."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path) as td:
            # No .git directory exists
            result = runner.invoke(cli, ["init", "--project", "no-git-proj"])
            assert result.exit_code == 0, result.output

            hh_dir = Path(td) / ".honeyhive"
            assert hh_dir.is_dir()
            config = json.loads(
                (hh_dir / "config.json").read_text(encoding="utf-8")
            )
            assert config["project"] == "no-git-proj"

    def test_output_messages(self, tmp_path: Path) -> None:
        """init prints expected output messages."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(cli, ["init", "--project", "msg-proj"])
            assert result.exit_code == 0
            assert "Created" in result.output
            assert "config.json" in result.output
            assert "config.local.json" in result.output
            assert "Updated" in result.output

    def test_gitignore_no_trailing_newline(self, tmp_path: Path) -> None:
        """init handles a .gitignore that lacks a trailing newline."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path) as td:
            gitignore = Path(td) / ".gitignore"
            # Write without trailing newline
            gitignore.write_text("node_modules/", encoding="utf-8")

            result = runner.invoke(cli, ["init", "--project", "proj"])
            assert result.exit_code == 0, result.output

            content = gitignore.read_text(encoding="utf-8")
            lines = content.splitlines()
            assert "node_modules/" in lines
            assert ".honeyhive/config.local.json" in lines
