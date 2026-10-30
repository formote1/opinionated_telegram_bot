from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.access import check_access_gate
from utils.helpers import delete_msg_callback, extract_file_data


@pytest.mark.asyncio
async def test_check_access_gate_admin():
    from config import ADMIN_ID
    # Admin is always authorized
    authorized, prefix, chat_id, frozen, expired = await check_access_gate(
        ADMIN_ID, {"code": "LOK001", "chat_id": 12345}
    )
    assert authorized is True
    assert prefix == "LOK"
    assert chat_id == 12345
    assert not frozen
    assert not expired

@pytest.mark.asyncio
async def test_check_access_gate_no_prefix():
    authorized, prefix, _chat_id, _frozen, _expired = await check_access_gate(
        1234, {"code": "", "chat_id": 12345}
    )
    assert authorized is True
    assert prefix == ""

@pytest.mark.asyncio
async def test_delete_msg_callback_batch():
    bot = AsyncMock()
    context = MagicMock()
    context.bot = bot

    # Batch messages
    context.job.data = {"chat_id": 999, "message_ids": [101, 102, 103]}
    await delete_msg_callback(context)
    assert bot.delete_message.await_count == 3

    # Single message
    bot.reset_mock()
    context.job.data = {"chat_id": 999, "message_id": 104}
    await delete_msg_callback(context)
    assert bot.delete_message.await_count == 1
    bot.delete_message.assert_awaited_with(chat_id=999, message_id=104)

    # Empty / None data
    bot.reset_mock()
    context.job.data = None
    await delete_msg_callback(context)
    assert bot.delete_message.await_count == 0

def test_extract_file_data():
    msg = MagicMock()
    msg.document = None
    msg.video = None
    msg.photo = None
    msg.audio = None
    msg.voice = None
    msg.animation = None

    # No media
    f_type, f_id, cap, name = extract_file_data(msg)
    assert f_type is None
    assert f_id is None

    # Document
    doc = MagicMock()
    doc.file_id = "doc123"
    doc.file_name = "test.pdf"
    msg.document = doc
    msg.caption = "my doc"
    f_type, f_id, cap, name = extract_file_data(msg)
    assert f_type == "document"
    assert f_id == "doc123"
    assert cap == "my doc"
    assert name == "test.pdf"


@pytest.mark.asyncio
async def test_delete_msg_callback_retry_after():
    from telegram.error import RetryAfter
    bot = AsyncMock()
    context = MagicMock()
    context.bot = bot
    context.job.data = {"chat_id": 999, "message_ids": [101]}

    # First call raises RetryAfter, second succeeds
    bot.delete_message = AsyncMock(side_effect=[RetryAfter(0.01), None])
    await delete_msg_callback(context)
    assert bot.delete_message.await_count == 2


def test_python_314_jobqueue_weakref_patch():
    from telegram.ext import JobQueue

    import bot  # noqa: F401
    jq = JobQueue()

    class NonWeakrefObject:
        __slots__ = ('bot',)
        def __init__(self):
            self.bot = None

    obj = NonWeakrefObject()
    # Should not raise TypeError: cannot create weak reference to object
    jq.set_application(obj)
    assert jq.application is obj


@pytest.mark.asyncio
async def test_resolve_target_user_indexed():
    from handlers.admin import resolve_target_user
    with patch("handlers.admin.users_col") as mock_col:
        mock_col.find_one = AsyncMock(return_value={"user_id": 7777, "username": "alice"})
        uid, uname = await resolve_target_user("@alice")
        assert uid == 7777
        assert uname == "@alice"
        # Verify direct indexed query was issued
        mock_col.find_one.assert_called_with({"username": "alice"})


def test_render_flf_text_multiline():
    from services.ascii_art import FLF_FONT_PATH, render_flf_text
    res = render_flf_text("AB\nCD", FLF_FONT_PATH)
    assert res is not None
    assert "\n" in res
