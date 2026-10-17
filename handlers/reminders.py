import html
import calendar
from datetime import datetime, time, timezone, timedelta
import pytz
from bson import ObjectId
from bson.errors import InvalidId
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    ContextTypes, CommandHandler, MessageHandler,
    filters, ConversationHandler, CallbackQueryHandler
)
from config import logger
from database import reminders_col
from utils.sentinel import log_event, notify_admin
from utils.timezones import tf

# Conversation States
GET_TZ_CHOICE, GET_DATE, GET_TIME, GET_LABEL = range(4)

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
        r_time_str = reminder_data.get('reminder_time', "09:00")
        hour, minute = map(int, r_time_str.split(':'))
        tz = pytz.timezone(reminder_data['timezone'])
        reminder_time = time(hour=hour, minute=minute, tzinfo=tz)
        job_name = str(reminder_data['_id'])
        for job in app.job_queue.get_jobs_by_name(job_name): job.schedule_removal()
        app.job_queue.run_daily(send_daily_reminder, time=reminder_time, chat_id=reminder_data['user_id'], data=reminder_data, name=job_name)
    except Exception as e: logger.error(f"Scheduling error: {e}")


async def send_daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    data = job.data
    try:
        # 1. Double check that reminder has not been deleted from DB
        if reminders_col is not None:
            exists = await reminders_col.find_one({"_id": data['_id']})
            if not exists:
                job.schedule_removal()
                return
            data = exists

        tz = pytz.timezone(data.get('timezone', 'UTC'))
        now = datetime.now(tz).date()
        target = datetime.strptime(data['target_date'], "%Y-%m-%d").date()
        days_left = (target - now).days
        
        # 2. Windowing: Only send daily notifications if within 3 months (90 days)
        if days_left > 90:
            return
            
        if days_left >= 0:
            if days_left > 0:
                months = days_left // 30
                rem_days = days_left % 30
                weeks = rem_days // 7
                days = rem_days % 7
                
                parts = []
                if months > 0: parts.append(f"{months} month{'s' if months > 1 else ''}")
                if weeks > 0: parts.append(f"{weeks} week{'s' if weeks > 1 else ''}")
                if days > 0: parts.append(f"{days} day{'s' if days > 1 else ''}")
                
                time_str = ", ".join(parts) if parts else f"{days_left} day{'s' if days_left > 1 else ''}"
                msg = f"🔔 REMINDER: {time_str} left to '{data['label']}'!"
            else:
                msg = f"🎉 TODAY IS THE DAY: '{data['label']}'!"
                
            action_kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("💤 Snooze 1 Day", callback_data=f"remsnooze_{data['_id']}"),
                    InlineKeyboardButton("✅ Mark Done", callback_data=f"remdone_{data['_id']}")
                ]
            ])
            await context.bot.send_message(chat_id=job.chat_id, text=msg, reply_markup=action_kb)
            await notify_admin(context, f"✅ Reminder Delivered to `{job.chat_id}` for `{data['label']}`.")
            
            if days_left == 0 and reminders_col is not None:
                rec = data.get("recurrence", "once")
                if rec == "once":
                    await reminders_col.delete_one({"_id": data['_id']})
                    job.schedule_removal()
                else:
                    # Roll over recurring event
                    rollover_days = {"daily": 1, "weekly": 7, "monthly": 30, "annually": 365}.get(rec, 7)
                    new_target = (target + timedelta(days=rollover_days)).strftime("%Y-%m-%d")
                    await reminders_col.update_one({"_id": data['_id']}, {"$set": {"target_date": new_target}})
        else:
            if reminders_col is not None:
                await reminders_col.delete_one({"_id": data['_id']})
            job.schedule_removal()
    except Exception as e:
        logger.error(f"Reminder error: {e}")
        await notify_admin(context, f"🔴 REMINDER FAILURE for User `{data.get('user_id')}`: {e}")


# --- HANDLERS ---

async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if reminders_col is None: return
    user_id = update.effective_user.id
    try:
        cursor = reminders_col.find({"user_id": user_id}).sort("target_date", 1)
        reminders = await cursor.to_list(length=50)
        if not reminders: return await update.effective_message.reply_text("No active reminders.")
        for r in reminders:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Delete 🗑️", callback_data=f"delrem_{r['_id']}")]])
            label = html.escape(str(r.get('label', 'No Label')))
            target_date_str = r.get('target_date', '')
            try:
                tz = pytz.timezone(r.get('timezone', 'UTC'))
                now_local = datetime.now(tz).date()
                target_dt = datetime.strptime(target_date_str, "%Y-%m-%d").date()
                days_left = (target_dt - now_local).days
            except Exception:
                days_left = 0
                
            if days_left > 90:
                status_header = f"💤 <b>{label}</b> (Dormant — {days_left}d left)"
                desc_extra = f"\n<i>Daily alerts start in {days_left - 90} days</i>"
            else:
                status_header = f"🟢 <b>{label}</b> (Active — {days_left}d left)"
                desc_extra = ""
                
            rec_tag = f" | 🔄 {r.get('recurrence', 'once').capitalize()}" if r.get('recurrence', 'once') != 'once' else ""
            await update.effective_message.reply_text(
                f"{status_header}\n📅 {target_date_str} | ⏰ {r['reminder_time']}{rec_tag}{desc_extra}",
                reply_markup=kb,
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
                    await reminders_col.update_one({"_id": oid}, {"$set": {"target_date": new_date}})
                    
                    # Reschedule job with updated date
                    rec["target_date"] = new_date
                    if context.application:
                        schedule_reminder_job(context.application, rec)
                        
                    await query.edit_message_text(f"💤 **Snoozed 1 Day**\nNew alert date: `{new_date}` for '{rec.get('label')}'", parse_mode="Markdown")
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
                    if recurrence != "once":
                        rollover_days = {"daily": 1, "weekly": 7, "monthly": 30, "annually": 365}.get(recurrence, 7)
                        curr_date = datetime.strptime(rec['target_date'], "%Y-%m-%d")
                        new_date = (curr_date + timedelta(days=rollover_days)).strftime("%Y-%m-%d")
                        await reminders_col.update_one({"_id": oid}, {"$set": {"target_date": new_date}})
                        rec["target_date"] = new_date
                        if context.application:
                            schedule_reminder_job(context.application, rec)
                        await query.edit_message_text(f"🎉 **Completed!**\nRolled over to `{new_date}` ({recurrence.capitalize()}).", parse_mode="Markdown")
                        return await query.answer("✅ Rolled over to next interval!")
                    else:
                        await reminders_col.delete_one({"_id": oid})
                        if context.job_queue:
                            for job in context.job_queue.get_jobs_by_name(str(r_id)):
                                job.schedule_removal()
                        await query.edit_message_text(f"✅ **Marked Completed!**\nReminder for '{rec.get('label')}' has been cleared.", parse_mode="Markdown")
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
    user_id = update.effective_user.id
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
    label = query.data.replace("label_sel_", "")
    context.user_data['label'] = label
    return await finish_reminder(query.message, context)


async def handle_label_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['label'] = update.message.text.strip()
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

        data = {
            "user_id": user_id, 
            "timezone": context.user_data['timezone'], 
            "target_date": context.user_data['target_date'], 
            "reminder_time": context.user_data['reminder_time'], 
            "label": context.user_data['label'],
            "recurrence": context.user_data.get('recurrence', 'once')
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
            
        success_text = f"🚀 **Reminder Armed!**\n\n🏷️ `{data['label']}` on `{data['target_date']}`\n\n{status_note}"
        
        if message.from_user and message.from_user.is_bot:
            try: await message.edit_text(success_text, parse_mode="Markdown")
            except: await message.reply_text(success_text, parse_mode="Markdown")
        else:
            await message.reply_text(success_text, parse_mode="Markdown")
        
        await log_event(user_id, "User", f"Set Reminder: {data['label']}")
        await notify_admin(context, f"📅 User `{user_id}` armed a reminder for `{data['label']}`.")
        context.user_data.clear()
        return ConversationHandler.END

    except Exception as e:
        logger.error(f"Finish Error: {e}")
        context.user_data.clear()
        return ConversationHandler.END


def get_reminder_conversation_handler():
    """Builds and returns the /remind ConversationHandler."""
    return ConversationHandler(
        entry_points=[CommandHandler("remind", start_remind)], 
        states={
            GET_TZ_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_tz_choice), MessageHandler(filters.LOCATION, handle_tz_choice)], 
            GET_DATE: [CallbackQueryHandler(date_callback_handler, pattern="^(cal_nav_|date_sel_|cal_view_|cal_set_|ignore)")], 
            GET_LABEL: [CallbackQueryHandler(label_callback_handler, pattern="^label_sel_"), MessageHandler(filters.TEXT & ~filters.COMMAND, handle_label_text)]
        }, 
        fallbacks=[CommandHandler("cancel", lambda u,c: (c.user_data.clear() or ConversationHandler.END))], 
        per_message=False
    )
