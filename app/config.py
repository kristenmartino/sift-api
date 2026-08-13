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

    # Regex pre-gate on the LLM entity linker — ON by default, set false to
    # disable. link_articles_llm makes one realtime call per article and is the
    # largest line item in the repo ($4.15/day of $8.99, 46%, per
    # ai_usage_daily 2026-07-31..08-04) because it is sized for the ~100
    # articles/day its docstring assumes rather than the ~2,000 actually
    # ingested. The gate sends an article to the LLM only when the free regex
    # matcher finds a candidate surface form — the LLM is there to
    # *disambiguate* collisions, not to *discover* names that never appear.
    #
    # Default true, unlike the flags above, deliberately: this one SAVES money
    # rather than spending it, and a default-off flag is exactly how
    # ai_cost_guard_enabled left ai_usage_daily empty for months (STATUS.md:40).
    # A flag you must remember to turn on is a saving you do not get. Treat it
    # as a kill switch, not an opt-in.
    #
    # Cost of the gate, measured by scripts/eval_linker_gate.py over 12,690
    # articles / 7 days: 98.11% of articles the LLM linked are still forwarded.
    # The ~1.9% it drops are last-name-only mentions (Kiggans, Khouw, Greene) —
    # the surface forms #40 deliberately refuses to match on.
    entity_linker_regex_gate_enabled: bool = True

    # Roster narrowing: send the LLM linker only the catalog rows the regex
    # gate already matched (plus same-surname siblings) instead of all ~856.
    # Measured at 2.3 rows per call — it deletes ~95% of the call's input,
    # which was the roster, not the article.
    #
    # Default true for the same reason as the gate above: it saves money, and a
    # saving you must remember to switch on is one you do not get. Treat it as
    # a kill switch. It rides on the gate, which is what computes the matches —
    # with the gate off this flag does nothing.
    #
    # It is also, on measurement, MORE accurate rather than a quality trade.
    # scripts/eval_linker_roster.py over 400 gated articles, disagreements plus
    # a sampled agreed base, blind-adjudicated by Sonnet: overall precision
    # 82.9% vs the full roster's 80.9%, and 257 correct links vs 241. Of the
    # links only the narrowed path found, 70.5% were judged correct against
    # 46.9% for those only the full roster found — fewer distractors, better
    # answers.
    #
    # Do NOT judge this by diffing against the full-roster path alone. That
    # reads "94.2% recall, 85.3% precision" and looks like a downgrade, because
    # it treats the incumbent as ground truth when the disagreements are mostly
    # where the incumbent is wrong. The eval script's docstring has the trap.
    entity_linker_roster_narrowing_enabled: bool = True

    # Incremental story threading (docs/INCREMENTAL_THREADING.md) — OFF, and
    # off is the right default here, unlike the gate above.
    #
    # That one was a small, measured, reversible saving on a leaf call site.
    # This rewrites the core product path: threading is 43% of Anthropic spend
    # and owns what the feed actually shows. It ships dark and proves itself in
    # shadow first, per STATUS.md:21 — "a detector that has never been run
    # against a known-true case is an untested detector."
    #
    # When false, `incremental_threading_shadow` still runs the free candidate
    # step read-only and logs what it *would* have grouped, so the comparison
    # against the live path is real data rather than a projection.
    incremental_threading_enabled: bool = False

    # Emit the shadow comparison. Costs nothing — Postgres kNN only, no LLM
    # call and no writes. Safe to leave on permanently; it is how the cutover
    # bar in docs/INCREMENTAL_THREADING.md gets evidence.
    incremental_threading_shadow: bool = True

    # Also run the real confirmation call during shadow, logging what it
    # decided and still writing nothing. OFF by default because it is the one
    # piece of cutover evidence that costs money — ~$0.005/run, ~$0.24/day.
    #
    # It exists because candidate counts are an upper bound, not a prediction:
    # the free shadow measures supply and the outlet gate, but only this
    # measures whether the LLM agrees the candidates are the same *event*
    # rather than merely the same topic. Turn it on for a day before cutover.
    incremental_threading_confirm_dryrun: bool = False

    # Per-stage model overrides, as "operation=catalog_id,operation=catalog_id"
    # against services/model_registry.py — e.g.
    #   LLM_MODEL_OVERRIDES=summarizer.batch=sonnet-4-6
    #
    # Empty means every stage runs the model it hardcoded before the registry
    # existed, so the default reproduces prior behaviour exactly. This is the
    # inverse of the flags above: they gate a new behaviour, this one is a
    # deliberate deviation from a known-good default, and the safe value is the
    # empty one.
    #
    # A malformed entry, an unknown operation, an unknown model, or a model
    # lacking a capability the operation needs is dropped with an ERROR and the
    # stage stays on its incumbent — never a hard failure at import, and never
    # silent. A silently-ignored override is the worst outcome available here:
    # the experiment runs, reports a number, and the number describes the
    # incumbent. `/health` echoes the resolved assignments so this is checkable
    # from outside the process rather than inferred.
    #
    # Changing it on Railway restarts the service (~30-60s, no CI, no build) —
    # which is what "without a deploy" buys, and what every flag above costs.
    llm_model_overrides: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
