from enum import Enum
from pydantic import BaseModel, EmailStr, Field


class BookingStatus(str, Enum):
    confirmed = "confirmed"
    cancelled = "cancelled"
    completed = "completed"


class BookingRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    email: EmailStr
    phone: str = Field(..., min_length=7, max_length=20)
    session_date: str = Field(..., description="Format: YYYY-MM-DD")
    session_time: str = Field(..., description="Format: HH:MM (24hr)")


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
