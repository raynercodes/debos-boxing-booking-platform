import re
from datetime import datetime
from enum import Enum
from typing import ClassVar, Dict, Set, Tuple

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

# E.164-ish: optional leading +, then 7-15 digits. Covers US numbers with or
# without country code, and international numbers, without being so strict
# it rejects real formats. Kept as a module-level constant so both booking.py
# and lead.py can share the exact same rule instead of drifting apart.
PHONE_PATTERN = re.compile(r"^\+?[0-9]{7,15}$")


class BookingStatus(str, Enum):
    confirmed = "confirmed"
    cancelled = "cancelled"
    completed = "completed"


class BookingType(str, Enum):
    """The four REAL offerings that exist, confirmed directly from Debo —
    not a guess. This went through two earlier, wrong shapes before this:
      1. First pass treated 'genes' as a third category alongside
         personal/kids, as if location and session type were the same axis.
      2. Second pass split location (is_genes bool) from session type
         (personal/kids enum) as two fully independent fields, assuming
         kids training could happen at either location — it can't.
    This third pass restores the location/subtype split (matching the
    original reference table), but with the two fields now CROSS-VALIDATED
    below so only the 4 real combinations are ever accepted — the earlier
    version's mistake wasn't having two fields, it was letting them vary
    independently when reality doesn't allow that.
    """
    personal_client_travels = "personal_client_travels"  # client comes to Debo — $40, Mon-Fri
    personal_trainer_travels = "personal_trainer_travels"  # Debo goes to client — $50, Mon-Fri
    genes_adult = "genes_adult"  # Gene's location, adults — $100, Mon-Thu
    genes_kids = "genes_kids"  # Gene's location, ages 6-13 — $90, Mon-Wed


class Location(str, Enum):
    """Where the session happens. Only two real values exist right now."""
    mobile_personal = "mobile_personal"  # "Mobile/Personal, not Gene's"
    genes = "genes"


class SessionDetail(str, Enum):
    """The detail WITHIN a location. Which values are valid depends on
    which Location was chosen — client_travels/trainer_travels only make
    sense for mobile_personal, adult/kids only make sense for genes. That
    dependency is exactly why this can't be a single flat field on its own;
    the model_validator below enforces the valid (location, detail) pairs."""
    client_travels = "client_travels"  # only valid with mobile_personal
    trainer_travels = "trainer_travels"  # only valid with mobile_personal
    adult = "adult"  # only valid with genes
    kids = "kids"  # only valid with genes


# Maps the (location, detail) pair to the resulting BookingType, plus
# validates that combination is one of the 4 real offerings. Using a tuple
# key here means an invalid pairing (e.g. genes + client_travels) simply
# isn't IN this dict at all — the lookup itself fails naturally rather than
# needing a separate list of "allowed pairs" to keep in sync.
LOCATION_DETAIL_TO_BOOKING_TYPE: Dict[Tuple[Location, SessionDetail], BookingType] = {
    (Location.mobile_personal, SessionDetail.client_travels): BookingType.personal_client_travels,
    (Location.mobile_personal, SessionDetail.trainer_travels): BookingType.personal_trainer_travels,
    (Location.genes, SessionDetail.adult): BookingType.genes_adult,
    (Location.genes, SessionDetail.kids): BookingType.genes_kids,
}


# Confirmed business rules — price and which weekdays each booking type is
# offered on. This is a lookup table, not something the client ever sends or
# controls (see the price note on BookingResponse below for why that matters).
# weekday() returns 0=Monday ... 6=Sunday, which is why the allowed-day sets
# below use those integers rather than day names.
BOOKING_TYPE_RULES: Dict[BookingType, dict] = {
    BookingType.personal_client_travels: {"price_usd": 40, "allowed_weekdays": {0, 1, 2, 3, 4}},  # Mon-Fri
    BookingType.personal_trainer_travels: {"price_usd": 50, "allowed_weekdays": {0, 1, 2, 3, 4}},  # Mon-Fri
    BookingType.genes_adult: {"price_usd": 100, "allowed_weekdays": {0, 1, 2, 3}},  # Mon-Thu
    BookingType.genes_kids: {"price_usd": 90, "allowed_weekdays": {0, 1, 2}},  # Mon-Wed
}

KIDS_AGE_RANGE = "6-13"  # confirmed — used for the UI label, e.g. "Kids (ages 6-13)"


class BookingRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    email: EmailStr
    # Deliberately a str, not an int — phone numbers aren't numbers you do
    # math on. An int silently drops leading zeros and can't represent a
    # '+' country-code prefix at all. Format is enforced via regex instead,
    # which catches garbage input without the downsides of int.
    phone: str = Field(..., min_length=7, max_length=20)
    session_date: str = Field(..., description="Format: YYYY-MM-DD")
    session_time: str = Field(..., description="Format: HH:MM (24hr)")
    location: Location
    session_detail: SessionDetail

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name cannot be blank or whitespace only")
        return stripped

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        cleaned = value.strip()
        if not PHONE_PATTERN.match(cleaned):
            raise ValueError("phone must be 7-15 digits, optionally prefixed with +")
        return cleaned

    @field_validator("session_date")
    @classmethod
    def validate_session_date_format(cls, value: str) -> str:
        """Format check only — the DAY-OF-WEEK check happens below in
        validate_schedule, since that needs session_date AND the resolved
        booking_type together, which a single-field validator can't see."""
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise ValueError("session_date must be in YYYY-MM-DD format")
        return value

    @field_validator("session_time")
    @classmethod
    def validate_session_time_format(cls, value: str) -> str:
        try:
            datetime.strptime(value, "%H:%M")
        except ValueError:
            raise ValueError("session_time must be in 24-hour HH:MM format")
        return value

    @model_validator(mode="after")
    def validate_location_detail_combo_and_schedule(self) -> "BookingRequest":
        """Two checks in one pass, since the second depends on the first
        succeeding:

        1. Is (location, session_detail) one of the 4 real combinations?
           e.g. (genes, client_travels) is NOT a real offering — client_travels
           only applies to mobile_personal. This is what actually prevents
           the earlier design flaw: location and detail LOOK independent as
           two separate fields, but they're not, and this check is what
           enforces that instead of silently accepting a nonsense pairing.

        2. Does session_date's actual weekday match a day the RESOLVED
           booking_type is offered on? e.g. genes_kids is Mon-Wed only.

        The Framer frontend will only ever present valid combinations as
        selectable options, but per the same defense-in-depth reasoning
        applied everywhere else in this file: never trust client-side
        constraints as the ONLY enforcement. A direct API call bypasses
        whatever the UI restricts."""
        combo = (self.location, self.session_detail)
        booking_type = LOCATION_DETAIL_TO_BOOKING_TYPE.get(combo)
        if booking_type is None:
            raise ValueError(
                f"'{self.session_detail.value}' is not offered at location '{self.location.value}'"
            )

        session_date = datetime.strptime(self.session_date, "%Y-%m-%d")
        allowed_weekdays = BOOKING_TYPE_RULES[booking_type]["allowed_weekdays"]
        if session_date.weekday() not in allowed_weekdays:
            weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
            allowed_names = [weekday_names[d] for d in sorted(allowed_weekdays)]
            raise ValueError(
                f"{booking_type.value} is only available on: {', '.join(allowed_names)}"
            )
        return self

    @property
    def booking_type(self) -> BookingType:
        """Derived, not stored — the (location, session_detail) pair is the
        single source of truth. Computing this on demand means there's no
        way for a stored booking_type to ever drift out of sync with the
        location/session_detail it was derived from, since it's never
        actually stored as separate state."""
        return LOCATION_DETAIL_TO_BOOKING_TYPE[(self.location, self.session_detail)]


class BookingResponse(BaseModel):
    booking_id: str
    name: str
    email: EmailStr
    phone: str
    session_date: str
    session_time: str
    booking_type: BookingType
    price_usd: int
    status: BookingStatus
    created_at: str
    reminder_sent: bool


# TODO (future, not MVP): mouthpiece sales (local pickup or delivery) — a
# separate Products/Orders model, deliberately not built now. Confirmed as
# a post-MVP add-on once bookings/leads are flowing well, not a blocker.
#
# Memberships were considered and CONFIRMED NOT A THING — no recurring
# subscription, no membership status tracking, no Members table needed.
# Every booking is a one-time payment via Stripe. This removes an entire
# category of complexity (subscription webhooks, payment-status caching)
# that earlier notes in this file were bracing for.
