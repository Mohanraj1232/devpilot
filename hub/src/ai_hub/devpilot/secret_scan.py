"""Pre-commit secret scanning on generated diffs."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("ai_hub.devpilot")


@dataclass
class SecretScanResult:
    clean: bool
    findings: list[str]


def scan_staged_diff(repo_path: Path, *, timeout: int = 60) -> SecretScanResult:
    """Run Gitleaks on the staged diff to detect secrets before commit."""
    try:
        result = subprocess.run(
            ["gitleaks", "detect", "--no-git", "--pipe", "--exit-code", "1"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=_get_staged_diff(repo_path),
        )
    except FileNotFoundError:
        logger.warning("Gitleaks not installed — skipping secret scan")
        return SecretScanResult(clean=True, findings=[])
    except subprocess.TimeoutExpired:
        return SecretScanResult(
            clean=False,
            findings=["Secret scan timed out — treating as failure"],
        )

    if result.returncode == 0:
        return SecretScanResult(clean=True, findings=[])

    findings = [line.strip() for line in result.stdout.splitlines() if line.strip()]

    return SecretScanResult(
        clean=False,
        findings=findings or ["Gitleaks detected potential secrets"],
    )


def _get_staged_diff(repo_path: Path) -> str:
    """Get the staged diff for scanning."""
    try:
        result = subprocess.run(
            ["git", "diff", "--staged"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def scan_diff_text(diff_text: str, repo_path: Path, *, timeout: int = 60) -> SecretScanResult:
    """Scan arbitrary diff text for secrets."""
    try:
        result = subprocess.run(
            ["gitleaks", "detect", "--no-git", "--pipe", "--exit-code", "1"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=diff_text,
        )
    except FileNotFoundError:
        return SecretScanResult(clean=True, findings=[])
    except subprocess.TimeoutExpired:
        return SecretScanResult(clean=False, findings=["Scan timed out"])

    if result.returncode == 0:
        return SecretScanResult(clean=True, findings=[])

    findings = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return SecretScanResult(
        clean=False,
        findings=findings or ["Potential secrets detected"],
    )
