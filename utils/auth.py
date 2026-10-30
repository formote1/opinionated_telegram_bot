import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from urllib.parse import parse_qsl, unquote

from config import ADMIN_ID, TOKEN

_pin_store: dict[str, dict] = {}
_pin_lock = threading.Lock()


def generate_auth_pin(user_id: int, username: str | None = None, expires_in: int = 600) -> str:
    """
    Generates a secure temporary 6-digit one-time PIN code mapping to user credentials.
    Defaults to 10 minutes (600s) validity.
    """
    now = time.time()
    is_admin = bool(ADMIN_ID and user_id == ADMIN_ID)
    with _pin_lock:
        expired = [k for k, v in _pin_store.items() if v["expires_at"] < now]
        for k in expired:
            _pin_store.pop(k, None)
        pin = f"{secrets.randbelow(1_000_000):06d}"
        while pin in _pin_store:
            pin = f"{secrets.randbelow(1_000_000):06d}"
        _pin_store[pin] = {
            "user_id": user_id,
            "username": username or "",
            "is_admin": is_admin,
            "expires_at": now + expires_in,
        }
    return pin


def verify_auth_pin(pin: str) -> dict | None:
    """
    Verifies and consumes a 6-digit one-time PIN code.
    Returns dict with user_id, username, is_admin, or None if invalid or expired.
    """
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
            "username": entry["username"],
            "is_admin": entry["is_admin"],
        }


def _get_auth_secret() -> bytes:
    """Returns secret bytes for HMAC signing based on TOKEN or fallback."""
    return (TOKEN or "opinionated-bot-default-secret-key").encode("utf-8")


def generate_auth_token(user_id: int, username: str | None = None, expires_in: int = 3600) -> str:
    """
    Generates a secure, time-limited HMAC-signed token for web companion login.
    Format: <base64_payload>.<hmac_signature>
    """
    ts = int(time.time())
    uname = username or ""
    # Payload: user_id:username:timestamp:expires_in
    payload = f"{user_id}:{uname}:{ts}:{expires_in}"
    sig = hmac.new(_get_auth_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    b64_payload = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("utf-8")
    return f"{b64_payload}.{sig}"


def verify_auth_token(token: str, max_age: int | None = None) -> dict | None:
    """
    Verifies an HMAC-signed web login token.
    Returns dict with user_id, username, is_admin, or None if invalid or expired.
    """
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
        if len(parts) < 4:
            return None

        user_id_str = parts[0]
        exp_str = parts[-1]
        ts_str = parts[-2]
        uname = ":".join(parts[1:-2])

        user_id = int(user_id_str)
        ts = int(ts_str)
        token_expires_in = int(exp_str)

        # Effective max age is token's embedded TTL, unless caller overrides with max_age
        effective_ttl = max_age if max_age is not None else token_expires_in
        now = int(time.time())
        if now - ts > effective_ttl or ts > now + 60:
            return None

        is_admin = bool(ADMIN_ID and user_id == ADMIN_ID)
        return {
            "user_id": user_id,
            "username": uname,
            "is_admin": is_admin
        }
    except Exception:
        return None


def verify_telegram_init_data(init_data: str, max_age: int = 86400) -> dict | None:
    """
    Validates Telegram WebApp initData string using Telegram's HMAC-SHA256 signature algorithm.
    """
    if not init_data or not TOKEN:
        return None
    try:
        # Some clients pass URL-encoded init_data, others unencoded
        pairs = parse_qsl(init_data, keep_blank_values=True)
        if not pairs:
            return None

        parsed = dict(pairs)
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None

        # Check auth_date freshness
        auth_date_val = parsed.get("auth_date")
        if auth_date_val:
            auth_date = int(auth_date_val)
            now = int(time.time())
            if max_age and (now - auth_date > max_age or auth_date > now + 60):
                return None

        # Data-check-string is sorted pairs of key=value separated by \n
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
        username = user_data.get("username") or ""
        is_admin = bool(ADMIN_ID and user_id == ADMIN_ID)
        return {
            "user_id": user_id,
            "username": username,
            "is_admin": is_admin
        }
    except Exception:
        return None
