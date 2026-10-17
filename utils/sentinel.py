from datetime import datetime, timezone
from config import ADMIN_ID, logger
from database import logs_col

# --- LOGGING & SENTINEL ---

async def log_event(user_id, username, action):
    if logs_col is None: return
    try:
        await logs_col.insert_one({
            "timestamp": datetime.now(timezone.utc),
            "user_id": user_id,
            "username": username or "Unknown",
            "action": action
        })
    except Exception as e:
        logger.error(f"Logging error: {e}")

async def notify_admin(context, text, silent=False):
    """Surgical notification system - Merged to internal logs as requested."""
    await log_event(ADMIN_ID, "SENTINEL", text)
