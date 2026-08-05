import pytest
from pydantic import ValidationError

from src.api.models.lead import LeadRequest


def _base_payload(**overrides) -> dict:
    payload = {
        "name": "Interested Person",
        "email": "interested@example.com",
        "phone": "4045551234",
    }
    payload.update(overrides)
    return payload


def test_valid_lead_accepted():
    r = LeadRequest(**_base_payload())
    assert r.name == "Interested Person"


def test_valid_lead_with_interest_note():
    r = LeadRequest(**_base_payload(interest_note="Asked about evening kids classes"))
    assert r.interest_note == "Asked about evening kids classes"


@pytest.mark.parametrize("bad_phone", ["abc-def-ghij", "123", "1" * 20, ""])
def test_invalid_phone_rejected(bad_phone):
    with pytest.raises(ValidationError):
        LeadRequest(**_base_payload(phone=bad_phone))


@pytest.mark.parametrize("bad_name", ["", "   "])
def test_blank_name_rejected(bad_name):
    with pytest.raises(ValidationError):
        LeadRequest(**_base_payload(name=bad_name))


def test_interest_note_over_max_length_rejected():
    with pytest.raises(ValidationError):
        LeadRequest(**_base_payload(interest_note="x" * 501))


def test_interest_note_at_max_length_accepted():
    r = LeadRequest(**_base_payload(interest_note="x" * 500))
    assert len(r.interest_note) == 500
