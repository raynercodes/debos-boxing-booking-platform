"""
EventBridge-triggered daily reminder job.

Runs once a day (scheduled in infrastructure/template.yaml). Finds every
booking scheduled for TOMORROW and sends a reminder email via SES, so the
gym owner doesn't have to manually track and message clients the day before
their session.

This is a stub — the real DynamoDB query and SES send are TODO until the
tables and SES identity exist. The handler signature and control flow are
locked in now so template.yaml has something real to point at.
"""

from datetime import datetime, timedelta, timezone

from src.api.core.logging_config import get_logger

logger = get_logger(__name__)


def handler(event, context):
    """
    TODO once infrastructure exists:
      1. Compute tomorrow_date = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
      2. Query the bookings table's session-date-index GSI for
         session_date == tomorrow_date (single Query, exact partition key match —
         same GSI already used by the admin list endpoint, no new index needed)
      3. For each booking where reminder_sent is False:
           - Send a reminder email via SES to booking.email
           - Update the booking's reminder_sent flag to True (prevents a
             re-run of this job on the same day from double-sending —
             EventBridge scheduled rules can occasionally double-fire,
             this flag is the safety net against that)
      4. Log a summary count (reminders sent, any failures) for visibility
         in CloudWatch — this job runs unattended once a day, so the log
         is the only way to notice if it silently stops working.
    """
    tomorrow_date = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    logger.info("Reminder job triggered for date: %s (stub — no real work done yet)", tomorrow_date)

    return {"status": "stub", "target_date": tomorrow_date, "reminders_sent": 0}
