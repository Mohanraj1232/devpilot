"""ESLint adapter — JS/TS linting via JSON output."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ai_hub.analysis.base import (
    ToolAdapter,
    parse_json_output,
    run_tool_subprocess,
    select_files,
)
from ai_hub.models import Finding, FindingCategory, FindingSource, Severity

if TYPE_CHECKING:
    from pathlib import Path

_SEVERITY_MAP: dict[int, Severity] = {
    0: Severity.INFO,
    1: Severity.LOW,
    2: Severity.MEDIUM,
}


_JS_EXTENSIONS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")
_CONFIG_NAMES = (
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.json",
    ".eslintrc.yml",
    ".eslintrc.yaml",
    "eslint.config.js",
    "eslint.config.mjs",
    "eslint.config.cjs",
    "eslint.config.ts",
)


def _has_eslint_config(repo_path: Path) -> bool:
    if any((repo_path / name).is_file() for name in _CONFIG_NAMES):
        return True
    package_json = repo_path / "package.json"
    try:
        return "eslintConfig" in json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False


class ESLintAdapter(ToolAdapter):
    name = "eslint"
    tool_cmd = "npx"

    def applicable(
        self, repo_path: Path, changed_files: list[str] | None = None
    ) -> tuple[bool, str]:
        files = select_files(changed_files, _JS_EXTENSIONS)
        if files is not None and not files:
            return False, "No JavaScript/TypeScript files changed"
        if not _has_eslint_config(repo_path):
            return False, "No ESLint configuration found"
        return True, ""

    def run(self, repo_path: Path, changed_files: list[str] | None = None) -> list[Finding]:
        # --no-install: never download and run code from the registry during a review.
        cmd = [
            "npx",
            "--no-install",
            "eslint",
            "--format=json",
            "--no-error-on-unmatched-pattern",
        ]
        files = select_files(changed_files, _JS_EXTENSIONS)
        cmd.extend(files if files else ["."])

        # Exit 0 = clean, 1 = lint errors, 2 = ESLint failed (bad config, crash).
        result = run_tool_subprocess(cmd, cwd=repo_path, ok_returncodes=(0, 1))
        raw_results = parse_json_output(result, "eslint")

        findings: list[Finding] = []
        for file_result in raw_results:
            filepath = file_result.get("filePath", "")
            for msg in file_result.get("messages", []):
                severity_num = msg.get("severity", 1)
                severity = _SEVERITY_MAP.get(severity_num, Severity.MEDIUM)
                rule_id = msg.get("ruleId") or "unknown"

                category = FindingCategory.STYLE
                if "security" in rule_id.lower() or "no-eval" in rule_id:
                    category = FindingCategory.SECURITY
                elif "no-unused" in rule_id or "prefer-" in rule_id:
                    category = FindingCategory.MAINTAINABILITY

                findings.append(
                    Finding(
                        source=FindingSource.STATIC,
                        tool="eslint",
                        rule_id=rule_id,
                        category=category,
                        severity=severity,
                        file=filepath,
                        line_start=msg.get("line", 0),
                        line_end=msg.get("endLine"),
                        title=f"{rule_id}: {msg.get('message', '')}",
                        explanation=msg.get("message", ""),
                        suggested_fix=msg.get("fix", {}).get("text") if msg.get("fix") else None,
                    )
                )
        return findings
