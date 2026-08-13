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

# Real, not placeholder — only ever surfaced in transactional emails where
# there's a legitimate, immediate reason someone would need it (a
# confirmed or just-cancelled real booking). Deliberately never displayed
# on the public marketing site itself — Debo's own call, keeping his
# personal number out of anything a random visitor could stumble across.
ADMIN_PHONE_NUMBER = "(912) 278-1181"


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

    def send_checkout_link(self, booking: dict, checkout_url: str) -> None:
        """Sent immediately when checkout STARTS, not when it confirms —
        the recovery path for someone who gets redirected to Stripe, then
        closes the tab, loses connection, or just gets distracted before
        paying. Without this, that booking sits stuck in 'processing' for
        the full 30-minute expiry with zero way back in, even though
        Stripe's own session URL is still perfectly valid and reusable
        the whole time. Re-sending them the exact same URL costs nothing
        and creates no duplicate booking or charge risk — it's the same
        checkout session, just given a second entry point."""
        subject = "Complete your booking with DEBO'S BOXING AND FITNESS"
        body = (
            f"Hi {booking['name']},\n\n"
            f"You started booking a session for {booking['session_date']} at {booking['session_time']}.\n\n"
            f"If you were redirected to payment automatically, you can ignore this email. "
            f"But if your browser closed, your connection dropped, or you just didn't finish — "
            f"use this link to complete your payment:\n\n"
            f"{checkout_url}\n\n"
            f"This link is valid for 30 minutes from when you started booking. After that, "
            f"you'll need to submit a new booking.\n\n"
            f"DEBO'S BOXING AND FITNESS"
        )
        self._send(booking["email"], subject, body)

    def send_booking_confirmation(self, booking: dict, receipt_url: str = None) -> None:
        subject = "Your session with DEBO'S BOXING AND FITNESS is confirmed!"
        receipt_line = f"\nYour payment receipt: {receipt_url}\n" if receipt_url else ""
        body = (
            f"Hi {booking['name']},\n\n"
            f"Your booked session is confirmed for {booking['session_date']} at {booking['session_time']} with Debo.\n\n"
            f"See you then — please arrive on time, geared up and ready to be great!\n"
            f"{receipt_line}\n"
            f"Questions before your session? Reach Debo directly at {ADMIN_PHONE_NUMBER} or debosboxingandfitness@gmail.com.\n\n"
            f"DEBO'S BOXING AND FITNESS"
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
            f"Payment amount sent to you: ${booking['price_usd']}\n"
        )
        self._send(admin_email, subject, body)

    def send_cancellation_notice_to_client(self, booking: dict, reason: str, refund_info: dict = None) -> None:
        subject = "Your booked session at DEBO'S BOXING AND FITNESS has been cancelled"
        refund_line = ""
        if refund_info:
            refund_line = (
                f"\nA refund of ${refund_info['amount_usd']:.2f} has been issued back to your original "
                f"payment method — please allow 5-10 business days for it to appear on your statement.\n"
            )
            if refund_info.get("receipt_url"):
                refund_line += f"You can view the refund on your receipt here: {refund_info['receipt_url']}\n"
        body = (
            f"Hi {booking['name']},\n\n"
            f"Sorry to inform you, but your session on {booking['session_date']} at {booking['session_time']} has been cancelled by Debo.\n\n"
            f"Cancelation reason: {reason}\n"
            f"{refund_line}\n"
            f"If you have any questions or want to rebook, please reach out to Debo directly at {ADMIN_PHONE_NUMBER} or debosboxingandfitness@gmail.com.\n\n"
            f"DEBO'S BOXING AND FITNESS"
        )
        self._send(booking["email"], subject, body)

    def send_cancellation_notice_to_admin(self, booking: dict, reason: str, admin_email: str) -> None:
        subject = f"Booking cancelled for {booking['name']} on {booking['session_date']}"
        body = (
            f"You cancelled the following booking:\n\n"
            f"Client: {booking['name']}\n"
            f"Date: {booking['session_date']} at {booking['session_time']}\n"
            f"Reason given to client: {reason}\n"
        )
        self._send(admin_email, subject, body)

    def send_brute_force_alert(self, ip_address: str, lockout_count: int, admin_email: str) -> None:
        """Fires once an attacker hits the 3rd separate lockout — a sign
        of a sustained attack, not an honest forgotten password. Purely
        informational, deliberately no verification link or action
        required — the activity is already being logged server-side, so
        there's nothing for Debo to confirm, just something to be aware
        of. Geo-location note: not included yet — this needs CloudFront's
        real viewer-location headers, which don't exist until CloudFront
        is actually enabled. Trivial to add here once that's live."""
        subject = "Security alert: repeated failed login attempts on your booking site"
        body = (
            f"Someone has now been locked out {lockout_count} separate times trying to "
            f"log into your admin panel.\n\n"
            f"Source IP address: {ip_address}\n\n"
            f"This is automatically logged — no action is required from you right now, "
            f"but if this continues, that IP address can be manually blocked from "
            f"reaching the site entirely.\n\n"
            f"DEBO'S BOXING AND FITNESS — Security"
        )
        self._send(admin_email, subject, body)

    def send_reminder(self, booking: dict) -> None:
        subject = "Reminder: your booked training session with DEBO'S BOXING AND FITNESS is tomorrow"
        body = (
            f"Hi {booking['name']},\n\n"
            f"Just a reminder — your session is tomorrow, {booking['session_date']} at {booking['session_time']}.\n\n"
            f"See you then!\n\n"
            f"DEBO'S BOXING AND FITNESS"
        )
        self._send(booking["email"], subject, body)


_ses_service = None


def get_ses_service() -> SesService:
    global _ses_service
    if _ses_service is None:
        _ses_service = SesService()
    return _ses_service
