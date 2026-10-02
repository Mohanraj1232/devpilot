"""Config loader: reads .ai-review/config.yml, merges with defaults and dashboard policy."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

import yaml
from pydantic import ValidationError

from ai_hub.config.defaults import DEFAULT_CONFIG
from ai_hub.config.schema import HubConfig
from ai_hub.errors import ConfigError


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge overlay into base. Overlay values win for scalars and lists."""
    merged = base.copy()
    for key, value in overlay.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(
    config_path: Path | None = None,
    policy: dict[str, Any] | None = None,
) -> tuple[HubConfig, str]:
    """Load, merge, validate, and return (config, config_version_hash).

    Merge order: hub defaults <- repo file <- dashboard policy.
    Raises ConfigError on any validation failure.
    """
    raw: dict[str, Any] = dict(DEFAULT_CONFIG)

    if config_path is not None:
        if not config_path.is_file():
            raise ConfigError(
                f"Config file not found: {config_path}",
                details={"path": str(config_path)},
            )
        try:
            with open(config_path, encoding="utf-8") as f:
                repo_config = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            raise ConfigError(f"Invalid YAML in config file: {exc}") from exc

        if repo_config is None:  # an empty (or comment-only) file means "all defaults"
            repo_config = {}

        if not isinstance(repo_config, dict):
            raise ConfigError("Config file must be a YAML mapping")

        raw = _deep_merge(raw, repo_config)

    if policy is not None:
        raw = _deep_merge(raw, policy)

    try:
        config = HubConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(
            "Config validation failed",
            details={"errors": exc.errors()},
        ) from exc

    config_version = hashlib.sha256(
        json.dumps(raw, sort_keys=True, default=str).encode()
    ).hexdigest()[:12]

    return config, config_version
