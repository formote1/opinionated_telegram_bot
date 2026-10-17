import asyncio
from datetime import datetime, timezone
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    InlineQueryHandler,
    filters,
)

from config import TOKEN, MONGO_URI, RENDER_URL, PORT, logger
from database import logs_col, users_col, reminders_col
from server import init_server, start_server_daemon

# Modular Handlers
from handlers.common import start_command
from handlers.reminders import (
    get_reminder_conversation_handler,
    list_reminders,
    handle_reminder_callback,
    schedule_reminder_job,
)
from handlers.media import (
    browse_library,
    show_alphabet_selector,
    show_filtered_library_items,
    handle_library_pick,
    handle_season_pick,
    handle_library_delivery,
    handle_next_episode_callback,
    get_file_command,
    core_routing_manager,
    inline_query_manager,
)
from handlers.admin import (
    handle_palette_callback,
    get_stats,
    export_data,
    manage_db_gui,
    handle_manage_callback,
    range_delete,
    save_message,
    auto_bulk_register,
    refresh_metadata,
    set_group_key,
    set_prefix_label,
    set_season_mapping,
    delete_season_mapping,
    clear_all_seasons,
    rename_prefix,
    grant_access_command,
    revoke_access_command,
    freeze_access_command,
    unfreeze_access_command,
    weekly_backup_job,
)
from services.ascii_art import ascii_command_handler


def create_application():
    """Assembles all handlers by category and builds the Telegram application."""
    app = ApplicationBuilder().token(TOKEN).build()

    # --- Common & Greeting Handlers ---
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("admin", start_command))

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
    app.add_handler(CommandHandler("save", save_message))
    app.add_handler(CommandHandler("del", range_delete))
    app.add_handler(CommandHandler("autobulk", auto_bulk_register))
    app.add_handler(CommandHandler("refresh", refresh_metadata))
    app.add_handler(CommandHandler("setkey", set_group_key))
    app.add_handler(CommandHandler("setlabel", set_prefix_label))
    app.add_handler(CommandHandler("setseason", set_season_mapping))
    app.add_handler(CommandHandler("delseason", delete_season_mapping))
    app.add_handler(CommandHandler("clearseasons", clear_all_seasons))
    app.add_handler(CommandHandler("rename_prefix", rename_prefix))
    app.add_handler(CommandHandler("grant", grant_access_command))
    app.add_handler(CommandHandler("revoke", revoke_access_command))
    app.add_handler(CommandHandler("freeze", freeze_access_command))
    app.add_handler(CommandHandler("unfreeze", unfreeze_access_command))
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
        start_server_daemon(port=PORT)
        while True:
            await asyncio.sleep(3600)

    main_loop = asyncio.get_running_loop()

    # Initialize MongoDB collections and indexes
    try:
        if logs_col is not None:
            await logs_col.create_index("timestamp", expireAfterSeconds=604800)
        if users_col is not None:
            await users_col.create_index("user_id", unique=True)
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

    # Load active reminders and purge past due ones
    try:
        if reminders_col is not None:
            cursor = reminders_col.find({})
            today_utc = datetime.now(timezone.utc).date()
            async for r in cursor:
                try:
                    t_date = datetime.strptime(r['target_date'], "%Y-%m-%d").date()
                    if t_date < today_utc:
                        await reminders_col.delete_one({"_id": r['_id']})
                        continue
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
