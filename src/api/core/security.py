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
