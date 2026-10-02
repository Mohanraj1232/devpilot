# DevPilot — AI Context Guide

This document is written for AI assistants working on this codebase. It explains what the project is, how it is structured, and how data flows through the system.

## What Is DevPilot?

DevPilot is an AI-based automated code review and quality analysis system built as a GitHub-integrated monorepo. It provides two main pipelines that run as reusable GitHub Actions workflows:

1. **AI Review Pipeline** — Triggered on pull requests. Runs static analysis, security scanning, AI-powered code review (Claude via Amazon Bedrock), coverage analysis, scoring, and a configurable quality gate. Posts findings as PR comments and a GitHub Check Run.

2. **DevPilot Agent** — Triggered when an issue is labeled `devpilot`. An AI coding agent reads the issue, clones the repo, creates a plan, implements the fix on a feature branch using sandboxed tools (no shell access), runs tests with repair retries, and opens a PR through a bot account. Humans must still approve and merge.

There is also a **Dashboard** (FastAPI + React) for repository registration, configuration, monitoring, and analytics.

## Repository Layout

```
devpilot/
├── .github/workflows/
│   ├── ai-review.yml          # Reusable workflow: PR review pipeline
│   ├── devpilot.yml           # Reusable workflow: DevPilot coding agent
│   ├── hub-ci.yml             # CI for this repo (lint, type-check, test)
│   └── release.yml            # Tag-based releases (v1, vX.Y.Z)
│
├── hub/                       # Python package: ai_hub (the core engine)
│   ├── pyproject.toml         # Hatch build, Python 3.12+, deps: boto3, httpx, pydantic, typer
│   ├── src/ai_hub/
│   │   ├── cli.py             # Typer CLI: review, gate, report, devpilot, validate-config
│   │   ├── models.py          # Domain models: Finding, Severity, CheckResult, RunContext, etc.
│   │   ├── errors.py          # Typed failure taxonomy (FailureReason enum) + safe_stop()
│   │   ├── config/            # Pydantic v2 strict config schema, YAML loader, defaults
│   │   ├── github/            # httpx-based GitHub REST client with retry/backoff
│   │   ├── llm/               # Amazon Bedrock Converse API client, tool schemas, prompts
│   │   ├── analysis/
│   │   │   ├── diff.py        # Unified diff parser, hunk extraction, changed-line maps
│   │   │   ├── static/        # Tool adapters: Ruff, ESLint, Semgrep
│   │   │   ├── security/      # Tool adapters: Bandit, Gitleaks, pip-audit, npm-audit
│   │   │   └── coverage.py    # Cobertura XML and LCOV parsers
│   │   ├── review/
│   │   │   ├── pipeline.py    # Runs static/security/test stages, collects findings
│   │   │   ├── ai_review.py   # LLM-based code review with chunking and validation
│   │   │   ├── inputs.py      # Prepares diff + context for the LLM
│   │   │   ├── outcome.py     # Merges findings, dedupes, computes scores, evaluates gate
│   │   │   ├── results.py     # Result aggregation helpers
│   │   │   └── chunker.py     # Splits large diffs into token-budgeted chunks
│   │   ├── scoring/engine.py  # Risk score (0-100) and quality score (0-100) computation
│   │   ├── gate/evaluator.py  # Ordered rule evaluation: config→checks→severity→tests→coverage
│   │   ├── report/
│   │   │   ├── markdown.py    # Sticky PR comment (<!-- ai-hub:summary -->)
│   │   │   ├── inline.py      # Inline review comments on changed lines
│   │   │   ├── checkrun.py    # GitHub Check Run (AI Hub / Quality Gate)
│   │   │   ├── sarif.py       # SARIF output for GitHub code scanning
│   │   │   ├── publisher.py   # Orchestrates all report outputs
│   │   │   ├── dashboard.py   # Posts results to the dashboard API
│   │   │   └── dedup.py       # Finding deduplication by fingerprint
│   │   ├── devpilot/          # The coding agent subsystem
│   │   │   ├── orchestrator.py    # Main entry: guard→triage→workspace→plan→implement→PR
│   │   │   ├── trigger.py         # Label check, policy validation, issue preconditions
│   │   │   ├── llm_tasks.py       # LLM calls: triage_issue_llm(), make_plan()
│   │   │   ├── workspace.py       # Clone, project detection, dependency install
│   │   │   ├── repo_map.py        # Builds a size-limited repo map for LLM context
│   │   │   ├── tools.py           # Sandboxed tool definitions: list_dir, read_file, write_file, etc.
│   │   │   ├── agent.py           # Tool-use loop with budget limits (calls, tokens, wall time)
│   │   │   ├── tester.py          # Test runner + repair retry loop with diff-hash dedup
│   │   │   ├── guardrails.py      # Diff size, unrelated changes, forbidden paths, destructive ops
│   │   │   ├── secret_scan.py     # Gitleaks on staged diff before commit
│   │   │   ├── preflight.py       # Pre-push revalidation (issue state, base HEAD, clean tree)
│   │   │   ├── git_ops.py         # Branch, commit, push, rebase operations
│   │   │   ├── pr.py              # PR body builder and payload
│   │   │   ├── branch_guard.py    # Execution trailer, branch ownership, unique naming
│   │   │   ├── duplicate.py       # Duplicate execution and existing-PR detection
│   │   │   ├── lock.py            # Execution lock (dashboard + in-progress label)
│   │   │   ├── flaky.py           # Flaky test detection via rerun analysis
│   │   │   └── prompts/           # Markdown prompt templates (system, plan, triage)
│   │   ├── safety/
│   │   │   ├── redact.py          # Secret redaction in logs, comments, and prompts
│   │   │   ├── env.py             # Environment scrubbing for tool subprocesses
│   │   │   └── untrusted.py       # Wraps user content in <untrusted> blocks
│   │   └── telemetry/
│   │       └── dashboard_client.py # httpx client for dashboard ingest API
│   └── tests/                 # Unit tests (pytest), fixture files, review helpers
│
├── dashboard/
│   ├── backend/
│   │   ├── app/
│   │   │   ├── main.py           # FastAPI app with session + CORS middleware
│   │   │   ├── config.py         # Pydantic settings (env prefix DEVPILOT_)
│   │   │   ├── database.py       # SQLAlchemy engine + session factory
│   │   │   ├── github_client.py  # Server-side GitHub API calls
│   │   │   ├── verification.py   # Repo setup verification (bot, workflows, protection)
│   │   │   ├── models/
│   │   │   │   └── tables.py     # 11 SQLAlchemy tables (User, Repository, ReviewRun, Finding, etc.)
│   │   │   ├── schemas/          # Pydantic request/response schemas
│   │   │   └── api/
│   │   │       ├── auth.py       # GitHub OAuth login/callback/logout
│   │   │       ├── repos.py      # Repository CRUD + verification
│   │   │       ├── policy.py     # Quality policy versioning
│   │   │       ├── ingest.py     # Workflow data ingestion (review runs, executions, locks)
│   │   │       ├── reviews.py    # Review run + findings queries
│   │   │       ├── executions.py # DevPilot execution queries
│   │   │       ├── analytics.py  # Aggregated charts data
│   │   │       ├── webhooks.py   # GitHub webhook receiver (HMAC verified)
│   │   │       └── deps.py       # Shared dependencies (auth, DB session, repo access)
│   │   ├── alembic/              # Database migrations
│   │   ├── tests/                # API tests, auth tests, security tests
│   │   └── requirements.txt
│   ├── frontend/
│   │   ├── src/
│   │   │   ├── App.tsx           # React Router: 6 routes
│   │   │   ├── api/client.ts     # Typed API client
│   │   │   └── pages/            # Login, Repos, RepoDetail, ReviewRunDetail, ExecutionDetail, Analytics
│   │   ├── package.json          # React 18 + Vite + TypeScript + Recharts
│   │   └── vite.config.ts        # Proxies /api to backend
│   └── docker-compose.yml        # MySQL 8 + backend + frontend
│
├── templates/target-repo/        # Files users copy into their repos
│   ├── .ai-review/config.yml     # Default configuration
│   └── .github/workflows/
│       ├── ai-review.yml         # ~15-line caller workflow
│       └── devpilot.yml          # ~15-line caller workflow
│
├── docs/setup.md
└── plan.md                       # Full implementation plan (the design document)
```

## Technology Stack

| Component | Technology |
|---|---|
| Hub engine | Python 3.12, `hatch` build, Pydantic v2 strict, Typer CLI |
| LLM | Claude on Amazon Bedrock (Converse API with tool use). Model ID is configurable, never hardcoded |
| AWS auth | GitHub OIDC → IAM role. No long-lived keys |
| GitHub API | `httpx` REST client with retry/backoff and rate-limit handling |
| Static analysis tools | Ruff (Python), ESLint (JS/TS), Semgrep (multi-language) |
| Security tools | Bandit (Python), Gitleaks (secrets), pip-audit / npm-audit (deps) |
| Coverage | Cobertura XML and LCOV parsers |
| Dashboard backend | FastAPI, SQLAlchemy 2.x, Alembic, Pydantic v2, session-based GitHub OAuth |
| Database | MySQL 8 (production), SQLite (local dev / CI) |
| Dashboard frontend | React 18 + Vite + TypeScript, Recharts for charts |
| CI | GitHub Actions (`hub-ci.yml`): ruff, mypy, pytest, dashboard tests, migration check |

## Data Flow: PR Review Pipeline

```
Developer opens/updates a PR
        │
        ▼
Target repo's ai-review.yml (caller) ──calls──► devpilot/ai-review.yml (reusable)
        │
        ├─► [prepare] Check out code + hub, install ai_hub, validate config, capture diff
        │
        ├─► [static]   Ruff / ESLint / Semgrep ──► normalized findings JSON
        ├─► [security] Bandit / Gitleaks / deps ──► normalized findings JSON
        ├─► [tests]    Run test command, parse coverage ──► check result + coverage data
        │   (these three run in parallel)
        │
        ├─► [ai-review] Chunk diff → Bedrock Converse API → validated findings
        │   (skipped for fork PRs — no secrets available)
        │
        └─► [score-gate-report]
            ├─ Merge + deduplicate findings (fingerprint-based)
            ├─ Compute risk score (0-100) and quality score (0-100)
            ├─ Evaluate quality gate (ordered rules)
            ├─ Upsert sticky PR comment with summary
            ├─ Post inline review comments (severity ≥ threshold)
            ├─ Create Check Run "AI Hub / Quality Gate" (success/failure)
            ├─ Upload SARIF (optional)
            └─ POST results to dashboard (best-effort)
```

## Data Flow: DevPilot Agent

```
User adds "devpilot" label to a GitHub issue
        │
        ▼
Target repo's devpilot.yml (caller) ──calls──► devpilot/devpilot.yml (reusable)
        │
        ▼
    ┌─ GUARD ─────────────────────────────────────────────────┐
    │  • Label is "devpilot", issue is open                   │
    │  • Repo registered + DevPilot enabled (dashboard policy)│
    │  • Bot is collaborator with push access                 │
    │  • No duplicate execution (lock + in-progress label)    │
    │  • No existing open DevPilot PR for this issue          │
    │  • LLM triage: issue is actionable (not vague/resolved) │
    └─────────────────────────────────────────────────────────┘
        │
        ▼
    ┌─ WORKSPACE ─────────────────────────────────────────────┐
    │  Clone repo (bot token), detect project type, install   │
    │  dependencies, init submodules                          │
    └─────────────────────────────────────────────────────────┘
        │
        ▼
    ┌─ PLAN ──────────────────────────────────────────────────┐
    │  Claude receives: issue (wrapped as untrusted), repo    │
    │  map (tree + key files). Returns: structured plan       │
    │  (files to touch, approach, test strategy)              │
    └─────────────────────────────────────────────────────────┘
        │
        ▼
    ┌─ IMPLEMENT ─────────────────────────────────────────────┐
    │  Tool-use loop with sandboxed tools:                    │
    │  list_dir, read_file, write_file, apply_edit,           │
    │  search_code, run_tests, finish                         │
    │  NO shell access. All paths sandboxed to workspace.     │
    │  Budget limits: max tool calls, tokens, wall time.      │
    └─────────────────────────────────────────────────────────┘
        │
        ▼
    ┌─ TEST + REPAIR ─────────────────────────────────────────┐
    │  Run tests → on failure, send error to Claude for fix   │
    │  Retry up to max_fix_attempts (default 3)               │
    │  Stop early if diff hash repeats (same broken fix)      │
    │  Detect flaky tests via rerun analysis                  │
    └─────────────────────────────────────────────────────────┘
        │
        ▼
    ┌─ GUARDRAILS ────────────────────────────────────────────┐
    │  • Diff size within limits (files + lines)              │
    │  • No unrelated changes vs. the plan                    │
    │  • Secret scan (Gitleaks) on staged diff                │
    │  • No forbidden changes (workflows, CODEOWNERS, etc.)   │
    │  • No destructive operations without policy permission  │
    └─────────────────────────────────────────────────────────┘
        │
        ▼
    ┌─ PRE-PUSH REVALIDATION ─────────────────────────────────┐
    │  Re-check: issue still open? body edited? base moved?   │
    │  Rebase on base if needed (conflict → stop). Never      │
    │  force-push. No foreign commits on the branch.          │
    └─────────────────────────────────────────────────────────┘
        │
        ▼
    ┌─ COMMIT + PUSH + PR ────────────────────────────────────┐
    │  Branch: devpilot/issue-<n>-<slug>                      │
    │  Commits carry DevPilot-Execution trailer                │
    │  PR body: summary, plan, test evidence, "Closes #<n>"   │
    │  The PR triggers the ai-review pipeline automatically   │
    └─────────────────────────────────────────────────────────┘
        │
        ▼
    ┌─ REPORT ────────────────────────────────────────────────┐
    │  Comment on the issue with outcome                      │
    │  Remove in-progress label, release lock                 │
    │  POST execution record to dashboard                     │
    └─────────────────────────────────────────────────────────┘
```

Any stage can **safe-stop**: preserve state as artifacts, report the failure on the issue and dashboard, release the lock, and exit. Nothing is ever auto-merged.

## Domain Models

**`hub/src/ai_hub/models.py`** defines the core types:

- `Severity` — info, low, medium, high, critical
- `Finding` — source (ai/static/security/coverage), tool, rule_id, category, severity, file, line range, title, explanation, suggested_fix, fingerprint (auto-computed SHA-256), status
- `CheckResult` — name, status (success/failed/error/skipped), summary, duration
- `RunContext` — repo, PR/issue number, SHAs, workflow run ID, config version
- `ReviewResult` — aggregated checks, findings, scores, gate result
- `ExecutionStatus` — queued → running → pr_opened / failed / needs_clarification / etc.

**`hub/src/ai_hub/errors.py`** has a `FailureReason` enum with 35+ typed failure codes covering config, auth, issues, workspace, AI, git, tests, guardrails, and dashboard failures. Every error flows through `HubError` subclasses (`ConfigError`, `AuthError`, `LLMError`, `GitError`, `GuardrailError`, `AnalysisError`).

## Scoring and Quality Gate

**Risk score (0-100, higher = riskier):**
`min(100, Σ severity_weights + size_factor + sensitive_path_factor + coverage_factor)`

Severity weights: critical=40, high=20, medium=8, low=2. Size factor = changed_lines/50 (cap 15). Sensitive paths (auth/crypto/infra) add +10. Below coverage threshold adds +10.

**Quality score (0-100):**
`100 - weighted_penalties` (static findings, coverage gap, test failures).

**Gate rules (evaluated in order, first failure wins):**
1. Config invalid → FAIL
2. Required check is error/missing → FAIL
3. Critical findings ≥ threshold → FAIL
4. High findings ≥ threshold → FAIL
5. Tests failed → FAIL (if require_tests_pass)
6. Coverage below threshold → FAIL
7. Otherwise → PASS

PASS never merges. Branch protection requires human approval.

## Configuration

Repos configure DevPilot via `.ai-review/config.yml` (Pydantic v2 strict validation — unknown keys and out-of-range values are errors):

```yaml
version: 1
review_mode: standard          # light | standard | strict
paths:
  ignore: ["dist/**"]
  generated: ["**/*_pb2.py"]
ai_review:
  enabled: true
  max_files: 50
static_analysis:
  enabled: true
  tools: [ruff, semgrep]
security:
  enabled: true
  tools: [semgrep, gitleaks, deps]
tests:
  command: "pytest --cov --cov-report=xml"
quality_gate:
  coverage_threshold: 80
  fail_on: { critical: 1, high: 1 }
devpilot:
  enabled: true
  max_fix_attempts: 3
  max_changed_files: 20
  max_changed_lines: 800
```

Effective config = hub defaults ← repo file ← dashboard policy (dashboard can only tighten).

## Database Schema

11 tables in `dashboard/backend/app/models/tables.py`:

| Table | Purpose |
|---|---|
| `users` | GitHub OAuth users (github_id, login) |
| `repositories` | Registered repos (owner, name, status, review/devpilot toggles) |
| `repo_policies` | Append-only versioned quality policy per repo (JSON) |
| `ingest_tokens` | Per-repo tokens for workflow authentication (SHA-256 hashed) |
| `review_runs` | PR review results (scores, gate result, config version) |
| `check_results` | Individual check results per review run |
| `findings` | Individual findings per review run (with fingerprint + resolution) |
| `devpilot_executions` | Agent execution state (with lock_key for concurrency) |
| `devpilot_attempts` | Per-attempt records within an execution |
| `webhook_deliveries` | GitHub webhook idempotency (delivery_id dedup) |
| `audit_log` | Actor/action/target audit trail |

## Security Model

- **Bot account**: Fine-grained PAT scoped to registered repos (Contents, PRs, Issues R/W). No admin scope. Stored as repo secret `DEVPILOT_BOT_TOKEN`.
- **Workflow permissions**: Least privilege per job. `id-token: write` only where OIDC is needed.
- **AWS**: OIDC-federated IAM role, policy allows only `bedrock:InvokeModel*`/`Converse*`.
- **Tool sandbox**: The DevPilot agent has NO shell access. Tools enforce workspace-only paths. `.git/`, `.github/workflows/`, vendor, and lockfile paths are denied by default.
- **Prompt injection**: Issue/repo content wrapped in `<untrusted>` blocks. Tool layer enforces policy regardless of model output.
- **Secret handling**: `redact.py` masks secrets in logs, comments, and prompts. Gitleaks scans staged diffs before commit.
- **Dashboard**: Session-based GitHub OAuth, webhook HMAC verification, per-repo ingest tokens (hashed at rest).

## Key Design Decisions

1. **Fail closed, not open**: If the dashboard is unreachable, DevPilot refuses to run (it writes code). AI review falls back to repo-level config only.
2. **Never auto-merge**: The system creates PRs and Check Runs. Humans approve and merge through branch protection.
3. **Tool adapters fail to error, not clean**: A crashing analysis tool becomes `status: error`, which fails the quality gate when required. It never produces a false-clean result.
4. **Config from base branch**: Quality gate config comes from the PR's base branch, not the PR itself. A PR cannot weaken its own gate.
5. **Execution trailers**: DevPilot commits carry `DevPilot-Execution: <id>` to track branch ownership and prevent force-pushing over foreign commits.
6. **Idempotent ingestion**: Review runs use `workflow_run_id` as upsert key. Webhooks deduplicate on `X-GitHub-Delivery`.
7. **Soft deletes**: Removing a repo sets `status=removed`. History is preserved and future workflow runs are rejected.

## Testing

Tests live in `hub/tests/` and `dashboard/backend/tests/`. The project uses:

- **pytest** with strict markers and coverage
- **respx** for mocking httpx HTTP calls
- **botocore.stub.Stubber** for mocking Bedrock Converse API
- **SQLite in-memory** for dashboard API tests
- **Fixture files** in `hub/tests/fixtures/` (tool output samples)

CI runs: ruff check, ruff format, mypy strict, pytest (hub + dashboard), alembic migration round-trip.

## Conventions

- All Python source uses `from __future__ import annotations` for deferred evaluation
- Pydantic v2 with strict validation throughout
- Type hints are enforced by mypy in strict mode
- Ruff handles linting and formatting (line length 100, target py312)
- The `TCH` ruff rule requires type-only imports in `TYPE_CHECKING` blocks
- Error handling uses the typed `HubError` hierarchy, never bare exceptions
- CLI commands are Typer-based, entry point is `ai-hub`
