#!/usr/bin/env bash
set -e

# Run this FROM INSIDE your existing debos-boxing-booking-platform folder.
#
# Wires in real email sending: booking confirmation + admin notification
# (webhook), cancellation notices both directions (cancel route), and the
# real reminder job logic (replacing the stub). No infra/IAM changes
# needed - the Lambda execution role already had ses:SendEmail from the
# original template.yaml.

echo "Wiring in SES email sending..."

cat > src/api/core/ses_service.py << 'FILEEOF'
"""
SES email service.

All sends go through the verified DOMAIN identity (debosboxingandfitness.com),
not a single verified email address — domain-level verification (the DKIM
setup) allows sending from ANY address @ that domain without separately
verifying each individual from-address.

Deliberate design choice: a failed email send is logged but NEVER re-raised
as a hard error up to the caller. The booking/cancellation itself already
succeeded by the time any of these methods run — a bounced or failed
notification email is a real problem worth knowing about (hence the
logging), but it should never roll back or fail an action that already
completed. The booking record in DynamoDB is the source of truth; email is
a courtesy layered on top of it, not a dependency of it.
"""

import boto3
from botocore.exceptions import ClientError, BotoCoreError

from src.api.core.logging_config import get_logger

logger = get_logger(__name__)

FROM_ADDRESS = "bookings@debosboxingandfitness.com"


class SesService:
    def __init__(self) -> None:
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = boto3.client("ses")
        return self._client

    def _send(self, to_address: str, subject: str, body_text: str) -> None:
        try:
            self.client.send_email(
                Source=FROM_ADDRESS,
                Destination={"ToAddresses": [to_address]},
                Message={
                    "Subject": {"Data": subject},
                    "Body": {"Text": {"Data": body_text}},
                },
            )
            logger.info("Email sent to %s: %s", to_address, subject)
        except (ClientError, BotoCoreError) as exc:
            # Swallowed deliberately — see module docstring for why.
            logger.error("Failed to send email to %s (%s): %s", to_address, subject, exc, exc_info=True)

    def send_booking_confirmation(self, booking: dict) -> None:
        subject = "Your session with Debo's Boxing and Fitness is confirmed!"
        body = (
            f"Hi {booking['name']},\n\n"
            f"Your session is confirmed for {booking['session_date']} at {booking['session_time']}.\n\n"
            f"See you then — please arrive on time and ready to train!\n\n"
            f"Debo's Boxing and Fitness"
        )
        self._send(booking["email"], subject, body)

    def send_new_booking_notification(self, booking: dict, admin_email: str) -> None:
        subject = f"New booking: {booking['name']} on {booking['session_date']}"
        body = (
            f"New confirmed booking:\n\n"
            f"Client: {booking['name']}\n"
            f"Phone: {booking['phone']}\n"
            f"Email: {booking['email']}\n"
            f"Type: {booking['booking_type']}\n"
            f"Date: {booking['session_date']} at {booking['session_time']}\n"
            f"Price: ${booking['price_usd']}\n"
        )
        self._send(admin_email, subject, body)

    def send_cancellation_notice_to_client(self, booking: dict, reason: str) -> None:
        subject = "Your session has been cancelled"
        body = (
            f"Hi {booking['name']},\n\n"
            f"Your session on {booking['session_date']} at {booking['session_time']} has been cancelled.\n\n"
            f"Reason: {reason}\n\n"
            f"Debo's Boxing and Fitness"
        )
        self._send(booking["email"], subject, body)

    def send_cancellation_notice_to_admin(self, booking: dict, reason: str, admin_email: str) -> None:
        subject = f"Booking cancelled: {booking['name']} on {booking['session_date']}"
        body = (
            f"You cancelled the following booking:\n\n"
            f"Client: {booking['name']}\n"
            f"Date: {booking['session_date']} at {booking['session_time']}\n"
            f"Reason given: {reason}\n"
        )
        self._send(admin_email, subject, body)

    def send_reminder(self, booking: dict) -> None:
        subject = "Reminder: your session with Debo's Boxing and Fitness is tomorrow"
        body = (
            f"Hi {booking['name']},\n\n"
            f"Just a reminder — your session is tomorrow, {booking['session_date']} at {booking['session_time']}.\n\n"
            f"See you then!\n\n"
            f"Debo's Boxing and Fitness"
        )
        self._send(booking["email"], subject, body)


_ses_service = None


def get_ses_service() -> SesService:
    global _ses_service
    if _ses_service is None:
        _ses_service = SesService()
    return _ses_service
FILEEOF

cat > src/api/core/bookings_repository.py << 'FILEEOF'
"""
Booking data access.

Same pattern as LockoutManager: this class owns ALL raw boto3 calls for
bookings, injected with a DynamoDBService rather than reaching for a global.
Routes talk to this class, never to boto3 directly — that separation means
the actual HTTP/routing code in routes/bookings.py stays focused on request
shape and response codes, while every DynamoDB-specific detail (conditional
writes, GSI query mechanics, error wrapping) lives in exactly one place.
"""

from typing import List, Optional

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError, BotoCoreError

from src.api.core.database import DynamoDBService
from src.api.core.exceptions import ExternalServiceError
from src.api.core.logging_config import get_logger

logger = get_logger(__name__)


class BookingRepository:
    def __init__(self, db_service: DynamoDBService) -> None:
        self._db = db_service

    def create(self, item: dict) -> dict:
        """Conditional write — attribute_not_exists(booking_id) guards
        against overwriting an existing item. With a fresh UUID4 per
        booking, a real collision is astronomically unlikely, but the
        conditional costs nothing and matches the same defensive pattern
        used everywhere else (never assume a generated ID is definitely
        unique just because the odds are low)."""
        try:
            self._db.bookings_table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(booking_id)",
            )
            return item
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # Only reachable on a genuine UUID collision — re-raising as
                # ExternalServiceError here rather than silently retrying,
                # since retrying with the SAME id would just fail again;
                # the caller needs to generate a new id and retry the whole
                # operation, not just this write.
                logger.error("Booking ID collision on create (extremely rare): %s", item.get("booking_id"))
                raise ExternalServiceError("Unable to create booking, please try again") from exc
            logger.error("Failed to write booking: %s", exc, exc_info=True)
            raise ExternalServiceError("Unable to save booking") from exc
        except BotoCoreError as exc:
            logger.error("Failed to write booking: %s", exc, exc_info=True)
            raise ExternalServiceError("Unable to save booking") from exc

    def get_by_id(self, booking_id: str) -> Optional[dict]:
        try:
            result = self._db.bookings_table.get_item(Key={"booking_id": booking_id})
            return result.get("Item")
        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to read booking %s: %s", booking_id, exc, exc_info=True)
            raise ExternalServiceError("Unable to retrieve booking") from exc

    def set_status(self, booking_id: str, new_status: str) -> None:
        """Unconditional status set — used ONLY by the Stripe webhook
        handler to transition processing -> confirmed/expired. Deliberately
        no ConditionExpression restricting the prior status, unlike
        cancel() above: the webhook is the trusted, verified source of
        truth for payment outcomes, not a user-facing action that needs the
        same "don't let this happen twice" guard admin cancellation does."""
        try:
            self._db.bookings_table.update_item(
                Key={"booking_id": booking_id},
                UpdateExpression="SET #s = :new_status",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":new_status": new_status},
            )
        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to set booking %s status to %s: %s", booking_id, new_status, exc, exc_info=True)
            raise ExternalServiceError("Unable to update booking status") from exc

    def mark_reminder_sent(self, booking_id: str) -> None:
        """Used by the daily reminder job — prevents a re-run on the same
        day (EventBridge scheduled rules can occasionally double-fire) from
        sending a second reminder for the same booking."""
        try:
            self._db.bookings_table.update_item(
                Key={"booking_id": booking_id},
                UpdateExpression="SET reminder_sent = :true_val",
                ExpressionAttributeValues={":true_val": True},
            )
        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to mark reminder sent for booking %s: %s", booking_id, exc, exc_info=True)
            raise ExternalServiceError("Unable to update reminder status") from exc

    def query_by_date(self, session_date: str) -> List[dict]:
        """One Query per exact date against the session-date-index GSI.
        Deliberately NOT a Scan — the admin week-view endpoint calls this
        once per date in the target range (up to 7 times), which is still
        far cheaper and more scalable than scanning the whole table, same
        "no table scans" instinct applied on fintech."""
        try:
            result = self._db.bookings_table.query(
                IndexName="session-date-index",
                KeyConditionExpression=Key("session_date").eq(session_date),
            )
            return result.get("Items", [])
        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to query bookings for %s: %s", session_date, exc, exc_info=True)
            raise ExternalServiceError("Unable to retrieve bookings") from exc

    def cancel(self, booking_id: str, reason: str) -> dict:
        """Atomically cancels a booking, but ONLY if it exists AND isn't
        already cancelled — both checks happen in ONE ConditionExpression,
        not as a separate get-then-update pair of calls. That matters for
        a real reason, not just tidiness: two separate calls have a race
        window (e.g. an admin double-clicking Cancel, or two browser tabs)
        where both checks could pass before either write happens, letting
        a second cancellation slip through and — once cancellation emails
        are wired in — send a duplicate cancellation notice to both Debo
        and the client. A single atomic conditional write closes that
        window entirely.

        DynamoDB doesn't report WHICH half of a compound condition failed,
        only that the condition failed overall — so on failure, a follow-up
        get_item disambiguates "doesn't exist" from "already cancelled"
        purely to return a precise error message. The write path itself
        (the common, successful case) is still a single atomic call.

        `reason` is always a real string by the time it reaches here — the
        route layer already substitutes DEFAULT_CANCELLATION_REASON if the
        admin didn't type one, so this method never has to think about
        None/blank handling itself."""
        try:
            result = self._db.bookings_table.update_item(
                Key={"booking_id": booking_id},
                UpdateExpression="SET #s = :new_status, cancellation_reason = :reason",
                ConditionExpression="attribute_exists(booking_id) AND #s <> :cancelled_status",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":new_status": "cancelled",
                    ":cancelled_status": "cancelled",
                    ":reason": reason,
                },
                ReturnValues="ALL_NEW",
            )
            return {"outcome": "cancelled", "item": result["Attributes"]}
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                existing = self.get_by_id(booking_id)
                if existing is None:
                    return {"outcome": "not_found"}
                return {"outcome": "already_cancelled", "item": existing}
            logger.error("Failed to cancel booking %s: %s", booking_id, exc, exc_info=True)
            raise ExternalServiceError("Unable to cancel booking") from exc
        except BotoCoreError as exc:
            logger.error("Failed to cancel booking %s: %s", booking_id, exc, exc_info=True)
            raise ExternalServiceError("Unable to cancel booking") from exc

    # ---------------------------------------------------------------
    # Slot claiming — Personal training exclusivity
    #
    # A "slot claim" is a SEPARATE item in the SAME bookings table, keyed
    # by a deterministic id derived from date+time rather than a random
    # UUID: "personal-slot#{session_date}#{session_time}". Reusing the same
    # table (rather than a whole new one) keeps this simple — DynamoDB is
    # schemaless, so a differently-shaped item living alongside real
    # bookings costs nothing extra in infrastructure.
    #
    # The claim's own "booking_id" being that deterministic string is what
    # makes the exclusivity atomic: attribute_not_exists(booking_id) can
    # only succeed ONCE for a given date+time, no matter how many requests
    # race to claim it simultaneously. Whoever's conditional write wins,
    # wins — there's no window where two requests could both believe they
    # successfully claimed the same slot.
    # ---------------------------------------------------------------

    @staticmethod
    def _slot_claim_key(session_date: str, session_time: str) -> str:
        return f"personal-slot#{session_date}#{session_time}"

    def claim_personal_slot(self, session_date: str, session_time: str, real_booking_id: str) -> dict:
        """Attempts to atomically claim a Personal-training date+time slot.
        Returns {"outcome": "claimed"} on success, or
        {"outcome": "already_processing" | "already_confirmed"} if someone
        else already holds it — the caller uses this to pick between the
        two different messages: "try again shortly" vs "pick another time."""
        claim_key = self._slot_claim_key(session_date, session_time)
        try:
            self._db.bookings_table.put_item(
                Item={
                    "booking_id": claim_key,
                    "real_booking_id": real_booking_id,
                    "status": "processing",
                },
                ConditionExpression="attribute_not_exists(booking_id)",
            )
            return {"outcome": "claimed"}
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                existing = self.get_by_id(claim_key)
                # existing should always be present here (the condition only
                # fails if something's already there) — defensive fallback
                # to "already_confirmed" (the safer, more restrictive
                # outcome) if it's somehow missing by the time we re-read it.
                current_status = existing["status"] if existing else "confirmed"
                outcome = "already_processing" if current_status == "processing" else "already_confirmed"
                return {"outcome": outcome}
            logger.error("Failed to claim slot %s: %s", claim_key, exc, exc_info=True)
            raise ExternalServiceError("Unable to check slot availability") from exc
        except BotoCoreError as exc:
            logger.error("Failed to claim slot %s: %s", claim_key, exc, exc_info=True)
            raise ExternalServiceError("Unable to check slot availability") from exc

    def confirm_slot(self, session_date: str, session_time: str) -> None:
        """Called from the webhook once payment succeeds — flips the claim
        from 'processing' to 'confirmed', so it now permanently blocks that
        date+time until an admin cancellation releases it."""
        claim_key = self._slot_claim_key(session_date, session_time)
        try:
            self._db.bookings_table.update_item(
                Key={"booking_id": claim_key},
                UpdateExpression="SET #s = :confirmed",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":confirmed": "confirmed"},
            )
        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to confirm slot claim %s: %s", claim_key, exc, exc_info=True)
            raise ExternalServiceError("Unable to finalize slot") from exc

    def release_slot(self, session_date: str, session_time: str) -> None:
        """Deletes the claim entirely — used on checkout expiry AND on
        admin cancellation of a confirmed Personal booking. delete_item on
        a key that doesn't exist is a harmless no-op in DynamoDB, so this
        is safe to call even if the claim was somehow already gone."""
        claim_key = self._slot_claim_key(session_date, session_time)
        try:
            self._db.bookings_table.delete_item(Key={"booking_id": claim_key})
        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to release slot claim %s: %s", claim_key, exc, exc_info=True)
            raise ExternalServiceError("Unable to release slot") from exc
FILEEOF

cat > src/api/routes/webhooks.py << 'FILEEOF'
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
FILEEOF

cat > src/api/routes/bookings.py << 'FILEEOF'
import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from fastapi import APIRouter, Depends, Query
from fastapi.security import HTTPBearer
from pydantic import BaseModel

from src.api.models.booking import (
    BookingRequest, BookingResponse, BookingCheckoutResponse, BookingStatus,
    BOOKING_TYPE_RULES, CHECKOUT_SESSION_EXPIRY_MINUTES, requires_slot_claim,
)
from src.api.core.security import get_security_service
from src.api.core.database import get_db_service
from src.api.core.bookings_repository import BookingRepository
from src.api.core.stripe_service import get_stripe_service
from src.api.core.ses_service import get_ses_service
from src.api.core.exceptions import (
    AppError, BookingNotFoundError, BookingAlreadyCancelledError,
    SlotProcessingError, SlotTakenError,
)
from src.api.core.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter()
bearer_scheme = HTTPBearer()

# Weekends deliberately excluded — no BookingType's allowed_weekdays ever
# includes Saturday/Sunday (Personal is Mon-Fri, the widest of the three),
# so a day_of_week filter value of "saturday" would always return an empty
# result. This isn't the actual defense against weekend bookings — that
# lives in booking.py's schedule validator, checked against real weekday
# integers independent of this list. This is just removing dead, unused
# input surface from an admin-only query param.
VALID_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]

# How long a booking stays visible in the admin list after its start time has
# passed. Deliberately a DISPLAY filter applied at query time, NOT a deletion
# policy — the record stays in DynamoDB permanently (useful for history,
# case-study metrics, and the gym owner's own records). Same TTL-explicit-
# evaluation lesson from fintech: never rely on a timestamp-based mechanism
# to physically remove something on a tight schedule — DynamoDB TTL deletion
# can lag up to 48 hours, useless for a "gone within an hour" rule. Explicit
# evaluation in application code, every time, is the only way to guarantee
# this window actually holds.
HISTORY_VISIBILITY_WINDOW = timedelta(hours=1)

# Shown on the booking record (and eventually in the cancellation email to
# both Debo and the client) whenever the admin cancels without typing a
# reason. Kept as a named constant rather than an inline string so there's
# exactly one place to change the wording later.
DEFAULT_CANCELLATION_REASON = "No reason was mentioned by Debo — contact him for more information."


class CancelBookingRequest(BaseModel):
    """Optional request body for the cancel endpoint — admin can type a
    reason, or send nothing at all and DEFAULT_CANCELLATION_REASON gets
    used instead. Kept optional (not required) since forcing a reason on
    every cancellation would just encourage typing throwaway text to get
    past a required field, which defeats the point of collecting it."""
    reason: Optional[str] = None


def require_admin(credentials=Depends(bearer_scheme)):
    """Delegates to SecurityService, which raises InvalidTokenError on
    failure — translated to a 401 by the global exception handler in
    main.py, so this stays a one-line dependency."""
    get_security_service().require_admin_token(credentials.credentials)


def _get_repository() -> BookingRepository:
    """Small helper so every route constructs the repository the same way,
    with the singleton DynamoDBService injected — matches the same pattern
    already used for LockoutManager in routes/auth.py."""
    return BookingRepository(get_db_service())


def _item_to_response(item: dict) -> BookingResponse:
    """DynamoDB returns numbers as Decimal, not int/float — BookingResponse
    expects a plain int for price_usd, so this conversion has to happen
    explicitly every time an item comes back out of the table."""
    return BookingResponse(
        booking_id=item["booking_id"],
        name=item["name"],
        email=item["email"],
        phone=item["phone"],
        session_date=item["session_date"],
        session_time=item["session_time"],
        booking_type=item["booking_type"],
        price_usd=int(item["price_usd"]),
        status=item["status"],
        created_at=item["created_at"],
        reminder_sent=item["reminder_sent"],
        cancellation_reason=item.get("cancellation_reason"),  # only present once cancelled
    )


def _is_past_visibility_window(item: dict) -> bool:
    """Explicit, computed-at-read-time check — see HISTORY_VISIBILITY_WINDOW
    comment above for why this is a display filter, not a stored flag."""
    session_start = datetime.strptime(
        f"{item['session_date']}T{item['session_time']}", "%Y-%m-%dT%H:%M"
    ).replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - session_start > HISTORY_VISIBILITY_WINDOW


@router.post(
    "",
    response_model=BookingCheckoutResponse,
    status_code=201,
    summary="Create Booking (starts Stripe checkout)",
    description="Submit a new session booking. Returns a Stripe checkout URL — "
                "the booking is NOT confirmed until payment succeeds via webhook.",
)
async def create_booking(request: BookingRequest):
    # Price is ALWAYS looked up server-side from BOOKING_TYPE_RULES, never
    # accepted as a value from the client. If the client could send its own
    # price, anyone could book a $100 Gene's adult session and submit
    # price_usd=1 — the server is the only source of truth for what
    # something costs, the request only says WHAT was booked, never
    # WHAT IT COSTS.
    booking_type = request.booking_type
    price_usd = BOOKING_TYPE_RULES[booking_type]["price_usd"]
    repo = _get_repository()
    booking_id = str(uuid.uuid4())

    # Slot claiming ONLY applies to Personal training (any of the 3
    # delivery methods) — Debo can only train one person at a given
    # date+time regardless of format. Gene's classes are group settings
    # and skip this entirely; multiple people can book the same class time.
    if requires_slot_claim(booking_type):
        claim_result = repo.claim_personal_slot(request.session_date, request.session_time, booking_id)
        if claim_result["outcome"] == "already_processing":
            raise SlotProcessingError(
                "This Session is currently being booked at this time. It might be available soon — check back shortly."
            )
        if claim_result["outcome"] == "already_confirmed":
            raise SlotTakenError(
                "Sorry, this booking is taken. Try another available day or time."
            )

    item = {
        "booking_id": booking_id,
        "name": request.name,
        "email": request.email,
        "phone": request.phone,
        "session_date": request.session_date,
        "session_time": request.session_time,
        "booking_type": booking_type.value,
        # location/session_detail stored alongside the derived booking_type
        # even though BookingResponse doesn't expose them yet — cheap to
        # store on a schemaless DynamoDB item, and useful for future
        # reporting (e.g. "all genes bookings" or "all mobile bookings")
        # without needing to parse booking_type strings.
        "location": request.location.value,
        "session_detail": request.session_detail.value,
        "price_usd": price_usd,
        "status": BookingStatus.processing.value,  # NEVER confirmed here — only the webhook confirms
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reminder_sent": False,
    }

    try:
        repo.create(item)
    except Exception:
        # If the booking write fails after a slot claim succeeded, release
        # the claim — otherwise a failed write would leave a phantom claim
        # blocking the slot forever with no booking behind it.
        if requires_slot_claim(booking_type):
            repo.release_slot(request.session_date, request.session_time)
        raise

    # Framer URLs are placeholders until the frontend exists — TODO once
    # Framer is wired in, point these at the real confirmation/cancelled
    # pages instead of this API's own domain.
    base_url = os.environ.get("FRONTEND_BASE_URL", "https://debosboxingandfitness.com")
    stripe_service = get_stripe_service()
    try:
        session = stripe_service.create_checkout_session(
            booking_id=booking_id,
            price_usd=price_usd,
            booking_type_label=booking_type.value.replace("_", " ").title(),
            session_date=request.session_date,
            session_time=request.session_time,
            customer_email=request.email,
            success_url=f"{base_url}/booking-confirmed?booking_id={booking_id}",
            cancel_url=f"{base_url}/booking-cancelled?booking_id={booking_id}",
            expires_in_minutes=CHECKOUT_SESSION_EXPIRY_MINUTES,
        )
    except Exception:
        # Same reasoning as above — if Stripe itself fails, don't leave a
        # phantom claim/booking behind with no way to ever pay for it.
        if requires_slot_claim(booking_type):
            repo.release_slot(request.session_date, request.session_time)
        raise

    logger.info(
        "Checkout started: %s %s (%s, $%s) booking_id=%s",
        request.session_date, request.session_time, booking_type.value, price_usd, booking_id,
    )
    return BookingCheckoutResponse(
        booking_id=booking_id,
        status=BookingStatus.processing,
        price_usd=price_usd,
        checkout_url=session.url,
    )


@router.get(
    "",
    summary="List Upcoming Bookings (Admin)",
    description="Admin-only. Filters: day_of_week, search (name/phone). "
                "Auto-excludes bookings more than 1hr past their start time.",
    dependencies=[Depends(require_admin)],
)
async def list_bookings(
    day_of_week: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
) -> List[BookingResponse]:
    if day_of_week and day_of_week.lower() not in VALID_DAYS:
        raise AppError(f"day_of_week must be one of {VALID_DAYS}")

    repo = _get_repository()
    today = datetime.now(timezone.utc).date()
    week_dates = [today + timedelta(days=i) for i in range(7)]

    # If a specific day_of_week was given, only query the ONE date in the
    # current week window that falls on that weekday — no reason to issue
    # 7 GSI queries and throw away 6 of them when we can compute which
    # single date we actually need.
    if day_of_week:
        target_weekday = VALID_DAYS.index(day_of_week.lower())
        week_dates = [d for d in week_dates if d.weekday() == target_weekday]

    all_items: List[dict] = []
    for date in week_dates:
        all_items.extend(repo.query_by_date(date.isoformat()))

    # Slot-claim records share the bookings table but aren't real bookings —
    # filter them out before anything else touches this list. They're
    # identifiable by their synthetic "personal-slot#..." booking_id prefix.
    all_items = [item for item in all_items if not item["booking_id"].startswith("personal-slot#")]

    # History filter — computed fresh on every call, never trusted from a
    # stored flag (see HISTORY_VISIBILITY_WINDOW comment above).
    visible_items = [item for item in all_items if not _is_past_visibility_window(item)]

    # Search filter — case-insensitive substring match on name or phone.
    # Done in Python, not a DynamoDB filter expression — not worth a GSI
    # for this at gym-scale data volume (dozens of bookings, not thousands).
    if search:
        search_lower = search.lower()
        visible_items = [
            item for item in visible_items
            if search_lower in item["name"].lower() or search_lower in item["phone"]
        ]

    # Sort ascending by (date, time) — since week_dates starts at today,
    # this naturally puts the soonest upcoming session first with no
    # extra logic needed.
    visible_items.sort(key=lambda item: (item["session_date"], item["session_time"]))

    return [_item_to_response(item) for item in visible_items]


@router.get(
    "/{booking_id}",
    response_model=BookingResponse,
    summary="Get Booking by ID",
)
async def get_booking(booking_id: str):
    item = _get_repository().get_by_id(booking_id)
    if item is None:
        raise BookingNotFoundError(f"Booking {booking_id} not found")
    return _item_to_response(item)


@router.patch(
    "/{booking_id}/cancel",
    response_model=BookingResponse,
    summary="Cancel Booking (Admin)",
    description="Admin-only. Cancels a booking at any time regardless of how "
                "close it is to the session start.",
    dependencies=[Depends(require_admin)],
)
async def cancel_booking(booking_id: str, request: Optional[CancelBookingRequest] = None):
    # Blank/whitespace-only reason treated the same as "no reason given" —
    # an admin submitting an empty string shouldn't produce a blank-looking
    # record; it should fall through to the same default as sending nothing.
    reason = (request.reason.strip() if request and request.reason else "") or DEFAULT_CANCELLATION_REASON

    # Atomic cancel — see BookingRepository.cancel docstring for why this is
    # ONE conditional write rather than a separate check-then-update pair.
    result = _get_repository().cancel(booking_id, reason)

    if result["outcome"] == "not_found":
        raise BookingNotFoundError(f"Booking {booking_id} not found")

    if result["outcome"] == "already_cancelled":
        # No emails fire here — this is a rejected no-op, not a state change.
        raise BookingAlreadyCancelledError(
            f"Booking {booking_id} is already cancelled for your client: {result['item']['name']}"
        )

    item = result["item"]

    # DELIBERATE BUSINESS DECISION — the slot claim is NOT released on
    # cancellation. If Debo manually cancels a Personal session, that's
    # almost always for a real reason (unavailable, emergency, etc.), and
    # the exact date+time shouldn't be instantly re-bookable by a stranger
    # without him actively re-opening it. This only affects the ONE
    # specific calendar date+time that was cancelled — slot claims are keyed
    # by exact (date, time), not a recurring weekly pattern, so cancelling
    # Aug 10 at 10am has zero effect on future weeks' Mondays at 10am.
    #
    # Refunds are handled the same way, on purpose — manually, by Debo,
    # directly in Stripe's own dashboard, NOT automated by this system.
    # Automating real refunds correctly means handling partial refunds,
    # preventing double-refunds, and listening for another webhook event
    # (charge.refunded) — real complexity that isn't worth it at this
    # scale (~20 clients/day). A human doing it in Stripe's already-safe,
    # already-built refund UI is both less code and less risk than custom
    # refund logic here. Revisit only if this ever becomes a much higher-
    # volume storefront where manual refund handling stops scaling.

    # Two emails on successful cancellation — one to Debo confirming it
    # happened, one to the client notifying them. Only on the "cancelled"
    # outcome above, never on the "already_cancelled" rejection path (that's
    # a no-op, not a real state change). The `reason` captured earlier
    # (typed by Debo, or DEFAULT_CANCELLATION_REASON if he didn't provide
    # one) goes into both email bodies.
    ses = get_ses_service()
    ses.send_cancellation_notice_to_client(item, reason)
    ses.send_cancellation_notice_to_admin(item, reason, admin_email=os.environ["ADMIN_EMAIL"])

    logger.info("Booking %s cancelled by admin (reason: %s)", booking_id, reason)
    return _item_to_response(item)
FILEEOF

cat > src/scheduled/reminder_handler.py << 'FILEEOF'
"""
EventBridge-triggered daily reminder job.

Runs once a day, finds every booking scheduled for TOMORROW that's actually
CONFIRMED (not processing/cancelled/expired), and sends a reminder email —
so Debo doesn't have to manually track and message clients the day before.
"""

from datetime import datetime, timedelta, timezone

from src.api.core.database import get_db_service
from src.api.core.bookings_repository import BookingRepository
from src.api.core.ses_service import get_ses_service
from src.api.core.logging_config import get_logger

logger = get_logger(__name__)


def handler(event, context):
    tomorrow_date = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    logger.info("Reminder job triggered for date: %s", tomorrow_date)

    repo = BookingRepository(get_db_service())
    ses = get_ses_service()

    items = repo.query_by_date(tomorrow_date)

    # Slot-claim records share this table/GSI query surface — filter them
    # out here too, same as the admin list endpoint does. They're not real
    # bookings and have no email to send to.
    items = [item for item in items if not item["booking_id"].startswith("personal-slot#")]

    sent_count = 0
    skipped_count = 0
    failed_count = 0

    for booking in items:
        if booking.get("reminder_sent"):
            skipped_count += 1
            continue
        if booking.get("status") != "confirmed":
            # Only remind people who actually paid and are confirmed — not
            # a still-processing checkout or an already-cancelled booking.
            skipped_count += 1
            continue

        try:
            ses.send_reminder(booking)
            repo.mark_reminder_sent(booking["booking_id"])
            sent_count += 1
        except Exception as exc:
            # A single failed reminder shouldn't stop the rest of the batch
            # from going out — log it and keep processing the remaining
            # bookings for the day.
            logger.error(
                "Failed to send reminder for booking %s: %s",
                booking["booking_id"], exc, exc_info=True,
            )
            failed_count += 1

    logger.info(
        "Reminder job complete for %s: %s sent, %s skipped, %s failed (of %s bookings checked)",
        tomorrow_date, sent_count, skipped_count, failed_count, len(items),
    )

    return {
        "status": "complete",
        "target_date": tomorrow_date,
        "reminders_sent": sent_count,
        "skipped": skipped_count,
        "failed": failed_count,
    }
FILEEOF

cat > tests/conftest.py << 'FILEEOF'
"""
Shared test fixtures.

CRITICAL: fintech hit a real, documented bug where a module-level DynamoDB
singleton wasn't reset between test runs — a warm client/table reference
from one test's moto mock context silently survived into the next test's
DIFFERENT mock context, causing confusing, hard-to-diagnose failures.

The fix is this autouse fixture: reset every singleton BEFORE and AFTER
every single test, not just one or the other. "Before" guards against a
previous test leaving stale state; "after" guards against this test's
state leaking into whatever runs next. Both directions matter — this is
exactly the class of bug worth preventing here rather than rediscovering.
"""

import json
from unittest.mock import patch, MagicMock

import boto3
import pytest
from moto import mock_aws

import src.api.core.database as database_module
import src.api.core.security as security_module
import src.api.core.stripe_service as stripe_service_module
import src.api.core.ses_service as ses_service_module

TEST_ENV_VARS = {
    "ENVIRONMENT": "dev",
    "AWS_DEFAULT_REGION": "us-east-1",
    "BOOKINGS_TABLE_NAME": "debos-boxing-bookings-test",
    "LEADS_TABLE_NAME": "debos-boxing-leads-test",
    "SECURITY_TABLE_NAME": "debos-boxing-security-test",
    "JWT_SECRET_PATH": "/debos-boxing/test/jwt-secret",
    "ADMIN_CREDENTIALS_PATH": "/debos-boxing/test/admin-credentials",
    "PASSWORD_PEPPER_PATH": "/debos-boxing/test/password-pepper",
    "STRIPE_SECRET_PATH": "/debos-boxing/test/stripe-secret",
    "ADMIN_EMAIL": "test-admin@example-test.invalid",
    "FRONTEND_BASE_URL": "https://test.example.invalid",
}

# A known password used ONLY in tests, to exercise the real /auth/login
# route end-to-end (right password, wrong password, lockout escalation).
# Previously nothing did this — the admin_token fixture below minted a JWT
# directly, bypassing login entirely, which meant the login route itself
# had zero real test coverage.
TEST_ADMIN_PASSWORD = "TestAdminPassword123!"


@pytest.fixture(autouse=True)
def reset_singletons():
    database_module._db_service = None
    security_module._security_service = None
    stripe_service_module._stripe_service = None
    ses_service_module._ses_service = None
    yield
    database_module._db_service = None
    security_module._security_service = None
    stripe_service_module._stripe_service = None
    ses_service_module._ses_service = None


@pytest.fixture(autouse=True)
def aws_test_env(monkeypatch):
    for key, value in TEST_ENV_VARS.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def mock_aws_infra(aws_test_env):
    """Spins up mocked DynamoDB tables (matching infrastructure/template.yaml's
    real schema) and mocked Secrets Manager entries, scoped to a single test
    via moto's context manager. Anything using get_db_service()/
    get_security_service() during this fixture's lifetime talks to this
    mocked infra, not real AWS."""
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")

        ddb.create_table(
            TableName=TEST_ENV_VARS["BOOKINGS_TABLE_NAME"],
            KeySchema=[{"AttributeName": "booking_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "booking_id", "AttributeType": "S"},
                {"AttributeName": "session_date", "AttributeType": "S"},
                {"AttributeName": "session_time", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "session-date-index",
                "KeySchema": [
                    {"AttributeName": "session_date", "KeyType": "HASH"},
                    {"AttributeName": "session_time", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )

        ddb.create_table(
            TableName=TEST_ENV_VARS["LEADS_TABLE_NAME"],
            KeySchema=[{"AttributeName": "lead_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "lead_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        ddb.create_table(
            TableName=TEST_ENV_VARS["SECURITY_TABLE_NAME"],
            KeySchema=[{"AttributeName": "security_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "security_key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        sm = boto3.client("secretsmanager", region_name="us-east-1")
        sm.create_secret(
            Name=TEST_ENV_VARS["JWT_SECRET_PATH"],
            SecretString='{"secret": "test-jwt-secret-not-a-real-value"}',
        )
        sm.create_secret(
            Name=TEST_ENV_VARS["PASSWORD_PEPPER_PATH"],
            SecretString='{"pepper": "test-pepper-not-a-real-value"}',
        )
        sm.create_secret(
            Name=TEST_ENV_VARS["STRIPE_SECRET_PATH"],
            SecretString='{"api_key": "sk_test_not_a_real_key", "webhook_secret": "whsec_not_a_real_secret"}',
        )

        # Admin credentials secret needs a REAL computed hash, not a
        # placeholder — otherwise no test could ever exercise a genuinely
        # correct /auth/login. The hash must be computed AFTER the pepper
        # secret above exists (hash_password reads the pepper), and the
        # singleton must be reset first so the SecurityService instance
        # used here actually talks to the freshly-mocked Secrets Manager
        # rather than any stale prior instance.
        security_module._security_service = None
        security_service = security_module.get_security_service()
        admin_password_hash = security_service.hash_password(TEST_ADMIN_PASSWORD)
        sm.create_secret(
            Name=TEST_ENV_VARS["ADMIN_CREDENTIALS_PATH"],
            SecretString=json.dumps({"password_hash": admin_password_hash}),
        )
        # Reset again so actual test code gets a clean instance too, rather
        # than reusing internal state left over from computing the hash above.
        security_module._security_service = None

        # Moto's SES mock enforces identity verification the same way real
        # AWS does — send_email fails against an unverified identity. This
        # verifies the domain WITHIN the mock so booking confirmation /
        # cancellation / reminder emails succeed during tests, matching
        # the real verified domain in production.
        ses_client = boto3.client("ses", region_name="us-east-1")
        ses_client.verify_domain_identity(Domain="debosboxingandfitness.com")

        yield ddb


@pytest.fixture
def admin_token(mock_aws_infra):
    """A real, validly-signed JWT — computed the same way the actual
    /auth/login route would, using the mocked JWT secret above. Lets tests
    exercise the real require_admin dependency instead of bypassing it."""
    return security_module.get_security_service().create_jwt()


@pytest.fixture
def mock_stripe_checkout():
    """Mocks the actual outbound call to Stripe's API — same philosophy as
    moto mocking AWS. Our OWN StripeService code still runs for real (secret
    fetching, price-to-cents conversion, error handling); only the real
    network call to Stripe is intercepted, so tests stay fast, offline, and
    deterministic without needing real Stripe test credentials."""
    with patch("stripe.checkout.Session.create") as mock_create:
        fake_session = MagicMock()
        fake_session.url = "https://checkout.stripe.com/test-session-url"
        fake_session.id = "cs_test_fake_session_id"
        mock_create.return_value = fake_session
        yield mock_create


@pytest.fixture
def mock_stripe_webhook_verify():
    """Mocks stripe.Webhook.construct_event — lets tests supply a fake but
    properly-shaped event without needing a real Stripe-signed payload,
    while still exercising our OWN webhook route logic for real (looking up
    the booking, updating status, releasing/confirming slots)."""
    with patch("stripe.Webhook.construct_event") as mock_verify:
        yield mock_verify
FILEEOF

cat > tests/test_ses_service.py << 'FILEEOF'
from unittest.mock import MagicMock

from botocore.exceptions import ClientError

from src.api.core.ses_service import SesService

BOOKING = {
    "name": "Test Client",
    "email": "client@example.com",
    "session_date": "2026-08-10",
    "session_time": "10:00",
    "phone": "4045551234",
    "booking_type": "genes_adult",
    "price_usd": 100,
}


def _service_with_mock_client() -> tuple:
    service = SesService()
    mock_client = MagicMock()
    service._client = mock_client  # bypasses the lazy boto3.client() call entirely
    return service, mock_client


def test_booking_confirmation_sent_to_client():
    service, mock_client = _service_with_mock_client()
    service.send_booking_confirmation(BOOKING)

    mock_client.send_email.assert_called_once()
    kwargs = mock_client.send_email.call_args.kwargs
    assert kwargs["Destination"]["ToAddresses"] == ["client@example.com"]
    assert "confirmed" in kwargs["Message"]["Subject"]["Data"].lower()
    assert "2026-08-10" in kwargs["Message"]["Body"]["Text"]["Data"]


def test_new_booking_notification_sent_to_admin_not_client():
    service, mock_client = _service_with_mock_client()
    service.send_new_booking_notification(BOOKING, admin_email="debo@example.com")

    kwargs = mock_client.send_email.call_args.kwargs
    assert kwargs["Destination"]["ToAddresses"] == ["debo@example.com"]
    assert BOOKING["name"] in kwargs["Message"]["Body"]["Text"]["Data"]


def test_cancellation_notice_to_client_includes_reason():
    service, mock_client = _service_with_mock_client()
    service.send_cancellation_notice_to_client(BOOKING, reason="Debo is sick today")

    kwargs = mock_client.send_email.call_args.kwargs
    assert kwargs["Destination"]["ToAddresses"] == ["client@example.com"]
    assert "Debo is sick today" in kwargs["Message"]["Body"]["Text"]["Data"]


def test_cancellation_notice_to_admin_includes_reason():
    service, mock_client = _service_with_mock_client()
    service.send_cancellation_notice_to_admin(BOOKING, reason="Debo is sick today", admin_email="debo@example.com")

    kwargs = mock_client.send_email.call_args.kwargs
    assert kwargs["Destination"]["ToAddresses"] == ["debo@example.com"]
    assert "Debo is sick today" in kwargs["Message"]["Body"]["Text"]["Data"]


def test_reminder_sent_to_client():
    service, mock_client = _service_with_mock_client()
    service.send_reminder(BOOKING)

    kwargs = mock_client.send_email.call_args.kwargs
    assert kwargs["Destination"]["ToAddresses"] == ["client@example.com"]
    assert "tomorrow" in kwargs["Message"]["Subject"]["Data"].lower()


def test_send_failure_is_swallowed_not_raised():
    """The core design guarantee: a failed email must NEVER propagate up
    and fail the booking/cancellation action that already succeeded."""
    service, mock_client = _service_with_mock_client()
    mock_client.send_email.side_effect = ClientError(
        {"Error": {"Code": "MessageRejected", "Message": "test failure"}}, "SendEmail"
    )

    # Should not raise — this is the whole point of the test
    service.send_booking_confirmation(BOOKING)
FILEEOF

cat > tests/test_reminder_handler.py << 'FILEEOF'
from datetime import datetime, timedelta, timezone

from src.scheduled.reminder_handler import handler
from src.api.core.bookings_repository import BookingRepository
from src.api.core.database import get_db_service


def _tomorrow() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()


def _base_item(booking_id: str, **overrides) -> dict:
    item = {
        "booking_id": booking_id,
        "name": "Test Client",
        "email": "test@example.com",
        "phone": "4045551234",
        "session_date": _tomorrow(),
        "session_time": "10:00",
        "booking_type": "genes_adult",
        "location": "genes",
        "session_detail": "adult",
        "price_usd": 100,
        "status": "confirmed",
        "created_at": "2026-08-01T00:00:00+00:00",
        "reminder_sent": False,
    }
    item.update(overrides)
    return item


def test_reminder_sent_for_confirmed_unsent_booking(mock_aws_infra):
    repo = BookingRepository(get_db_service())
    booking_id = "reminder-test-1"
    repo.create(_base_item(booking_id))

    result = handler({}, None)

    assert result["reminders_sent"] == 1
    updated = repo.get_by_id(booking_id)
    assert updated["reminder_sent"] is True


def test_reminder_skips_already_sent(mock_aws_infra):
    repo = BookingRepository(get_db_service())
    repo.create(_base_item("reminder-test-2", reminder_sent=True))

    result = handler({}, None)

    assert result["reminders_sent"] == 0
    assert result["skipped"] == 1


def test_reminder_skips_non_confirmed_booking(mock_aws_infra):
    """A still-processing checkout shouldn't get a reminder — they haven't
    actually paid yet, so there's nothing confirmed to remind them about."""
    repo = BookingRepository(get_db_service())
    repo.create(_base_item("reminder-test-3", status="processing"))

    result = handler({}, None)

    assert result["reminders_sent"] == 0
    assert result["skipped"] == 1


def test_reminder_skips_cancelled_booking(mock_aws_infra):
    repo = BookingRepository(get_db_service())
    repo.create(_base_item("reminder-test-4", status="cancelled"))

    result = handler({}, None)

    assert result["reminders_sent"] == 0
    assert result["skipped"] == 1


def test_reminder_handles_multiple_bookings_same_day(mock_aws_infra):
    """Confirms the job processes an entire day's bookings correctly,
    sending only for the eligible ones — not all-or-nothing."""
    repo = BookingRepository(get_db_service())
    repo.create(_base_item("multi-1", session_time="09:00", status="confirmed"))
    repo.create(_base_item("multi-2", session_time="10:00", status="confirmed"))
    repo.create(_base_item("multi-3", session_time="11:00", status="cancelled"))

    result = handler({}, None)

    assert result["reminders_sent"] == 2
    assert result["skipped"] == 1


def test_reminder_job_returns_correct_target_date(mock_aws_infra):
    result = handler({}, None)
    assert result["target_date"] == _tomorrow()
FILEEOF

echo "Files updated. Running full test suite..."
python3 -m pip install -r requirements-dev.txt
python3 -m pytest tests/ -v

echo ""
echo "If all tests pass, review before committing:"
echo "  git status"
echo "  git diff"
echo ""
echo "Then commit (still on dev):"
echo "  git add ."
echo "  git commit -m 'Wire in real SES email sending: confirmations, notifications, cancellations, reminders'"
echo "  git push"
echo ""
echo "Once confirmed on dev, remove this script:"
echo "  rm wire_in_ses_emails.sh"
