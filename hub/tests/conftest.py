"""Test-suite wide fixtures."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _ci_like_git_environment(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run every test as on a CI runner: no global git identity, and git refuses to guess one.

    A developer's own ~/.gitconfig (or an auto-detected OS account name) otherwise hides code that
    forgets to supply an identity - a `git rebase` rewrites commits and fails on a clean runner.
    """
    config: Path = tmp_path_factory.mktemp("gitconfig") / "gitconfig"
    config.write_text("[user]\n\tuseConfigOnly = true\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)
