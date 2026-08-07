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
