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

    def set_stripe_session_id(self, booking_id: str, stripe_session_id: str) -> None:
        """Stored so /cancel-checkout can look up and force-expire the
        exact Stripe session when someone explicitly cancels — the
        session doesn't exist yet at the moment the booking item is
        first created, so this is a separate update right after Stripe
        actually returns it."""
        try:
            self._db.bookings_table.update_item(
                Key={"booking_id": booking_id},
                UpdateExpression="SET stripe_session_id = :sid",
                ExpressionAttributeValues={":sid": stripe_session_id},
            )
        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to store stripe_session_id for booking %s: %s", booking_id, exc, exc_info=True)
            raise ExternalServiceError("Unable to update booking") from exc

    def find_processing_booking_by_ip(self, client_ip: str) -> Optional[dict]:
        """Real Query against the client-ip-status-index GSI — not a table
        scan. This check runs on EVERY booking creation request, not an
        occasional admin action, so it needed a proper index rather than
        the scan-and-filter approach used elsewhere for genuinely rare
        operations. Slot-claim records never appear here at all — they
        have no client_ip attribute, so DynamoDB's GSI simply never
        indexes them in the first place.

        Returns the first processing booking found for this IP, or None."""
        try:
            response = self._db.bookings_table.query(
                IndexName="client-ip-status-index",
                KeyConditionExpression=Key("client_ip").eq(client_ip) & Key("status").eq("processing"),
                Limit=1,
            )
            items = response.get("Items", [])
            return items[0] if items else None
        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to check processing bookings for IP %s: %s", client_ip, exc, exc_info=True)
            raise ExternalServiceError("Unable to check existing bookings") from exc

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
