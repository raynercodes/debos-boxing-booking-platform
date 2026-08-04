#!/usr/bin/env bash
set -e

# Run this FROM INSIDE your existing debos-boxing-booking-platform folder.
# Adds structured logging + AWS exception handling per protocol #1
# (error prevention, edge-case coverage, logging/monitoring by default).

echo "Applying logging + error handling updates..."

cat > src/api/core/logging_config.py << 'FILEEOF'
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
FILEEOF

cat > src/api/core/exceptions.py << 'FILEEOF'
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
FILEEOF

cat > src/api/core/security.py << 'FILEEOF'
"""
Admin authentication — JWT issuance/verification and password hashing.

Scoped down from fintech's multi-tenant auth pattern on purpose: there is
exactly one admin identity here, not a user table, so there's no separate
pepper-per-user or refresh token rotation. There IS still a dedicated pepper
secret (separate from the password hash and the JWT secret, each with its
own KMS key) — that part matters regardless of how many admins exist, since
it protects against a leaked hash+salt pair alone being crackable.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Optional

import boto3
import jwt
from botocore.exceptions import ClientError, BotoCoreError

from src.api.core.exceptions import InvalidTokenError, ExternalServiceError
from src.api.core.logging_config import get_logger

logger = get_logger(__name__)


class SecurityService:
    """Singleton, same reasoning as DynamoDBService — secrets are fetched
    from Secrets Manager once per cold start and cached for the life of the
    execution context (L1 cache)."""

    JWT_ALGORITHM = "HS256"
    JWT_EXPIRY_SECONDS = 3600  # 1 hour — admin sessions, not customer-facing, longer is fine
    PBKDF2_ITERATIONS = 600_000
    SALT_BYTES = 16

    def __init__(self) -> None:
        self._secrets_client = None
        self._jwt_secret: Optional[str] = None
        self._admin_password_hash: Optional[str] = None
        self._password_pepper: Optional[str] = None

    @property
    def secrets_client(self):
        if self._secrets_client is None:
            self._secrets_client = boto3.client("secretsmanager")
        return self._secrets_client

    def _fetch_secret(self, secret_id: str) -> dict:
        """Wraps the actual AWS call. If Secrets Manager is unreachable, the
        secret doesn't exist, or the Lambda's IAM role is missing permission,
        we log the FULL error here (table/secret names, AWS error codes —
        useful for debugging in CloudWatch) but only ever raise the generic
        ExternalServiceError upward. A client hitting a broken deploy should
        see 'something went wrong', never 'AccessDeniedException: user
        arn:aws:iam::123456789:role/... is not authorized to perform:
        secretsmanager:GetSecretValue on resource: ...' — that string alone
        would hand an attacker your account ID and role name for free."""
        try:
            response = self.secrets_client.get_secret_value(SecretId=secret_id)
            return json.loads(response["SecretString"])
        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to fetch secret '%s': %s", secret_id, exc, exc_info=True)
            raise ExternalServiceError("Unable to retrieve required configuration") from exc

    @property
    def jwt_secret(self) -> str:
        if self._jwt_secret is None:
            secret_path = os.environ["JWT_SECRET_PATH"]
            self._jwt_secret = self._fetch_secret(secret_path)["secret"]
        return self._jwt_secret

    @property
    def admin_password_hash(self) -> str:
        """Stored as base64(salt + derived_hash)."""
        if self._admin_password_hash is None:
            secret_path = os.environ["ADMIN_CREDENTIALS_PATH"]
            self._admin_password_hash = self._fetch_secret(secret_path)["password_hash"]
        return self._admin_password_hash

    @property
    def password_pepper(self) -> str:
        """Dedicated secret, separate KMS key from the JWT secret and admin
        hash — same blast-radius-minimization pattern as fintech. A pepper
        compromised together with the password hash defeats the purpose of
        having one, so it lives in its own Secrets Manager path."""
        if self._password_pepper is None:
            secret_path = os.environ["PASSWORD_PEPPER_PATH"]
            self._password_pepper = self._fetch_secret(secret_path)["pepper"]
        return self._password_pepper

    def hash_password(self, password: str, salt: Optional[bytes] = None) -> str:
        """PBKDF2-HMAC-SHA256 with salt + pepper. Salt is random per-password
        and stored alongside the hash (standard practice — salts aren't
        secret). Pepper is a separate, never-stored-with-the-hash secret, so
        a stolen hash+salt pair alone still isn't crackable."""
        if salt is None:
            salt = secrets.token_bytes(self.SALT_BYTES)
        peppered = password.encode() + self.password_pepper.encode()
        derived = hashlib.pbkdf2_hmac("sha256", peppered, salt, self.PBKDF2_ITERATIONS)
        return base64.b64encode(salt + derived).decode()

    def verify_password(self, password: str, stored_hash: str) -> bool:
        decoded = base64.b64decode(stored_hash)
        salt, expected_derived = decoded[: self.SALT_BYTES], decoded[self.SALT_BYTES :]
        peppered = password.encode() + self.password_pepper.encode()
        actual_derived = hashlib.pbkdf2_hmac("sha256", peppered, salt, self.PBKDF2_ITERATIONS)
        return hmac.compare_digest(actual_derived, expected_derived)

    def create_jwt(self) -> str:
        now = int(time.time())
        payload = {"role": "admin", "iat": now, "exp": now + self.JWT_EXPIRY_SECONDS}
        return jwt.encode(payload, self.jwt_secret, algorithm=self.JWT_ALGORITHM)

    def verify_jwt(self, token: str) -> bool:
        """Returns True/False rather than raising, so callers (like a FastAPI
        dependency) can decide how to respond. Route-layer code that wants a
        raised error should use require_admin_token() below instead."""
        try:
            payload = jwt.decode(token, self.jwt_secret, algorithms=[self.JWT_ALGORITHM])
            return payload.get("role") == "admin"
        except jwt.PyJWTError:
            return False

    def require_admin_token(self, token: str) -> None:
        """Raises InvalidTokenError if the token doesn't verify. Used by the
        FastAPI dependency in routes/bookings.py to keep the route decorator
        clean and translate to a 401 via the global exception handler."""
        if not self.verify_jwt(token):
            # Logged at WARNING, not ERROR — an invalid token on its own isn't
            # necessarily an attack (could just be an expired session), but a
            # pattern of these in CloudWatch is worth being able to spot.
            logger.warning("Rejected invalid or expired admin token")
            raise InvalidTokenError("Invalid or expired admin token")


_security_service: Optional[SecurityService] = None


def get_security_service() -> SecurityService:
    global _security_service
    if _security_service is None:
        _security_service = SecurityService()
    return _security_service
FILEEOF

cat > src/api/core/lockout.py << 'FILEEOF'
"""
Progressive brute-force lockout for admin login.

Applies regardless of whether an attacker found /auth/login via /docs or by
scanning common paths — an undocumented endpoint is not a protected one.

PHASE 2 (post-MVP, not built yet): upgrade to dual-identifier lockout matching
the fintech authorizer's pattern — track failed attempts by BOTH the admin
email AND the source IP independently. Right now this only catches an
attacker hammering the single known admin email; it does NOT catch an
attacker trying many different email guesses from one IP, since there's only
one valid email to guess correctly. Add a second LockoutManager instance
keyed by source IP (from the API Gateway event via Mangum's scope) once the
MVP is stable.
"""

import time
from dataclasses import dataclass

from botocore.exceptions import ClientError, BotoCoreError

from src.api.core.database import DynamoDBService
from src.api.core.exceptions import LockedOutError, ExternalServiceError
from src.api.core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class LockoutRecord:
    """Typed structure instead of a raw dict — a mistyped key like
    'locked_untill' now fails at construction, not silently at runtime."""

    attempts: int = 0
    locked_until: int = 0
    lockout_stage: int = 0


class LockoutManager:
    MAX_ATTEMPTS = 5
    # Progressive lockout windows in seconds — same escalation as fintech's authorizer
    LOCKOUT_WINDOWS = [15 * 60, 30 * 60, 60 * 60, 24 * 60 * 60]  # 15min, 30min, 1hr, 24hr
    LOCKOUT_KEY = "brute_force:admin_login"

    def __init__(self, db_service: DynamoDBService) -> None:
        self._db = db_service

    def _get_record(self) -> LockoutRecord:
        """Reads the current lockout state. Wrapped in try/except because a
        DynamoDB read CAN fail (throttling, table not ready, network blip) —
        and if this silently crashed instead of failing safely, it could
        either lock out the real admin forever (bad) or, worse, let lockout
        checks be bypassed entirely if the exception weren't handled at all
        (much worse — that would defeat the whole point of this module).
        We fail CLOSED here: if we can't confirm the current state, we log
        it and raise, rather than assuming 'no record' and letting a login
        attempt through unchecked."""
        try:
            result = self._db.security_table.get_item(Key={"security_key": self.LOCKOUT_KEY})
        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to read lockout record: %s", exc, exc_info=True)
            raise ExternalServiceError("Unable to verify login attempt status") from exc

        item = result.get("Item")
        if not item:
            return LockoutRecord()

        # Never trust physical TTL deletion alone — evaluate explicitly
        locked_until = item.get("locked_until", 0)
        if locked_until and locked_until < int(time.time()):
            return LockoutRecord(attempts=item.get("attempts", 0), locked_until=0,
                                  lockout_stage=item.get("lockout_stage", 0))

        return LockoutRecord(
            attempts=item.get("attempts", 0),
            locked_until=locked_until,
            lockout_stage=item.get("lockout_stage", 0),
        )

    def check_lockout(self) -> None:
        """Call before verifying a password. Raises LockedOutError if currently locked out."""
        record = self._get_record()
        now = int(time.time())
        if record.locked_until > now:
            logger.warning("Blocked login attempt — admin account currently locked out")
            raise LockedOutError(retry_after_seconds=record.locked_until - now)

    def record_failed_attempt(self) -> None:
        """Call after a failed password check. Escalates the lockout window
        each time MAX_ATTEMPTS is hit again after a previous lockout."""
        record = self._get_record()
        attempts = record.attempts + 1

        try:
            if attempts >= self.MAX_ATTEMPTS:
                window = self.LOCKOUT_WINDOWS[min(record.lockout_stage, len(self.LOCKOUT_WINDOWS) - 1)]
                locked_until = int(time.time()) + window
                self._db.security_table.put_item(Item={
                    "security_key": self.LOCKOUT_KEY,
                    "attempts": 0,
                    "lockout_stage": min(record.lockout_stage + 1, len(self.LOCKOUT_WINDOWS) - 1),
                    "locked_until": locked_until,
                    "expires_at": locked_until + 3600,  # TTL cleanup, 1hr after lockout ends
                })
                logger.warning(
                    "Admin login locked out for %s seconds after %s failed attempts",
                    window, attempts,
                )
            else:
                self._db.security_table.put_item(Item={
                    "security_key": self.LOCKOUT_KEY,
                    "attempts": attempts,
                    "lockout_stage": record.lockout_stage,
                    "locked_until": 0,
                    "expires_at": int(time.time()) + 86400,  # stale counters expire in 24hr
                })
                logger.warning("Failed admin login attempt #%s recorded", attempts)
        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to record lockout attempt: %s", exc, exc_info=True)
            raise ExternalServiceError("Unable to process login attempt") from exc

    def clear_attempts(self) -> None:
        """Call after a successful login — resets the counter and stage entirely."""
        try:
            self._db.security_table.delete_item(Key={"security_key": self.LOCKOUT_KEY})
        except (ClientError, BotoCoreError) as exc:
            # Deliberately NOT re-raised as ExternalServiceError here — this
            # runs after a password already verified successfully. Failing
            # the whole login because the cleanup step had a hiccup would be
            # a worse outcome than just leaving a stale counter behind for
            # next time (it'll self-correct via TTL anyway). Log and move on.
            logger.error("Failed to clear lockout attempts (non-fatal): %s", exc, exc_info=True)
FILEEOF

cat > src/api/routes/auth.py << 'FILEEOF'
import os
from fastapi import APIRouter
from pydantic import BaseModel

from src.api.core.security import get_security_service
from src.api.core.lockout import LockoutManager
from src.api.core.database import get_db_service
from src.api.core.exceptions import InvalidCredentialsError
from src.api.core.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter()


class AdminLoginRequest(BaseModel):
    email: str
    password: str


class AdminLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: str = "1 hour"


@router.post(
    "/login",
    response_model=AdminLoginResponse,
    summary="Admin Login",
    description="Authenticate as the gym admin to access booking management endpoints.",
)
async def admin_login(request: AdminLoginRequest):
    security = get_security_service()
    lockout = LockoutManager(get_db_service())

    # Check lockout FIRST — before touching the password at all. An attacker
    # shouldn't get a free password verification attempt while locked out.
    lockout.check_lockout()

    admin_email = os.environ["ADMIN_EMAIL"]

    # Uniform error — don't reveal whether the email or password was wrong.
    # Logging DOES distinguish which one failed (useful for us in CloudWatch
    # to spot a pattern — e.g. someone hammering the wrong email entirely vs.
    # guessing passwords against the right one) but this distinction NEVER
    # reaches the client response, only the log line. Never log the actual
    # password value itself, only that an attempt happened.
    if request.email.lower() != admin_email.lower():
        logger.warning("Failed login attempt — unrecognized email: %s", request.email)
        lockout.record_failed_attempt()
        raise InvalidCredentialsError("Invalid credentials")

    if not security.verify_password(request.password, security.admin_password_hash):
        logger.warning("Failed login attempt — wrong password for admin email")
        lockout.record_failed_attempt()
        raise InvalidCredentialsError("Invalid credentials")

    logger.info("Admin login successful")
    lockout.clear_attempts()
    return AdminLoginResponse(access_token=security.create_jwt())
FILEEOF

cat > src/api/main.py << 'FILEEOF'
import os
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mangum import Mangum

from src.api.routes import health, bookings, leads, auth
from src.api.core.exceptions import (
    AppError,
    InvalidCredentialsError,
    LockedOutError,
    InvalidTokenError,
    ExternalServiceError,
)
from src.api.core.logging_config import get_logger

logger = get_logger(__name__)

ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")
ROOT_PATH = "" if ENVIRONMENT == "prod" else f"/{ENVIRONMENT}"

app = FastAPI(
    title="Debo's Boxing and Fitness — Booking API",
    description="""
## Debo's Boxing and Fitness — Booking & Automation Platform

Serverless booking and lead capture system built on AWS Lambda, DynamoDB, SES, and EventBridge.

---

## Architecture
- **Compute:** AWS Lambda + FastAPI + Mangum
- **Database:** DynamoDB — bookings table (GSI on session date) + leads table
- **Email:** SES — automated confirmations, gym owner notifications, 24hr reminders
- **Scheduling:** EventBridge — daily reminder job
- **Deployment:** GitHub Actions CI/CD
""",
    version="1.0.0",
    root_path=ROOT_PATH,
)


# --- Global exception handlers ---------------------------------------------
# Translate domain-level exceptions (raised from service classes in src/api/core/)
# into HTTP responses in ONE place, instead of scattering try/except HTTPException
# across every route. Routes stay focused on request/response shape; error
# translation lives here.

@app.exception_handler(LockedOutError)
async def locked_out_handler(request: Request, exc: LockedOutError):
    minutes = exc.retry_after_seconds // 60
    return JSONResponse(
        status_code=429,
        content={"detail": f"Too many failed login attempts. Try again in {minutes} minute(s)."},
    )


@app.exception_handler(InvalidCredentialsError)
async def invalid_credentials_handler(request: Request, exc: InvalidCredentialsError):
    return JSONResponse(status_code=401, content={"detail": "Invalid credentials"})


@app.exception_handler(InvalidTokenError)
async def invalid_token_handler(request: Request, exc: InvalidTokenError):
    return JSONResponse(status_code=401, content={"detail": str(exc)})


@app.exception_handler(ExternalServiceError)
async def external_service_error_handler(request: Request, exc: ExternalServiceError):
    """The underlying AWS error (with all its specific detail) was already
    logged at the point it was caught, inside the service class that hit it.
    This handler's only job is to return a clean, generic response — it
    deliberately does NOT include str(exc) in the response body, unlike the
    other handlers, because ExternalServiceError messages are written for
    CloudWatch readers (us), not API clients."""
    logger.error("External service error reached the top-level handler: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Something went wrong on our end. Please try again shortly."},
    )


@app.exception_handler(AppError)
async def generic_app_error_handler(request: Request, exc: AppError):
    """Catch-all for any AppError subclass that doesn't have a specific
    handler above — ensures a new exception type added later still returns
    a sane response instead of leaking a 500."""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


app.include_router(health.router, tags=["Health"])
app.include_router(auth.router, prefix="/auth", tags=["Auth"])
app.include_router(bookings.router, prefix="/bookings", tags=["Bookings"])
app.include_router(leads.router, prefix="/leads", tags=["Leads"])

handler = Mangum(app)
FILEEOF

cat > src/api/models/booking.py << 'FILEEOF'
from datetime import datetime
from enum import Enum
from pydantic import BaseModel, EmailStr, Field, field_validator


class BookingStatus(str, Enum):
    confirmed = "confirmed"
    cancelled = "cancelled"
    completed = "completed"


class BookingRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    email: EmailStr
    phone: str = Field(..., min_length=7, max_length=20)
    session_date: str = Field(..., description="Format: YYYY-MM-DD")
    session_time: str = Field(..., description="Format: HH:MM (24hr)")

    @field_validator("session_date")
    @classmethod
    def validate_session_date(cls, value: str) -> str:
        """Without this, a malformed date (e.g. 'banana', '13/45/2026')
        would pass validation fine here, then fail confusingly later —
        either inside the DynamoDB write, or worse, inside the EventBridge
        reminder job's date matching logic where a bad value could silently
        never match anything instead of raising a clear error. Catching it
        HERE means the client gets an immediate, clear 422 at the moment
        they made the mistake, not a mysterious failure somewhere downstream."""
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise ValueError("session_date must be in YYYY-MM-DD format")
        return value

    @field_validator("session_time")
    @classmethod
    def validate_session_time(cls, value: str) -> str:
        try:
            datetime.strptime(value, "%H:%M")
        except ValueError:
            raise ValueError("session_time must be in 24-hour HH:MM format")
        return value


class BookingResponse(BaseModel):
    booking_id: str
    name: str
    email: EmailStr
    phone: str
    session_date: str
    session_time: str
    status: BookingStatus
    created_at: str
    reminder_sent: bool


# TODO (future, not MVP): if customer accounts get added later (e.g. mouthpiece
# sales / e-commerce), this is where a UserAccount model would live, separate
# from the single-admin auth in core/security.py — different identity, different
# threat model, don't retrofit the admin JWT pattern onto customer logins.
FILEEOF

echo "Files updated. Running tests to verify..."
python3 -m pip install -r requirements-dev.txt
python3 -m pytest tests/ -v

echo ""
echo "If tests pass, review before committing:"
echo "  git status"
echo "  git diff"
echo ""
echo "Then commit:"
echo "  git add ."
echo "  git commit -m 'Add structured logging and AWS exception handling per error-prevention protocol'"
echo "  git push"
