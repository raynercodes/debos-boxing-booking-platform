"""
Force-expires a specific Stripe Checkout Session immediately — lets you
test the expiration/slot-release webhook path without waiting the real
30-minute minimum.

Pulls the real Stripe API key straight from Secrets Manager via
StripeService, same as the deployed app does — you never type or paste
the raw key anywhere, here or in your shell history.

Usage:
    export AWS_PROFILE=debos-boxing
    export AWS_DEFAULT_REGION=us-east-1
    export STRIPE_SECRET_PATH=/debos-boxing/dev/stripe-secret
    PYTHONPATH=. python3 scripts/force_expire_session.py cs_test_...

Get the session ID from the checkout_url a booking request returns —
it's the part starting with "cs_test_" right after "/pay/".
"""

import sys
import stripe
from src.api.core.stripe_service import get_stripe_service

if len(sys.argv) != 2:
    print("Usage: python3 scripts/force_expire_session.py <checkout_session_id>")
    sys.exit(1)

session_id = sys.argv[1]
stripe.api_key = get_stripe_service().api_key

try:
    session = stripe.checkout.Session.expire(session_id)
    print(f"Session {session_id} expired successfully. Status: {session.status}")
    print("Check CloudWatch logs for the webhook processing this event, "
          "then confirm the booking's status flipped to 'expired' and the slot is bookable again.")
except stripe.error.StripeError as exc:
    print(f"Stripe rejected the request: {exc}")
    print("Common cause: the session already completed, already expired, "
          "or the session ID was copied incorrectly.")
