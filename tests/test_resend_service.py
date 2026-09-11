"""
TEMPORARY FILE — delete entirely once AWS SES production access clears
and resend_service.py is deleted.
"""

import json
from unittest.mock import MagicMock, patch
import urllib.error

from src.api.core.resend_service import ResendService

BOOKING = {
    "name": "Test Client",
    "email": "client@example.com",
    "session_date": "2026-08-10",
    "session_time": "10:00",
    "phone": "4045551234",
    "booking_type": "genes_adult",
    "price_usd": 100,
}


def _mock_urlopen_response():
    mock_response = MagicMock()
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)
    mock_response.read.return_value = b'{"id": "fake-email-id"}'
    return mock_response


def _mock_ssm_client():
    """Matches the real SSM get_parameter response shape
    ({"Parameter": {"Value": "..."}}) - NOT Secrets Manager's shape
    ({"SecretString": "..."}). Getting this response shape wrong is
    exactly what caused a real TypeError when this project migrated from
    Secrets Manager to SSM Parameter Store - the mock kept the OLD shape
    while the real code moved to the new one."""
    mock_client = MagicMock()
    mock_client.get_parameter.return_value = {
        "Parameter": {"Value": '{"api_key": "re_test_fake_key"}'}
    }
    return mock_client


def test_send_booking_confirmation_posts_to_resend(monkeypatch):
    monkeypatch.setenv("RESEND_SECRET_PATH", "fake-resend-secret-path")
    service = ResendService()

    mock_ssm_client = _mock_ssm_client()

    with patch("boto3.client", return_value=mock_ssm_client), \
         patch("urllib.request.urlopen", return_value=_mock_urlopen_response()) as mock_urlopen:
        service.send_booking_confirmation(BOOKING)

    mock_urlopen.assert_called_once()
    sent_request = mock_urlopen.call_args[0][0]
    assert sent_request.full_url == "https://api.resend.com/emails"
    assert sent_request.get_header("Authorization") == "Bearer re_test_fake_key"
    assert sent_request.get_header("User-agent") == "DebosBoxingBookingPlatform/1.0"
    sent_body = json.loads(sent_request.data)
    assert sent_body["to"] == "client@example.com"
    assert "confirmed" in sent_body["subject"].lower()


def test_api_key_cached_across_multiple_sends(monkeypatch):
    """Same L1-caching pattern as every other secret in this project —
    fetched once per warm container, not on every single send."""
    monkeypatch.setenv("RESEND_SECRET_PATH", "fake-resend-secret-path")
    service = ResendService()

    mock_ssm_client = _mock_ssm_client()

    with patch("boto3.client", return_value=mock_ssm_client) as mock_boto3_client, \
         patch("urllib.request.urlopen", return_value=_mock_urlopen_response()):
        service.send_booking_confirmation(BOOKING)
        service.send_booking_confirmation(BOOKING)

    mock_boto3_client.assert_called_once()  # not twice


def test_send_failure_is_swallowed_not_raised(monkeypatch):
    """Matches ses_service.py's own discipline — a failed send should
    never roll back or fail an action that already completed."""
    monkeypatch.setenv("RESEND_SECRET_PATH", "fake-resend-secret-path")
    service = ResendService()

    mock_ssm_client = _mock_ssm_client()

    with patch("boto3.client", return_value=mock_ssm_client), \
         patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection failed")):
        service.send_booking_confirmation(BOOKING)  # should not raise


def test_http_error_logs_the_actual_response_body(monkeypatch, caplog):
    """The real bug this fixes: urllib's HTTPError carries the RESPONSE
    BODY from Resend (the actual, specific reason a request was
    rejected), which the old exception handling never captured - only
    the generic status line. This confirms the real body now makes it
    into the logs, which is what actually diagnoses a 403/422/etc."""
    monkeypatch.setenv("RESEND_SECRET_PATH", "fake-resend-secret-path")
    service = ResendService()

    mock_ssm_client = _mock_ssm_client()

    mock_fp = MagicMock()
    mock_fp.read.return_value = b'{"message": "This domain is not verified", "name": "validation_error"}'
    http_error = urllib.error.HTTPError(
        url="https://api.resend.com/emails", code=403, msg="Forbidden", hdrs=None, fp=mock_fp,
    )

    with patch("boto3.client", return_value=mock_ssm_client), \
         patch("urllib.request.urlopen", side_effect=http_error):
        service.send_booking_confirmation(BOOKING)  # should not raise

    assert "This domain is not verified" in caplog.text


def test_cancellation_notice_includes_refund_info():
    service = ResendService()
    refund_info = {"amount_usd": 100.0, "receipt_url": "https://pay.stripe.com/receipts/fake"}

    with patch.object(service, "_send") as mock_send:
        service.send_cancellation_notice_to_client(BOOKING, reason="Debo is sick", refund_info=refund_info)

    body = mock_send.call_args[0][2]
    assert "$100.00" in body
    assert "5-10 business days" in body


def test_new_booking_notification_uses_friendly_type_name_not_raw_enum():
    """Same fix as ses_service.py - the raw database value should never
    leak into a customer/admin-facing email."""
    service = ResendService()
    with patch.object(service, "_send") as mock_send:
        service.send_new_booking_notification(BOOKING, admin_email="debo@example.com")

    body = mock_send.call_args[0][2]
    assert "genes_adult" not in body
    assert "Adult Gene's Class" in body
