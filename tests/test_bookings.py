from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes.bookings import get_client_ip
from src.api.models.booking import BOOKING_TYPE_RULES, BookingType, AVAILABLE_TIMES_BY_TYPE

client = TestClient(app)


def _find_valid_date_and_time(booking_type: BookingType) -> tuple:
    """Like _find_valid_date, but ALSO returns a session_time that's safe
    regardless of what time the test actually runs at. If the chosen date
    is TODAY, a hardcoded "10:00" could already be more than an hour in the
    past by the time the test runs later in the day — which the history-
    visibility filter would then (correctly) exclude from list results,
    making an otherwise-correct test fail for a reason that has nothing to
    do with the thing being tested. When today qualifies, push the time 2
    hours into the future instead; if that would roll past midnight, skip
    today entirely and use the next valid day (where any fixed time is
    safe, since the whole day is still in the future)."""
    allowed_weekdays = BOOKING_TYPE_RULES[booking_type]["allowed_weekdays"]
    now = datetime.now(timezone.utc)
    for offset in range(7):
        candidate_date = (now + timedelta(days=offset)).date()
        if candidate_date.weekday() not in allowed_weekdays:
            continue
        if offset == 0:
            future_point = now + timedelta(hours=2)
            if future_point.date() != candidate_date:
                continue  # would roll into tomorrow — skip today, try the next valid day
            return candidate_date.isoformat(), future_point.strftime("%H:%M")
        return candidate_date.isoformat(), "10:00"
    raise RuntimeError("no valid date/time found — should be impossible for any real schedule")


def _find_valid_date(booking_type: BookingType) -> str:
    """Finds a real date, within the next 7 days from whenever this test
    actually runs, that falls on a weekday this booking_type is offered.
    Deliberately NOT a hardcoded future date — a hardcoded date would make
    these tests pass today and silently start failing months from now once
    that date is no longer "within the next week." Any 7 consecutive
    calendar days always contain exactly one occurrence of every weekday,
    so a match within range(7) is always guaranteed to exist."""
    date, _ = _find_valid_date_and_time(booking_type)
    return date


def _booking_payload(booking_type: BookingType, **overrides) -> dict:
    location, session_detail = {
        BookingType.personal_client_travels: ("mobile_personal", "client_travels"),
        BookingType.personal_trainer_travels: ("mobile_personal", "trainer_travels"),
        BookingType.personal_virtual: ("mobile_personal", "virtual"),
        BookingType.genes_adult: ("genes", "adult"),
        BookingType.genes_kids: ("genes", "kids"),
    }[booking_type]
    date, time = _find_valid_date_and_time(booking_type)
    payload = {
        "name": "Test Client",
        "email": "testclient@example.com",
        "phone": "4045551234",
        "session_date": date,
        "session_time": time,
        "location": location,
        "session_detail": session_detail,
    }
    payload.update(overrides)
    return payload


def _confirm_via_webhook(booking_id: str, mock_stripe_webhook_verify):
    """Simulates Stripe confirming payment for a booking — the ONLY way a
    booking should ever become 'confirmed' in this system."""
    mock_stripe_webhook_verify.return_value = {
        "type": "checkout.session.completed",
        "data": {"object": {"metadata": {"booking_id": booking_id}}},
    }
    response = client.post(
        "/webhooks/stripe", content=b"fake-payload", headers={"stripe-signature": "fake-sig"}
    )
    assert response.status_code == 200
    return response


# --- Booking creation / checkout flow ---------------------------------------

def test_create_booking_starts_checkout_not_confirmed(mock_aws_infra, mock_stripe_checkout):
    """Creation should NEVER confirm a booking directly — this is the core
    guarantee the whole payment-gating requirement rests on."""
    response = client.post("/bookings", json=_booking_payload(BookingType.genes_kids))
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "processing"
    assert body["price_usd"] == 90  # confirmed kids pricing — never client-supplied
    assert body["checkout_url"] == "https://checkout.stripe.com/test-session-url"


def test_create_booking_sends_checkout_link_email(mock_aws_infra, mock_stripe_checkout):
    """The recovery-path email — sent the moment checkout STARTS, not on
    confirmation, so someone who closes the tab before paying isn't
    stranded with a stuck booking and no way back into the same session."""
    from unittest.mock import MagicMock
    import src.api.core.ses_service as ses_module

    mock_client = MagicMock()
    ses_module.get_ses_service()._client = mock_client

    response = client.post("/bookings", json=_booking_payload(BookingType.genes_kids))
    assert response.status_code == 201

    mock_client.send_email.assert_called_once()
    kwargs = mock_client.send_email.call_args.kwargs
    assert kwargs["Destination"]["ToAddresses"] == [_booking_payload(BookingType.genes_kids)["email"]]
    assert "https://checkout.stripe.com/test-session-url" in kwargs["Message"]["Body"]["Text"]["Data"]
    assert "30 minutes" in kwargs["Message"]["Body"]["Text"]["Data"]


def test_second_booking_same_ip_blocked_while_first_processing(mock_aws_infra, mock_stripe_checkout):
    """The one-booking-in-progress-per-IP guard — different booking types,
    different dates even, same simulated client. Second must be blocked
    regardless of what's actually being booked, since this check fires
    before any type/slot-specific logic runs at all."""
    app.dependency_overrides[get_client_ip] = lambda: "203.0.113.5"

    first_payload = _booking_payload(BookingType.genes_kids)
    first = client.post("/bookings", json=first_payload)
    assert first.status_code == 201

    second_payload = _booking_payload(BookingType.personal_virtual)
    second = client.post("/bookings", json=second_payload)
    app.dependency_overrides.clear()

    assert second.status_code == 409
    assert "booking in progress" in second.json()["detail"].lower()


def test_different_ips_not_blocked_by_each_other(mock_aws_infra, mock_stripe_checkout):
    """Confirms the IP check is genuinely scoped per-IP, not accidentally
    global — two different simulated clients booking simultaneously must
    both succeed."""
    app.dependency_overrides[get_client_ip] = lambda: "203.0.113.10"
    first = client.post("/bookings", json=_booking_payload(BookingType.genes_kids))

    app.dependency_overrides[get_client_ip] = lambda: "203.0.113.20"
    second = client.post("/bookings", json=_booking_payload(BookingType.personal_virtual))
    app.dependency_overrides.clear()

    assert first.status_code == 201
    assert second.status_code == 201


def test_cancel_checkout_expires_session_and_returns_html(mock_aws_infra, mock_stripe_checkout):
    """The recovery path for Stripe's own cancel/back link — force-expires
    the real Stripe session rather than leaving the booking stranded for
    the full 30-minute natural expiry."""
    from unittest.mock import patch

    app.dependency_overrides[get_client_ip] = lambda: "203.0.113.30"
    created = client.post("/bookings", json=_booking_payload(BookingType.genes_kids))
    app.dependency_overrides.clear()
    booking_id = created.json()["booking_id"]

    with patch("stripe.checkout.Session.expire") as mock_expire:
        response = client.get(f"/bookings/{booking_id}/cancel-checkout")

    assert response.status_code == 200
    assert "cancelled" in response.text.lower()
    mock_expire.assert_called_once()


def test_cancel_checkout_is_idempotent_for_already_resolved_booking(mock_aws_infra, mock_stripe_checkout):
    """Reloading the cancel page, or clicking an old link after the
    booking already resolved (confirmed, expired, or already cancelled),
    should show a clean message — never a confusing error."""
    from src.api.core.bookings_repository import BookingRepository
    from src.api.core.database import get_db_service

    app.dependency_overrides[get_client_ip] = lambda: "203.0.113.40"
    created = client.post("/bookings", json=_booking_payload(BookingType.genes_kids))
    app.dependency_overrides.clear()
    booking_id = created.json()["booking_id"]

    repo = BookingRepository(get_db_service())
    repo.set_status(booking_id, "confirmed")

    response = client.get(f"/bookings/{booking_id}/cancel-checkout")
    assert response.status_code == 200
    assert "already been resolved" in response.text.lower()


def test_cancel_checkout_unknown_booking_returns_404(mock_aws_infra):
    response = client.get("/bookings/not-a-real-id/cancel-checkout")
    assert response.status_code == 404


def test_create_booking_invalid_location_detail_combo(mock_aws_infra):
    """genes + client_travels isn't a real offering — rejected at the API
    boundary (422), never even reaching Stripe."""
    payload = _booking_payload(BookingType.genes_kids, session_detail="client_travels")
    response = client.post("/bookings", json=payload)
    assert response.status_code == 422


def test_create_personal_virtual_booking_priced_correctly(mock_aws_infra, mock_stripe_checkout):
    """The new Zoom option — confirmed $40, matching client_travels pricing
    (Debo reasoned no gas cost either way)."""
    response = client.post("/bookings", json=_booking_payload(BookingType.personal_virtual))
    assert response.status_code == 201
    assert response.json()["price_usd"] == 40


# --- Webhook confirmation flow ----------------------------------------------

def test_webhook_confirms_booking_on_successful_payment(mock_aws_infra, mock_stripe_checkout, mock_stripe_webhook_verify):
    created = client.post("/bookings", json=_booking_payload(BookingType.genes_adult)).json()
    _confirm_via_webhook(created["booking_id"], mock_stripe_webhook_verify)

    booking = client.get(f"/bookings/{created['booking_id']}").json()
    assert booking["status"] == "confirmed"


def test_webhook_rejects_invalid_signature(mock_aws_infra, mock_stripe_webhook_verify):
    mock_stripe_webhook_verify.side_effect = ValueError("Invalid signature")
    response = client.post(
        "/webhooks/stripe", content=b"tampered-payload", headers={"stripe-signature": "bad-sig"}
    )
    assert response.status_code == 400


def test_webhook_expiry_releases_personal_slot(mock_aws_infra, mock_stripe_checkout, mock_stripe_webhook_verify):
    """Confirms an abandoned checkout actually frees the slot back up —
    without this, an abandoned cart would permanently block a real Personal
    time slot forever."""
    payload = _booking_payload(BookingType.personal_client_travels)
    created = client.post("/bookings", json=payload).json()

    mock_stripe_webhook_verify.return_value = {
        "type": "checkout.session.expired",
        "data": {"object": {"metadata": {"booking_id": created["booking_id"]}}},
    }
    client.post("/webhooks/stripe", content=b"fake-payload", headers={"stripe-signature": "fake-sig"})

    # Slot should now be free — a new booking for the SAME date/time/type
    # should succeed, not be rejected as taken/processing.
    second_attempt = client.post("/bookings", json=payload)
    assert second_attempt.status_code == 201


# --- Slot-claim race condition (the actual double-booking prevention) ------

def test_personal_slot_claimed_by_first_request_blocks_second(mock_aws_infra, mock_stripe_checkout):
    """THE core test for the double-booking requirement — two requests for
    the EXACT same Personal date+time. First one claims it and proceeds to
    checkout; second must be rejected with 409, telling the client it's
    currently being processed."""
    payload = _booking_payload(BookingType.personal_trainer_travels)

    # Different simulated IPs — this test is specifically about SLOT-CLAIM
    # behavior (same date+time), not the IP-based one-booking-at-a-time
    # check, which would otherwise mask the thing actually being tested
    # here since both requests would appear to come from the same client.
    app.dependency_overrides[get_client_ip] = lambda: "10.0.0.1"
    first = client.post("/bookings", json=payload)
    assert first.status_code == 201

    app.dependency_overrides[get_client_ip] = lambda: "10.0.0.2"
    second = client.post("/bookings", json=payload)
    app.dependency_overrides.clear()

    assert second.status_code == 409
    assert "check back shortly" in second.json()["detail"].lower()


def test_personal_slot_confirmed_blocks_new_booking_with_different_message(
    mock_aws_infra, mock_stripe_checkout, mock_stripe_webhook_verify
):
    """Once a Personal slot is CONFIRMED (not just processing), a new
    attempt at the same date+time should get the FINAL rejection message,
    distinct from the 'try again shortly' one."""
    payload = _booking_payload(BookingType.personal_client_travels)
    first = client.post("/bookings", json=payload).json()
    _confirm_via_webhook(first["booking_id"], mock_stripe_webhook_verify)

    second = client.post("/bookings", json=payload)
    assert second.status_code == 409
    assert "taken" in second.json()["detail"].lower()


def test_different_personal_delivery_methods_still_block_each_other(mock_aws_infra, mock_stripe_checkout):
    """Confirms the exclusivity is keyed by DATE+TIME, not by the specific
    delivery method — Debo can't simultaneously do client_travels AND
    trainer_travels AND virtual at the same moment, so booking one must
    block the others at that exact date+time too."""
    date = _find_valid_date(BookingType.personal_client_travels)

    client_travels_payload = _booking_payload(BookingType.personal_client_travels, session_date=date)
    virtual_payload = _booking_payload(BookingType.personal_virtual, session_date=date)

    first = client.post("/bookings", json=client_travels_payload)
    assert first.status_code == 201

    second = client.post("/bookings", json=virtual_payload)
    assert second.status_code == 409  # different delivery method, SAME time — still blocked


def test_genes_bookings_never_slot_claimed_multiple_allowed(mock_aws_infra, mock_stripe_checkout):
    """The inverse confirmation — Gene's classes are group settings and
    must NOT be exclusive. Two different clients booking the exact same
    Gene's adult class time should both succeed."""
    payload = _booking_payload(BookingType.genes_adult)

    # Different simulated IPs — genuinely different clients, matching the
    # test's own premise ("two different clients"). Without this, the
    # test would trip the unrelated one-booking-per-IP check instead of
    # exercising what it's actually meant to test.
    app.dependency_overrides[get_client_ip] = lambda: "10.0.0.3"
    first = client.post("/bookings", json=payload)

    app.dependency_overrides[get_client_ip] = lambda: "10.0.0.4"
    second = client.post("/bookings", json={**payload, "name": "Second Client", "email": "second@example.com"})
    app.dependency_overrides.clear()

    assert first.status_code == 201
    assert second.status_code == 201  # NOT blocked — group class, multiple bookings allowed


def test_cancelling_confirmed_personal_booking_does_not_release_slot(
    mock_aws_infra, mock_stripe_checkout, mock_stripe_webhook_verify, admin_token
):
    """DELIBERATE business decision, not a bug: cancelling a Personal
    booking does NOT free the slot back up. If Debo cancels, it's almost
    always for a real reason, and that exact date+time shouldn't be
    instantly re-bookable by a stranger without him actively re-opening it.
    Refunds are handled the same way — manually, by Debo, in Stripe's own
    dashboard, not automated here. See the reasoning in routes/bookings.py's
    cancel_booking for the full case (this only affects the ONE cancelled
    date+time, not future weeks at the same time)."""
    payload = _booking_payload(BookingType.personal_trainer_travels)
    created = client.post("/bookings", json=payload).json()
    _confirm_via_webhook(created["booking_id"], mock_stripe_webhook_verify)

    cancel_response = client.patch(
        f"/bookings/{created['booking_id']}/cancel",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert cancel_response.status_code == 200
    assert cancel_response.json()["status"] == "cancelled"

    # The slot should STILL be blocked — a new booking attempt for the same
    # exact date+time+type must be rejected, not allowed through.
    retry = client.post("/bookings", json=payload)
    assert retry.status_code == 409


# --- Retrieval / listing / cancellation (updated for checkout-based creation) ---

def test_get_booking_by_id(mock_aws_infra, mock_stripe_checkout):
    created = client.post("/bookings", json=_booking_payload(BookingType.personal_client_travels)).json()
    response = client.get(f"/bookings/{created['booking_id']}")
    assert response.status_code == 200
    assert response.json()["booking_id"] == created["booking_id"]


def test_get_booking_not_found(mock_aws_infra):
    response = client.get("/bookings/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


def test_list_bookings_requires_admin(mock_aws_infra):
    response = client.get("/bookings")
    assert response.status_code in (401, 403)


def test_list_bookings_with_admin_token(mock_aws_infra, admin_token, mock_stripe_checkout):
    client.post("/bookings", json=_booking_payload(BookingType.genes_adult))
    response = client.get("/bookings", headers={"Authorization": f"Bearer {admin_token}"})
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["booking_type"] == "genes_adult"


def test_list_bookings_excludes_slot_claim_records(mock_aws_infra, admin_token, mock_stripe_checkout):
    """Slot claims live in the SAME table as real bookings — this confirms
    the list endpoint filters them out and never shows a synthetic
    'personal-slot#...' entry as if it were a real booking a client made."""
    client.post("/bookings", json=_booking_payload(BookingType.personal_client_travels))
    response = client.get("/bookings", headers={"Authorization": f"Bearer {admin_token}"})
    body = response.json()
    assert len(body) == 1
    assert not any(b["booking_id"].startswith("personal-slot#") for b in body)


def test_list_bookings_excludes_expired(mock_aws_infra, admin_token, mock_stripe_checkout):
    """An expired booking never became a real relationship at all — no
    payment, nothing Debo needs to prepare for. Should never show up in
    his day-to-day list, unlike a cancelled booking which stays visible
    since it represents something that actually happened."""
    from src.api.core.bookings_repository import BookingRepository
    from src.api.core.database import get_db_service

    created = client.post("/bookings", json=_booking_payload(BookingType.genes_kids))
    booking_id = created.json()["booking_id"]

    repo = BookingRepository(get_db_service())
    repo.set_status(booking_id, "expired")

    response = client.get("/bookings", headers={"Authorization": f"Bearer {admin_token}"})
    body = response.json()
    assert not any(b["booking_id"] == booking_id for b in body)


def test_list_bookings_show_expired_returns_only_expired_leads(mock_aws_infra, admin_token, mock_stripe_checkout):
    """The dedicated follow-up-leads view — show_expired=True should
    return ONLY expired bookings, excluding confirmed/processing ones
    entirely. This is the inverse guarantee of the default-view test
    above: expired is invisible by default, but fully recoverable here."""
    from src.api.core.bookings_repository import BookingRepository
    from src.api.core.database import get_db_service

    repo = BookingRepository(get_db_service())

    expired = client.post("/bookings", json=_booking_payload(BookingType.genes_kids, name="Expired Lead"))
    expired_id = expired.json()["booking_id"]
    repo.set_status(expired_id, "expired")

    confirmed = client.post("/bookings", json=_booking_payload(BookingType.genes_adult, name="Real Client"))
    confirmed_id = confirmed.json()["booking_id"]
    repo.set_status(confirmed_id, "confirmed")

    response = client.get(
        "/bookings", params={"show_expired": "true"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    body = response.json()

    assert any(b["booking_id"] == expired_id for b in body)
    assert not any(b["booking_id"] == confirmed_id for b in body)


def test_list_bookings_search_filters_by_name(mock_aws_infra, admin_token, mock_stripe_checkout):
    client.post("/bookings", json=_booking_payload(BookingType.genes_adult, name="Alice Boxer"))
    client.post("/bookings", json=_booking_payload(BookingType.genes_adult, name="Bob Fighter"))
    response = client.get(
        "/bookings", params={"search": "alice"}, headers={"Authorization": f"Bearer {admin_token}"}
    )
    body = response.json()
    assert len(body) == 1
    assert body[0]["name"] == "Alice Boxer"


def test_cancel_booking_admin(mock_aws_infra, admin_token, mock_stripe_checkout):
    created = client.post("/bookings", json=_booking_payload(BookingType.genes_kids)).json()
    response = client.patch(
        f"/bookings/{created['booking_id']}/cancel",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


def test_cancel_booking_without_reason_uses_default(mock_aws_infra, admin_token, mock_stripe_checkout):
    """No body sent at all — should fall back to the documented default,
    not a blank/null value."""
    created = client.post("/bookings", json=_booking_payload(BookingType.genes_kids)).json()
    response = client.patch(
        f"/bookings/{created['booking_id']}/cancel",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.json()["cancellation_reason"] == "No reason was mentioned by Debo — contact him for more information."


def test_cancel_booking_with_blank_reason_uses_default(mock_aws_infra, admin_token, mock_stripe_checkout):
    """An explicitly blank/whitespace reason should be treated the same as
    not sending one at all — not stored as an empty string."""
    created = client.post("/bookings", json=_booking_payload(BookingType.genes_kids)).json()
    response = client.patch(
        f"/bookings/{created['booking_id']}/cancel",
        json={"reason": "   "},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.json()["cancellation_reason"] == "No reason was mentioned by Debo — contact him for more information."


def test_cancel_booking_with_custom_reason_is_stored(mock_aws_infra, admin_token, mock_stripe_checkout):
    created = client.post("/bookings", json=_booking_payload(BookingType.genes_kids)).json()
    response = client.patch(
        f"/bookings/{created['booking_id']}/cancel",
        json={"reason": "Debo is sick today"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.json()["cancellation_reason"] == "Debo is sick today"


def test_cancel_already_cancelled_booking_rejected(mock_aws_infra, admin_token, mock_stripe_checkout):
    created = client.post("/bookings", json=_booking_payload(BookingType.genes_kids)).json()
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


def test_cancel_booking_requires_admin(mock_aws_infra, mock_stripe_checkout):
    created = client.post("/bookings", json=_booking_payload(BookingType.genes_kids)).json()
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
        repo.create(item)


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
    deleted, only hidden from the default list.

    Computed as 2 hours before whenever this test actually runs, not a
    hardcoded "00:05 today" — that earlier approach had a real, self-
    documented ~65-minute daily blind spot (any CI run landing in the
    first hour of UTC day), which is exactly what failed once a real run
    happened to land at 00:00-00:01 UTC. This version is unconditionally
    correct regardless of what time the suite runs."""
    from src.api.core.bookings_repository import BookingRepository
    from src.api.core.database import get_db_service

    repo = BookingRepository(get_db_service())
    past_moment = datetime.now(timezone.utc) - timedelta(hours=2)
    past_item = {
        "booking_id": "past-visibility-test-id",
        "name": "Past Client", "email": "past@example.com", "phone": "4045551234",
        "session_date": past_moment.date().isoformat(), "session_time": past_moment.strftime("%H:%M"),
        "booking_type": "genes_adult", "location": "genes", "session_detail": "adult",
        "price_usd": 100, "status": "confirmed",
        "created_at": datetime.now(timezone.utc).isoformat(), "reminder_sent": False,
    }
    repo.create(past_item)

    get_response = client.get(f"/bookings/{past_item['booking_id']}")
    assert get_response.status_code == 200

    list_response = client.get("/bookings", headers={"Authorization": f"Bearer {admin_token}"})
    booking_ids = [b["booking_id"] for b in list_response.json()]
    assert past_item["booking_id"] not in booking_ids


def test_list_bookings_invalid_day_of_week_rejected(mock_aws_infra, admin_token):
    response = client.get(
        "/bookings", params={"day_of_week": "someday"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 400


def test_available_times_endpoint_is_public_and_returns_full_mapping(mock_aws_infra):
    # No Authorization header - this must be public, same as booking
    # creation itself, since a prospective client filling out the form
    # hasn't authenticated at all.
    response = client.get("/bookings/available-times")
    assert response.status_code == 200
    data = response.json()
    assert data == AVAILABLE_TIMES_BY_TYPE


def test_available_times_covers_every_booking_type():
    # Every real booking type must have at least one offered time, or the
    # frontend dropdown would render empty for that option.
    for booking_type in BookingType:
        assert booking_type.value in AVAILABLE_TIMES_BY_TYPE
        assert len(AVAILABLE_TIMES_BY_TYPE[booking_type.value]) > 0


def test_available_times_does_not_shadow_get_booking_by_id(mock_aws_infra):
    # Route-ordering regression guard: "/available-times" must never be
    # matched as if "available-times" were a booking_id path parameter.
    response = client.get("/bookings/available-times")
    assert response.status_code == 200
    assert isinstance(response.json(), dict)
    # A real booking_id lookup for a nonexistent id should still 404
    # correctly and separately, proving both routes coexist correctly.
    not_found = client.get("/bookings/definitely-not-a-real-id")
    assert not_found.status_code == 404
