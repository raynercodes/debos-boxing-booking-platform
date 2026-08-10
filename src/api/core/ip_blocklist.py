"""
Manual IP blocklist for the admin login endpoint.

Deliberately NOT AWS WAF. WAF is the "correct" edge-level tool for this,
but carries a real recurring cost (~$5-6/month minimum, even at zero
traffic) — not justified before any real abuse has actually happened,
matching the same cost discipline already applied elsewhere in this
project (WAF was passed on for the same reason on the fintech build).

This checks INSIDE our own Lambda, not at the API Gateway edge — the
request still reaches Lambda before being rejected, unlike true edge
blocking. That's the accepted tradeoff for staying free at this scale.
If real abuse ever becomes an ongoing problem, WAF is the natural,
easily-justified upgrade at that point, not before.
"""

from botocore.exceptions import ClientError, BotoCoreError
from datetime import datetime, timezone

from src.api.core.database import DynamoDBService
from src.api.core.logging_config import get_logger

logger = get_logger(__name__)

BLOCKLIST_KEY = "ip_blocklist"


class IPBlocklist:
    def __init__(self, db_service: DynamoDBService) -> None:
        self._db = db_service

    def is_blocked(self, ip: str) -> bool:
        """Fails OPEN (assumes not-blocked) on a read error — deliberately
        different from LockoutManager's fail-CLOSED behavior. This
        blocklist is an ADDITIONAL layer on top of the already fail-closed
        lockout system, not a replacement for it — if this specific check
        fails, real brute-force protection is still fully intact and
        unaffected. Failing open here just means a manually-banned IP
        might get one more attempt against the (still fully functional)
        lockout system, rather than risking a transient DynamoDB blip
        locking out the real admin entirely."""
        try:
            result = self._db.security_table.get_item(Key={"security_key": BLOCKLIST_KEY})
        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to check IP blocklist (failing open): %s", exc, exc_info=True)
            return False

        item = result.get("Item")
        if not item:
            return False
        return ip in item.get("blocked_ips", set())

    def add_ip(self, ip: str) -> None:
        try:
            self._db.security_table.update_item(
                Key={"security_key": BLOCKLIST_KEY},
                UpdateExpression="ADD blocked_ips :ip",
                ExpressionAttributeValues={":ip": {ip}},
            )
            logger.warning("IP %s added to blocklist", ip)
        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to add IP %s to blocklist: %s", ip, exc, exc_info=True)
            raise

    def remove_ip(self, ip: str) -> None:
        try:
            self._db.security_table.update_item(
                Key={"security_key": BLOCKLIST_KEY},
                UpdateExpression="DELETE blocked_ips :ip",
                ExpressionAttributeValues={":ip": {ip}},
            )
            logger.info("IP %s removed from blocklist", ip)
        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to remove IP %s from blocklist: %s", ip, exc, exc_info=True)
            raise

    def list_blocked(self) -> list:
        try:
            result = self._db.security_table.get_item(Key={"security_key": BLOCKLIST_KEY})
        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to list blocked IPs: %s", exc, exc_info=True)
            raise
        item = result.get("Item")
        if not item:
            return []
        return sorted(item.get("blocked_ips", set()))


class FailedLoginTracker:
    """Per-IP failed-attempt counts, separate from LockoutManager's
    global lockout state and the brute-force alert email (which only
    fires at the 3rd separate lockout). This exists purely for quick
    visibility — "which IPs have actually been failing, and how many
    times" — so an admin can decide who to block without digging through
    raw CloudWatch logs one line at a time. A scan is used to list all
    tracked IPs, deliberately — this is a rare, admin-only lookup action,
    not something on the request-critical login path itself, matching
    the same reasoning already applied to the leads-follow-up feature."""

    KEY_PREFIX = "failed_login_ip:"

    def __init__(self, db_service: DynamoDBService) -> None:
        self._db = db_service

    def record_attempt(self, ip: str) -> None:
        try:
            self._db.security_table.update_item(
                Key={"security_key": f"{self.KEY_PREFIX}{ip}"},
                UpdateExpression="ADD attempt_count :one SET last_attempt_at = :now",
                ExpressionAttributeValues={":one": 1, ":now": datetime.now(timezone.utc).isoformat()},
            )
        except (ClientError, BotoCoreError) as exc:
            # Deliberately never raises — a tracking failure must not
            # block the actual login rejection from completing.
            logger.error("Failed to record failed-attempt tracking for IP %s: %s", ip, exc, exc_info=True)

    def list_recent(self) -> list:
        try:
            response = self._db.security_table.scan(
                FilterExpression="begins_with(security_key, :prefix)",
                ExpressionAttributeValues={":prefix": self.KEY_PREFIX},
            )
        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to list failed-login attempts: %s", exc, exc_info=True)
            raise
        items = response.get("Items", [])
        results = [
            {
                "ip": item["security_key"][len(self.KEY_PREFIX):],
                "attempt_count": int(item.get("attempt_count", 0)),
                "last_attempt_at": item.get("last_attempt_at"),
            }
            for item in items
        ]
        return sorted(results, key=lambda r: r["attempt_count"], reverse=True)
