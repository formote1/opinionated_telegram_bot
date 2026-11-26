import base64
import logging
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId

from handlers.reminders import send_daily_reminder
from server import flask_app
from utils.auth import generate_auth_token, save_web_session
from utils.privacy import is_public_id, public_id
from utils.sentinel import log_event, notify_admin


def test_public_id_is_stable_non_reversible_and_has_no_username():
    alias = public_id(123456789)
    assert is_public_id(alias)
    assert alias == public_id(123456789)
    assert alias != public_id(123456790)
    assert "123456789" not in alias
    assert "username" not in alias.lower()


@pytest.mark.asyncio
async def test_log_event_does_not_print_user_identifiers(caplog):
    with caplog.at_level(logging.DEBUG):
        await log_event(7777777, "secret_user", "banned someone")
    assert "7777777" not in caplog.text
    assert "secret_user" not in caplog.text
    assert "banned" not in caplog.text


@pytest.mark.asyncio
async def test_notify_admin_drops_user_linked_text_but_allows_anonymous_system_event():
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    with patch("utils.sentinel.ADMIN_ID", 99999):
        await notify_admin(context, "Sent to user 1234567 with label Mom")
        context.bot.send_message.assert_not_awaited()

        await notify_admin(context, "Monthly aggregate report ready")
        context.bot.send_message.assert_awaited_once()
        assert context.bot.send_message.await_args.kwargs["chat_id"] == 99999


@pytest.mark.asyncio
async def test_daily_reminder_delivery_never_sends_to_admin():
    user_id = 424242
    tomorrow = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
    record = {
        "_id": ObjectId(),
        "user_id": user_id,
        "label": "Mom",
        "category": "Birthday",
        "target_date": tomorrow,
        "recurrence": "annually",
        "last_notified": "",
    }
    context = MagicMock()
    context.job.data = {"user_id": user_id, "timezone": "UTC", "reminder_time": "09:00"}
    context.bot.send_message = AsyncMock()

    with patch("handlers.reminders.reminders_col") as col, patch("utils.sentinel.ADMIN_ID", 99999):
        col.find.return_value.to_list = AsyncMock(return_value=[record])
        col.update_one = AsyncMock()
        await send_daily_reminder(context)

    assert context.bot.send_message.await_count >= 1
    for call in context.bot.send_message.await_args_list:
        assert call.kwargs["chat_id"] == user_id


def test_auth_token_payload_contains_no_username():
    token = generate_auth_token(555, "secret_username", expires_in=3600)
    payload = base64.urlsafe_b64decode(token.split(".", 1)[0]).decode("utf-8")
    assert "secret_username" not in payload
    assert "username" not in payload


@pytest.mark.asyncio
async def test_web_session_persists_no_username_or_raw_token():
    with patch("database.web_sessions_col") as col:
        col.update_one = AsyncMock()
        data = await save_web_session("bearer-token", 555, "secret_username")
        stored = col.update_one.call_args[0][1]["$set"]
        assert "username" not in data
        assert "username" not in stored
        assert "token" not in stored
        assert "token_hash" in stored


def test_admin_access_api_exposes_pseudonyms_only():
    with flask_app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_id"] = 99999
            sess["is_admin"] = True

        with patch("server.ADMIN_ID", 99999), \
             patch("server.unlocked_groups_col") as col:
            col.find.return_value.to_list = AsyncMock(return_value=[{
                "user_id": 7777777,
                "unlocked_prefixes": ["123_ABC"],
                "frozen_prefixes": [],
                "prefix_expirations": {},
            }])
            res = client.get("/api/admin/access")

        assert res.status_code == 200
        entry = res.get_json()["access"][0]
        assert "user_id" not in entry
        assert "username" not in entry
        assert entry["public_id"] == public_id(7777777)
