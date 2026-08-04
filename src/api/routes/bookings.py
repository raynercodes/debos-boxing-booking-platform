import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.security import HTTPBearer

from src.api.models.booking import BookingRequest, BookingResponse, BookingStatus
from src.api.core.security import verify_jwt

router = APIRouter()
bearer_scheme = HTTPBearer()

VALID_DAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}


def require_admin(credentials=Depends(bearer_scheme)):
    if not verify_jwt(credentials.credentials):
        raise HTTPException(status_code=401, detail="Invalid or expired admin token")


@router.post(
    "/",
    response_model=BookingResponse,
    status_code=201,
    summary="Create Booking",
    description="""
Submit a new session booking.

📋 **Fill in the form fields** — name, email, phone, session date, and time

▶️ **Click Execute** — a confirmation email is sent automatically once wired to SES

⏳ **Note:** DynamoDB write and SES integration are not yet implemented — this currently
returns a mock response so the API contract is locked in before infrastructure is built.
""",
)
async def create_booking(request: BookingRequest):
    # TODO: write to debos-boxing-bookings table once infrastructure/template.yaml is deployed
    # TODO: trigger SES confirmation email + gym owner notification
    return BookingResponse(
        booking_id=str(uuid.uuid4()),
        name=request.name,
        email=request.email,
        phone=request.phone,
        session_date=request.session_date,
        session_time=request.session_time,
        status=BookingStatus.confirmed,
        created_at=datetime.now(timezone.utc).isoformat(),
        reminder_sent=False,
    )


@router.get(
    "/",
    summary="List Upcoming Bookings (Admin)",
    description="""
Returns bookings for the upcoming 7-day window (today through today+6), sorted
soonest-first by default. **Admin-only endpoint.**

🔐 Requires a valid admin token — obtain one via `POST /auth/login`, then click
**Authorize** above and paste it in.

**Optional filters:**
- `day_of_week` — narrow to a specific day (e.g. `monday`) within the current week window
- `search` — match against client name or phone number
""",
    dependencies=[Depends(require_admin)],
)
async def list_bookings(
    day_of_week: Optional[str] = Query(
        None, description="e.g. 'monday' — filters within the current 7-day window"
    ),
    search: Optional[str] = Query(
        None, description="Matches against client name or phone number"
    ),
):
    if day_of_week and day_of_week.lower() not in VALID_DAYS:
        raise HTTPException(status_code=422, detail=f"day_of_week must be one of {sorted(VALID_DAYS)}")

    # TODO — real implementation once the table + GSI exist:
    #
    # 1. Build today..today+6 date list (Python, not a DynamoDB feature):
    #      today = datetime.now(timezone.utc).date()
    #      week_dates = [(today + timedelta(days=i)).isoformat() for i in range(7)]
    #
    # 2. The session-date-index GSI's partition key is an EXACT date, so a
    #    single Query can't span a date range — issue one Query per date in
    #    week_dates (max 7 calls), merge results. This is intentional: explicit
    #    per-date queries over a Scan, same "no table scans" instinct as fintech,
    #    and 7 calls at gym scale is trivial cost.
    #
    # 3. If day_of_week given, only query the ONE matching date instead of all 7
    #    (compute which date in week_dates falls on that weekday first).
    #
    # 4. If search given, filter the merged results in Python on name/phone
    #    (case-insensitive substring match) — not worth a GSI for this at this
    #    data volume; a dedicated search index would be overengineering here.
    #
    # 5. Sort merged results by (session_date, session_time) ascending — this
    #    naturally puts today's soonest session first with no extra logic,
    #    since week_dates already starts at today.
    return []


@router.get(
    "/{booking_id}",
    response_model=BookingResponse,
    summary="Get Booking by ID",
)
async def get_booking(booking_id: str):
    # TODO: get_item from debos-boxing-bookings table
    raise HTTPException(status_code=404, detail="Booking not found")
