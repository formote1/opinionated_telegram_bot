from datetime import datetime, timezone
from telegram.ext import ContextTypes
from config import logger
from database import users_col

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
    """Saves or updates basic user info without persisting raw GPS coordinates."""
    if users_col is None: return
    try:
        update_data = {
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "last_seen": datetime.now(timezone.utc)
        }
        await users_col.update_one(
            {"user_id": user.id},
            {"$set": update_data},
            upsert=True
        )
    except Exception as e:
        logger.error(f"Error saving user info: {e}")

async def delete_msg_callback(context: ContextTypes.DEFAULT_TYPE):
    """Callback triggered by JobQueue to delete ephemeral copyright messages."""
    job = context.job
    try:
        await context.bot.delete_message(chat_id=job.data["chat_id"], message_id=job.data["message_id"])
    except Exception as e:
        logger.warning(f"Cleanup note: {e}")
