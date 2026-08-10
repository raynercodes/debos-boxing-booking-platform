from datetime import datetime, timedelta, timezone

from src.scheduled.reminder_handler import handler
from src.api.core.bookings_repository import BookingRepository
from src.api.core.database import get_db_service
from src.api.models.booking import GYM_TIMEZONE


def _tomorrow() -> str:
    """Matches the handler's own corrected logic - "tomorrow" is Eastern's
    calendar day, not UTC's. Using the same naive UTC computation the old
    buggy handler used would make this test flaky depending on time of
    day, same class of bug as elsewhere in this project."""
    now_eastern = datetime.now(timezone.utc).astimezone(GYM_TIMEZONE)
    return (now_eastern + timedelta(days=1)).date().isoformat()


def _base_item(booking_id: str, **overrides) -> dict:
    item = {
        "booking_id": booking_id,
        "name": "Test Client",
        "email": "test@example.com",
        "phone": "4045551234",
        "session_date": _tomorrow(),
        "session_time": "10:00",
        "booking_type": "genes_adult",
        "location": "genes",
        "session_detail": "adult",
        "price_usd": 100,
        "status": "confirmed",
        "created_at": "2026-08-01T00:00:00+00:00",
        "reminder_sent": False,
    }
    item.update(overrides)
    return item


def test_reminder_sent_for_confirmed_unsent_booking(mock_aws_infra):
    repo = BookingRepository(get_db_service())
    booking_id = "reminder-test-1"
    repo.create(_base_item(booking_id))

    result = handler({}, None)

    assert result["reminders_sent"] == 1
    updated = repo.get_by_id(booking_id)
    assert updated["reminder_sent"] is True


def test_reminder_skips_already_sent(mock_aws_infra):
    repo = BookingRepository(get_db_service())
    repo.create(_base_item("reminder-test-2", reminder_sent=True))

    result = handler({}, None)

    assert result["reminders_sent"] == 0
    assert result["skipped"] == 1


def test_reminder_skips_non_confirmed_booking(mock_aws_infra):
    """A still-processing checkout shouldn't get a reminder — they haven't
    actually paid yet, so there's nothing confirmed to remind them about."""
    repo = BookingRepository(get_db_service())
    repo.create(_base_item("reminder-test-3", status="processing"))

    result = handler({}, None)

    assert result["reminders_sent"] == 0
    assert result["skipped"] == 1


def test_reminder_skips_cancelled_booking(mock_aws_infra):
    repo = BookingRepository(get_db_service())
    repo.create(_base_item("reminder-test-4", status="cancelled"))

    result = handler({}, None)

    assert result["reminders_sent"] == 0
    assert result["skipped"] == 1


def test_reminder_handles_multiple_bookings_same_day(mock_aws_infra):
    """Confirms the job processes an entire day's bookings correctly,
    sending only for the eligible ones — not all-or-nothing."""
    repo = BookingRepository(get_db_service())
    repo.create(_base_item("multi-1", session_time="09:00", status="confirmed"))
    repo.create(_base_item("multi-2", session_time="10:00", status="confirmed"))
    repo.create(_base_item("multi-3", session_time="11:00", status="cancelled"))

    result = handler({}, None)

    assert result["reminders_sent"] == 2
    assert result["skipped"] == 1


def test_reminder_job_returns_correct_target_date(mock_aws_infra):
    result = handler({}, None)
    assert result["target_date"] == _tomorrow()
