"""
Stripe integration.

Two responsibilities, both security-sensitive in different ways:

1. create_checkout_session — starts a hosted Stripe payment page. We never
   touch card details ourselves; Stripe's own PCI-compliant checkout page
   handles that entirely. All we send is WHAT was booked and HOW MUCH it
   costs (looked up server-side from BOOKING_TYPE_RULES, never trusted from
   the client — same principle applied to booking creation generally).

2. verify_webhook_event — the ONLY trustworthy signal that a payment
   actually succeeded. Stripe signs every webhook request with a secret
   only Stripe and we know; verifying that signature is what proves a
   webhook call genuinely came from Stripe and wasn't forged by someone who
   just POSTed a fake "payment succeeded" body at our endpoint.

MIGRATED from Secrets Manager to SSM Parameter Store (see security.py's
module docstring for the full cost/security reasoning) — values are still
stored as the same JSON string format, just fetched via a different AWS
API now.
"""

import json
import os
from typing import Optional

import boto3
import stripe
from botocore.exceptions import ClientError, BotoCoreError

from src.api.core.exceptions import ExternalServiceError
from src.api.core.logging_config import get_logger

logger = get_logger(__name__)


class StripeService:
    def __init__(self) -> None:
        self._ssm_client = None
        self._api_key: Optional[str] = None
        self._webhook_secret: Optional[str] = None

    @property
    def ssm_client(self):
        if self._ssm_client is None:
            self._ssm_client = boto3.client("ssm")
        return self._ssm_client

    def _fetch_secret(self, parameter_name: str) -> dict:
        try:
            response = self.ssm_client.get_parameter(Name=parameter_name, WithDecryption=True)
            return json.loads(response["Parameter"]["Value"])
        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to fetch parameter '%s': %s", parameter_name, exc, exc_info=True)
            raise ExternalServiceError("Unable to retrieve required configuration") from exc

    @property
    def api_key(self) -> str:
        if self._api_key is None:
            secret_path = os.environ["STRIPE_SECRET_PATH"]
            self._api_key = self._fetch_secret(secret_path)["api_key"]
        return self._api_key

    @property
    def webhook_secret(self) -> str:
        if self._webhook_secret is None:
            secret_path = os.environ["STRIPE_SECRET_PATH"]
            self._webhook_secret = self._fetch_secret(secret_path)["webhook_secret"]
        return self._webhook_secret

    def create_checkout_session(
        self,
        booking_id: str,
        price_usd: int,
        booking_type_label: str,
        session_date: str,
        session_time: str,
        customer_email: str,
        success_url: str,
        cancel_url: str,
        expires_in_minutes: int,
    ) -> stripe.checkout.Session:
        """One-time payment only — Checkout Session in 'payment' mode, NOT
        'subscription' mode. Memberships were confirmed not a thing, so
        there's no reason to reach for Stripe's recurring-billing machinery
        at all here.

        booking_id goes into BOTH client_reference_id and metadata — Stripe
        surfaces client_reference_id prominently in its own dashboard (handy
        for Debo manually cross-referencing a payment), while metadata is
        what our OWN webhook handler actually reads programmatically to
        know which booking this payment belongs to. Redundant on purpose,
        for two different audiences (a human looking at Stripe's dashboard
        vs. our own code).

        session_date/session_time go into the line item's `description` —
        without this, the Stripe checkout page just shows a generic label
        like "Genes Adult" with no indication of WHICH date/time the client
        is actually paying for. A client should see clear confirmation of
        what they picked before handing over a card number."""
        stripe.api_key = self.api_key
        try:
            session = stripe.checkout.Session.create(
                mode="payment",
                payment_method_types=["card"],
                line_items=[{
                    "price_data": {
                        "currency": "usd",
                        "product_data": {
                            "name": f"Debo's Boxing and Fitness — {booking_type_label}",
                            "description": f"Once you confirm your booking, you'll be scheduled for a session on {session_date} at {session_time} — please arrive on time and ready to train!",
                        },
                        "unit_amount": price_usd * 100,  # Stripe expects cents, not dollars
                    },
                    "quantity": 1,
                }],
                customer_email=customer_email,
                client_reference_id=booking_id,
                metadata={"booking_id": booking_id},
                success_url=success_url,
                cancel_url=cancel_url,
                expires_at=self._expiry_timestamp(expires_in_minutes),
            )
            return session
        except stripe.error.StripeError as exc:
            logger.error("Stripe checkout session creation failed for booking %s: %s",
                         booking_id, exc, exc_info=True)
            raise ExternalServiceError("Unable to start payment — please try again") from exc

    @staticmethod
    def _expiry_timestamp(minutes: int) -> int:
        import time
        return int(time.time()) + (minutes * 60)

    def verify_webhook_event(self, payload: bytes, signature_header: str) -> stripe.Event:
        """Raises ExternalServiceError-adjacent... actually raises a plain
        ValueError/SignatureVerificationError on failure, deliberately NOT
        wrapped in our own exception types — the webhook ROUTE is
        responsible for translating a failed verification into a 400
        response (Stripe expects a 4xx on bad signature, not a 401/403),
        which is a different translation than every other auth failure in
        this project. Keeping that translation at the route layer, not
        here, matches the same separation-of-concerns already used
        everywhere else — this service raises what actually happened,
        the route decides the HTTP response."""
        return stripe.Webhook.construct_event(payload, signature_header, self.webhook_secret)

    def expire_checkout_session(self, session_id: str) -> None:
        """Forces an in-progress checkout session to expire immediately,
        rather than waiting out the full 30-minute natural expiry. Used
        when someone EXPLICITLY cancels (clicks Stripe's own cancel link)
        — we already know they're not paying, so there's no reason to
        keep their slot claim reserved for 30 more minutes on the off
        chance they change their mind again. This triggers the SAME real
        checkout.session.expired webhook as natural expiration does,
        reusing all existing release-the-slot logic rather than
        duplicating it.

        Deliberately swallows StripeError — if the session already
        completed, already expired naturally, or the ID is stale, that's
        not a real problem worth failing the cancel-page request over;
        the booking is either already resolved or will resolve itself
        shortly regardless."""
        stripe.api_key = self.api_key
        try:
            stripe.checkout.Session.expire(session_id)
        except stripe.error.StripeError as exc:
            logger.warning("Could not expire checkout session %s (likely already resolved): %s", session_id, exc)

    def get_receipt_url(self, session_id: str) -> Optional[str]:
        """Stripe already generates a clean, professional-looking hosted
        receipt page for every real charge — no reason to build our own
        receipt formatting from scratch when Stripe's is better and free.
        This same URL stays useful after a refund too: Stripe's hosted
        receipt page is dynamic, so viewing it after a refund shows
        "Refunded" directly on the same page — meaning this one fetch,
        done once at confirmation time and stored, covers both the
        confirmation AND cancellation emails without needing a second,
        separate "refund receipt" concept at all.

        Returns None on any failure — a missing receipt link is a
        cosmetic gap in the email, never worth failing the actual booking
        confirmation over."""
        stripe.api_key = self.api_key
        try:
            session = stripe.checkout.Session.retrieve(session_id, expand=["payment_intent.latest_charge"])
            charge = session.payment_intent.latest_charge
            return charge.receipt_url if charge else None
        except (stripe.error.StripeError, AttributeError) as exc:
            logger.warning("Could not fetch receipt URL for session %s: %s", session_id, exc)
            return None

    def refund_full_payment(self, session_id: str) -> Optional[dict]:
        """Issues a FULL refund against the real charge tied to this
        checkout session — deliberately full-only, not partial, keeping
        this scoped to exactly what's needed rather than building
        partial-refund logic nobody asked for.

        Safe to call from the cancel endpoint specifically because that
        endpoint's own cancel() is already atomic and only ever succeeds
        ONCE per booking (see BookingRepository.cancel's docstring) — so
        this can never be triggered twice for the same booking, no
        separate double-refund tracking needed here.

        Returns None on failure rather than raising — a failed refund
        must never block the cancellation itself from completing. Debo's
        intent to cancel is a separate concern from whether the refund
        technically succeeded; a failure here gets logged clearly so it's
        visible for manual follow-up, but the booking still gets
        cancelled regardless."""
        stripe.api_key = self.api_key
        try:
            session = stripe.checkout.Session.retrieve(session_id)
            payment_intent_id = session.payment_intent
            if not payment_intent_id:
                logger.warning("No payment_intent on session %s — nothing to refund", session_id)
                return None
            refund = stripe.Refund.create(payment_intent=payment_intent_id)
            logger.info("Refund %s issued for session %s: $%s", refund.id, session_id, refund.amount / 100)
            return {"refund_id": refund.id, "amount_usd": refund.amount / 100, "status": refund.status}
        except stripe.error.StripeError as exc:
            logger.error("Refund FAILED for session %s — needs manual follow-up: %s", session_id, exc, exc_info=True)
            return None


_stripe_service: Optional[StripeService] = None


def get_stripe_service() -> StripeService:
    global _stripe_service
    if _stripe_service is None:
        _stripe_service = StripeService()
    return _stripe_service
