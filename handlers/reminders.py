import calendar
import html
import re
from datetime import date, datetime, time, timedelta

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
from utils.sentinel import notify_admin
from utils.timezones import tf

# Conversation States
GET_TZ_CHOICE, GET_DATE, GET_TIME, GET_LABEL, GET_BIRTHDAY_NAME = range(5)

# Deduplication state to prevent concurrent double-dispatches
_active_reminder_dispatches = set()

_MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def format_concise_date(date_val: str | datetime | date | None) -> str:
    """
    Formats a date string or object into concise ordinal format without year
    (e.g. '1st Feb', '24th Sep', '2nd Nov', '3rd Mar', '21st Jun').
    Properly handles 11th-13th exceptions and is locale-independent.
    """
    if not date_val:
        return ""
    if isinstance(date_val, datetime):
        dt = date_val.date()
    elif isinstance(date_val, date):
        dt = date_val
    else:
        try:
            dt = datetime.strptime(str(date_val)[:10], "%Y-%m-%d").date()
        except Exception:
            return str(date_val)

    day = dt.day
    if 11 <= day <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix} {_MONTH_NAMES[dt.month - 1]}"


def roll_date_annually(base_date: date, target_after: date | None = None) -> date:
    """
    Rolls an annual date forward year-by-year, preserving the exact day and month.
    Handles Feb 29 leap day fallback to Feb 28 on non-leap years.
    If target_after is provided, rolls forward until the date is strictly after target_after.
    """
    res = base_date
    while True:
        try:
            res = res.replace(year=res.year + 1)
        except ValueError:
            res = res.replace(year=res.year + 1, day=28)
        if target_after is None or res > target_after:
            break
    return res


def build_list_keyboard(reminders_batch: list[tuple[int, dict]]) -> InlineKeyboardMarkup | None:
    """
    Renders dual-row button grid for /list:
    Row 1 (Instant silent dispatch): [ ⚡ 1 ]  [ ⚡ 2 ] ...
    Row 2 (Manage/Edit): [ ⚙️ 1 ]  [ ⚙️ 2 ] ...
    Neatly wrapped up to 5 per row.
    """
    if not reminders_batch:
        return None
    grid = []
    for i in range(0, len(reminders_batch), 5):
        chunk = reminders_batch[i:i + 5]
        fire_row = [
            InlineKeyboardButton(f"⚡ {idx}", callback_data=f"remfire_{str(r['_id'])}")
            for idx, r in chunk
        ]
        edit_row = [
            InlineKeyboardButton(f"⚙️ {idx}", callback_data=f"editrem_{str(r['_id'])}")
            for idx, r in chunk
        ]
        grid.append(fire_row)
        grid.append(edit_row)
    return InlineKeyboardMarkup(grid)


def format_reminder_notification(r: dict) -> tuple[str, InlineKeyboardMarkup | None]:
    """
    Formats the exact reminder notification message and reply markup for instant dispatch.
    For Birthday reminders: omits recurrence labels and buttons.
    For other categories: includes category header, label, concise date, snooze, and mark-done buttons.
    """
    raw_cat = str(r.get("category", "")).strip()
    raw_lbl = str(r.get("label", ""))
    is_bday = (raw_cat.lower() == "birthday" or "birthday" in raw_lbl.lower() or "🎂" in raw_lbl)

    tz_name = r.get("timezone", "UTC")
    try:
        user_tz = pytz.timezone(tz_name)
    except Exception:
        user_tz = pytz.UTC
    now_local = datetime.now(user_tz).date()

    t_date = r.get("target_date", "")
    try:
        target_dt = datetime.strptime(str(t_date)[:10], "%Y-%m-%d").date()
        d_left = (target_dt - now_local).days
    except Exception:
        d_left = 0

    c_date = format_concise_date(t_date)

    if d_left == 0:
        date_info = f"{c_date} (Today)"
    elif d_left == 1:
        date_info = f"{c_date} (Tomorrow)"
    elif d_left == -1:
        date_info = f"{c_date} (Yesterday)"
    elif d_left < 0:
        date_info = f"{c_date} ({abs(d_left)}d ago)"
    else:
        date_info = f"{c_date} (In {format_time_left(d_left)})"

    if is_bday:
        header = "<b>Birthday Reminder</b>"
        prefix = "✦ " if r.get("is_spotlight") else ""
        escaped_label = html.escape(raw_lbl or "Birthday")
        age = calculate_upcoming_age(r)
        age_str = f" • Turning {age}" if age is not None else ""
        lines = [header, "", f"{prefix}<b>{escaped_label}</b> — {date_info}{age_str}"]
        return "\n".join(lines), None
    else:
        cat = raw_cat if raw_cat else "General"
        category_icons = {
            "Travel": "✈️", "Launch": "🚀", "Payment": "💳",
            "Medication": "💊", "Deadline": "🏆", "General": "⏰"
        }
        icon = category_icons.get(cat, "")
        header_prefix = f"{icon} " if icon else ""
        header = f"<b>{header_prefix}{html.escape(cat)} Reminder</b>"
        escaped_label = html.escape(raw_lbl or cat)
        lines = [header, "", f"<b>{escaped_label}</b> — {date_info}"]
        r_id = str(r.get("_id", ""))
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("💤 Snooze (1 Day)", callback_data=f"remsnooze_{r_id}"),
            InlineKeyboardButton("✅ Mark Done", callback_data=f"remdone_{r_id}")
        ]])
        return "\n".join(lines), reply_markup


async def send_instant_reminder_dispatch(bot, r: dict, user_id: int):
    """Dispatches a single reminder notification immediately to the user's chat."""
    msg_text, reply_markup = format_reminder_notification(r)
    await bot.send_message(
        chat_id=user_id,
        text=msg_text,
        reply_markup=reply_markup,
        parse_mode="HTML"
    )


def format_relative_countdown(days_left: int) -> str:
    """
    Formats countdown into concise units: 'today', 'tomorrow', or 'in Xm Yw Zd',
    omitting zero units (e.g. 'in 2m 2w 4d', 'in 2w', 'in 3d').
    Handles negative offsets with 'yesterday' or 'Xd ago'.
    """
    if days_left is None:
        return ""
    try:
        days_left = int(days_left)
    except (ValueError, TypeError):
        return ""

    if days_left == 0:
        return "today"
    if days_left == 1:
        return "tomorrow"
    if days_left == -1:
        return "yesterday"
    if days_left < 0:
        return f"{abs(days_left)}d ago"

    months = days_left // 30
    rem = days_left % 30
    weeks = rem // 7
    days = rem % 7

    parts = []
    if months > 0:
        parts.append(f"{months}m")
    if weeks > 0:
        parts.append(f"{weeks}w")
    if days > 0:
        parts.append(f"{days}d")

    return "in " + " ".join(parts) if parts else f"in {days_left}d"


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


def calculate_upcoming_age(reminder: dict) -> int | None:
    """
    Computes the upcoming age on the target date.
    Returns integer age or None if birth_date / birth_year is not available.
    """
    if not isinstance(reminder, dict):
        return None

    b_date_val = reminder.get("birth_date")
    b_year_val = reminder.get("birth_year")
    target_date_val = reminder.get("target_date")

    target_year = None
    if hasattr(target_date_val, "year"):
        target_year = target_date_val.year
    elif target_date_val:
        try:
            target_year = datetime.strptime(str(target_date_val)[:10], "%Y-%m-%d").year
        except Exception:
            pass
    if target_year is None:
        target_year = datetime.now().year

    birth_year_num = None
    if hasattr(b_date_val, "year"):
        birth_year_num = b_date_val.year
    elif b_date_val:
        try:
            birth_year_num = datetime.strptime(str(b_date_val)[:10], "%Y-%m-%d").year
        except Exception:
            pass

    if birth_year_num is None and b_year_val is not None:
        try:
            birth_year_num = int(b_year_val)
        except (ValueError, TypeError):
            pass

    if birth_year_num is not None and target_year >= birth_year_num:
        return target_year - birth_year_num
    return None


async def resolve_reminder_id(user_id: int, r_id_str: str) -> tuple[ObjectId | None, dict | None]:
    """Resolves a reminder by either 1-based list index or 24-character ObjectId."""
    if reminders_col is None:
        return None, None

    r_id_str = str(r_id_str).strip()
    if r_id_str.isdigit() and len(r_id_str) <= 4:
        idx = int(r_id_str) - 1
        if idx >= 0:
            cursor = reminders_col.find({"user_id": user_id}).sort("target_date", 1).skip(idx).limit(1)
            docs = await cursor.to_list(length=1)
            if docs:
                return docs[0]["_id"], docs[0]

    try:
        oid = ObjectId(r_id_str)
        rec = await reminders_col.find_one({"_id": oid, "user_id": user_id})
        return oid, rec
    except Exception:
        return None, None



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
                raw_cat = str(r.get("category", "")).strip()
                raw_lbl = str(r.get("label", ""))
                is_bday = (raw_cat.lower() == "birthday" or "birthday" in raw_lbl.lower() or "🎂" in raw_lbl)
                rec = "annually" if is_bday else r.get("recurrence", "once")

                if rec == "once":
                    await reminders_col.delete_one({"_id": r_id})
                    if context.job_queue:
                        for j in context.job_queue.get_jobs_by_name(str(r_id)):
                            j.schedule_removal()
                else:
                    if rec == "annually" or is_bday:
                        new_dt = roll_date_annually(t_date, today_local)
                        new_target = new_dt.strftime("%Y-%m-%d")
                    else:
                        rollover_days = {"daily": 1, "weekly": 7, "monthly": 30}.get(rec, 7)
                        new_dt = t_date
                        while new_dt <= today_local:
                            new_dt += timedelta(days=rollover_days)
                        new_target = new_dt.strftime("%Y-%m-%d")
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
            raw_lbl = str(r.get("label", ""))
            raw_cat = str(r.get("category", "")).strip()
            if raw_cat.lower() == "birthday" or "birthday" in raw_lbl.lower() or "🎂" in raw_lbl:
                cat = "Birthday"
            elif raw_cat:
                cat = raw_cat
            else:
                cat = "General"

            due_by_category.setdefault(cat, []).append((r, days_left))

        # --- A. Deliver Merged Birthday Reminders ---
        if "Birthday" in due_by_category:
            birthday_items = due_by_category["Birthday"]
            # Sort spotlight first, then by days_left
            birthday_items.sort(key=lambda x: (0 if x[0].get("is_spotlight") else 1, x[1]))

            spotlight_item = None
            other_items = []
            for item in birthday_items:
                if item[0].get("is_spotlight") and spotlight_item is None:
                    spotlight_item = item
                else:
                    other_items.append(item)

            def _format_bday_entry(b_rec, d_left, is_spot=False) -> str:
                raw_label = str(b_rec.get('label', 'Birthday'))
                escaped_label = html.escape(raw_label)
                t_date = b_rec.get('target_date', '')
                c_date = format_concise_date(t_date)

                if d_left == 0:
                    date_info = f"{c_date} (Today)"
                elif d_left == 1:
                    date_info = f"{c_date} (Tomorrow)"
                elif d_left == -1:
                    date_info = f"{c_date} (Yesterday)"
                elif d_left < 0:
                    date_info = f"{c_date} ({abs(d_left)}d ago)"
                else:
                    date_info = f"{c_date} (In {format_time_left(d_left)})"

                age = calculate_upcoming_age(b_rec)
                age_str = f" • Turning {age}" if age is not None else ""
                prefix = "✦ " if is_spot else ""
                return f"{prefix}<b>{escaped_label}</b> — {date_info}{age_str}"

            lines = []
            header = "<b>Birthday Reminders</b>" if len(birthday_items) > 1 else "<b>Birthday Reminder</b>"
            lines.append(header)
            lines.append("")

            if spotlight_item:
                lines.append(_format_bday_entry(spotlight_item[0], spotlight_item[1], is_spot=True))
                if other_items:
                    quote_lines = [_format_bday_entry(r_rec, d_l) for (r_rec, d_l) in other_items]
                    lines.append(f"<blockquote expandable>{chr(10).join(quote_lines)}</blockquote>")
            else:
                for b_rec, d_left in birthday_items:
                    lines.append(_format_bday_entry(b_rec, d_left))

            msg_text = "\n".join(lines)
            await context.bot.send_message(
                chat_id=user_id,
                text=msg_text,
                reply_markup=None,
                parse_mode="HTML"
            )
            await notify_admin(context, f"🎂 Birthday Reminders sent to `{user_id}` ({len(birthday_items)} birthdays).")

        # --- B. Deliver Other Reminders Segregated by Category ---
        category_icons = {
            "Travel": "✈️", "Launch": "🚀", "Payment": "💳",
            "Medication": "💊", "Deadline": "🏆", "General": "⏰"
        }

        for cat, items in due_by_category.items():
            if cat == "Birthday":
                continue

            items.sort(key=lambda x: x[1])
            icon = category_icons.get(cat, "")
            header_prefix = f"{icon} " if icon else ""
            header = f"<b>{header_prefix}{html.escape(cat)} Reminders</b>" if len(items) > 1 else f"<b>{header_prefix}{html.escape(cat)} Reminder</b>"
            lines = [header, ""]
            kb_rows = []

            for idx, (r_rec, d_left) in enumerate(items, 1):
                r_id = r_rec['_id']
                raw_label = str(r_rec.get('label', cat))
                escaped_label = html.escape(raw_label)
                t_date = r_rec.get('target_date', '')
                c_date = format_concise_date(t_date)

                if d_left == 0:
                    time_info = f"{c_date} (Today)"
                elif d_left == 1:
                    time_info = f"{c_date} (Tomorrow)"
                elif d_left == -1:
                    time_info = f"{c_date} (Yesterday)"
                elif d_left < 0:
                    time_info = f"{c_date} ({abs(d_left)}d ago)"
                else:
                    time_info = f"{c_date} (In {format_time_left(d_left)})"

                lines.append(f"<b>{escaped_label}</b> — {time_info}")

                if len(items) > 1:
                    row = [
                        InlineKeyboardButton(f"💤 Snooze #{idx}", callback_data=f"remsnooze_{r_id}"),
                        InlineKeyboardButton(f"✅ Done #{idx}", callback_data=f"remdone_{r_id}")
                    ]
                    kb_rows.append(row)
                else:
                    row = [
                        InlineKeyboardButton("💤 Snooze (1 Day)", callback_data=f"remsnooze_{r_id}"),
                        InlineKeyboardButton("✅ Mark Done", callback_data=f"remdone_{r_id}")
                    ]
                    kb_rows.append(row)

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
    query = update.callback_query
    is_callback = bool(query and isinstance(getattr(query, "data", None), str))
    try:
        cursor = reminders_col.find({"user_id": user_id}).sort("target_date", 1)
        reminders = await cursor.to_list(length=50)
        if not reminders:
            if is_callback:
                return await query.edit_message_text("No active reminders.")
            return await update.effective_message.reply_text("No active reminders.")

        tz_cache = {}
        first_page = True

        for chunk_start in range(0, len(reminders), 25):
            chunk = reminders[chunk_start:chunk_start + 25]
            main_entries = []
            next_year_entries = []
            chunk_batch = []

            for idx_offset, r in enumerate(chunk):
                idx = chunk_start + idx_offset + 1
                chunk_batch.append((idx, r))

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
                    target_dt = datetime.strptime(str(target_date_str)[:10], "%Y-%m-%d").date()
                    days_left = (target_dt - now_local).days
                except Exception:
                    now_local = datetime.now(pytz.UTC).date()
                    target_dt = None
                    days_left = 0

                countdown_text = format_relative_countdown(days_left)
                concise_date = format_concise_date(target_date_str)

                raw_cat = str(r.get('category', 'General')).strip()
                is_bday = (raw_cat.lower() == "birthday" or "birthday" in raw_label.lower() or "🎂" in raw_label)

                if is_bday:
                    cat_tag = ""
                else:
                    cat_display = html.escape(r.get('category', 'General') or 'General')
                    rec = r.get('recurrence', 'once')
                    rec_tag = f" • {html.escape(rec.capitalize())}" if rec != 'once' else ""
                    cat_tag = f" [{cat_display}{rec_tag}]"

                age = calculate_upcoming_age(r) if is_bday else None
                age_tag = f" ↑{age}" if age is not None else ""
                is_spot = bool(r.get('is_spotlight'))
                spotlight_tag = " ✦" if is_spot else ""

                date_part = f"{concise_date} · {countdown_text}" if concise_date else countdown_text
                entry_text = f"{idx}. <code>{label_esc}</code>{age_tag}{spotlight_tag}\n   └ {date_part}{cat_tag}"

                is_next_year = bool(target_dt and target_dt.year > now_local.year)
                # CRITICAL EXCEPTION: Spotlighted reminders are ALWAYS outside the blockquote in the main list, regardless of year!
                if is_bday and is_next_year and not is_spot:
                    next_year_entries.append(entry_text)
                else:
                    main_entries.append(entry_text)

            header = "<b>Active Reminders</b>" if chunk_start == 0 else "<b>Active Reminders (Cont.)</b>"
            lines = [header]
            if main_entries:
                lines.extend(main_entries)
            if next_year_entries:
                lines.append("<blockquote expandable>\nUpcoming Next Year:\n" + "\n\n".join(next_year_entries) + "\n</blockquote>")

            chunk_text = "\n\n".join(lines)
            markup = build_list_keyboard(chunk_batch)

            if is_callback and first_page:
                try:
                    await query.edit_message_text(
                        chunk_text,
                        reply_markup=markup,
                        parse_mode="HTML"
                    )
                except Exception:
                    await update.effective_message.reply_text(
                        chunk_text,
                        reply_markup=markup,
                        parse_mode="HTML"
                    )
                first_page = False
            else:
                await update.effective_message.reply_text(
                    chunk_text,
                    reply_markup=markup,
                    parse_mode="HTML"
                )
    except Exception as e:
        logger.error(f"List error: {e}")
        await update.effective_message.reply_text("❌ Failed to list reminders.")


async def handle_reminder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles delete, snooze, mark-done, instant-trigger, and edit actions on reminders."""
    query = update.callback_query
    user_id = update.effective_user.id
    data_str = query.data

    if data_str.startswith("remfire_"):
        r_id = data_str.split('_')[1]
        if reminders_col is not None:
            try:
                oid = ObjectId(r_id)
                rec = await reminders_col.find_one({"_id": oid, "user_id": user_id})
                if not rec:
                    return await query.answer("Reminder not found.", show_alert=True)

                await send_instant_reminder_dispatch(context.bot, rec, user_id)
                return await query.answer("⚡ Sent reminder on the spot!")
            except Exception as e:
                logger.error(f"Instant reminder trigger error: {e}")
                return await query.answer("Error sending reminder.", show_alert=True)
        return await query.answer()

    elif data_str.startswith("delrem_"):
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

                    await query.edit_message_text(
                        "🗑️ <b>Reminder deleted.</b>",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to List", callback_data="edfield_global_back")]]),
                        parse_mode="HTML"
                    )
                    await query.answer("🗑️ Reminder deleted!", show_alert=True)
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
                    raw_cat = str(rec.get("category", "")).strip()
                    raw_lbl = str(rec.get("label", ""))
                    if raw_cat.lower() == "birthday" or "birthday" in raw_lbl.lower() or "🎂" in raw_lbl:
                        return await query.answer("Birthday reminders cannot be snoozed.", show_alert=True)

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
                    raw_cat = str(rec.get("category", "")).strip()
                    raw_lbl = str(rec.get("label", ""))
                    is_bday = (raw_cat.lower() == "birthday" or "birthday" in raw_lbl.lower() or "🎂" in raw_lbl)
                    recurrence = "annually" if is_bday else rec.get("recurrence", "once")
                    label_raw = str(rec.get('label', 'Reminder'))
                    label_esc = html.escape(label_raw)
                    if recurrence != "once":
                        curr_date = datetime.strptime(rec['target_date'], "%Y-%m-%d").date()
                        if recurrence == "annually" or is_bday:
                            new_date = roll_date_annually(curr_date).strftime("%Y-%m-%d")
                        else:
                            rollover_days = {"daily": 1, "weekly": 7, "monthly": 30}.get(recurrence, 7)
                            new_date = (curr_date + timedelta(days=rollover_days)).strftime("%Y-%m-%d")
                        await reminders_col.update_one({"_id": oid}, {"$set": {"target_date": new_date, "last_notified": ""}})
                        rec["target_date"] = new_date
                        if context.application:
                            schedule_reminder_job(context.application, rec)

                        c_date = format_concise_date(new_date)
                        rec_label = "" if is_bday else f" ({recurrence.capitalize()})"
                        rec_html = "" if is_bday else f" (<b>{html.escape(recurrence.capitalize())}</b>)"
                        kb = query.message.reply_markup.inline_keyboard if query.message and query.message.reply_markup else []
                        if len(kb) > 1:
                            new_kb = [row for row in kb if not any(r_id in btn.callback_data for btn in row)]
                            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_kb) if new_kb else None)
                            return await query.answer(f"🎉 Completed! Rolled over to {c_date}{rec_label}.", show_alert=True)
                        else:
                            await query.edit_message_text(f"🎉 <b>Completed!</b>\nRolled over to <code>{c_date}</code>{rec_html}.", parse_mode="HTML")
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

    elif data_str.startswith("editrem_"):
        r_id = data_str.split('_')[1]
        context.user_data.pop('pending_bdate_rid', None)
        if reminders_col is not None:
            try:
                oid = ObjectId(r_id)
                rec = await reminders_col.find_one({"_id": oid, "user_id": user_id})
                if not rec:
                    return await query.answer("Reminder not found.", show_alert=True)
                text = build_reminder_card_text(rec)
                markup = build_reminder_edit_markup(rec)
                await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
                return await query.answer()
            except Exception as e:
                logger.error(f"Edit menu error: {e}")
                return await query.answer("Error opening edit menu.", show_alert=True)

    elif data_str.startswith("edfield_"):
        parts = data_str.split('_')
        if (len(parts) >= 3 and parts[1] == "global" and parts[2] == "back") or (len(parts) >= 3 and parts[2] == "back"):
            context.user_data.pop('pending_bdate_rid', None)
            return await list_reminders(update, context)

        r_id = parts[1]
        field = "_".join(parts[2:]) if len(parts) > 2 else ""

        if field == "spotlight_on":
            if reminders_col is not None:
                try:
                    oid = ObjectId(r_id)
                    await reminders_col.update_many({"user_id": user_id, "is_spotlight": True}, {"$set": {"is_spotlight": False}})
                    await reminders_col.update_one({"_id": oid, "user_id": user_id}, {"$set": {"is_spotlight": True}})
                    rec = await reminders_col.find_one({"_id": oid, "user_id": user_id})
                    if rec:
                        text = build_reminder_card_text(rec)
                        markup = build_reminder_edit_markup(rec)
                        await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
                        return await query.answer("✦ Set as spotlight birthday!")
                except Exception as e:
                    logger.error(f"Spotlight on error: {e}")
            return await query.answer("Failed to update spotlight.", show_alert=True)

        elif field == "spotlight_off":
            if reminders_col is not None:
                try:
                    oid = ObjectId(r_id)
                    await reminders_col.update_one({"_id": oid, "user_id": user_id}, {"$set": {"is_spotlight": False}})
                    rec = await reminders_col.find_one({"_id": oid, "user_id": user_id})
                    if rec:
                        text = build_reminder_card_text(rec)
                        markup = build_reminder_edit_markup(rec)
                        await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
                        return await query.answer("Spotlight removed.")
                except Exception as e:
                    logger.error(f"Spotlight off error: {e}")
            return await query.answer("Failed to update spotlight.", show_alert=True)

        elif field == "bdate":
            rec = None
            if reminders_col is not None:
                try:
                    rec = await reminders_col.find_one({"_id": ObjectId(r_id), "user_id": user_id})
                except Exception:
                    pass
            context.user_data['pending_bdate_rid'] = r_id
            kb = [
                [
                    InlineKeyboardButton("1950s", callback_data=f"edset_{r_id}_bdec_1950"),
                    InlineKeyboardButton("1960s", callback_data=f"edset_{r_id}_bdec_1960"),
                    InlineKeyboardButton("1970s", callback_data=f"edset_{r_id}_bdec_1970"),
                ],
                [
                    InlineKeyboardButton("1980s", callback_data=f"edset_{r_id}_bdec_1980"),
                    InlineKeyboardButton("1990s", callback_data=f"edset_{r_id}_bdec_1990"),
                    InlineKeyboardButton("2000s", callback_data=f"edset_{r_id}_bdec_2000"),
                ],
                [
                    InlineKeyboardButton("2010s", callback_data=f"edset_{r_id}_bdec_2010"),
                    InlineKeyboardButton("2020s", callback_data=f"edset_{r_id}_bdec_2020"),
                ]
            ]
            if rec and (rec.get("birth_date") or rec.get("birth_year")):
                kb.append([InlineKeyboardButton("✕ Remove Birth Date", callback_data=f"edset_{r_id}_clearbdate_1")])
            kb.append([InlineKeyboardButton("🔙 Back to Reminder", callback_data=f"editrem_{r_id}")])
            await query.edit_message_text(
                "🎂 <b>Set Birth Date</b>\n\n"
                "Select a birth year decade below, or send <code>YYYY-MM-DD</code> / <code>YYYY</code> directly:\n"
                f"<i>Or use command: <code>/editrem {r_id} birthdate YYYY-MM-DD</code></i>",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="HTML"
            )
            return await query.answer()

        elif field == "details":
            rec = await reminders_col.find_one({"_id": ObjectId(r_id), "user_id": user_id}) if reminders_col is not None else None
            raw_cat = str(rec.get("category", "")).strip() if rec else ""
            raw_lbl = str(rec.get("label", "")) if rec else ""
            is_bday = bool(rec and (raw_cat.lower() == "birthday" or "birthday" in raw_lbl.lower() or "🎂" in raw_lbl))

            kb = [
                [
                    InlineKeyboardButton("🏷️ Label", callback_data=f"edfield_{r_id}_label"),
                    InlineKeyboardButton("📂 Category", callback_data=f"edfield_{r_id}_cat"),
                ],
                [
                    InlineKeyboardButton("📅 Date", callback_data=f"edfield_{r_id}_date"),
                    InlineKeyboardButton("⏰ Time", callback_data=f"edfield_{r_id}_time"),
                ]
            ]
            if not is_bday:
                kb.append([
                    InlineKeyboardButton("🔄 Recurrence", callback_data=f"edfield_{r_id}_rec"),
                    InlineKeyboardButton("🔙 Back to Reminder", callback_data=f"editrem_{r_id}"),
                ])
            else:
                kb.append([InlineKeyboardButton("🔙 Back to Reminder", callback_data=f"editrem_{r_id}")])

            await query.edit_message_text(
                "✏️ <b>Edit Details:</b>",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="HTML"
            )
            return await query.answer()

        elif field == "cat":
            cats = ["🎂 Birthday", "✈️ Travel", "🚀 Launch", "💳 Payment", "💊 Medication", "🏆 Deadline", "📌 General"]
            kb = []
            for i in range(0, len(cats), 2):
                row = [InlineKeyboardButton(cats[i], callback_data=f"edset_{r_id}_cat_{cats[i].split()[-1]}")]
                if i + 1 < len(cats):
                    row.append(InlineKeyboardButton(cats[i+1], callback_data=f"edset_{r_id}_cat_{cats[i+1].split()[-1]}"))
                kb.append(row)
            kb.append([InlineKeyboardButton("🔙 Cancel", callback_data=f"edfield_{r_id}_details")])
            await query.edit_message_text(
                "📂 <b>Select New Category:</b>",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="HTML"
            )
            return await query.answer()

        elif field == "rec":
            rec = await reminders_col.find_one({"_id": ObjectId(r_id), "user_id": user_id}) if reminders_col is not None else None
            raw_cat = str(rec.get("category", "")).strip() if rec else ""
            raw_lbl = str(rec.get("label", "")) if rec else ""
            if rec and (raw_cat.lower() == "birthday" or "birthday" in raw_lbl.lower() or "🎂" in raw_lbl):
                return await query.answer("Birthday reminders automatically recur annually.", show_alert=True)

            recs = [("Once", "once"), ("Daily", "daily"), ("Weekly", "weekly"), ("Monthly", "monthly"), ("Annually", "annually")]
            kb = [
                [InlineKeyboardButton(r[0], callback_data=f"edset_{r_id}_rec_{r[1]}") for r in recs[:2]],
                [InlineKeyboardButton(r[0], callback_data=f"edset_{r_id}_rec_{r[1]}") for r in recs[2:4]],
                [InlineKeyboardButton(recs[4][0], callback_data=f"edset_{r_id}_rec_{recs[4][1]}")],
                [InlineKeyboardButton("🔙 Cancel", callback_data=f"edfield_{r_id}_details")]
            ]
            await query.edit_message_text(
                "🔄 <b>Select Recurrence Interval:</b>",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="HTML"
            )
            return await query.answer()

        elif field == "time":
            times = ["08:00", "09:00", "10:00", "12:00", "15:00", "18:00", "20:00", "22:00"]
            kb = [
                [InlineKeyboardButton(times[0], callback_data=f"edset_{r_id}_time_{times[0]}"), InlineKeyboardButton(times[1], callback_data=f"edset_{r_id}_time_{times[1]}"), InlineKeyboardButton(times[2], callback_data=f"edset_{r_id}_time_{times[2]}")],
                [InlineKeyboardButton(times[3], callback_data=f"edset_{r_id}_time_{times[3]}"), InlineKeyboardButton(times[4], callback_data=f"edset_{r_id}_time_{times[4]}"), InlineKeyboardButton(times[5], callback_data=f"edset_{r_id}_time_{times[5]}")],
                [InlineKeyboardButton(times[6], callback_data=f"edset_{r_id}_time_{times[6]}"), InlineKeyboardButton(times[7], callback_data=f"edset_{r_id}_time_{times[7]}")],
                [InlineKeyboardButton("🔙 Cancel", callback_data=f"edfield_{r_id}_details")]
            ]
            await query.edit_message_text(
                f"⏰ <b>Select Notification Time (UTC):</b>\n<i>Or send <code>/editrem {r_id} time HH:MM</code></i>",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="HTML"
            )
            return await query.answer()

        elif field == "date":
            kb = [
                [InlineKeyboardButton("+1 Day", callback_data=f"edset_{r_id}_shift_1"), InlineKeyboardButton("+3 Days", callback_data=f"edset_{r_id}_shift_3")],
                [InlineKeyboardButton("+1 Week", callback_data=f"edset_{r_id}_shift_7"), InlineKeyboardButton("+1 Month", callback_data=f"edset_{r_id}_shift_30")],
                [InlineKeyboardButton("+1 Year", callback_data=f"edset_{r_id}_shift_365")],
                [InlineKeyboardButton("🔙 Cancel", callback_data=f"edfield_{r_id}_details")]
            ]
            await query.edit_message_text(
                f"📅 <b>Quick Shift Target Date:</b>\n<i>Or set exact date via: <code>/editrem {r_id} date YYYY-MM-DD</code></i>",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="HTML"
            )
            return await query.answer()

        elif field == "label":
            kb = [[InlineKeyboardButton("🔙 Back to Edit Menu", callback_data=f"edfield_{r_id}_details")]]
            await query.edit_message_text(
                f"🏷️ <b>Modify Reminder Label:</b>\n\nReply with:\n<code>/editrem {r_id} label New Event Name</code>",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="HTML"
            )
            return await query.answer()

    elif data_str.startswith("edset_"):
        parts = data_str.split('_')
        r_id = parts[1]
        kind = parts[2]
        val = parts[3]

        if reminders_col is not None:
            try:
                oid = ObjectId(r_id)
                rec = await reminders_col.find_one({"_id": oid, "user_id": user_id})
                if not rec:
                    return await query.answer("Reminder not found.", show_alert=True)

                if kind == "bdec":
                    decade = int(val)
                    cur_yr = datetime.now().year
                    end_yr = min(decade + 9, cur_yr)
                    years = list(range(decade, end_yr + 1))
                    row1 = [InlineKeyboardButton(str(y), callback_data=f"edset_{r_id}_byr_{y}") for y in years[:5]]
                    row2 = [InlineKeyboardButton(str(y), callback_data=f"edset_{r_id}_byr_{y}") for y in years[5:10]]
                    kb = [r for r in [row1, row2] if r]
                    kb.append([InlineKeyboardButton("🔙 Back to Decades", callback_data=f"edfield_{r_id}_bdate")])
                    await query.edit_message_text(
                        f"🎂 <b>Select Birth Year ({decade}s)</b>\n\nChoose year or send <code>YYYY-MM-DD</code> as text:",
                        reply_markup=InlineKeyboardMarkup(kb),
                        parse_mode="HTML"
                    )
                    return await query.answer()

                elif kind == "byr":
                    byear = int(val)
                    t_date = rec.get("target_date", "")
                    b_date = None
                    if len(t_date) >= 10:
                        b_date = f"{byear}-{t_date[5:10]}"
                    updates = {"birth_year": byear, "birth_date": b_date}
                    await reminders_col.update_one({"_id": oid, "user_id": user_id}, {"$set": updates})
                    context.user_data.pop('pending_bdate_rid', None)
                    rec.update(updates)
                    card = build_reminder_card_text(rec)
                    markup = build_reminder_edit_markup(rec)
                    await query.edit_message_text(f"✅ <b>Birth Date Updated!</b>\n\n{card}", reply_markup=markup, parse_mode="HTML")
                    return await query.answer(f"🎂 Birth year set to {byear}!")

                elif kind == "clearbdate":
                    await reminders_col.update_one({"_id": oid}, {"$set": {"birth_date": None, "birth_year": None}})
                    context.user_data.pop('pending_bdate_rid', None)
                    rec["birth_date"] = None
                    rec["birth_year"] = None
                    card = build_reminder_card_text(rec)
                    markup = build_reminder_edit_markup(rec)
                    await query.edit_message_text(card, reply_markup=markup, parse_mode="HTML")
                    return await query.answer("Birth date removed.")

                updates = {}
                if kind == "cat":
                    updates["category"] = val
                    if val == "Birthday":
                        updates["recurrence"] = "annually"
                elif kind == "rec":
                    raw_cat = str(rec.get("category", "")).strip()
                    raw_lbl = str(rec.get("label", ""))
                    if not (raw_cat.lower() == "birthday" or "birthday" in raw_lbl.lower() or "🎂" in raw_lbl):
                        updates["recurrence"] = val
                elif kind == "time":
                    updates["reminder_time"] = val
                elif kind == "shift":
                    days_shift = int(val)
                    curr_date = datetime.strptime(rec['target_date'], "%Y-%m-%d").date()
                    if days_shift == 365:
                        new_date = roll_date_annually(curr_date).strftime("%Y-%m-%d")
                    else:
                        new_date = (curr_date + timedelta(days=days_shift)).strftime("%Y-%m-%d")
                    updates["target_date"] = new_date
                    updates["last_notified"] = ""

                if updates:
                    await reminders_col.update_one({"_id": oid}, {"$set": updates})
                    rec.update(updates)
                    if context.application:
                        schedule_reminder_job(context.application, rec)

                card = build_reminder_card_text(rec)
                markup = build_reminder_edit_markup(rec)
                await query.edit_message_text(f"✅ <b>Updated successfully!</b>\n\n{card}", reply_markup=markup, parse_mode="HTML")
                return await query.answer("Updated!")
            except Exception as e:
                logger.error(f"edset error: {e}")
                return await query.answer("Error updating reminder.", show_alert=True)

    await query.answer()


def build_reminder_card_text(r: dict) -> str:
    r_id = str(r.get('_id', ''))
    raw_label = str(r.get('label', 'No Label'))
    label = html.escape(raw_label)
    raw_cat = str(r.get('category', 'General'))
    cat = html.escape(raw_cat)
    date_val = html.escape(str(r.get('target_date', '')))
    concise_d = html.escape(format_concise_date(date_val))
    time_val = html.escape(str(r.get('reminder_time', '09:00')))
    rec = html.escape(str(r.get('recurrence', 'once')).capitalize())

    is_bday = (raw_cat.lower() == "birthday" or "birthday" in raw_label.lower() or "🎂" in raw_label)

    b_date = r.get('birth_date')
    b_year = r.get('birth_year')
    age = calculate_upcoming_age(r)

    if b_date:
        b_str = f"<code>{html.escape(str(b_date))}</code>"
        if age is not None:
            b_str += f" (Turning {age})"
    elif b_year:
        b_str = f"<code>{b_year}</code>"
        if age is not None:
            b_str += f" (Turning {age})"
    else:
        b_str = "Not set"

    is_spot = bool(r.get('is_spotlight', False))
    spot_str = "Active ✦" if is_spot else "Off"

    lines = [
        "<b>CUSTOMIZE REMINDER</b>",
        "",
        f"<b>Label:</b> <code>{label}</code>",
        f"<b>Category:</b> {cat}",
        f"<b>Target Date:</b> <code>{concise_d}</code>",
        f"<b>Time:</b> <code>{time_val}</code>",
    ]
    if not is_bday:
        lines.append(f"<b>Recurrence:</b> {rec}")
    lines.extend([
        f"<b>Birth Date:</b> {b_str}",
        f"<b>Spotlight:</b> {spot_str}",
        f"<b>ID:</b> <code>{r_id}</code>"
    ])
    return "\n".join(lines)


def build_reminder_edit_markup(r_id_or_rec, is_spotlight: bool | None = None):
    if isinstance(r_id_or_rec, dict):
        r_id = str(r_id_or_rec.get('_id', ''))
        if is_spotlight is None:
            is_spotlight = bool(r_id_or_rec.get('is_spotlight', False))
    else:
        r_id = str(r_id_or_rec)
        if is_spotlight is None:
            is_spotlight = False

    spotlight_btn = (
        InlineKeyboardButton("✕ Remove Spotlight", callback_data=f"edfield_{r_id}_spotlight_off")
        if is_spotlight
        else InlineKeyboardButton("✦ Spotlight", callback_data=f"edfield_{r_id}_spotlight_on")
    )
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Send on the Spot", callback_data=f"remfire_{r_id}")],
        [
            spotlight_btn,
            InlineKeyboardButton("🎂 Set Birth Date", callback_data=f"edfield_{r_id}_bdate")
        ],
        [InlineKeyboardButton("✏️ Edit Details", callback_data=f"edfield_{r_id}_details")],
        [InlineKeyboardButton("🗑️ Delete Reminder", callback_data=f"delrem_{r_id}")],
        [InlineKeyboardButton("🔙 Back to List", callback_data="edfield_global_back")]
    ])


async def edit_reminder_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command router: /editrem <ID_OR_NUM> [field] [value]"""
    user_id = update.effective_user.id
    args = context.args or []
    if not args:
        return await list_reminders(update, context)

    r_id_arg = args[0].strip()
    oid, rec = await resolve_reminder_id(user_id, r_id_arg)
    if not rec or oid is None:
        return await update.effective_message.reply_text("❌ Reminder not found or permission denied.")

    r_id = str(oid)
    if len(args) == 1:
        text = build_reminder_card_text(rec)
        markup = build_reminder_edit_markup(rec)
        return await update.effective_message.reply_text(text, reply_markup=markup, parse_mode="HTML")

    field = args[1].lower().strip()
    val = " ".join(args[2:]).strip()

    if field in ["delete", "del", "remove"]:
        await reminders_col.delete_one({"_id": oid, "user_id": user_id})
        if context.job_queue:
            for job in context.job_queue.get_jobs_by_name(str(oid)):
                job.schedule_removal()
            remaining = await reminders_col.count_documents({"user_id": user_id})
            if remaining == 0:
                for job in context.job_queue.jobs():
                    if job.name and job.name.startswith(f"user_rem_{user_id}_"):
                        job.schedule_removal()
        return await update.effective_message.reply_text("🗑️ <b>Reminder deleted.</b>", parse_mode="HTML")

    if field in ["spotlight", "star", "pinned"]:
        if not val or val.lower() == "toggle":
            new_spotlight = not bool(rec.get("is_spotlight", False))
        elif val.lower() in ["on", "true", "yes", "1", "enable"]:
            new_spotlight = True
        elif val.lower() in ["off", "false", "no", "0", "disable"]:
            new_spotlight = False
        else:
            return await update.effective_message.reply_text("❌ Spotlight value must be 'on' or 'off'.")

        if new_spotlight:
            await reminders_col.update_many({"user_id": user_id, "is_spotlight": True}, {"$set": {"is_spotlight": False}})
            await reminders_col.update_one({"_id": oid}, {"$set": {"is_spotlight": True}})
        else:
            await reminders_col.update_one({"_id": oid}, {"$set": {"is_spotlight": False}})

        updated_rec = await reminders_col.find_one({"_id": oid})
        card = build_reminder_card_text(updated_rec)
        markup = build_reminder_edit_markup(updated_rec)
        return await update.effective_message.reply_text(f"✅ <b>Spotlight Updated!</b>\n\n{card}", reply_markup=markup, parse_mode="HTML")

    if not val:
        return await update.effective_message.reply_text(
            f"❌ Please specify a new value. Example: <code>/editrem {r_id} label Birthday Party</code>",
            parse_mode="HTML"
        )

    updates = {}
    if field in ["birthdate", "birth_date", "bdate", "birthday_date"]:
        if val.lower() in ["none", "clear", "remove", "null", "delete"]:
            updates["birth_date"] = None
            updates["birth_year"] = None
        else:
            try:
                b_dt = datetime.strptime(val, "%Y-%m-%d").date()
                updates["birth_date"] = val
                updates["birth_year"] = b_dt.year
            except ValueError:
                return await update.effective_message.reply_text("❌ Invalid birth date format. Expected YYYY-MM-DD (e.g. 1995-06-20).")

    elif field in ["birthyear", "birth_year", "byear"]:
        if val.lower() in ["none", "clear", "remove", "null", "delete"]:
            updates["birth_year"] = None
            updates["birth_date"] = None
        else:
            try:
                byear = int(val)
                if 1900 <= byear <= datetime.now().year + 1:
                    updates["birth_year"] = byear
                else:
                    return await update.effective_message.reply_text("❌ Please enter a realistic 4-digit birth year (e.g. 1995).")
            except ValueError:
                return await update.effective_message.reply_text("❌ Invalid birth year. Expected 4-digit year (e.g. 1995).")

    elif field in ["label", "name", "title"]:
        updates["label"] = val[:120]
    elif field in ["category", "cat"]:
        new_cat = val.capitalize()
        updates["category"] = new_cat
        if new_cat == "Birthday":
            updates["recurrence"] = "annually"
    elif field in ["time", "reminder_time"]:
        parts = val.split(":")
        valid = False
        if len(parts) == 2:
            try:
                h, m = int(parts[0]), int(parts[1])
                if 0 <= h <= 23 and 0 <= m <= 59:
                    updates["reminder_time"] = f"{h:02d}:{m:02d}"
                    valid = True
            except ValueError:
                valid = False
        if not valid:
            return await update.effective_message.reply_text("❌ Invalid time format. Expected HH:MM (24-hour).")
    elif field in ["date", "target_date"]:
        try:
            datetime.strptime(val, "%Y-%m-%d")
            updates["target_date"] = val
            updates["last_notified"] = ""
        except ValueError:
            return await update.effective_message.reply_text("❌ Invalid date format. Expected YYYY-MM-DD.")
    elif field in ["rec", "recurrence"]:
        raw_cat = str(rec.get("category", "")).strip()
        raw_lbl = str(rec.get("label", ""))
        if raw_cat.lower() == "birthday" or "birthday" in raw_lbl.lower() or "🎂" in raw_lbl:
            return await update.effective_message.reply_text("🎂 Birthday reminders automatically recur annually.")
        if val.lower() in ["once", "daily", "weekly", "monthly", "annually"]:
            updates["recurrence"] = val.lower()
        else:
            return await update.effective_message.reply_text("❌ Recurrence must be: once, daily, weekly, monthly, or annually.")
    else:
        return await update.effective_message.reply_text("❌ Unknown field. Options: label, category, time, date, recurrence, birthdate, birthyear, spotlight.")

    await reminders_col.update_one({"_id": oid}, {"$set": updates})
    updated_rec = await reminders_col.find_one({"_id": oid})
    if context.application and updated_rec:
        schedule_reminder_job(context.application, updated_rec)

    card = build_reminder_card_text(updated_rec)
    markup = build_reminder_edit_markup(updated_rec)
    await update.effective_message.reply_text(f"✅ <b>Reminder Updated!</b>\n\n{card}", reply_markup=markup, parse_mode="HTML")


async def handle_pending_birth_date_input(update: Update, context: ContextTypes.DEFAULT_TYPE, r_id: str, text: str):
    """Processes conversational / text birth date entry for a reminder."""
    user_id = update.effective_user.id
    oid, rec = await resolve_reminder_id(user_id, r_id)
    if not rec or oid is None:
        context.user_data.pop('pending_bdate_rid', None)
        return await update.effective_message.reply_text("❌ Reminder not found or permission denied.")

    val = text.strip()
    if val.lower() in ["clear", "none", "delete", "remove"]:
        await reminders_col.update_one({"_id": oid, "user_id": user_id}, {"$set": {"birth_date": None, "birth_year": None}})
        context.user_data.pop('pending_bdate_rid', None)
        rec["birth_date"] = None
        rec["birth_year"] = None
        card = build_reminder_card_text(rec)
        markup = build_reminder_edit_markup(rec)
        return await update.effective_message.reply_text(f"✅ <b>Birth Date Removed!</b>\n\n{card}", reply_markup=markup, parse_mode="HTML")

    if re.match(r"^\d{4}-\d{2}-\d{2}$", val):
        try:
            b_dt = datetime.strptime(val, "%Y-%m-%d").date()
            updates = {"birth_date": val, "birth_year": b_dt.year}
            await reminders_col.update_one({"_id": oid, "user_id": user_id}, {"$set": updates})
            context.user_data.pop('pending_bdate_rid', None)
            rec.update(updates)
            card = build_reminder_card_text(rec)
            markup = build_reminder_edit_markup(rec)
            return await update.effective_message.reply_text(f"✅ <b>Birth Date Updated!</b>\n\n{card}", reply_markup=markup, parse_mode="HTML")
        except ValueError:
            return await update.effective_message.reply_text("❌ Invalid birth date format. Expected YYYY-MM-DD (e.g. 1995-06-20).")

    if re.match(r"^\d{4}$", val):
        try:
            byear = int(val)
            if 1900 <= byear <= datetime.now().year + 1:
                t_date = rec.get("target_date", "")
                b_date = None
                if len(t_date) >= 10:
                    b_date = f"{byear}-{t_date[5:10]}"
                updates = {"birth_year": byear, "birth_date": b_date}
                await reminders_col.update_one({"_id": oid, "user_id": user_id}, {"$set": updates})
                context.user_data.pop('pending_bdate_rid', None)
                rec.update(updates)
                card = build_reminder_card_text(rec)
                markup = build_reminder_edit_markup(rec)
                return await update.effective_message.reply_text(f"✅ <b>Birth Year Updated!</b>\n\n{card}", reply_markup=markup, parse_mode="HTML")
            else:
                return await update.effective_message.reply_text("❌ Please enter a realistic 4-digit birth year (e.g. 1995).")
        except ValueError:
            pass

    return await update.effective_message.reply_text(
        "❌ Invalid format. Please send a date as <code>YYYY-MM-DD</code> or 4-digit year <code>YYYY</code>, or send /cancel.",
        parse_mode="HTML"
    )


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

        raw_lbl = str(context.user_data.get('label', ''))
        raw_cat = str(context.user_data.get('category', ''))
        is_bday = (raw_cat.lower() == "birthday" or "birthday" in raw_lbl.lower() or "🎂" in raw_lbl)

        category = "Birthday" if is_bday else (raw_cat or "General")
        recurrence = "annually" if is_bday else (context.user_data.get('recurrence') or "once")

        data = {
            "user_id": user_id,
            "timezone": context.user_data['timezone'],
            "target_date": context.user_data['target_date'],
            "reminder_time": context.user_data['reminder_time'],
            "label": context.user_data['label'],
            "category": category,
            "recurrence": recurrence,
            "birth_date": None,
            "birth_year": None,
            "is_spotlight": False,
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

        is_bday = (category.lower() == "birthday" or "birthday" in str(data.get('label', '')).lower() or "🎂" in str(data.get('label', '')))
        rec_info = "" if is_bday else (f" (🔄 {recurrence.capitalize()})" if recurrence != "once" else "")
        c_date = format_concise_date(data['target_date'])
        success_text = f"🚀 **Reminder Armed!**\n\n🏷️ `{data['label']}` [{category}]\n📅 `{c_date}`{rec_info}\n\n{status_note}"

        if message.from_user and message.from_user.is_bot:
            try: await message.edit_text(success_text, parse_mode="Markdown")
            except: await message.reply_text(success_text, parse_mode="Markdown")
        else:
            await message.reply_text(success_text, parse_mode="Markdown")

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
