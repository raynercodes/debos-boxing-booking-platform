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
        self._secrets_client = None
        self._api_key: Optional[str] = None
        self._webhook_secret: Optional[str] = None

    @property
    def secrets_client(self):
        if self._secrets_client is None:
            self._secrets_client = boto3.client("secretsmanager")
        return self._secrets_client

    def _fetch_secret(self, secret_id: str) -> dict:
        try:
            response = self.secrets_client.get_secret_value(SecretId=secret_id)
            return json.loads(response["SecretString"])
        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to fetch secret '%s': %s", secret_id, exc, exc_info=True)
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
        vs. our own code)."""
        stripe.api_key = self.api_key
        try:
            session = stripe.checkout.Session.create(
                mode="payment",
                payment_method_types=["card"],
                line_items=[{
                    "price_data": {
                        "currency": "usd",
                        "product_data": {"name": f"Debo's Boxing and Fitness — {booking_type_label}"},
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


_stripe_service: Optional[StripeService] = None


def get_stripe_service() -> StripeService:
    global _stripe_service
    if _stripe_service is None:
        _stripe_service = StripeService()
    return _stripe_service
