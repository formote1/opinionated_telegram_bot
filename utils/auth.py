import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, unquote

from config import ADMIN_ID, TOKEN
from utils.privacy import public_id

_pin_store: dict[str, dict] = {}
_pin_lock = threading.Lock()


def _token_hash(token: str) -> str:
    """Stores a one-way fingerprint of bearer tokens instead of the token itself."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_auth_pin(user_id: int, username: str | None = None, expires_in: int = 600) -> str:
    """
    Generates a secure temporary 6-digit one-time PIN code mapping to user credentials.
    Defaults to 10 minutes (600s) validity. Username is ignored and never stored.
    """
    now = time.time()
    with _pin_lock:
        expired = [k for k, v in _pin_store.items() if v["expires_at"] < now]
        for k in expired:
            _pin_store.pop(k, None)
        pin = f"{secrets.randbelow(1_000_000):06d}"
        while pin in _pin_store:
            pin = f"{secrets.randbelow(1_000_000):06d}"
        _pin_store[pin] = {
            "user_id": user_id,
            "public_id": public_id(user_id),
            "is_admin": bool(ADMIN_ID and user_id == ADMIN_ID),
            "expires_at": now + expires_in,
        }
    return pin


def verify_auth_pin(pin: str) -> dict | None:
    """Verifies and consumes a 6-digit one-time PIN code."""
    if not pin or not isinstance(pin, str):
        return None
    cleaned_pin = pin.strip()
    now = time.time()
    with _pin_lock:
        entry = _pin_store.pop(cleaned_pin, None)
        if not entry:
            return None
        if entry["expires_at"] < now:
            return None
        return {
            "user_id": entry["user_id"],
            "public_id": entry["public_id"],
            "username": "",
            "is_admin": entry["is_admin"],
        }


def _get_auth_secret() -> bytes:
    """Returns secret bytes for HMAC signing based on TOKEN or fallback."""
    return (TOKEN or "opinionated-bot-default-secret-key").encode("utf-8")


def generate_auth_token(user_id: int, username: str | None = None, expires_in: int = 86400 * 30) -> str:
    """
    Generates a secure, time-limited HMAC-signed token for web companion login.
    Format: <base64_payload>.<hmac_signature>. The payload carries the internal
    user ID only; no username or personal detail is embedded.
    """
    ts = int(time.time())
    payload = f"{user_id}:{ts}:{expires_in}"
    sig = hmac.new(_get_auth_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    b64_payload = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("utf-8")
    return f"{b64_payload}.{sig}"


def verify_auth_token(token: str, max_age: int | None = None) -> dict | None:
    """Verifies an HMAC-signed web login token without exposing usernames."""
    if not token or "." not in token:
        return None
    try:
        b64_payload, sig = token.split(".", 1)
        payload_bytes = base64.urlsafe_b64decode(b64_payload.encode("utf-8"))
        payload = payload_bytes.decode("utf-8")

        expected_sig = hmac.new(_get_auth_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None

        parts = payload.split(":")
        if len(parts) < 3:
            return None

        user_id = int(parts[0])
        ts = int(parts[-2])
        token_expires_in = int(parts[-1])

        effective_ttl = max_age if max_age is not None else token_expires_in
        now = int(time.time())
        if now - ts > effective_ttl or ts > now + 60:
            return None

        return {
            "user_id": user_id,
            "public_id": public_id(user_id),
            "username": "",
            "is_admin": bool(ADMIN_ID and user_id == ADMIN_ID),
        }
    except Exception:
        return None


async def save_web_session(token: str, user_id: int, username: str | None = None, expires_in: int = 86400 * 30) -> dict:
    """Persists an active web auth session as a token hash; no username is stored."""
    if not token:
        return {}
    from database import web_sessions_col
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=expires_in)
    try:
        user_id_int = int(user_id)
    except (ValueError, TypeError):
        user_id_int = user_id
    session_data = {
        "token_hash": _token_hash(token),
        "user_id": user_id_int,
        "public_id": public_id(user_id_int),
        "is_admin": bool(ADMIN_ID and user_id_int == ADMIN_ID),
        "created_at": now.isoformat(),
        "expires_at": expires_at,
    }
    if web_sessions_col is not None:
        try:
            await web_sessions_col.update_one(
                {"token_hash": session_data["token_hash"]},
                {"$set": session_data},
                upsert=True
            )
        except Exception:
            pass
    return session_data


async def get_web_session(token: str) -> dict | None:
    """Retrieves an active web auth session by token fingerprint."""
    from database import web_sessions_col
    if not token or web_sessions_col is None:
        return None
    try:
        doc = await web_sessions_col.find_one({"token_hash": _token_hash(token)})
        if not doc:
            return None
        exp = doc.get("expires_at")
        if exp:
            if isinstance(exp, str):
                exp_dt = datetime.fromisoformat(exp)
            elif isinstance(exp, datetime):
                exp_dt = exp
            else:
                exp_dt = None
            if exp_dt:
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > exp_dt:
                    await web_sessions_col.delete_one({"token_hash": _token_hash(token)})
                    return None
        user_id = doc["user_id"]
        import config
        curr_admin_id = getattr(config, "ADMIN_ID", 0)
        is_admin = bool(curr_admin_id and user_id == curr_admin_id) if curr_admin_id else doc.get("is_admin", False)
        return {
            "user_id": user_id,
            "public_id": doc.get("public_id") or public_id(user_id),
            "username": "",
            "is_admin": is_admin,
        }
    except Exception:
        return None


async def delete_web_session(token: str) -> bool:
    """Removes a web session by token fingerprint upon logout."""
    from database import web_sessions_col
    if not token or web_sessions_col is None:
        return False
    try:
        res = await web_sessions_col.delete_one({"token_hash": _token_hash(token)})
        return res.deleted_count > 0
    except Exception:
        return False


def verify_telegram_init_data(init_data: str, max_age: int = 86400) -> dict | None:
    """Validates Telegram WebApp initData using Telegram's HMAC-SHA256 algorithm."""
    if not init_data or not TOKEN:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True)
        if not pairs:
            return None

        parsed = dict(pairs)
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None

        auth_date_val = parsed.get("auth_date")
        if auth_date_val:
            auth_date = int(auth_date_val)
            now = int(time.time())
            if max_age and (now - auth_date > max_age or auth_date > now + 60):
                return None

        check_items = [f"{k}={v}" for k, v in sorted(parsed.items())]
        data_check_string = "\n".join(check_items)

        secret_key = hmac.new(b"WebAppData", TOKEN.encode("utf-8"), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(received_hash, calculated_hash):
            return None

        user_raw = parsed.get("user")
        if not user_raw:
            return None

        user_data = json.loads(unquote(user_raw) if "%" in user_raw else user_raw)
        user_id = int(user_data["id"])
        return {
            "user_id": user_id,
            "public_id": public_id(user_id),
            "username": "",
            "is_admin": bool(ADMIN_ID and user_id == ADMIN_ID),
        }
    except Exception:
        return None
