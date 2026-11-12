"""Startup configuration validation for local and deployable beta modes."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin


class ConfigError(RuntimeError):
    pass


LOCAL_SESSION_SECRET = "local-dev-only-session-secret-change-me"


@dataclass(frozen=True)
class AppConfig:
    database_url: str
    upload_dir: str
    session_secret: str
    auth_mode: str  # local | google
    environment: str  # local | beta | production
    max_upload_mb: int
    allow_dev_login: bool
    timezone: str
    public_base_url: str
    google_client_id: str
    google_client_secret: str
    google_redirect_uri: str

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    @property
    def google_oauth_configured(self) -> bool:
        return bool(
            self.google_client_id
            and self.google_client_secret
            and self.google_redirect_uri
        )


def _truthy(name: str, default: str = "") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes"}


def _derive_redirect_uri(public_base_url: str, explicit: str) -> str:
    if explicit:
        return explicit.rstrip("/")
    base = public_base_url.rstrip("/")
    if not base:
        return ""
    return urljoin(base + "/", "api/auth/google/callback")


def load_config(*, strict: Optional[bool] = None) -> AppConfig:
    environment = os.getenv("LIFE_INBOX_ENV", "local").strip() or "local"
    database_url = os.getenv(
        "DATABASE_URL",
        "sqlite:///./data/life_inbox.db",
    ).strip()
    upload_dir = os.getenv("LIFE_INBOX_UPLOAD_DIR", "./data/uploads").strip()
    session_secret = (
        os.getenv("LIFE_INBOX_SESSION_SECRET")
        or os.getenv("SESSION_SECRET")
        or ""
    ).strip()
    auth_mode = (
        os.getenv("LIFE_INBOX_AUTH_MODE", "local").strip() or "local"
    )
    # Backward compatible with older "dev" / "google_stub" values.
    if auth_mode in {"dev", "google_stub"}:
        auth_mode = "local" if auth_mode == "dev" else "google"
    timezone = os.getenv("LIFE_INBOX_TIMEZONE", "UTC").strip() or "UTC"
    max_upload_mb = int(os.getenv("LIFE_INBOX_MAX_UPLOAD_MB", "5") or "5")
    # Dev Login is local-only; always fail-closed outside local.
    allow_dev_login = environment == "local" and _truthy(
        "LIFE_INBOX_ALLOW_DEV_LOGIN",
        "1",
    )
    public_base_url = os.getenv("LIFE_INBOX_PUBLIC_BASE_URL", "").strip()
    google_client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    google_client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    google_redirect_uri = _derive_redirect_uri(
        public_base_url,
        os.getenv("GOOGLE_REDIRECT_URI", "").strip(),
    )

    if strict is None:
        strict = environment != "local"

    errors: list[str] = []
    if environment not in {"local", "beta", "production"}:
        errors.append("LIFE_INBOX_ENV must be 'local', 'beta', or 'production'.")
    if not database_url:
        errors.append("DATABASE_URL is required.")
    if environment in {"beta", "production"} and database_url.startswith("sqlite"):
        errors.append(
            "PostgreSQL DATABASE_URL is required for beta/production "
            "(SQLite is local/dev only)."
        )
    if not upload_dir:
        errors.append("LIFE_INBOX_UPLOAD_DIR is required.")
    if environment != "local" and len(session_secret) < 32:
        errors.append(
            "LIFE_INBOX_SESSION_SECRET must be at least 32 characters in "
            "beta/production."
        )
    if environment != "local" and session_secret == LOCAL_SESSION_SECRET:
        errors.append(
            "LIFE_INBOX_SESSION_SECRET must use a private value outside local mode."
        )
    if auth_mode not in {"local", "google"}:
        errors.append("LIFE_INBOX_AUTH_MODE must be 'local' or 'google'.")
    if environment in {"beta", "production"} and allow_dev_login:
        errors.append(
            "Dev login must be disabled in beta/production "
            "(LIFE_INBOX_ALLOW_DEV_LOGIN is forced off outside local)."
        )
    if environment in {"beta", "production"} and auth_mode == "google":
        if not google_client_id or not google_client_secret:
            errors.append(
                "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are required "
                "when LIFE_INBOX_AUTH_MODE=google in beta/production."
            )
        if not google_redirect_uri:
            errors.append(
                "GOOGLE_REDIRECT_URI or LIFE_INBOX_PUBLIC_BASE_URL is "
                "required for Google OAuth in beta/production."
            )
    if max_upload_mb < 1 or max_upload_mb > 50:
        errors.append("LIFE_INBOX_MAX_UPLOAD_MB must be between 1 and 50.")

    if errors and strict:
        raise ConfigError("; ".join(errors))

    if not session_secret and environment == "local":
        session_secret = LOCAL_SESSION_SECRET

    return AppConfig(
        database_url=database_url,
        upload_dir=upload_dir,
        session_secret=session_secret,
        auth_mode=auth_mode,
        environment=environment,
        max_upload_mb=max_upload_mb,
        allow_dev_login=allow_dev_login,
        timezone=timezone,
        public_base_url=public_base_url.rstrip("/"),
        google_client_id=google_client_id,
        google_client_secret=google_client_secret,
        google_redirect_uri=google_redirect_uri,
    )


def config_warnings(config: AppConfig) -> list[str]:
    warnings: list[str] = []
    if config.session_secret.startswith("local-dev-only"):
        warnings.append("Using default local session secret.")
    if config.allow_dev_login:
        warnings.append("Dev login is enabled.")
    if config.is_sqlite and config.environment != "local":
        warnings.append("SQLite in use outside local environment.")
    if config.auth_mode == "google" and not config.google_oauth_configured:
        warnings.append("Google OAuth selected but credentials are incomplete.")
    return warnings
