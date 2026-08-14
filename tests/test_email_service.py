"""
TEMPORARY FILE — delete entirely once AWS SES production access clears
and email_service.py is deleted.
"""

from src.api.core.email_service import get_email_service
from src.api.core.ses_service import SesService
from src.api.core.resend_service import ResendService


def test_defaults_to_ses_when_env_var_unset(monkeypatch):
    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    service = get_email_service()
    assert isinstance(service, SesService)


def test_routes_to_resend_when_explicitly_set(monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    service = get_email_service()
    assert isinstance(service, ResendService)


def test_stays_on_ses_for_any_other_value(monkeypatch):
    """Only an EXACT "resend" match activates the bridge - any other
    unexpected value falls back to the real, permanent SES path rather
    than silently breaking."""
    monkeypatch.setenv("EMAIL_PROVIDER", "something_unexpected")
    service = get_email_service()
    assert isinstance(service, SesService)
