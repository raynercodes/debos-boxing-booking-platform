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
    """Base class for all domain-level errors in this project."""


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


class BookingNotFoundError(AppError):
    """A specific 404 case — kept as its own type rather than reusing a
    generic AppError so the exception handler in main.py can map it to 404
    specifically instead of the catch-all AppError's 400."""


class BookingAlreadyCancelledError(AppError):
    """409 (Conflict). Confirmed requirement: the UI should only ever offer
    "cancel" as an option when a booking ISN'T already cancelled — but per
    the same defense-in-depth principle applied everywhere else, the
    backend enforces this independently too, not just the frontend. This is
    also what prevents a real duplicate-email bug: once cancellation emails
    are wired in (to both Debo and the client), a second cancel attempt on
    an already-cancelled booking must NOT re-trigger those emails."""


class SlotProcessingError(AppError):
    """409 — a Personal slot is currently being checked out by someone
    else. Confirmed UX: 'this booking is currently being booked, it might
    be available soon, try again later' — the person hasn't fully lost the
    slot yet (the other checkout could still expire), just not right now."""


class SlotTakenError(AppError):
    """409 — a Personal slot is already CONFIRMED (paid) by someone else.
    Confirmed UX: distinct message from SlotProcessingError — 'sorry, this
    booking is taken, try another day or time' — this one is final, not a
    'try again shortly' situation."""


class TooManyProcessingBookingsError(AppError):
    """409 — this same client already has a booking sitting in
    'processing' (checkout started, not yet paid or expired). Prevents
    one person from stacking multiple simultaneous in-progress bookings.
    Identified by request IP — a known, honest tradeoff: people sharing a
    network (family on the same wifi, coworkers on the same office
    connection) could trip this even though they're genuinely different
    customers. Accepted because the error message gives a clear path
    forward (check email to finish the existing one) rather than a hard
    dead end."""


class IPBlockedError(AppError):
    """403 — this specific IP has been manually added to the blocklist
    (see core/ip_blocklist.py), typically after repeated brute-force
    lockouts against the admin login. 403, not the generic 400, since
    this is genuinely a permissions/access issue, not a malformed
    request. Deliberately vague message — never confirms to an attacker
    that their IP specifically is what's being blocked."""


class WebhookSignatureError(AppError):
    """400, not 401/403 — Stripe's own webhook documentation expects a 4xx
    on signature failure so it knows to stop retrying that specific event
    rather than hammering the endpoint indefinitely. Kept as its own type
    for the same reason every other specific error case in this project is:
    a precise, intentional HTTP response rather than falling through to a
    generic one."""
