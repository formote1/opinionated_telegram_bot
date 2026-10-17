from motor.motor_asyncio import AsyncIOMotorClient
from config import MONGO_URI

# --- DATABASE INITIALIZATION ---
client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000) if MONGO_URI else None
db = client.telegram_bot if client is not None else None

reminders_col = db.reminders if db is not None else None
codes_col = db.saved_codes if db is not None else None
group_keys_col = db.group_keys if db is not None else None
unlocked_groups_col = db.unlocked_users if db is not None else None
logs_col = db.system_logs if db is not None else None
users_col = db.users if db is not None else None
prefix_labels_col = db.prefix_labels if db is not None else None
