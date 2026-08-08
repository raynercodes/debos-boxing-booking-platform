import os
from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPBearer
from pydantic import BaseModel

from src.api.core.security import get_security_service
from src.api.core.lockout import LockoutManager
from src.api.core.ip_blocklist import IPBlocklist
from src.api.core.database import get_db_service
from src.api.core.exceptions import InvalidCredentialsError, IPBlockedError
from src.api.core.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter()
bearer_scheme = HTTPBearer()


def get_client_ip(http_request: Request) -> str:
    """Same dependency pattern as bookings.py — extracted so tests can
    override the simulated IP via app.dependency_overrides, since
    TestClient itself can't fake different client IPs directly."""
    return http_request.client.host if http_request.client else "unknown"


def require_admin(credentials=Depends(bearer_scheme)):
    """Same pattern as bookings.py's require_admin — delegates to
    SecurityService, which raises InvalidTokenError on failure,
    translated to a 401 by the global exception handler in main.py."""
    get_security_service().require_admin_token(credentials.credentials)


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
async def admin_login(request: AdminLoginRequest, client_ip: str = Depends(get_client_ip)):
    # Manual IP blocklist checked FIRST, before anything else — a
    # previously-flagged/manually-banned IP shouldn't even get a chance
    # to attempt the lockout system at all. Generic rejection, deliberately
    # revealing nothing about WHY (same uniform-messaging principle used
    # everywhere else in this file).
    blocklist = IPBlocklist(get_db_service())
    if blocklist.is_blocked(client_ip):
        logger.warning("Rejected login attempt from blocked IP: %s", client_ip)
        raise IPBlockedError("Access denied.")

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
        lockout.record_failed_attempt(client_ip)
        raise InvalidCredentialsError("Invalid credentials")

    if not security.verify_password(request.password, security.admin_password_hash):
        logger.warning("Failed login attempt — wrong password for admin email")
        lockout.record_failed_attempt(client_ip)
        raise InvalidCredentialsError("Invalid credentials")

    logger.info("Admin login successful")
    lockout.clear_attempts()
    return AdminLoginResponse(access_token=security.create_jwt())


class BlockedIPResponse(BaseModel):
    blocked_ips: list[str]


@router.get(
    "/blocked-ips",
    response_model=BlockedIPResponse,
    summary="List Blocked IPs (Admin)",
    dependencies=[Depends(require_admin)],
)
async def list_blocked_ips():
    blocklist = IPBlocklist(get_db_service())
    return BlockedIPResponse(blocked_ips=blocklist.list_blocked())


@router.post(
    "/blocked-ips/{ip_address}",
    summary="Block an IP (Admin)",
    description="Manually ban an IP from reaching the admin login entirely — "
                "typically used after the security alert email flags repeated "
                "lockouts from the same address.",
    dependencies=[Depends(require_admin)],
)
async def block_ip(ip_address: str):
    blocklist = IPBlocklist(get_db_service())
    blocklist.add_ip(ip_address)
    return {"status": "blocked", "ip_address": ip_address}


@router.delete(
    "/blocked-ips/{ip_address}",
    summary="Unblock an IP (Admin)",
    dependencies=[Depends(require_admin)],
)
async def unblock_ip(ip_address: str):
    blocklist = IPBlocklist(get_db_service())
    blocklist.remove_ip(ip_address)
    return {"status": "unblocked", "ip_address": ip_address}
