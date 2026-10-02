"""Execution lock management for DevPilot — prevents concurrent runs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ExecutionLock:
    """Represents an active execution lock for an issue."""

    repo_full_name: str
    issue_number: int
    execution_id: str
    lock_key: str

    @classmethod
    def make_key(cls, repo_full_name: str, issue_number: int) -> str:
        return f"devpilot:{repo_full_name}:issue-{issue_number}"


def create_lock_key(repo_full_name: str, issue_number: int, execution_id: str) -> ExecutionLock:
    """Create a new execution lock."""
    return ExecutionLock(
        repo_full_name=repo_full_name,
        issue_number=issue_number,
        execution_id=execution_id,
        lock_key=ExecutionLock.make_key(repo_full_name, issue_number),
    )
