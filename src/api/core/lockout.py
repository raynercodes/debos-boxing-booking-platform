"""
Progressive brute-force lockout for admin login.

Applies regardless of whether an attacker found /auth/login via /docs or by
scanning common paths — an undocumented endpoint is not a protected one.

PHASE 2 (post-MVP, not built yet): upgrade to dual-identifier lockout matching
the fintech authorizer's pattern — track failed attempts by BOTH the admin
email AND the source IP independently. Right now this only catches an
attacker hammering the single known admin email; it does NOT catch an
attacker trying many different email guesses from one IP, since there's only
one valid email to guess correctly. Add a second LockoutManager instance
keyed by source IP (from the API Gateway event via Mangum's scope) once the
MVP is stable.
"""

import time
from dataclasses import dataclass

from botocore.exceptions import ClientError, BotoCoreError

from src.api.core.database import DynamoDBService
from src.api.core.exceptions import LockedOutError, ExternalServiceError
from src.api.core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class LockoutRecord:
    """Typed structure instead of a raw dict — a mistyped key like
    'locked_untill' now fails at construction, not silently at runtime."""

    attempts: int = 0
    locked_until: int = 0
    lockout_stage: int = 0


class LockoutManager:
    MAX_ATTEMPTS = 5
    # Progressive lockout windows in seconds — same escalation as fintech's authorizer
    LOCKOUT_WINDOWS = [15 * 60, 30 * 60, 60 * 60, 24 * 60 * 60]  # 15min, 30min, 1hr, 24hr
    LOCKOUT_KEY = "brute_force:admin_login"

    def __init__(self, db_service: DynamoDBService) -> None:
        self._db = db_service

    def _get_record(self) -> LockoutRecord:
        """Reads the current lockout state. Wrapped in try/except because a
        DynamoDB read CAN fail (throttling, table not ready, network blip) —
        and if this silently crashed instead of failing safely, it could
        either lock out the real admin forever (bad) or, worse, let lockout
        checks be bypassed entirely if the exception weren't handled at all
        (much worse — that would defeat the whole point of this module).
        We fail CLOSED here: if we can't confirm the current state, we log
        it and raise, rather than assuming 'no record' and letting a login
        attempt through unchecked."""
        try:
            result = self._db.security_table.get_item(Key={"security_key": self.LOCKOUT_KEY})
        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to read lockout record: %s", exc, exc_info=True)
            raise ExternalServiceError("Unable to verify login attempt status") from exc

        item = result.get("Item")
        if not item:
            return LockoutRecord()

        # DynamoDB returns numbers as Decimal, not int/float — explicit int()
        # conversion here is what fixes a real bug this test suite caught:
        # record.lockout_stage was being used directly as a list index
        # (LOCKOUT_WINDOWS[record.lockout_stage]) further down, which crashes
        # immediately on a Decimal ("list indices must be integers... not
        # decimal.Decimal"). No test before this ever drove a real 5th-failure
        # through actual DynamoDB, so this was invisible until tested for real.
        locked_until = int(item.get("locked_until", 0))

        # Never trust physical TTL deletion alone — evaluate explicitly
        if locked_until and locked_until < int(time.time()):
            return LockoutRecord(attempts=int(item.get("attempts", 0)), locked_until=0,
                                  lockout_stage=int(item.get("lockout_stage", 0)))

        return LockoutRecord(
            attempts=int(item.get("attempts", 0)),
            locked_until=locked_until,
            lockout_stage=int(item.get("lockout_stage", 0)),
        )

    def check_lockout(self) -> None:
        """Call before verifying a password. Raises LockedOutError if currently locked out."""
        record = self._get_record()
        now = int(time.time())
        if record.locked_until > now:
            logger.warning("Blocked login attempt — admin account currently locked out")
            raise LockedOutError(retry_after_seconds=record.locked_until - now)

    def record_failed_attempt(self) -> None:
        """Call after a failed password check. Escalates the lockout window
        each time MAX_ATTEMPTS is hit again after a previous lockout."""
        record = self._get_record()
        attempts = record.attempts + 1

        try:
            if attempts >= self.MAX_ATTEMPTS:
                window = self.LOCKOUT_WINDOWS[min(record.lockout_stage, len(self.LOCKOUT_WINDOWS) - 1)]
                locked_until = int(time.time()) + window
                self._db.security_table.put_item(Item={
                    "security_key": self.LOCKOUT_KEY,
                    "attempts": 0,
                    "lockout_stage": min(record.lockout_stage + 1, len(self.LOCKOUT_WINDOWS) - 1),
                    "locked_until": locked_until,
                    "expires_at": locked_until + 3600,  # TTL cleanup, 1hr after lockout ends
                })
                logger.warning(
                    "Admin login locked out for %s seconds after %s failed attempts",
                    window, attempts,
                )
            else:
                self._db.security_table.put_item(Item={
                    "security_key": self.LOCKOUT_KEY,
                    "attempts": attempts,
                    "lockout_stage": record.lockout_stage,
                    "locked_until": 0,
                    "expires_at": int(time.time()) + 86400,  # stale counters expire in 24hr
                })
                logger.warning("Failed admin login attempt #%s recorded", attempts)
        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to record lockout attempt: %s", exc, exc_info=True)
            raise ExternalServiceError("Unable to process login attempt") from exc

    def clear_attempts(self) -> None:
        """Call after a successful login — resets the counter and stage entirely."""
        try:
            self._db.security_table.delete_item(Key={"security_key": self.LOCKOUT_KEY})
        except (ClientError, BotoCoreError) as exc:
            # Deliberately NOT re-raised as ExternalServiceError here — this
            # runs after a password already verified successfully. Failing
            # the whole login because the cleanup step had a hiccup would be
            # a worse outcome than just leaving a stale counter behind for
            # next time (it'll self-correct via TTL anyway). Log and move on.
            logger.error("Failed to clear lockout attempts (non-fatal): %s", exc, exc_info=True)
