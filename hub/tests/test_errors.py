"""Tests for ai_hub.errors."""

import pytest

from ai_hub.errors import (
    AnalysisError,
    AuthError,
    ConfigError,
    FailureReason,
    GitError,
    GuardrailError,
    HubError,
    LLMError,
    safe_stop,
)


class TestHubError:
    def test_base_error(self) -> None:
        err = HubError(FailureReason.INTERNAL_ERROR, "something broke")
        assert err.reason == FailureReason.INTERNAL_ERROR
        assert err.message == "something broke"
        assert err.details == {}
        assert "[internal_error]" in str(err)

    def test_with_details(self) -> None:
        err = HubError(FailureReason.TIMEOUT, "timed out", {"seconds": 30})
        assert err.details["seconds"] == 30


class TestConfigError:
    def test_auto_reason(self) -> None:
        err = ConfigError("bad version")
        assert err.reason == FailureReason.CONFIG_INVALID


class TestAuthError:
    def test_custom_reason(self) -> None:
        err = AuthError(FailureReason.AWS_OIDC_FAILURE, "OIDC failed")
        assert err.reason == FailureReason.AWS_OIDC_FAILURE


class TestSpecializedErrors:
    def test_llm_error(self) -> None:
        err = LLMError(FailureReason.MODEL_UNAVAILABLE, "Bedrock down")
        assert isinstance(err, HubError)

    def test_git_error(self) -> None:
        err = GitError(FailureReason.REBASE_CONFLICT, "conflict in main.py")
        assert isinstance(err, HubError)

    def test_guardrail_error(self) -> None:
        err = GuardrailError(FailureReason.SECRET_DETECTED, "found API key")
        assert isinstance(err, HubError)

    def test_analysis_error(self) -> None:
        err = AnalysisError(FailureReason.TOOL_CRASH, "ruff segfaulted")
        assert isinstance(err, HubError)


class TestSafeStop:
    def test_exits_with_code(self) -> None:
        err = HubError(FailureReason.INTERNAL_ERROR, "boom")
        with pytest.raises(SystemExit) as exc_info:
            safe_stop(err)
        assert exc_info.value.code == 1

    def test_no_exit(self) -> None:
        err = HubError(FailureReason.INTERNAL_ERROR, "boom")
        safe_stop(err, exit_process=False)


class TestFailureReasonCoverage:
    def test_all_reasons_are_strings(self) -> None:
        for reason in FailureReason:
            assert isinstance(reason.value, str)
            assert len(reason.value) > 0
