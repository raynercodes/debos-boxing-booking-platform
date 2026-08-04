import re
from datetime import datetime
from enum import Enum
from pydantic import BaseModel, EmailStr, Field, field_validator

# E.164-ish: optional leading +, then 7-15 digits. Covers US numbers with or
# without country code, and international numbers, without being so strict
# it rejects real formats. Kept as a module-level constant so both booking.py
# and lead.py can share the exact same rule instead of drifting apart.
PHONE_PATTERN = re.compile(r"^\+?[0-9]{7,15}$")


class BookingStatus(str, Enum):
    confirmed = "confirmed"
    cancelled = "cancelled"
    completed = "completed"


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
    def validate_session_date(cls, value: str) -> str:
        """The Framer frontend will only ever present the gym's real
        pre-configured available slots — a client can't type a free-text
        date. But this validator stays regardless: never trust client-side
        constraints as the ONLY line of defense. A direct API call (someone
        testing with curl/Postman, or a bug in the frontend) bypasses
        whatever the UI restricts, so the backend has to independently
        enforce the same rule no matter how the request arrives. Format-only
        here — validating it's an ACTUAL configured slot (not just a
        correctly-shaped date) is a separate, later check once the gym's
        real schedule/availability data exists."""
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise ValueError("session_date must be in YYYY-MM-DD format")
        return value

    @field_validator("session_time")
    @classmethod
    def validate_session_time(cls, value: str) -> str:
        try:
            datetime.strptime(value, "%H:%M")
        except ValueError:
            raise ValueError("session_time must be in 24-hour HH:MM format")
        return value


class BookingResponse(BaseModel):
    booking_id: str
    name: str
    email: EmailStr
    phone: str
    session_date: str
    session_time: str
    status: BookingStatus
    created_at: str
    reminder_sent: bool


# TODO (future, not MVP): if customer accounts get added later (e.g. mouthpiece
# sales / e-commerce), this is where a UserAccount model would live, separate
# from the single-admin auth in core/security.py — different identity, different
# threat model, don't retrofit the admin JWT pattern onto customer logins.
