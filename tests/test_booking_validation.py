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


@pytest.mark.parametrize("location,detail", [
    ("genes", "client_travels"),      # client_travels only valid for mobile_personal
    ("genes", "trainer_travels"),     # same
    ("mobile_personal", "adult"),     # adult only valid for genes
    ("mobile_personal", "kids"),      # kids only valid for genes
])
def test_invalid_location_detail_combinations_rejected(location, detail):
    """These are the exact combinations that DON'T exist in Debo's real
    business — confirms the cross-field validator actually rejects every
    invalid pairing, not just the one case manually spot-checked earlier."""
    payload = _base_payload(location=location, session_detail=detail)
    # session_date needs to be valid for SOME type to isolate the combo
    # check itself rather than accidentally failing on the date check first
    with pytest.raises(ValidationError):
        BookingRequest(**payload)


@pytest.mark.parametrize("booking_type,location,detail", [
    (BookingType.personal_client_travels, "mobile_personal", "client_travels"),
    (BookingType.personal_trainer_travels, "mobile_personal", "trainer_travels"),
    (BookingType.genes_adult, "genes", "adult"),
    (BookingType.genes_kids, "genes", "kids"),
])
def test_all_four_real_combinations_accepted(booking_type, location, detail):
    """The inverse of the rejection test above — confirms all 4 REAL
    offerings are accepted, not just that invalid ones are rejected."""
    payload = _base_payload(
        session_date=_valid_date_for(booking_type), location=location, session_detail=detail
    )
    r = BookingRequest(**payload)
    assert r.booking_type == booking_type


def test_wrong_weekday_for_valid_combo_rejected():
    """genes_kids is Mon-Wed only — a VALID combo on an INVALID day should
    still be rejected. This is different from the combo-rejection test
    above: here the (location, detail) pair is real, only the date is wrong."""
    today = datetime.now(timezone.utc).date()
    # find a Thu/Fri/Sat/Sun (NOT in genes_kids' Mon-Wed allowed set)
    for offset in range(7):
        candidate = today + timedelta(days=offset)
        if candidate.weekday() not in BOOKING_TYPE_RULES[BookingType.genes_kids]["allowed_weekdays"]:
            bad_date = candidate.isoformat()
            break
    with pytest.raises(ValidationError):
        BookingRequest(**_base_payload(session_date=bad_date, location="genes", session_detail="kids"))
