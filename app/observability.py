from __future__ import annotations

import logging

import sentry_sdk

from app.config import settings

logger = logging.getLogger("sift-api")


def init_sentry() -> bool:
    """Initialize Sentry error monitoring.

    No-op unless ``SENTRY_DSN`` is configured, so local dev, tests, and any
    unconfigured deploy run untouched. The FastAPI/Starlette integration is
    auto-enabled by sentry-sdk when FastAPI is installed; its default
    failed-request reporting captures unhandled 5xx responses and leaves
    routine 4xx client errors alone. No PII is sent (``send_default_pii`` is
    ``False``).

    Returns ``True`` if Sentry was initialized, ``False`` if skipped.
    """
    if not settings.sentry_dsn:
        return False

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=False,
    )
    logger.info("Sentry error monitoring initialized (env=%s)", settings.environment)
    return True
