import hashlib
import hmac
import json
import time

from config import ADMIN_ID, TOKEN
from utils.auth import (
    generate_auth_token,
    verify_auth_token,
    verify_telegram_init_data,
)


def test_auth_token_roundtrip():
    user_id = 123456789
    username = "testuser"
    token = generate_auth_token(user_id, username, expires_in=3600)
    assert token is not None
    assert "." in token

    info = verify_auth_token(token)
    assert info is not None
    assert info["user_id"] == user_id
    assert info["username"] == username
    assert info["is_admin"] == (ADMIN_ID and user_id == ADMIN_ID)


def test_auth_token_tampered():
    token = generate_auth_token(999, "alice")
    b64_part, sig = token.split(".", 1)
    # Alter the signature
    tampered_sig = "a" * len(sig) if sig[0] != "a" else "b" * len(sig)
    assert verify_auth_token(f"{b64_part}.{tampered_sig}") is None

    # Alter payload
    assert verify_auth_token(f"malformed_payload.{sig}") is None


def test_auth_token_expired():
    # Token with 0 second TTL
    token = generate_auth_token(888, "bob", expires_in=-10)
    assert verify_auth_token(token) is None


def test_auth_token_admin_flag():
    if ADMIN_ID != 0:
        admin_token = generate_auth_token(ADMIN_ID, "superadmin")
        info = verify_auth_token(admin_token)
        assert info is not None
        assert info["is_admin"] is True

    non_admin_token = generate_auth_token(ADMIN_ID + 99999, "regular")
    info2 = verify_auth_token(non_admin_token)
    assert info2 is not None
    assert info2["is_admin"] is False


def test_verify_telegram_init_data_valid():
    if not TOKEN:
        return
    user_dict = {"id": 4242, "first_name": "Test", "username": "tguser"}
    auth_date = int(time.time())

    params = {
        "auth_date": str(auth_date),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(user_dict)
    }

    # Generate valid signature
    check_items = [f"{k}={v}" for k, v in sorted(params.items())]
    data_check_string = "\n".join(check_items)
    secret_key = hmac.new(b"WebAppData", TOKEN.encode("utf-8"), hashlib.sha256).digest()
    sig_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    init_data = f"auth_date={auth_date}&query_id=AAHdF6IQAAAAAN0XohDhrOrc&user={json.dumps(user_dict)}&hash={sig_hash}"
    verified = verify_telegram_init_data(init_data)
    assert verified is not None
    assert verified["user_id"] == 4242
    assert verified["username"] == "tguser"


def test_verify_telegram_init_data_invalid():
    # Missing hash
    assert verify_telegram_init_data("auth_date=12345&user={}") is None
    # Bad hash
    assert verify_telegram_init_data("auth_date=12345&user={}&hash=badhash") is None
    # Expired
    assert verify_telegram_init_data("auth_date=100000&user={}&hash=badhash", max_age=10) is None


def test_auth_token_username_with_colons():
    user_id = 98765
    username = "user:with:multiple:colons"
    token = generate_auth_token(user_id, username)
    info = verify_auth_token(token)
    assert info is not None
    assert info["user_id"] == user_id
    assert info["username"] == username

