import time
from collections import defaultdict
from config import ADMIN_ID
from database import group_keys_col, unlocked_groups_col

# Brute-force tracking: user_id -> {"attempts": [timestamps], "locked_until": timestamp}
_brute_force_tracker = defaultdict(lambda: {"attempts": [], "locked_until": 0})


def is_locked_out(user_id: int) -> tuple[bool, int]:
    """
    Checks if a user is currently locked out from entering passkeys.
    Returns (is_locked: bool, remaining_seconds: int).
    """
    now = time.time()
    state = _brute_force_tracker[user_id]
    if now < state["locked_until"]:
        return True, int(state["locked_until"] - now)
    return False, 0


def record_failed_attempt(user_id: int) -> tuple[int, bool]:
    """
    Records a failed passkey attempt for user.
    If attempts >= 5 within 10 minutes (600s), triggers 1-hour lockout.
    Returns (current_failed_count: int, just_locked_out: bool).
    """
    now = time.time()
    state = _brute_force_tracker[user_id]
    cutoff = now - 600
    state["attempts"] = [t for t in state["attempts"] if t > cutoff]
    state["attempts"].append(now)

    if len(state["attempts"]) >= 5:
        state["locked_until"] = now + 3600  # 1 hour lockout
        return len(state["attempts"]), True

    return len(state["attempts"]), False


def reset_failed_attempts(user_id: int):
    """Resets failed passkey attempts upon successful authentication."""
    if user_id in _brute_force_tracker:
        _brute_force_tracker[user_id] = {"attempts": [], "locked_until": 0}


async def check_access_gate(user_id: int, record: dict) -> tuple[bool, str, int | None, bool, bool]:
    """
    Verifies if a user has permission to access a record.
    Returns: (is_authorized, prefix, chat_id, is_frozen, is_expired)
    """
    code = record.get("code", "")
    prefix = code[:3].upper() if code else ""
    chat_id = record.get("chat_id")

    if user_id == ADMIN_ID:
        return True, prefix, chat_id, False, False

    if not prefix or chat_id is None or group_keys_col is None:
        return True, prefix, chat_id, False, False

    gate = await group_keys_col.find_one({"chat_id": chat_id, "prefix": prefix})
    if not gate:
        return True, prefix, chat_id, False, False

    if unlocked_groups_col is not None:
        auth = await unlocked_groups_col.find_one({"user_id": user_id})
        if auth:
            auth_key = f"{chat_id}_{prefix}"
            # Check if access was frozen by admin
            if auth_key in auth.get("frozen_prefixes", []):
                return False, prefix, chat_id, True, False

            # Check time-limited expiration
            expirations = auth.get("prefix_expirations", {})
            expires_at = expirations.get(auth_key)
            if expires_at and time.time() > expires_at:
                # Auto-revoke expired access
                await unlocked_groups_col.update_one(
                    {"user_id": user_id},
                    {
                        "$pull": {"unlocked_prefixes": auth_key},
                        "$unset": {f"prefix_expirations.{auth_key}": ""}
                    }
                )
                return False, prefix, chat_id, False, True

            if auth_key in auth.get("unlocked_prefixes", []):
                return True, prefix, chat_id, False, False

    return False, prefix, chat_id, False, False
