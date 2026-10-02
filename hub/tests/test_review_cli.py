"""End-to-end: the review CLI commands run in the order the workflow runs them.

Real git merge commit, real ruff, a fake model, and mocked GitHub/dashboard HTTP.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from ai_hub.cli import app
from ai_hub.llm.schemas import AIFinding, ReviewFindings
from tests.review_helpers import make_pr_repo

runner = CliRunner()
GH = "https://api.github.test"
DASH = "http://dash.test"

CONFIG = """\
static_analysis:
  tools: [ruff]
security:
  enabled: false
ai_review:
  inline_min_severity: low
tests:
  command: "{py} -c pass"
quality_gate:
  coverage_threshold: 0
  required_checks: [static, security, ai_review]
"""


@pytest.fixture(autouse=True)
def tools_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """ruff lives next to the interpreter when running from a virtualenv."""
    scripts = str(Path(sys.executable).parent)
    monkeypatch.setenv("PATH", scripts + os.pathsep + os.environ.get("PATH", ""))


class FakeBedrock:
    findings: list[AIFinding] = []

    def __init__(self, model_id: str, region: str | None = None, **kwargs: Any) -> None:
        self.model_id = model_id

    def converse_with_tool_retry(self, *, messages, system, tool_schema, response_model):  # type: ignore[no-untyped-def]
        assert "<untrusted_diff>" in messages[0]["content"][0]["text"]
        return ReviewFindings(findings=list(FakeBedrock.findings), summary="reviewed")


def make_repo(tmp_path: Path, pr_files: dict[str, str]) -> Path:
    config = CONFIG.format(py=Path(sys.executable).as_posix())
    return make_pr_repo(
        tmp_path,
        base={"src/app.py": "a = 1\n", ".ai-review/config.yml": config},
        pr=pr_files,
    )


def invoke(*args: str, env: dict[str, str] | None = None) -> Any:
    return runner.invoke(app, list(args), env=env or {}, catch_exceptions=False)


def run_stages(repo: Path, tmp_path: Path, *, with_ai: bool = True) -> tuple[Path, Path, Path]:
    inputs, results, outcome = tmp_path / "in", tmp_path / "res", tmp_path / "out"
    assert (
        invoke("review", "prepare", "--workspace", str(repo), "--out", str(inputs)).exit_code == 0
    )
    for stage in ("static", "security", "tests"):
        r = invoke(
            "review",
            stage,
            "--workspace",
            str(repo),
            "--inputs",
            str(inputs),
            "--out",
            str(results),
        )
        assert r.exit_code == 0, r.output
    if with_ai:
        r = invoke(
            "review",
            "ai",
            "--workspace",
            str(repo),
            "--inputs",
            str(inputs),
            "--out",
            str(results),
            env={"BEDROCK_MODEL_ID": "test-model"},
        )
        assert r.exit_code == 0, r.output
    return inputs, results, outcome


def gate(inputs: Path, results: Path, outcome: Path, *extra: str) -> dict[str, Any]:
    r = invoke(
        "gate", "--inputs", str(inputs), "--results", str(results), "--out", str(outcome), *extra
    )
    assert r.exit_code == 0, r.output
    return json.loads((outcome / "outcome.json").read_text())  # type: ignore[no-any-return]


def mock_github() -> dict[str, respx.Route]:
    return {
        "list_comments": respx.get(f"{GH}/repos/o/r/issues/5/comments").mock(
            return_value=httpx.Response(200, json=[])
        ),
        "create_comment": respx.post(f"{GH}/repos/o/r/issues/5/comments").mock(
            return_value=httpx.Response(201, json={"id": 1})
        ),
        "list_review_comments": respx.get(f"{GH}/repos/o/r/pulls/5/comments").mock(
            return_value=httpx.Response(200, json=[])
        ),
        "review": respx.post(f"{GH}/repos/o/r/pulls/5/reviews").mock(
            return_value=httpx.Response(200, json={})
        ),
        "check_run": respx.post(f"{GH}/repos/o/r/check-runs").mock(
            return_value=httpx.Response(201, json={})
        ),
    }


REPORT_ENV = {
    "GITHUB_TOKEN": "tok",
    "GITHUB_API_URL": GH,
    "GITHUB_REPOSITORY": "o/r",
    "GITHUB_RUN_ID": "77",
}


@pytest.mark.parametrize("scenario", ["unused_import"])
class TestEndToEnd:
    @respx.mock
    def test_a_pr_with_a_real_lint_error_fails_the_gate_and_is_reported(
        self, scenario: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("ai_hub.llm.bedrock.BedrockClient", FakeBedrock)
        FakeBedrock.findings = [
            AIFinding(
                category="bug",
                severity="medium",
                file="src/bad.py",
                line_start=1,
                title="Unused import",
                explanation="os is never used",
            )
        ]
        repo = make_repo(tmp_path, {"src/bad.py": "import os\n"})
        inputs, results, outcome_dir = run_stages(repo, tmp_path)
        data = gate(inputs, results, outcome_dir)

        assert data["gate_result"] == "fail"
        files = {(f["tool"], f["file"], f["line_start"]) for f in data["findings"]}
        assert ("ruff", "src/bad.py", 1) in files  # real ruff, repo-relative path
        assert ("ai_review", "src/bad.py", 1) in files
        checks = {c["name"]: c["status"] for c in data["checks"]}
        assert checks == {
            "static": "failed",
            "security": "skipped",
            "tests": "success",
            "ai_review": "failed",
        }
        assert (outcome_dir / "findings.sarif").is_file()

        routes = mock_github()
        dash = respx.post(f"{DASH}/api/v1/ingest/review-runs").mock(
            return_value=httpx.Response(201, json={"id": 1})
        )
        summary = tmp_path / "step_summary.md"
        result = invoke(
            "report", "--outcome", str(outcome_dir / "outcome.json"), "--pr", "5",
            "--head-sha", "headsha1",
            env={
                **REPORT_ENV,
                "GITHUB_STEP_SUMMARY": str(summary),
                "DASHBOARD_URL": DASH,
                "DASHBOARD_TOKEN": "dtok",
            },
        )  # fmt: skip
        assert result.exit_code == 1  # the gate failed, so the job fails

        comment = json.loads(routes["create_comment"].calls[0].request.content)["body"]
        assert "FAIL" in comment and "src/bad.py" in comment
        review = json.loads(routes["review"].calls[0].request.content)
        assert review["commit_id"] == "headsha1"
        assert review["event"] == "COMMENT"  # never approves or requests changes
        assert {c["path"] for c in review["comments"]} == {"src/bad.py"}
        check_run = json.loads(routes["check_run"].calls[0].request.content)
        assert check_run["name"] == "AI Hub / Quality Gate"
        assert check_run["conclusion"] == "failure"
        assert check_run["head_sha"] == "headsha1"
        assert "FAIL" in summary.read_text(encoding="utf-8")

        payload = json.loads(dash.calls[0].request.content)
        assert dash.calls[0].request.headers["authorization"] == "Bearer dtok"
        assert payload["gate_result"] == "FAIL"
        assert payload["workflow_run_id"] == 77
        assert payload["repo_full_name"] == "o/r"
        assert {c["name"] for c in payload["checks"]} == {
            "static",
            "security",
            "tests",
            "ai_review",
        }
        assert any(f["tool"] == "ruff" for f in payload["findings"])

    @respx.mock
    def test_a_clean_pr_passes(
        self, scenario: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("ai_hub.llm.bedrock.BedrockClient", FakeBedrock)
        FakeBedrock.findings = []
        repo = make_repo(tmp_path, {"src/good.py": "def add(a, b):\n    return a + b\n"})
        inputs, results, outcome_dir = run_stages(repo, tmp_path)
        assert gate(inputs, results, outcome_dir)["gate_result"] == "pass"

        routes = mock_github()
        result = invoke(
            "report", "--outcome", str(outcome_dir / "outcome.json"), "--pr", "5",
            "--head-sha", "h", env=REPORT_ENV,
        )  # fmt: skip
        assert result.exit_code == 0
        assert json.loads(routes["check_run"].calls[0].request.content)["conclusion"] == "success"
        assert not routes["review"].called  # nothing to say inline

    def test_missing_model_configuration_fails_the_gate(
        self, scenario: str, tmp_path: Path
    ) -> None:
        repo = make_repo(tmp_path, {"src/good.py": "x = 1\n"})
        inputs, results, outcome_dir = run_stages(repo, tmp_path, with_ai=False)
        # run the AI stage without BEDROCK_MODEL_ID
        r = invoke(
            "review", "ai", "--workspace", str(repo), "--inputs", str(inputs), "--out", str(results)
        )
        assert r.exit_code == 0
        data = gate(inputs, results, outcome_dir)
        assert data["gate_result"] == "fail"
        assert any("ai_review" in reason for reason in data["gate_reasons"])

    def test_a_model_crash_is_an_error_check_not_a_crashed_job(
        self, scenario: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Exploding(FakeBedrock):
            def converse_with_tool_retry(self, **kwargs: Any) -> Any:
                raise RuntimeError("bedrock exploded")

        monkeypatch.setattr("ai_hub.llm.bedrock.BedrockClient", Exploding)
        repo = make_repo(tmp_path, {"src/good.py": "x = 1\n"})
        inputs, results, outcome_dir = run_stages(repo, tmp_path)
        data = gate(inputs, results, outcome_dir)
        ai = next(c for c in data["checks"] if c["name"] == "ai_review")
        assert ai["status"] == "error"
        assert data["gate_result"] == "fail"

    def test_fork_pull_request_flow(self, scenario: str, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, {"src/good.py": "x = 1\n"})
        inputs, results, outcome_dir = run_stages(repo, tmp_path, with_ai=False)
        data = gate(inputs, results, outcome_dir, "--fork-pr")
        assert data["gate_result"] == "pass"
        assert {c["name"]: c["status"] for c in data["checks"]}["ai_review"] == "skipped"

    @respx.mock
    def test_read_only_token_still_reports_the_verdict(self, scenario: str, tmp_path: Path) -> None:
        repo = make_repo(tmp_path, {"src/bad.py": "import os\n"})
        inputs, results, outcome_dir = run_stages(repo, tmp_path, with_ai=False)
        gate(inputs, results, outcome_dir, "--fork-pr")
        # Every GitHub write is forbidden, as on a pull request from a fork.
        respx.route(host="api.github.test").mock(
            return_value=httpx.Response(403, json={"message": "Resource not accessible"})
        )
        summary = tmp_path / "summary.md"
        result = invoke(
            "report", "--outcome", str(outcome_dir / "outcome.json"), "--pr", "5",
            "--head-sha", "h", env={**REPORT_ENV, "GITHUB_STEP_SUMMARY": str(summary)},
        )  # fmt: skip
        assert result.exit_code == 1  # still the gate's verdict
        assert "src/bad.py" in summary.read_text(
            encoding="utf-8"
        )  # the result is visible in the job summary
        assert "read-only" in result.output

    def test_corrupt_outcome_fails_closed(self, scenario: str, tmp_path: Path) -> None:
        bad = tmp_path / "outcome.json"
        bad.write_text("{not json")
        result = invoke("report", "--outcome", str(bad), "--pr", "5", "--head-sha", "h")
        assert result.exit_code == 1

    def test_missing_outcome_fails_closed(self, scenario: str, tmp_path: Path) -> None:
        result = invoke(
            "report", "--outcome", str(tmp_path / "nope.json"), "--pr", "5", "--head-sha", "h"
        )
        assert result.exit_code == 1

    def test_report_without_credentials_skips_publishing_but_keeps_the_verdict(
        self, scenario: str, tmp_path: Path
    ) -> None:
        repo = make_repo(tmp_path, {"src/good.py": "x = 1\n"})
        inputs, results, outcome_dir = run_stages(repo, tmp_path, with_ai=False)
        gate(inputs, results, outcome_dir, "--fork-pr")
        result = invoke("report", "--outcome", str(outcome_dir / "outcome.json"))
        assert result.exit_code == 0
        assert "skipped" in result.output

    def test_prepare_outside_a_merge_commit_fails(self, scenario: str, tmp_path: Path) -> None:
        import subprocess

        repo = tmp_path / "plain"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        result = invoke(
            "review", "prepare", "--workspace", str(repo), "--out", str(tmp_path / "in")
        )
        assert result.exit_code == 1

    def test_stage_commands_reject_missing_inputs(self, scenario: str, tmp_path: Path) -> None:
        result = invoke(
            "review", "static", "--workspace", str(tmp_path), "--inputs", str(tmp_path / "none"),
            "--out", str(tmp_path / "res"),
        )  # fmt: skip
        assert result.exit_code == 1
