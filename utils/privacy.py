"""Privacy helpers: stable, non-reversible public pseudonyms for Telegram user IDs.

The numeric Telegram ID is kept internally as the delivery key for proactive
messages, but no human-facing surface (admin chat, logs, web responses, tokens
in URLs) ever needs to see it.  SECRET_KEY is an environment secret; derived
keys exist only in process memory.
"""

import hashlib
import hmac
import re

import database
from config import SECRET_KEY, TOKEN

_PUBLIC_ID_RE = re.compile(r"^U1_[0-9a-f]{12}$")


def _secret_bytes() -> bytes:
    """Returns the deployment secret. Tests/offline mode may fall back, but the bot refuses to start without SECRET_KEY."""
    return (SECRET_KEY or TOKEN or "opinionated-bot-offline-fallback").encode("utf-8")


def _derive(info: str) -> bytes:
    """Domain-separated key derivation so one secret can safely serve several purposes."""
    return hashlib.sha256(b"opinionated-privacy-v1|" + info.encode("utf-8") + b"|" + _secret_bytes()).digest()


_PSEUDONYM_KEY = _derive("pseudonym-v1")


def public_id(user_id) -> str:
    """Returns a stable pseudonym such as U1_3f9a1c2b7e04 for an internal user ID."""
    try:
        raw = str(int(user_id)).encode("utf-8")
    except (TypeError, ValueError):
        raw = str(user_id).encode("utf-8")
    digest = hmac.new(_PSEUDONYM_KEY, raw, hashlib.sha256).hexdigest()
    return f"U1_{digest[:12]}"


def is_public_id(value) -> bool:
    """True when value has the U1_<12 hex> pseudonym shape."""
    return bool(value) and bool(_PUBLIC_ID_RE.fullmatch(str(value)))


def flask_secret_key() -> bytes:
    """Session-signing key derived from SECRET_KEY, never the raw secret or bot token."""
    return _derive("flask-session-v1")


async def ensure_user_alias(user_id) -> str | None:
    """Creates (or backfills) the minimal alias record {user_id, public_id}."""
    users_col = database.users_col
    if users_col is None or not user_id:
        return None
    alias = public_id(user_id)
    try:
        await users_col.update_one(
            {"user_id": user_id},
            {"$setOnInsert": {"user_id": user_id}, "$set": {"public_id": alias}},
            upsert=True,
        )
    except Exception:
        pass
    return alias


async def resolve_public_id(value) -> int | None:
    """Resolves a pseudonym back to the internal delivery ID via the alias directory."""
    users_col = database.users_col
    if not is_public_id(value) or users_col is None:
        return None
    try:
        doc = await users_col.find_one({"public_id": str(value)})
        return doc.get("user_id") if doc else None
    except Exception:
        return None
