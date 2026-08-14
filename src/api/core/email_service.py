"""
TEMPORARY FILE — delete entirely once AWS SES production access clears.

Thin dispatcher, nothing more — decides whether to hand back the real
SesService or the temporary ResendService based on the EMAIL_PROVIDER
env var. This is the ONLY new file webhooks.py, bookings.py, lockout.py,
and reminder_handler.py need to import instead of importing
get_ses_service directly — that one-line import swap in each is the
sole change made to those four permanent files. Cleanup once SES
clears: delete this file, delete resend_service.py, revert those four
import lines back to `from src.api.core.ses_service import
get_ses_service` directly.
"""

import os

from src.api.core.ses_service import get_ses_service


def get_email_service():
    """Returns whichever email service is currently active. Callers
    never need to know or care which — both expose the exact same
    public methods (send_booking_confirmation, send_checkout_link, etc.)."""
    if os.environ.get("EMAIL_PROVIDER") == "resend":
        from src.api.core.resend_service import get_resend_service
        return get_resend_service()
    return get_ses_service()
