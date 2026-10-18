import re
import uuid
import asyncio
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InlineQueryResultCachedVideo,
    InlineQueryResultCachedDocument,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedAudio,
    InlineQueryResultCachedVoice,
    InlineQueryResultCachedMpeg4Gif,
)
from telegram.ext import ContextTypes

from config import ADMIN_ID, logger
from database import codes_col, group_keys_col, unlocked_groups_col, prefix_labels_col
from utils.helpers import delete_msg_callback
from utils.sentinel import log_event, notify_admin
from utils.rate_limiter import is_rate_limited
from services.access import (
    check_access_gate,
    is_locked_out,
    record_failed_attempt,
    reset_failed_attempts,
)
from services.ascii_art import ascii_command_handler
from services.tmdb import fetch_tmdb_metadata


async def browse_library(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📺 Series", callback_data="lib_cat_series"), 
         InlineKeyboardButton("🎬 Movies", callback_data="lib_cat_movie")],
        [InlineKeyboardButton("🌐 All Titles (Unified Library)", callback_data="lib_cat_all")]
    ]
    text = "📂 **CONTENT LIBRARY**\nSelect browsing mode:"
    if update.callback_query: 
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else: 
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def show_alphabet_selector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    cat = query.data.split('_')[2]
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ#"
    keyboard = []
    row = []
    for letter in alphabet:
        row.append(InlineKeyboardButton(letter, callback_data=f"lib_alpha_{cat}_{letter}"))
        if len(row) == 5:
            keyboard.append(row)
            row = []
    if row: keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Back to Categories", callback_data="lib_back")])
    cat_label = "ALL TITLES" if cat == "all" else cat.upper()
    await query.edit_message_text(f"📂 **{cat_label} - Select Letter**\nChoose starting letter:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def show_filtered_library_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split('_')
    cat, letter = parts[2], parts[3]
    escaped_letter = re.escape(letter)
    regex = f"^{escaped_letter}" if letter != "#" else "^[^A-Za-z]"

    query_filter = {"name": {"$regex": regex, "$options": "i"}}
    if cat in ["series", "movie"]:
        query_filter["category"] = cat

    cursor = prefix_labels_col.find(query_filter).sort("name", 1)
    prefixes = await cursor.to_list(length=100)
    if not prefixes:
        cat_name = "titles" if cat == "all" else cat
        return await query.answer(f"No {cat_name} starting with '{letter}'.", show_alert=True)
    
    keyboard = []
    for p in prefixes:
        p_cat = p.get("category", "movie")
        cat_icon = "🎬" if p_cat == "movie" else "📺"
        tmdb = p.get("tmdb", {})
        rating_str = f" ⭐{tmdb['rating']}" if tmdb.get("rating") else ""
        keyboard.append([InlineKeyboardButton(f"{cat_icon} {p['name']}{rating_str}", callback_data=f"lib_pick_{p_cat}_{p['prefix']}")])
        
    keyboard.append([InlineKeyboardButton("🔙 Back to Alphabet", callback_data=f"lib_cat_{cat}")])
    cat_label = "ALL TITLES" if cat == "all" else cat.upper()
    await query.edit_message_text(f"📂 **{cat_label} ({letter})**\nSelect a title:ㅤㅤㅤㅤ", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def handle_library_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split('_')
    cat, prefix = parts[2], parts[3]
    escaped_prefix = re.escape(prefix)
    label_rec = await prefix_labels_col.find_one({"prefix": prefix})
    name = label_rec['name'] if label_rec else prefix
    first_letter = name[0].upper() if name and name[0].isalpha() else "#"

    # TMDB metadata (only if handpicked and explicitly attached by admin)
    tmdb = label_rec.get("tmdb") if label_rec else None

    # Format TMDB official poster / backdrop banner & rich overview
    banner_img_url = ""
    header_parts = []
    cat_icon = "🎬" if cat == "movie" else "📺"

    custom_banner = label_rec.get("custom_banner") if label_rec else None
    if custom_banner:
        banner_img_url = custom_banner
    elif tmdb:
        banner_mode = label_rec.get("banner_mode", "poster") if label_rec else "poster"
        if banner_mode == "backdrop":
            banner_img_url = tmdb.get("backdrop_url") or tmdb.get("poster_url") or ""
        else:
            banner_img_url = tmdb.get("poster_url") or tmdb.get("backdrop_url") or ""

    if tmdb:
        title_display = tmdb.get("title") or name
        year_display = f" ({tmdb['year']})" if tmdb.get("year") else ""
        header_parts.append(f"{cat_icon} **{title_display}**{year_display}")

        if tmdb.get("rating"):
            header_parts.append(f"⭐ **Rating:** `{tmdb['rating']}/10`")
        if tmdb.get("genres"):
            header_parts.append(f"🎭 **Genres:** {tmdb['genres']}")
        if tmdb.get("overview"):
            synopsis = tmdb["overview"]
            if len(synopsis) > 280:
                synopsis = synopsis[:275] + "..."
            header_parts.append(f"\n📖 **Overview:**\n_{synopsis}_")
    else:
        header_parts.append(f"{cat_icon} **{name}**")

    # Embed poster image link at top of markdown for Telegram rich preview
    banner_prefix = f"[\u200b]({banner_img_url})\n" if banner_img_url else ""
    card_text = banner_prefix + "\n".join(header_parts)

    if cat == "movie":
        cursor = codes_col.find({"code": {"$regex": f"^{escaped_prefix}"}}).sort("code", 1)
        items = await cursor.to_list(length=100)
        keyboard = []
        for item in items:
            label = item.get("file_name") or item.get("caption", "").strip() or item["code"]
            if len(label) > 35: label = label[:32] + "..."
            keyboard.append([InlineKeyboardButton(f"🎬 {label}", callback_data=f"lib_dl_{item['code']}_one")])
        keyboard.append([InlineKeyboardButton("🔙 Back to Library", callback_data=f"lib_alpha_{cat}_{first_letter}")])
        await query.edit_message_text(card_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        seasons = label_rec.get("seasons", []) if label_rec else []
        if seasons:
            keyboard = [[InlineKeyboardButton(f"❄️ Season {sn['num']}", callback_data=f"lib_sn_{prefix}_{sn['num']}")] for sn in seasons]
            keyboard.append([InlineKeyboardButton("🔙 Back to Library", callback_data=f"lib_alpha_{cat}_{first_letter}")])
            await query.edit_message_text(card_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        else:
            cursor = codes_col.find({"code": {"$regex": f"^{escaped_prefix}"}}).sort("code", 1)
            items = await cursor.to_list(length=100)
            keyboard, row = [], []
            for i, item in enumerate(items):
                row.append(InlineKeyboardButton(f"Ep {i+1}", callback_data=f"lib_dl_{item['code']}_one"))
                if len(row) == 4: keyboard.append(row); row = []
            if row: keyboard.append(row)
            keyboard.append([InlineKeyboardButton("🔙 Back to Library", callback_data=f"lib_alpha_{cat}_{first_letter}")])
            await query.edit_message_text(card_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def handle_season_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split('_')
    prefix, sn_num = parts[2], int(parts[3])
    label_rec = await prefix_labels_col.find_one({"prefix": prefix})
    name = label_rec['name'] if label_rec else prefix
    season_obj = next((s for s in label_rec['seasons'] if s['num'] == sn_num), None) if label_rec else None
    if not season_obj: return await query.answer("Error.")

    tmdb = label_rec.get("tmdb") if label_rec else None
    custom_banner = label_rec.get("custom_banner") if label_rec else None
    if custom_banner:
        banner_img_url = custom_banner
    elif tmdb:
        banner_mode = label_rec.get("banner_mode", "poster") if label_rec else "poster"
        banner_img_url = (tmdb.get("backdrop_url") or tmdb.get("poster_url") or "") if banner_mode == "backdrop" else (tmdb.get("poster_url") or tmdb.get("backdrop_url") or "")
    else:
        banner_img_url = ""
    banner_prefix = f"[\u200b]({banner_img_url})\n" if banner_img_url else ""
    rating_str = f" ⭐ `{tmdb['rating']}/10`" if tmdb and tmdb.get("rating") else ""
    season_header = f"{banner_prefix}📺 **{name}** — Season {sn_num}{rating_str}\n\nSelect episode to download:"

    start, end = season_obj['start'], season_obj['end']
    target_codes = [f"{prefix}{i:03d}" for i in range(start, end + 1)]
    cursor = codes_col.find({"code": {"$in": target_codes}}).sort("code", 1)
    items = await cursor.to_list(length=100)
    keyboard, row = [], []
    for i, item in enumerate(items):
        row.append(InlineKeyboardButton(f"Ep {i+1}", callback_data=f"lib_dl_{item['code']}_one"))
        if len(row) == 4: keyboard.append(row); row = []
    if row: keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Back to Seasons", callback_data=f"lib_pick_series_{prefix}")])
    await query.edit_message_text(season_header, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")



async def execute_file_delivery(chat_id, record, context, user, send_alert=True):
    """Delivers file to user and appends Next Episode button if part of an episodic series."""
    try:
        f_type, f_id, caption = record.get("file_type"), record.get("file_id"), record.get("caption", "")
        code = record.get("code", "")
        prefix = code[:3].upper() if len(code) >= 3 else ""

        # Build One-Click Next Episode button if next code exists in series
        reply_markup = None
        if prefix and len(code) > 3 and code[3:].isdigit():
            curr_num = int(code[3:])
            next_code = f"{prefix}{curr_num + 1:03d}"
            next_record = await codes_col.find_one({"code": next_code})
            if next_record:
                reply_markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"▶️ Next: Ep {curr_num + 1}", callback_data=f"next_ep_{next_code}")]
                ])

        if f_id and f_type:
            if f_type == "video": msg = await context.bot.send_video(chat_id, video=f_id, caption=caption, reply_markup=reply_markup)
            elif f_type == "document": msg = await context.bot.send_document(chat_id, document=f_id, caption=caption, reply_markup=reply_markup)
            elif f_type == "photo": msg = await context.bot.send_photo(chat_id, photo=f_id, caption=caption, reply_markup=reply_markup)
            elif f_type == "audio": msg = await context.bot.send_audio(chat_id, audio=f_id, caption=caption, reply_markup=reply_markup)
            elif f_type == "voice": msg = await context.bot.send_voice(chat_id, voice=f_id, caption=caption, reply_markup=reply_markup)
            elif f_type == "animation": msg = await context.bot.send_animation(chat_id, animation=f_id, caption=caption, reply_markup=reply_markup)
            else: msg = await context.bot.copy_message(chat_id, from_chat_id=record["chat_id"], message_id=record["message_id"], reply_markup=reply_markup)
        else: msg = await context.bot.copy_message(chat_id, from_chat_id=record["chat_id"], message_id=record["message_id"], reply_markup=reply_markup)
        
        await log_event(user.id, user.username, f"Fetched: {record['code']}")
        await notify_admin(context, f"📦 User `{user.id}` fetched `{record['code']}`.")
        
        if send_alert:
            warn = await context.bot.send_message(chat_id, "⚠️File(s) are *EPHEMERAL*⚠️\n\nFile will be deleted in 3 minutes due to copyright.\n\nPlease forward it to your *Saved Messages* to download", parse_mode="Markdown")
            context.job_queue.run_once(delete_msg_callback, 180, data={"chat_id": chat_id, "message_id": msg.message_id})
            context.job_queue.run_once(delete_msg_callback, 180, data={"chat_id": chat_id, "message_id": warn.message_id})
        return msg
    except Exception as e:
        logger.error(f"Delivery Error: {e}")
        return None


async def handle_next_episode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """One-click callback handler for sequential episode streaming."""
    query = update.callback_query
    next_code = query.data.split('_')[2]
    user = update.effective_user
    chat_id = update.effective_chat.id

    record = await codes_col.find_one({"code": next_code})
    if not record:
        return await query.answer("Next episode not found in catalog.", show_alert=True)

    authorized, prefix, p_chat_id, is_frozen, is_expired = await check_access_gate(user.id, record)
    if not authorized:
        if is_frozen:
            return await query.answer(f"Access to '{prefix}' has been frozen.", show_alert=True)
        elif is_expired:
            return await query.answer(f"Access to '{prefix}' has expired.", show_alert=True)
        context.user_data['pending_unlock_group_id'] = p_chat_id
        context.user_data['pending_unlock_prefix'] = prefix
        context.user_data['interrupted_file_codes'] = [next_code]
        alert = await context.bot.send_message(chat_id, f"🔒 Prefix '{prefix}' is Locked. Enter Access Key:")
        context.user_data['alert_message_id'] = alert.message_id
        return await query.answer("Key required.", show_alert=True)

    await query.answer("Delivering next episode...")
    await execute_file_delivery(chat_id, record, context, user, send_alert=True)


async def handle_library_delivery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split('_')
    target, mode = parts[2], parts[3]
    user = update.effective_user
    chat_id = update.effective_chat.id
    if mode == "all":
        escaped_target = re.escape(target)
        cursor = codes_col.find({"code": {"$regex": f"^{escaped_target}"}}).sort("code", 1)
        records = await cursor.to_list(length=100)
        if not records:
            return await query.answer("No items found.")
        authorized, prefix, p_chat_id, is_frozen, is_expired = await check_access_gate(user.id, records[0])
        if not authorized:
            if is_frozen:
                await context.bot.send_message(chat_id, f"❄️ Access to prefix '{prefix}' has been temporarily frozen by administrator.")
                return await query.answer("Access frozen.", show_alert=True)
            elif is_expired:
                await context.bot.send_message(chat_id, f"⏳ Guest access to prefix '{prefix}' has expired.")
                return await query.answer("Access expired.", show_alert=True)
            context.user_data['pending_unlock_group_id'] = p_chat_id
            context.user_data['pending_unlock_prefix'] = prefix
            context.user_data['interrupted_file_codes'] = [r['code'] for r in records]
            alert = await context.bot.send_message(chat_id, f"🔒 Prefix '{prefix}' is Locked. Enter Access Key:")
            context.user_data['alert_message_id'] = alert.message_id
            return await query.answer("Key required.", show_alert=True)
        for r in records:
            await execute_file_delivery(chat_id, r, context, user, send_alert=True)
            await asyncio.sleep(0.15)
    else:
        record = await codes_col.find_one({"code": target})
        if record:
            authorized, prefix, p_chat_id, is_frozen, is_expired = await check_access_gate(user.id, record)
            if not authorized:
                if is_frozen:
                    await context.bot.send_message(chat_id, f"❄️ Access to prefix '{prefix}' has been temporarily frozen by administrator.")
                    return await query.answer("Access frozen.", show_alert=True)
                elif is_expired:
                    await context.bot.send_message(chat_id, f"⏳ Guest access to prefix '{prefix}' has expired.")
                    return await query.answer("Access expired.", show_alert=True)
                context.user_data['pending_unlock_group_id'] = p_chat_id
                context.user_data['pending_unlock_prefix'] = prefix
                context.user_data['interrupted_file_codes'] = [target]
                alert = await context.bot.send_message(chat_id, f"🔒 Prefix '{prefix}' is Locked. Enter Access Key:")
                context.user_data['alert_message_id'] = alert.message_id
                return await query.answer("Key required.", show_alert=True)
            await execute_file_delivery(chat_id, record, context, user, send_alert=True)
        else:
            return await query.answer("Item not found.", show_alert=True)
    await query.answer("Delivering...")


async def get_file_command(update: Update, context: ContextTypes.DEFAULT_TYPE, force_args=None):
    if codes_col is None: return
    args = force_args if force_args is not None else context.args
    chat_id = update.effective_chat.id
    if not args: return await browse_library(update, context)
    user = update.effective_user
    is_admin = (user.id == ADMIN_ID)
    
    # Rate limit check for regular users
    if not is_admin:
        limited, retry_after = is_rate_limited(user.id, max_hits=10, window_seconds=30)
        if limited:
            return await update.message.reply_text(f"⏳ **Rate limit exceeded.** Please wait `{retry_after}s` before requesting more files.", parse_mode="Markdown")

    if len(args) == 1:
        code = args[0].upper().strip()
        record = await codes_col.find_one({"code": code})
        if record:
            authorized, prefix, p_chat_id, is_frozen, is_expired = await check_access_gate(user.id, record)
            if not authorized:
                if is_frozen:
                    return await context.bot.send_message(chat_id, f"❄️ Access to prefix '{prefix}' has been temporarily frozen by administrator.")
                elif is_expired:
                    return await context.bot.send_message(chat_id, f"⏳ Guest access to prefix '{prefix}' has expired.")
                context.user_data['pending_unlock_group_id'] = p_chat_id
                context.user_data['pending_unlock_prefix'] = prefix
                context.user_data['interrupted_file_codes'] = [code]
                alert = await context.bot.send_message(chat_id, f"🔒 '{prefix}' Locked. Enter Key (or /cancel):")
                context.user_data['alert_message_id'] = alert.message_id
                return
            await execute_file_delivery(chat_id, record, context, user, send_alert=True)
        else: await update.message.reply_text("❌ Not found.")
    elif len(args) == 3:
        prefix_arg = args[0].upper().strip()[:3]
        try:
            start_num, end_num = int(args[1]), int(args[2])
        except ValueError:
            return await update.message.reply_text("❌ Start and End numbers must be integers.")
            
        if start_num < 1 or end_num < start_num:
            return await update.message.reply_text("❌ Invalid range: START must be <= END and >= 1.")
            
        if (end_num - start_num + 1) > 100:
            return await update.message.reply_text("❌ Range error: Maximum 100 items can be requested at once.")
            
        target_codes = [f"{prefix_arg}{i:03d}" for i in range(start_num, end_num + 1)]
        cursor = codes_col.find({"code": {"$in": target_codes}}).sort("code", 1)
        records = await cursor.to_list(length=100)
        if not records:
            return await update.message.reply_text("❌ No items found in that range.")
            
        if not is_admin:
            for record in records:
                authorized, p, p_chat, is_frozen, is_expired = await check_access_gate(user.id, record)
                if not authorized:
                    if is_frozen:
                        return await context.bot.send_message(chat_id, f"❄️ Access to prefix '{p}' has been temporarily frozen by administrator.")
                    elif is_expired:
                        return await context.bot.send_message(chat_id, f"⏳ Guest access to prefix '{p}' has expired.")
                    context.user_data['pending_unlock_group_id'] = p_chat
                    context.user_data['pending_unlock_prefix'] = p
                    context.user_data['interrupted_file_codes'] = target_codes
                    alert = await context.bot.send_message(chat_id, f"🔒 '{p}' Locked. Enter Key (or /cancel):")
                    context.user_data['alert_message_id'] = alert.message_id
                    return
                    
        delivered_ids = []
        for record in records:
            msg = await execute_file_delivery(chat_id, record, context, user, send_alert=False)
            if msg: delivered_ids.append(msg.message_id)
            await asyncio.sleep(0.15)
        if delivered_ids:
            warn = await context.bot.send_message(chat_id, "⚠️File(s) are *EPHEMERAL*⚠️\n\nFile will be deleted in 3 minutes due to copyright.\n\nPlease forward it to your *Saved Messages* to download", parse_mode="Markdown")
            for m_id in delivered_ids: context.job_queue.run_once(delete_msg_callback, 180, data={"chat_id": chat_id, "message_id": m_id})
            context.job_queue.run_once(delete_msg_callback, 180, data={"chat_id": chat_id, "message_id": warn.message_id})


async def core_routing_manager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text or codes_col is None: return
    user, chat_id, text = update.effective_user, update.effective_chat.id, update.message.text.strip()
    is_admin = (user.id == ADMIN_ID)
    
    # Check pending passkey prompt
    pending_chat = context.user_data.get('pending_unlock_group_id')
    pending_prefix = context.user_data.get('pending_unlock_prefix')
    if pending_chat and pending_prefix and not is_admin:
        if text.lower() == "/cancel":
            context.user_data.pop('pending_unlock_group_id', None)
            context.user_data.pop('pending_unlock_prefix', None)
            context.user_data.pop('interrupted_file_codes', None)
            alert_id = context.user_data.pop('alert_message_id', None)
            if alert_id:
                try: await context.bot.delete_message(chat_id, alert_id)
                except: pass
            return await update.message.reply_text("🚫 Unlock attempt cancelled.")

        # Brute-force lockout verification
        locked, rem_secs = is_locked_out(user.id)
        if locked:
            rem_mins = max(1, rem_secs // 60)
            return await update.message.reply_text(f"⛔ **Account Locked**: Too many failed passkey attempts. Please wait `{rem_mins} minutes`.", parse_mode="Markdown")

        try: await update.message.delete()
        except: pass
        
        gate = await group_keys_col.find_one({"chat_id": pending_chat, "prefix": pending_prefix})
        if gate and text == gate.get("secret_key"):
            reset_failed_attempts(user.id)
            await unlocked_groups_col.update_one({"user_id": user.id}, {"$addToSet": {"unlocked_prefixes": f"{pending_chat}_{pending_prefix}"}}, upsert=True)
            alert_id = context.user_data.pop('alert_message_id', None)
            if alert_id:
                try: await context.bot.delete_message(chat_id, alert_id)
                except: pass
            codes = context.user_data.pop('interrupted_file_codes', [])
            context.user_data.pop('pending_unlock_group_id', None)
            context.user_data.pop('pending_unlock_prefix', None)
            if codes:
                delivered_ids = []
                cursor = codes_col.find({"code": {"$in": codes}}).sort("code", 1)
                async for record in cursor:
                    m = await execute_file_delivery(chat_id, record, context, user, send_alert=False)
                    if m: delivered_ids.append(m.message_id)
                    await asyncio.sleep(0.15)
                if delivered_ids:
                    warn = await context.bot.send_message(chat_id, "⚠️File(s) are *EPHEMERAL*⚠️\n\nFile will be deleted in 3 minutes due to copyright.\n\nPlease forward it to your *Saved Messages* to download", parse_mode="Markdown")
                    for m_id in delivered_ids: context.job_queue.run_once(delete_msg_callback, 180, data={"chat_id": chat_id, "message_id": m_id})
                    context.job_queue.run_once(delete_msg_callback, 180, data={"chat_id": chat_id, "message_id": warn.message_id})
        else:
            fails, just_locked = record_failed_attempt(user.id)
            if just_locked:
                await notify_admin(context, f"🛡️ **SECURITY ALERT**: User `{user.id}` locked out for 1 hour after 5 failed password attempts on `{pending_prefix}`.")
                await context.bot.send_message(chat_id, f"⛔ **Security Lockout**: 5 failed passkey attempts on '{pending_prefix}'. You are locked out for 1 hour.")
                context.user_data.pop('pending_unlock_group_id', None)
                context.user_data.pop('pending_unlock_prefix', None)
            else:
                remaining = 5 - fails
                await notify_admin(context, f"🛡️ SECURITY: Unauthorized key attempt on `{pending_prefix}` by `{user.id}` (Fail {fails}/5).")
                await context.bot.send_message(chat_id, f"❌ Incorrect key for '{pending_prefix}'. {remaining} attempt(s) remaining before 1-hour lockout.")
        return

    # Slash Commands
    if text.upper().startswith("/GET "):
        parts = text.split()
        if len(parts) >= 2: return await get_file_command(update, context, force_args=parts[1:])
    if text.lower().startswith("/ascii"): return await ascii_command_handler(update, context)

    # Ignore other slash commands
    if text.startswith("/"):
        return

    # Plain-Text Keyword Search (no /search command needed)
    if not is_admin:
        limited, retry_after = is_rate_limited(user.id, max_hits=8, window_seconds=20)
        if limited:
            return await update.message.reply_text(f"⏳ Please wait `{retry_after}s` before searching again.", parse_mode="Markdown")

    escaped_query = re.escape(text)

    # 1. Search Prefix Titles
    matched_labels = await prefix_labels_col.find({
        "name": {"$regex": escaped_query, "$options": "i"}
    }).limit(6).to_list(length=6)

    if matched_labels:
        buttons = []
        for ml in matched_labels:
            cat = ml.get("category", "movie")
            p = ml.get("prefix")
            title_name = ml.get("name", p)
            tmdb = ml.get("tmdb", {})
            rating_str = f" ⭐{tmdb['rating']}" if tmdb.get("rating") else ""
            year_str = f" ({tmdb['year']})" if tmdb.get("year") else ""
            icon = "🎬" if cat == "movie" else "📺"
            buttons.append([InlineKeyboardButton(f"{icon} {title_name}{year_str}{rating_str}", callback_data=f"lib_pick_{cat}_{p}")])
        return await update.message.reply_text(
            f"🔍 **Found {len(matched_labels)} title(s) matching \"{text}\":**",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown"
        )

    # 2. Search Asset File Names and Captions
    matched_files = await codes_col.find({
        "$or": [
            {"caption": {"$regex": escaped_query, "$options": "i"}},
            {"file_name": {"$regex": escaped_query, "$options": "i"}}
        ]
    }).limit(6).to_list(length=6)

    if matched_files:
        buttons = []
        for mf in matched_files:
            fname = mf.get("file_name") or mf.get("caption", "").strip() or mf["code"]
            if len(fname) > 35: fname = fname[:32] + "..."
            buttons.append([InlineKeyboardButton(f"📦 {fname}", callback_data=f"lib_dl_{mf['code']}_one")])
        return await update.message.reply_text(
            f"🔍 **Found {len(matched_files)} asset(s) matching \"{text}\":**",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown"
        )

    # If length >= 3, indicate no results found
    if len(text) >= 3 and any(c.isalnum() for c in text):
        await update.message.reply_text(f"🔍 No catalog media found matching \"{text}\". Use `/get` to browse the library.", parse_mode="Markdown")


async def inline_query_manager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.strip().upper()
    if not query.startswith("/GET "): return await update.inline_query.answer([], cache_time=0)
    parts = query.split()
    if len(parts) < 2: return await update.inline_query.answer([], cache_time=0)
    user_id = update.effective_user.id
    results = []
    if len(parts) == 2:
        code = parts[1]
        record = await codes_col.find_one({"code": code})
        if not record: return await update.inline_query.answer([], cache_time=0)
        authorized, prefix, p_chat_id, is_frozen, is_expired = await check_access_gate(user_id, record)
        if not authorized:
            title = "❄️ Frozen" if is_frozen else ("⏳ Expired" if is_expired else "🔒 Locked")
            return await update.inline_query.answer([InlineQueryResultArticle(id=str(uuid.uuid4()), title=title, input_message_content=InputTextMessageContent(f"/get {code}"))], cache_time=0)
        f_type, f_id, caption = record.get("file_type"), record.get("file_id"), record.get("caption", "")
        uid = str(uuid.uuid4())
        if f_id:
            if f_type == "video": results.append(InlineQueryResultCachedVideo(id=uid, video_file_id=f_id, title=f"📦 {code}", caption=caption))
            elif f_type == "document": results.append(InlineQueryResultCachedDocument(id=uid, document_file_id=f_id, title=f"📦 {code}", caption=caption))
            elif f_type == "photo": results.append(InlineQueryResultCachedPhoto(id=uid, photo_file_id=f_id, caption=caption))
            elif f_type == "audio": results.append(InlineQueryResultCachedAudio(id=uid, audio_file_id=f_id, title=f"📦 {code}", caption=caption))
            elif f_type == "voice": results.append(InlineQueryResultCachedVoice(id=uid, voice_file_id=f_id, title=f"📦 {code}", caption=caption))
            elif f_type == "animation": results.append(InlineQueryResultCachedMpeg4Gif(id=uid, mpeg4_file_id=f_id, title=f"📦 {code}", caption=caption))
        if not results: results.append(InlineQueryResultArticle(id=uid, title=f"📦 {code}", input_message_content=InputTextMessageContent(f"/get {code}")))
    elif len(parts) == 4:
        prefix, start, end = parts[1], parts[2], parts[3]
        try:
            s_num, e_num = int(start), int(end)
            count = e_num - s_num + 1
            if count > 0: results.append(InlineQueryResultArticle(id=str(uuid.uuid4()), title=f"📦 Batch: {count} files", input_message_content=InputTextMessageContent(f"/get {prefix} {start} {end}")))
        except: pass
    await update.inline_query.answer(results, cache_time=0, is_personal=True)
