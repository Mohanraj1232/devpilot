# Setting up DevPilot

This guide covers everything that has to be done by a person (accounts, secrets, cloud
permissions, branch protection). The code expects exactly the names used below.

> **Status.** The hub, the dashboard and both workflows are covered by automated tests and
> `actionlint`, but they have **not yet been run against real GitHub Actions, real Amazon
> Bedrock, or a real MySQL server**. Do the first run on a throw-away sandbox repository
> (see [First run](#7-first-run-on-a-sandbox-repository)).

## What you are setting up

| Piece | What it does | Where it runs |
|---|---|---|
| **Central AI Hub** (this repo) | Reusable workflows `ai-review.yml` and `devpilot.yml`, plus the `ai-hub` CLI | GitHub Actions |
| **PR review** | Static + security analysis, tests/coverage, AI review, quality gate, PR report | `pull_request` in each target repo |
| **DevPilot** | Claude (Bedrock) implements a labelled issue and opens a PR as a bot | `issues: labeled` in each target repo |
| **Dashboard** | Registration, settings, monitoring (FastAPI + React + MySQL) | Your server / Docker |

Nothing is ever merged automatically. Branch protection plus a human approval is the last gate.

---

## 1. Publish the hub and cut a release

Target repositories call the hub's workflows pinned to `@v1`, so that tag must exist.

```bash
git tag v1.0.0
git push origin v1.0.0
```

`release.yml` re-runs the checks, moves the `v1` tag to the same commit and creates a GitHub
release. Until `v1` exists, every call to `…/ai-review.yml@v1` or `…/devpilot.yml@v1` fails at
checkout. The hub repository must be **public** (or you must change the checkout steps to use
a token that can read it).

## 2. The DevPilot bot account

DevPilot acts on GitHub as a dedicated user so its actions are attributable and its
permissions are minimal. Claude is the "brain"; this account is the identity.

1. Create a GitHub user (for example `devpilot-bot`).
2. For each target repository, invite it as a **collaborator with Write** access.
3. As the bot, create a **fine-grained personal access token**:
   - Repository access: *Only select repositories* (the targets).
   - Repository permissions: **Contents: Read and write**, **Pull requests: Read and write**,
     **Issues: Read and write**, Metadata: Read-only (automatic).
   - Do **not** grant Workflows or Administration.
   - Set an expiry and put a reminder in your calendar to rotate it.
4. Store it in each target repo as the Actions secret **`DEVPILOT_BOT_TOKEN`**.

The bot cannot push to `main` once branch protection is on (section 6). Because PRs are
authored with a PAT (not `GITHUB_TOKEN`), they trigger the AI-review workflow normally.

## 3. AWS: Amazon Bedrock access without stored keys

The workflows authenticate with GitHub OIDC; no AWS keys are stored anywhere.

1. In **Bedrock → Model access**, enable the Claude model you intend to use.
2. In **IAM → Identity providers**, add an OpenID Connect provider:
   URL `https://token.actions.githubusercontent.com`, audience `sts.amazonaws.com`.
3. Create an IAM role with this **trust policy** (replace `ACCOUNT_ID`, `OWNER`, `REPO`;
   add one `repo:` entry per target repository):

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Principal": {"Federated": "arn:aws:iam::ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com"},
       "Action": "sts:AssumeRoleWithWebIdentity",
       "Condition": {
         "StringEquals": {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"},
         "StringLike": {"token.actions.githubusercontent.com:sub": ["repo:OWNER/REPO:*"]}
       }
     }]
   }
   ```

4. Attach a **permissions policy** that allows only invoking your model:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Action": ["bedrock:InvokeModel"],
       "Resource": ["arn:aws:bedrock:REGION::foundation-model/MODEL_ID"]
     }]
   }
   ```

   If you use a cross-region *inference profile*, also allow
   `arn:aws:bedrock:REGION:ACCOUNT_ID:inference-profile/PROFILE_ID` and the foundation-model
   ARN in each region the profile can route to. The Converse API needs `bedrock:InvokeModel`.
5. In each target repo set these Actions **variables** (not secrets):

   | Variable | Value |
   |---|---|
   | `AWS_ROLE_ARN` | ARN of the role above |
   | `AWS_REGION` | e.g. `us-east-1` |
   | `BEDROCK_MODEL_ID` | The model or inference-profile ID exactly as shown in the Bedrock console |

## 4. The dashboard

```bash
cd dashboard
export DEVPILOT_SECRET_KEY=$(openssl rand -hex 32)   # required; the app refuses the placeholder
export DEVPILOT_GITHUB_CLIENT_ID=...                 # from step 2 below
export DEVPILOT_GITHUB_CLIENT_SECRET=...
export DEVPILOT_BOT_LOGIN=devpilot-bot
export DEVPILOT_WEBHOOK_SECRET=$(openssl rand -hex 32)
docker compose up --build
```

Compose runs `alembic upgrade head` before starting the API (backend on `:8000`, UI on `:5173`).
Change the MySQL passwords in `docker-compose.yml` before exposing it anywhere.

1. **MySQL** is created by compose. The initial migration has been verified on SQLite only;
   check the first start against MySQL.
2. **GitHub OAuth App** (Settings → Developer settings → OAuth Apps): callback URL
   `http://localhost:8000/api/v1/auth/callback` (or your public URL; also set
   `DEVPILOT_GITHUB_REDIRECT_URI`). Its client ID/secret go in the variables above.
3. **Webhook** on each target repo: payload URL `https://YOUR_DASHBOARD/api/v1/webhooks/github`,
   content type `application/json`, secret = `DEVPILOT_WEBHOOK_SECRET`, events *Pull requests*
   and *Issues*. Without the secret the endpoint refuses everything (by design).
4. Put the dashboard behind HTTPS. The login cookie is marked `Secure` and `SameSite=Lax`; for local
   development over plain HTTP set `DEVPILOT_ENVIRONMENT=development` (which also allows the placeholder
   secret key). The user's GitHub token lives only in that signed cookie, never in the database.

### Registering a repository

Log in, register the repository, then:

1. **Verify** — checks, live against GitHub, that the bot has Write access, both workflow files
   exist, and the default branch is protected as required (section 6). It tells you what to fix.
2. **Enable DevPilot** — it is *off by default*; the review pipeline is on.
3. **Issue a token** — shown once; revokes the previous one. Store it in the target repo as
   **`DASHBOARD_TOKEN`**, and the dashboard's base URL as **`DASHBOARD_URL`**.

You need **admin rights on the GitHub repository** to register or manage it, and you only see
repositories you registered. DevPilot refuses to run for unregistered or disabled repositories and
**fails closed if the dashboard is unreachable**. (PR review keeps working without the dashboard.)

## 5. Onboard a target repository

Copy from [`templates/target-repo/`](../templates/target-repo):

```
.github/workflows/ai-review.yml   # pull_request  -> the review pipeline
.github/workflows/devpilot.yml    # issues:labeled -> DevPilot
.ai-review/config.yml             # per-repository rules
```

Then create the secrets/variables from sections 2–4 and add the `devpilot` label to the repository.

`.ai-review/config.yml` is validated strictly: unknown keys, unknown tool names, invalid
severities and out-of-range values are errors, never silently ignored. Important behaviours:

- **The gate configuration is read from the base branch, not from the pull request.** A PR that
  edits this file cannot weaken its own gate; the report says so, and the change applies once merged.
- `quality_gate.coverage_threshold` defaults to **80**. A repository that does not measure
  coverage will therefore fail the gate ("coverage unavailable"); set it to `0` or configure
  `tests.coverage_report`.
- `quality_gate.required_checks` may contain `static`, `security`, `tests`, `ai_review`. A required
  check that errors or is missing fails the gate. `tests` is added automatically when
  `require_tests_pass` is true.
- Findings only count when they are on lines the PR changed.

## 6. Protect `main`

Branch protection (or a ruleset) on the default branch is what actually stops the bot, and the AI,
from changing `main`:

- Require a pull request before merging, with **at least 1 approving review**.
- Require the status check **`AI Hub / Quality Gate`**.
- Block force pushes and branch deletion.
- Do **not** add the bot to any bypass list; do not give it admin.
- Recommended: dismiss stale approvals when new commits are pushed.

(The dashboard's **Verify** button checks the first four.)

## 7. First run on a sandbox repository

1. **Review:** open a PR that adds a Python file with `import os` (unused). Expect the sticky
   comment, an inline comment, the `AI Hub / Quality Gate` check **failing**, and an entry in the dashboard.
2. Fix it; push; expect the gate to pass. Merging still needs a human approval.
3. **DevPilot:** create an issue with a concrete description and add the `devpilot` label. Expect a
   "started" comment, a branch `devpilot/issue-N-…`, a PR from the bot that goes through step 1,
   and a final comment on the issue.
4. **Negative checks:** edit the issue while it runs (the run stops and says so); add the label to an
   issue that already has a DevPilot PR (nothing happens); disable DevPilot in the dashboard (no run);
   label a vague issue (it asks for clarification).

You can also run pieces locally. For DevPilot against a sandbox repo, without a dashboard:

```bash
pip install -e hub
export DEVPILOT_BOT_TOKEN=... BEDROCK_MODEL_ID=... AWS_REGION=...   # AWS credentials as usual
ai-hub devpilot --repo OWNER/REPO --issue 1 --workspace ./checkout --standalone
```

`--standalone` skips the dashboard registration checks and is for sandboxes only.

## Day-to-day operation

- **A DevPilot run stuck?** A crashed runner can leave the `devpilot:in-progress` label on the issue,
  which blocks new runs. Remove the label and re-apply `devpilot`.
- **Re-running DevPilot** after it asked for clarification: update the issue, then re-apply the
  `devpilot` label (it removes the label when it asks for more detail).
- **Artifacts** (`devpilot-issue-N`, `review-outcome`) hold the result, a redacted agent transcript
  and the diff, kept 14 days.
- **Rotate secrets:** the bot PAT (expiry), the dashboard ingest token (dashboard button), the webhook
  secret, and the OAuth client secret.

## Security model and known limits

- **Human approval is required.** DevPilot only opens pull requests; it never merges, and cannot
  push to a protected `main`.
- **Untrusted input.** Issue text, repository content, test output and PR diffs are passed to the
  model as delimited *data*; the model is told not to follow instructions in them. Its tools are a
  fixed set (no shell, no network) confined to the workspace, and guardrails reject workflow,
  CODEOWNERS, security-config and credential-file changes, oversized diffs, and secrets.
- **Code from the PR or the AI is executed** by the test and dependency steps. That code runs with a
  scrubbed environment (no tokens or cloud credentials) but **with normal network access** from the
  runner, and secrets could still be exfiltrated if they were placed in files. Keep secrets out of repositories.
- **Diffs leave GitHub.** The AI review sends PR diffs to Amazon Bedrock in your AWS account.
- **Forks and Dependabot** cannot receive AWS credentials: AI review is recorded as *skipped* for them,
  and results go to the job summary because their token is read-only.
- **The dashboard** authenticates workflows with a per-repository token and users via GitHub OAuth.
  Webhooks are verified with an HMAC secret. Every route that exposes or changes data requires a login
  or a token and is scoped to the caller's repositories. Not implemented: rate limiting and CSRF tokens
  (the session cookie is `SameSite=Lax` instead), so put the dashboard behind HTTPS and, ideally, a
  trusted network or an authenticating proxy.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Workflow fails at "Check out the Central AI Hub" | The `v1` tag does not exist, or the hub repo is private |
| `Could not assume role` | Trust policy `sub` does not match `repo:OWNER/REPO:*`, or the OIDC provider is missing |
| AI review `error: AI service unavailable` | Model access not enabled, wrong `BEDROCK_MODEL_ID`/region, or the role lacks `bedrock:InvokeModel` |
| Gate fails with "check 'x' is missing" | That job crashed before uploading its result; open its log |
| Gate fails with "Coverage data unavailable" | Set `coverage_threshold: 0` or configure `tests.coverage_report` |
| DevPilot says "Dashboard unreachable" | `DASHBOARD_URL`/`DASHBOARD_TOKEN` wrong, token revoked, or dashboard down (DevPilot fails closed) |
| DevPilot says "Bot is not a collaborator" | Invite the bot with Write access; the PAT must belong to that user |
| Dashboard won't start | `DEVPILOT_SECRET_KEY` unset (or the placeholder) outside `DEVPILOT_ENVIRONMENT=development` |
| Webhook returns 503 / 401 | `DEVPILOT_WEBHOOK_SECRET` unset on the dashboard / does not match the webhook's secret |
