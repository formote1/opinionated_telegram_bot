import base64
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl, unquote

from config import ADMIN_ID, TOKEN


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
