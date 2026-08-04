import time
from fastapi import HTTPException

from src.api.core.database import get_security_table

MAX_ATTEMPTS = 5
# Progressive lockout windows in seconds — same escalation as fintech's authorizer.
# Applies regardless of whether the attacker found this route via /docs or by
# scanning common paths — an undocumented endpoint is not a protected one.
LOCKOUT_WINDOWS = [15 * 60, 30 * 60, 60 * 60, 24 * 60 * 60]  # 15min, 30min, 1hr, 24hr

LOCKOUT_KEY = "brute_force:admin_login"


def _get_lockout_record() -> dict:
    table = get_security_table()
    result = table.get_item(Key={"security_key": LOCKOUT_KEY})
    item = result.get("Item")
    if not item:
        return {"attempts": 0, "locked_until": 0, "lockout_stage": 0}
    # Never trust physical TTL deletion alone — evaluate explicitly
    if item.get("locked_until", 0) and item["locked_until"] < int(time.time()):
        return {"attempts": item["attempts"], "locked_until": 0, "lockout_stage": item.get("lockout_stage", 0)}
    return item


def check_lockout():
    """Call before verifying a password. Raises 429 if currently locked out."""
    record = _get_lockout_record()
    now = int(time.time())
    if record.get("locked_until", 0) > now:
        remaining = record["locked_until"] - now
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed login attempts. Try again in {remaining // 60} minute(s).",
        )


def record_failed_attempt():
    """Call after a failed password check. Escalates lockout window each time
    MAX_ATTEMPTS is hit again after a previous lockout."""
    table = get_security_table()
    record = _get_lockout_record()
    attempts = record.get("attempts", 0) + 1
    stage = record.get("lockout_stage", 0)

    if attempts >= MAX_ATTEMPTS:
        window = LOCKOUT_WINDOWS[min(stage, len(LOCKOUT_WINDOWS) - 1)]
        locked_until = int(time.time()) + window
        table.put_item(Item={
            "security_key": LOCKOUT_KEY,
            "attempts": 0,
            "lockout_stage": min(stage + 1, len(LOCKOUT_WINDOWS) - 1),
            "locked_until": locked_until,
            "expires_at": locked_until + 3600,  # TTL cleanup, one hour after lockout ends
        })
    else:
        table.put_item(Item={
            "security_key": LOCKOUT_KEY,
            "attempts": attempts,
            "lockout_stage": stage,
            "locked_until": 0,
            "expires_at": int(time.time()) + 86400,  # stale attempt counters expire in 24hr
        })


def clear_attempts():
    """Call after a successful login — resets the counter and stage entirely."""
    table = get_security_table()
    table.delete_item(Key={"security_key": LOCKOUT_KEY})
