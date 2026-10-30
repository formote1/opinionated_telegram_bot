import asyncio
import weakref
from datetime import datetime, timedelta, timezone

from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    InlineQueryHandler,
    JobQueue,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

# Python 3.14 compatibility: Application lacks __weakref__ slot; provide safe fallback in JobQueue
_orig_set_application = JobQueue.set_application

def _compat_set_application(self, application):
    try:
        self._application = weakref.ref(application)
    except TypeError:
        self._application = lambda: application
    self.scheduler.configure(**self.scheduler_configuration)

JobQueue.set_application = _compat_set_application

from config import MONGO_URI, PORT, RENDER_URL, TOKEN, logger
from database import (
    codes_col,
    group_keys_col,
    logs_col,
    prefix_labels_col,
    reminders_col,
    unlocked_groups_col,
    users_col,
)
from handlers.admin import (
    export_data,
    file_command_router,
    get_stats,
    handle_manage_callback,
    handle_palette_callback,
    manage_db_gui,
    media_command_router,
    user_command_router,
    weekly_backup_job,
)

# Modular Handlers
from handlers.common import start_command, web_command
from handlers.media import (
    browse_library,
    core_routing_manager,
    get_file_command,
    handle_library_delivery,
    handle_library_pick,
    handle_next_episode_callback,
    handle_season_pick,
    inline_query_manager,
    show_alphabet_selector,
    show_filtered_library_items,
)
from handlers.reminders import (
    get_reminder_conversation_handler,
    handle_reminder_callback,
    list_reminders,
    schedule_reminder_job,
)
from server import init_server, start_server_daemon
from services.ascii_art import ascii_command_handler
from services.tmdb import close_tmdb_client


async def post_stop_cleanup(application):
    """Gracefully releases persistent network connection pools when bot stops."""
    await close_tmdb_client()


def create_application():
    """Assembles all handlers by category and builds the Telegram application."""
    request_client = HTTPXRequest(
        connection_pool_size=20,
        read_timeout=15.0,
        write_timeout=15.0,
        connect_timeout=10.0
    )
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .concurrent_updates(True)
        .request(request_client)
        .post_stop(post_stop_cleanup)
        .build()
    )

    # --- Common & Greeting Handlers ---
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("admin", start_command))
    app.add_handler(CommandHandler(["web", "login", "webapp"], web_command))

    # --- Reminder Handlers ---
    app.add_handler(CommandHandler("list", list_reminders))
    app.add_handler(CallbackQueryHandler(handle_reminder_callback, pattern="^(delrem_|remsnooze_|remdone_)"))
    app.add_handler(get_reminder_conversation_handler())

    # --- Media Retrieval & Browsing Handlers ---
    app.add_handler(CommandHandler("get", get_file_command))
    app.add_handler(CallbackQueryHandler(browse_library, pattern="^lib_back$"))
    app.add_handler(CallbackQueryHandler(show_alphabet_selector, pattern="^lib_cat_"))
    app.add_handler(CallbackQueryHandler(show_filtered_library_items, pattern="^lib_alpha_"))
    app.add_handler(CallbackQueryHandler(handle_library_pick, pattern="^lib_pick_"))
    app.add_handler(CallbackQueryHandler(handle_season_pick, pattern="^lib_sn_"))
    app.add_handler(CallbackQueryHandler(handle_library_delivery, pattern="^lib_dl_"))
    app.add_handler(CallbackQueryHandler(handle_next_episode_callback, pattern="^next_ep_"))

    # --- Admin & Management Handlers ---
    app.add_handler(CommandHandler("stats", get_stats))
    app.add_handler(CommandHandler("export", export_data))
    app.add_handler(CommandHandler("user", user_command_router))
    app.add_handler(CommandHandler("file", file_command_router))
    app.add_handler(CommandHandler("media", media_command_router))
    app.add_handler(CallbackQueryHandler(handle_palette_callback, pattern="^pal_"))
    app.add_handler(CallbackQueryHandler(manage_db_gui, pattern="^pal_manage$"))
    app.add_handler(CallbackQueryHandler(handle_manage_callback, pattern="^pref_wipe_"))

    # --- Services & Utilities ---
    app.add_handler(CommandHandler("ascii", ascii_command_handler))

    # --- Fallback Routing & Inlines ---
    app.add_handler(MessageHandler(filters.TEXT, core_routing_manager))
    app.add_handler(InlineQueryHandler(inline_query_manager))

    return app


async def main():
    """Bot lifecycle orchestrator: initializes database, webhook server, and task queues."""
    if not TOKEN or not MONGO_URI or not RENDER_URL:
        main_loop = asyncio.get_running_loop()
        init_server(None, main_loop)
        start_server_daemon(port=PORT)
        while True:
            await asyncio.sleep(3600)

    main_loop = asyncio.get_running_loop()

    # Initialize MongoDB collections and complete index coverage for fast lookups
    try:
        if logs_col is not None:
            await logs_col.create_index("timestamp", expireAfterSeconds=604800)
        if users_col is not None:
            await users_col.create_index("user_id", unique=True)
            await users_col.create_index("username")
        if codes_col is not None:
            await codes_col.create_index("code", unique=True)
            await codes_col.create_index("file_name")
            await codes_col.create_index("caption")
        if prefix_labels_col is not None:
            await prefix_labels_col.create_index("prefix", unique=True)
            await prefix_labels_col.create_index("name")
            await prefix_labels_col.create_index([("category", 1), ("name", 1)])
        if group_keys_col is not None:
            await group_keys_col.create_index("prefix")
            await group_keys_col.create_index([("chat_id", 1), ("prefix", 1)])
        if unlocked_groups_col is not None:
            await unlocked_groups_col.create_index("user_id", unique=True)
        if reminders_col is not None:
            await reminders_col.create_index([("user_id", 1), ("target_date", 1)])
    except Exception as e:
        logger.warning(f"Index creation warning: {e}")


    # Build and start Telegram Application
    application = create_application()
    init_server(application, main_loop)
    await application.initialize()
    await application.start()

    # Schedule weekly automated database backup to Admin
    try:
        if application.job_queue:
            application.job_queue.run_repeating(weekly_backup_job, interval=604800, first=86400)
    except Exception as e:
        logger.warning(f"Backup job scheduling warning: {e}")

    # Load active reminders, rolling over recurring events and purging past-due one-offs
    try:
        if reminders_col is not None:
            cursor = reminders_col.find({})
            today_utc = datetime.now(timezone.utc).date()
            async for r in cursor:
                try:
                    t_date = datetime.strptime(r['target_date'], "%Y-%m-%d").date()
                    if t_date < today_utc:
                        rec = r.get("recurrence", "once")
                        if rec == "once":
                            await reminders_col.delete_one({"_id": r['_id']})
                            continue
                        else:
                            cat_check = r.get("category", "")
                            rollover_days = {"daily": 1, "weekly": 7, "monthly": 30, "annually": 365}.get(
                                rec, 365 if cat_check == "Birthday" else 7
                            )
                            new_date = t_date
                            while new_date < today_utc:
                                new_date += timedelta(days=rollover_days)
                            new_target = new_date.strftime("%Y-%m-%d")
                            await reminders_col.update_one({"_id": r['_id']}, {"$set": {"target_date": new_target, "last_notified": ""}})
                            r['target_date'] = new_target
                    schedule_reminder_job(application, r)
                except Exception:
                    schedule_reminder_job(application, r)
    except Exception as e:
        logger.warning(f"Reminder recovery warning: {e}")

    # Register Telegram webhook
    try:
        await application.bot.set_webhook(url=f"{RENDER_URL.rstrip('/')}/{TOKEN}")
    except Exception as e:
        logger.warning(f"Webhook setup warning: {e}")

    # Launch Flask Webhook & Health server in daemon thread
    start_server_daemon(port=PORT)

    # Keep async main loop active
    while True:
        await asyncio.sleep(3600)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as e:
        logger.critical(f"FATAL: {e}")
