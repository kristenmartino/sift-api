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

    # Daily AI cost ceiling (sift-api#70) — inert unless ai_cost_guard_enabled.
    # Tracks live-path Claude + Voyage spend and hard-stops paid calls for the
    # rest of the UTC day once daily_ai_cost_limit_usd is reached.
    ai_cost_guard_enabled: bool = False
    daily_ai_cost_limit_usd: float = 10.0
    ai_cost_alert_threshold_ratio: float = 0.8

    # Runtime LLM-judge for why_it_matters (sift-api#90) — OFF by default. When
    # enabled, each generated line that survives the deterministic gate is judged
    # (Sonnet) in the batch-result path and dropped if it restates or
    # editorializes — catching the paraphrase residual the cheap gate can't.
    # Adds a paid call per kept line; respects the cost guard above.
    why_it_matters_judge_enabled: bool = False

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
