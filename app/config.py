from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/ghlconnector"

    # Secrets
    secrets_provider: str = "env"
    doppler_token: str | None = None

    # GHL
    ghl_oauth_client_id: str | None = None
    ghl_oauth_client_secret: str | None = None
    ghl_webhook_signing_secret: str | None = None
    ghl_redirect_uri: str | None = None
    # HMAC secret for OAuth state parameter (prevents CSRF on the install flow).
    # Generate with: python -c "import secrets; print(secrets.token_hex(32))"
    ghl_install_secret: str | None = None

    # Admin API
    # Protects the GHL OAuth admin endpoints.  Set in .env as ADMIN_API_KEY=...
    admin_api_key: str | None = None

    # AI
    anthropic_api_key: str | None = None
    ai_default_model: str = "claude-sonnet-4-6"
    ai_classifier_model: str = "claude-haiku-4-5"

    # Observability
    sentry_dsn: str | None = None
    prometheus_pushgateway: str | None = None
    slack_alert_webhook: str | None = None

    # Operations
    env: str = "development"
    log_level: str = "INFO"
    foundation_api_signing_secret: str | None = None

    # Server
    port: int = 8080


@lru_cache
def get_settings() -> Settings:
    return Settings()
