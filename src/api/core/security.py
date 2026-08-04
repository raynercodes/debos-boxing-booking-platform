import base64
import hashlib
import hmac
import json
import os
import secrets
import time

import boto3
import jwt

JWT_ALGORITHM = "HS256"
JWT_EXPIRY_SECONDS = 3600  # 1 hour — admin sessions, not customer-facing, longer is fine
PBKDF2_ITERATIONS = 600_000

# Outside handler — L1 cached in Lambda execution context, only fetched on cold start
_jwt_secret = None
_admin_password_hash = None


def _get_secret(secret_id: str) -> dict:
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_id)
    return json.loads(response["SecretString"])


def get_jwt_secret() -> str:
    global _jwt_secret
    if _jwt_secret is None:
        secret_path = os.environ["JWT_SECRET_PATH"]
        _jwt_secret = _get_secret(secret_path)["secret"]
    return _jwt_secret


def get_admin_password_hash() -> str:
    """Stored as base64(salt + hash) — same format as the hash_password output below."""
    global _admin_password_hash
    if _admin_password_hash is None:
        secret_path = os.environ["ADMIN_CREDENTIALS_PATH"]
        _admin_password_hash = _get_secret(secret_path)["password_hash"]
    return _admin_password_hash


def hash_password(password: str, salt: bytes = None) -> str:
    """PBKDF2-HMAC-SHA256. No separate pepper key — single admin credential,
    not a multi-tenant user table, so the added KMS key doesn't buy much here."""
    if salt is None:
        salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return base64.b64encode(salt + derived).decode()


def verify_password(password: str, stored_hash: str) -> bool:
    decoded = base64.b64decode(stored_hash)
    salt, expected_derived = decoded[:16], decoded[16:]
    actual_derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return hmac.compare_digest(actual_derived, expected_derived)


def create_jwt() -> str:
    now = int(time.time())
    payload = {
        "role": "admin",
        "iat": now,
        "exp": now + JWT_EXPIRY_SECONDS,
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)


def verify_jwt(token: str) -> bool:
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
        return payload.get("role") == "admin"
    except jwt.PyJWTError:
        return False
