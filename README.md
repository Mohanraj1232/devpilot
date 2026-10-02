# DevPilot — AI-based code review and issue-to-PR agent

A GitHub-integrated platform with two capabilities, built around reusable GitHub Actions workflows:

1. **AI code review and quality gate.** On every pull request: static analysis, security analysis
   (including secrets and dependencies), tests and coverage, an AI review with Claude on Amazon
   Bedrock, risk/quality scoring, and a configurable quality gate reported on the PR.
2. **DevPilot.** Label an issue `devpilot` and a Claude-powered agent implements it on a feature
   branch, runs the tests (with a limited number of repair attempts), and opens a pull request through
   a dedicated bot account. The result is reviewed by the pipeline above and approved by a human.

Nothing is merged automatically: `main` stays protected and a person approves every change.

```
hub/         the engine: the `ai-hub` CLI (review pipeline + DevPilot agent), Python 3.12
dashboard/   registration, settings and monitoring: FastAPI + React + MySQL
templates/   the three files a target repository adds
.github/     reusable workflows (ai-review, devpilot), CI and release
docs/        setup guide
plan.md      the original implementation plan
```

**Start with [docs/setup.md](docs/setup.md).** It lists the accounts, secrets, AWS permissions and
branch protection you need, and a first-run checklist. The code has automated tests and the workflows
are linted with `actionlint`, but it has not yet been exercised against real GitHub Actions, Bedrock
or MySQL; use a sandbox repository first.

## Development

```bash
# hub
cd hub && python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
ruff check src tests && ruff format --check src tests && mypy src && pytest

# dashboard
cd dashboard/backend && python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest respx && pytest
```

Some tests (real Gitleaks, symlinks) are skipped when the tool or OS support is missing; CI installs a
checksum-verified Gitleaks and runs them on Linux.
