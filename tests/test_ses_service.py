from unittest.mock import MagicMock

from botocore.exceptions import ClientError

from src.api.core.ses_service import SesService

BOOKING = {
    "name": "Test Client",
    "email": "client@example.com",
    "session_date": "2026-08-10",
    "session_time": "10:00",
    "phone": "4045551234",
    "booking_type": "genes_adult",
    "price_usd": 100,
}


def _service_with_mock_client() -> tuple:
    service = SesService()
    mock_client = MagicMock()
    service._client = mock_client  # bypasses the lazy boto3.client() call entirely
    return service, mock_client


def test_booking_confirmation_sent_to_client():
    service, mock_client = _service_with_mock_client()
    service.send_booking_confirmation(BOOKING)

    mock_client.send_email.assert_called_once()
    kwargs = mock_client.send_email.call_args.kwargs
    assert kwargs["Destination"]["ToAddresses"] == ["client@example.com"]
    assert "confirmed" in kwargs["Message"]["Subject"]["Data"].lower()
    assert "2026-08-10" in kwargs["Message"]["Body"]["Text"]["Data"]


def test_new_booking_notification_sent_to_admin_not_client():
    service, mock_client = _service_with_mock_client()
    service.send_new_booking_notification(BOOKING, admin_email="debo@example.com")

    kwargs = mock_client.send_email.call_args.kwargs
    assert kwargs["Destination"]["ToAddresses"] == ["debo@example.com"]
    assert BOOKING["name"] in kwargs["Message"]["Body"]["Text"]["Data"]


def test_cancellation_notice_to_client_includes_reason():
    service, mock_client = _service_with_mock_client()
    service.send_cancellation_notice_to_client(BOOKING, reason="Debo is sick today")

    kwargs = mock_client.send_email.call_args.kwargs
    assert kwargs["Destination"]["ToAddresses"] == ["client@example.com"]
    assert "Debo is sick today" in kwargs["Message"]["Body"]["Text"]["Data"]


def test_cancellation_notice_to_admin_includes_reason():
    service, mock_client = _service_with_mock_client()
    service.send_cancellation_notice_to_admin(BOOKING, reason="Debo is sick today", admin_email="debo@example.com")

    kwargs = mock_client.send_email.call_args.kwargs
    assert kwargs["Destination"]["ToAddresses"] == ["debo@example.com"]
    assert "Debo is sick today" in kwargs["Message"]["Body"]["Text"]["Data"]


def test_reminder_sent_to_client():
    service, mock_client = _service_with_mock_client()
    service.send_reminder(BOOKING)

    kwargs = mock_client.send_email.call_args.kwargs
    assert kwargs["Destination"]["ToAddresses"] == ["client@example.com"]
    assert "tomorrow" in kwargs["Message"]["Subject"]["Data"].lower()


def test_send_failure_is_swallowed_not_raised():
    """The core design guarantee: a failed email must NEVER propagate up
    and fail the booking/cancellation action that already succeeded."""
    service, mock_client = _service_with_mock_client()
    mock_client.send_email.side_effect = ClientError(
        {"Error": {"Code": "MessageRejected", "Message": "test failure"}}, "SendEmail"
    )

    # Should not raise — this is the whole point of the test
    service.send_booking_confirmation(BOOKING)
