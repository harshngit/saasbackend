from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import certifi
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///./crm_saas.db"

    # --- PostgreSQL connection pool (ignored for SQLite — see app/core/database.py) ---
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_pool_recycle: int = 300

    # --- One-off inline-upload -> stored-file conversion (app/services/file_migration_service.py) ---
    # Runs a full table scan across 8 models; off by default so it never runs implicitly
    # on every boot. Run explicitly via `python -m app.scripts.convert_inline_uploads`
    # instead, or set this to true only when an automatic run is actually wanted.
    convert_inline_uploads_on_startup: bool = False

    # --- Cloudflare R2 (S3-compatible) file storage — see app/core/r2.py ---
    # All empty by default: R2 is considered "configured" only when every one of these
    # is non-empty (see r2_configured below). Until then, file storage is unchanged
    # (bytes live in stored_files.data, exactly as today).
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket_name: str = ""

    jwt_secret: str = "dev-insecure-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 30

    # Free-trial length (days) applied at registration.
    trial_days: int = 7

    # Absolute base used to make *_url fields absolute in API responses
    # (https://api.example.com — no trailing slash needed either way, see
    # app.core.files.normalize_file_url). The database always keeps storing
    # the relative /files/{id} form regardless of this setting; only response
    # serialization reads it. Left blank, responses stay relative too (today's
    # behavior) — set it once the API is served from a stable public host.
    public_base_url: str = ""

    # Swagger/ReDoc/OpenAPI documentation exposure.
    # Set to true for local/development; set ENABLE_DOCS=false in production.
    enable_docs: bool = True

    cors_origins: str = "https://crm-saas.asynk.in"
    cors_origin_regex: str = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"

    # --- Google Sign-In & OAuth 2.0 Redirect ---
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"
    frontend_url: str = "http://localhost:5173"

    # --- Password reset ---
    reset_token_expire_minutes: int = 30
    # Front-end page that receives the ?token=... link from the reset email.
    frontend_reset_url: str = "http://localhost:8080/reset-password"
    # DEV ONLY: when true, /auth/forgot-password returns the raw token in the
    # response so the flow is testable without a mailbox. Never enable in prod.
    expose_reset_token: bool = False

    # --- SMTP (email delivery) ---
    # Leave smtp_host empty to run in "console" mode: emails are printed to the
    # server log instead of being sent (handy for local dev).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "CRM SaaS <no-reply@crm-saas.local>"
    smtp_use_tls: bool = True

    super_admin_email: str = "superadmin@demo.com"
    # No default on purpose: a known password baked into source code would be
    # a working production credential for anyone who reads the repo. Set
    # SUPER_ADMIN_PASSWORD explicitly wherever a Super Admin actually needs to
    # be created — app.seed.seed_super_admin() refuses to seed one without it.
    super_admin_password: str = ""
    super_admin_name: str = "Ravi Malhotra"

    # Seed the Super Admin (and demo firm) automatically on first startup.
    # Handy on hosts like Render where you can't easily run a one-off command.
    seed_on_startup: bool = False

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def r2_configured(self) -> bool:
        """R2 is only ever used once every one of these is set — never partially."""
        return bool(
            self.r2_account_id and self.r2_access_key_id
            and self.r2_secret_access_key and self.r2_bucket_name
        )

    @property
    def sqlalchemy_database_url(self) -> str:
        """Normalize the DB URL so managed-Postgres URLs work with psycopg3, and
        make a PlanetScale-style `sslrootcert=system` query value usable outside
        the specific Linux distros that actually keep a system CA bundle at the
        path libpq expects.

        - Render/Heroku hand out `postgres://` or `postgresql://` URLs, which
          SQLAlchemy maps to the (uninstalled) psycopg2 driver. Force psycopg3.
        - PlanetScale (and some other managed Postgres providers) hand out a
          connection string with `?sslmode=verify-full&sslrootcert=system` —
          `system` tells libpq to trust the OS's own CA bundle, which doesn't
          exist in every deployment environment. `certifi` ships a CA bundle
          that does, so `sslrootcert=system` specifically is rewritten to
          `certifi.where()`; every other query parameter (including
          `sslmode`, which must stay `verify-full`, never downgraded) is left
          exactly as given. A URL without that exact value is not touched.
        """
        url = self.database_url
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        elif not url.startswith("postgresql+psycopg://"):
            return url  # SQLite (or anything else) — nothing further to do

        parts = urlsplit(url)
        query = parse_qsl(parts.query, keep_blank_values=True)
        if not any(k == "sslrootcert" and v == "system" for k, v in query):
            return url
        query = [(k, certifi.where()) if k == "sslrootcert" and v == "system" else (k, v) for k, v in query]
        return urlunsplit(parts._replace(query=urlencode(query)))


settings = Settings()
