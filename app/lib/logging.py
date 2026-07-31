from __future__ import annotations

import logging
import os

import structlog


def configure_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    renderer = (
        structlog.dev.ConsoleRenderer()
        if os.getenv("LOG_FORMAT") == "console"
        else structlog.processors.JSONRenderer()
    )
    timestamp = structlog.processors.TimeStamper(fmt="iso", utc=True)

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            timestamp,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            # Leave rendering to the stdlib handler. Rendering here as well
            # produces JSON-inside-JSON and makes structured fields unusable.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging through structlog
    root_logger = logging.getLogger()
    for existing in list(root_logger.handlers):
        if getattr(existing, "_clusy_handler", False):
            root_logger.removeHandler(existing)
            existing.close()
    handler = logging.StreamHandler()
    handler._clusy_handler = True  # type: ignore[attr-defined]
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=renderer,
            foreign_pre_chain=[
                structlog.stdlib.add_logger_name,
                structlog.stdlib.add_log_level,
                timestamp,
            ],
        )
    )
    root_logger.addHandler(handler)
    root_logger.setLevel(level)

    # Silence noisy third-party loggers
    for noisy in ("httpx", "httpcore", "urllib3", "playwright", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
