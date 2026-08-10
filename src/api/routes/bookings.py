import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Literal
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBearer
from pydantic import BaseModel

from src.api.models.booking import (
    BookingRequest, BookingResponse, BookingCheckoutResponse, BookingStatus,
    BOOKING_TYPE_RULES, CHECKOUT_SESSION_EXPIRY_MINUTES, AVAILABLE_TIMES_BY_TYPE,
    BOOKING_TYPE_DISPLAY_NAMES, LOCATION_DETAIL_TO_BOOKING_TYPE, requires_slot_claim,
    get_session_end_datetime,
)
from src.api.core.security import get_security_service
from src.api.core.database import get_db_service
from src.api.core.bookings_repository import BookingRepository
from src.api.core.stripe_service import get_stripe_service
from src.api.core.ses_service import get_ses_service
from src.api.core.exceptions import (
    AppError, BookingNotFoundError, BookingAlreadyCancelledError,
    SlotProcessingError, SlotTakenError, TooManyProcessingBookingsError,
    InvalidBookingRequestError, NoShowTooEarlyError,
)
from src.api.core.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter()


def get_client_ip(http_request: Request) -> str:
    """Extracted as its own dependency, not inlined — lets tests override
    the simulated IP per-test via app.dependency_overrides, which is the
    clean, idiomatic way to simulate different clients in FastAPI tests
    (TestClient itself doesn't support faking different client IPs
    directly)."""
    return http_request.client.host if http_request.client else "unknown"


@router.get(
    "/available-times",
    summary="Get Available Times By Booking Type",
    description="Returns the full mapping of booking type to its offered "
                "times. Public, no auth — the frontend fetches this once "
                "and looks up times locally as the person changes their "
                "selected booking type, rather than a real-time-conflict-"
                "aware slot picker. Changing gym hours means editing "
                "AVAILABLE_TIMES_BY_TYPE in the backend and redeploying — "
                "nothing on the frontend ever needs to change.",
)
async def get_available_times():
    return AVAILABLE_TIMES_BY_TYPE
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
    past a required field, which defeats the point of collecting it.

    cancellation_type distinguishes WHO the cancellation is really about:
    "emergency" (Debo's own side — sick, gym closed, unavailable) keeps
    auto-refund eligibility and stays unrestricted by time. "no_show"
    (the CLIENT didn't show up) never auto-refunds — that's on them, not
    Debo — and can only be marked once the session has actually
    concluded (see NoShowTooEarlyError)."""
    reason: Optional[str] = None
    cancellation_type: Literal["emergency", "no_show"] = "emergency"


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
    comment above for why this is a display filter, not a stored flag.

    Anchored to the session's REAL end time (get_session_end_datetime),
    not a fixed offset from start — a 2-hour Adult class needs the same
    1-hour grace period AFTER it actually concludes, not 1 hour after it
    starts (which would hide it from the admin list while the class is
    still literally in progress)."""
    session_end = get_session_end_datetime(item)
    return datetime.now(timezone.utc) - session_end > HISTORY_VISIBILITY_WINDOW


@router.post(
    "",
    response_model=BookingCheckoutResponse,
    status_code=201,
    summary="Create Booking (starts Stripe checkout)",
    description="Submit a new session booking. Returns a Stripe checkout URL — "
                "the booking is NOT confirmed until payment succeeds via webhook.",
)
async def create_booking(
    request: BookingRequest,
    http_request: Request,
    client_ip: str = Depends(get_client_ip),
):
    # One IP can only have ONE booking sitting in "processing" at a time —
    # checked FIRST, before touching slot-claims or anything else, so this
    # fails fast without any DynamoDB writes if it's going to fail at all.
    # Known, accepted tradeoff: people sharing a network (family wifi,
    # office) could share an IP and trip this even as different customers
    # — the error message gives them a clear path forward regardless.
    repo = _get_repository()
    existing = repo.find_processing_booking_by_ip(client_ip)
    if existing is not None:
        raise TooManyProcessingBookingsError(
            "You currently have a booking in progress. Check your email to finish "
            "that booking before starting another one."
        )

    # Combo + schedule validation — moved here from a Pydantic model_validator
    # that used to raise a plain ValueError. That got wrapped in FastAPI's
    # own default validation format (an array of error objects), which our
    # frontend couldn't render as text — a real bug that crashed the
    # booking form. This explicit check raises a proper AppError subclass
    # instead, returning the same clean string format every other
    # rejection in this app already uses. Checked BEFORE any side effects
    # (slot claims, DynamoDB writes), same discipline as the IP check above.
    combo = (request.location, request.session_detail)
    if combo not in LOCATION_DETAIL_TO_BOOKING_TYPE:
        raise InvalidBookingRequestError(
            f"'{request.session_detail.value}' is not offered at location '{request.location.value}'"
        )

    booking_type = request.booking_type  # safe now — combo is confirmed valid above

    session_date_parsed = datetime.strptime(request.session_date, "%Y-%m-%d")

    # Genuine gap found in production: nothing server-side ever rejected a
    # PAST date. The frontend's date picker has a min={today} attribute,
    # but that's purely client-side UI — trivially bypassed by anyone
    # calling the API directly, same "never trust client-side as the only
    # enforcement" principle already applied to weekday/combo validation
    # below. Without this, Stripe would happily charge someone for a
    # session dated yesterday — real money for something nobody can ever
    # attend, and invisible to the admin's normal 7-day list window since
    # nothing queries backward in time by default.
    if session_date_parsed.date() < datetime.now(timezone.utc).date():
        raise InvalidBookingRequestError("Session date can't be in the past.")

    allowed_weekdays = BOOKING_TYPE_RULES[booking_type]["allowed_weekdays"]
    if session_date_parsed.weekday() not in allowed_weekdays:
        weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        allowed_names = [weekday_names[d] for d in sorted(allowed_weekdays)]
        raise InvalidBookingRequestError(
            f"{BOOKING_TYPE_DISPLAY_NAMES[booking_type.value]} is only available on: {', '.join(allowed_names)}"
        )

    # Price is ALWAYS looked up server-side from BOOKING_TYPE_RULES, never
    # accepted as a value from the client. If the client could send its own
    # price, anyone could book a $100 Gene's adult session and submit
    # price_usd=1 — the server is the only source of truth for what
    # something costs, the request only says WHAT was booked, never
    # WHAT IT COSTS.
    price_usd = BOOKING_TYPE_RULES[booking_type]["price_usd"]
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
        "client_ip": client_ip,
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
    # Framer is wired in, point success_url at the real confirmation page.
    # cancel_url deliberately points at OUR OWN backend, not Framer — this
    # endpoint works right now without needing any frontend page to exist,
    # and can be swapped to a nicer Framer page later once that's built.
    base_url = os.environ.get("FRONTEND_BASE_URL", "https://debosboxingandfitness.com")
    api_base_url = str(http_request.base_url).rstrip("/")
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
            cancel_url=f"{api_base_url}/bookings/{booking_id}/cancel-checkout",
            expires_in_minutes=CHECKOUT_SESSION_EXPIRY_MINUTES,
        )
    except Exception:
        # Same reasoning as above — if Stripe itself fails, don't leave a
        # phantom claim/booking behind with no way to ever pay for it.
        if requires_slot_claim(booking_type):
            repo.release_slot(request.session_date, request.session_time)
        raise

    # Stored so /cancel-checkout can look up and force-expire THIS exact
    # session when someone explicitly cancels, rather than making them
    # wait out the full 30-minute natural expiry.
    repo.set_stripe_session_id(booking_id, session.id)

    logger.info(
        "Checkout started: %s %s (%s, $%s) booking_id=%s",
        request.session_date, request.session_time, booking_type.value, price_usd, booking_id,
    )

    # Recovery path — if they don't complete payment right now (closed
    # tab, dropped connection, distracted), this is how they get back
    # into the SAME checkout session without their booking sitting
    # stranded for the full 30-minute expiry with no way back in.
    get_ses_service().send_checkout_link(
        {"name": request.name, "email": request.email,
         "session_date": request.session_date, "session_time": request.session_time},
        checkout_url=session.url,
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
    show_expired: bool = Query(False, description="Dedicated follow-up-leads view — when "
                                "True, returns ONLY expired bookings instead of the normal "
                                "list. These are real warm leads (name, email, phone all on "
                                "file) who started booking but never paid — worth a follow-up "
                                "call, just never mixed into the default day-to-day list. "
                                "Scoped to the current + previous calendar month — broad "
                                "enough to be a useful catalog without becoming an "
                                "ever-growing pile of stale leads."),
) -> List[BookingResponse]:
    if day_of_week and day_of_week.lower() not in VALID_DAYS:
        raise AppError(f"day_of_week must be one of {VALID_DAYS}")

    repo = _get_repository()
    today = datetime.now(timezone.utc).date()

    if show_expired:
        # Current + previous calendar month, anchored to today, filtering
        # on created_at (when the attempt happened) via a scan — see
        # scan_expired_since()'s docstring for why the date-indexed GSI
        # approach used below for the normal view doesn't work here.
        first_of_this_month = today.replace(day=1)
        if first_of_this_month.month == 1:
            range_start = first_of_this_month.replace(year=first_of_this_month.year - 1, month=12)
        else:
            range_start = first_of_this_month.replace(month=first_of_this_month.month - 1)

        all_items = repo.scan_expired_since(range_start.isoformat())
    else:
        query_dates = [today + timedelta(days=i) for i in range(7)]

        # If a specific day_of_week was given, only query the ONE date in
        # the current week window that falls on that weekday — no reason
        # to issue 7 GSI queries and throw away 6 of them when we can
        # compute which single date we actually need.
        if day_of_week:
            target_weekday = VALID_DAYS.index(day_of_week.lower())
            query_dates = [d for d in query_dates if d.weekday() == target_weekday]

        all_items = []
        for date in query_dates:
            all_items.extend(repo.query_by_date(date.isoformat()))

    # Slot-claim records share the bookings table but aren't real bookings —
    # filter them out before anything else touches this list. They're
    # identifiable by their synthetic "personal-slot#..." booking_id prefix.
    all_items = [item for item in all_items if not item["booking_id"].startswith("personal-slot#")]

    if show_expired:
        # No further status filtering needed — scan_expired_since()
        # already returns only status="expired" items directly.
        visible_items = all_items
    else:
        # History filter — computed fresh on every call, never trusted from a
        # stored flag (see HISTORY_VISIBILITY_WINDOW comment above).
        visible_items = [item for item in all_items if not _is_past_visibility_window(item)]

        # Expired bookings excluded from the DEFAULT view — an abandoned
        # checkout never became a real relationship on its own (no
        # payment), and would just be noise in the day-to-day "who am I
        # training" list. Still fully visible via show_expired=True above,
        # as a genuine follow-up-leads opportunity, just never mixed in
        # here by default.
        visible_items = [item for item in visible_items if item["status"] != "expired"]

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


@router.get(
    "/{booking_id}/cancel-checkout",
    summary="Cancel In-Progress Checkout",
    description="This is where Stripe's own cancel/back link redirects the "
                "browser when someone changes their mind mid-checkout. "
                "Public, no auth — a person canceling their OWN in-progress "
                "checkout shouldn't need to be logged in. Force-expires the "
                "Stripe session immediately (same real webhook path as "
                "natural 30-minute expiry) rather than leaving the slot "
                "claim reserved for no reason once we already know they're "
                "not paying. Returns plain HTML directly since this is a "
                "browser redirect target, not a JSON API call from code — "
                "no Framer page needs to exist yet for this to work.",
)
async def cancel_checkout(booking_id: str):
    repo = _get_repository()
    item = repo.get_by_id(booking_id)

    if item is None:
        return HTMLResponse("<h1>Booking not found</h1>", status_code=404)

    if item["status"] != BookingStatus.processing.value:
        # Idempotent-safe — someone reloading this page, or clicking an
        # old link after the booking already resolved one way or another,
        # shouldn't see a confusing error.
        return HTMLResponse(
            "<h1>This booking has already been resolved.</h1>"
            "<p>No further action needed.</p>"
        )

    session_id = item.get("stripe_session_id")
    if session_id:
        get_stripe_service().expire_checkout_session(session_id)

    logger.info("Checkout explicitly cancelled by customer for booking %s", booking_id)

    return HTMLResponse(
        "<h1>Booking cancelled</h1>"
        "<p>No charge was made. Feel free to book another session anytime.</p>"
    )


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
    cancellation_type = request.cancellation_type if request else "emergency"

    # No-show can only be marked once the session has actually concluded —
    # checked BEFORE the atomic cancel(), using the real end time (type-
    # specific duration, not a fixed assumption). This is a separate,
    # additional business-rule check, not something that needs the same
    # atomicity as the cancel itself — a genuine race between two admins
    # cancelling the same booking simultaneously is still fully handled
    # by cancel()'s own atomic conditional write regardless.
    if cancellation_type == "no_show":
        existing = _get_repository().get_by_id(booking_id)
        if existing is None:
            raise BookingNotFoundError(f"Booking {booking_id} not found")
        if datetime.now(timezone.utc) < get_session_end_datetime(existing):
            raise NoShowTooEarlyError(
                "Can't mark this as a no-show until the session's scheduled time has passed."
            )

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

    # Auto-refund — ONLY for "emergency" cancellations (a no-show is the
    # client's own fault, never auto-refunded — matches real business
    # logic). Also requires this was a genuinely paid booking (receipt_url
    # is only ever set by the webhook after a real charge succeeded — a
    # proxy that survives here since `item` reflects the POST-cancel state,
    # where status has already been overwritten to "cancelled") AND the
    # session hasn't STARTED yet — once a session has begun, even an
    # emergency cancellation no longer auto-refunds; Debo can still issue
    # one manually via Stripe's dashboard if genuinely warranted. Full
    # refunds only, tied directly to this same already-atomic, once-only
    # cancel path — see refund_full_payment's docstring for why that makes
    # this safe against double-refunds without needing separate tracking.
    refund_info = None
    if cancellation_type == "emergency" and item.get("receipt_url") and item.get("stripe_session_id"):
        session_datetime = datetime.strptime(
            f"{item['session_date']} {item['session_time']}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) < session_datetime:
            refund_info = get_stripe_service().refund_full_payment(item["stripe_session_id"])
            if refund_info:
                refund_info["receipt_url"] = item["receipt_url"]
                logger.info("Auto-refund issued for booking %s: $%s", booking_id, refund_info["amount_usd"])
            else:
                logger.error(
                    "Auto-refund FAILED for booking %s — needs manual refund in Stripe dashboard", booking_id
                )

    # DELIBERATE BUSINESS DECISION — the slot claim is NOT released on
    # cancellation. If Debo manually cancels a Personal session, that's
    # almost always for a real reason (unavailable, emergency, etc.), and
    # the exact date+time shouldn't be instantly re-bookable by a stranger
    # without him actively re-opening it. This only affects the ONE
    # specific calendar date+time that was cancelled — slot claims are keyed
    # by exact (date, time), not a recurring weekly pattern, so cancelling
    # Aug 10 at 10am has zero effect on future weeks' Mondays at 10am.

    # Two emails on successful cancellation — one to Debo confirming it
    # happened, one to the client notifying them. Only on the "cancelled"
    # outcome above, never on the "already_cancelled" rejection path (that's
    # a no-op, not a real state change). The `reason` captured earlier
    # (typed by Debo, or DEFAULT_CANCELLATION_REASON if he didn't provide
    # one) goes into both email bodies.
    ses = get_ses_service()
    ses.send_cancellation_notice_to_client(item, reason, refund_info=refund_info)
    ses.send_cancellation_notice_to_admin(item, reason, admin_email=os.environ["ADMIN_EMAIL"])

    logger.info("Booking %s cancelled by admin (reason: %s)", booking_id, reason)
    return _item_to_response(item)
