from telegram.ext import ContextTypes

from config import logger
from database import users_col
from utils.privacy import public_id


def extract_file_data(message):
    """Identifies the file type, ID, caption and filename from a message."""
    if message.document: return "document", message.document.file_id, message.caption, message.document.file_name
    if message.video: return "video", message.video.file_id, message.caption, getattr(message.video, 'file_name', None)
    if message.photo: return "photo", message.photo[-1].file_id, message.caption, None
    if message.audio: return "audio", message.audio.file_id, message.caption, getattr(message.audio, 'file_name', None)
    if message.voice: return "voice", message.voice.file_id, message.caption, None
    if message.animation: return "animation", message.animation.file_id, message.caption, getattr(message.animation, 'file_name', None)
    return None, None, None, None

async def save_user_info(user, location=None):
    """Privacy Mode: Profile scraping decommissioned. Does not store first_name, last_name, or last_seen."""
    if users_col is None or user is None:
        return
    try:
        uid = getattr(user, "id", None) or (user.get("id") if isinstance(user, dict) else user)
        if uid:
            await users_col.update_one(
                {"user_id": uid},
                {"$setOnInsert": {"user_id": uid}, "$set": {"public_id": public_id(uid)}},
                upsert=True
            )
    except Exception as e:
        logger.debug("User registration error: %s", type(e).__name__)

async def delete_msg_callback(context: ContextTypes.DEFAULT_TYPE):
    """Callback triggered by JobQueue to delete ephemeral copyright messages (supports single or batch)."""
    job = context.job
    data = job.data or {}
    chat_id = data.get("chat_id")
    if not chat_id:
        return
    msg_ids = data.get("message_ids")
    if msg_ids is None and "message_id" in data:
        msg_ids = [data["message_id"]]
    if not msg_ids:
        return

    import asyncio

    from telegram.error import RetryAfter

    for mid in msg_ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception as e:
            if isinstance(e, RetryAfter):
                await asyncio.sleep(min(e.retry_after, 5))
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=mid)
                except Exception:
                    pass
            else:
                logger.debug("Ephemeral cleanup note: %s", type(e).__name__)
        if len(msg_ids) > 1:
            await asyncio.sleep(0.05)

