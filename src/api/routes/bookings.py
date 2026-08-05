import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from fastapi import APIRouter, Depends, Query
from fastapi.security import HTTPBearer

from src.api.models.booking import BookingRequest, BookingResponse, BookingStatus, BOOKING_TYPE_RULES
from src.api.core.security import get_security_service
from src.api.core.database import get_db_service
from src.api.core.bookings_repository import BookingRepository
from src.api.core.exceptions import AppError
from src.api.core.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter()
bearer_scheme = HTTPBearer()

VALID_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

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


class BookingNotFoundError(AppError):
    """A specific 404 case — kept as its own type rather than reusing a
    generic AppError so the exception handler in main.py can map it to 404
    specifically instead of the catch-all AppError's 400."""


class BookingAlreadyCancelledError(AppError):
    """A specific 409 (Conflict) case. Confirmed requirement: the UI should
    only ever offer "cancel" as an option when a booking ISN'T already
    cancelled — but per the same defense-in-depth principle applied
    everywhere else in this file, the backend enforces this independently
    too, not just the frontend. This is also what prevents a real duplicate-
    email bug: once cancellation emails are wired in (to both Debo and the
    client), a second cancel attempt on an already-cancelled booking must
    NOT re-trigger those emails."""


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
    )


def _is_past_visibility_window(item: dict) -> bool:
    """Explicit, computed-at-read-time check — see HISTORY_VISIBILITY_WINDOW
    comment above for why this is a display filter, not a stored flag."""
    session_start = datetime.strptime(
        f"{item['session_date']}T{item['session_time']}", "%Y-%m-%dT%H:%M"
    ).replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - session_start > HISTORY_VISIBILITY_WINDOW


@router.post(
    "/",
    response_model=BookingResponse,
    status_code=201,
    summary="Create Booking",
    description="Submit a new session booking.",
)
async def create_booking(request: BookingRequest):
    # Price is ALWAYS looked up server-side from BOOKING_TYPE_RULES, never
    # accepted as a value from the client. If the client could send its own
    # price, anyone could book a $100 Gene's adult session and submit
    # price_usd=1 — the server is the only source of truth for what
    # something costs, the request only says WHAT was booked, never
    # WHAT IT COSTS.
    price_usd = BOOKING_TYPE_RULES[request.booking_type]["price_usd"]

    item = {
        "booking_id": str(uuid.uuid4()),
        "name": request.name,
        "email": request.email,
        "phone": request.phone,
        "session_date": request.session_date,
        "session_time": request.session_time,
        "booking_type": request.booking_type.value,
        # location/session_detail stored alongside the derived booking_type
        # even though BookingResponse doesn't expose them yet — cheap to
        # store on a schemaless DynamoDB item, and useful for future
        # reporting (e.g. "all genes bookings" or "all mobile bookings")
        # without needing to parse booking_type strings.
        "location": request.location.value,
        "session_detail": request.session_detail.value,
        "price_usd": price_usd,
        "status": BookingStatus.confirmed.value,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reminder_sent": False,
    }

    _get_repository().create(item)

    # TODO: trigger SES confirmation email + gym owner notification
    # TODO: create a Stripe Payment Intent / Checkout Session for price_usd
    #       once payments are wired in — one-time charge only.

    logger.info(
        "Booking created: %s %s (%s, $%s)",
        request.session_date, request.session_time, request.booking_type.value, price_usd,
    )
    return _item_to_response(item)


@router.get(
    "/",
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
async def cancel_booking(booking_id: str):
    # Atomic cancel — see BookingRepository.cancel docstring for why this is
    # ONE conditional write rather than a separate check-then-update pair.
    result = _get_repository().cancel(booking_id)

    if result["outcome"] == "not_found":
        raise BookingNotFoundError(f"Booking {booking_id} not found")

    if result["outcome"] == "already_cancelled":
        # No emails fire here — this is a rejected no-op, not a state change.
        raise BookingAlreadyCancelledError(f"Booking {booking_id} is already cancelled")

    # TODO (future, once SES is wired in): send TWO emails on successful
    # cancellation — one to Debo confirming the cancellation happened, and
    # one to the original booker (client) notifying them their session was
    # cancelled. Both emails belong HERE, only on the "cancelled" outcome
    # above, never on the "already_cancelled" rejection path.
    logger.info("Booking %s cancelled by admin", booking_id)
    return _item_to_response(result["item"])
