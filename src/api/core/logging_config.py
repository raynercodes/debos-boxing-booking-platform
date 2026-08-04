"""
Shared logging setup.

Lambda ships anything written to stdout straight into CloudWatch Logs
automatically — no extra library or CloudWatch SDK calls needed to get logs
out of the function. This module just makes sure every file in the project
formats its log lines the same way, so scanning CloudWatch later actually
shows a consistent, readable trail instead of a mix of ad-hoc print statements.

Usage in any module:
    from src.api.core.logging_config import get_logger
    logger = get_logger(__name__)
    logger.info("Booking created: %s", booking_id)
"""

import logging
import os

_configured = False


def _configure_once() -> None:
    """Runs the actual logging.basicConfig() exactly once per Lambda
    execution context — calling basicConfig multiple times is a no-op after
    the first call anyway, but the flag makes the intent explicit rather
    than relying on that implicit behavior."""
    global _configured
    if _configured:
        return

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Call this once at the top of any module, right after the imports —
    same pattern as calling `logging.getLogger(__name__)` directly, but
    guarantees the shared format is applied first."""
    _configure_once()
    return logging.getLogger(name)
