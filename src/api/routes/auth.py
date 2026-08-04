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
