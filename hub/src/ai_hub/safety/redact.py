"""Redaction utility — masks secrets in logs, comments, and prompts."""

from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("AWS Key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("AWS Secret", re.compile(r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])")),
    ("GitHub Token", re.compile(r"gh[ps]_[A-Za-z0-9_]{36,}")),
    ("GitHub OAuth", re.compile(r"gho_[A-Za-z0-9_]{36,}")),
    (
        "Generic Secret",
        re.compile(
            r"""(?i)(?:password|secret|token|api[_-]?key|private[_-]?key)\s*[:=]\s*['"]?([^\s'"]{8,})"""
        ),
    ),
    ("Bearer Token", re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*")),
    (
        "Base64 Private Key",
        re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"),
    ),
    (
        "Connection String",
        re.compile(r"(?:mysql|postgres(?:ql)?|mongodb(?:\+srv)?|redis)://[^\s]+"),
    ),
]

REDACTED = "***REDACTED***"


def redact(text: str) -> str:
    """Replace known secret patterns with a redaction marker."""
    result = text
    for _name, pattern in _PATTERNS:
        result = pattern.sub(REDACTED, result)
    return result


def redact_dict(
    data: dict[str, object], sensitive_keys: set[str] | None = None
) -> dict[str, object]:
    """Redact string values whose keys match sensitive_keys, plus pattern-based redaction."""
    if sensitive_keys is None:
        sensitive_keys = {"token", "secret", "password", "api_key", "private_key", "authorization"}

    redacted: dict[str, object] = {}
    for key, value in data.items():
        if any(s in key.lower() for s in sensitive_keys):
            redacted[key] = REDACTED
        elif isinstance(value, str):
            redacted[key] = redact(value)
        elif isinstance(value, dict):
            redacted[key] = redact_dict(value, sensitive_keys)
        else:
            redacted[key] = value
    return redacted
