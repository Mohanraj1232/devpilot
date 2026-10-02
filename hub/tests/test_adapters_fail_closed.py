"""Every adapter must fail closed: a broken tool is an ERROR, never a clean result."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ai_hub.analysis.base import (
    ToolAdapter,
    parse_json_output,
    run_tool_subprocess,
    select_files,
)
from ai_hub.analysis.security.bandit import BanditAdapter
from ai_hub.analysis.security.deps import NpmAuditAdapter, PipAuditAdapter
from ai_hub.analysis.security.gitleaks import GitleaksAdapter
from ai_hub.analysis.static.eslint import ESLintAdapter
from ai_hub.analysis.static.ruff import RuffAdapter
from ai_hub.analysis.static.semgrep import SemgrepAdapter
from ai_hub.errors import AnalysisError, FailureReason
from ai_hub.models import CheckStatus

PY = Path(sys.executable).as_posix()


class _Result:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


# ── run_tool_subprocess ──────────────────────────────────────


class TestRunToolSubprocess:
    def test_ok_returncodes_distinguish_findings_from_crashes(self, tmp_path: Path) -> None:
        cmd = [PY, "-c", "raise SystemExit(1)"]
        assert run_tool_subprocess(cmd, cwd=tmp_path, ok_returncodes=(0, 1)).returncode == 1
        with pytest.raises(AnalysisError, match="exited with code 2"):
            run_tool_subprocess(
                [PY, "-c", "raise SystemExit(2)"], cwd=tmp_path, ok_returncodes=(0, 1)
            )

    def test_stderr_is_included_and_redacted(self, tmp_path: Path) -> None:
        token = "ghp_" + "a" * 36
        script = f"import sys; sys.stderr.write('bad config {token}'); sys.exit(2)"
        with pytest.raises(AnalysisError) as exc:
            run_tool_subprocess([PY, "-c", script], cwd=tmp_path, ok_returncodes=(0,))
        assert "bad config" in exc.value.message
        assert token not in exc.value.message

    def test_timeout_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(AnalysisError, match="timed out"):
            run_tool_subprocess([PY, "-c", "import time; time.sleep(30)"], cwd=tmp_path, timeout=1)

    def test_credentials_are_not_passed_to_tools(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "secret")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
        script = "import os; print(sorted(k for k in os.environ if 'TOKEN' in k or 'AWS' in k))"
        out = run_tool_subprocess([PY, "-c", script], cwd=tmp_path).stdout
        assert "GITHUB_TOKEN" not in out
        assert "AWS_SECRET_ACCESS_KEY" not in out


class TestParseJsonOutput:
    def test_valid(self) -> None:
        assert parse_json_output(_Result('{"a": 1}'), "t") == {"a": 1}

    def test_empty_is_an_error(self) -> None:
        with pytest.raises(AnalysisError, match="no output"):
            parse_json_output(_Result(""), "t")

    def test_garbage_is_an_error(self) -> None:
        with pytest.raises(AnalysisError, match="not valid JSON"):
            parse_json_output(_Result("Traceback (most recent call last)"), "t")


def test_select_files() -> None:
    assert select_files(None, (".py",)) is None
    assert select_files(["a.py", "b.js", "C.PY"], (".py",)) == ["a.py", "C.PY"]
    assert select_files([], (".py",)) == []


# ── a crashing tool is ERROR for every adapter ───────────────

ADAPTERS: list[tuple[str, str, ToolAdapter]] = [
    ("ai_hub.analysis.static.ruff", "ruff", RuffAdapter()),
    ("ai_hub.analysis.static.eslint", "eslint", ESLintAdapter()),
    ("ai_hub.analysis.static.semgrep", "semgrep", SemgrepAdapter()),
    ("ai_hub.analysis.security.bandit", "bandit", BanditAdapter()),
    ("ai_hub.analysis.security.deps", "pip-audit", PipAuditAdapter()),
    ("ai_hub.analysis.security.deps", "npm-audit", NpmAuditAdapter()),
]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A repository that every adapter considers applicable."""
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n")
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "package-lock.json").write_text("{}")
    (tmp_path / ".eslintrc.json").write_text("{}")
    return tmp_path


@pytest.mark.parametrize(("module", "label", "adapter"), ADAPTERS)
class TestEveryAdapterFailsClosed:
    def test_unparseable_output(
        self, module: str, label: str, adapter: ToolAdapter, project: Path
    ) -> None:
        with patch(f"{module}.run_tool_subprocess", return_value=_Result("Segmentation fault")):
            assert adapter.check(project).status == CheckStatus.ERROR

    def test_empty_output(
        self, module: str, label: str, adapter: ToolAdapter, project: Path
    ) -> None:
        with patch(f"{module}.run_tool_subprocess", return_value=_Result("")):
            assert adapter.check(project).status == CheckStatus.ERROR

    def test_tool_failure_is_reported_with_a_reason(
        self, module: str, label: str, adapter: ToolAdapter, project: Path
    ) -> None:
        boom = AnalysisError(FailureReason.TOOL_CRASH, "exit 2")
        with patch(f"{module}.run_tool_subprocess", side_effect=boom):
            result = adapter.check(project)
        assert result.status == CheckStatus.ERROR
        assert "exit 2" in result.summary

    def test_missing_binary(
        self, module: str, label: str, adapter: ToolAdapter, project: Path
    ) -> None:
        # No mocking: if the tool isn't installed, run_tool_subprocess raises and check()
        # converts it into ERROR rather than letting the pipeline crash or report clean.
        adapter_cls = type(adapter)
        missing = adapter_cls.__new__(adapter_cls)
        missing.__dict__.update(adapter.__dict__)
        with patch(
            f"{module}.run_tool_subprocess",
            side_effect=AnalysisError(
                FailureReason.TOOL_CRASH,
                "Tool x not found on PATH",
            ),
        ):
            assert missing.check(project).status == CheckStatus.ERROR


# ── exit codes are checked by the real subprocess wrapper ────


class TestExitCodesAreEnforced:
    @pytest.mark.parametrize(
        ("adapter", "module"),
        [
            (RuffAdapter(), "ruff"),
            (SemgrepAdapter(), "semgrep"),
            (BanditAdapter(), "bandit"),
        ],
    )
    def test_crash_exit_codes_are_rejected(
        self, adapter: ToolAdapter, module: str, tmp_path: Path
    ) -> None:
        (tmp_path / "a.py").write_text("x = 1\n")
        captured: dict[str, Any] = {}

        def fake_run(cmd: list[str], cwd: Path, **kwargs: Any) -> Any:
            captured["ok_returncodes"] = kwargs.get("ok_returncodes")
            return _Result("[]" if module == "ruff" else '{"results": []}')

        target = {
            "ruff": "ai_hub.analysis.static.ruff",
            "semgrep": "ai_hub.analysis.static.semgrep",
            "bandit": "ai_hub.analysis.security.bandit",
        }[module]
        with patch(f"{target}.run_tool_subprocess", side_effect=fake_run):
            adapter.run(tmp_path, ["a.py"])
        # 0 = clean and 1 = findings are fine; 2+ must raise inside run_tool_subprocess.
        assert set(captured["ok_returncodes"]) == {0, 1}


# ── applicability: skip rather than fake a result ────────────


class TestApplicability:
    def test_python_tools_skip_when_no_python_changed(self, tmp_path: Path) -> None:
        for adapter in (RuffAdapter(), BanditAdapter()):
            ok, reason = adapter.applicable(tmp_path, ["README.md", "app.js"])
            assert ok is False
            assert "Python" in reason

    def test_python_tools_apply_when_python_changed_or_unknown(self, tmp_path: Path) -> None:
        assert RuffAdapter().applicable(tmp_path, ["a.py"])[0] is True
        assert RuffAdapter().applicable(tmp_path, None)[0] is True

    def test_eslint_needs_config_and_js_files(self, tmp_path: Path) -> None:
        adapter = ESLintAdapter()
        assert adapter.applicable(tmp_path, ["a.py"])[0] is False
        assert "JavaScript" in adapter.applicable(tmp_path, ["a.py"])[1]
        assert adapter.applicable(tmp_path, ["a.js"]) == (False, "No ESLint configuration found")
        (tmp_path / "eslint.config.js").write_text("export default []")
        assert adapter.applicable(tmp_path, ["a.js"])[0] is True

    def test_eslint_config_in_package_json(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"eslintConfig": {"rules": {}}}')
        assert ESLintAdapter().applicable(tmp_path, ["a.ts"])[0] is True

    def test_eslint_never_downloads_packages(self, tmp_path: Path) -> None:
        (tmp_path / ".eslintrc.json").write_text("{}")
        seen: list[list[str]] = []

        def fake(cmd: list[str], cwd: Path, **kwargs: Any) -> Any:
            seen.append(cmd)
            return _Result("[]")

        with patch("ai_hub.analysis.static.eslint.run_tool_subprocess", side_effect=fake):
            ESLintAdapter().run(tmp_path, ["a.js", "b.py"])
        assert "--no-install" in seen[0]
        assert "b.py" not in seen[0]

    def test_pip_audit_only_audits_declared_requirements(self, tmp_path: Path) -> None:
        adapter = PipAuditAdapter()
        assert adapter.applicable(tmp_path)[0] is False
        assert adapter.run(tmp_path) == []  # must not fall back to auditing the runner
        (tmp_path / "requirements.txt").write_text("x==1\n")
        assert adapter.applicable(tmp_path)[0] is True

    def test_npm_audit_needs_a_lockfile(self, tmp_path: Path) -> None:
        adapter = NpmAuditAdapter()
        (tmp_path / "package.json").write_text("{}")
        ok, reason = adapter.applicable(tmp_path)
        assert ok is False
        assert "lockfile" in reason
        (tmp_path / "package-lock.json").write_text("{}")
        assert adapter.applicable(tmp_path)[0] is True

    def test_npm_audit_error_payload_is_an_error(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{}")
        payload = '{"error": {"code": "ENOAUDIT", "summary": "audit endpoint unavailable"}}'
        with (
            patch(
                "ai_hub.analysis.security.deps.run_tool_subprocess",
                return_value=_Result(payload, "", 1),
            ),
            pytest.raises(AnalysisError, match="audit endpoint unavailable"),
        ):
            NpmAuditAdapter().run(tmp_path)


class TestSemgrep:
    def test_metrics_are_off_and_ruleset_is_explicit(self, tmp_path: Path) -> None:
        seen: list[list[str]] = []

        def fake(cmd: list[str], cwd: Path, **kwargs: Any) -> Any:
            seen.append(cmd)
            return _Result('{"results": []}')

        with patch("ai_hub.analysis.static.semgrep.run_tool_subprocess", side_effect=fake):
            SemgrepAdapter().run(tmp_path, ["a.py"])
        assert "--metrics=off" in seen[0]
        assert seen[0][seen[0].index("--config") + 1] != "auto"

    def test_no_changed_files_means_nothing_to_scan(self, tmp_path: Path) -> None:
        with patch("ai_hub.analysis.static.semgrep.run_tool_subprocess") as run:
            assert SemgrepAdapter().run(tmp_path, []) == []
        run.assert_not_called()


class TestBandit:
    def test_line_end_uses_line_range_not_a_column(self, tmp_path: Path) -> None:
        payload = (
            '{"results": [{"issue_severity": "HIGH", "issue_confidence": "HIGH",'
            ' "test_id": "B602", "issue_text": "shell", "filename": "a.py",'
            ' "line_number": 10, "line_range": [10, 11, 12], "end_col_offset": 99}]}'
        )
        with patch(
            "ai_hub.analysis.security.bandit.run_tool_subprocess", return_value=_Result(payload)
        ):
            finding = BanditAdapter().run(tmp_path, ["a.py"])[0]
        assert (finding.line_start, finding.line_end) == (10, 12)


# ── the real Gitleaks binary (skipped when it is not installed) ──


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks is not installed")
class TestGitleaksAdapterWithRealBinary:
    # Assembled at runtime so no secret-looking literal lives in the repository.
    TOKEN = "ghp_" + "x9Kq2LmZ8Vb3NcT7yHwP4dR6sFgJ1aEuB0oX"

    def test_finds_a_secret_and_never_echoes_it(self, tmp_path: Path) -> None:
        (tmp_path / "cfg.py").write_text(f'token = "{self.TOKEN}"\n')
        findings = GitleaksAdapter().run(tmp_path)
        assert [f.rule_id for f in findings] == ["github-pat"]
        assert findings[0].file.replace("\\", "/").endswith("cfg.py")
        assert self.TOKEN not in findings[0].model_dump_json()

    def test_clean_directory(self, tmp_path: Path) -> None:
        (tmp_path / "ok.py").write_text("x = 1\n")
        assert GitleaksAdapter().run(tmp_path) == []

    def test_unreadable_source_is_an_error_not_a_clean_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # gitleaks exits 1 for an unreadable source - the same code it uses for "leaks
        # found" - and writes no report. Keep cwd valid so only the --source is bad.
        import ai_hub.analysis.security.gitleaks as module

        real = module.run_tool_subprocess
        monkeypatch.setattr(
            module, "run_tool_subprocess", lambda cmd, cwd, **kw: real(cmd, cwd=tmp_path, **kw)
        )
        with pytest.raises(AnalysisError, match="did not produce a report"):
            GitleaksAdapter().run(tmp_path / "does-not-exist")

    def test_missing_working_directory_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(AnalysisError):
            run_tool_subprocess([PY, "-c", "pass"], cwd=tmp_path / "nope")
