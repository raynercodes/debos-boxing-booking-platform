"""
Stripe webhook handler.

This is the ONE place a booking is ever marked "confirmed" — never on
initial creation, never based on a client-side redirect. See the module-
level docstring in core/stripe_service.py and the design notes in
routes/bookings.py's create_booking for the full reasoning.

Deliberately NOT behind require_admin — Stripe itself is calling this, not
an admin with a JWT. The authentication mechanism here is entirely
different: signature verification proves the request genuinely came from
Stripe, which is a stronger and more specific guarantee than "someone has a
valid admin token" would even be for this purpose.
"""

import os
from fastapi import APIRouter, Request

from src.api.core.stripe_service import get_stripe_service
from src.api.core.database import get_db_service
from src.api.core.bookings_repository import BookingRepository
from src.api.core.ses_service import get_ses_service
from src.api.models.booking import BookingStatus, requires_slot_claim, BookingType
from src.api.core.logging_config import get_logger
from src.api.core.exceptions import WebhookSignatureError

logger = get_logger(__name__)
router = APIRouter()


@router.post(
    "/stripe",
    summary="Stripe Webhook",
    description="Receives payment confirmation events from Stripe. Not for "
                "direct use — called by Stripe's servers only.",
)
async def stripe_webhook(request: Request):
    payload = await request.body()
    signature_header = request.headers.get("stripe-signature", "")

    try:
        event = get_stripe_service().verify_webhook_event(payload, signature_header)
    except Exception as exc:
        # Broad except is intentional here — stripe's SDK can raise several
        # different exception types for a bad signature/malformed payload
        # (SignatureVerificationError, ValueError), and ALL of them mean
        # the same thing to us: reject it, don't process an unverified
        # payload. Logged at WARNING since a bad signature could be a
        # misconfiguration (wrong webhook secret) OR someone probing the
        # endpoint — worth being able to spot a pattern of these.
        logger.warning("Rejected webhook with invalid signature: %s", exc)
        raise WebhookSignatureError("Invalid signature") from exc

    repo = BookingRepository(get_db_service())

    if event["type"] == "checkout.session.completed":
        booking_id = event["data"]["object"]["metadata"].get("booking_id")
        if not booking_id:
            logger.error("checkout.session.completed event missing booking_id in metadata")
            return {"status": "ignored", "reason": "missing booking_id"}

        booking = repo.get_by_id(booking_id)
        if booking is None:
            logger.error("Webhook confirmed payment for unknown booking_id: %s", booking_id)
            return {"status": "ignored", "reason": "booking not found"}

        repo.set_status(booking_id, BookingStatus.confirmed.value)

        if requires_slot_claim(BookingType(booking["booking_type"])):
            repo.confirm_slot(booking["session_date"], booking["session_time"])

        # This is the ONLY point a booking is actually verified-paid, so
        # it's the correct place for confirmation emails to originate from.
        # booking dict here is the PRE-update snapshot (status still says
        # "processing" in memory) — fine, since neither email body
        # references the status field, only name/date/time/price.
        ses = get_ses_service()
        ses.send_booking_confirmation(booking)
        ses.send_new_booking_notification(booking, admin_email=os.environ["ADMIN_EMAIL"])

        logger.info("Booking %s confirmed via Stripe webhook", booking_id)

    elif event["type"] == "checkout.session.expired":
        booking_id = event["data"]["object"]["metadata"].get("booking_id")
        if not booking_id:
            return {"status": "ignored", "reason": "missing booking_id"}

        booking = repo.get_by_id(booking_id)
        if booking is None:
            return {"status": "ignored", "reason": "booking not found"}

        repo.set_status(booking_id, BookingStatus.expired.value)

        if requires_slot_claim(BookingType(booking["booking_type"])):
            repo.release_slot(booking["session_date"], booking["session_time"])

        logger.info("Booking %s expired (unpaid checkout) — slot released", booking_id)

    else:
        # Stripe sends many event types we don't act on (e.g. charge.refunded
        # isn't handled yet, payment_intent.created, etc.) — acknowledging
        # with 200 and no action is correct; returning an error for events
        # we simply don't care about would make Stripe retry them forever.
        logger.info("Received unhandled Stripe event type: %s", event["type"])

    # Stripe expects a fast 2xx acknowledgment — this return IS that ack.
    return {"status": "received"}
