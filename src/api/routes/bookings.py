import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, Query
from fastapi.security import HTTPBearer

from src.api.models.booking import BookingRequest, BookingResponse, BookingStatus, BOOKING_TYPE_RULES
from src.api.core.security import get_security_service
from src.api.core.exceptions import AppError
from src.api.core.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter()
bearer_scheme = HTTPBearer()

VALID_DAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}

# How long a booking stays visible in the admin list after its start time has
# passed. Deliberately a DISPLAY filter applied at query time, NOT a deletion
# policy — the record stays in DynamoDB permanently (useful for history,
# case-study metrics, and the gym owner's own records). This mirrors the
# fintech project's TTL lesson: never rely on a timestamp-based mechanism to
# physically remove something on a tight schedule — DynamoDB TTL deletion can
# lag up to 48 hours, which would be useless for a "gone within an hour" rule.
# Explicit evaluation in application code, every time, is the only way to
# guarantee this window actually holds.
HISTORY_VISIBILITY_WINDOW = timedelta(hours=1)


class BookingNotFoundError(AppError):
    """A specific 404 case — kept as its own type rather than reusing a
    generic AppError so the exception handler in main.py can map it to 404
    specifically instead of the catch-all AppError's 400."""


def require_admin(credentials=Depends(bearer_scheme)):
    """Delegates to SecurityService, which raises InvalidTokenError on
    failure — translated to a 401 by the global exception handler in
    main.py, so this stays a one-line dependency."""
    get_security_service().require_admin_token(credentials.credentials)


@router.post(
    "/",
    response_model=BookingResponse,
    status_code=201,
    summary="Create Booking",
    description="Submit a new session booking. DynamoDB write + SES confirmation "
                "are TODO until infrastructure/template.yaml is deployed.",
)
async def create_booking(request: BookingRequest):
    # TODO: write to debos-boxing-bookings table once infrastructure/template.yaml is deployed
    # TODO: trigger SES confirmation email + gym owner notification
    # TODO: create a Stripe Payment Intent / Checkout Session for price_usd
    #       once payments are wired in — one-time charge only, no
    #       subscription/recurring billing needed (memberships confirmed
    #       not a thing).

    # Price is ALWAYS looked up server-side from BOOKING_TYPE_RULES, never
    # accepted as a value from the client. If the client could send its own
    # price, anyone could book a $100 Gene's adult session and submit
    # price_usd=1 — the server is the only source of truth for what
    # something costs, the request only says WHAT was booked, never
    # WHAT IT COSTS.
    price_usd = BOOKING_TYPE_RULES[request.booking_type]["price_usd"]

    logger.info(
        "Booking created for session %s %s (%s, $%s)",
        request.session_date, request.session_time, request.booking_type.value, price_usd,
    )
    return BookingResponse(
        booking_id=str(uuid.uuid4()),
        name=request.name,
        email=request.email,
        phone=request.phone,
        session_date=request.session_date,
        session_time=request.session_time,
        booking_type=request.booking_type,
        price_usd=price_usd,
        status=BookingStatus.confirmed,
        created_at=datetime.now(timezone.utc).isoformat(),
        reminder_sent=False,
    )


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
):
    if day_of_week and day_of_week.lower() not in VALID_DAYS:
        raise AppError(f"day_of_week must be one of {sorted(VALID_DAYS)}")

    # TODO — real implementation once the table + GSI exist:
    #
    # 1. Build today..today+6 date list (Python, not a DynamoDB feature):
    #      today = datetime.now(timezone.utc).date()
    #      week_dates = [(today + timedelta(days=i)).isoformat() for i in range(7)]
    #
    # 2. The session-date-index GSI's partition key is an EXACT date, so a
    #    single Query can't span a date range — issue one Query per date in
    #    week_dates (max 7 calls), merge results. Explicit per-date queries
    #    over a Scan, same "no table scans" instinct as fintech.
    #
    # 3. If day_of_week given, only query the ONE matching date instead of all 7.
    #
    # 4. If search given, filter merged results in Python on name/phone
    #    (case-insensitive substring match) — not worth a GSI at this volume.
    #
    # 5. HISTORY FILTER (the "disappear after an hour" requirement) — for each
    #    result, compute:
    #      session_start = datetime.fromisoformat(f"{item['session_date']}T{item['session_time']}")
    #      if datetime.now(timezone.utc) - session_start > HISTORY_VISIBILITY_WINDOW:
    #          exclude from results  # still exists in DynamoDB, just not shown here
    #    This is evaluated HERE, at read time, every single call — never
    #    trust a stored "is_visible" flag that would need a separate process
    #    to keep updated. Same explicit-evaluation principle as TTL handling.
    #
    # 6. Sort merged results by (session_date, session_time) ascending — today's
    #    soonest session lands first automatically since week_dates starts at today.
    return []


@router.get(
    "/{booking_id}",
    response_model=BookingResponse,
    summary="Get Booking by ID",
)
async def get_booking(booking_id: str):
    # TODO: get_item from debos-boxing-bookings table
    raise BookingNotFoundError(f"Booking {booking_id} not found")


@router.patch(
    "/{booking_id}/cancel",
    response_model=BookingResponse,
    summary="Cancel Booking (Admin)",
    description="Admin-only. Cancels a booking at any time regardless of how "
                "close it is to the session start.",
    dependencies=[Depends(require_admin)],
)
async def cancel_booking(booking_id: str):
    # TODO once the table exists:
    #   1. get_item(booking_id) — raise BookingNotFoundError if missing
    #   2. update_item setting status = "cancelled"
    #      NOTE: unlike fintech's loan status transitions, this does NOT need
    #      a ConditionExpression restricting which prior status is valid —
    #      the requirement is explicitly "cancel at any time," so a confirmed,
    #      already-completed, or even already-cancelled booking can all be
    #      set to cancelled without error. No state machine restriction here,
    #      on purpose, matching what was actually asked for.
    #   3. TODO (future): trigger a cancellation notice email via SES
    logger.info("Booking %s cancelled by admin", booking_id)
    raise BookingNotFoundError(f"Booking {booking_id} not found")
