import re

from config import ADMIN_ID, logger

# --- LOGGING & SENTINEL ---

_PII_MARKERS = re.compile(r"(?i)\b(user|username|label|code|passkey|secret|fetched|banned|granted|revoked)\b")
_LONG_NUMBER = re.compile(r"\b\d{6,}\b")
_MENTION = re.compile(r"@\w+")


def _is_anonymous(text: str) -> bool:
    """Only pre-approved, identifier-free system events may reach the admin chat."""
    if not isinstance(text, str) or not text:
        return False
    if _PII_MARKERS.search(text) or _LONG_NUMBER.search(text) or _MENTION.search(text):
        return False
    if "`" in text:
        return False
    return True


async def log_event(user_id, username, action):
    """Privacy Mode: activity events are dropped. No identifier is written or logged."""
    logger.debug("[PRIVACY-MODE] Activity event dropped without storing user data")


async def notify_admin(context, text, silent=False):
    """Sends only anonymous system-health notices; user-linked details are discarded."""
    if not _is_anonymous(text):
        logger.debug("[SENTINEL] User-linked notice discarded by privacy guard")
        return
    logger.info("[SENTINEL] %s", text)
    if context and getattr(context, "bot", None) and ADMIN_ID:
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=text,
                disable_notification=silent,
            )
        except Exception as e:
            logger.debug("Admin notification delivery error: %s", type(e).__name__)
