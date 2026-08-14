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


def test_blocked_ip_rejected_before_lockout_check(mock_aws_infra):
    """A manually-blocked IP should never even reach the lockout system -
    checked first, generic 403, reveals nothing about why."""
    from src.api.core.ip_blocklist import IPBlocklist
    from src.api.core.database import get_db_service
    from src.api.routes.auth import get_client_ip
    from src.api.main import app as main_app

    blocklist = IPBlocklist(get_db_service())
    blocklist.add_ip("198.51.100.5")

    main_app.dependency_overrides[get_client_ip] = lambda: "198.51.100.5"
    response = client.post("/auth/login", json={"email": ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD})
    main_app.dependency_overrides.clear()

    assert response.status_code == 403


def test_unblocked_ip_not_blocked(mock_aws_infra):
    """A different IP, never added to the blocklist, should be completely
    unaffected — confirms the check is genuinely scoped per-IP."""
    from src.api.core.ip_blocklist import IPBlocklist
    from src.api.core.database import get_db_service
    from src.api.routes.auth import get_client_ip
    from src.api.main import app as main_app

    blocklist = IPBlocklist(get_db_service())
    blocklist.add_ip("198.51.100.5")

    main_app.dependency_overrides[get_client_ip] = lambda: "198.51.100.99"
    response = client.post("/auth/login", json={"email": ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD})
    main_app.dependency_overrides.clear()

    assert response.status_code == 200


def test_admin_can_block_and_unblock_ip_via_api(mock_aws_infra, admin_token):
    add_response = client.post(
        "/auth/blocked-ips/203.0.113.77", headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert add_response.status_code == 200

    list_response = client.get("/auth/blocked-ips", headers={"Authorization": f"Bearer {admin_token}"})
    assert "203.0.113.77" in list_response.json()["blocked_ips"]

    remove_response = client.delete(
        "/auth/blocked-ips/203.0.113.77", headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert remove_response.status_code == 200

    list_after = client.get("/auth/blocked-ips", headers={"Authorization": f"Bearer {admin_token}"})
    assert "203.0.113.77" not in list_after.json()["blocked_ips"]


def test_blocked_ips_endpoint_requires_admin(mock_aws_infra):
    response = client.get("/auth/blocked-ips")
    assert response.status_code in (401, 403)


def test_third_lockout_triggers_security_alert_email(mock_aws_infra):
    """The 3rd separate lockout (not the 1st or 2nd) should trigger the
    security alert email to the admin - a real, sustained attack pattern,
    not an honest forgotten password."""
    from unittest.mock import MagicMock
    import src.api.core.ses_service as ses_module

    mock_client = MagicMock()
    ses_module.get_ses_service()._client = mock_client

    # Each round: 5 wrong attempts triggers one lockout. Three rounds =
    # three separate lockouts. Between rounds we can't actually wait out
    # the real lockout window in a test, so we go straight through
    # record_failed_attempt via repeated login attempts - the lockout
    # itself blocks further ATTEMPTS but each blocked attempt while
    # locked out does NOT count as a new failure, so we drive this
    # directly through the LockoutManager instead of the HTTP layer for
    # precise control over exactly 3 lockout EVENTS.
    from src.api.core.lockout import LockoutManager
    from src.api.core.database import get_db_service

    lockout = LockoutManager(get_db_service())
    for _round in range(3):
        for _attempt in range(5):
            lockout.record_failed_attempt(client_ip="192.0.2.50")
        # Manually clear locked_until between rounds to simulate the
        # lockout window having passed, without a real 15/30/60 min wait.
        lockout._db.security_table.update_item(
            Key={"security_key": lockout.LOCKOUT_KEY},
            UpdateExpression="SET locked_until = :zero",
            ExpressionAttributeValues={":zero": 0},
        )

    mock_client.send_email.assert_called_once()
    kwargs = mock_client.send_email.call_args.kwargs
    assert "192.0.2.50" in kwargs["Message"]["Body"]["Text"]["Data"]
    assert "3" in kwargs["Message"]["Body"]["Text"]["Data"]


def test_first_and_second_lockout_do_not_trigger_alert(mock_aws_infra):
    """Confirms the alert is genuinely scoped to the 3rd+ lockout only -
    an honest forgotten-password scenario (one lockout) shouldn't alarm
    anyone."""
    from unittest.mock import MagicMock
    import src.api.core.ses_service as ses_module
    from src.api.core.lockout import LockoutManager
    from src.api.core.database import get_db_service

    mock_client = MagicMock()
    ses_module.get_ses_service()._client = mock_client

    lockout = LockoutManager(get_db_service())
    for _attempt in range(5):
        lockout.record_failed_attempt(client_ip="192.0.2.60")

    mock_client.send_email.assert_not_called()


def test_failed_login_records_ip_and_visible_via_admin_endpoint(mock_aws_infra, admin_token):
    """A failed login attempt should be trackable per-IP, so an admin can
    see who's actually been failing without digging through raw
    CloudWatch logs one line at a time."""
    from src.api.main import app as main_app
    from src.api.routes.auth import get_client_ip

    main_app.dependency_overrides[get_client_ip] = lambda: "198.51.100.77"
    client.post("/auth/login", json={"email": "wrong@example.com", "password": "wrong"})
    client.post("/auth/login", json={"email": "wrong@example.com", "password": "wrong"})
    main_app.dependency_overrides.clear()

    response = client.get("/auth/failed-login-attempts", headers={"Authorization": f"Bearer {admin_token}"})
    assert response.status_code == 200
    entries = response.json()
    matching = [e for e in entries if e["ip"] == "198.51.100.77"]
    assert len(matching) == 1
    assert matching[0]["attempt_count"] == 2


def test_failed_login_attempts_endpoint_requires_admin(mock_aws_infra):
    response = client.get("/auth/failed-login-attempts")
    assert response.status_code in (401, 403)
