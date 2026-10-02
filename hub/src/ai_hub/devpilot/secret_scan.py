"""Pre-commit secret scanning on generated diffs.

Gitleaks is the primary scanner. A built-in pattern scanner always runs as well,
so the scan never silently passes just because Gitleaks is not installed.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ai_hub.safety.redact import redact

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("ai_hub.devpilot")


@dataclass
class SecretScanResult:
    clean: bool
    findings: list[str]
    gitleaks_ran: bool = True


_BUILTIN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("AWS access key ID", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    (
        "Private key block",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY"),
    ),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Stripe live key", re.compile(r"\b[sr]k_live_[0-9a-zA-Z]{24,}\b")),
    (
        "Hard-coded credential",
        re.compile(
            r"""(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\b"""
            r"""\s*[:=]\s*['"]([^'"\s]{16,})['"]"""
        ),
    ),
]
_PLACEHOLDER = re.compile(r"(?i)example|changeme|your[_-]|xxxx|<[^>]+>|\$\{|\{\{|placeholder|dummy")


def scan_builtin(diff_text: str) -> list[str]:
    """Scan the added lines of a unified diff. Findings never include the secret value."""
    findings: list[str] = []
    current_file = "<unknown>"
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            current_file = line[4:].removeprefix("b/").strip()
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        content = line[1:]
        for name, pattern in _BUILTIN_PATTERNS:
            match = pattern.search(content)
            if not match:
                continue
            if name == "Hard-coded credential" and _PLACEHOLDER.search(match.group(1)):
                continue
            findings.append(f"{name} in {current_file}")
    return list(dict.fromkeys(findings))


def _run_gitleaks(diff_text: str, repo_path: Path, timeout: int) -> tuple[bool, list[str], bool]:
    """Return (clean, findings, ran)."""
    try:
        result = subprocess.run(
            ["gitleaks", "detect", "--no-git", "--pipe", "--redact", "--exit-code", "1"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            input=diff_text,
        )
    except FileNotFoundError:
        logger.warning("Gitleaks not installed — relying on built-in secret scanner")
        return True, [], False
    except subprocess.TimeoutExpired:
        return False, ["Secret scan timed out — treating as failure"], True

    if result.returncode == 0:
        return True, [], True

    findings = [redact(line.strip()) for line in result.stdout.splitlines() if line.strip()]
    return False, findings or ["Gitleaks detected potential secrets"], True


def scan_diff_text(
    diff_text: str,
    repo_path: Path,
    *,
    timeout: int = 60,
    require_gitleaks: bool = False,
) -> SecretScanResult:
    """Scan diff text for secrets with Gitleaks (if present) plus the built-in scanner."""
    gl_clean, gl_findings, gl_ran = _run_gitleaks(diff_text, repo_path, timeout)
    findings = list(gl_findings)
    findings.extend(f for f in scan_builtin(diff_text) if f not in findings)

    if require_gitleaks and not gl_ran:
        findings.append("Gitleaks is required but not installed")

    return SecretScanResult(clean=not findings, findings=findings, gitleaks_ran=gl_ran)


def _get_staged_diff(repo_path: Path) -> str:
    """Get the staged diff for scanning."""
    try:
        result = subprocess.run(
            ["git", "diff", "--staged", "--no-color"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def scan_staged_diff(
    repo_path: Path, *, timeout: int = 60, require_gitleaks: bool = False
) -> SecretScanResult:
    """Scan the staged diff for secrets before commit."""
    return scan_diff_text(
        _get_staged_diff(repo_path),
        repo_path,
        timeout=timeout,
        require_gitleaks=require_gitleaks,
    )
