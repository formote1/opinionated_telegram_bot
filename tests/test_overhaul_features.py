import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId

from config import ADMIN_ID
from handlers.common import web_command
from handlers.reminders import (
    edit_reminder_command,
    handle_reminder_callback,
    send_daily_reminder,
)
from server import flask_app
from utils.auth import (
    generate_auth_pin,
    generate_auth_token,
    verify_auth_pin,
)


@pytest.fixture
def client():
    flask_app.config["TESTING"] = True
    flask_app.config["SECRET_KEY"] = "test-secret-key-123"
    with flask_app.test_client() as c:
        yield c


def test_auth_pin_lifecycle():
    user_id = 445566
    username = "pinhodler"

    # 1. Generate PIN
    pin = generate_auth_pin(user_id, username, expires_in=600)
    assert len(pin) == 6
    assert pin.isdigit()

    # 2. Verify PIN
    res = verify_auth_pin(pin)
    assert res is not None
    assert res["user_id"] == user_id
    assert res["username"] == username

    # 3. One-time use: verify again should return None
    assert verify_auth_pin(pin) is None

    # 4. Expired PIN
    expired_pin = generate_auth_pin(user_id, username, expires_in=-1)
    assert verify_auth_pin(expired_pin) is None

    # 5. Invalid pins
    assert verify_auth_pin("invalid") is None
    assert verify_auth_pin("000000") is None
    assert verify_auth_pin("") is None


def test_bot_web_command_pin_and_link():
    update = MagicMock()
    user = MagicMock()
    user.id = 112233
    user.username = "alice"
    update.effective_user = user
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()

    context = MagicMock()

    with patch("handlers.common.save_user_info", new=AsyncMock()):
        asyncio.run(web_command(update, context))

    assert update.message.reply_text.awaited
    call_args = update.message.reply_text.call_args
    text = call_args[0][0] if call_args[0] else call_args[1].get("text", "")
    reply_markup = call_args[1].get("reply_markup")

    # Assert 6-digit PIN and copyable link are displayed in message text
    assert "One-Time Login PIN:" in text
    assert "Direct Web Link:" in text
    assert "<code>" in text

    # Assert inline buttons still have login link with token
    buttons = reply_markup.inline_keyboard
    assert any("/login?token=" in (btn.url or "") for row in buttons for btn in row)


def test_companion_auth_routes_with_pin(client):
    user_id = 998877
    username = "pinlogin_user"

    # 1. Successful /auth?pin=
    pin1 = generate_auth_pin(user_id, username)
    res1 = client.get(f"/auth?pin={pin1}", follow_redirects=False)
    assert res1.status_code == 302
    assert "auth=success" in res1.headers["Location"]
    assert "token=" in res1.headers["Location"]

    # 2. Invalid /auth?pin=
    res2 = client.get("/auth?pin=999999", follow_redirects=False)
    assert res2.status_code == 302
    assert "auth=invalid" in res2.headers["Location"]

    # 3. Successful /login?pin=
    pin2 = generate_auth_pin(user_id, username)
    res3 = client.get(f"/login?pin={pin2}", follow_redirects=False)
    assert res3.status_code == 302
    assert "auth=success" in res3.headers["Location"]


def test_api_auth_token_all_formats(client):
    user_id = 554433
    username = "multiauth_user"

    # Case A: {"pin": "123456"}
    pin_a = generate_auth_pin(user_id, username)
    res_a = client.post("/api/auth/token", json={"pin": pin_a})
    assert res_a.status_code == 200
    assert res_a.get_json()["success"] is True

    # Case B: {"token": "123456"} (6-digit PIN in token field)
    pin_b = generate_auth_pin(user_id, username)
    res_b = client.post("/api/auth/token", json={"token": pin_b})
    assert res_b.status_code == 200
    assert res_b.get_json()["success"] is True

    # Case C: {"token": "http://example.com/auth?pin=123456"} (Full URL with PIN)
    pin_c = generate_auth_pin(user_id, username)
    res_c = client.post("/api/auth/token", json={"token": f"https://example.com/auth?pin={pin_c}"})
    assert res_c.status_code == 200
    assert res_c.get_json()["success"] is True

    # Case D: {"token": "http://example.com/login?token=..."} (Full URL with token)
    raw_tok = generate_auth_token(user_id, username)
    res_d = client.post("/api/auth/token", json={"token": f"https://example.com/login?token={raw_tok}"})
    assert res_d.status_code == 200
    assert res_d.get_json()["success"] is True

    # Case E: {"token": raw_token}
    raw_tok2 = generate_auth_token(user_id, username)
    res_e = client.post("/api/auth/token", json={"token": raw_tok2})
    assert res_e.status_code == 200
    assert res_e.get_json()["success"] is True


def test_api_status_includes_bot_username(client):
    res = client.get("/api/status")
    assert res.status_code == 200
    data = res.get_json()
    assert "bot_username" in data


def test_api_reminders_put_update(client):
    user_id = 121212
    token = generate_auth_token(user_id, "reminder_updater")
    headers = {"Authorization": f"Bearer {token}"}

    rem_id = ObjectId()
    existing_doc = {
        "_id": rem_id,
        "user_id": user_id,
        "label": "Old Birthday",
        "target_date": "2026-11-01",
        "reminder_time": "09:00",
        "category": "Birthday",
        "recurrence": "annually",
    }

    with patch("server.reminders_col") as mock_col:
        mock_col.find_one = AsyncMock(return_value=existing_doc)
        mock_col.update_one = AsyncMock(return_value=MagicMock(matched_count=1))

        # 1. Successful PUT
        update_payload = {
            "label": "Updated Birthday Party",
            "target_date": "2026-11-05",
            "reminder_time": "18:00",
            "category": "Event",
            "recurrence": "once",
        }
        res = client.put(f"/api/reminders/{rem_id}", headers=headers, json=update_payload)
        assert res.status_code == 200
        assert res.get_json()["success"] is True
        mock_col.update_one.assert_awaited_once()

        # 2. Validation error: empty label or invalid date
        res_bad1 = client.put(f"/api/reminders/{rem_id}", headers=headers, json={"label": ""})
        assert res_bad1.status_code == 400

        res_bad2 = client.put(f"/api/reminders/{rem_id}", headers=headers, json={"target_date": "invalid-date"})
        assert res_bad2.status_code == 400

        # 3. Non-existent reminder
        mock_col.find_one = AsyncMock(return_value=None)
        res_nf = client.put(f"/api/reminders/{rem_id}", headers=headers, json=update_payload)
        assert res_nf.status_code == 404


def test_api_admin_endpoints(client):
    admin_uid = ADMIN_ID if ADMIN_ID != 0 else 88888888
    admin_token = generate_auth_token(admin_uid, "boss_admin")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    regular_token = generate_auth_token(12345, "regular_joe")
    regular_headers = {"Authorization": f"Bearer {regular_token}"}

    with patch("server.ADMIN_ID", admin_uid), \
         patch("server.group_keys_col") as mock_keys, \
         patch("server.codes_col") as mock_codes, \
         patch("server.prefix_labels_col") as mock_labels, \
         patch("server.log_event", new=AsyncMock()):

        mock_keys.update_one = AsyncMock()
        mock_keys.update_many = AsyncMock()
        mock_keys.delete_one = AsyncMock()
        mock_keys.delete_many = AsyncMock()
        mock_keys.find_one = AsyncMock(return_value={"prefix": "LOK", "secret_key": "oldpass"})
        mock_codes.find_one = AsyncMock(return_value={"chat_id": 12345})
        mock_codes.update_many = AsyncMock(return_value=MagicMock(modified_count=5))
        mock_codes.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))
        mock_codes.delete_many = AsyncMock(return_value=MagicMock(deleted_count=10))
        mock_codes.bulk_write = AsyncMock()
        mock_labels.find_one = AsyncMock(return_value={"prefix": "LOK", "banner_type": "poster", "seasons": [{"num": 1, "start": 1, "end": 6}]})
        mock_labels.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
        mock_labels.update_many = AsyncMock(return_value=MagicMock(modified_count=1))
        mock_labels.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))
        mock_labels.delete_many = AsyncMock()

        class AsyncCursor:
            def __aiter__(self):
                return self._gen()
            async def _gen(self):
                if False:
                    yield None

        mock_labels.find.side_effect = lambda *a, **kw: AsyncCursor()
        mock_codes.find.side_effect = lambda *a, **kw: AsyncCursor()

        # Non-admin rejected with 403 on all admin routes
        res_403 = client.post("/api/admin/file/lock", headers=regular_headers, json={"prefix": "LOK", "password": "abc"})
        assert res_403.status_code == 403

        # 1. Lock partition
        res_lock = client.post("/api/admin/file/lock", headers=admin_headers, json={"prefix": "LOK", "password": "newpass"})
        assert res_lock.status_code == 200
        assert res_lock.get_json()["success"] is True

        # 2. Unlock partition
        res_unlock = client.post("/api/admin/file/unlock", headers=admin_headers, json={"prefix": "LOK"})
        assert res_unlock.status_code == 200
        assert res_unlock.get_json()["success"] is True

        # 3. Change passkey
        res_rot = client.post("/api/admin/file/change_passkey", headers=admin_headers, json={"prefix": "LOK", "old_passkey": "oldpass", "new_passkey": "freshpass"})
        assert res_rot.status_code == 200
        assert res_rot.get_json()["success"] is True

        # 4. Move prefix
        res_mv = client.post("/api/admin/file/mv", headers=admin_headers, json={"old_prefix": "LOK", "new_prefix": "NEW"})
        assert res_mv.status_code == 200
        assert res_mv.get_json()["success"] is True

        # 5. Delete single file
        res_del1 = client.post("/api/admin/file/del", headers=admin_headers, json={"mode": "single", "code": "LOK001"})
        assert res_del1.status_code == 200
        assert res_del1.get_json()["success"] is True

        # 6. Delete range
        res_del2 = client.post("/api/admin/file/del", headers=admin_headers, json={"mode": "range", "prefix": "LOK", "start": 1, "end": 10})
        assert res_del2.status_code == 200
        assert res_del2.get_json()["success"] is True

        # 7. Media label set & del
        res_lbl = client.post("/api/admin/media/label/set", headers=admin_headers, json={"prefix": "LOK", "name": "Loki", "category": "series"})
        assert res_lbl.status_code == 200
        assert res_lbl.get_json()["success"] is True

        res_lbldel = client.post("/api/admin/media/label/del", headers=admin_headers, json={"prefix": "LOK"})
        assert res_lbldel.status_code == 200
        assert res_lbldel.get_json()["success"] is True

        # 8. TMDB set & del
        with patch("services.tmdb.fetch_tmdb_metadata", new=AsyncMock(return_value={"id": 12345, "title": "Loki"})):
            res_tmdb = client.post("/api/admin/media/tmdb/set", headers=admin_headers, json={"prefix": "LOK", "query": "Loki"})
            assert res_tmdb.status_code == 200
            assert res_tmdb.get_json()["success"] is True

        res_tmdbdel = client.post("/api/admin/media/tmdb/del", headers=admin_headers, json={"prefix": "LOK"})
        assert res_tmdbdel.status_code == 200
        assert res_tmdbdel.get_json()["success"] is True

        # 9. Banner toggle, set, reset
        mock_labels.find_one = AsyncMock(return_value={"prefix": "LOK", "banner_type": "poster", "seasons": [{"num": 1, "start": 1, "end": 6}]})
        res_ban_tog = client.post("/api/admin/media/banner", headers=admin_headers, json={"prefix": "LOK", "action": "toggle"})
        assert res_ban_tog.status_code == 200
        assert res_ban_tog.get_json()["success"] is True

        res_ban_set = client.post("/api/admin/media/banner", headers=admin_headers, json={"prefix": "LOK", "action": "set", "banner_url": "https://img.com/p.jpg"})
        assert res_ban_set.status_code == 200

        res_ban_rst = client.post("/api/admin/media/banner", headers=admin_headers, json={"prefix": "LOK", "action": "reset"})
        assert res_ban_rst.status_code == 200

        # 10. Seasons set, del, clear
        res_sn_set = client.post("/api/admin/media/season/set", headers=admin_headers, json={"prefix": "LOK", "season_num": 1, "start_ep": 1, "end_ep": 6})
        assert res_sn_set.status_code == 200
        assert res_sn_set.get_json()["success"] is True

        res_sn_del = client.post("/api/admin/media/season/del", headers=admin_headers, json={"prefix": "LOK", "season_num": 1})
        assert res_sn_del.status_code == 200

        res_sn_clr = client.post("/api/admin/media/season/clear", headers=admin_headers, json={"prefix": "LOK"})
        assert res_sn_clr.status_code == 200

        # 11. Refresh metadata trigger
        res_ref = client.post("/api/admin/media/refresh", headers=admin_headers)
        assert res_ref.status_code == 200
        assert res_ref.get_json()["success"] is True


@pytest.mark.asyncio
async def test_send_daily_reminder_birthday_omits_done_button():
    user_id = 990011
    bday_reminder = {
        "_id": ObjectId(),
        "user_id": user_id,
        "label": "Mom's Birthday",
        "category": "Birthday",
        "target_date": date.today().isoformat(),
        "recurrence": "annually",
    }

    context = MagicMock()
    context.bot.send_message = AsyncMock()
    context.job.data = {"user_id": user_id, "timezone": "UTC"}

    with patch("handlers.reminders.reminders_col") as mock_col, \
         patch("handlers.reminders._active_reminder_dispatches", set()):
        mock_cursor = MagicMock()
        mock_cursor.to_list = AsyncMock(return_value=[bday_reminder])
        mock_col.find.return_value = mock_cursor
        mock_col.update_one = AsyncMock(return_value=MagicMock(modified_count=1))

        # Dispatch birthday reminder
        await send_daily_reminder(context)
        assert context.bot.send_message.awaited
        call_args = context.bot.send_message.call_args
        markup = call_args[1].get("reply_markup")
        assert markup is not None
        # Check that Done button is omitted
        buttons = markup.inline_keyboard
        button_texts = [btn.text for row in buttons for btn in row]
        callback_data = [btn.callback_data for row in buttons for btn in row]
        assert not any("Done" in text for text in button_texts)
        assert not any("remdone_" in cb for cb in callback_data)
        # Snooze button should still be present
        assert any("Snooze" in text for text in button_texts)

        # Now dispatch non-birthday reminder (e.g. Deadline)
        context.bot.send_message.reset_mock()
        event_reminder = {
            "_id": ObjectId(),
            "user_id": user_id,
            "label": "Project Deadline",
            "category": "Deadline",
            "target_date": date.today().isoformat(),
            "recurrence": "once",
        }
        mock_cursor.to_list = AsyncMock(return_value=[event_reminder])
        await send_daily_reminder(context)
        call_args_event = context.bot.send_message.call_args
        markup_event = call_args_event[1].get("reply_markup")
        buttons_event = markup_event.inline_keyboard
        button_texts_event = [btn.text for row in buttons_event for btn in row]
        assert any("Done" in text for text in button_texts_event)


@pytest.mark.asyncio
async def test_bot_editrem_command_and_callbacks():
    update = MagicMock()
    user = MagicMock()
    user.id = 776655
    update.effective_user = user
    update.effective_message.reply_text = AsyncMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()

    rem_id = ObjectId()
    sample_doc = {
        "_id": rem_id,
        "user_id": 776655,
        "label": "Dentist Appointment",
        "target_date": "2026-12-15",
        "reminder_time": "14:00",
        "category": "General",
        "recurrence": "once",
    }

    with patch("handlers.reminders.reminders_col") as mock_col:
        mock_col.find_one = AsyncMock(return_value=sample_doc)
        mock_col.update_one = AsyncMock()
        mock_cursor = MagicMock()
        mock_cursor.to_list = AsyncMock(return_value=[sample_doc])
        mock_col.find.return_value.sort.return_value = mock_cursor

        # 1. /editrem without arguments lists reminders with edit buttons
        context.args = []
        await edit_reminder_command(update, context)
        assert update.effective_message.reply_text.awaited

        # 2. /editrem <id> displays edit menu
        update.effective_message.reply_text.reset_mock()
        context.args = [str(rem_id)]
        await edit_reminder_command(update, context)
        assert update.effective_message.reply_text.awaited
        assert "CUSTOMIZE REMINDER" in update.effective_message.reply_text.call_args[0][0]

        # 3. /editrem <id> label <new_label> updates directly
        update.effective_message.reply_text.reset_mock()
        context.args = [str(rem_id), "label", "Orthodontist Appointment"]
        await edit_reminder_command(update, context)
        mock_col.update_one.assert_awaited()
        assert "Reminder Updated" in update.effective_message.reply_text.call_args[0][0]

        # 4. Callback query editrem_
        query = MagicMock()
        query.from_user = user
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update.callback_query = query

        query.data = f"editrem_{rem_id}"
        await handle_reminder_callback(update, context)
        assert query.edit_message_text.awaited

        # 5. Callback query edfield_ (category picker)
        query.edit_message_text.reset_mock()
        query.data = f"edfield_{rem_id}_cat"
        await handle_reminder_callback(update, context)
        assert "Select New Category" in query.edit_message_text.call_args[0][0]

        # 6. Callback query edset_ (set category to Birthday)
        query.edit_message_text.reset_mock()
        query.data = f"edset_{rem_id}_cat_Birthday"
        await handle_reminder_callback(update, context)
        assert query.edit_message_text.awaited


@pytest.mark.asyncio
async def test_bot_editrem_edge_cases():
    update = MagicMock()
    user = MagicMock()
    user.id = 776655
    update.effective_user = user
    update.effective_message.reply_text = AsyncMock()
    context = MagicMock()

    rem_id = ObjectId()
    sample_doc = {
        "_id": rem_id,
        "user_id": 776655,
        "label": "Old Dentist",
        "target_date": "2026-12-15",
        "reminder_time": "14:00",
        "category": "General",
        "recurrence": "once",
    }

    with patch("handlers.reminders.reminders_col") as mock_col:
        mock_col.find_one = AsyncMock(return_value=sample_doc)
        mock_col.update_one = AsyncMock()

        # 1. Non-integer time format should return clean error, not raise ValueError
        update.effective_message.reply_text.reset_mock()
        context.args = [str(rem_id), "time", "ab:cd"]
        await edit_reminder_command(update, context)
        assert update.effective_message.reply_text.awaited
        assert "Invalid time format" in update.effective_message.reply_text.call_args[0][0]

        # 2. Out of bounds time format
        update.effective_message.reply_text.reset_mock()
        context.args = [str(rem_id), "time", "25:99"]
        await edit_reminder_command(update, context)
        assert "Invalid time format" in update.effective_message.reply_text.call_args[0][0]

        # 3. Setting category to Birthday should auto-set recurrence to annually when once
        update.effective_message.reply_text.reset_mock()
        context.args = [str(rem_id), "category", "Birthday"]
        await edit_reminder_command(update, context)
        mock_col.update_one.assert_awaited()
        update_call = mock_col.update_one.call_args[0][1]["$set"]
        assert update_call.get("category") == "Birthday"
        assert update_call.get("recurrence") == "annually"


@pytest.mark.asyncio
async def test_daily_reminder_birthday_label_omits_done_even_with_other_category():
    user_id = 990022
    # Reminder labeled as birthday, but category was set to General
    bday_by_label = {
        "_id": ObjectId(),
        "user_id": user_id,
        "label": "Sister's 25th Birthday Party 🎂",
        "category": "General",
        "target_date": date.today().isoformat(),
        "recurrence": "once",
    }

    context = MagicMock()
    context.bot.send_message = AsyncMock()
    context.job.data = {"user_id": user_id, "timezone": "UTC"}

    with patch("handlers.reminders.reminders_col") as mock_col, \
         patch("handlers.reminders._active_reminder_dispatches", set()):
        mock_cursor = MagicMock()
        mock_cursor.to_list = AsyncMock(return_value=[bday_by_label])
        mock_col.find.return_value = mock_cursor
        mock_col.update_one = AsyncMock(return_value=MagicMock(modified_count=1))

        await send_daily_reminder(context)
        assert context.bot.send_message.awaited
        call_args = context.bot.send_message.call_args
        markup = call_args[1].get("reply_markup")
        buttons = markup.inline_keyboard
        button_texts = [btn.text for row in buttons for btn in row]
        callback_data = [btn.callback_data for row in buttons for btn in row]

        # Assert no Done button appears anywhere
        assert not any("Done" in text for text in button_texts)
        assert not any("remdone_" in cb for cb in callback_data)
        # Snooze button should still be available
        assert any("Snooze" in text for text in button_texts)


def test_api_auth_token_pasted_strings_and_case_insensitive(client):
    user_id = 665544
    username = "token_tester"

    # 1. Pasted "token=..." raw query string
    token = generate_auth_token(user_id, username)
    res1 = client.post("/api/auth/token", json={"token": f"token={token}"})
    assert res1.status_code == 200
    assert res1.get_json()["success"] is True

    # 2. Pasted "pin=123456" raw query string
    pin = generate_auth_pin(user_id, username)
    res2 = client.post("/api/auth/token", json={"token": f"pin={pin}"})
    assert res2.status_code == 200
    assert res2.get_json()["success"] is True

    # 3. Pasted Telegram message text containing PIN
    pin2 = generate_auth_pin(user_id, username)
    msg_text = f"🔑 One-Time Login PIN: {pin2}\n(Valid for 10 minutes)"
    res3 = client.post("/api/auth/token", json={"token": msg_text})
    assert res3.status_code == 200
    assert res3.get_json()["success"] is True

    # 4. Case-insensitive prefix search in media item endpoint
    with patch("server.prefix_labels_col") as mock_labels, \
         patch("server.codes_col") as mock_codes, \
         patch("server.group_keys_col", None):
        mock_labels.find_one = AsyncMock(return_value=None)
        # File has lowercase code 'bat001'
        mock_codes.find_one = AsyncMock(return_value={"code": "bat001", "chat_id": 111})
        mock_cursor = MagicMock()
        mock_cursor.sort.return_value.to_list = AsyncMock(return_value=[{"code": "bat001", "file_name": "Batman Begins"}])
        mock_codes.find.return_value = mock_cursor

        res_item = client.get("/api/media/item/BAT")
        assert res_item.status_code == 200
        assert res_item.get_json()["success"] is True
        assert res_item.get_json()["item"]["prefix"] == "BAT"

