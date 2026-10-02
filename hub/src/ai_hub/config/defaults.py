"""Default configuration values for the AI Hub."""

from __future__ import annotations

DEFAULT_CONFIG: dict[str, object] = {
    "version": 1,
    "review_mode": "standard",
    "paths": {"ignore": [], "generated": []},
    "ai_review": {"enabled": True, "max_files": 50, "inline_min_severity": "medium"},
    "static_analysis": {"enabled": True, "tools": ["ruff", "semgrep"]},
    "security": {"enabled": True, "tools": ["semgrep", "gitleaks", "deps"]},
    "tests": {"command": None, "coverage_report": None, "timeout_minutes": 15},
    "quality_gate": {
        "enabled": True,
        "coverage_threshold": 80,
        "fail_on": {"critical": 1, "high": 1},
        "require_tests_pass": True,
        "required_checks": ["static", "security", "ai_review"],
    },
    "devpilot": {
        "enabled": True,
        "base_branch": "main",
        "test_command": None,
        "max_fix_attempts": 3,
        "max_changed_files": 20,
        "max_changed_lines": 800,
        "allow_workflow_changes": False,
    },
}
