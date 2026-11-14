from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId

from handlers.media import core_routing_manager
from handlers.reminders import (
    build_reminder_card_text,
    build_reminder_edit_markup,
    calculate_upcoming_age,
    edit_reminder_command,
    handle_pending_birth_date_input,
    handle_reminder_callback,
    list_reminders,
    resolve_reminder_id,
    send_daily_reminder,
)
from server import flask_app
from utils.auth import generate_auth_token


@pytest.fixture
def client():
    flask_app.config["TESTING"] = True
    flask_app.config["SECRET_KEY"] = "test-secret-key-123"
    with flask_app.test_client() as c:
        yield c


def test_calculate_upcoming_age():
    # 1. From birth_date
    rem1 = {"birth_date": "2001-05-14", "target_date": "2026-11-15"}
    assert calculate_upcoming_age(rem1) == 25

    # 2. From birth_year int
    rem2 = {"birth_year": 2000, "target_date": "2026-01-01"}
    assert calculate_upcoming_age(rem2) == 26

    # 3. From birth_year string
    rem3 = {"birth_year": "1990", "target_date": "2026-06-20"}
    assert calculate_upcoming_age(rem3) == 36

    # 4. Target date in future year
    rem4 = {"birth_date": "2000-01-01", "target_date": "2030-01-01"}
    assert calculate_upcoming_age(rem4) == 30

    # 5. Invalid / missing birth info returns None
    assert calculate_upcoming_age({}) is None
    assert calculate_upcoming_age({"birth_date": "invalid"}) is None
    assert calculate_upcoming_age({"birth_year": "invalid"}) is None
    assert calculate_upcoming_age("not a dict") is None

    # 6. Unborn / target_year < birth_year returns None
    assert calculate_upcoming_age({"birth_year": 2050, "target_date": "2026-01-01"}) is None


@pytest.mark.asyncio
async def test_list_reminders_sleek_layout_and_number_grid():
    update = MagicMock()
    user = MagicMock()
    user.id = 12345
    update.effective_user = user
    update.effective_message.reply_text = AsyncMock()
    context = MagicMock()

    id1, id2, id3 = ObjectId(), ObjectId(), ObjectId()
    reminders = [
        {
            "_id": id1,
            "user_id": 12345,
            "label": "Alice",
            "target_date": "2026-11-15",
            "reminder_time": "09:00",
            "category": "Birthday",
            "recurrence": "annually",
            "birth_date": "2001-11-15",
            "is_spotlight": True,
            "timezone": "UTC",
        },
        {
            "_id": id2,
            "user_id": 12345,
            "label": "Flight to Tokyo",
            "target_date": "2026-12-01",
            "reminder_time": "14:30",
            "category": "Travel",
            "recurrence": "once",
            "timezone": "UTC",
        },
        {
            "_id": id3,
            "user_id": 12345,
            "label": "Bob",
            "target_date": "2026-12-10",
            "reminder_time": "10:00",
            "category": "Birthday",
            "recurrence": "annually",
            "birth_year": 1996,
            "is_spotlight": False,
            "timezone": "UTC",
        },
    ]

    with patch("handlers.reminders.reminders_col") as mock_col:
        mock_cursor = MagicMock()
        mock_cursor.to_list = AsyncMock(return_value=reminders)
        mock_col.find.return_value.sort.return_value = mock_cursor

        await list_reminders(update, context)
        assert update.effective_message.reply_text.awaited
        args, kwargs = update.effective_message.reply_text.call_args
        text = args[0]
        markup = kwargs.get("reply_markup")

        # Verify sleek typography and label before date
        assert "<b>Reminders</b>" in text
        assert "─────" not in text
        assert "📜" not in text
        assert "Alice — 2026-11-15" in text
        assert "Turning 25" in text
        assert "⭐" in text
        assert "Flight to Tokyo — 2026-12-01" in text

        # Verify compact number button grid (1 row of 3 buttons: 1, 2, 3)
        assert markup is not None
        rows = markup.inline_keyboard
        assert len(rows) == 1
        assert len(rows[0]) == 3
        assert rows[0][0].text == "1"
        assert rows[0][0].callback_data == f"editrem_{id1}"
        assert rows[0][1].text == "2"
        assert rows[0][1].callback_data == f"editrem_{id2}"
        assert rows[0][2].text == "3"
        assert rows[0][2].callback_data == f"editrem_{id3}"


@pytest.mark.asyncio
async def test_interactive_edit_card_and_buttons():
    rem_id = ObjectId()
    rec = {
        "_id": rem_id,
        "user_id": 999,
        "label": "Charlie",
        "category": "Birthday",
        "target_date": "2026-08-20",
        "reminder_time": "09:00",
        "recurrence": "annually",
        "birth_date": "1996-08-20",
        "is_spotlight": True,
    }

    card_text = build_reminder_card_text(rec)
    assert "<b>Label:</b> Charlie" in card_text
    assert "<b>Category:</b> Birthday" in card_text
    assert "<b>Birth Date:</b> <code>1996-08-20</code> (Turning 30)" in card_text
    assert "<b>Spotlight:</b> Active ⭐" in card_text

    # Spotlight on markup
    markup_spot = build_reminder_edit_markup(rec)
    flat_btns = [btn for row in markup_spot.inline_keyboard for btn in row]
    flat_texts = [b.text for b in flat_btns]
    flat_cbs = [b.callback_data for b in flat_btns]

    assert "✕ Remove Spotlight" in flat_texts
    assert f"edfield_{rem_id}_spotlight_off" in flat_cbs
    assert "🎂 Set Birth Date" in flat_texts
    assert "✏️ Edit Details" in flat_texts
    assert "🗑️ Delete Reminder" in flat_texts
    assert "🔙 Back to List" in flat_texts

    # Spotlight off markup
    rec_off = dict(rec)
    rec_off["is_spotlight"] = False
    markup_off = build_reminder_edit_markup(rec_off)
    flat_texts_off = [btn.text for row in markup_off.inline_keyboard for btn in row]
    assert "⭐ Spotlight" in flat_texts_off


@pytest.mark.asyncio
async def test_spotlight_callback_atomic_toggle():
    update = MagicMock()
    user = MagicMock()
    user.id = 888
    update.effective_user = user
    query = MagicMock()
    query.from_user = user
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update.callback_query = query
    context = MagicMock()

    rem_id = ObjectId()
    rec = {
        "_id": rem_id,
        "user_id": 888,
        "label": "Mom",
        "category": "Birthday",
        "target_date": "2026-05-10",
        "is_spotlight": False,
    }

    with patch("handlers.reminders.reminders_col") as mock_col:
        mock_col.find_one = AsyncMock(return_value=rec)
        mock_col.update_many = AsyncMock()
        mock_col.update_one = AsyncMock()

        # Turn spotlight ON
        query.data = f"edfield_{rem_id}_spotlight_on"
        await handle_reminder_callback(update, context)

        # Assert update_many unset previous spotlights for this user
        mock_col.update_many.assert_awaited_once_with({"user_id": 888, "is_spotlight": True}, {"$set": {"is_spotlight": False}})
        mock_col.update_one.assert_awaited_once_with({"_id": rem_id, "user_id": 888}, {"$set": {"is_spotlight": True}})
        assert query.edit_message_text.awaited

        # Turn spotlight OFF
        mock_col.update_many.reset_mock()
        mock_col.update_one.reset_mock()
        query.data = f"edfield_{rem_id}_spotlight_off"
        await handle_reminder_callback(update, context)
        mock_col.update_one.assert_awaited_once_with({"_id": rem_id, "user_id": 888}, {"$set": {"is_spotlight": False}})


@pytest.mark.asyncio
async def test_birthdate_and_spotlight_text_commands():
    update = MagicMock()
    user = MagicMock()
    user.id = 777
    update.effective_user = user
    update.effective_message.reply_text = AsyncMock()
    context = MagicMock()

    rem_id = ObjectId()
    sample_rec = {
        "_id": rem_id,
        "user_id": 777,
        "label": "Friend",
        "category": "Birthday",
        "target_date": "2026-07-04",
        "reminder_time": "09:00",
        "recurrence": "annually",
        "is_spotlight": False,
    }

    with patch("handlers.reminders.reminders_col") as mock_col:
        mock_col.find_one = AsyncMock(return_value=sample_rec)
        mock_col.update_one = AsyncMock()
        mock_col.update_many = AsyncMock()

        # 1. /editrem <id> birthdate 2002-07-04
        context.args = [str(rem_id), "birthdate", "2002-07-04"]
        await edit_reminder_command(update, context)
        mock_col.update_one.assert_awaited()
        call_set = mock_col.update_one.call_args[0][1]["$set"]
        assert call_set["birth_date"] == "2002-07-04"
        assert call_set["birth_year"] == 2002

        # 2. /editrem <id> birthdate invalid
        update.effective_message.reply_text.reset_mock()
        context.args = [str(rem_id), "birthdate", "not-a-date"]
        await edit_reminder_command(update, context)
        assert "Invalid birth date" in update.effective_message.reply_text.call_args[0][0]

        # 3. /editrem <id> birthyear 1999
        context.args = [str(rem_id), "birthyear", "1999"]
        await edit_reminder_command(update, context)
        call_set_yr = mock_col.update_one.call_args[0][1]["$set"]
        assert call_set_yr["birth_year"] == 1999

        # 4. /editrem <id> spotlight (toggle)
        update.effective_message.reply_text.reset_mock()
        context.args = [str(rem_id), "spotlight"]
        await edit_reminder_command(update, context)
        assert "Spotlight Updated" in update.effective_message.reply_text.call_args[0][0]
        mock_col.update_many.assert_awaited_with({"user_id": 777, "is_spotlight": True}, {"$set": {"is_spotlight": False}})


@pytest.mark.asyncio
async def test_send_daily_reminder_spotlight_expandable_blockquote():
    user_id = 55512
    today_str = date.today().isoformat()

    alice = {
        "_id": ObjectId(),
        "user_id": user_id,
        "label": "Alice",
        "category": "Birthday",
        "target_date": today_str,
        "birth_date": "2001-11-15",
        "is_spotlight": True,
    }
    bob = {
        "_id": ObjectId(),
        "user_id": user_id,
        "label": "Bob",
        "category": "Birthday",
        "target_date": today_str,
        "birth_year": 1995,
        "is_spotlight": False,
    }
    charlie = {
        "_id": ObjectId(),
        "user_id": user_id,
        "label": "Charlie",
        "category": "Birthday",
        "target_date": today_str,
        "is_spotlight": False,
    }

    context = MagicMock()
    context.bot.send_message = AsyncMock()
    context.job.data = {"user_id": user_id, "timezone": "UTC"}

    with patch("handlers.reminders.reminders_col") as mock_col, \
         patch("handlers.reminders._active_reminder_dispatches", set()):
        mock_cursor = MagicMock()
        mock_cursor.to_list = AsyncMock(return_value=[alice, bob, charlie])
        mock_col.find.return_value = mock_cursor
        mock_col.update_one = AsyncMock(return_value=MagicMock(modified_count=1))

        await send_daily_reminder(context)
        assert context.bot.send_message.awaited
        args, kwargs = context.bot.send_message.call_args
        text = kwargs.get("text", "")
        markup = kwargs.get("reply_markup")

        # Zero buttons for birthdays
        assert markup is None

        # Alice is spotlighted, so she appears first with ⭐ and outside the blockquote
        assert "⭐ <b>Alice</b>" in text
        assert "Turning" in text

        # Bob and Charlie are grouped inside expandable blockquote
        assert "<blockquote expandable>" in text
        assert "<b>Bob</b>" in text
        assert "<b>Charlie</b>" in text
        assert "</blockquote>" in text


def test_companion_api_birthdate_and_spotlight(client):
    user_id = 44433
    token = generate_auth_token(user_id, "spotlight_tester")
    headers = {"Authorization": f"Bearer {token}"}

    rem_id = ObjectId()
    doc = {
        "_id": rem_id,
        "user_id": user_id,
        "label": "Diana",
        "category": "Birthday",
        "target_date": "2030-04-10",
        "reminder_time": "09:00",
        "recurrence": "annually",
        "birth_date": "2000-04-10",
        "birth_year": 2000,
        "is_spotlight": True,
        "timezone": "UTC",
    }

    with patch("server.reminders_col") as mock_col:
        # 1. GET /api/reminders returns age and spotlight
        mock_cursor = MagicMock()
        mock_cursor.to_list = AsyncMock(return_value=[doc])
        mock_col.find.return_value.sort.return_value = mock_cursor

        res_get = client.get("/api/reminders", headers=headers)
        assert res_get.status_code == 200
        items = res_get.get_json()["reminders"]
        assert len(items) == 1
        assert items[0]["is_spotlight"] is True
        assert items[0]["age"] == 30

        # 2. POST /api/reminders with birth_date & is_spotlight
        mock_col.insert_one = AsyncMock(return_value=MagicMock(inserted_id=ObjectId()))
        mock_col.update_many = AsyncMock()

        res_post = client.post("/api/reminders", headers=headers, json={
            "label": "Eva",
            "target_date": "2030-05-15",
            "category": "Birthday",
            "birth_date": "2005-05-15",
            "is_spotlight": True,
        })
        assert res_post.status_code == 200
        mock_col.update_many.assert_awaited_with({"user_id": user_id, "is_spotlight": True}, {"$set": {"is_spotlight": False}})
        created = res_post.get_json()["reminder"]
        assert created["birth_date"] == "2005-05-15"
        assert created["is_spotlight"] is True
        assert created["age"] == 25

        # 3. PUT /api/reminders/<id> updates birth_date and is_spotlight
        mock_col.find_one = AsyncMock(return_value=dict(doc))
        mock_col.update_one = AsyncMock()

        res_put = client.put(f"/api/reminders/{rem_id}", headers=headers, json={
            "birth_date": "1998-04-10",
            "is_spotlight": False,
        })
        assert res_put.status_code == 200
        assert res_put.get_json()["success"] is True
        mock_col.update_one.assert_awaited()


@pytest.mark.asyncio
async def test_list_reminders_empty_and_grid_wrapping():
    update = MagicMock()
    user = MagicMock()
    user.id = 1111
    update.effective_user = user
    update.effective_message.reply_text = AsyncMock()
    context = MagicMock()

    with patch("handlers.reminders.reminders_col") as mock_col:
        # 1. Zero reminders
        mock_cursor = MagicMock()
        mock_cursor.to_list = AsyncMock(return_value=[])
        mock_col.find.return_value.sort.return_value = mock_cursor

        await list_reminders(update, context)
        assert "No active reminders." in update.effective_message.reply_text.call_args[0][0]

        # 2. 12 reminders wrapped into 5-per-row grid
        twelve_reminders = [
            {
                "_id": ObjectId(),
                "user_id": 1111,
                "label": f"Reminder {i}",
                "target_date": "2026-11-20",
                "reminder_time": "09:00",
                "category": "General",
                "recurrence": "once",
            }
            for i in range(1, 13)
        ]
        mock_cursor.to_list = AsyncMock(return_value=twelve_reminders)
        update.effective_message.reply_text.reset_mock()

        await list_reminders(update, context)
        args, kwargs = update.effective_message.reply_text.call_args
        markup = kwargs["reply_markup"]
        rows = markup.inline_keyboard

        # 12 buttons should produce 3 rows: [5, 5, 2]
        assert len(rows) == 3
        assert len(rows[0]) == 5
        assert len(rows[1]) == 5
        assert len(rows[2]) == 2
        assert rows[0][0].text == "1"
        assert rows[1][0].text == "6"
        assert rows[2][1].text == "12"


@pytest.mark.asyncio
async def test_send_daily_reminder_birthday_without_spotlight():
    user_id = 2222
    today_str = date.today().isoformat()

    b1 = {
        "_id": ObjectId(),
        "user_id": user_id,
        "label": "Sam",
        "category": "Birthday",
        "target_date": today_str,
        "is_spotlight": False,
    }
    b2 = {
        "_id": ObjectId(),
        "user_id": user_id,
        "label": "Max",
        "category": "Birthday",
        "target_date": today_str,
        "birth_date": "1990-01-01",
        "is_spotlight": False,
    }

    context = MagicMock()
    context.bot.send_message = AsyncMock()
    context.job.data = {"user_id": user_id, "timezone": "UTC"}

    with patch("handlers.reminders.reminders_col") as mock_col, \
         patch("handlers.reminders._active_reminder_dispatches", set()):
        mock_cursor = MagicMock()
        mock_cursor.to_list = AsyncMock(return_value=[b1, b2])
        mock_col.find.return_value = mock_cursor
        mock_col.update_one = AsyncMock(return_value=MagicMock(modified_count=1))

        await send_daily_reminder(context)
        kwargs = context.bot.send_message.call_args[1]
        text = kwargs["text"]

        # No spotlight: no blockquote expandable wrapper
        assert "<blockquote" not in text
        assert "<b>Sam</b>" in text
        assert "<b>Max</b>" in text
        assert "Turning" in text
        assert kwargs["reply_markup"] is None


@pytest.mark.asyncio
async def test_edit_reminder_command_edge_cases():
    update = MagicMock()
    user = MagicMock()
    user.id = 3333
    update.effective_user = user
    update.effective_message.reply_text = AsyncMock()
    context = MagicMock()

    rem_id = ObjectId()
    rec = {
        "_id": rem_id,
        "user_id": 3333,
        "label": "Event",
        "target_date": "2026-11-20",
        "reminder_time": "09:00",
        "category": "General",
        "recurrence": "once",
        "birth_date": "2000-01-01",
        "birth_year": 2000,
        "is_spotlight": True,
    }

    with patch("handlers.reminders.reminders_col") as mock_col:
        mock_col.find_one = AsyncMock(return_value=rec)
        mock_col.update_one = AsyncMock()
        mock_col.update_many = AsyncMock()

        # 1. Invalid spotlight option
        context.args = [str(rem_id), "spotlight", "maybe"]
        await edit_reminder_command(update, context)
        assert "Spotlight value must be 'on' or 'off'" in update.effective_message.reply_text.call_args[0][0]

        # 2. Invalid birth year (non-digit)
        update.effective_message.reply_text.reset_mock()
        context.args = [str(rem_id), "birthyear", "abc"]
        await edit_reminder_command(update, context)
        assert "Invalid birth year" in update.effective_message.reply_text.call_args[0][0]

        # 3. Out of range birth year
        update.effective_message.reply_text.reset_mock()
        context.args = [str(rem_id), "birthyear", "1800"]
        await edit_reminder_command(update, context)
        assert "realistic 4-digit" in update.effective_message.reply_text.call_args[0][0]

        # 4. Clear birth date with "none"
        update.effective_message.reply_text.reset_mock()
        context.args = [str(rem_id), "birthdate", "none"]
        await edit_reminder_command(update, context)
        assert mock_col.update_one.call_args[0][1]["$set"]["birth_date"] is None
        assert mock_col.update_one.call_args[0][1]["$set"]["birth_year"] is None

        # 5. Clear birth year with "none"
        update.effective_message.reply_text.reset_mock()
        context.args = [str(rem_id), "birthyear", "clear"]
        await edit_reminder_command(update, context)
        assert mock_col.update_one.call_args[0][1]["$set"]["birth_year"] is None


@pytest.mark.asyncio
async def test_callback_clearbdate_and_details_menu():
    update = MagicMock()
    user = MagicMock()
    user.id = 4444
    update.effective_user = user
    query = MagicMock()
    query.from_user = user
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update.callback_query = query
    context = MagicMock()

    rem_id = ObjectId()
    rec = {
        "_id": rem_id,
        "user_id": 4444,
        "label": "Sister",
        "category": "Birthday",
        "target_date": "2026-09-09",
        "birth_date": "1998-09-09",
        "birth_year": 1998,
        "is_spotlight": False,
    }

    with patch("handlers.reminders.reminders_col") as mock_col:
        mock_col.find_one = AsyncMock(return_value=rec)
        mock_col.update_one = AsyncMock()

        # 1. Clear birthdate callback: edset_{id}_clearbdate_1
        query.data = f"edset_{rem_id}_clearbdate_1"
        await handle_reminder_callback(update, context)
        mock_col.update_one.assert_awaited_with({"_id": rem_id}, {"$set": {"birth_date": None, "birth_year": None}})
        assert query.answer.call_args[0][0] == "Birth date removed."

        # 2. Details sub-menu callback: edfield_{id}_details
        query.data = f"edfield_{rem_id}_details"
        await handle_reminder_callback(update, context)
        assert "Edit Details" in query.edit_message_text.call_args[0][0]

        # 3. Birth date prompt callback: edfield_{id}_bdate
        query.data = f"edfield_{rem_id}_bdate"
        await handle_reminder_callback(update, context)
        assert "Set Birth Date" in query.edit_message_text.call_args[0][0]


def test_companion_api_validation_errors(client):
    user_id = 8888
    token = generate_auth_token(user_id, "val_tester")
    headers = {"Authorization": f"Bearer {token}"}
    rem_id = ObjectId()
    doc = {
        "_id": rem_id,
        "user_id": user_id,
        "label": "Test",
        "target_date": "2030-01-01",
        "reminder_time": "09:00",
        "category": "General",
        "recurrence": "once",
    }

    with patch("server.reminders_col") as mock_col:
        mock_col.find_one = AsyncMock(return_value=dict(doc))

        # 1. Invalid birth date in POST
        res_post_bad = client.post("/api/reminders", headers=headers, json={
            "label": "Bad Date",
            "target_date": "2030-01-01",
            "birth_date": "bad-date",
        })
        assert res_post_bad.status_code == 400
        assert "Invalid birth date" in res_post_bad.get_json()["error"]

        # 2. Invalid birth date in PUT
        res_put_bdate = client.put(f"/api/reminders/{rem_id}", headers=headers, json={
            "birth_date": "bad-date"
        })
        assert res_put_bdate.status_code == 400
        assert "Invalid birth date" in res_put_bdate.get_json()["error"]

        # 3. Invalid birth year in PUT
        res_put_byear = client.put(f"/api/reminders/{rem_id}", headers=headers, json={
            "birth_year": "notanumber"
        })
        assert res_put_byear.status_code == 400
        assert "Invalid birth year" in res_put_byear.get_json()["error"]


def test_calculate_upcoming_age_datetime_and_newborn():
    # 1. date objects
    rem_date = {"birth_date": date(1995, 6, 20), "target_date": date(2026, 6, 20)}
    assert calculate_upcoming_age(rem_date) == 31

    # 2. datetime objects
    rem_dt = {"birth_date": datetime(1995, 6, 20, 12, 0), "target_date": datetime(2026, 6, 20, 9, 0)}
    assert calculate_upcoming_age(rem_dt) == 31

    # 3. ISO timestamps
    rem_iso = {"birth_date": "1995-06-20 00:00:00", "target_date": "2026-06-20T09:00:00"}
    assert calculate_upcoming_age(rem_iso) == 31

    # 4. Newborn turning 0
    rem_newborn = {"birth_year": 2026, "target_date": "2026-10-01"}
    assert calculate_upcoming_age(rem_newborn) == 0


@pytest.mark.asyncio
async def test_gui_decade_and_year_picker_flow():
    update = MagicMock()
    user = MagicMock()
    user.id = 5566
    update.effective_user = user
    query = MagicMock()
    query.from_user = user
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update.callback_query = query
    context = MagicMock()
    context.user_data = {}

    rem_id = ObjectId()
    rec = {
        "_id": rem_id,
        "user_id": 5566,
        "label": "David",
        "category": "Birthday",
        "target_date": "2026-04-15",
        "reminder_time": "09:00",
        "recurrence": "annually",
        "birth_date": None,
        "birth_year": None,
        "is_spotlight": False,
    }

    with patch("handlers.reminders.reminders_col") as mock_col:
        mock_col.find_one = AsyncMock(return_value=rec)
        mock_col.update_one = AsyncMock()

        # Step 1: Click "Set Birth Date" -> renders decades (1950s - 2020s)
        query.data = f"edfield_{rem_id}_bdate"
        await handle_reminder_callback(update, context)
        assert context.user_data.get("pending_bdate_rid") == str(rem_id)
        assert query.edit_message_text.awaited
        markup = query.edit_message_text.call_args[1]["reply_markup"]
        all_texts = [btn.text for row in markup.inline_keyboard for btn in row]
        assert "1990s" in all_texts
        assert "2000s" in all_texts

        # Step 2: Click "1990s" decade -> renders year buttons 1990 - 1999
        query.edit_message_text.reset_mock()
        query.data = f"edset_{rem_id}_bdec_1990"
        await handle_reminder_callback(update, context)
        assert query.edit_message_text.awaited
        markup_years = query.edit_message_text.call_args[1]["reply_markup"]
        year_texts = [btn.text for row in markup_years.inline_keyboard for btn in row]
        assert "1990" in year_texts
        assert "1995" in year_texts
        assert "1999" in year_texts

        # Step 3: Click "1995" -> sets birth_year=1995, birth_date="1995-04-15", updates card
        query.edit_message_text.reset_mock()
        query.data = f"edset_{rem_id}_byr_1995"
        await handle_reminder_callback(update, context)
        mock_col.update_one.assert_awaited()
        set_updates = mock_col.update_one.call_args[0][1]["$set"]
        assert set_updates["birth_year"] == 1995
        assert set_updates["birth_date"] == "1995-04-15"
        assert context.user_data.get("pending_bdate_rid") is None
        assert "Birth Date Updated!" in query.edit_message_text.call_args[0][0]


@pytest.mark.asyncio
async def test_pending_birth_date_text_input_and_routing():
    update = MagicMock()
    user = MagicMock()
    user.id = 6677
    update.effective_user = user
    update.effective_message.reply_text = AsyncMock()
    update.message = update.effective_message
    context = MagicMock()
    context.user_data = {}

    rem_id = ObjectId()
    rec = {
        "_id": rem_id,
        "user_id": 6677,
        "label": "Emma",
        "category": "Birthday",
        "target_date": "2026-08-25",
        "reminder_time": "09:00",
        "recurrence": "annually",
    }

    with patch("handlers.reminders.reminders_col") as mock_col:
        mock_col.find_one = AsyncMock(return_value=rec)
        mock_col.update_one = AsyncMock()

        # 1. Full date input via handle_pending_birth_date_input
        await handle_pending_birth_date_input(update, context, str(rem_id), "1998-08-25")
        mock_col.update_one.assert_awaited()
        set_vals = mock_col.update_one.call_args[0][1]["$set"]
        assert set_vals["birth_date"] == "1998-08-25"
        assert set_vals["birth_year"] == 1998

        # 2. Year only input
        update.effective_message.reply_text.reset_mock()
        await handle_pending_birth_date_input(update, context, str(rem_id), "2002")
        set_vals_yr = mock_col.update_one.call_args[0][1]["$set"]
        assert set_vals_yr["birth_year"] == 2002
        assert set_vals_yr["birth_date"] == "2002-08-25"

        # 3. Clear date
        update.effective_message.reply_text.reset_mock()
        await handle_pending_birth_date_input(update, context, str(rem_id), "none")
        set_vals_clear = mock_col.update_one.call_args[0][1]["$set"]
        assert set_vals_clear["birth_date"] is None
        assert set_vals_clear["birth_year"] is None

        # 4. Routing via core_routing_manager with pending_bdate_rid
        context.user_data["pending_bdate_rid"] = str(rem_id)
        update.message.text = "1993-08-25"
        with patch("handlers.media.codes_col", MagicMock()):
            await core_routing_manager(update, context)
            assert "Birth Date Updated!" in update.effective_message.reply_text.call_args[0][0]

        # 5. Routing cancel via core_routing_manager
        context.user_data["pending_bdate_rid"] = str(rem_id)
        update.message.text = "/cancel"
        with patch("handlers.media.codes_col", MagicMock()):
            await core_routing_manager(update, context)
            assert "Cancelled" in update.effective_message.reply_text.call_args[0][0]
            assert "pending_bdate_rid" not in context.user_data


@pytest.mark.asyncio
async def test_editrem_with_list_index_numbers():
    user_id = 7788
    update = MagicMock()
    user = MagicMock()
    user.id = user_id
    update.effective_user = user
    update.effective_message.reply_text = AsyncMock()
    context = MagicMock()

    id1, id2 = ObjectId(), ObjectId()
    rem1 = {"_id": id1, "user_id": user_id, "label": "First Item", "target_date": "2026-11-20", "category": "Birthday", "is_spotlight": False}
    rem2 = {"_id": id2, "user_id": user_id, "label": "Second Item", "target_date": "2026-12-20", "category": "General", "is_spotlight": False}

    with patch("handlers.reminders.reminders_col") as mock_col:
        # Mock skip(0).limit(1) to return rem1 for index 1
        mock_cursor = MagicMock()
        mock_cursor.to_list = AsyncMock(return_value=[rem1])
        mock_col.find.return_value.sort.return_value.skip.return_value.limit.return_value = mock_cursor
        mock_col.find_one = AsyncMock(return_value=rem1)
        mock_col.update_one = AsyncMock()
        mock_col.update_many = AsyncMock()
        mock_col.delete_one = AsyncMock()
        mock_col.count_documents = AsyncMock(return_value=0)

        # Direct test for resolve_reminder_id with 1 and 2
        r_oid1, r_rec1 = await resolve_reminder_id(user_id, "1")
        assert r_oid1 == id1
        assert r_rec1["label"] == "First Item"

        mock_cursor.to_list = AsyncMock(return_value=[rem2])
        r_oid2, r_rec2 = await resolve_reminder_id(user_id, "2")
        assert r_oid2 == id2
        assert r_rec2["label"] == "Second Item"

        # 1. /editrem 1 -> loads reminder card for rem1
        mock_cursor.to_list = AsyncMock(return_value=[rem1])
        context.args = ["1"]
        await edit_reminder_command(update, context)
        assert update.effective_message.reply_text.awaited
        card_text = update.effective_message.reply_text.call_args[0][0]
        assert "First Item" in card_text

        # 2. /editrem 1 spotlight -> toggles spotlight on rem1
        update.effective_message.reply_text.reset_mock()
        context.args = ["1", "spotlight"]
        await edit_reminder_command(update, context)
        mock_col.update_one.assert_awaited()
        assert "Spotlight Updated" in update.effective_message.reply_text.call_args[0][0]

        # 3. /editrem 1 birthdate 2000-11-20 -> updates birth date
        update.effective_message.reply_text.reset_mock()
        context.args = ["1", "birthdate", "2000-11-20"]
        await edit_reminder_command(update, context)
        assert mock_col.update_one.call_args[0][1]["$set"]["birth_date"] == "2000-11-20"

        # 4. /editrem 1 delete -> deletes rem1
        update.effective_message.reply_text.reset_mock()
        context.args = ["1", "delete"]
        await edit_reminder_command(update, context)
        mock_col.delete_one.assert_awaited_with({"_id": id1, "user_id": user_id})
        assert "Reminder deleted" in update.effective_message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_delrem_card_text_updated_with_back_button():
    update = MagicMock()
    user = MagicMock()
    user.id = 8899
    update.effective_user = user
    query = MagicMock()
    query.from_user = user
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update.callback_query = query
    context = MagicMock()

    rem_id = ObjectId()
    with patch("handlers.reminders.reminders_col") as mock_col:
        mock_col.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))
        mock_col.count_documents = AsyncMock(return_value=0)

        query.data = f"delrem_{rem_id}"
        await handle_reminder_callback(update, context)
        assert query.edit_message_text.awaited
        call_text = query.edit_message_text.call_args[0][0]
        call_markup = query.edit_message_text.call_args[1]["reply_markup"]

        # Text confirms deletion and back button is present
        assert "Reminder deleted" in call_text
        assert call_markup.inline_keyboard[0][0].text == "🔙 Back to List"
        assert call_markup.inline_keyboard[0][0].callback_data == "edfield_global_back"


@pytest.mark.asyncio
async def test_snooze_birthday_rejected_in_callback():
    user_id = 9900
    rem_id = ObjectId()

    bday_rec = {
        "_id": rem_id,
        "user_id": user_id,
        "label": "Grandma's 80th",
        "category": "Birthday",
        "target_date": "2026-11-20",
    }

    update = MagicMock()
    user = MagicMock()
    user.id = user_id
    update.effective_user = user
    query = MagicMock()
    query.from_user = user
    query.answer = AsyncMock()
    update.callback_query = query
    context = MagicMock()

    with patch("handlers.reminders.reminders_col") as mock_col:
        mock_col.find_one = AsyncMock(return_value=bday_rec)
        query.data = f"remsnooze_{rem_id}"
        await handle_reminder_callback(update, context)
        assert query.answer.call_args[0][0] == "Birthday reminders cannot be snoozed."


def test_snooze_birthday_rejected_in_api(client):
    user_id = 9900
    token = generate_auth_token(user_id, "snooze_tester")
    headers = {"Authorization": f"Bearer {token}"}
    rem_id = ObjectId()

    bday_rec = {
        "_id": rem_id,
        "user_id": user_id,
        "label": "Grandma's 80th",
        "category": "Birthday",
        "target_date": "2026-11-20",
    }

    with patch("server.reminders_col") as mock_col:
        mock_col.find_one = AsyncMock(return_value=bday_rec)
        res_snooze = client.post(f"/api/reminders/{rem_id}/snooze", headers=headers)
        assert res_snooze.status_code == 400
        assert "Birthday reminders cannot be snoozed." in res_snooze.get_json()["error"]


