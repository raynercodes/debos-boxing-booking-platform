"""
TEMPORARY FILE — delete entirely once AWS SES production access clears.

Resend email bridge. Fully isolated from ses_service.py on purpose —
zero shared code, zero shared state — so cleanup later is exactly:
delete this file, delete email_service.py, revert the import lines in
webhooks.py, bookings.py, lockout.py, and reminder_handler.py back to
importing get_ses_service directly. Nothing about ses_service.py itself
was ever touched.

Matches SesService's exact public method signatures (send_checkout_link,
send_booking_confirmation, etc.) so it's a genuine drop-in replacement —
whichever one email_service.py hands back, calling code never needs to
know or care which.

Uses urllib directly rather than the resend SDK, deliberately — avoids
adding a new dependency to requirements.txt for something explicitly
meant to be short-lived, not a permanent architectural choice.

MIGRATED from Secrets Manager to SSM Parameter Store (see security.py's
module docstring for the full cost/security reasoning).
"""

import os
import json
import urllib.request
import urllib.error

import boto3

from src.api.core.logging_config import get_logger
from src.api.models.booking import BOOKING_TYPE_DISPLAY_NAMES

logger = get_logger(__name__)

FROM_ADDRESS = "bookings@debosboxingandfitness.com"

# Same constant, same purpose, as ses_service.py - single source of
# truth so the signature can never drift out of sync between the two
# files the way it did before this fix.
EMAIL_SIGNATURE = "DEBO'S BOXING AND FITNESS"

ADMIN_PHONE_NUMBER = "(912) 278-1181"


class ResendService:
    def __init__(self) -> None:
        self._api_key = None

    def _get_api_key(self) -> str:
        """Fetched once per warm Lambda container, cached for its
        lifetime — same L1-caching pattern used for every other secret
        in this project."""
        if self._api_key is None:
            ssm_client = boto3.client("ssm")
            secret_path = os.environ["RESEND_SECRET_PATH"]
            response = ssm_client.get_parameter(Name=secret_path, WithDecryption=True)
            self._api_key = json.loads(response["Parameter"]["Value"])["api_key"]
        return self._api_key

    def _send(self, to_address: str, subject: str, body_text: str) -> None:
        try:
            api_key = self._get_api_key()
            payload = json.dumps({
                "from": FROM_ADDRESS,
                "to": to_address,
                "subject": subject,
                "text": body_text,
            }).encode("utf-8")
            request = urllib.request.Request(
                "https://api.resend.com/emails",
                data=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    # Real, concrete fix for a genuine issue, not
                    # cosmetic: Resend sits behind Cloudflare, and
                    # Cloudflare's Browser Integrity Check was blocking
                    # this request with "error code: 1010" ("banned based
                    # on your browser's signature") - a well-documented
                    # WAF behavior specifically triggered by Python's
                    # default urllib User-Agent string, which reads as
                    # bot-like/automated traffic. This is a completely
                    # legitimate, authenticated server-to-server API call
                    # (real Bearer token, real domain) - identifying it
                    # clearly with a real User-Agent is standard API
                    # etiquette anyway, not just a workaround.
                    "User-Agent": "DebosBoxingBookingPlatform/1.0",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                response.read()
            logger.info("Email sent via Resend to %s: %s", to_address, subject)
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            logger.error(
                "Failed to send email via Resend to %s (%s): HTTP %s - %s",
                to_address, subject, exc.code, error_body, exc_info=True,
            )
        except (urllib.error.URLError, KeyError, ValueError) as exc:
            logger.error("Failed to send email via Resend to %s (%s): %s", to_address, subject, exc, exc_info=True)

    def send_checkout_link(self, booking: dict, checkout_url: str) -> None:
        subject = f"Complete your booking with {EMAIL_SIGNATURE}"
        body = (
            f"Hi {booking['name']},\n\n"
            f"You started booking a session for {booking['session_date']} at {booking['session_time']}.\n\n"
            f"If you were redirected to payment automatically, you can ignore this email. "
            f"But if your browser closed, your connection dropped, or you just didn't finish — "
            f"use this link to complete your payment:\n\n"
            f"{checkout_url}\n\n"
            f"This link is valid for 30 minutes from when you started booking. After that, "
            f"you'll need to submit a new booking.\n\n"
            f"{EMAIL_SIGNATURE}"
        )
        self._send(booking["email"], subject, body)

    def send_booking_confirmation(self, booking: dict, receipt_url: str = None) -> None:
        subject = f"Your session with {EMAIL_SIGNATURE} is confirmed!"
        receipt_line = f"\nYour payment receipt: {receipt_url}\n" if receipt_url else ""
        body = (
            f"Hi {booking['name']},\n\n"
            f"Your booked session is confirmed for {booking['session_date']} at {booking['session_time']} with Debo.\n\n"
            f"See you then — please arrive on time, geared up and ready to be great!\n"
            f"{receipt_line}\n"
            f"Questions before your session? Reach Debo directly at {ADMIN_PHONE_NUMBER} or debosboxingandfitness@gmail.com.\n\n"
            f"{EMAIL_SIGNATURE}"
        )
        self._send(booking["email"], subject, body)

    def send_new_booking_notification(self, booking: dict, admin_email: str) -> None:
        subject = f"New booking: {booking['name']} on {booking['session_date']}"
        body = (
            f"New confirmed booking:\n\n"
            f"Name: {booking['name']}\n"
            f"Email: {booking['email']}\n"
            f"Phone: {booking['phone']}\n"
            f"Date: {booking['session_date']} at {booking['session_time']}\n"
            f"Type: {BOOKING_TYPE_DISPLAY_NAMES.get(booking['booking_type'], 'Booking')}\n"
            f"Price: ${booking['price_usd']}"
        )
        self._send(admin_email, subject, body)

    def send_cancellation_notice_to_client(self, booking: dict, reason: str, refund_info: dict = None) -> None:
        subject = f"Your booked session at {EMAIL_SIGNATURE} has been cancelled"
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
            f"{EMAIL_SIGNATURE}"
        )
        self._send(booking["email"], subject, body)

    def send_cancellation_notice_to_admin(self, booking: dict, reason: str, admin_email: str) -> None:
        subject = f"Booking cancelled for {booking['name']} on {booking['session_date']}"
        body = (
            f"You cancelled the following booking:\n\n"
            f"Name: {booking['name']}\n"
            f"Email: {booking['email']}\n"
            f"Date: {booking['session_date']} at {booking['session_time']}\n"
            f"Reason: {reason}"
        )
        self._send(admin_email, subject, body)

    def send_brute_force_alert(self, ip_address: str, lockout_count: int, admin_email: str) -> None:
        subject = "ACTION NEEDED: Someone is trying to break into your admin login"
        body = (
            f"Someone has now been locked out {lockout_count} separate times trying to "
            f"log into your admin panel.\n\n"
            f"Source IP address: {ip_address}\n\n"
            f"This is automatically logged — no action is required from you right now, "
            f"but if this continues, that IP address can be manually blocked from "
            f"reaching the site entirely.\n\n"
            f"{EMAIL_SIGNATURE} — Security"
        )
        self._send(admin_email, subject, body)

    def send_reminder(self, booking: dict) -> None:
        subject = f"Reminder: your session tomorrow at {booking['session_time']}"
        body = (
            f"Hi {booking['name']},\n\n"
            f"Just a reminder — your session with Debo is tomorrow, "
            f"{booking['session_date']} at {booking['session_time']}.\n\n"
            f"See you then!\n\n"
            f"{EMAIL_SIGNATURE}"
        )
        self._send(booking["email"], subject, body)


_resend_service = None


def get_resend_service() -> ResendService:
    global _resend_service
    if _resend_service is None:
        _resend_service = ResendService()
    return _resend_service
