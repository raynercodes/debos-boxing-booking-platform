from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.models.booking import BOOKING_TYPE_RULES, BookingType

client = TestClient(app)


def _find_valid_date(booking_type: BookingType) -> str:
    """Finds a real date, within the next 7 days from whenever this test
    actually runs, that falls on a weekday this booking_type is offered.
    Deliberately NOT a hardcoded future date — a hardcoded date would make
    these tests pass today and silently start failing months from now once
    that date is no longer "within the next week." Any 7 consecutive
    calendar days always contain exactly one occurrence of every weekday,
    so a match within range(7) is always guaranteed to exist."""
    allowed_weekdays = BOOKING_TYPE_RULES[booking_type]["allowed_weekdays"]
    today = datetime.now(timezone.utc).date()
    for offset in range(7):
        candidate = today + timedelta(days=offset)
        if candidate.weekday() in allowed_weekdays:
            return candidate.isoformat()
    raise RuntimeError("no valid date found — should be impossible for any real schedule")


def _booking_payload(booking_type: BookingType, **overrides) -> dict:
    location, session_detail = {
        BookingType.personal_client_travels: ("mobile_personal", "client_travels"),
        BookingType.personal_trainer_travels: ("mobile_personal", "trainer_travels"),
        BookingType.genes_adult: ("genes", "adult"),
        BookingType.genes_kids: ("genes", "kids"),
    }[booking_type]
    payload = {
        "name": "Test Client",
        "email": "testclient@example.com",
        "phone": "4045551234",
        "session_date": _find_valid_date(booking_type),
        "session_time": "10:00",
        "location": location,
        "session_detail": session_detail,
    }
    payload.update(overrides)
    return payload


def test_create_booking_success(mock_aws_infra):
    response = client.post("/bookings/", json=_booking_payload(BookingType.genes_kids))
    assert response.status_code == 201
    body = response.json()
    assert body["booking_type"] == "genes_kids"
    assert body["price_usd"] == 90  # confirmed kids pricing — never client-supplied
    assert body["status"] == "confirmed"
    assert body["reminder_sent"] is False


def test_create_booking_invalid_location_detail_combo(mock_aws_infra):
    """genes + client_travels isn't a real offering — client_travels only
    applies to mobile_personal. Should be rejected at the API boundary
    (422) before ever reaching the database."""
    payload = _booking_payload(BookingType.genes_kids, session_detail="client_travels")
    response = client.post("/bookings/", json=payload)
    assert response.status_code == 422


def test_get_booking_by_id(mock_aws_infra):
    created = client.post("/bookings/", json=_booking_payload(BookingType.personal_client_travels)).json()
    response = client.get(f"/bookings/{created['booking_id']}")
    assert response.status_code == 200
    assert response.json()["booking_id"] == created["booking_id"]


def test_get_booking_not_found(mock_aws_infra):
    response = client.get("/bookings/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


def test_list_bookings_requires_admin(mock_aws_infra):
    response = client.get("/bookings/")
    assert response.status_code in (401, 403)  # HTTPBearer's own missing-header response is 403


def test_list_bookings_with_admin_token(mock_aws_infra, admin_token):
    client.post("/bookings/", json=_booking_payload(BookingType.genes_adult))
    response = client.get("/bookings/", headers={"Authorization": f"Bearer {admin_token}"})
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["booking_type"] == "genes_adult"


def test_list_bookings_search_filters_by_name(mock_aws_infra, admin_token):
    client.post("/bookings/", json=_booking_payload(BookingType.genes_adult, name="Alice Boxer"))
    client.post("/bookings/", json=_booking_payload(BookingType.genes_adult, name="Bob Fighter"))
    response = client.get(
        "/bookings/", params={"search": "alice"}, headers={"Authorization": f"Bearer {admin_token}"}
    )
    body = response.json()
    assert len(body) == 1
    assert body[0]["name"] == "Alice Boxer"


def test_cancel_booking_admin(mock_aws_infra, admin_token):
    created = client.post("/bookings/", json=_booking_payload(BookingType.genes_kids)).json()
    response = client.patch(
        f"/bookings/{created['booking_id']}/cancel",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


def test_cancel_already_cancelled_booking_rejected(mock_aws_infra, admin_token):
    """Confirms the double-cancel guard actually works — first cancel
    succeeds, second attempt on the SAME booking must be rejected with 409,
    not silently succeed again (which would, once emails are wired in,
    re-trigger duplicate cancellation notices to both Debo and the client)."""
    created = client.post("/bookings/", json=_booking_payload(BookingType.genes_kids)).json()
    headers = {"Authorization": f"Bearer {admin_token}"}

    first = client.patch(f"/bookings/{created['booking_id']}/cancel", headers=headers)
    assert first.status_code == 200

    second = client.patch(f"/bookings/{created['booking_id']}/cancel", headers=headers)
    assert second.status_code == 409


def test_cancel_booking_not_found(mock_aws_infra, admin_token):
    response = client.patch(
        "/bookings/00000000-0000-0000-0000-000000000000/cancel",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 404


def test_cancel_booking_requires_admin(mock_aws_infra):
    created = client.post("/bookings/", json=_booking_payload(BookingType.genes_kids)).json()
    response = client.patch(f"/bookings/{created['booking_id']}/cancel")
    assert response.status_code in (401, 403)


def test_repository_rejects_duplicate_booking_id(mock_aws_infra):
    """The API layer always generates a fresh UUID, so this path can't be
    triggered through HTTP requests alone — testing the repository directly
    confirms the conditional write itself actually guards against a
    collision, rather than trusting that "UUIDs basically never collide"
    without ever having verified the guard code runs correctly."""
    from src.api.core.bookings_repository import BookingRepository
    from src.api.core.database import get_db_service
    from src.api.core.exceptions import ExternalServiceError

    repo = BookingRepository(get_db_service())
    item = {
        "booking_id": "duplicate-test-id",
        "name": "First", "email": "a@example.com", "phone": "4045551234",
        "session_date": "2026-08-10", "session_time": "10:00",
        "booking_type": "genes_adult", "location": "genes", "session_detail": "adult",
        "price_usd": 100, "status": "confirmed",
        "created_at": "2026-08-01T00:00:00+00:00", "reminder_sent": False,
    }
    repo.create(item)
    with pytest.raises(ExternalServiceError):
        repo.create(item)  # same booking_id again


def test_cancel_completed_booking_still_succeeds(mock_aws_infra, admin_token):
    """Confirms the double-cancel guard ONLY blocks the specific case of
    already-cancelled — a 'completed' booking (or any other non-cancelled
    status) must still be cancellable "at any time," exactly as specified."""
    from src.api.core.bookings_repository import BookingRepository
    from src.api.core.database import get_db_service

    repo = BookingRepository(get_db_service())
    item = {
        "booking_id": "completed-test-id",
        "name": "Done Client", "email": "done@example.com", "phone": "4045551234",
        "session_date": "2026-08-10", "session_time": "10:00",
        "booking_type": "genes_adult", "location": "genes", "session_detail": "adult",
        "price_usd": 100, "status": "completed",
        "created_at": "2026-08-01T00:00:00+00:00", "reminder_sent": False,
    }
    repo.create(item)

    response = client.patch(
        f"/bookings/{item['booking_id']}/cancel",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


def test_history_visibility_filter_excludes_past_bookings(mock_aws_infra, admin_token):
    """A booking more than 1hr past its start time should disappear from the
    admin LIST view, but remain fully fetchable by ID — the record is never
    deleted, only hidden from the default list (see HISTORY_VISIBILITY_WINDOW
    comment in routes/bookings.py for why this is a display filter, not a
    deletion policy).

    NOTE: constructs the past time as today's date at 00:05 UTC, which is
    safely >1hr in the past for any test run after ~01:05 UTC — the only
    edge case this doesn't cover is a test suite run in the first ~65
    minutes of the UTC day, which is an accepted, extremely low-probability
    gap rather than something worth writing significantly more complex
    date-rollover-safe logic for."""
    from src.api.core.bookings_repository import BookingRepository
    from src.api.core.database import get_db_service
    from datetime import datetime, timezone

    repo = BookingRepository(get_db_service())
    today = datetime.now(timezone.utc).date()
    past_item = {
        "booking_id": "past-visibility-test-id",
        "name": "Past Client", "email": "past@example.com", "phone": "4045551234",
        "session_date": today.isoformat(), "session_time": "00:05",
        "booking_type": "genes_adult", "location": "genes", "session_detail": "adult",
        "price_usd": 100, "status": "confirmed",
        "created_at": datetime.now(timezone.utc).isoformat(), "reminder_sent": False,
    }
    repo.create(past_item)

    # Still individually fetchable — history filter only applies to the LIST view
    get_response = client.get(f"/bookings/{past_item['booking_id']}")
    assert get_response.status_code == 200

    # But excluded from the admin list
    list_response = client.get("/bookings/", headers={"Authorization": f"Bearer {admin_token}"})
    booking_ids = [b["booking_id"] for b in list_response.json()]
    assert past_item["booking_id"] not in booking_ids


def test_list_bookings_invalid_day_of_week_rejected(mock_aws_infra, admin_token):
    response = client.get(
        "/bookings/", params={"day_of_week": "someday"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 400  # generic AppError handler
