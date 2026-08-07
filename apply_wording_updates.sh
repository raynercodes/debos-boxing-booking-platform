#!/usr/bin/env bash
set -e

# Run this FROM INSIDE your existing debos-boxing-booking-platform folder.
#
# Applies your wording updates to SlotProcessingError and
# BookingAlreadyCancelledError (now includes client name), updates the
# matching test assertion, and adds the force_expire_session.py utility
# script for testing checkout expiration without waiting 30 real minutes.

echo "Applying wording updates and new utility script..."

cat > src/api/routes/bookings.py << 'FILEEOF'
import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from fastapi import APIRouter, Depends, Query
from fastapi.security import HTTPBearer
from pydantic import BaseModel

from src.api.models.booking import (
    BookingRequest, BookingResponse, BookingCheckoutResponse, BookingStatus,
    BOOKING_TYPE_RULES, CHECKOUT_SESSION_EXPIRY_MINUTES, requires_slot_claim,
)
from src.api.core.security import get_security_service
from src.api.core.database import get_db_service
from src.api.core.bookings_repository import BookingRepository
from src.api.core.stripe_service import get_stripe_service
from src.api.core.exceptions import (
    AppError, BookingNotFoundError, BookingAlreadyCancelledError,
    SlotProcessingError, SlotTakenError,
)
from src.api.core.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter()
bearer_scheme = HTTPBearer()

# Weekends deliberately excluded — no BookingType's allowed_weekdays ever
# includes Saturday/Sunday (Personal is Mon-Fri, the widest of the three),
# so a day_of_week filter value of "saturday" would always return an empty
# result. This isn't the actual defense against weekend bookings — that
# lives in booking.py's schedule validator, checked against real weekday
# integers independent of this list. This is just removing dead, unused
# input surface from an admin-only query param.
VALID_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]

# How long a booking stays visible in the admin list after its start time has
# passed. Deliberately a DISPLAY filter applied at query time, NOT a deletion
# policy — the record stays in DynamoDB permanently (useful for history,
# case-study metrics, and the gym owner's own records). Same TTL-explicit-
# evaluation lesson from fintech: never rely on a timestamp-based mechanism
# to physically remove something on a tight schedule — DynamoDB TTL deletion
# can lag up to 48 hours, useless for a "gone within an hour" rule. Explicit
# evaluation in application code, every time, is the only way to guarantee
# this window actually holds.
HISTORY_VISIBILITY_WINDOW = timedelta(hours=1)

# Shown on the booking record (and eventually in the cancellation email to
# both Debo and the client) whenever the admin cancels without typing a
# reason. Kept as a named constant rather than an inline string so there's
# exactly one place to change the wording later.
DEFAULT_CANCELLATION_REASON = "No reason was mentioned by Debo — contact him for more information."


class CancelBookingRequest(BaseModel):
    """Optional request body for the cancel endpoint — admin can type a
    reason, or send nothing at all and DEFAULT_CANCELLATION_REASON gets
    used instead. Kept optional (not required) since forcing a reason on
    every cancellation would just encourage typing throwaway text to get
    past a required field, which defeats the point of collecting it."""
    reason: Optional[str] = None


def require_admin(credentials=Depends(bearer_scheme)):
    """Delegates to SecurityService, which raises InvalidTokenError on
    failure — translated to a 401 by the global exception handler in
    main.py, so this stays a one-line dependency."""
    get_security_service().require_admin_token(credentials.credentials)


def _get_repository() -> BookingRepository:
    """Small helper so every route constructs the repository the same way,
    with the singleton DynamoDBService injected — matches the same pattern
    already used for LockoutManager in routes/auth.py."""
    return BookingRepository(get_db_service())


def _item_to_response(item: dict) -> BookingResponse:
    """DynamoDB returns numbers as Decimal, not int/float — BookingResponse
    expects a plain int for price_usd, so this conversion has to happen
    explicitly every time an item comes back out of the table."""
    return BookingResponse(
        booking_id=item["booking_id"],
        name=item["name"],
        email=item["email"],
        phone=item["phone"],
        session_date=item["session_date"],
        session_time=item["session_time"],
        booking_type=item["booking_type"],
        price_usd=int(item["price_usd"]),
        status=item["status"],
        created_at=item["created_at"],
        reminder_sent=item["reminder_sent"],
        cancellation_reason=item.get("cancellation_reason"),  # only present once cancelled
    )


def _is_past_visibility_window(item: dict) -> bool:
    """Explicit, computed-at-read-time check — see HISTORY_VISIBILITY_WINDOW
    comment above for why this is a display filter, not a stored flag."""
    session_start = datetime.strptime(
        f"{item['session_date']}T{item['session_time']}", "%Y-%m-%dT%H:%M"
    ).replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - session_start > HISTORY_VISIBILITY_WINDOW


@router.post(
    "",
    response_model=BookingCheckoutResponse,
    status_code=201,
    summary="Create Booking (starts Stripe checkout)",
    description="Submit a new session booking. Returns a Stripe checkout URL — "
                "the booking is NOT confirmed until payment succeeds via webhook.",
)
async def create_booking(request: BookingRequest):
    # Price is ALWAYS looked up server-side from BOOKING_TYPE_RULES, never
    # accepted as a value from the client. If the client could send its own
    # price, anyone could book a $100 Gene's adult session and submit
    # price_usd=1 — the server is the only source of truth for what
    # something costs, the request only says WHAT was booked, never
    # WHAT IT COSTS.
    booking_type = request.booking_type
    price_usd = BOOKING_TYPE_RULES[booking_type]["price_usd"]
    repo = _get_repository()
    booking_id = str(uuid.uuid4())

    # Slot claiming ONLY applies to Personal training (any of the 3
    # delivery methods) — Debo can only train one person at a given
    # date+time regardless of format. Gene's classes are group settings
    # and skip this entirely; multiple people can book the same class time.
    if requires_slot_claim(booking_type):
        claim_result = repo.claim_personal_slot(request.session_date, request.session_time, booking_id)
        if claim_result["outcome"] == "already_processing":
            raise SlotProcessingError(
                "This Session is currently being booked at this time. It might be available soon — check back shortly."
            )
        if claim_result["outcome"] == "already_confirmed":
            raise SlotTakenError(
                "Sorry, this booking is taken. Try another available day or time."
            )

    item = {
        "booking_id": booking_id,
        "name": request.name,
        "email": request.email,
        "phone": request.phone,
        "session_date": request.session_date,
        "session_time": request.session_time,
        "booking_type": booking_type.value,
        # location/session_detail stored alongside the derived booking_type
        # even though BookingResponse doesn't expose them yet — cheap to
        # store on a schemaless DynamoDB item, and useful for future
        # reporting (e.g. "all genes bookings" or "all mobile bookings")
        # without needing to parse booking_type strings.
        "location": request.location.value,
        "session_detail": request.session_detail.value,
        "price_usd": price_usd,
        "status": BookingStatus.processing.value,  # NEVER confirmed here — only the webhook confirms
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reminder_sent": False,
    }

    try:
        repo.create(item)
    except Exception:
        # If the booking write fails after a slot claim succeeded, release
        # the claim — otherwise a failed write would leave a phantom claim
        # blocking the slot forever with no booking behind it.
        if requires_slot_claim(booking_type):
            repo.release_slot(request.session_date, request.session_time)
        raise

    # Framer URLs are placeholders until the frontend exists — TODO once
    # Framer is wired in, point these at the real confirmation/cancelled
    # pages instead of this API's own domain.
    base_url = os.environ.get("FRONTEND_BASE_URL", "https://debosboxingandfitness.com")
    stripe_service = get_stripe_service()
    try:
        session = stripe_service.create_checkout_session(
            booking_id=booking_id,
            price_usd=price_usd,
            booking_type_label=booking_type.value.replace("_", " ").title(),
            session_date=request.session_date,
            session_time=request.session_time,
            customer_email=request.email,
            success_url=f"{base_url}/booking-confirmed?booking_id={booking_id}",
            cancel_url=f"{base_url}/booking-cancelled?booking_id={booking_id}",
            expires_in_minutes=CHECKOUT_SESSION_EXPIRY_MINUTES,
        )
    except Exception:
        # Same reasoning as above — if Stripe itself fails, don't leave a
        # phantom claim/booking behind with no way to ever pay for it.
        if requires_slot_claim(booking_type):
            repo.release_slot(request.session_date, request.session_time)
        raise

    logger.info(
        "Checkout started: %s %s (%s, $%s) booking_id=%s",
        request.session_date, request.session_time, booking_type.value, price_usd, booking_id,
    )
    return BookingCheckoutResponse(
        booking_id=booking_id,
        status=BookingStatus.processing,
        price_usd=price_usd,
        checkout_url=session.url,
    )


@router.get(
    "",
    summary="List Upcoming Bookings (Admin)",
    description="Admin-only. Filters: day_of_week, search (name/phone). "
                "Auto-excludes bookings more than 1hr past their start time.",
    dependencies=[Depends(require_admin)],
)
async def list_bookings(
    day_of_week: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
) -> List[BookingResponse]:
    if day_of_week and day_of_week.lower() not in VALID_DAYS:
        raise AppError(f"day_of_week must be one of {VALID_DAYS}")

    repo = _get_repository()
    today = datetime.now(timezone.utc).date()
    week_dates = [today + timedelta(days=i) for i in range(7)]

    # If a specific day_of_week was given, only query the ONE date in the
    # current week window that falls on that weekday — no reason to issue
    # 7 GSI queries and throw away 6 of them when we can compute which
    # single date we actually need.
    if day_of_week:
        target_weekday = VALID_DAYS.index(day_of_week.lower())
        week_dates = [d for d in week_dates if d.weekday() == target_weekday]

    all_items: List[dict] = []
    for date in week_dates:
        all_items.extend(repo.query_by_date(date.isoformat()))

    # Slot-claim records share the bookings table but aren't real bookings —
    # filter them out before anything else touches this list. They're
    # identifiable by their synthetic "personal-slot#..." booking_id prefix.
    all_items = [item for item in all_items if not item["booking_id"].startswith("personal-slot#")]

    # History filter — computed fresh on every call, never trusted from a
    # stored flag (see HISTORY_VISIBILITY_WINDOW comment above).
    visible_items = [item for item in all_items if not _is_past_visibility_window(item)]

    # Search filter — case-insensitive substring match on name or phone.
    # Done in Python, not a DynamoDB filter expression — not worth a GSI
    # for this at gym-scale data volume (dozens of bookings, not thousands).
    if search:
        search_lower = search.lower()
        visible_items = [
            item for item in visible_items
            if search_lower in item["name"].lower() or search_lower in item["phone"]
        ]

    # Sort ascending by (date, time) — since week_dates starts at today,
    # this naturally puts the soonest upcoming session first with no
    # extra logic needed.
    visible_items.sort(key=lambda item: (item["session_date"], item["session_time"]))

    return [_item_to_response(item) for item in visible_items]


@router.get(
    "/{booking_id}",
    response_model=BookingResponse,
    summary="Get Booking by ID",
)
async def get_booking(booking_id: str):
    item = _get_repository().get_by_id(booking_id)
    if item is None:
        raise BookingNotFoundError(f"Booking {booking_id} not found")
    return _item_to_response(item)


@router.patch(
    "/{booking_id}/cancel",
    response_model=BookingResponse,
    summary="Cancel Booking (Admin)",
    description="Admin-only. Cancels a booking at any time regardless of how "
                "close it is to the session start.",
    dependencies=[Depends(require_admin)],
)
async def cancel_booking(booking_id: str, request: Optional[CancelBookingRequest] = None):
    # Blank/whitespace-only reason treated the same as "no reason given" —
    # an admin submitting an empty string shouldn't produce a blank-looking
    # record; it should fall through to the same default as sending nothing.
    reason = (request.reason.strip() if request and request.reason else "") or DEFAULT_CANCELLATION_REASON

    # Atomic cancel — see BookingRepository.cancel docstring for why this is
    # ONE conditional write rather than a separate check-then-update pair.
    result = _get_repository().cancel(booking_id, reason)

    if result["outcome"] == "not_found":
        raise BookingNotFoundError(f"Booking {booking_id} not found")

    if result["outcome"] == "already_cancelled":
        # No emails fire here — this is a rejected no-op, not a state change.
        raise BookingAlreadyCancelledError(
            f"Booking {booking_id} is already cancelled for your client: {result['item']['name']}"
        )

    item = result["item"]

    # DELIBERATE BUSINESS DECISION — the slot claim is NOT released on
    # cancellation. If Debo manually cancels a Personal session, that's
    # almost always for a real reason (unavailable, emergency, etc.), and
    # the exact date+time shouldn't be instantly re-bookable by a stranger
    # without him actively re-opening it. This only affects the ONE
    # specific calendar date+time that was cancelled — slot claims are keyed
    # by exact (date, time), not a recurring weekly pattern, so cancelling
    # Aug 10 at 10am has zero effect on future weeks' Mondays at 10am.
    #
    # Refunds are handled the same way, on purpose — manually, by Debo,
    # directly in Stripe's own dashboard, NOT automated by this system.
    # Automating real refunds correctly means handling partial refunds,
    # preventing double-refunds, and listening for another webhook event
    # (charge.refunded) — real complexity that isn't worth it at this
    # scale (~20 clients/day). A human doing it in Stripe's already-safe,
    # already-built refund UI is both less code and less risk than custom
    # refund logic here. Revisit only if this ever becomes a much higher-
    # volume storefront where manual refund handling stops scaling.

    # TODO (future, once SES is wired in): send TWO emails on successful
    # cancellation — one to Debo confirming the cancellation happened, and
    # one to the original booker (client) notifying them their session was
    # cancelled. Both emails belong HERE, only on the "cancelled" outcome
    # above, never on the "already_cancelled" rejection path. The `reason`
    # captured above (typed by Debo, or DEFAULT_CANCELLATION_REASON if he
    # didn't provide one) belongs in BOTH email bodies once that's wired in.
    logger.info("Booking %s cancelled by admin (reason: %s)", booking_id, reason)
    return _item_to_response(item)
FILEEOF

cat > tests/test_bookings.py << 'FILEEOF'
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.models.booking import BOOKING_TYPE_RULES, BookingType

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

    first = client.post("/bookings", json=payload)
    assert first.status_code == 201

    second = client.post("/bookings", json=payload)
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

    first = client.post("/bookings", json=payload)
    second = client.post("/bookings", json={**payload, "name": "Second Client", "email": "second@example.com"})

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

    NOTE: constructs the past time as today's date at 00:05 UTC, which is
    safely >1hr in the past for any test run after ~01:05 UTC — the only
    edge case this doesn't cover is a test suite run in the first ~65
    minutes of the UTC day, an accepted, extremely low-probability gap."""
    from src.api.core.bookings_repository import BookingRepository
    from src.api.core.database import get_db_service

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
FILEEOF

cat > scripts/force_expire_session.py << 'FILEEOF'
"""
Force-expires a specific Stripe Checkout Session immediately — lets you
test the expiration/slot-release webhook path without waiting the real
30-minute minimum.

Pulls the real Stripe API key straight from Secrets Manager via
StripeService, same as the deployed app does — you never type or paste
the raw key anywhere, here or in your shell history.

Usage:
    export AWS_PROFILE=debos-boxing
    export AWS_DEFAULT_REGION=us-east-1
    export STRIPE_SECRET_PATH=/debos-boxing/dev/stripe-secret
    PYTHONPATH=. python3 scripts/force_expire_session.py cs_test_...

Get the session ID from the checkout_url a booking request returns —
it's the part starting with "cs_test_" right after "/pay/".
"""

import sys
import stripe
from src.api.core.stripe_service import get_stripe_service

if len(sys.argv) != 2:
    print("Usage: python3 scripts/force_expire_session.py <checkout_session_id>")
    sys.exit(1)

session_id = sys.argv[1]
stripe.api_key = get_stripe_service().api_key

try:
    session = stripe.checkout.Session.expire(session_id)
    print(f"Session {session_id} expired successfully. Status: {session.status}")
    print("Check CloudWatch logs for the webhook processing this event, "
          "then confirm the booking's status flipped to 'expired' and the slot is bookable again.")
except stripe.error.StripeError as exc:
    print(f"Stripe rejected the request: {exc}")
    print("Common cause: the session already completed, already expired, "
          "or the session ID was copied incorrectly.")
FILEEOF

echo "Files updated. Running full test suite..."
python3 -m pip install -r requirements-dev.txt
python3 -m pytest tests/ -v

echo ""
echo "If all tests pass, review before committing:"
echo "  git status"
echo "  git diff"
echo ""
echo "Then commit (still on dev):"
echo "  git add ."
echo "  git commit -m 'Update error message wording; add force-expire testing utility'"
echo "  git push"
echo ""
echo "Once confirmed on dev, remove this script:"
echo "  rm apply_wording_updates.sh"
