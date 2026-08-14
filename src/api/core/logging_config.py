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
    execution context.

    force=True is REQUIRED, not optional, in this specific environment —
    AWS Lambda's Python runtime pre-attaches its own handler to the root
    logger before our code ever runs. logging.basicConfig() silently does
    NOTHING if the root logger already has handlers (documented Python
    behavior), so without force=True, our LOG_LEVEL setting and format
    string were being completely ignored — every logger.info()/warning()
    call in the whole app was silently swallowed in the real deployed
    Lambda, invisible locally since pytest has no such pre-existing
    handler to conflict with. force=True explicitly clears Lambda's
    existing handler and applies ours instead."""
    global _configured
    if _configured:
        return

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        force=True,
    )
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Call this once at the top of any module, right after the imports —
    same pattern as calling `logging.getLogger(__name__)` directly, but
    guarantees the shared format is applied first."""
    _configure_once()
    return logging.getLogger(name)
