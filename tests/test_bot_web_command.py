from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from handlers.common import web_command


@pytest.mark.asyncio
async def test_bot_web_command_generates_valid_link():
    update = MagicMock()
    user = MagicMock()
    user.id = 55555
    user.username = "charlie"
    update.effective_user = user
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()

    context = MagicMock()

    with patch("handlers.common.save_user_info", new=AsyncMock()):
        await web_command(update, context)

    assert update.message.reply_text.awaited
    call_args = update.message.reply_text.call_args
    text = call_args[0][0] if call_args[0] else call_args[1].get("text", "")
    reply_markup = call_args[1].get("reply_markup")

    assert "Web Companion Access" in text
    assert reply_markup is not None
    # Inspect button URLs
    buttons = reply_markup.inline_keyboard
    assert len(buttons) >= 1
    # Check that the URL contains /login?token=
    url_found = any(
        btn.url and "/login?token=" in btn.url for row in buttons for btn in row
    ) or any(
        btn.web_app and "/login?token=" in btn.web_app.url for row in buttons for btn in row
    )
    assert url_found


def test_api_media_deliver_endpoint():
    from server import flask_app
    with flask_app.test_client() as client:
        # 1. Unauthenticated -> 401
        res_unauth = client.post("/api/media/deliver", json={"code": "LOK001"})
        assert res_unauth.status_code == 401

        # 2. Authenticated
        with client.session_transaction() as sess:
            sess["user_id"] = 12345
            sess["username"] = "deliver_user"

        with patch("server.codes_col") as mock_codes, \
             patch("server.is_user_banned", new=AsyncMock(return_value=False)), \
             patch("server.check_access_gate", new=AsyncMock(return_value=(True, "LOK", 100, False, False))), \
             patch("server.execute_file_delivery", new=AsyncMock(return_value=MagicMock())):

            mock_codes.find_one = AsyncMock(return_value={"code": "LOK001", "file_type": "video", "file_id": "v1"})

            # Simulate bot application attached
            mock_app = MagicMock()
            mock_app.bot = MagicMock()
            mock_app.job_queue = MagicMock()
            with patch("server._application", mock_app):
                res = client.post("/api/media/deliver", json={"code": "LOK001"})
                assert res.status_code == 200
                data = res.get_json()
                assert data["success"] is True
                assert "delivered to your Telegram chat" in data["message"]
