"""Application configuration, loaded from environment variables (.env)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Database connection settings, read from environment variables.

    `postgres_host` and `postgres_port` default to values suited for
    local development (app running outside Docker). Docker Compose
    overrides them explicitly to reach the `db` service by name.
    """

    postgres_db: str
    postgres_user: str
    postgres_password: str
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def database_url(self) -> str:
        """Build the SQLAlchemy connection string for PostgreSQL.

        Returns:
            str: The full connection string, e.g.
                "postgresql+psycopg://user:pass@host:port/db".
        """
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()