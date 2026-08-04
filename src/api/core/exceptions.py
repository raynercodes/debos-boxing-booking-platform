"""
Domain-level exceptions for this project.

Business logic (SecurityService, LockoutManager, etc.) should raise these
instead of fastapi.HTTPException directly. Keeping domain errors decoupled
from HTTP specifics means the same service classes could be reused outside
a FastAPI context (e.g. a script, a different handler) without dragging
fastapi as a dependency into business logic that has nothing to do with it.

Routes translate these to HTTP responses via the exception handlers
registered in main.py, rather than each route needing its own try/except.
"""


class AppError(Exception):
    """Base class for all domain-level errors in this platform."""


class InvalidCredentialsError(AppError):
    """Raised when login credentials don't match — email or password wrong.
    Deliberately doesn't distinguish which one failed, to avoid leaking
    which part of the credential pair was incorrect."""


class LockedOutError(AppError):
    """Raised when the admin login is currently in a brute-force lockout window."""

    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Locked out — retry after {retry_after_seconds} seconds")


class InvalidTokenError(AppError):
    """Raised when a JWT is missing, malformed, expired, or fails verification."""


class ExternalServiceError(AppError):
    """Raised when a call to an AWS service (Secrets Manager, DynamoDB, etc.)
    fails for any reason — network blip, throttling, permission issue, the
    table not existing yet. The ORIGINAL exception (with full AWS-specific
    detail — table names, region, error codes) gets logged internally where
    only we can see it. This exception carries just a generic message so the
    client-facing response never leaks infrastructure detail, same uniform-
    messaging principle as InvalidCredentialsError, just extended to cover
    infra failures instead of auth failures."""
