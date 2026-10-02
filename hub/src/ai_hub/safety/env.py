"""Environment scrubbing for subprocesses that run untrusted repository code.

Dependency installs and test suites execute code from the target repository (and
code written by the AI). They must never inherit the bot token, cloud
credentials, or dashboard tokens from the orchestrator's environment.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

_SENSITIVE_NAME = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API[_-]?KEY|PRIVATE[_-]?KEY|AUTH)",
    re.IGNORECASE,
)
_SENSITIVE_PREFIXES = (
    "AWS_",
    "GITHUB_",
    "ACTIONS_",
    "RUNNER_",
    "DASHBOARD_",
    "DEVPILOT_",
    "BEDROCK_",
    "INPUT_",
    "ANTHROPIC_",
)
# Names that match the patterns above but are harmless and commonly needed by tooling.
_ALLOWED = {"GITHUB_WORKSPACE", "GITHUB_ACTIONS", "CI"}


def scrubbed_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a copy of the environment with credential-like variables removed."""
    source = os.environ if base is None else base
    clean: dict[str, str] = {}
    for name, value in source.items():
        if name in _ALLOWED:
            clean[name] = value
            continue
        upper = name.upper()
        if upper.startswith(_SENSITIVE_PREFIXES) or _SENSITIVE_NAME.search(upper):
            continue
        clean[name] = value
    # Never let child processes pick up git credentials injected for the orchestrator.
    for name in ("GIT_CONFIG_COUNT", "GIT_ASKPASS", "SSH_AUTH_SOCK"):
        clean.pop(name, None)
    for name in [n for n in clean if n.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))]:
        clean.pop(name)
    return clean
