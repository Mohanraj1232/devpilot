# DevPilot: Implementation Plan
**AI-Based Automated Code Review and Quality Analysis System**

## 1. Goals and non-goals
**Goals**
- A Central AI Hub repo (`Mohanraj1232/devpilot`) that provides reusable GitHub Actions workflows for: AI code review, static analysis, security analysis, test/coverage analysis, quality and risk scoring, a configurable quality gate, and PR reporting.
- DevPilot: a Claude-on-Amazon-Bedrock coding agent triggered by the `devpilot` issue label. It implements the issue on a feature branch, runs tests with limited repair retries, and opens a PR through a dedicated bot account.
- A dashboard (FastAPI + React + MySQL) for repository registration, configuration, monitoring and historical analytics.
- Human-in-the-loop throughout: no auto-merge, protected `main`, and safe failure on any unexpected condition.

**Non-goals** (from the idea doc's scope section): production deployment automation, AI auto-merge, enterprise infrastructure/IAM, Project Board automation, unlimited retries, distributed agent orchestration.

## 2. Technology stack
| Area | Choice |
|---|---|
| Hub engine | Python 3.12, `ai_hub` package (src layout), Typer CLI, Pydantic v2 |
| LLM | Claude via Amazon Bedrock, using `boto3` `bedrock-runtime` **Converse API** with tool use for structured output. Model ID set by the `BEDROCK_MODEL_ID` input/variable, never hard-coded |
| AWS auth | GitHub OIDC → IAM role (`aws-actions/configure-aws-credentials`). No long-lived AWS keys |
| GitHub API | `httpx`-based thin client (REST + GraphQL where needed) with retry/backoff and rate-limit handling |
| Static analysis | Ruff (Python), ESLint (JS/TS, if the repo has a config), Semgrep (multi-language) |
| Security analysis | Semgrep security rulesets, Bandit (Python), Gitleaks (secrets), pip-audit / `npm audit` (dependencies) |
| Coverage | Cobertura XML / `coverage.xml` and LCOV parsers |
| Dashboard backend | FastAPI, SQLAlchemy 2.x, Alembic, PyMySQL, Pydantic v2, Authlib (GitHub OAuth) |
| Database | **MySQL 8** (utf8mb4, InnoDB, JSON columns) |
| Dashboard frontend | React 18 + Vite + TypeScript, Tailwind CSS, TanStack Query, React Router, Recharts |
| Tooling | uv (Python deps), pytest, respx, botocore Stubber, mypy, Ruff; pnpm, Vitest, Playwright; Docker Compose for local dev |

## 3. Repository layout (monorepo = the Central AI Hub)
```text
devpilot/
├── .github/workflows/
│   ├── ai-review.yml          # reusable (workflow_call): review + analysis + gate + report
│   ├── devpilot.yml           # reusable (workflow_call): DevPilot agent
│   ├── hub-ci.yml             # CI for this repo (lint, type-check, tests, frontend build)
│   └── release.yml            # tags vX.Y.Z and moves the major tag (v1)
├── hub/                       # Python package `ai_hub`
│   ├── pyproject.toml
│   ├── src/ai_hub/
│   │   ├── cli.py             # `ai-hub review | gate | report | devpilot | validate-config`
│   │   ├── models.py          # Finding, Severity, CheckResult, RunContext, ExecutionStatus
│   │   ├── errors.py          # typed failure taxonomy -> safe-stop handling
│   │   ├── config/            # schema.py (pydantic), loader.py, defaults.py
│   │   ├── github/            # client.py, checks.py, comments.py, prs.py, branches.py, issues.py
│   │   ├── llm/               # bedrock.py, schemas.py (tool JSON schemas), prompts/*.md
│   │   ├── analysis/
│   │   │   ├── diff.py        # PR diff fetch, hunk parsing, changed-line maps
│   │   │   ├── static/        # ruff.py, eslint.py, semgrep.py (adapter interface)
│   │   │   ├── security/      # semgrep_sec.py, bandit.py, gitleaks.py, deps.py
│   │   │   └── coverage.py
│   │   ├── review/            # ai_review.py, chunker.py, context.py
│   │   ├── scoring/engine.py
│   │   ├── gate/evaluator.py
│   │   ├── report/            # markdown.py (sticky comment), inline.py, sarif.py, checkrun.py
│   │   ├── devpilot/          # trigger.py, lock.py, workspace.py, project_detect.py,
│   │   │                      # tools.py, agent.py, tester.py, guardrails.py, git_ops.py, pr.py
│   │   ├── safety/            # redact.py, secrets_scan.py, untrusted.py, policy.py
│   │   └── telemetry/dashboard_client.py
│   └── tests/                 # unit + fixture repos + recorded API responses
├── dashboard/
│   ├── backend/app/           # main.py, api/, models/, schemas/, services/, auth/, webhooks/
│   ├── backend/alembic/
│   ├── frontend/src/          # pages/, components/, api/, hooks/
│   └── docker-compose.yml     # mysql:8, backend, frontend
├── templates/target-repo/
│   ├── .github/workflows/ai-review.yml   # ~15-line caller
│   ├── .github/workflows/devpilot.yml    # ~15-line caller
│   └── .ai-review/config.yml
├── docs/                      # setup.md, configuration.md, security.md, bot-account.md, architecture.md
└── plan.md
```

## 4. Architecture and data flow

### 4.1 PR review pipeline (`ai-review.yml`)
Trigger in the target repo is `pull_request: [opened, synchronize, reopened, ready_for_review]`, calling `uses: Mohanraj1232/devpilot/.github/workflows/ai-review.yml@v1`.

Jobs:
1. **prepare**: check out the target at the PR head SHA and the hub at the pinned ref (`.ai-hub/`). Install `ai_hub`. Load and validate `.ai-review/config.yml`, merged with dashboard policy if reachable. Compute `config_version` = hash of the effective config. Emit the effective config as a job output and artifact.
2. **static** / **security** / **tests** run in parallel. Each runs its tool adapters and writes normalized `findings-<tool>.json` plus `check-<tool>.json` (status: `success | failed | error | skipped`). A tool crash yields status `error`, never an empty "clean" result.
3. **ai-review**: fetch the diff, chunk it per file/hunk within a token budget (largest-risk files first, generated/vendor files skipped), add surrounding context lines, and call Bedrock with a `report_findings` tool schema. Validate the response with Pydantic. On malformed output, re-ask once with the validation error, then mark `error`. Findings must anchor to changed lines. Off-diff findings go to the summary only.
4. **score-gate-report**: merge findings and dedupe by fingerprint `(tool, rule, file, line, normalized message)`. When sources conflict, keep all of them and mark the item `conflict`. Compute the scores and evaluate the gate. Upsert one sticky PR comment (marker `<!-- ai-hub:summary -->`), post inline review comments for findings at or above `report.inline_min_severity` (capped, deduped against earlier runs), create the Check Run `AI Hub / Quality Gate` with conclusion `success|failure`, optionally upload SARIF, and POST the results to the dashboard (best-effort with retries; the full JSON is always kept as a workflow artifact).

Fork PRs: secrets are unavailable, so AI review is skipped with a clear "skipped: fork PR" status. Never use `pull_request_target` with an untrusted checkout.

### 4.2 DevPilot pipeline (`devpilot.yml`)
Trigger in the target repo is `issues: [labeled]` with `if: github.event.label.name == 'devpilot'`. Concurrency is `group: devpilot-${{ github.repository }}-${{ github.event.issue.number }}` with `cancel-in-progress: false`.

Stages (each one can safe-stop; see §9):
1. **Guard** (`trigger.py`): label is `devpilot`; issue is open; repo is registered and DevPilot is enabled (dashboard policy, **fail closed** if the dashboard is unreachable); the bot is a collaborator with `push`; no active execution exists (dashboard lock plus a `devpilot:in-progress` label); no open DevPilot PR already exists for this issue; the issue has enough detail (LLM triage call returning `{actionable, missing_info[], already_resolved_hint}`).
2. **Snapshot**: record the issue `updated_at` and a hash of title and body, the base branch HEAD SHA, and `config_version`.
3. **Workspace**: clone with the bot token, check out the base branch, init submodules (report and stop if that fails), detect the project type (`project_detect.py`: pyproject/requirements, package.json, go.mod, pom.xml...). Unsupported or empty repo → report and stop.
4. **Plan**: Claude receives the issue (wrapped as untrusted data), a repo map (tree + key files, size-limited, relevant files ranked by keyword/path search), and returns a structured plan (files to touch, approach, test strategy).
5. **Implement loop** (`agent.py`): a tool-use loop with a fixed toolset: `list_dir`, `read_file`, `search_code`, `write_file`, `apply_edit`, `run_tests`, `finish`. There is **no arbitrary shell**. All paths are sandboxed to the workspace, and denied paths are `.git/`, `.github/workflows/` (unless policy allows), vendor/generated globs and lockfiles (unless the plan requires them). Budgets: max tool calls, max tokens, max wall time.
6. **Test and repair**: `run_tests` runs only the configured or detected command with a timeout. On failure, the failure summary (redacted) goes back to Claude for a fix. Limit is `devpilot.max_fix_attempts` (default 3). Stop early if the diff hash repeats (the same failed fix). Flaky test detection: on pass-after-fail without code change, or fail-after-pass, rerun up to 2 times and report the instability.
7. **Guardrails** (`guardrails.py`): diff size limits (files and lines), unrelated-path detection versus the plan, secret scan of the diff (Gitleaks), forbidden-change checks (workflows, CODEOWNERS, branch-protection/security config, disabling checks). Any violation means stop and report, with no push.
8. **Pre-push revalidation**: re-fetch the issue (state, labels, body hash), the base HEAD, and the branch state. Issue edited, closed or deleted → stop and comment. Base moved → rebase locally, and on conflict stop and report. Never force-push over commits not authored by this execution.
9. **Commit, push and PR**: branch `devpilot/issue-<n>-<slug>`, with a `-<short-exec-id>` suffix on collision with a foreign branch. Commits carry the trailer `DevPilot-Execution: <id>`. The PR body contains the summary, plan, test evidence (or "automated verification unavailable"), limitations, and `Closes #<n>`. Reuse the existing PR if one exists for this branch. Because the PR is authored by the bot PAT, the `ai-review` workflow triggers normally (GITHUB_TOKEN-created PRs would not trigger workflows).
10. **Report**: issue comment with the outcome, remove the in-progress label, POST the execution record to the dashboard.

### 4.3 Dashboard
- **Management**: GitHub OAuth login. Register a repo after verifying the user has admin rights on it via the API, checking that the bot is a collaborator, that the caller workflow files exist, and that `main` is protected and requires the `AI Hub / Quality Gate` check. Toggles for review and DevPilot; a quality-rule editor; review settings.
- **Monitoring**: data comes from workflow ingest calls plus a GitHub webhook receiver (PR closed/merged, branch deleted, issue closed/edited, label removed) for status reconciliation.
- **Not in the critical path**: AI review runs with repo-level config if the dashboard is down. DevPilot fails closed because it writes code.

## 5. Repository configuration (`.ai-review/config.yml`)
Pydantic schema with strict validation (unknown keys and out-of-range values are errors). An invalid config gives the gate result **ERROR (fail)** with a message; the system never silently falls back to defaults.
```yaml
version: 1
review_mode: standard            # light | standard | strict
paths:
  ignore: ["dist/**", "vendor/**", "**/*.min.js"]
  generated: ["**/*_pb2.py"]
ai_review:
  enabled: true
  max_files: 50
  inline_min_severity: medium
static_analysis:
  enabled: true
  tools: [ruff, semgrep]
security:
  enabled: true
  tools: [semgrep, gitleaks, deps]
tests:
  command: "pytest --cov --cov-report=xml"
  coverage_report: coverage.xml
  timeout_minutes: 15
quality_gate:
  enabled: true
  coverage_threshold: 80         # 0-100
  fail_on: { critical: 1, high: 1 }
  require_tests_pass: true
  required_checks: [static, security, ai_review]   # an 'error' status fails the gate
devpilot:
  enabled: true
  base_branch: main
  test_command: "pytest"
  max_fix_attempts: 3
  max_changed_files: 20
  max_changed_lines: 800
  allow_workflow_changes: false
```
Effective config = hub defaults ← repo file ← dashboard policy (the dashboard can only tighten the gate, never loosen it). The workflow logs and records its `config_version`.

## 6. Core domain model (`ai_hub/models.py`)
- `Severity`: info, low, medium, high, critical.
- `Finding`: id, source (`ai|static|security|coverage`), tool, rule_id, category (bug, security, performance, maintainability, style, test), severity, file, line_start/line_end, title, explanation, suggested_fix (optional patch/text), confidence, fingerprint, status (`new|persisting|conflict`).
- `CheckResult`: name, status (`success|failed|error|skipped`), summary, duration, details.
- `ExecutionStatus` (DevPilot): `queued, running, needs_clarification, blocked, tests_failed, failed, pr_opened, pr_updated, pr_merged, pr_closed, cancelled`, plus a `failure_reason` code from `errors.py`.

## 7. Scoring and quality gate

**Risk score (0-100, higher is riskier)** = min(100, Σ severity weights over findings on changed lines (critical 40, high 20, medium 8, low 2) + size factor (changed lines / 50, cap 15) + sensitive-path factor (auth/crypto/infra/migrations: +10) + coverage factor (below threshold: +10; unknown: +5)).

**Quality score (0-100)** = 100 − weighted penalties (static/maintainability findings, coverage gap, test failures). Both formulas live in `scoring/engine.py` with documented weights. If any required input has status `error`, the score is reported as "unavailable" instead of a number.

**Gate (`gate/evaluator.py`)**, evaluated in order. Every rule produces a line in the report.
1. Config invalid → FAIL (config error).
2. Any required check `error` or missing → FAIL ("check unavailable").
3. Critical security finding ≥ `fail_on.critical` → FAIL.
4. High-severity count ≥ `fail_on.high` → FAIL.
5. `require_tests_pass` and tests failed or could not run → FAIL.
6. Coverage < threshold → FAIL. Coverage not measurable → FAIL if the threshold is set, with the "coverage unavailable" reason.
7. Otherwise PASS. PASS never merges anything. Merging requires human approval through branch protection.

## 8. Database schema (MySQL 8, Alembic-managed)
| Table | Key columns |
|---|---|
| `users` | id, github_id (unique), login, avatar_url, created_at |
| `repositories` | id, github_repo_id (unique), owner, name, full_name (unique), registered_by, review_enabled, devpilot_enabled, status (`active|disabled|removed|inaccessible`), created_at, removed_at |
| `repo_policies` | id, repo_id, version (unique per repo), policy_json (JSON), created_by, created_at. Append-only versioning |
| `ingest_tokens` | id, repo_id, token_hash (SHA-256), last_used_at, revoked_at |
| `review_runs` | id, repo_id, pr_number, head_sha, workflow_run_id (unique), config_version, status, risk_score, quality_score, gate_result, gate_reasons JSON, started_at, finished_at |
| `check_results` | id, review_run_id, name, status, summary, duration_ms |
| `findings` | id, review_run_id, source, tool, rule_id, category, severity, file, line_start, line_end, title, explanation, suggested_fix, fingerprint (indexed), resolution (`open|accepted|dismissed|fixed`) |
| `devpilot_executions` | id, repo_id, issue_number, issue_hash, base_sha, branch, pr_number, workflow_run_id (unique), config_version, status, attempts, test_status, failure_reason, started_at, finished_at. Partial-unique "active" lock enforced via a `lock_key` column (unique, nulled on finish) |
| `devpilot_attempts` | id, execution_id, attempt_no, diff_hash, test_status, summary |
| `webhook_deliveries` | delivery_id (PK, idempotency), event, received_at, processed |
| `audit_log` | id, actor, action, target, details JSON, created_at |

Idempotency: ingest uses `workflow_run_id` as an upsert key, and webhooks dedupe on `X-GitHub-Delivery`. Removing a repo is a soft delete (`status=removed`), so history is kept and future runs are rejected by the policy endpoint.

## 9. Edge-case coverage map (idea doc §12)
| Group | Mechanisms |
|---|---|
| A. Issue triggers | label filter in the caller `if:`. Workflow concurrency group plus dashboard `lock_key` and an in-progress label stop duplicates. LLM triage → `needs_clarification` comment. Issue re-fetch at start and before push (closed, deleted, edited → stop; the hash compare detects edits). Label removed mid-run → finish safely and do not restart. Existing open DevPilot PR → update it instead of creating a new one. `already_resolved_hint` → stop and comment |
| B. Repository | dashboard policy check (not registered or disabled → no run). API 404/403 → `inaccessible` and report. Empty repo or unsupported type detection. Relevance-ranked repo map with file/size caps for large repos. `paths.generated` and vendor deny-list. Submodule init failure → stop |
| C. Git/branch | feature branches only. Execution trailer marks branch ownership. Unique-suffix branch names. Pre-push re-check of base and branch. Rebase, and on conflict stop. Never force-push. Clean-tree check before writing. Push/commit/PR failures recorded with state kept as artifacts. No diff → no PR, status `failed: no_changes` |
| D. AI/Claude | `bedrock.py`: timeouts, bounded exponential retry for throttling/5xx only, typed errors (`ModelUnavailable`, `AuthFailure`, `InvalidResponse`). Pydantic validation of every tool call. Unknown tool → error back to the model, then stop after N. Diff-hash repetition detection. Budget exhaustion → `failed: cannot_solve` |
| E. Tests/build | detect or configure the test command. Missing → PR states "verification unavailable". Dependency install step with a distinct failure code. Per-run timeout. Flaky detection via rerun. Coverage parser returns `unavailable`, not 0 |
| F. Permissions | bot collaborator/permission check via the API before cloning. Token presence check per step. AWS OIDC failure → stop before any LLM call. 401/403 mid-run → stop safely |
| G. Security | Gitleaks on the generated diff before commit. `redact.py` masks secrets in every log, comment and prompt. Issue and repo content wrapped in `<untrusted>` blocks with a system prompt rule that it is data, not instructions. The tool layer enforces policy no matter what the model asks: no network tool, no shell, no workflow/CODEOWNERS/protection edits, workspace-only paths. Destructive operations (file deletions over N, mass rewrites) need `policy.allow_destructive`. A scanner failure becomes check status `error`, which fails the gate |
| H. Pull requests | gate and branch protection block merges, and the AI never merges. Webhooks reconcile closed/merged/branch-deleted. DevPilot never pushes to a PR branch that has foreign commits. A new commit retriggers `ai-review` via `synchronize` |
| I. Quality analysis | per-tool `error` status. Conflicts kept and flagged. Score "unavailable". Strict config validation with a safe FAIL. A required check that is missing fails the gate |
| J. Dashboard | transactional registration with unique constraints. Config validated server-side with the same Pydantic schema (shared `ai_hub.config.schema`). Each run pins `config_version`. Ingest is best-effort with retry, plus a workflow artifact for backfill. Soft-delete on removal |
| K. General | `errors.py` → one `safe_stop()` path: preserve state (artifact upload), redact, report to the issue/PR and the dashboard, exit non-zero, never merge |

## 10. Security model implementation
- **Bot account** `devpilot-bot`: fine-grained PAT scoped to registered repos with Contents R/W, Pull requests R/W, Issues R/W and Metadata R. No admin and no workflow scope. Stored as the org/repo secret `DEVPILOT_BOT_TOKEN`, passed explicitly to the reusable workflow (not `secrets: inherit`).
- **Workflow permissions**: least privilege per job (`contents: read`, `pull-requests: write`, `checks: write`, `id-token: write` only where needed).
- **AWS**: IAM role trusted only for `repo:<owner>/<repo>:*` subjects of registered repos, with a policy allowing only `bedrock:InvokeModel*`/`Converse*` on the chosen model ARN.
- **Branch protection on `main`**: require PRs, at least 1 approval, the required check `AI Hub / Quality Gate`, and no bypass for the bot. Docs include a checklist, and the dashboard verifies these settings at registration.
- **Dashboard**: GitHub OAuth (state + PKCE), httpOnly secure session cookie, CSRF protection on mutations, per-repo ingest tokens (hashed at rest), webhook HMAC (`X-Hub-Signature-256`) verification. No AWS or bot credentials in MySQL.
- **Prompt-injection posture**: content is treated as untrusted, the capabilities are enforced by the tool layer, and outputs are always human-reviewed.

## 11. Dashboard API (FastAPI, `/api/v1`)
- Auth: `GET /auth/login`, `GET /auth/callback`, `POST /auth/logout`, `GET /me`
- Repos: `GET/POST /repos`, `GET/PATCH/DELETE /repos/{id}`, `POST /repos/{id}/verify` (bot, workflows, protection), `POST /repos/{id}/tokens` (rotate ingest token)
- Policy: `GET /repos/{id}/policy`, `PUT /repos/{id}/policy` (validated, new version), `GET /policy/{owner}/{repo}` (workflow-facing, ingest-token auth)
- Ingest (workflow auth): `POST /ingest/review-runs`, `POST /ingest/devpilot-executions`, `PATCH /ingest/devpilot-executions/{id}`, `POST /locks/devpilot` / `DELETE /locks/devpilot/{key}`
- Monitoring: `GET /repos/{id}/review-runs`, `GET /review-runs/{id}` (with findings and checks), `PATCH /findings/{id}` (resolution), `GET /repos/{id}/devpilot-executions`, `GET /devpilot-executions/{id}`
- Analytics: `GET /analytics/findings-over-time`, `/analytics/categories`, `/analytics/gate-failures`, `/analytics/devpilot-success`, `/analytics/repo-trends`
- Webhooks: `POST /webhooks/github`

**Frontend pages**: Login, Repositories (list + register wizard with verification checklist), Repo detail (settings toggles, quality rules editor with live validation, policy version history), Review runs (list → detail with findings table, filters, check statuses, gate reasons), DevPilot executions (timeline of stages, attempts, failure reason, PR link), Analytics (Recharts line/bar charts).

## 12. Implementation milestones
| # | Milestone | Deliverables | Exit criteria |
|---|---|---|---|
| M0 | Foundations | monorepo skeleton, `hub-ci.yml`, `ai_hub` package, models, `errors.py`, config schema + `validate-config` CLI, redaction utility | CI green; config schema unit-tested with valid/invalid fixtures |
| M1 | Analysis adapters | diff parser, Ruff/ESLint/Semgrep/Bandit/Gitleaks/deps adapters, coverage parsers, normalized findings | adapters tested against fixture repos with known issues; tool crash → `error` |
| M2 | AI review | Bedrock client (Converse + tool schema), chunker, prompts, validation/retry | Stubber-based tests for success, throttle, malformed, timeout paths; manual run on a sample PR |
| M3 | Score, gate, report + `ai-review.yml` | scoring engine, gate evaluator, sticky comment, inline comments, check run, SARIF, artifact output, template caller workflow | E2E on a sandbox target repo: seeded-vuln PR → gate FAIL; clean PR → PASS; invalid config → FAIL |
| M4 | DevPilot core | trigger guard, snapshot, workspace, project detect, agent tool loop, test runner, retry loop, git ops, PR creation, `devpilot.yml` + template | labeling a simple issue in the sandbox produces a bot PR that passes through ai-review |
| M5 | DevPilot hardening | guardrails, pre-push revalidation, flaky detection, branch ownership, duplicate/lock handling, all §9 A–G paths | scenario test matrix (below) passes |
| M6 | Dashboard backend | MySQL compose, SQLAlchemy models, Alembic migrations, OAuth, repo registration + verification, policy versioning, ingest, locks, webhooks, analytics queries | pytest API suite against MySQL (testcontainers) green |
| M7 | Dashboard frontend | all pages above, API client, charts | Playwright smoke: login (mocked), register, view run, view execution |
| M8 | Integration and release | hub ↔ dashboard wiring, docs (setup, bot account, AWS OIDC, branch protection), `v1` tag, demo script | fresh target repo onboarded from docs alone; full demo flow works |

## 13. Testing strategy
- **Unit** (pytest): config validation, diff parsing, every adapter parser, scoring math, gate rule ordering, redaction, guardrails, path sandbox, branch naming, dedupe/conflict logic.
- **LLM paths**: `botocore.stub.Stubber` / fake Bedrock client with canned Converse responses (valid, malformed, tool-loop, throttling, timeout). No live model calls in CI.
- **GitHub paths**: `respx` mocks with recorded fixtures (403, 404, 409 conflict, 422 existing PR, rate limit).
- **DevPilot scenario matrix**: fixture repos (python-pytest, node-jest, empty, unsupported, no-tests, flaky-test, submodule-missing) run through the agent with a scripted fake model, asserting the final `ExecutionStatus` and `failure_reason` for each §12 edge case.
- **Dashboard**: pytest + httpx AsyncClient against MySQL in testcontainers; Vitest for components; Playwright smoke.
- **Live E2E** (manual/nightly, opt-in): sandbox repo `devpilot-sandbox` with real Bedrock and the bot account.

## 14. End-to-end verification (demo script)
1. `docker compose up` the dashboard. Log in with GitHub and register `devpilot-sandbox`. Verification shows bot ✓, workflows ✓, protection ✓.
2. Open a PR adding a hardcoded secret and a SQL-injection pattern → sticky comment with findings, Check Run FAIL, dashboard shows the run with findings.
3. Fix the PR → re-run (synchronize) → PASS. Merge still requires a human approval.
4. Create the issue "Add `/health` endpoint" with label `devpilot` → bot branch `devpilot/issue-N-add-health-endpoint`, tests run, PR opened with test evidence → ai-review runs on it → a human approves and merges → the webhook marks the execution `pr_merged`.
5. Negative checks: re-add the label during a run (no duplicate), edit the issue mid-run (stop + comment), vague issue (`needs_clarification`), disable DevPilot in the dashboard (no run).

## 15. Open items to finalize during implementation
- Exact Bedrock model ID and region (configured via variables).
- Whether the dashboard is deployed (e.g. a single VM/container host) or local-only for the demo.
- Language coverage beyond Python/JS for v1 (adapter interface allows adding more).
