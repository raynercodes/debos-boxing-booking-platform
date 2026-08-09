from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.api.models.booking import BookingRequest, BOOKING_TYPE_RULES, BookingType, Location, SessionDetail


def _valid_date_for(booking_type: BookingType) -> str:
    allowed = BOOKING_TYPE_RULES[booking_type]["allowed_weekdays"]
    today = datetime.now(timezone.utc).date()
    for offset in range(7):
        candidate = today + timedelta(days=offset)
        if candidate.weekday() in allowed:
            return candidate.isoformat()
    raise RuntimeError("no valid date found — should be impossible")


def _base_payload(**overrides) -> dict:
    payload = {
        "name": "Test Client",
        "email": "test@example.com",
        "phone": "4045551234",
        "session_date": _valid_date_for(BookingType.genes_adult),
        "session_time": "10:00",
        "location": "genes",
        "session_detail": "adult",
    }
    payload.update(overrides)
    return payload


def test_valid_booking_accepted():
    r = BookingRequest(**_base_payload())
    assert r.booking_type == BookingType.genes_adult


@pytest.mark.parametrize("bad_phone", [
    "abc-def-ghij",       # letters
    "123",                # too short
    "1" * 20,             # too long (20 digits exceeds the 15-digit cap)
    "",                   # blank
    "555 123 4567 ext 9", # spaces/text mixed in
])
def test_invalid_phone_rejected(bad_phone):
    with pytest.raises(ValidationError):
        BookingRequest(**_base_payload(phone=bad_phone))


def test_phone_with_country_code_accepted():
    """+ prefix should be accepted — this is exactly why phone stayed a
    str instead of becoming an int (see booking.py's PHONE_PATTERN comment)."""
    r = BookingRequest(**_base_payload(phone="+14045551234"))
    assert r.phone == "+14045551234"


@pytest.mark.parametrize("bad_name", ["", "   ", "\t\n"])
def test_blank_or_whitespace_name_rejected(bad_name):
    with pytest.raises(ValidationError):
        BookingRequest(**_base_payload(name=bad_name))


def test_name_gets_stripped():
    r = BookingRequest(**_base_payload(name="  Test Client  "))
    assert r.name == "Test Client"


@pytest.mark.parametrize("bad_email", ["not-an-email", "missing-at-sign.com", "@no-local-part.com", ""])
def test_invalid_email_rejected(bad_email):
    with pytest.raises(ValidationError):
        BookingRequest(**_base_payload(email=bad_email))


@pytest.mark.parametrize("bad_date", [
    "not-a-date",
    "2026-13-45",   # invalid month/day
    "08/10/2026",   # wrong format (slashes, US order)
])
def test_invalid_session_date_format_rejected(bad_date):
    with pytest.raises(ValidationError):
        BookingRequest(**_base_payload(session_date=bad_date))


def test_non_zero_padded_date_still_accepted():
    """NOT a bug — Python's strptime with %m/%d accepts non-zero-padded
    numbers by design (parses "8" the same as "08"). Documented here so
    it's a known, understood behavior rather than something that looks
    like a validation gap if someone notices it later."""
    r = BookingRequest(**_base_payload(session_date="2026-8-10"))
    assert r.session_date == "2026-8-10"  # stored as-submitted, not reformatted


@pytest.mark.parametrize("bad_time", [
    "not-a-time",
    "25:00",   # hour out of range
    "10:70",   # minute out of range
    "10pm",    # 12-hour format, not accepted
    "10:00:00",  # includes seconds, not accepted
])
def test_invalid_session_time_format_rejected(bad_time):
    with pytest.raises(ValidationError):
        BookingRequest(**_base_payload(session_time=bad_time))


@pytest.mark.parametrize("missing_field", ["name", "email", "phone", "session_date", "session_time", "location", "session_detail"])
def test_missing_required_field_rejected(missing_field):
    payload = _base_payload()
    del payload[missing_field]
    with pytest.raises(ValidationError):
        BookingRequest(**payload)


@pytest.mark.parametrize("booking_type,location,detail", [
    (BookingType.personal_client_travels, "mobile_personal", "client_travels"),
    (BookingType.personal_trainer_travels, "mobile_personal", "trainer_travels"),
    (BookingType.personal_virtual, "mobile_personal", "virtual"),
    (BookingType.genes_adult, "genes", "adult"),
    (BookingType.genes_kids, "genes", "kids"),
])
def test_all_real_combinations_accepted(booking_type, location, detail):
    """Confirms all 5 REAL offerings are accepted, not just that invalid
    ones are rejected."""
    payload = _base_payload(
        session_date=_valid_date_for(booking_type), location=location, session_detail=detail
    )
    r = BookingRequest(**payload)
    assert r.booking_type == booking_type

# Combo-mismatch and wrong-weekday rejection moved to test_bookings.py,
# tested via real HTTP requests instead of direct model construction.
# That validation now lives in the ROUTE (routes/bookings.py), not a
# Pydantic model_validator — see InvalidBookingRequestError's docstring
# in core/exceptions.py for why: the old model_validator raised a plain
# ValueError, which Pydantic/FastAPI wrapped into an array-of-objects
# response format the frontend couldn't render as text, crashing the
# booking form. Testing this at the route level is also more correct
# anyway — it's exactly where the real bug actually happened.
