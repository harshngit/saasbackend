from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///./crm_saas.db"

    jwt_secret: str = "dev-insecure-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 30

    cors_origins: str = "http://localhost,http://localhost:3000,http://localhost:8080"

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
    super_admin_password: str = "Admin@123"
    super_admin_name: str = "Ravi Malhotra"

    # Seed the Super Admin (and demo firm) automatically on first startup.
    # Handy on hosts like Render where you can't easily run a one-off command.
    seed_on_startup: bool = False

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def sqlalchemy_database_url(self) -> str:
        """Normalize the DB URL so managed-Postgres URLs work with psycopg3.

        Render/Heroku hand out `postgres://` or `postgresql://` URLs, which
        SQLAlchemy maps to the (uninstalled) psycopg2 driver. Force psycopg3.
        """
        url = self.database_url
        if url.startswith("postgres://"):
            return "postgresql+psycopg://" + url[len("postgres://"):]
        if url.startswith("postgresql://"):
            return "postgresql+psycopg://" + url[len("postgresql://"):]
        return url


settings = Settings()
