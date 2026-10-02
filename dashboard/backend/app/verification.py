"""Repository setup verification: is the protected-branch workflow actually enforced?"""

from __future__ import annotations

from typing import Any

from app.github_client import GitHubUserClient

GATE_CHECK = "AI Hub / Quality Gate"
REQUIRED_WORKFLOWS = (".github/workflows/ai-review.yml", ".github/workflows/devpilot.yml")


def _classic_contexts(protection: dict[str, Any]) -> set[str]:
    checks = protection.get("required_status_checks") or {}
    contexts = set(checks.get("contexts") or [])
    contexts.update(c.get("context", "") for c in checks.get("checks") or [])
    return contexts


def evaluate_branch_protection(
    protection: dict[str, Any] | None, rules: list[dict[str, Any]]
) -> list[str]:
    """Return the list of problems; an empty list means the branch is properly protected.

    Satisfied by classic branch protection or by repository rulesets. Required: pull
    requests with at least one approval, the quality gate as a required check, and no
    force-pushes or deletions.
    """
    problems: list[str] = []

    approvals = 0
    gate_required = False
    force_push_blocked = False
    deletion_blocked = False

    if protection:
        reviews = protection.get("required_pull_request_reviews") or {}
        approvals = max(approvals, int(reviews.get("required_approving_review_count", 0) or 0))
        gate_required = gate_required or GATE_CHECK in _classic_contexts(protection)
        force_push_blocked = not (protection.get("allow_force_pushes") or {}).get("enabled", False)
        deletion_blocked = not (protection.get("allow_deletions") or {}).get("enabled", False)

    rule_types = {r.get("type") for r in rules}
    for rule in rules:
        params = rule.get("parameters") or {}
        if rule.get("type") == "pull_request":
            approvals = max(approvals, int(params.get("required_approving_review_count", 0) or 0))
        if rule.get("type") == "required_status_checks":
            contexts = {c.get("context") for c in params.get("required_status_checks") or []}
            gate_required = gate_required or GATE_CHECK in contexts
    if "non_fast_forward" in rule_types:
        force_push_blocked = True
    if "deletion" in rule_types:
        deletion_blocked = True

    if not protection and not rules:
        return ["The default branch has no branch protection or ruleset (or it could not be read)"]
    if approvals < 1:
        problems.append("Require a pull request with at least 1 approving review")
    if not gate_required:
        problems.append(f"Require the '{GATE_CHECK}' status check")
    if not force_push_blocked:
        problems.append("Block force pushes")
    if not deletion_blocked:
        problems.append("Block branch deletion")
    return problems


def verify_repository(
    gh: GitHubUserClient, full_name: str, default_branch: str, bot_login: str
) -> dict[str, Any]:
    """Run every setup check and explain each failure."""
    messages: list[str] = []

    bot_ok = False
    if not bot_login:
        messages.append("The dashboard has no DEVPILOT_BOT_LOGIN configured, so the bot cannot be checked")
    else:
        permission = gh.collaborator_permission(full_name, bot_login)
        bot_ok = permission in ("write", "admin")
        if not bot_ok:
            messages.append(f"Add '{bot_login}' as a collaborator with Write access")

    missing = [w for w in REQUIRED_WORKFLOWS if not gh.file_exists(full_name, w, default_branch)]
    workflows_ok = not missing
    messages.extend(f"Missing workflow file: {w}" for w in missing)

    problems = evaluate_branch_protection(
        gh.branch_protection(full_name, default_branch),
        gh.branch_rules(full_name, default_branch),
    )
    protection_ok = not problems
    messages.extend(problems)

    return {
        "bot_collaborator": bot_ok,
        "workflows_present": workflows_ok,
        "branch_protection": protection_ok,
        "all_passed": bot_ok and workflows_ok and protection_ok,
        "messages": messages,
    }
