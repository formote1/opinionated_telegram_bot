import calendar
import html
from datetime import datetime, time, timedelta

import pytz
from bson import ObjectId
from bson.errors import InvalidId
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import logger
from database import reminders_col
from utils.sentinel import log_event, notify_admin
from utils.timezones import tf

# Conversation States
GET_TZ_CHOICE, GET_DATE, GET_TIME, GET_LABEL, GET_BIRTHDAY_NAME = range(5)

# Deduplication state to prevent concurrent double-dispatches
_active_reminder_dispatches = set()

def format_time_left(days_left: int) -> str:
    if days_left == 0:
        return "today"
    elif days_left == 1:
        return "1 day"
    elif days_left == -1:
        return "yesterday"
    elif days_left < 0:
        return f"{abs(days_left)} days ago"
    months = days_left // 30
    rem_days = days_left % 30
    weeks = rem_days // 7
    days = rem_days % 7
    parts = []
    if months > 0: parts.append(f"{months} month{'s' if months > 1 else ''}")
    if weeks > 0: parts.append(f"{weeks} week{'s' if weeks > 1 else ''}")
    if days > 0: parts.append(f"{days} day{'s' if days > 1 else ''}")
    return ", ".join(parts) if parts else f"{days_left} days"


# --- GUI BUILDERS ---

def create_calendar(year=None, month=None):
    now = datetime.now()
    if year is None: year = now.year
    if month is None: month = now.month

    markup = []
    markup.append([
        InlineKeyboardButton(calendar.month_name[month], callback_data=f"cal_view_months_{year}"),
        InlineKeyboardButton(str(year), callback_data=f"cal_view_years_{month}")
    ])
    markup.append([InlineKeyboardButton(d, callback_data="ignore") for d in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]])

    month_calendar = calendar.monthcalendar(year, month)
    for week in month_calendar:
        row = []
        for day in week:
            if day == 0: row.append(InlineKeyboardButton(" ", callback_data="ignore"))
            else:
                cb_data = f"date_sel_{year}-{month:02d}-{day:02d}"
                row.append(InlineKeyboardButton(str(day), callback_data=cb_data))
        markup.append(row)

    prev_m = month - 1 if month > 1 else 12
    prev_y = year if month > 1 else year - 1
    next_m = month + 1 if month < 12 else 1
    next_y = year if month < 12 else year + 1

    markup.append([
        InlineKeyboardButton("<<", callback_data=f"cal_nav_{prev_y}_{prev_m}"),
        InlineKeyboardButton(">>", callback_data=f"cal_nav_{next_y}_{next_m}")
    ])
    return InlineKeyboardMarkup(markup)


def create_month_grid(year):
    markup = []
    for i in range(1, 13, 3):
        row = [InlineKeyboardButton(calendar.month_name[m][:3], callback_data=f"cal_set_m_{year}_{m}") for m in range(i, i + 3)]
        markup.append(row)
    markup.append([InlineKeyboardButton("🔙 Back to Days", callback_data=f"cal_nav_{year}_{datetime.now().month}")])
    return InlineKeyboardMarkup(markup)


def create_year_grid(month):
    current_year = datetime.now().year
    markup = []
    for y_start in range(current_year, current_year + 6, 3):
        row = [InlineKeyboardButton(str(y), callback_data=f"cal_set_y_{y}_{month}") for y in range(y_start, y_start + 3)]
        markup.append(row)
    markup.append([InlineKeyboardButton("🔙 Back to Days", callback_data=f"cal_nav_{current_year}_{month}")])
    return InlineKeyboardMarkup(markup)


def create_label_buttons():
    labels = ["🎂 Birthday", "✈️ Travel", "🚀 Launch", "💳 Payment", "💊 Medication", "🏆 Deadline"]
    markup = []
    for i in range(0, len(labels), 2):
        row = [InlineKeyboardButton(labels[i], callback_data=f"label_sel_{labels[i]}")]
        if i + 1 < len(labels):
            row.append(InlineKeyboardButton(labels[i + 1], callback_data=f"label_sel_{labels[i + 1]}"))
        markup.append(row)
    return InlineKeyboardMarkup(markup)


# --- SCHEDULER & JOBS ---

def schedule_reminder_job(app, reminder_data):
    try:
        if not app or not app.job_queue:
            return
        user_id = reminder_data.get('user_id')
        if not user_id:
            return
        r_time_str = reminder_data.get('reminder_time', "09:00")
        hour, minute = map(int, r_time_str.split(':'))
        tz_name = reminder_data.get('timezone', 'UTC')
        try:
            tz = pytz.timezone(tz_name)
        except Exception:
            tz = pytz.UTC
        reminder_time = time(hour=hour, minute=minute, tzinfo=tz)
        job_name = f"user_rem_{user_id}_{hour:02d}{minute:02d}"

        # Clean up legacy per-reminder job if exists
        if '_id' in reminder_data:
            for legacy_job in app.job_queue.get_jobs_by_name(str(reminder_data['_id'])):
                legacy_job.schedule_removal()

        # Check if daily consolidated job for this user and time is already scheduled
        existing_jobs = app.job_queue.get_jobs_by_name(job_name)
        if existing_jobs:
            return

        app.job_queue.run_daily(
            send_daily_reminder,
            time=reminder_time,
            chat_id=user_id,
            data={
                "user_id": user_id,
                "timezone": tz_name,
                "reminder_time": r_time_str
            },
            name=job_name
        )

    except Exception as e:
        logger.error(f"Scheduling error: {e}")


async def send_daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    data = job.data
    user_id = data.get("user_id") or job.chat_id
    if reminders_col is None:
        return

    tz_name = data.get('timezone', 'UTC')
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.UTC

    today_local = datetime.now(tz).date()
    today_str = today_local.strftime("%Y-%m-%d")

    lock_key = (user_id, today_str)
    if lock_key in _active_reminder_dispatches:
        logger.info(f"Reminders for user {user_id} already being dispatched today ({today_str}).")
        return
    _active_reminder_dispatches.add(lock_key)

    try:
        user_reminders = await reminders_col.find({"user_id": user_id}).to_list(length=100)
        if not user_reminders:
            job.schedule_removal()
            return

        due_by_category = {}

        for r in user_reminders:
            r_id = r['_id']
            try:
                t_date = datetime.strptime(r['target_date'], "%Y-%m-%d").date()
            except Exception:
                continue

            days_left = (t_date - today_local).days

            # 1. Clean up or roll over past-due reminders
            if days_left < 0:
                rec = r.get("recurrence", "once")
                if rec == "once":
                    await reminders_col.delete_one({"_id": r_id})
                    if context.job_queue:
                        for j in context.job_queue.get_jobs_by_name(str(r_id)):
                            j.schedule_removal()
                else:
                    cat_check = r.get("category", "")
                    rollover_days = {"daily": 1, "weekly": 7, "monthly": 30, "annually": 365}.get(
                        rec, 365 if cat_check == "Birthday" else 7
                    )
                    new_target = (t_date + timedelta(days=rollover_days)).strftime("%Y-%m-%d")
                    await reminders_col.update_one({"_id": r_id}, {"$set": {"target_date": new_target, "last_notified": ""}})
                continue

            # 2. Windowing: skip dormant reminders (> 90 days away)
            if days_left > 90:
                continue

            # 3. Deduplicate: skip if this reminder was already notified today
            if r.get("last_notified") == today_str:
                continue

            # Atomically claim notification for today to prevent duplicate sends across concurrent executions
            claim_res = await reminders_col.update_one(
                {"_id": r_id, "last_notified": {"$ne": today_str}},
                {"$set": {"last_notified": today_str}}
            )
            if claim_res.modified_count == 0:
                continue

            # 4. Resolve category
            cat = r.get("category")
            if not cat:
                lbl = str(r.get("label", ""))
                if "birthday" in lbl.lower() or "🎂" in lbl:
                    cat = "Birthday"
                else:
                    cat = "General"

            due_by_category.setdefault(cat, []).append((r, days_left))

        # --- A. Deliver Merged Birthday Reminders ---
        if "Birthday" in due_by_category:
            birthday_items = due_by_category["Birthday"]
            birthday_items.sort(key=lambda x: x[1])

            header = "🎂 <b>BIRTHDAY REMINDERS</b>" if len(birthday_items) > 1 else "🎂 <b>BIRTHDAY REMINDER</b>"
            lines = [header, "───────────────────────────"]
            kb_rows = []

            for idx, (b_rec, d_left) in enumerate(birthday_items, 1):
                b_id = b_rec['_id']
                raw_label = str(b_rec.get('label', 'Birthday'))
                escaped_label = html.escape(raw_label)

                if d_left == 0:
                    event_str = f"🎉 <b>TODAY: {escaped_label}!</b>"
                elif d_left == 1:
                    event_str = f"🔔 <b>Tomorrow</b>: {escaped_label} (<code>{b_rec['target_date']}</code>)"
                else:
                    time_str = format_time_left(d_left)
                    event_str = f"🔔 <b>In {time_str}</b>: {escaped_label} (<code>{b_rec['target_date']}</code>)"

                if len(birthday_items) > 1:
                    num_prefix = f"{idx}️⃣ " if 1 <= idx <= 9 else f"#{idx} "
                    lines.append(f"{num_prefix}{event_str}")
                    kb_rows.append([
                        InlineKeyboardButton(f"💤 Snooze #{idx}", callback_data=f"remsnooze_{b_id}"),
                        InlineKeyboardButton(f"✅ Done #{idx}", callback_data=f"remdone_{b_id}")
                    ])
                else:
                    lines.append(event_str)
                    kb_rows.append([
                        InlineKeyboardButton("💤 Snooze (1 Day)", callback_data=f"remsnooze_{b_id}"),
                        InlineKeyboardButton("✅ Mark Done", callback_data=f"remdone_{b_id}")
                    ])

            msg_text = "\n".join(lines)
            await context.bot.send_message(
                chat_id=user_id,
                text=msg_text,
                reply_markup=InlineKeyboardMarkup(kb_rows) if kb_rows else None,
                parse_mode="HTML"
            )
            await notify_admin(context, f"🎂 Consolidated Birthday Reminders sent to `{user_id}` ({len(birthday_items)} birthdays).")

        # --- B. Deliver Other Reminders Segregated by Category ---
        category_icons = {
            "Travel": "✈️", "Launch": "🚀", "Payment": "💳",
            "Medication": "💊", "Deadline": "🏆", "General": "⏰"
        }

        for cat, items in due_by_category.items():
            if cat == "Birthday":
                continue

            items.sort(key=lambda x: x[1])
            icon = category_icons.get(cat, "🔔")
            header = f"{icon} <b>{html.escape(cat.upper())} REMINDERS</b>" if len(items) > 1 else f"{icon} <b>{html.escape(cat.upper())} REMINDER</b>"
            lines = [header, "───────────────────────────"]
            kb_rows = []

            for idx, (r_rec, d_left) in enumerate(items, 1):
                r_id = r_rec['_id']
                raw_label = str(r_rec.get('label', cat))
                escaped_label = html.escape(raw_label)

                if d_left == 0:
                    event_str = f"🎉 <b>TODAY: {escaped_label}!</b>"
                elif d_left == 1:
                    event_str = f"🔔 <b>Tomorrow</b>: {escaped_label} (<code>{r_rec['target_date']}</code>)"
                else:
                    time_str = format_time_left(d_left)
                    event_str = f"🔔 <b>In {time_str}</b>: {escaped_label} (<code>{r_rec['target_date']}</code>)"

                if len(items) > 1:
                    num_prefix = f"{idx}️⃣ " if 1 <= idx <= 9 else f"#{idx} "
                    lines.append(f"{num_prefix}{event_str}")
                    kb_rows.append([
                        InlineKeyboardButton(f"💤 Snooze #{idx}", callback_data=f"remsnooze_{r_id}"),
                        InlineKeyboardButton(f"✅ Done #{idx}", callback_data=f"remdone_{r_id}")
                    ])
                else:
                    lines.append(event_str)
                    kb_rows.append([
                        InlineKeyboardButton("💤 Snooze (1 Day)", callback_data=f"remsnooze_{r_id}"),
                        InlineKeyboardButton("✅ Mark Done", callback_data=f"remdone_{r_id}")
                    ])

            msg_text = "\n".join(lines)
            await context.bot.send_message(
                chat_id=user_id,
                text=msg_text,
                reply_markup=InlineKeyboardMarkup(kb_rows) if kb_rows else None,
                parse_mode="HTML"
            )
            await notify_admin(context, f"🔔 Reminders [{cat}] sent to `{user_id}` ({len(items)} items).")

    except Exception as e:
        logger.error(f"Reminder delivery error: {e}")
        await notify_admin(context, f"🔴 REMINDER FAILURE for User `{user_id}`: {e}")
    finally:
        _active_reminder_dispatches.discard(lock_key)



# --- HANDLERS ---

async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if reminders_col is None: return
    user_id = update.effective_user.id
    try:
        cursor = reminders_col.find({"user_id": user_id}).sort("target_date", 1)
        reminders = await cursor.to_list(length=50)
        if not reminders: return await update.effective_message.reply_text("No active reminders.")

        tz_cache = {}
        current_lines = ["📜 <b>YOUR ACTIVE REMINDERS</b>\n───────────────────────────"]
        current_kb = []
        current_len = len(current_lines[0])

        for idx, r in enumerate(reminders, 1):
            r_id = str(r['_id'])
            raw_label = str(r.get('label', 'No Label'))
            label_esc = html.escape(raw_label)
            target_date_str = r.get('target_date', '')
            tz_name = r.get('timezone', 'UTC')
            if tz_name not in tz_cache:
                try: tz_cache[tz_name] = pytz.timezone(tz_name)
                except Exception: tz_cache[tz_name] = pytz.UTC
            user_tz = tz_cache[tz_name]

            try:
                now_local = datetime.now(user_tz).date()
                target_dt = datetime.strptime(target_date_str, "%Y-%m-%d").date()
                days_left = (target_dt - now_local).days
            except Exception:
                days_left = 0

            status_icon = "🟢" if days_left <= 90 else "💤"
            rec = r.get('recurrence', 'once')
            rec_tag = f" (🔄 {html.escape(rec.capitalize())})" if rec != 'once' else ""
            cat_display = html.escape(r.get('category', 'General'))
            dormant_note = f" (starts in {days_left - 90}d)" if days_left > 90 else ""

            entry_text = (
                f"{idx}️⃣ {status_icon} <b>{label_esc}</b> [{cat_display}]\n"
                f"    📅 <code>{target_date_str}</code> | ⏰ <code>{r.get('reminder_time', '09:00')}</code> ({days_left}d left{dormant_note}){rec_tag}"
            )
            btn_text = f"🗑️ Del #{idx} ({raw_label[:14]})" if len(raw_label) <= 14 else f"🗑️ Del #{idx} ({raw_label[:12]}...)"
            btn_row = [InlineKeyboardButton(btn_text, callback_data=f"delrem_{r_id}")]

            if (current_len + len(entry_text) > 3500) or len(current_kb) >= 25:
                await update.effective_message.reply_text(
                    "\n".join(current_lines),
                    reply_markup=InlineKeyboardMarkup(current_kb) if current_kb else None,
                    parse_mode="HTML"
                )
                current_lines = ["📜 <b>YOUR ACTIVE REMINDERS (Cont.)</b>\n───────────────────────────"]
                current_kb = []
                current_len = len(current_lines[0])

            current_lines.append(entry_text)
            current_kb.append(btn_row)
            current_len += len(entry_text) + 1

        if current_lines:
            await update.effective_message.reply_text(
                "\n".join(current_lines),
                reply_markup=InlineKeyboardMarkup(current_kb) if current_kb else None,
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"List error: {e}")
        await update.effective_message.reply_text("❌ Failed to list reminders.")


async def handle_reminder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles delete, snooze, and mark-done actions on reminders."""
    query = update.callback_query
    user_id = update.effective_user.id
    data_str = query.data

    if data_str.startswith("delrem_"):
        r_id = data_str.split('_')[1]
        if reminders_col is not None:
            try:
                oid = ObjectId(r_id)
                res = await reminders_col.delete_one({"_id": oid, "user_id": user_id})
                if res.deleted_count > 0:
                    if context.job_queue:
                        for job in context.job_queue.get_jobs_by_name(str(r_id)):
                            job.schedule_removal()
                        remaining = await reminders_col.count_documents({"user_id": user_id})
                        if remaining == 0:
                            for job in context.job_queue.jobs():
                                if job.name and job.name.startswith(f"user_rem_{user_id}_"):
                                    job.schedule_removal()

                    kb = query.message.reply_markup.inline_keyboard if query.message and query.message.reply_markup else []
                    if len(kb) > 1:
                        new_kb = [row for row in kb if not any(r_id in btn.callback_data for btn in row)]
                        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_kb) if new_kb else None)
                        await query.answer("🗑️ Reminder deleted!", show_alert=True)
                    else:
                        await query.edit_message_text("❌ Reminder deleted.")
                    await notify_admin(context, f"🔔 User `{user_id}` deleted a reminder.")
                else:
                    await query.answer("Reminder not found or permission denied.", show_alert=True)
            except InvalidId:
                await query.answer("Invalid reminder ID.", show_alert=True)
        return await query.answer()


    elif data_str.startswith("remsnooze_"):
        r_id = data_str.split('_')[1]
        if reminders_col is not None:
            try:
                oid = ObjectId(r_id)
                rec = await reminders_col.find_one({"_id": oid, "user_id": user_id})
                if rec:
                    curr_date = datetime.strptime(rec['target_date'], "%Y-%m-%d")
                    new_date = (curr_date + timedelta(days=1)).strftime("%Y-%m-%d")
                    await reminders_col.update_one({"_id": oid}, {"$set": {"target_date": new_date, "last_notified": ""}})

                    # Reschedule job with updated date
                    rec["target_date"] = new_date
                    if context.application:
                        schedule_reminder_job(context.application, rec)

                    label_raw = str(rec.get('label', 'Reminder'))
                    label_esc = html.escape(label_raw)
                    kb = query.message.reply_markup.inline_keyboard if query.message and query.message.reply_markup else []
                    if len(kb) > 1:
                        new_kb = [row for row in kb if not any(r_id in btn.callback_data for btn in row)]
                        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_kb) if new_kb else None)
                        return await query.answer(f"💤 Snoozed '{label_raw}' by 1 day ({new_date})!", show_alert=True)
                    else:
                        await query.edit_message_text(f"💤 <b>Snoozed 1 Day</b>\nNew alert date: <code>{new_date}</code> for '<b>{label_esc}</b>'", parse_mode="HTML")
                        return await query.answer("💤 Snoozed by 1 day!")
                else:
                    return await query.answer("Reminder not found.", show_alert=True)
            except Exception as e:
                logger.error(f"Snooze error: {e}")
                return await query.answer("Error processing snooze.", show_alert=True)

    elif data_str.startswith("remdone_"):
        r_id = data_str.split('_')[1]
        if reminders_col is not None:
            try:
                oid = ObjectId(r_id)
                rec = await reminders_col.find_one({"_id": oid, "user_id": user_id})
                if rec:
                    recurrence = rec.get("recurrence", "once")
                    label_raw = str(rec.get('label', 'Reminder'))
                    label_esc = html.escape(label_raw)
                    if recurrence != "once":
                        rollover_days = {"daily": 1, "weekly": 7, "monthly": 30, "annually": 365}.get(
                            recurrence, 365 if rec.get("category") == "Birthday" else 7
                        )
                        curr_date = datetime.strptime(rec['target_date'], "%Y-%m-%d")
                        new_date = (curr_date + timedelta(days=rollover_days)).strftime("%Y-%m-%d")
                        await reminders_col.update_one({"_id": oid}, {"$set": {"target_date": new_date, "last_notified": ""}})
                        rec["target_date"] = new_date
                        if context.application:
                            schedule_reminder_job(context.application, rec)

                        kb = query.message.reply_markup.inline_keyboard if query.message and query.message.reply_markup else []
                        if len(kb) > 1:
                            new_kb = [row for row in kb if not any(r_id in btn.callback_data for btn in row)]
                            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_kb) if new_kb else None)
                            return await query.answer(f"🎉 Completed! Rolled over to {new_date} ({recurrence.capitalize()}).", show_alert=True)
                        else:
                            await query.edit_message_text(f"🎉 <b>Completed!</b>\nRolled over to <code>{new_date}</code> (<b>{html.escape(recurrence.capitalize())}</b>).", parse_mode="HTML")
                            return await query.answer("✅ Rolled over to next interval!")
                    else:
                        await reminders_col.delete_one({"_id": oid})
                        if context.job_queue:
                            for job in context.job_queue.get_jobs_by_name(str(r_id)):
                                job.schedule_removal()
                            remaining = await reminders_col.count_documents({"user_id": user_id})
                            if remaining == 0:
                                for job in context.job_queue.jobs():
                                    if job.name and job.name.startswith(f"user_rem_{user_id}_"):
                                        job.schedule_removal()

                        kb = query.message.reply_markup.inline_keyboard if query.message and query.message.reply_markup else []
                        if len(kb) > 1:
                            new_kb = [row for row in kb if not any(r_id in btn.callback_data for btn in row)]
                            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_kb) if new_kb else None)
                            return await query.answer(f"✅ Cleared reminder for '{label_raw}'!", show_alert=True)
                        else:
                            await query.edit_message_text(f"✅ <b>Marked Completed!</b>\nReminder for '<b>{label_esc}</b>' has been cleared.", parse_mode="HTML")
                            return await query.answer("✅ Reminder cleared!")
                else:
                    return await query.answer("Reminder not found.", show_alert=True)
            except Exception as e:
                logger.error(f"Mark Done error: {e}")
                return await query.answer("Error completing reminder.", show_alert=True)

    await query.answer()


# --- CONVERSATION FLOW ---

async def start_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    kb = [["🇺🇿 Tashkent/Uzbekistan"], [KeyboardButton("📍 Share Location", request_location=True)]]
    await update.effective_message.reply_text("🕒 **Step 1: Timezone**", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True), parse_mode="Markdown")
    return GET_TZ_CHOICE


async def handle_tz_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz_str = "Asia/Tashkent"
    if update.message.location:
        lat, lon = update.message.location.latitude, update.message.location.longitude
        found_tz = tf.timezone_at(lng=lon, lat=lat) if tf else None
        if found_tz: tz_str = found_tz
    context.user_data['timezone'] = tz_str
    await update.message.reply_text(f"✅ Zone: `{tz_str}`", reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
    await update.message.reply_text("📅 **Step 2: Pick Date**", reply_markup=create_calendar(), parse_mode="Markdown")
    return GET_DATE


async def date_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "ignore": return
    elif data.startswith("cal_nav_"):
        parts = data.split('_')
        y, m = int(parts[2]), int(parts[3])
        await query.edit_message_reply_markup(create_calendar(year=y, month=m))
    elif data.startswith("cal_view_months_"):
        year = int(data.split('_')[3])
        await query.edit_message_reply_markup(create_month_grid(year))
    elif data.startswith("cal_view_years_"):
        month = int(data.split('_')[3])
        await query.edit_message_reply_markup(create_year_grid(month))
    elif data.startswith("cal_set_m_"):
        parts = data.split('_')
        y, m = int(parts[3]), int(parts[4])
        await query.edit_message_reply_markup(create_calendar(year=y, month=m))
    elif data.startswith("cal_set_y_"):
        parts = data.split('_')
        y, m = int(parts[3]), int(parts[4])
        await query.edit_message_reply_markup(create_calendar(year=y, month=m))
    elif data.startswith("date_sel_"):
        selected_date = data.split('_')[2]
        context.user_data['target_date'] = selected_date
        context.user_data['reminder_time'] = "09:00"
        await query.edit_message_text(f"📅 Target Date: `{selected_date}`", parse_mode="Markdown")
        await query.message.reply_text("🏷️ **Step 3: Choose Label or Type One**", reply_markup=create_label_buttons(), parse_mode="Markdown")
        return GET_LABEL


async def label_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    raw_label = query.data.replace("label_sel_", "")

    if "birthday" in raw_label.lower() or "🎂" in raw_label:
        context.user_data['category'] = "Birthday"
        context.user_data['recurrence'] = "annually"
        await query.edit_message_text(
            "🎂 **Whose birthday is this?**\n\nPlease enter the person's name or title (e.g., `Mom's Birthday`, `Alice`, `Best Friend`):",
            parse_mode="Markdown"
        )
        return GET_BIRTHDAY_NAME

    cat_map = {
        "✈️ Travel": "Travel",
        "🚀 Launch": "Launch",
        "💳 Payment": "Payment",
        "💊 Medication": "Medication",
        "🏆 Deadline": "Deadline"
    }
    context.user_data['category'] = cat_map.get(raw_label, "General")
    context.user_data['label'] = raw_label
    return await finish_reminder(query.message, context)


async def handle_birthday_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    context.user_data['label'] = name
    context.user_data['category'] = "Birthday"
    context.user_data['recurrence'] = context.user_data.get('recurrence', 'annually')
    return await finish_reminder(update.message, context)


async def handle_label_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data['label'] = text
    if "birthday" in text.lower() or "🎂" in text:
        context.user_data['category'] = "Birthday"
        context.user_data['recurrence'] = "annually"
    else:
        context.user_data['category'] = "General"
    return await finish_reminder(update.message, context)


async def finish_reminder(message, context):
    user_id = message.chat_id
    try:
        if reminders_col is None: return ConversationHandler.END

        req = ['timezone', 'target_date', 'reminder_time', 'label']
        if not all(k in context.user_data for k in req):
            await message.reply_text("❌ Session error. Try /remind again.")
            context.user_data.clear()
            return ConversationHandler.END

        category = context.user_data.get('category')
        if not category:
            lbl = context.user_data.get('label', '')
            category = "Birthday" if ("birthday" in lbl.lower() or "🎂" in lbl) else "General"

        recurrence = context.user_data.get('recurrence')
        if not recurrence:
            recurrence = "annually" if category == "Birthday" else "once"

        data = {
            "user_id": user_id,
            "timezone": context.user_data['timezone'],
            "target_date": context.user_data['target_date'],
            "reminder_time": context.user_data['reminder_time'],
            "label": context.user_data['label'],
            "category": category,
            "recurrence": recurrence,
            "last_notified": ""
        }
        res = await reminders_col.insert_one(data)
        data['_id'] = res.inserted_id

        if context.application: schedule_reminder_job(context.application, data)

        target_dt = datetime.strptime(data['target_date'], "%Y-%m-%d").date()
        user_tz = pytz.timezone(data['timezone'])
        now_dt = datetime.now(user_tz).date()
        days_away = (target_dt - now_dt).days

        if days_away > 90:
            status_note = f"💤 *Event is {days_away} days away (>3 months). Active daily notifications will automatically begin 90 days prior to the date.*"
        else:
            status_note = f"🔔 *Active countdown engaged! ({days_away} days remaining)*"

        rec_info = f" (🔄 {recurrence.capitalize()})" if recurrence != "once" else ""
        success_text = f"🚀 **Reminder Armed!**\n\n🏷️ `{data['label']}` [{category}]\n📅 `{data['target_date']}`{rec_info}\n\n{status_note}"

        if message.from_user and message.from_user.is_bot:
            try: await message.edit_text(success_text, parse_mode="Markdown")
            except: await message.reply_text(success_text, parse_mode="Markdown")
        else:
            await message.reply_text(success_text, parse_mode="Markdown")

        await log_event(user_id, "User", f"Set Reminder: {data['label']} [{category}]")
        await notify_admin(context, f"📅 User `{user_id}` armed a [{category}] reminder for `{data['label']}`.")
        context.user_data.clear()
        return ConversationHandler.END

    except Exception as e:
        logger.error(f"Finish Error: {e}")
        context.user_data.clear()
        return ConversationHandler.END


async def cancel_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancels the current reminder creation conversation."""
    context.user_data.clear()
    if update.effective_message:
        await update.effective_message.reply_text("🚫 Reminder setup cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


def get_reminder_conversation_handler():
    """Builds and returns the /remind ConversationHandler."""
    return ConversationHandler(
        entry_points=[CommandHandler("remind", start_remind)],
        states={
            GET_TZ_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_tz_choice), MessageHandler(filters.LOCATION, handle_tz_choice)],
            GET_DATE: [CallbackQueryHandler(date_callback_handler, pattern="^(cal_nav_|date_sel_|cal_view_|cal_set_|ignore)")],
            GET_LABEL: [CallbackQueryHandler(label_callback_handler, pattern="^label_sel_"), MessageHandler(filters.TEXT & ~filters.COMMAND, handle_label_text)],
            GET_BIRTHDAY_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_birthday_name)]
        },
        fallbacks=[CommandHandler("cancel", cancel_remind)],
        per_message=False
    )
