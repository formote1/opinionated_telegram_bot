import time
from collections import defaultdict

from config import ADMIN_ID
from database import group_keys_col, unlocked_groups_col

# Brute-force tracking: user_id -> {"attempts": [timestamps], "locked_until": timestamp}
_brute_force_tracker = defaultdict(lambda: {"attempts": [], "locked_until": 0})
_last_access_prune = 0.0

# In-memory gate keys cache: (chat_id, prefix) -> (timestamp, dict | None)
_group_keys_cache: dict[tuple[int, str], tuple[float, dict | None]] = {}
GROUP_KEY_CACHE_TTL = 300.0  # 5 minutes


def invalidate_group_keys_cache(prefix: str | None = None):
    """Invalidates the in-memory cache of group keys when an admin updates keys."""
    if prefix is None:
        _group_keys_cache.clear()
    else:
        pfx = prefix.upper()
        keys_to_del = [k for k in _group_keys_cache if len(k) > 1 and k[1] == pfx]
        for k in keys_to_del:
            _group_keys_cache.pop(k, None)



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
    global _last_access_prune
    now = time.time()
    state = _brute_force_tracker[user_id]
    cutoff = now - 600
    state["attempts"] = [t for t in state["attempts"] if t > cutoff]
    state["attempts"].append(now)

    if len(state["attempts"]) >= 5:
        state["locked_until"] = now + 3600  # 1 hour lockout
        return len(state["attempts"]), True

    if len(_brute_force_tracker) > 200 and (now - _last_access_prune > 60.0):
        _last_access_prune = now
        stale = [u for u, st in _brute_force_tracker.items() if st["locked_until"] <= now and (not st["attempts"] or st["attempts"][-1] <= cutoff)]
        for u in stale:
            _brute_force_tracker.pop(u, None)

    return len(state["attempts"]), False


def reset_failed_attempts(user_id: int):
    """Resets failed passkey attempts upon successful authentication."""
    _brute_force_tracker.pop(user_id, None)



async def get_group_gate(chat_id: int, prefix: str) -> dict | None:
    """Retrieves gate document for partition, utilizing in-memory cache."""
    if not prefix or chat_id is None:
        return None
    pfx = prefix.upper()
    cache_key = (chat_id, pfx)
    now = time.time()
    if cache_key in _group_keys_cache:
        ts, cached_gate = _group_keys_cache[cache_key]
        if now - ts < GROUP_KEY_CACHE_TTL:
            return cached_gate
        _group_keys_cache.pop(cache_key, None)

    if group_keys_col is None:
        return None

    gate = await group_keys_col.find_one({"chat_id": chat_id, "prefix": pfx})
    _group_keys_cache[cache_key] = (now, gate)
    return gate


async def is_user_banned(user_id: int) -> bool:
    """Checks whether a user is banned from accessing content."""
    if user_id == ADMIN_ID or unlocked_groups_col is None:
        return False
    doc = await unlocked_groups_col.find_one({"user_id": user_id}, {"is_banned": 1})
    return bool(doc and doc.get("is_banned"))


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

    if unlocked_groups_col is not None:
        auth = await unlocked_groups_col.find_one(
            {"user_id": user_id},
            {"is_banned": 1, "unlocked_prefixes": 1, "frozen_prefixes": 1, "prefix_expirations": 1}
        )
        if auth and auth.get("is_banned"):
            return False, prefix, chat_id, False, False
    else:
        auth = None

    if not prefix or chat_id is None or group_keys_col is None:
        return True, prefix, chat_id, False, False

    gate = await get_group_gate(chat_id, prefix)
    if not gate:
        return True, prefix, chat_id, False, False

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

