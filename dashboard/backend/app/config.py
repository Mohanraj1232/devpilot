"""Application configuration via environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings


INSECURE_SECRET_KEY = "change-me-in-production"


class Settings(BaseSettings):
    database_url: str = "mysql+pymysql://devpilot:devpilot@localhost:3306/devpilot"
    secret_key: str = INSECURE_SECRET_KEY
    # "development" allows the placeholder secret key; anything else refuses to start with it.
    environment: str = "production"
    github_client_id: str = ""
    github_client_secret: str = ""
    github_redirect_uri: str = "http://localhost:8000/api/v1/auth/callback"
    github_api_url: str = "https://api.github.com"
    # Login of the DevPilot bot account (checked when verifying a repository's setup).
    bot_login: str = ""
    # GitHub App used by DevPilot to act on repositories (preferred over a bot user + PAT).
    # The private key never leaves this server; runs ask the dashboard for short-lived tokens.
    github_app_id: str = ""
    github_app_slug: str = ""
    github_app_private_key: str = ""  # PEM; "\n" escapes are accepted
    github_app_private_key_path: str = ""  # alternative to the value above
    # Shared secret for GitHub webhooks (X-Hub-Signature-256). Empty = webhooks are refused.
    webhook_secret: str = ""
    cors_origins: list[str] = ["http://localhost:5173"]
    session_max_age: int = 86400

    model_config = {"env_prefix": "DEVPILOT_"}

    def validate_for_runtime(self) -> None:
        """Refuse to run with a publicly known session-signing key outside development."""
        if self.secret_key == INSECURE_SECRET_KEY and self.environment != "development":
            raise RuntimeError(
                "DEVPILOT_SECRET_KEY must be set to a long random value "
                "(or set DEVPILOT_ENVIRONMENT=development for local use)"
            )
