#!/usr/bin/env bash
set -e

# Run this FROM INSIDE your existing debos-boxing-booking-platform folder
# (the one with git history already initialized). This only touches the
# files that changed in the refactor — it does NOT recreate the repo.

echo "Applying refactor to core modules..."

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
FILEEOF

cat > src/api/core/database.py << 'FILEEOF'
"""
DynamoDB access layer.

DynamoDBService is a singleton — get_db_service() always returns the same
instance within a single Lambda execution context, so table connections and
resource clients persist across warm invocations exactly like the old
module-level globals did. The difference is the state now lives inside an
object with a clear interface, instead of scattered module-level variables.
"""

import os
from typing import Optional

import boto3


class DynamoDBService:
    """Lazily-initialized DynamoDB table access, cached for the life of this
    Lambda execution context (L1 cache — dies with the container, persists
    across warm invocations)."""

    def __init__(self) -> None:
        self._resource = None
        self._bookings_table = None
        self._leads_table = None
        self._security_table = None

    @property
    def resource(self):
        if self._resource is None:
            self._resource = boto3.resource("dynamodb")
        return self._resource

    @property
    def bookings_table(self):
        if self._bookings_table is None:
            table_name = os.environ["BOOKINGS_TABLE_NAME"]
            self._bookings_table = self.resource.Table(table_name)
        return self._bookings_table

    @property
    def leads_table(self):
        if self._leads_table is None:
            table_name = os.environ["LEADS_TABLE_NAME"]
            self._leads_table = self.resource.Table(table_name)
        return self._leads_table

    @property
    def security_table(self):
        """Small dedicated table — brute force lockout tracking only. Not a
        general cache table like fintech's, because this project doesn't need
        L2 caching at gym scale. Single-purpose and named accordingly avoids
        scope creep into a cache layer nothing here needs yet."""
        if self._security_table is None:
            table_name = os.environ["SECURITY_TABLE_NAME"]
            self._security_table = self.resource.Table(table_name)
        return self._security_table


_db_service: Optional[DynamoDBService] = None


def get_db_service() -> DynamoDBService:
    """Module-level singleton accessor. Only one DynamoDBService instance
    exists per Lambda execution context — this function just hands back
    the same one every time it's called within that context."""
    global _db_service
    if _db_service is None:
        _db_service = DynamoDBService()
    return _db_service
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

from src.api.core.exceptions import InvalidTokenError


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
        response = self.secrets_client.get_secret_value(SecretId=secret_id)
        return json.loads(response["SecretString"])

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

from src.api.core.database import DynamoDBService
from src.api.core.exceptions import LockedOutError


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
        result = self._db.security_table.get_item(Key={"security_key": self.LOCKOUT_KEY})
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
            raise LockedOutError(retry_after_seconds=record.locked_until - now)

    def record_failed_attempt(self) -> None:
        """Call after a failed password check. Escalates the lockout window
        each time MAX_ATTEMPTS is hit again after a previous lockout."""
        record = self._get_record()
        attempts = record.attempts + 1

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
        else:
            self._db.security_table.put_item(Item={
                "security_key": self.LOCKOUT_KEY,
                "attempts": attempts,
                "lockout_stage": record.lockout_stage,
                "locked_until": 0,
                "expires_at": int(time.time()) + 86400,  # stale counters expire in 24hr
            })

    def clear_attempts(self) -> None:
        """Call after a successful login — resets the counter and stage entirely."""
        self._db.security_table.delete_item(Key={"security_key": self.LOCKOUT_KEY})
FILEEOF

cat > src/api/routes/auth.py << 'FILEEOF'
import os
from fastapi import APIRouter
from pydantic import BaseModel

from src.api.core.security import get_security_service
from src.api.core.lockout import LockoutManager
from src.api.core.database import get_db_service
from src.api.core.exceptions import InvalidCredentialsError

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

    # Uniform error — don't reveal whether the email or password was wrong
    if request.email.lower() != admin_email.lower():
        lockout.record_failed_attempt()
        raise InvalidCredentialsError("Invalid credentials")

    if not security.verify_password(request.password, security.admin_password_hash):
        lockout.record_failed_attempt()
        raise InvalidCredentialsError("Invalid credentials")

    lockout.clear_attempts()
    return AdminLoginResponse(access_token=security.create_jwt())
FILEEOF

cat > src/api/routes/bookings.py << 'FILEEOF'
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.security import HTTPBearer

from src.api.models.booking import BookingRequest, BookingResponse, BookingStatus
from src.api.core.security import get_security_service

router = APIRouter()
bearer_scheme = HTTPBearer()

VALID_DAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}


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
    description="""
Submit a new session booking.

📋 **Fill in the form fields** — name, email, phone, session date, and time

▶️ **Click Execute** — a confirmation email is sent automatically once wired to SES

⏳ **Note:** DynamoDB write and SES integration are not yet implemented — this currently
returns a mock response so the API contract is locked in before infrastructure is built.
""",
)
async def create_booking(request: BookingRequest):
    # TODO: write to debos-boxing-bookings table once infrastructure/template.yaml is deployed
    # TODO: trigger SES confirmation email + gym owner notification
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
    description="""
Returns bookings for the upcoming 7-day window (today through today+6), sorted
soonest-first by default. **Admin-only endpoint.**

🔐 Requires a valid admin token — obtain one via `POST /auth/login`, then click
**Authorize** above and paste it in.

**Optional filters:**
- `day_of_week` — narrow to a specific day (e.g. `monday`) within the current week window
- `search` — match against client name or phone number
""",
    dependencies=[Depends(require_admin)],
)
async def list_bookings(
    day_of_week: Optional[str] = Query(
        None, description="e.g. 'monday' — filters within the current 7-day window"
    ),
    search: Optional[str] = Query(
        None, description="Matches against client name or phone number"
    ),
):
    if day_of_week and day_of_week.lower() not in VALID_DAYS:
        raise HTTPException(status_code=422, detail=f"day_of_week must be one of {sorted(VALID_DAYS)}")

    # TODO — real implementation once the table + GSI exist:
    #
    # 1. Build today..today+6 date list (Python, not a DynamoDB feature):
    #      today = datetime.now(timezone.utc).date()
    #      week_dates = [(today + timedelta(days=i)).isoformat() for i in range(7)]
    #
    # 2. The session-date-index GSI's partition key is an EXACT date, so a
    #    single Query can't span a date range — issue one Query per date in
    #    week_dates (max 7 calls), merge results. This is intentional: explicit
    #    per-date queries over a Scan, same "no table scans" instinct as fintech,
    #    and 7 calls at gym scale is trivial cost.
    #
    # 3. If day_of_week given, only query the ONE matching date instead of all 7
    #    (compute which date in week_dates falls on that weekday first).
    #
    # 4. If search given, filter the merged results in Python on name/phone
    #    (case-insensitive substring match) — not worth a GSI for this at this
    #    data volume; a dedicated search index would be overengineering here.
    #
    # 5. Sort merged results by (session_date, session_time) ascending — this
    #    naturally puts today's soonest session first with no extra logic,
    #    since week_dates already starts at today.
    return []


@router.get(
    "/{booking_id}",
    response_model=BookingResponse,
    summary="Get Booking by ID",
)
async def get_booking(booking_id: str):
    # TODO: get_item from debos-boxing-bookings table
    raise HTTPException(status_code=404, detail="Booking not found")
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
)

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

echo "Files updated. Running tests to verify..."
python3 -m pip install -r requirements-dev.txt
python3 -m pytest tests/ -v

echo ""
echo "If tests pass, review the changes before committing:"
echo "  git status"
echo "  git diff"
echo ""
echo "Then commit:"
echo "  git add ."
echo "  git commit -m 'Refactor core modules into service classes with typed records and domain exceptions'"
echo "  git push"
