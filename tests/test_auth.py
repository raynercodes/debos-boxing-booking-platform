from fastapi.testclient import TestClient

from src.api.main import app
from tests.conftest import TEST_ADMIN_PASSWORD, TEST_ENV_VARS

client = TestClient(app)

ADMIN_EMAIL = TEST_ENV_VARS["ADMIN_EMAIL"]


def test_login_success(mock_aws_infra):
    response = client.post("/auth/login", json={"email": ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD})
    assert response.status_code == 200
    body = response.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"


def test_login_wrong_password(mock_aws_infra):
    response = client.post("/auth/login", json={"email": ADMIN_EMAIL, "password": "totally-wrong"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid credentials"


def test_login_wrong_email(mock_aws_infra):
    response = client.post(
        "/auth/login", json={"email": "not-the-admin@example.com", "password": TEST_ADMIN_PASSWORD}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid credentials"


def test_login_wrong_email_and_wrong_password_return_identical_response(mock_aws_infra):
    """Uniform error messaging — the response body must be byte-for-byte
    identical whether the EMAIL was wrong or the PASSWORD was wrong. If
    these ever diverged, an attacker could use the difference to enumerate
    which part of a guess was correct."""
    wrong_email_response = client.post(
        "/auth/login", json={"email": "nope@example.com", "password": TEST_ADMIN_PASSWORD}
    )
    wrong_password_response = client.post(
        "/auth/login", json={"email": ADMIN_EMAIL, "password": "nope"}
    )
    assert wrong_email_response.status_code == wrong_password_response.status_code == 401
    assert wrong_email_response.json() == wrong_password_response.json()


def test_login_lockout_after_max_failed_attempts(mock_aws_infra):
    """MAX_ATTEMPTS is 5. The 5th failing attempt still returns 401 for
    itself (its own password genuinely was wrong) — but it's also what
    records the lockout. The lockout then takes effect starting with the
    NEXT (6th) request, which should be blocked with 429 regardless of
    what credentials it carries."""
    for _ in range(5):
        response = client.post("/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong"})
        assert response.status_code == 401

    sixth_attempt = client.post("/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong"})
    assert sixth_attempt.status_code == 429
    assert "minute" in sixth_attempt.json()["detail"]


def test_lockout_blocks_even_the_correct_password(mock_aws_infra):
    """THE important one — this is what actually proves check_lockout() runs
    BEFORE password verification, not after. If lockout were checked in the
    wrong order (or not at all once locked), a locked-out attacker who
    eventually guessed the right password would still get in. This test
    fails loudly if that fail-closed ordering is ever broken."""
    for _ in range(5):
        client.post("/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong"})

    # Now try the ACTUAL correct password while still locked out
    response = client.post("/auth/login", json={"email": ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD})
    assert response.status_code == 429


def test_login_success_clears_prior_failed_attempts(mock_aws_infra):
    """A successful login should reset the counter — otherwise a legitimate
    admin who mistyped their password a couple times, then logged in
    correctly, would be closer to lockout than they should be on their next
    genuine mistake."""
    for _ in range(3):
        client.post("/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong"})

    success = client.post("/auth/login", json={"email": ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD})
    assert success.status_code == 200

    # 4 more wrong attempts after the reset should NOT trigger lockout yet —
    # if the counter hadn't actually been cleared, this would be attempt
    # #4-7 overall and would have already locked at #5.
    for _ in range(4):
        response = client.post("/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong"})
        assert response.status_code == 401  # not 429 — still not locked


def test_protected_endpoint_rejects_missing_token(mock_aws_infra):
    response = client.get("/bookings")
    assert response.status_code in (401, 403)  # HTTPBearer's own missing-header response is 403


def test_protected_endpoint_rejects_malformed_token(mock_aws_infra):
    response = client.get("/bookings", headers={"Authorization": "Bearer this-is-not-a-real-jwt"})
    assert response.status_code == 401


def test_protected_endpoint_rejects_token_signed_with_wrong_secret(mock_aws_infra):
    """A token that's structurally a valid JWT but signed with the WRONG
    secret must still be rejected — confirms verify_jwt actually checks the
    signature, not just that the string parses as a JWT shape."""
    import jwt as pyjwt
    fake_token = pyjwt.encode({"role": "admin"}, "wrong-secret-entirely", algorithm="HS256")
    response = client.get("/bookings", headers={"Authorization": f"Bearer {fake_token}"})
    assert response.status_code == 401
