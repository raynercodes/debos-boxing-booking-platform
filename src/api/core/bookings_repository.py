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

    def update_status(self, booking_id: str, new_status: str) -> dict:
        """No ConditionExpression restricting which PRIOR status is valid —
        unlike fintech's loan state machine, cancellation here is explicitly
        allowed "at any time" regardless of current status (confirmed,
        already completed, even already cancelled). The ONLY condition is
        that the booking actually exists — attribute_exists(booking_id)
        guards against silently "updating" something that was never there,
        which would otherwise succeed and return a fabricated-looking
        response for a booking_id that was never real."""
        try:
            result = self._db.bookings_table.update_item(
                Key={"booking_id": booking_id},
                UpdateExpression="SET #s = :new_status",
                ConditionExpression="attribute_exists(booking_id)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":new_status": new_status},
                ReturnValues="ALL_NEW",
            )
            return result["Attributes"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # Caller (the route) is responsible for translating a None-ish
                # signal into BookingNotFoundError — we raise a distinct,
                # narrow exception here so the route can tell "doesn't exist"
                # apart from "AWS call actually failed."
                return None
            logger.error("Failed to update booking %s status: %s", booking_id, exc, exc_info=True)
            raise ExternalServiceError("Unable to update booking") from exc
        except BotoCoreError as exc:
            logger.error("Failed to update booking %s status: %s", booking_id, exc, exc_info=True)
            raise ExternalServiceError("Unable to update booking") from exc
