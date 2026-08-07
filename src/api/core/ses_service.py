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
