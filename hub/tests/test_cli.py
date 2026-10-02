"""Tests for ai_hub.cli — validate-config command."""

from pathlib import Path
from textwrap import dedent

from typer.testing import CliRunner

from ai_hub.cli import app

runner = CliRunner()


class TestValidateConfig:
    def test_valid_config(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.yml"
        cfg.write_text(
            dedent("""\
            version: 1
            review_mode: standard
        """)
        )
        result = runner.invoke(app, ["validate-config", str(cfg)])
        assert result.exit_code == 0
        assert "valid" in result.output.lower() or "passed" in result.output.lower()

    def test_invalid_config(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.yml"
        cfg.write_text(
            dedent("""\
            version: 1
            unknown_key: true
        """)
        )
        result = runner.invoke(app, ["validate-config", str(cfg)])
        assert result.exit_code == 1

    def test_bad_yaml(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.yml"
        cfg.write_text("{{{{ bad yaml")
        result = runner.invoke(app, ["validate-config", str(cfg)])
        assert result.exit_code == 1
