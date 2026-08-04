import os
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.api.core.security import verify_password, get_admin_password_hash, create_jwt
from src.api.core.lockout import check_lockout, record_failed_attempt, clear_attempts

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
    # Check lockout FIRST — before touching the password at all. An attacker
    # shouldn't get a free password verification attempt while locked out.
    check_lockout()

    admin_email = os.environ["ADMIN_EMAIL"]

    # Uniform error — don't reveal whether the email or password was wrong
    if request.email.lower() != admin_email.lower():
        record_failed_attempt()
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not verify_password(request.password, get_admin_password_hash()):
        record_failed_attempt()
        raise HTTPException(status_code=401, detail="Invalid credentials")

    clear_attempts()
    return AdminLoginResponse(access_token=create_jwt())
