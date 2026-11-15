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
    format_concise_date,
    format_relative_countdown,
    format_reminder_notification,
    handle_pending_birth_date_input,
    handle_reminder_callback,
    list_reminders,
    resolve_reminder_id,
    roll_date_annually,
    send_daily_reminder,
    send_instant_reminder_dispatch,
)
from server import flask_app
from utils.auth import (
    delete_web_session,
    generate_auth_token,
    get_web_session,
    save_web_session,
)


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

        # Verify Option B minimalist tree typography
        assert "<b>Active Reminders</b>" in text
        assert "─────" not in text
        assert "📜" not in text
        assert "1. <code>Alice</code> ↑25 ✦" in text
        assert "└ 15th Nov · today" in text
        assert "2. <code>Flight to Tokyo</code>" in text
        assert "└ 1st Dec · in 2w 2d [Travel]" in text
        assert "3. <code>Bob</code> ↑30" in text
        assert "└ 10th Dec · in 3w 4d" in text

        # Verify dual-row button grid (Row 1: instant dispatch, Row 2: edit/manage)
        assert markup is not None
        rows = markup.inline_keyboard
        assert len(rows) == 2
        # Row 1: instant silent triggers
        assert len(rows[0]) == 3
        assert rows[0][0].text == "⚡ 1"
        assert rows[0][0].callback_data == f"remfire_{id1}"
        assert rows[0][1].text == "⚡ 2"
        assert rows[0][1].callback_data == f"remfire_{id2}"
        assert rows[0][2].text == "⚡ 3"
        assert rows[0][2].callback_data == f"remfire_{id3}"
        # Row 2: manage / edit
        assert len(rows[1]) == 3
        assert rows[1][0].text == "⚙️ 1"
        assert rows[1][0].callback_data == f"editrem_{id1}"
        assert rows[1][1].text == "⚙️ 2"
        assert rows[1][1].callback_data == f"editrem_{id2}"
        assert rows[1][2].text == "⚙️ 3"
        assert rows[1][2].callback_data == f"editrem_{id3}"


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
    assert "<b>Label:</b> <code>Charlie</code>" in card_text
    assert "<b>Category:</b> Birthday" in card_text
    assert "<b>Target Date:</b> <code>20th Aug</code>" in card_text
    assert "<b>Birth Date:</b> <code>1996-08-20</code> (Turning 30)" in card_text
    assert "<b>Spotlight:</b> Active ✦" in card_text
    # Birthday reminders suppress recurrence line
    assert "<b>Recurrence:</b>" not in card_text

    # Spotlight on markup
    markup_spot = build_reminder_edit_markup(rec)
    flat_btns = [btn for row in markup_spot.inline_keyboard for btn in row]
    flat_texts = [b.text for b in flat_btns]
    flat_cbs = [b.callback_data for b in flat_btns]

    assert "⚡ Send on the Spot" in flat_texts
    assert f"remfire_{rem_id}" in flat_cbs
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
    assert "⚡ Send on the Spot" in flat_texts_off
    assert "✦ Spotlight" in flat_texts_off


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

        # Alice is spotlighted, so she appears first with ✦ and outside the blockquote
        assert "✦ <b>Alice</b>" in text
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

        # 12 buttons should produce 6 rows (3 pairs of [5, 5, 2] for ⚡ and ⚙️)
        assert len(rows) == 6
        assert len(rows[0]) == 5
        assert len(rows[1]) == 5
        assert len(rows[2]) == 5
        assert len(rows[3]) == 5
        assert len(rows[4]) == 2
        assert len(rows[5]) == 2
        assert rows[0][0].text == "⚡ 1"
        assert rows[1][0].text == "⚙️ 1"
        assert rows[2][0].text == "⚡ 6"
        assert rows[3][0].text == "⚙️ 6"
        assert rows[4][1].text == "⚡ 12"
        assert rows[5][1].text == "⚙️ 12"


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


def test_format_concise_date_ordinals_and_edge_cases():
    # 1. 1st, 2nd, 3rd, 4th, etc.
    assert format_concise_date("2026-02-01") == "1st Feb"
    assert format_concise_date("2026-11-02") == "2nd Nov"
    assert format_concise_date("2026-03-03") == "3rd Mar"
    assert format_concise_date("2026-04-04") == "4th Apr"

    # 2. 11th, 12th, 13th exceptions
    assert format_concise_date("2026-05-11") == "11th May"
    assert format_concise_date("2026-06-12") == "12th Jun"
    assert format_concise_date("2026-07-13") == "13th Jul"
    assert format_concise_date("2026-08-14") == "14th Aug"

    # 3. 21st, 22nd, 23rd, 24th
    assert format_concise_date("2026-09-21") == "21st Sep"
    assert format_concise_date("2026-10-22") == "22nd Oct"
    assert format_concise_date("2026-11-23") == "23rd Nov"
    assert format_concise_date("2026-12-24") == "24th Dec"
    assert format_concise_date("2026-01-31") == "31st Jan"

    # 4. datetime and date objects
    assert format_concise_date(datetime(2027, 2, 1, 10, 0)) == "1st Feb"
    assert format_concise_date(date(2027, 9, 24)) == "24th Sep"

    # 5. Invalid / empty fallbacks
    assert format_concise_date("") == ""
    assert format_concise_date(None) == ""
    assert format_concise_date("invalid-date") == "invalid-date"


@pytest.mark.asyncio
async def test_instant_trigger_remfire_callback_and_isolation():
    user_id = 7788
    rem_id = ObjectId()
    rec = {
        "_id": rem_id,
        "user_id": user_id,
        "label": "Alice's Birthday",
        "category": "Birthday",
        "target_date": "2026-11-25",
        "reminder_time": "09:00",
        "recurrence": "annually",
        "birth_date": "2001-11-25",
        "is_spotlight": True,
        "last_notified": "2026-11-01",
        "timezone": "UTC",
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
    context.bot.send_message = AsyncMock()

    with patch("handlers.reminders.reminders_col") as mock_col:
        mock_col.find_one = AsyncMock(return_value=rec)
        mock_col.update_one = AsyncMock()
        mock_col.delete_one = AsyncMock()

        query.data = f"remfire_{rem_id}"
        await handle_reminder_callback(update, context)

        # 1. Answer toast verification
        query.answer.assert_awaited_once_with("⚡ Sent reminder on the spot!")

        # 2. Exact reminder notification dispatched
        assert context.bot.send_message.awaited
        k_args = context.bot.send_message.call_args[1]
        assert k_args["chat_id"] == user_id
        text = k_args["text"]
        assert "<b>Birthday Reminder</b>" in text
        assert "✦ <b>Alice&#x27;s Birthday</b>" in text
        assert "25th Nov" in text
        assert "Turning 25" in text
        assert "• Annually" not in text
        assert "(🔄 Annually)" not in text
        assert k_args["reply_markup"] is None

        # 3. Target date, last_notified, and DB were completely untouched
        mock_col.update_one.assert_not_awaited()
        mock_col.delete_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_instant_trigger_remfire_non_birthday():
    user_id = 7789
    rem_id = ObjectId()
    rec = {
        "_id": rem_id,
        "user_id": user_id,
        "label": "Project Launch",
        "category": "Launch",
        "target_date": "2026-12-05",
        "reminder_time": "12:00",
        "recurrence": "once",
        "last_notified": "",
        "timezone": "UTC",
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
    context.bot.send_message = AsyncMock()

    with patch("handlers.reminders.reminders_col") as mock_col:
        mock_col.find_one = AsyncMock(return_value=rec)

        query.data = f"remfire_{rem_id}"
        await handle_reminder_callback(update, context)

        query.answer.assert_awaited_once_with("⚡ Sent reminder on the spot!")
        assert context.bot.send_message.awaited
        k_args = context.bot.send_message.call_args[1]
        text = k_args["text"]
        markup = k_args["reply_markup"]

        assert "<b>🚀 Launch Reminder</b>" in text
        assert "<b>Project Launch</b>" in text
        assert "5th Dec" in text
        assert markup is not None
        btn_texts = [b.text for row in markup.inline_keyboard for b in row]
        assert "💤 Snooze (1 Day)" in btn_texts
        assert "✅ Mark Done" in btn_texts


@pytest.mark.asyncio
async def test_instant_trigger_remfire_not_found_or_invalid():
    user_id = 7790
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
        # Not found
        mock_col.find_one = AsyncMock(return_value=None)
        query.data = f"remfire_{ObjectId()}"
        await handle_reminder_callback(update, context)
        query.answer.assert_awaited_once_with("Reminder not found.", show_alert=True)

        # Invalid ID
        query.answer.reset_mock()
        query.data = "remfire_invalidid"
        await handle_reminder_callback(update, context)
        query.answer.assert_awaited_once_with("Error sending reminder.", show_alert=True)


def test_api_reminders_trigger_companion_endpoint(client):
    user_id = 8899
    token = generate_auth_token(user_id, "trigger_tester")
    headers = {"Authorization": f"Bearer {token}"}
    rem_id = ObjectId()

    rec = {
        "_id": rem_id,
        "user_id": user_id,
        "label": "Buy Flowers",
        "category": "Birthday",
        "target_date": "2026-11-20",
        "reminder_time": "09:00",
        "recurrence": "annually",
        "timezone": "UTC",
    }

    # 1. Happy path: bot is online and delivers notification
    mock_app = MagicMock()
    mock_app.bot = MagicMock()
    mock_app.bot.send_message = AsyncMock()

    with patch("server.reminders_col") as mock_col, \
         patch("server._application", mock_app):
        mock_col.find_one = AsyncMock(return_value=rec)
        res = client.post(f"/api/reminders/{rem_id}/trigger", headers=headers)
        assert res.status_code == 200
        data = res.get_json()
        assert data["success"] is True
        assert data["message"] == "Reminder sent on the spot!"
        assert mock_app.bot.send_message.awaited

    # 2. Bot offline: returns 503
    with patch("server.reminders_col") as mock_col, \
         patch("server._application", None):
        mock_col.find_one = AsyncMock(return_value=rec)
        res_off = client.post(f"/api/reminders/{rem_id}/trigger", headers=headers)
        assert res_off.status_code == 503
        assert "Bot is currently offline." in res_off.get_json()["error"]

    # 3. Reminder not found: returns 404
    with patch("server.reminders_col") as mock_col, \
         patch("server._application", mock_app):
        mock_col.find_one = AsyncMock(return_value=None)
        res_404 = client.post(f"/api/reminders/{rem_id}/trigger", headers=headers)
        assert res_404.status_code == 404
        assert "Reminder not found." in res_404.get_json()["error"]

    # 4. Invalid ObjectId: returns 400
    res_400 = client.post("/api/reminders/notanobjectid/trigger", headers=headers)
    assert res_400.status_code == 400
    assert "Invalid reminder ID." in res_400.get_json()["error"]

    # 5. Unauthenticated: returns 401
    res_401 = client.post(f"/api/reminders/{rem_id}/trigger")
    assert res_401.status_code == 401


def test_api_reminders_get_includes_concise_date(client):
    user_id = 9911
    token = generate_auth_token(user_id, "concise_tester")
    headers = {"Authorization": f"Bearer {token}"}
    rem_id = ObjectId()

    doc = {
        "_id": rem_id,
        "user_id": user_id,
        "label": "her Birthday",
        "category": "Birthday",
        "target_date": "2027-02-01",
        "reminder_time": "09:00",
        "recurrence": "annually",
        "birth_date": None,
        "birth_year": None,
        "is_spotlight": False,
        "timezone": "UTC",
    }

    with patch("server.reminders_col") as mock_col:
        mock_cursor = MagicMock()
        mock_cursor.to_list = AsyncMock(return_value=[doc])
        mock_col.find.return_value.sort.return_value = mock_cursor

        res = client.get("/api/reminders", headers=headers)
        assert res.status_code == 200
        items = res.get_json()["reminders"]
        assert len(items) == 1
        assert items[0]["concise_date"] == "1st Feb"


@pytest.mark.asyncio
async def test_format_reminder_notification_and_dispatch():
    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    # 1. Birthday reminder
    b_rec = {
        "_id": ObjectId(),
        "label": "Mom",
        "category": "Birthday",
        "target_date": "2026-11-20",
        "recurrence": "annually",
        "is_spotlight": True,
        "birth_year": 1960,
    }
    text, markup = format_reminder_notification(b_rec)
    assert "<b>Birthday Reminder</b>" in text
    assert "✦ <b>Mom</b>" in text
    assert "20th Nov" in text
    assert "Turning" in text
    assert markup is None

    await send_instant_reminder_dispatch(mock_bot, b_rec, 1234)
    assert mock_bot.send_message.awaited
    assert mock_bot.send_message.call_args[1]["chat_id"] == 1234


def test_roll_date_annually_leap_and_non_leap():
    # 1. Normal annual rollover
    d1 = date(2026, 9, 24)
    assert roll_date_annually(d1) == date(2027, 9, 24)

    # 2. Leap year Feb 1st rollover (must not become Jan 31st)
    d2 = date(2024, 2, 1)
    assert roll_date_annually(d2) == date(2025, 2, 1)

    # 3. Leap day Feb 29th rollover to non-leap year falls back to Feb 28th
    d3 = date(2024, 2, 29)
    assert roll_date_annually(d3) == date(2025, 2, 28)

    # 4. Roll forward past a target date
    d4 = date(2022, 11, 20)
    assert roll_date_annually(d4, target_after=date(2026, 11, 15)) == date(2026, 11, 20)


@pytest.mark.asyncio
async def test_daily_reminder_past_due_birthday_never_deleted_and_preserves_date():
    user_id = 998877
    context = MagicMock()
    context.job.data = {"user_id": user_id, "timezone": "UTC", "reminder_time": "09:00"}
    context.bot.send_message = AsyncMock()

    # Birthday reminder past-due with recurrence 'once'
    bday_doc = {
        "_id": ObjectId(),
        "user_id": user_id,
        "label": "Best Friend",
        "category": "Birthday",
        "target_date": "2024-02-01",
        "recurrence": "once",
        "last_notified": "",
    }

    with patch("handlers.reminders.reminders_col") as mock_col, \
         patch("handlers.reminders.notify_admin", AsyncMock()):
        mock_col.find.return_value.to_list = AsyncMock(return_value=[bday_doc])
        mock_col.delete_one = AsyncMock()
        mock_col.update_one = AsyncMock()

        await send_daily_reminder(context)

        # MUST NOT be deleted
        mock_col.delete_one.assert_not_called()
        # MUST roll forward to next year preserving Feb 1st (not shifting to Jan 31st)
        assert mock_col.update_one.awaited
        update_call = mock_col.update_one.call_args[0][1]["$set"]
        new_target = update_call["target_date"]
        assert new_target.endswith("-02-01")


@pytest.mark.asyncio
async def test_remdone_birthday_with_once_recurrence_rolls_forward_annually():
    user_id = 556677
    update = MagicMock()
    user = MagicMock()
    user.id = user_id
    update.effective_user = user
    query = MagicMock()
    query.from_user = user
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message.reply_markup = None
    update.callback_query = query
    context = MagicMock()
    context.application = None

    rem_id = ObjectId()
    rec = {
        "_id": rem_id,
        "user_id": user_id,
        "label": "Sister",
        "category": "Birthday",
        "target_date": "2026-06-15",
        "recurrence": "once",
    }

    with patch("handlers.reminders.reminders_col") as mock_col:
        mock_col.find_one = AsyncMock(return_value=rec)
        mock_col.delete_one = AsyncMock()
        mock_col.update_one = AsyncMock()

        query.data = f"remdone_{rem_id}"
        await handle_reminder_callback(update, context)

        # Must not be deleted; must roll over
        mock_col.delete_one.assert_not_called()
        mock_col.update_one.assert_awaited()
        update_set = mock_col.update_one.call_args[0][1]["$set"]
        assert update_set["target_date"] == "2027-06-15"
        # Confirm text in message uses concise date and omits recurrence tag
        msg = query.edit_message_text.call_args[0][0]
        assert "15th Jun" in msg
        assert "Annually" not in msg


@pytest.mark.asyncio
async def test_list_reminders_callback_pagination_preserves_order():
    user_id = 332211
    update = MagicMock()
    user = MagicMock()
    user.id = user_id
    update.effective_user = user
    update.effective_message.reply_text = AsyncMock()
    query = MagicMock()
    query.data = "edfield_global_back"
    query.edit_message_text = AsyncMock()
    update.callback_query = query
    context = MagicMock()

    # Create 26 reminders so pagination triggers at 25
    twenty_six = [
        {
            "_id": ObjectId(),
            "user_id": user_id,
            "label": f"Task {i}",
            "target_date": "2026-12-01",
            "reminder_time": "09:00",
            "category": "General",
            "recurrence": "once",
        }
        for i in range(1, 27)
    ]

    with patch("handlers.reminders.reminders_col") as mock_col:
        mock_cursor = MagicMock()
        mock_cursor.to_list = AsyncMock(return_value=twenty_six)
        mock_col.find.return_value.sort.return_value = mock_cursor

        await list_reminders(update, context)

        # Page 1 (chunk 1 with tasks 1-25) should EDIT the query message
        assert query.edit_message_text.awaited
        page1_text = query.edit_message_text.call_args[0][0]
        assert "Active Reminders" in page1_text
        assert "1. <code>Task 1</code>" in page1_text
        assert "25. <code>Task 25</code>" in page1_text
        assert "Task 26" not in page1_text

        # Page 2 (chunk 2 with task 26) should be sent via reply_text
        assert update.effective_message.reply_text.awaited
        page2_text = update.effective_message.reply_text.call_args[0][0]
        assert "Active Reminders (Cont.)" in page2_text
        assert "26. <code>Task 26</code>" in page2_text


def test_api_reminders_create_and_update_birthday_guarantees(client):
    user_id = 112233
    token = generate_auth_token(user_id, "bday_guard")
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Creating birthday forces recurrence = annually even if sent as once
    with patch("server.reminders_col") as mock_col:
        inserted_doc = None
        async def fake_insert(doc):
            nonlocal inserted_doc
            inserted_doc = dict(doc)
            inserted_doc["_id"] = ObjectId()
            mock_res = MagicMock()
            mock_res.inserted_id = inserted_doc["_id"]
            return mock_res

        mock_col.insert_one = fake_insert
        res = client.post("/api/reminders", headers=headers, json={
            "label": "My Brother",
            "category": "Birthday",
            "target_date": "2027-03-05",
            "recurrence": "once",
        })
        assert res.status_code == 200
        data = res.get_json()["reminder"]
        assert inserted_doc["recurrence"] == "annually"
        assert data["concise_date"] == "5th Mar"
        assert data["status"] == "active"

    # 2. Updating existing reminder to category Birthday forces recurrence = annually
    rem_id = ObjectId()
    existing_rem = {
        "_id": rem_id,
        "user_id": user_id,
        "label": "Party",
        "category": "General",
        "target_date": "2027-04-10",
        "recurrence": "once",
    }
    with patch("server.reminders_col") as mock_col:
        mock_col.find_one = AsyncMock(side_effect=[
            existing_rem,
            {**existing_rem, "category": "Birthday", "recurrence": "annually"}
        ])
        mock_col.update_one = AsyncMock()

        res_up = client.put(f"/api/reminders/{rem_id}", headers=headers, json={
            "category": "Birthday"
        })
        assert res_up.status_code == 200
        mock_col.update_one.assert_awaited()
        update_set = mock_col.update_one.call_args[0][1]["$set"]
        assert update_set["category"] == "Birthday"
        assert update_set["recurrence"] == "annually"
        res_doc = res_up.get_json()["reminder"]
        assert res_doc["concise_date"] == "10th Apr"


@pytest.mark.asyncio
async def test_edfield_details_and_command_suppress_recurrence_for_birthdays():
    user_id = 9988
    rem_id = ObjectId()
    bday_rec = {
        "_id": rem_id,
        "user_id": user_id,
        "label": "Alice",
        "category": "Birthday",
        "target_date": "2027-02-01",
        "recurrence": "annually",
    }

    # 1. edfield_details omits recurrence button for birthdays
    update = MagicMock()
    user = MagicMock()
    user.id = user_id
    update.effective_user = user
    query = MagicMock()
    query.from_user = user
    query.data = f"edfield_{rem_id}_details"
    query.edit_message_text = AsyncMock()
    query.answer = AsyncMock()
    update.callback_query = query
    context = MagicMock()

    with patch("handlers.reminders.reminders_col") as mock_col:
        mock_col.find_one = AsyncMock(return_value=bday_rec)
        await handle_reminder_callback(update, context)

        assert query.edit_message_text.awaited
        reply_markup = query.edit_message_text.call_args[1]["reply_markup"]
        all_cb_data = [btn.callback_data for row in reply_markup.inline_keyboard for btn in row]
        # Should NOT contain edfield_{id}_rec
        assert not any("edfield_" in cb and "_rec" in cb for cb in all_cb_data)

    # 2. /editrem <id> rec <val> informs user birthdays recur annually
    update_cmd = MagicMock()
    update_cmd.effective_user = user
    update_cmd.effective_message.reply_text = AsyncMock()
    context_cmd = MagicMock()
    context_cmd.args = [str(rem_id), "rec", "daily"]

    with patch("handlers.reminders.reminders_col") as mock_col:
        mock_col.find_one = AsyncMock(return_value=bday_rec)
        await edit_reminder_command(update_cmd, context_cmd)
        assert update_cmd.effective_message.reply_text.awaited
        reply = update_cmd.effective_message.reply_text.call_args[0][0]
        assert "Birthday reminders automatically recur annually." in reply


def test_format_relative_countdown():
    assert format_relative_countdown(0) == "today"
    assert format_relative_countdown(1) == "tomorrow"
    assert format_relative_countdown(-1) == "yesterday"
    assert format_relative_countdown(-5) == "5d ago"
    assert format_relative_countdown(-30) == "30d ago"
    assert format_relative_countdown(2) == "in 2d"
    assert format_relative_countdown(7) == "in 1w"
    assert format_relative_countdown(8) == "in 1w 1d"
    assert format_relative_countdown(14) == "in 2w"
    assert format_relative_countdown(16) == "in 2w 2d"
    assert format_relative_countdown(30) == "in 1m"
    assert format_relative_countdown(78) == "in 2m 2w 4d"
    assert format_relative_countdown(313) == "in 10m 1w 6d"
    assert format_relative_countdown(352) == "in 11m 3w 1d"


@pytest.mark.asyncio
async def test_list_reminders_next_year_expandable_blockquote_with_spotlight_exception():
    update = MagicMock()
    user = MagicMock()
    user.id = 778899
    update.effective_user = user
    update.effective_message.reply_text = AsyncMock()
    context = MagicMock()

    id1, id2, id3, id4 = ObjectId(), ObjectId(), ObjectId(), ObjectId()
    reminders = [
        {
            "_id": id1,
            "user_id": 778899,
            "label": "Current Year Birthday",
            "target_date": "2026-11-20",
            "category": "Birthday",
            "recurrence": "annually",
            "birth_year": 2000,
            "is_spotlight": False,
            "timezone": "UTC",
        },
        {
            "_id": id2,
            "user_id": 778899,
            "label": "Next Year Spotlight",
            "target_date": "2027-02-14",
            "category": "Birthday",
            "recurrence": "annually",
            "birth_year": 1995,
            "is_spotlight": True,
            "timezone": "UTC",
        },
        {
            "_id": id3,
            "user_id": 778899,
            "label": "Next Year Non-Spotlight",
            "target_date": "2027-05-10",
            "category": "Birthday",
            "recurrence": "annually",
            "birth_year": 1990,
            "is_spotlight": False,
            "timezone": "UTC",
        },
        {
            "_id": id4,
            "user_id": 778899,
            "label": "Future Flight",
            "target_date": "2027-06-01",
            "category": "Travel",
            "recurrence": "once",
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
        text = update.effective_message.reply_text.call_args[0][0]

        # 1. Main list contains current year birthday with monospace and age tag
        assert "1. <code>Current Year Birthday</code> ↑26" in text

        # 2. CRITICAL EXCEPTION: Next-year spotlighted birthday stays in main list outside blockquote
        assert "2. <code>Next Year Spotlight</code> ↑32 ✦" in text

        # 3. Non-birthday event stays in main list even if in next year
        assert "4. <code>Future Flight</code>" in text
        assert "[Travel]" in text

        # 4. Next-year non-spotlight birthday is collapsed inside <blockquote expandable>
        assert "<blockquote expandable>" in text
        assert "Upcoming Next Year:" in text
        assert "3. <code>Next Year Non-Spotlight</code> ↑37" in text
        assert "</blockquote>" in text

        # Verify blockquote structure: next year spotlight is NOT inside blockquote
        bq_content = text.split("<blockquote expandable>")[1].split("</blockquote>")[0]
        assert "3. <code>Next Year Non-Spotlight</code> ↑37" in bq_content
        assert "Next Year Spotlight" not in bq_content
        assert "Current Year Birthday" not in bq_content
        assert "Future Flight" not in bq_content


@pytest.mark.asyncio
async def test_list_reminders_all_current_year_no_blockquote():
    update = MagicMock()
    user = MagicMock()
    user.id = 121212
    update.effective_user = user
    update.effective_message.reply_text = AsyncMock()
    context = MagicMock()

    reminders = [
        {
            "_id": ObjectId(),
            "user_id": 121212,
            "label": "Sister",
            "target_date": "2026-11-25",
            "category": "Birthday",
            "birth_year": 1999,
            "is_spotlight": True,
            "timezone": "UTC",
        },
        {
            "_id": ObjectId(),
            "user_id": 121212,
            "label": "Doctor",
            "target_date": "2026-12-05",
            "category": "Medication",
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
        text = update.effective_message.reply_text.call_args[0][0]

        assert "1. <code>Sister</code> ↑27 ✦" in text
        assert "2. <code>Doctor</code>" in text
        # No blockquote when no next-year non-spotlight birthdays exist
        assert "<blockquote expandable>" not in text


@pytest.mark.asyncio
async def test_persistent_web_auth_session_mongodb_lifecycle():
    fake_sessions_db = {}

    class MockWebSessionsCol:
        async def update_one(self, filter_query, update_doc, upsert=False):
            token = filter_query.get("token")
            data = update_doc.get("$set", {})
            fake_sessions_db[token] = dict(data)
            return MagicMock(modified_count=1, upserted_id=token)

        async def find_one(self, filter_query):
            token = filter_query.get("token")
            return fake_sessions_db.get(token)

        async def delete_one(self, filter_query):
            token = filter_query.get("token")
            existed = token in fake_sessions_db
            fake_sessions_db.pop(token, None)
            return MagicMock(deleted_count=1 if existed else 0)

    mock_col = MockWebSessionsCol()
    with patch("database.web_sessions_col", mock_col):
        # 1. Save session (30 days validity)
        token = "test-persistent-token-xyz.12345"
        saved = await save_web_session(token, 445566, "persist_user", expires_in=86400 * 30)
        assert saved["user_id"] == 445566
        assert saved["username"] == "persist_user"
        assert token in fake_sessions_db
        assert "expires_at" in fake_sessions_db[token]

        # 2. Get active session
        session_info = await get_web_session(token)
        assert session_info is not None
        assert session_info["user_id"] == 445566
        assert session_info["username"] == "persist_user"

        # 3. Expired session returns None and cleans up
        fake_sessions_db[token]["expires_at"] = "2020-01-01T00:00:00+00:00"
        expired_info = await get_web_session(token)
        assert expired_info is None
        assert token not in fake_sessions_db

        # 4. Delete session upon logout
        token2 = "test-token-2.67890"
        await save_web_session(token2, 7788, "user2")
        assert token2 in fake_sessions_db
        deleted = await delete_web_session(token2)
        assert deleted is True
        assert token2 not in fake_sessions_db


def test_server_persistent_web_session_across_restarts(client):
    persistent_token = "persistent-stored-session-token-999.sig"
    session_doc = {
        "token": persistent_token,
        "user_id": 99999,
        "username": "persisted_dashboard_user",
        "is_admin": False,
        "created_at": "2026-11-15T00:00:00+00:00",
        "expires_at": "2026-12-15T00:00:00+00:00",
    }

    mock_col = MagicMock()
    with patch("server.web_sessions_col", mock_col), \
         patch("database.web_sessions_col", mock_col):
        mock_col.find_one = AsyncMock(return_value=session_doc)
        mock_col.update_one = AsyncMock()

        # 1. Even without valid HMAC or Flask cookie, user authenticates via MongoDB session
        res = client.get("/api/auth/me", headers={"Authorization": f"Bearer {persistent_token}"})
        assert res.status_code == 200
        data = res.get_json()
        assert data["authenticated"] is True
        assert data["user"]["user_id"] == 99999
        assert data["user"]["username"] == "persisted_dashboard_user"

        # 2. POST /api/auth/token with persistent token restores session
        res_post = client.post("/api/auth/token", json={"token": persistent_token})
        assert res_post.status_code == 200
        post_data = res_post.get_json()
        assert post_data["success"] is True
        assert post_data["user"]["user_id"] == 99999
        mock_col.update_one.assert_awaited()

        # 3. Logout cleans up persistent session
        mock_col.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))
        res_logout = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {persistent_token}"})
        assert res_logout.status_code == 200
        mock_col.delete_one.assert_awaited_with({"token": persistent_token})

        # 4. Session cookie logout also cleans up persistent session
        mock_col.delete_one.reset_mock()
        with client.session_transaction() as sess:
            sess["user_id"] = 99999
            sess["token"] = persistent_token
        res_cookie_logout = client.post("/api/auth/logout")
        assert res_cookie_logout.status_code == 200
        mock_col.delete_one.assert_awaited_with({"token": persistent_token})


def test_format_relative_countdown_extra_edge_cases():
    assert format_relative_countdown(None) == ""
    assert format_relative_countdown("abc") == ""
    assert format_relative_countdown("14") == "in 2w"
    assert format_relative_countdown(-2) == "2d ago"
    assert format_relative_countdown(21) == "in 3w"
    assert format_relative_countdown(28) == "in 4w"
    assert format_relative_countdown(37) == "in 1m 1w"
    assert format_relative_countdown(45) == "in 1m 2w 1d"
    assert format_relative_countdown(60) == "in 2m"
    assert format_relative_countdown(90) == "in 3m"
    assert format_relative_countdown(365) == "in 12m 5d"


@pytest.mark.asyncio
async def test_list_reminders_non_birthday_with_birth_year_suppresses_age_tag():
    update = MagicMock()
    user = MagicMock()
    user.id = 556677
    update.effective_user = user
    update.effective_message.reply_text = AsyncMock()
    context = MagicMock()

    id1 = ObjectId()
    reminders = [
        {
            "_id": id1,
            "user_id": 556677,
            "label": "Flight to Paris",
            "target_date": "2026-12-10",
            "category": "Travel",
            "recurrence": "once",
            "birth_year": 1995,  # accidentally set
            "is_spotlight": False,
            "timezone": "UTC",
        }
    ]

    with patch("handlers.reminders.reminders_col") as mock_col:
        mock_cursor = MagicMock()
        mock_cursor.to_list = AsyncMock(return_value=reminders)
        mock_col.find.return_value.sort.return_value = mock_cursor

        await list_reminders(update, context)
        assert update.effective_message.reply_text.awaited
        text = update.effective_message.reply_text.call_args[0][0]

        # Verify age is NOT rendered for non-birthday
        assert "↑" not in text
        assert "1. <code>Flight to Paris</code>" in text
        assert "[Travel]" in text


@pytest.mark.asyncio
async def test_list_reminders_all_next_year_birthdays_only_blockquote():
    update = MagicMock()
    user = MagicMock()
    user.id = 889900
    update.effective_user = user
    update.effective_message.reply_text = AsyncMock()
    context = MagicMock()

    id1, id2 = ObjectId(), ObjectId()
    reminders = [
        {
            "_id": id1,
            "user_id": 889900,
            "label": "Future Birthday One",
            "target_date": "2027-03-01",
            "category": "Birthday",
            "recurrence": "annually",
            "birth_year": 1990,
            "is_spotlight": False,
            "timezone": "UTC",
        },
        {
            "_id": id2,
            "user_id": 889900,
            "label": "Future Birthday Two",
            "target_date": "2027-08-15",
            "category": "Birthday",
            "recurrence": "annually",
            "birth_year": 1995,
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
        text = update.effective_message.reply_text.call_args[0][0]

        assert "<b>Active Reminders</b>" in text
        assert "<blockquote expandable>" in text
        assert "Upcoming Next Year:" in text
        assert "1. <code>Future Birthday One</code> ↑37" in text
        assert "2. <code>Future Birthday Two</code> ↑32" in text
        assert "</blockquote>" in text





