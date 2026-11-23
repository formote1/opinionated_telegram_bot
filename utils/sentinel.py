from config import ADMIN_ID, logger

# --- LOGGING & SENTINEL ---

async def log_event(user_id, username, action):
    """Privacy Mode: User activity logging decommissioned; no records persisted to database."""
    logger.debug(f"[PRIVACY-MODE] Activity event dropped without storing: user={user_id} action={action}")


async def notify_admin(context, text, silent=False):
    """Surgical administrative notifications without persisting user activity logs to MongoDB."""
    logger.info(f"[SENTINEL] {text}")
    if context and getattr(context, "bot", None) and ADMIN_ID:
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=text,
                disable_notification=silent,
            )
        except Exception as e:
            logger.debug(f"Admin notification delivery error: {e}")

