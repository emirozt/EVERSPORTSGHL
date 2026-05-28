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

    # ── M5 Writeback safety ───────────────────────────────────────────────────
    # Controls the dev-mode safety guard on writeback handlers.
    # "dev"  — hard whitelist: only emiroztrk@gmail.com + Reformer Booty Burn slot
    # "prod" — no whitelist; production Eversports account may be touched
    # MUST be explicitly set to "prod" in production.  Default-deny.
    writeback_safety_mode: str = "dev"

    # When True, all writeback handlers log their payload and return a fake
    # success without contacting Eversports.  Flip to False only after the
    # user has reviewed dry-run logs and given written go-ahead in chat.
    writeback_dry_run: bool = True

    # ── M5 Notification (audit email on every live writeback) ─────────────
    # SMTP credentials for sending writeback audit notifications.
    # Leave smtp_host unset to use the stub path (log-only).
    notification_smtp_host: str | None = None
    notification_smtp_port: int = 587
    notification_smtp_user: str | None = None
    notification_smtp_password: str | None = None
    notification_from_email: str = "noreply@eversports-ghl.local"
    notification_owner_email: str | None = None  # default notification recipient


@lru_cache
def get_settings() -> Settings:
    return Settings()
