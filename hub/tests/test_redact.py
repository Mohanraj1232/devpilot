"""Tests for ai_hub.safety.redact."""

from ai_hub.safety.redact import REDACTED, redact, redact_dict


class TestRedact:
    def test_aws_key(self) -> None:
        text = "key=AKIAIOSFODNN7EXAMPLE"
        assert "AKIA" not in redact(text)

    def test_github_pat(self) -> None:
        text = "token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"
        assert "ghp_" not in redact(text)

    def test_github_oauth(self) -> None:
        text = "auth: gho_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"
        assert "gho_" not in redact(text)

    def test_bearer_token(self) -> None:
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.test.signature"
        result = redact(text)
        assert "eyJ" not in result

    def test_private_key(self) -> None:
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----"
        result = redact(text)
        assert "MIIE" not in result

    def test_connection_string(self) -> None:
        text = "dsn=mysql://user:pass@host:3306/db"
        result = redact(text)
        assert "user:pass" not in result

    def test_generic_secret_assignment(self) -> None:
        text = 'password = "my_super_secret_password123"'
        result = redact(text)
        assert "my_super_secret" not in result

    def test_clean_text_unchanged(self) -> None:
        text = "This is a normal log message with no secrets."
        assert redact(text) == text


class TestRedactDict:
    def test_sensitive_key(self) -> None:
        result = redact_dict({"token": "abc123456789"})
        assert result["token"] == REDACTED

    def test_nested_dict(self) -> None:
        result = redact_dict({"config": {"password": "secret123"}})
        inner = result["config"]
        assert isinstance(inner, dict)
        assert inner["password"] == REDACTED

    def test_pattern_in_value(self) -> None:
        result = redact_dict({"log": "key=AKIAIOSFODNN7EXAMPLE"})
        assert "AKIA" not in str(result["log"])

    def test_non_string_values_pass_through(self) -> None:
        result = redact_dict({"count": 42, "enabled": True})
        assert result["count"] == 42
        assert result["enabled"] is True
