import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from handlers.admin import (
    export_data,
    get_system_logs,
    monthly_system_logs_job,
    weekly_backup_job,
)
from server import flask_app
from utils.auth import save_web_session
from utils.helpers import save_user_info
from utils.rate_limiter import is_rate_limited
from utils.sentinel import log_event, notify_admin


@pytest.mark.asyncio
async def test_monthly_system_logs_job_zero_pii():
    context = MagicMock()
    context.bot.send_document = AsyncMock()

    with patch("handlers.admin.ADMIN_ID", 99999), \
         patch("handlers.admin.codes_col") as mock_codes, \
         patch("handlers.admin.reminders_col") as mock_rems, \
         patch("handlers.admin.users_col") as mock_users:

        mock_codes.count_documents = AsyncMock(return_value=142)
        mock_rems.count_documents = AsyncMock(return_value=29)
        mock_users.count_documents = AsyncMock(return_value=65)

        await monthly_system_logs_job(context)

        assert context.bot.send_document.awaited
        call_kwargs = context.bot.send_document.call_args[1]

        # Verify chat_id is admin
        assert call_kwargs["chat_id"] == 99999

        # Verify filename pattern: system_logs_YYYYMMDD_HHMMSS.json
        filename = call_kwargs["filename"]
        assert filename.startswith("system_logs_")
        assert filename.endswith(".json")
        timestamp_part = filename[len("system_logs_"):-len(".json")]
        # Ensure it parses as YYYYMMDD_HHMMSS
        datetime.strptime(timestamp_part, "%Y%m%d_%H%M%S")

        # Verify caption format
        caption = call_kwargs["caption"]
        assert "📊 **System Logs (Monthly Summary)**" in caption
        assert "• Assets: 142" in caption
        assert "• Reminders: 29" in caption
        assert "• Total Users: 65" in caption
        assert "🔒 Privacy Mode: Zero User Logs Stored" in caption

        # Inspect document JSON payload content
        doc_bytes = call_kwargs["document"]
        doc_bytes.seek(0)
        payload = json.loads(doc_bytes.read().decode("utf-8"))

        assert payload["report_type"] == "system_logs"
        assert payload["period"] == "monthly"
        assert "generated_at" in payload
        assert payload["metrics"] == {
            "total_assets": 142,
            "total_reminders": 29,
            "total_users": 65,
        }

        # STRICT PRIVACY AUDIT: Ensure absolutely no user identifiers exist
        payload_str = json.dumps(payload).lower()
        forbidden_keys = ["user_id", "username", "first_name", "last_name", "password", "passkey", "code", "label"]
        for key in forbidden_keys:
            assert f'"{key}"' not in payload_str


@pytest.mark.asyncio
async def test_monthly_system_logs_fallback_user_calculation():
    context = MagicMock()
    context.bot.send_document = AsyncMock()

    with patch("handlers.admin.ADMIN_ID", 99999), \
         patch("handlers.admin.codes_col") as mock_codes, \
         patch("handlers.admin.reminders_col") as mock_rems, \
         patch("handlers.admin.users_col", None), \
         patch("handlers.admin.unlocked_groups_col") as mock_unlocked:

        mock_codes.count_documents = AsyncMock(return_value=10)
        mock_rems.count_documents = AsyncMock(return_value=5)
        mock_rems.distinct = AsyncMock(return_value=[101, 102])
        mock_unlocked.distinct = AsyncMock(return_value=[102, 103])

        await monthly_system_logs_job(context)

        call_kwargs = context.bot.send_document.call_args[1]
        doc_bytes = call_kwargs["document"]
        doc_bytes.seek(0)
        payload = json.loads(doc_bytes.read().decode("utf-8"))

        # Distinct union of {101, 102} and {102, 103} = 3 users
        assert payload["metrics"]["total_users"] == 3


def test_weekly_backup_job_alias():
    # weekly_backup_job must reference monthly_system_logs_job for compatibility
    assert weekly_backup_job is monthly_system_logs_job


@pytest.mark.asyncio
async def test_log_event_does_not_persist():
    # Calling log_event should not raise and should not write to any DB collection
    await log_event(12345, "testuser", "test_action")


@pytest.mark.asyncio
async def test_notify_admin_sends_without_persisting_user_logs():
    context = MagicMock()
    context.bot.send_message = AsyncMock()

    with patch("utils.sentinel.ADMIN_ID", 88888):
        await notify_admin(context, "Surgical Alert: test notification")
        context.bot.send_message.assert_awaited_once_with(
            chat_id=88888,
            text="Surgical Alert: test notification",
            disable_notification=False,
        )


@pytest.mark.asyncio
async def test_save_user_info_no_pii_stored():
    user = MagicMock()
    user.id = 77777
    user.username = "secretuser"
    user.first_name = "Secret"
    user.last_name = "Agent"

    with patch("utils.helpers.users_col") as mock_users:
        mock_users.update_one = AsyncMock()
        await save_user_info(user)

        assert mock_users.update_one.awaited
        call_args = mock_users.update_one.call_args
        filter_dict = call_args[0][0]
        update_dict = call_args[0][1]

        assert filter_dict == {"user_id": 77777}
        # Only anonymous user presence ($setOnInsert with user_id)
        assert "$setOnInsert" in update_dict
        assert update_dict["$setOnInsert"] == {"user_id": 77777}
        # Strictly ensure no first_name, last_name, or last_seen
        assert "$set" not in update_dict
        update_str = json.dumps(update_dict)
        assert "first_name" not in update_str
        assert "last_name" not in update_str
        assert "last_seen" not in update_str
        assert "username" not in update_str


@pytest.mark.asyncio
async def test_get_system_logs_handler_privacy_mode():
    update = MagicMock()
    update.effective_user.id = 99999
    update.effective_message.reply_text = AsyncMock()
    context = MagicMock()

    with patch("handlers.admin.ADMIN_ID", 99999):
        await get_system_logs(update, context)

        assert update.effective_message.reply_text.awaited
        text = update.effective_message.reply_text.call_args[0][0]
        assert "Privacy Mode Active" in text
        assert "Zero user activity logs are recorded or stored" in text


@pytest.mark.asyncio
async def test_export_data_handler_zero_pii():
    update = MagicMock()
    update.effective_user.id = 99999
    update.effective_message.reply_document = AsyncMock()
    context = MagicMock()

    with patch("handlers.admin.ADMIN_ID", 99999), \
         patch("handlers.admin.codes_col") as mock_codes, \
         patch("handlers.admin.reminders_col") as mock_rems, \
         patch("handlers.admin.users_col") as mock_users:

        mock_codes.count_documents = AsyncMock(return_value=50)
        mock_rems.count_documents = AsyncMock(return_value=12)
        mock_users.count_documents = AsyncMock(return_value=8)

        await export_data(update, context)

        assert update.effective_message.reply_document.awaited
        kwargs = update.effective_message.reply_document.call_args[1]
        assert kwargs["filename"].startswith("system_logs_")
        assert kwargs["filename"].endswith(".json")
        assert "Privacy Mode: Zero User Logs Stored" in kwargs["caption"]

        doc_bytes = kwargs["document"]
        doc_bytes.seek(0)
        payload = json.loads(doc_bytes.read().decode("utf-8"))
        assert payload["report_type"] == "system_logs"
        assert payload["metrics"] == {"total_assets": 50, "total_reminders": 12, "total_users": 8}


def test_api_admin_logs_returns_privacy_mode():
    with flask_app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_id"] = 99999
            sess["is_admin"] = True

        with patch("server.ADMIN_ID", 99999):
            res = client.get("/api/admin/logs")
            assert res.status_code == 200
            data = res.get_json()
            assert data["success"] is True
            assert data["privacy_mode"] is True
            assert data["logs"] == []
            assert "Privacy Mode Active" in data["message"]


@pytest.mark.asyncio
async def test_retained_user_caching_web_sessions_no_logs_insert():
    # Verifies user companion web token caching functions without writing to logs_col
    with patch("database.web_sessions_col") as mock_sessions:
        mock_sessions.update_one = AsyncMock()
        session_data = await save_web_session("test-token-123", 55555, "caching_user")
        assert session_data["token"] == "test-token-123"
        assert session_data["user_id"] == 55555
        assert mock_sessions.update_one.awaited


def test_retained_in_memory_rate_limiter():
    uid = 987654
    # User's ephemeral rate limit checks succeed without database persistence
    limited, retry_after = is_rate_limited(uid, max_hits=5, window_seconds=10)
    assert limited is False
    assert retry_after == 0


@pytest.mark.asyncio
async def test_monthly_system_logs_job_handles_db_disconnection():
    context = MagicMock()
    context.bot.send_document = AsyncMock()

    with patch("handlers.admin.ADMIN_ID", 99999), \
         patch("handlers.admin.codes_col") as mock_codes, \
         patch("handlers.admin.reminders_col") as mock_rems, \
         patch("handlers.admin.users_col") as mock_users:

        # Database error on codes_col
        mock_codes.count_documents = AsyncMock(side_effect=RuntimeError("MongoDB connection lost"))
        mock_rems.count_documents = AsyncMock(return_value=15)
        mock_users.count_documents = AsyncMock(return_value=40)

        await monthly_system_logs_job(context)

        # Job should gracefully proceed with partial metrics instead of crashing
        assert context.bot.send_document.awaited
        call_kwargs = context.bot.send_document.call_args[1]
        doc_bytes = call_kwargs["document"]
        doc_bytes.seek(0)
        payload = json.loads(doc_bytes.read().decode("utf-8"))
        assert payload["metrics"]["total_assets"] == 0
        assert payload["metrics"]["total_reminders"] == 15
        assert payload["metrics"]["total_users"] == 40


@pytest.mark.asyncio
async def test_monthly_system_logs_job_none_context_safety():
    # Null context or bot should not raise exceptions
    await monthly_system_logs_job(None)
    mock_ctx = MagicMock()
    mock_ctx.bot = None
    await monthly_system_logs_job(mock_ctx)


@pytest.mark.asyncio
async def test_get_total_users_count_across_all_functional_collections():
    from handlers.admin import get_total_users_count

    with patch("handlers.admin.users_col") as mock_u, \
         patch("handlers.admin.reminders_col") as mock_r, \
         patch("handlers.admin.unlocked_groups_col") as mock_ug, \
         patch("handlers.admin.web_sessions_col") as mock_ws:

        mock_u.distinct = AsyncMock(return_value=[1, 2])
        mock_r.distinct = AsyncMock(return_value=[2, 3])
        mock_ug.distinct = AsyncMock(return_value=[3, 4])
        mock_ws.distinct = AsyncMock(return_value=[4, 5])

        total = await get_total_users_count()
        # Union of {1, 2}, {2, 3}, {3, 4}, {4, 5} is 5 unique users
        assert total == 5


@pytest.mark.asyncio
async def test_get_stats_fallback_user_calculation():
    from handlers.admin import get_stats

    update = MagicMock()
    update.effective_user.id = 99999
    update.effective_message.reply_text = AsyncMock()
    context = MagicMock()

    with patch("handlers.admin.ADMIN_ID", 99999), \
         patch("handlers.admin.codes_col") as mock_codes, \
         patch("handlers.admin.reminders_col") as mock_rems, \
         patch("handlers.admin.users_col") as mock_users, \
         patch("handlers.admin.unlocked_groups_col") as mock_ug, \
         patch("handlers.admin.web_sessions_col") as mock_ws, \
         patch("handlers.admin.group_keys_col") as mock_keys:

        mock_codes.count_documents = AsyncMock(return_value=10)
        mock_rems.count_documents = AsyncMock(return_value=5)
        mock_keys.count_documents = AsyncMock(return_value=2)
        # users_col has 0 users (e.g. no profile scraping)
        mock_users.count_documents = AsyncMock(return_value=0)
        mock_users.distinct = AsyncMock(return_value=[])
        mock_rems.distinct = AsyncMock(return_value=[101, 102])
        mock_ug.distinct = AsyncMock(return_value=[102, 103])
        mock_ws.distinct = AsyncMock(return_value=[104])

        await get_stats(update, context)

        assert update.effective_message.reply_text.awaited
        msg = update.effective_message.reply_text.call_args[0][0]
        # Should count unique users from functional collections: {101, 102, 103, 104} = 4
        assert "Unique Users:** `004`" in msg


@pytest.mark.asyncio
async def test_export_data_handles_db_disconnection():
    update = MagicMock()
    update.effective_user.id = 99999
    update.effective_message.reply_document = AsyncMock()
    context = MagicMock()

    with patch("handlers.admin.ADMIN_ID", 99999), \
         patch("handlers.admin.codes_col") as mock_codes, \
         patch("handlers.admin.reminders_col") as mock_rems, \
         patch("handlers.admin.users_col") as mock_users:

        # Database error on codes_col
        mock_codes.count_documents = AsyncMock(side_effect=RuntimeError("Codes collection timeout"))
        mock_rems.count_documents = AsyncMock(return_value=12)
        mock_users.count_documents = AsyncMock(return_value=8)

        await export_data(update, context)

        assert update.effective_message.reply_document.awaited
        kwargs = update.effective_message.reply_document.call_args[1]
        doc_bytes = kwargs["document"]
        doc_bytes.seek(0)
        payload = json.loads(doc_bytes.read().decode("utf-8"))
        assert payload["metrics"] == {"total_assets": 0, "total_reminders": 12, "total_users": 8}


def test_api_admin_stats_fallback_unique_users():
    with flask_app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_id"] = 99999
            sess["is_admin"] = True

        with patch("server.ADMIN_ID", 99999), \
             patch("server.reminders_col") as mock_r, \
             patch("server.codes_col") as mock_c, \
             patch("server.logs_col", None), \
             patch("server.group_keys_col") as mock_k, \
             patch("server.users_col") as mock_u, \
             patch("server.unlocked_groups_col") as mock_ug, \
             patch("server.web_sessions_col") as mock_ws:

            mock_r.count_documents = AsyncMock(return_value=5)
            mock_c.count_documents = AsyncMock(return_value=20)
            mock_k.count_documents = AsyncMock(return_value=1)
            # users_col has 0 count
            mock_u.count_documents = AsyncMock(return_value=0)
            # fallback distinct users
            mock_r.distinct = AsyncMock(return_value=[11, 22])
            mock_ug.distinct = AsyncMock(return_value=[22, 33])
            mock_ws.distinct = AsyncMock(return_value=[44])

            res = client.get("/api/admin/stats")
            assert res.status_code == 200
            stats = res.get_json()["stats"]
            assert stats["users"] == 4
