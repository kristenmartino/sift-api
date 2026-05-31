from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://sift:sift@localhost:5432/siftdb"
    anthropic_api_key: str = ""
    voyage_api_key: str = ""
    pipeline_api_key: str = "dev-key"
    port: int = 8000
    environment: str = "development"
    log_level: str = "info"

    # Error monitoring (Sentry) — inert unless sentry_dsn (SENTRY_DSN) is set.
    # Reuses `environment` as the Sentry environment tag.
    sentry_dsn: str = ""
    sentry_traces_sample_rate: float = 0.1

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
