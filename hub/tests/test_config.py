"""Tests for ai_hub.config — schema validation, loading, merging."""

from pathlib import Path
from textwrap import dedent

import pytest
from pydantic import ValidationError

from ai_hub.config.loader import _deep_merge, load_config
from ai_hub.config.schema import HubConfig, ReviewMode
from ai_hub.errors import ConfigError


class TestHubConfigSchema:
    def test_defaults_are_valid(self) -> None:
        config = HubConfig()
        assert config.version == 1
        assert config.review_mode == ReviewMode.STANDARD

    def test_rejects_unknown_keys(self) -> None:
        with pytest.raises(ValidationError):
            HubConfig.model_validate({"version": 1, "unknown_field": True})

    def test_rejects_wrong_version(self) -> None:
        with pytest.raises(ValidationError):
            HubConfig.model_validate({"version": 2})

    def test_review_modes(self) -> None:
        for mode in ["light", "standard", "strict"]:
            config = HubConfig.model_validate({"version": 1, "review_mode": mode})
            assert config.review_mode == mode

    def test_rejects_invalid_review_mode(self) -> None:
        with pytest.raises(ValidationError):
            HubConfig.model_validate({"version": 1, "review_mode": "turbo"})

    def test_max_files_bounds(self) -> None:
        with pytest.raises(ValidationError):
            HubConfig.model_validate(
                {
                    "version": 1,
                    "ai_review": {"enabled": True, "max_files": 0},
                }
            )

    def test_coverage_threshold_bounds(self) -> None:
        with pytest.raises(ValidationError):
            HubConfig.model_validate(
                {
                    "version": 1,
                    "quality_gate": {"coverage_threshold": 150},
                }
            )

    def test_devpilot_max_fix_attempts_bounds(self) -> None:
        with pytest.raises(ValidationError):
            HubConfig.model_validate(
                {
                    "version": 1,
                    "devpilot": {"max_fix_attempts": 99},
                }
            )

    def test_valid_full_config(self) -> None:
        config = HubConfig.model_validate(
            {
                "version": 1,
                "review_mode": "strict",
                "paths": {"ignore": ["dist/**"], "generated": ["**/*_pb2.py"]},
                "ai_review": {"enabled": True, "max_files": 30, "inline_min_severity": "high"},
                "static_analysis": {"enabled": True, "tools": ["ruff"]},
                "security": {"enabled": True, "tools": ["semgrep", "gitleaks"]},
                "tests": {
                    "command": "pytest",
                    "coverage_report": "coverage.xml",
                    "timeout_minutes": 10,
                },
                "quality_gate": {
                    "enabled": True,
                    "coverage_threshold": 90,
                    "fail_on": {"critical": 1, "high": 2},
                    "require_tests_pass": True,
                    "required_checks": ["static", "security"],
                },
                "devpilot": {
                    "enabled": False,
                    "base_branch": "develop",
                    "max_fix_attempts": 5,
                    "max_changed_files": 10,
                    "max_changed_lines": 500,
                    "allow_workflow_changes": False,
                },
            }
        )
        assert config.review_mode == ReviewMode.STRICT
        assert config.quality_gate.coverage_threshold == 90
        assert config.devpilot.enabled is False


class TestDeepMerge:
    def test_scalar_overlay(self) -> None:
        assert _deep_merge({"a": 1}, {"a": 2}) == {"a": 2}

    def test_nested_merge(self) -> None:
        base = {"a": {"b": 1, "c": 2}}
        overlay = {"a": {"c": 3, "d": 4}}
        result = _deep_merge(base, overlay)
        assert result == {"a": {"b": 1, "c": 3, "d": 4}}

    def test_list_replacement(self) -> None:
        result = _deep_merge({"a": [1, 2]}, {"a": [3]})
        assert result == {"a": [3]}

    def test_new_keys(self) -> None:
        result = _deep_merge({"a": 1}, {"b": 2})
        assert result == {"a": 1, "b": 2}


class TestLoadConfig:
    def test_load_defaults(self) -> None:
        config, version = load_config()
        assert config.version == 1
        assert isinstance(version, str)
        assert len(version) == 12

    def test_load_from_file(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.yml"
        cfg_file.write_text(
            dedent("""\
            version: 1
            review_mode: strict
            quality_gate:
              coverage_threshold: 95
        """)
        )
        config, _ = load_config(cfg_file)
        assert config.review_mode == ReviewMode.STRICT
        assert config.quality_gate.coverage_threshold == 95

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_config(tmp_path / "nonexistent.yml")

    def test_invalid_yaml_raises(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.yml"
        cfg_file.write_text("{{{{invalid yaml")
        with pytest.raises(ConfigError, match="Invalid YAML"):
            load_config(cfg_file)

    def test_non_dict_yaml_raises(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.yml"
        cfg_file.write_text("- just a list")
        with pytest.raises(ConfigError, match="must be a YAML mapping"):
            load_config(cfg_file)

    def test_unknown_key_raises(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.yml"
        cfg_file.write_text(
            dedent("""\
            version: 1
            surprise_key: true
        """)
        )
        with pytest.raises(ConfigError, match="validation failed"):
            load_config(cfg_file)

    def test_policy_overlay(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.yml"
        cfg_file.write_text("version: 1\nreview_mode: light\n")
        policy = {"quality_gate": {"coverage_threshold": 99}}
        config, _ = load_config(cfg_file, policy=policy)
        assert config.review_mode == ReviewMode.LIGHT
        assert config.quality_gate.coverage_threshold == 99

    def test_config_version_changes_on_different_input(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.yml"
        f1.write_text("version: 1\nreview_mode: light\n")
        f2 = tmp_path / "b.yml"
        f2.write_text("version: 1\nreview_mode: strict\n")
        _, v1 = load_config(f1)
        _, v2 = load_config(f2)
        assert v1 != v2

    def test_config_version_deterministic(self, tmp_path: Path) -> None:
        cfg = tmp_path / "c.yml"
        cfg.write_text("version: 1\n")
        _, v1 = load_config(cfg)
        _, v2 = load_config(cfg)
        assert v1 == v2
