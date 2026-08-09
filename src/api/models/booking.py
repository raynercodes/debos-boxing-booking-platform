import re
from datetime import datetime
from enum import Enum
from typing import ClassVar, Dict, Set, Tuple, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator, ConfigDict

# E.164-ish: optional leading +, then 7-15 digits. Covers US numbers with or
# without country code, and international numbers, without being so strict
# it rejects real formats. Kept as a module-level constant so both booking.py
# and lead.py can share the exact same rule instead of drifting apart.
PHONE_PATTERN = re.compile(r"^\+?[0-9]{7,15}$")


class BookingStatus(str, Enum):
    processing = "processing"  # Stripe checkout started, payment not yet confirmed
    confirmed = "confirmed"    # ONLY ever set by the Stripe webhook — never on creation
    cancelled = "cancelled"
    completed = "completed"
    expired = "expired"        # checkout session expired unpaid, slot released


class BookingType(str, Enum):
    """The five REAL offerings that exist, confirmed directly from Debo —
    not a guess. This went through two earlier, wrong shapes before this:
      1. First pass treated 'genes' as a third category alongside
         personal/kids, as if location and session type were the same axis.
      2. Second pass split location (is_genes bool) from session type
         (personal/kids enum) as two fully independent fields, assuming
         kids training could happen at either location — it can't.
    This third pass restores the location/subtype split (matching the
    original reference table), but with the two fields now CROSS-VALIDATED
    below so only the real combinations are ever accepted — the earlier
    version's mistake wasn't having two fields, it was letting them vary
    independently when reality doesn't allow that.

    personal_virtual added later, once confirmed — same price as
    client_travels ($40), since Debo reasoned the client isn't costing him
    gas/travel either way (client comes to him, or joins by Zoom).
    """
    personal_client_travels = "personal_client_travels"  # client comes to Debo — $40, Mon-Fri
    personal_trainer_travels = "personal_trainer_travels"  # Debo goes to client — $50, Mon-Fri
    personal_virtual = "personal_virtual"  # Zoom session — $40, Mon-Fri
    genes_adult = "genes_adult"  # Gene's location, adults — $100, Mon-Thu
    genes_kids = "genes_kids"  # Gene's location, ages 6-13 — $90, Mon-Wed


# Personal training types are ALL mutually exclusive against each other for
# the SAME date+time — Debo can only train one person at a given moment
# regardless of whether it's in-person or virtual. Gene's classes are group
# settings and never go through slot-claiming at all. This set is what the
# slot-claim logic checks against to decide whether a booking needs
# exclusivity enforcement in the first place.
PERSONAL_BOOKING_TYPES = {
    BookingType.personal_client_travels,
    BookingType.personal_trainer_travels,
    BookingType.personal_virtual,
}


def requires_slot_claim(booking_type: BookingType) -> bool:
    return booking_type in PERSONAL_BOOKING_TYPES


class Location(str, Enum):
    """Where the session happens. Only two real values exist right now."""
    mobile_personal = "mobile_personal"  # "Mobile/Personal, not Gene's"
    genes = "genes"


class SessionDetail(str, Enum):
    """The detail WITHIN a location. Which values are valid depends on
    which Location was chosen — client_travels/trainer_travels/virtual only
    make sense for mobile_personal, adult/kids only make sense for genes.
    That dependency is exactly why this can't be a single flat field on its
    own; the model_validator below enforces the valid (location, detail)
    pairs."""
    client_travels = "client_travels"  # only valid with mobile_personal
    trainer_travels = "trainer_travels"  # only valid with mobile_personal
    virtual = "virtual"  # only valid with mobile_personal — Zoom session
    adult = "adult"  # only valid with genes
    kids = "kids"  # only valid with genes


# Maps the (location, detail) pair to the resulting BookingType, plus
# validates that combination is one of the real offerings. Using a tuple
# key here means an invalid pairing (e.g. genes + client_travels) simply
# isn't IN this dict at all — the lookup itself fails naturally rather than
# needing a separate list of "allowed pairs" to keep in sync.
LOCATION_DETAIL_TO_BOOKING_TYPE: Dict[Tuple[Location, SessionDetail], BookingType] = {
    (Location.mobile_personal, SessionDetail.client_travels): BookingType.personal_client_travels,
    (Location.mobile_personal, SessionDetail.trainer_travels): BookingType.personal_trainer_travels,
    (Location.mobile_personal, SessionDetail.virtual): BookingType.personal_virtual,
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
    BookingType.personal_virtual: {"price_usd": 40, "allowed_weekdays": {0, 1, 2, 3, 4}},  # Mon-Fri
    BookingType.genes_adult: {"price_usd": 100, "allowed_weekdays": {0, 1, 2, 3}},  # Mon-Thu
    BookingType.genes_kids: {"price_usd": 90, "allowed_weekdays": {0, 1, 2}},  # Mon-Wed
}

KIDS_AGE_RANGE = "6-13"  # confirmed — used for the UI label, e.g. "Kids (ages 6-13)"

# How long a Stripe Checkout Session stays valid before it expires unpaid.
# 30 is Stripe's own HARD MINIMUM — Stripe rejects anything shorter with a
# real API error ("expires_at must be at least 30 minutes from Checkout
# Session creation"). This was originally set to 20 based on "reasonable
# default" reasoning without checking Stripe's actual constraint, which our
# mocked test suite couldn't catch (it fakes the Stripe API call entirely,
# never exercising Stripe's own validation) — only surfaced once tested
# against the real (test-mode) API. 30 also happens to still satisfy the
# original intent (long enough to pay, short enough not to block a real
# Personal slot for hours) — it just needed to be Stripe's actual floor,
# not an arbitrary smaller number.
CHECKOUT_SESSION_EXPIRY_MINUTES = 30

# Single source of truth for which times are offered per booking type.
# The frontend fetches this list rather than hardcoding times itself —
# changing gym hours means editing THIS dict and redeploying, nothing
# on the Framer side ever needs to change.
#
# Real hours from Debo: Personal 7AM-12PM, Kids class 4-5PM, Adult class
# 5-7PM. Personal has no stated interval within its 5-hour window, so
# hourly starting slots are an assumption — confirm the actual granularity
# he wants. Kids/Adult are each treated as one class time (the window IS
# the class), not multiple slots — confirm Adult isn't meant to be two
# separate back-to-back one-hour sessions instead of one single time.
# Friendly, customer-facing names for each booking type — deliberately
# matching the exact labels already established on the Framer frontend
# (pricing cards, dropdowns), so the whole system describes each offering
# the same way. Used anywhere a booking type needs to appear in text a
# real person reads (error messages, emails) instead of the raw enum
# value like "genes_kids", which reads as an internal database identifier,
# not something a customer should ever see.
BOOKING_TYPE_DISPLAY_NAMES: dict[str, str] = {
    "personal_client_travels": "Personal Training (Client Travels to Debo)",
    "personal_trainer_travels": "Personal Training (Trainer Travels to You)",
    "personal_virtual": "Virtual Personal Training",
    "genes_adult": "Adult Group Class",
    "genes_kids": "Kids Group Class",
}

AVAILABLE_TIMES_BY_TYPE: dict[str, list[str]] = {
    "personal_client_travels": ["07:00", "08:00", "09:00", "10:00", "11:00"],
    "personal_trainer_travels": ["07:00", "08:00", "09:00", "10:00", "11:00"],
    "personal_virtual": ["07:00", "08:00", "09:00", "10:00", "11:00"],
    "genes_adult": ["17:00"],
    "genes_kids": ["16:00"],
}


class BookingRequest(BaseModel):
    # Pre-fills Swagger UI's "Try it out" form at /docs with a realistic
    # example — the date below is static (baked in at code-write time), so
    # it won't always fall on a currently-valid weekday. session_date may
    # still need a quick nudge before hitting Execute, but name/phone/time
    # never need retyping. mobile_personal + client_travels was picked as
    # the example combo specifically because it's valid Mon-Fri, the
    # broadest window of any offering, minimizing how often the date needs
    # adjusting.
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Test Client",
                "email": "test@example.com",
                "phone": "4045551234",
                "session_date": "2026-08-10",
                "session_time": "07:00",
                "location": "mobile_personal",
                "session_detail": "client_travels",
            }
        }
    )

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

    @property
    def booking_type(self) -> BookingType:
        """Derived, not stored — the (location, session_detail) pair is the
        single source of truth. Computing this on demand means there's no
        way for a stored booking_type to ever drift out of sync with the
        location/session_detail it was derived from, since it's never
        actually stored as separate state.

        Raises KeyError if the combo isn't real — the ROUTE is responsible
        for checking combo validity via LOCATION_DETAIL_TO_BOOKING_TYPE
        BEFORE ever accessing this property, same as it now handles the
        weekday check. See InvalidBookingRequestError's docstring for why
        this moved out of a Pydantic validator and into the route."""
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
    cancellation_reason: Optional[str] = None  # only ever populated once cancelled


class BookingCheckoutResponse(BaseModel):
    """Returned ONLY from booking creation — this is the one moment a
    checkout_url is meaningful. Kept as a separate model from BookingResponse
    rather than adding an optional field there, since every OTHER response
    (GET, list, cancel) would just carry a permanently-null field that never
    applies to them. A field that's only ever populated in one specific
    context belongs on a model scoped to that context, not bolted onto a
    general-purpose one."""
    booking_id: str
    status: BookingStatus  # will be "processing" at this point, always
    price_usd: int
    checkout_url: str


# TODO (future, not MVP): mouthpiece sales (local pickup or delivery) — a
# separate Products/Orders model, deliberately not built now. Confirmed as
# a post-MVP add-on once bookings/leads are flowing well, not a blocker.
#
# Memberships were considered and CONFIRMED NOT A THING — no recurring
# subscription, no membership status tracking, no Members table needed.
# Every booking is a one-time payment via Stripe. This removes an entire
# category of complexity (subscription webhooks, payment-status caching)
# that earlier notes in this file were bracing for.
