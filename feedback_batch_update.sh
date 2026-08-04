#!/usr/bin/env bash
set -e

# Run this FROM INSIDE your existing debos-boxing-booking-platform folder.
# Covers: phone validation, name validation, salt size, cancel-booking
# endpoint, history-visibility filter, trimmed docs, health router prefix
# restructure, dynamic lockout message.

echo "Applying feedback batch updates..."

cat > src/api/models/booking.py << 'FILEEOF'
import re
from datetime import datetime
from enum import Enum
from pydantic import BaseModel, EmailStr, Field, field_validator

# E.164-ish: optional leading +, then 7-15 digits. Covers US numbers with or
# without country code, and international numbers, without being so strict
# it rejects real formats. Kept as a module-level constant so both booking.py
# and lead.py can share the exact same rule instead of drifting apart.
PHONE_PATTERN = re.compile(r"^\+?[0-9]{7,15}$")


class BookingStatus(str, Enum):
    confirmed = "confirmed"
    cancelled = "cancelled"
    completed = "completed"


class BookingRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    email: EmailStr
    # Deliberately a str, not an int — phone numbers aren't numbers you do
    # math on. An int silently drops leading zeros and can't represent a
    # '+' country-code prefix at all. Format is enforced via regex instead,
    # which catches garbage input without the downsides of int.
    phone: str = Field(..., min_length=7, max_length=20)
    session_date: str = Field(..., description="Format: YYYY-MM-DD")
    session_time: str = Field(..., description="Format: HH:MM (24hr)")

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name cannot be blank or whitespace only")
        return stripped

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        cleaned = value.strip()
        if not PHONE_PATTERN.match(cleaned):
            raise ValueError("phone must be 7-15 digits, optionally prefixed with +")
        return cleaned

    @field_validator("session_date")
    @classmethod
    def validate_session_date(cls, value: str) -> str:
        """The Framer frontend will only ever present the gym's real
        pre-configured available slots — a client can't type a free-text
        date. But this validator stays regardless: never trust client-side
        constraints as the ONLY line of defense. A direct API call (someone
        testing with curl/Postman, or a bug in the frontend) bypasses
        whatever the UI restricts, so the backend has to independently
        enforce the same rule no matter how the request arrives. Format-only
        here — validating it's an ACTUAL configured slot (not just a
        correctly-shaped date) is a separate, later check once the gym's
        real schedule/availability data exists."""
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

cat > src/api/models/lead.py << 'FILEEOF'
from pydantic import BaseModel, EmailStr, Field, field_validator
from typing import Optional

from src.api.models.booking import PHONE_PATTERN


class LeadRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    email: EmailStr
    phone: str = Field(..., min_length=7, max_length=20)
    interest_note: Optional[str] = Field(None, max_length=500)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name cannot be blank or whitespace only")
        return stripped

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        cleaned = value.strip()
        if not PHONE_PATTERN.match(cleaned):
            raise ValueError("phone must be 7-15 digits, optionally prefixed with +")
        return cleaned


class LeadResponse(BaseModel):
    lead_id: str
    name: str
    email: EmailStr
    phone: str
    interest_note: Optional[str] = None
    created_at: str
    converted: bool
FILEEOF

cat > src/api/routes/bookings.py << 'FILEEOF'
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, Query
from fastapi.security import HTTPBearer

from src.api.models.booking import BookingRequest, BookingResponse, BookingStatus
from src.api.core.security import get_security_service
from src.api.core.exceptions import AppError
from src.api.core.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter()
bearer_scheme = HTTPBearer()

VALID_DAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}

# How long a booking stays visible in the admin list after its start time has
# passed. Deliberately a DISPLAY filter applied at query time, NOT a deletion
# policy — the record stays in DynamoDB permanently (useful for history,
# case-study metrics, and the gym owner's own records). This mirrors the
# fintech project's TTL lesson: never rely on a timestamp-based mechanism to
# physically remove something on a tight schedule — DynamoDB TTL deletion can
# lag up to 48 hours, which would be useless for a "gone within an hour" rule.
# Explicit evaluation in application code, every time, is the only way to
# guarantee this window actually holds.
HISTORY_VISIBILITY_WINDOW = timedelta(hours=1)


class BookingNotFoundError(AppError):
    """A specific 404 case — kept as its own type rather than reusing a
    generic AppError so the exception handler in main.py can map it to 404
    specifically instead of the catch-all AppError's 400."""


def require_admin(credentials=Depends(bearer_scheme)):
    """Delegates to SecurityService, which raises InvalidTokenError on
    failure — translated to a 401 by the global exception handler in
    main.py, so this stays a one-line dependency."""
    get_security_service().require_admin_token(credentials.credentials)


@router.post(
    "/",
    response_model=BookingResponse,
    status_code=201,
    summary="Create Booking",
    description="Submit a new session booking. DynamoDB write + SES confirmation "
                "are TODO until infrastructure/template.yaml is deployed.",
)
async def create_booking(request: BookingRequest):
    # TODO: write to debos-boxing-bookings table once infrastructure/template.yaml is deployed
    # TODO: trigger SES confirmation email + gym owner notification
    logger.info("Booking created for session %s %s", request.session_date, request.session_time)
    return BookingResponse(
        booking_id=str(uuid.uuid4()),
        name=request.name,
        email=request.email,
        phone=request.phone,
        session_date=request.session_date,
        session_time=request.session_time,
        status=BookingStatus.confirmed,
        created_at=datetime.now(timezone.utc).isoformat(),
        reminder_sent=False,
    )


@router.get(
    "/",
    summary="List Upcoming Bookings (Admin)",
    description="Admin-only. Filters: day_of_week, search (name/phone). "
                "Auto-excludes bookings more than 1hr past their start time.",
    dependencies=[Depends(require_admin)],
)
async def list_bookings(
    day_of_week: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
):
    if day_of_week and day_of_week.lower() not in VALID_DAYS:
        raise AppError(f"day_of_week must be one of {sorted(VALID_DAYS)}")

    # TODO — real implementation once the table + GSI exist:
    #
    # 1. Build today..today+6 date list (Python, not a DynamoDB feature):
    #      today = datetime.now(timezone.utc).date()
    #      week_dates = [(today + timedelta(days=i)).isoformat() for i in range(7)]
    #
    # 2. The session-date-index GSI's partition key is an EXACT date, so a
    #    single Query can't span a date range — issue one Query per date in
    #    week_dates (max 7 calls), merge results. Explicit per-date queries
    #    over a Scan, same "no table scans" instinct as fintech.
    #
    # 3. If day_of_week given, only query the ONE matching date instead of all 7.
    #
    # 4. If search given, filter merged results in Python on name/phone
    #    (case-insensitive substring match) — not worth a GSI at this volume.
    #
    # 5. HISTORY FILTER (the "disappear after an hour" requirement) — for each
    #    result, compute:
    #      session_start = datetime.fromisoformat(f"{item['session_date']}T{item['session_time']}")
    #      if datetime.now(timezone.utc) - session_start > HISTORY_VISIBILITY_WINDOW:
    #          exclude from results  # still exists in DynamoDB, just not shown here
    #    This is evaluated HERE, at read time, every single call — never
    #    trust a stored "is_visible" flag that would need a separate process
    #    to keep updated. Same explicit-evaluation principle as TTL handling.
    #
    # 6. Sort merged results by (session_date, session_time) ascending — today's
    #    soonest session lands first automatically since week_dates starts at today.
    return []


@router.get(
    "/{booking_id}",
    response_model=BookingResponse,
    summary="Get Booking by ID",
)
async def get_booking(booking_id: str):
    # TODO: get_item from debos-boxing-bookings table
    raise BookingNotFoundError(f"Booking {booking_id} not found")


@router.patch(
    "/{booking_id}/cancel",
    response_model=BookingResponse,
    summary="Cancel Booking (Admin)",
    description="Admin-only. Cancels a booking at any time regardless of how "
                "close it is to the session start.",
    dependencies=[Depends(require_admin)],
)
async def cancel_booking(booking_id: str):
    # TODO once the table exists:
    #   1. get_item(booking_id) — raise BookingNotFoundError if missing
    #   2. update_item setting status = "cancelled"
    #      NOTE: unlike fintech's loan status transitions, this does NOT need
    #      a ConditionExpression restricting which prior status is valid —
    #      the requirement is explicitly "cancel at any time," so a confirmed,
    #      already-completed, or even already-cancelled booking can all be
    #      set to cancelled without error. No state machine restriction here,
    #      on purpose, matching what was actually asked for.
    #   3. TODO (future): trigger a cancellation notice email via SES
    logger.info("Booking %s cancelled by admin", booking_id)
    raise BookingNotFoundError(f"Booking {booking_id} not found")
FILEEOF

cat > src/api/routes/leads.py << 'FILEEOF'
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter
from src.api.models.lead import LeadRequest, LeadResponse

router = APIRouter()


@router.post(
    "/",
    response_model=LeadResponse,
    status_code=201,
    summary="Capture Lead",
    description="Capture someone interested but not ready to book yet. "
                "DynamoDB write is TODO until infrastructure/template.yaml is deployed.",
)
async def create_lead(request: LeadRequest):
    # TODO: write to debos-boxing-leads table once infrastructure/template.yaml is deployed
    return LeadResponse(
        lead_id=str(uuid.uuid4()),
        name=request.name,
        email=request.email,
        phone=request.phone,
        interest_note=request.interest_note,
        created_at=datetime.now(timezone.utc).isoformat(),
        converted=False,
    )
FILEEOF

cat > src/api/routes/health.py << 'FILEEOF'
from fastapi import APIRouter
from datetime import datetime, timezone

router = APIRouter()


@router.get("", summary="Health Check")
async def health_check():
    """Basic liveness check — confirms the Lambda is running and responsive."""
    return {
        "status": "healthy",
        "service": "debos-boxing-and-booking-platform",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
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
from src.api.routes.bookings import BookingNotFoundError
from src.api.core.logging_config import get_logger

logger = get_logger(__name__)

ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")
ROOT_PATH = "" if ENVIRONMENT == "prod" else f"/{ENVIRONMENT}"

# /docs isn't the real testing surface for this project — Framer is the
# actual frontend, and /docs is closer to a staging/dev convenience for us
# than a demo interface for anyone else. Keeping the description short on
# purpose instead of the heavy walkthrough-style docs fintech has, since
# nobody but us is expected to open this page.
app = FastAPI(
    title="Debo's Boxing and Fitness — Booking Platform",
    description="Booking and lead automation backend for Debo's Boxing and Fitness. "
                "Internal/staging use — the real frontend is the Framer site.",
    version="1.0.0",
    root_path=ROOT_PATH,
)


# --- Global exception handlers ---------------------------------------------
# Translate domain-level exceptions (raised from service classes in src/api/core/)
# into HTTP responses in ONE place, instead of scattering try/except HTTPException
# across every route. Routes stay focused on request/response shape; error
# translation lives here.

def _format_retry_after(seconds: int) -> str:
    """Renders a lockout duration as minutes for short windows or hours for
    long ones — a 24-hour lockout showing '1440 minute(s)' is technically
    correct but unreadable and unhelpful to whoever's staring at that error
    message. Anything under 60 minutes shows minutes; 60 and up shows hours,
    rounded up so a 61-minute wait says '2 hours' rather than '1 hours'
    (which would understate how long is actually left)."""
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute(s)"
    hours = (minutes + 59) // 60  # ceiling division, not floor
    return f"{hours} hour(s)"


@app.exception_handler(LockedOutError)
async def locked_out_handler(request: Request, exc: LockedOutError):
    wait_str = _format_retry_after(exc.retry_after_seconds)
    return JSONResponse(
        status_code=429,
        content={"detail": f"Too many failed login attempts. Try again in {wait_str}."},
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


@app.exception_handler(BookingNotFoundError)
async def booking_not_found_handler(request: Request, exc: BookingNotFoundError):
    """Registered BEFORE the generic AppError handler below, but order
    doesn't actually matter to FastAPI/Starlette — handler lookup walks the
    exception's MRO to find the most specific registered type regardless of
    registration order. Listed here in logical proximity to its sibling
    error handlers rather than for any ordering reason."""
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(AppError)
async def generic_app_error_handler(request: Request, exc: AppError):
    """Catch-all for any AppError subclass that doesn't have a specific
    handler above — ensures a new exception type added later still returns
    a sane response instead of leaking a 500."""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# health.py declares its route at "/" internally and gets its "/health" path
# from the prefix here — same consistent pattern as auth/bookings/leads below,
# where each router only knows its own relative paths and main.py assembles
# the real URL structure. Previously health.py hardcoded "/health" itself
# while also having no prefix here, which worked but was the odd one out.
app.include_router(health.router, prefix="/health", tags=["Health"])
app.include_router(auth.router, prefix="/auth", tags=["Auth"])
app.include_router(bookings.router, prefix="/bookings", tags=["Bookings"])
app.include_router(leads.router, prefix="/leads", tags=["Leads"])

handler = Mangum(app)
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
    SALT_BYTES = 32

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

cat > tests/test_health.py << 'FILEEOF'
from fastapi.testclient import TestClient
from src.api.main import app

client = TestClient(app)


def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["service"] == "debos-boxing-and-booking-platform"
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
echo "  git commit -m 'Add phone/name validation, cancel endpoint, history filter, docs trim, health prefix fix'"
echo "  git push"
