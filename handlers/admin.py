import os
import re
import json
import html
import time
import asyncio
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import ADMIN_ID, logger
from database import (
    codes_col,
    reminders_col,
    logs_col,
    group_keys_col,
    users_col,
    unlocked_groups_col,
    prefix_labels_col,
)
from utils.helpers import extract_file_data
from utils.sentinel import log_event, notify_admin
from services.tmdb import fetch_tmdb_metadata, clean_title_for_search


def parse_duration(d_str: str) -> float | None:
    """Parses duration string like '24h', '7d', '30m' into future unix timestamp."""
    d_str = d_str.strip().lower()
    multipliers = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    if len(d_str) > 1 and d_str[-1] in multipliers:
        try:
            val = float(d_str[:-1])
            return time.time() + (val * multipliers[d_str[-1]])
        except ValueError:
            return None
    return None


async def admin_palette_msg():
    keyboard = [
        [
            InlineKeyboardButton("📊 System Stats", callback_data="pal_stats"),
            InlineKeyboardButton("📦 Export Data", callback_data="pal_export")
        ],
        [
            InlineKeyboardButton("📜 All Reminders", callback_data="pal_alllists"),
            InlineKeyboardButton("🗝️ Key Matrix", callback_data="pal_keys")
        ],
        [
            InlineKeyboardButton("👥 Access Matrix", callback_data="pal_access"),
            InlineKeyboardButton("📋 System Logs", callback_data="pal_logs")
        ],
        [
            InlineKeyboardButton("🗑️ Manage DB", callback_data="pal_manage"),
            InlineKeyboardButton("🔄 Sync Metadata", callback_data="pal_sync")
        ]
    ]

    text = (
        "👑 **MASTER CONSOLE (Ultimate Master)**\n"
        "───────────────────────\n"
        "System Status: 🌐 Operational\n"
        "───────────────────────\n"
        "📜 COMPLETE COMMAND LIST:\n\n"
        "Core Commands:\n"
        "• `/start` / `/admin` - Dashboard\n"
        "• `/remind` - Setup countdown (GUI)\n"
        "• `/list` - View reminders\n"
        "• `/ascii` - Text to ASCII art\n\n"
        "Access Management:\n"
        "• `/grant USER_ID PREFIX [DURATION]` - Grant media access (e.g. 24h, 7d)\n"
        "• `/revoke USER_ID PREFIX` - Remove media access\n"
        "• `/freeze USER_ID PREFIX` - Suspend media access\n"
        "• `/unfreeze USER_ID PREFIX` - Restore media access\n\n"
        "Database Matrix:\n"
        "• `/save CODE` - Index by reply\n"
        "• `/autobulk START END PREFIX` - Mass index\n"
        "• `/del CODE` - Single file delete\n"
        "• `/del PREFIX START END` - Surgical range delete\n"
        "• `/setkey PREFIX PASSWORD` - Secure prefix partition\n"
        "• `/setlabel PREFIX CAT TITLE` - Assign title & category\n"
        "• `/dellabel PREFIX` - Remove title & category partition\n"
        "• `/tmdb PREFIX QUERY/ID` - Fetch & link TMDB metadata\n"
        "• `/deltmdb PREFIX` - Unlink TMDB metadata\n"
        "• `/setseason PREFIX SN START END` - Map season range\n"
        "• `/delseason PREFIX SN` - Delete specific season\n"
        "• `/clearseasons PREFIX` - Wipe all seasons\n"
        "• `/rename_prefix OLD NEW` - Bulk migrate prefix\n"
        "• `/refresh` - Sync files and TMDB metadata\n\n"
        "File Retrieval Engine:\n"
        "• `/get CODE` - Fetch specific asset\n"
        "• `/get PREFIX START END` - Sequential range delivery\n\n"
        "Monitoring:\n"
        "• `/stats` - Live system audit report\n"
        "• `/export` - Database JSON backup\n"
        "───────────────────────"
    )
    return text, InlineKeyboardMarkup(keyboard)



async def show_admin_media_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🗄️ **MEDIA & CATALOG MANAGEMENT**\n"
        "─────────────────────────────────\n"
        "**Available Commands:**\n"
        "• `/save <CODE>` — Index single media by reply\n"
        "• `/autobulk <START> <END> <PREFIX>` — Mass forward index\n"
        "• `/del <CODE>` — Single file delete\n"
        "• `/del <PREFIX> <START> <END>` — Surgical range delete\n"
        "• `/setlabel <PREFIX> <MOVIE|SERIES> <TITLE>` — Assign title & TMDB sync\n"
        "• `/setseason <PREFIX> <SN> <START> <END>` — Map season episode range\n"
        "• `/delseason <PREFIX> <SN>` — Delete specific season\n"
        "• `/clearseasons <PREFIX>` — Clear all mapped seasons\n"
        "• `/rename_prefix <OLD> <NEW>` — Bulk migrate prefix\n"
        "• `/refresh` — Sync files & fetch missing TMDB metadata\n"
        "─────────────────────────────────"
    )
    keyboard = [
        [InlineKeyboardButton("🔄 Sync TMDB & Files", callback_data="pal_sync"), InlineKeyboardButton("🗑️ Manage / Nuke DB", callback_data="pal_manage")],
        [InlineKeyboardButton("🔙 Back to Main Console", callback_data="pal_home")]
    ]
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def show_admin_security_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🔐 **ACCESS & SECURITY MANAGEMENT**\n"
        "─────────────────────────────────\n"
        "**Available Commands:**\n"
        "• `/setkey <PREFIX> <PASSWORD>` — Lock prefix behind a passkey partition\n"
        "• `/unsetkey <PREFIX>` — Remove passkey partition completely\n"
        "• `/grant <USER_ID> <PREFIX> [DURATION]` — Grant media access (e.g. `24h`, `7d`)\n"
        "• `/revoke <USER_ID> <PREFIX>` — Completely remove media access\n"
        "• `/freeze <USER_ID> <PREFIX>` — Temporarily suspend access\n"
        "• `/unfreeze <USER_ID> <PREFIX>` — Restore suspended access\n"
        "─────────────────────────────────"
    )
    keyboard = [
        [InlineKeyboardButton("👥 Access Matrix", callback_data="pal_access"), InlineKeyboardButton("🗝️ Key Matrix", callback_data="pal_keys")],
        [InlineKeyboardButton("🔙 Back to Main Console", callback_data="pal_home")]
    ]
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def show_admin_reminders_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "⏰ **REMINDERS & SCHEDULES**\n"
        "─────────────────────────────────\n"
        "**Features & Commands:**\n"
        "• `/remind` — Interactive calendar setup wizard with category tags\n"
        "• `/list` — View and manage active reminders\n"
        "• 🎂 **Birthday Engine:** Consolidates all birthdays into a single daily digest\n"
        "• 🔔 **Other Categories:** Dispatches segregated notifications by category\n"
        "• 📦 Automated weekly JSON database backups sent directly to admin\n"
        "─────────────────────────────────"
    )
    keyboard = [
        [InlineKeyboardButton("📜 Global Reminders Audit", callback_data="pal_alllists")],
        [InlineKeyboardButton("🔙 Back to Main Console", callback_data="pal_home")]
    ]
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def show_admin_system_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📈 **AUDITS & SYSTEM CONTROLS**\n"
        "─────────────────────────────────\n"
        "**Available Commands:**\n"
        "• `/stats` — Live system audit report\n"
        "• `/export` — Database JSON snapshot backup\n"
        "• Automated 7-day TTL auto-pruning active for activity logs\n"
        "─────────────────────────────────"
    )
    keyboard = [
        [InlineKeyboardButton("📊 System Stats", callback_data="pal_stats"), InlineKeyboardButton("📦 Export JSON Backup", callback_data="pal_export")],
        [InlineKeyboardButton("📋 Activity Logs", callback_data="pal_logs")],
        [InlineKeyboardButton("🔙 Back to Main Console", callback_data="pal_home")]
    ]
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def handle_palette_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_user.id != ADMIN_ID:
        return await query.answer("Access Denied.", show_alert=True)

    parts = query.data.split('_')
    action = parts[1]

    if action == "home":
        text, markup = await admin_palette_msg()
        await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
    elif action == "cat":
        sub = parts[2]
        if sub == "media":
            await show_admin_media_menu(update, context)
        elif sub == "security":
            await show_admin_security_menu(update, context)
        elif sub == "reminders":
            await show_admin_reminders_menu(update, context)
        elif sub == "system":
            await show_admin_system_menu(update, context)
    elif action == "stats":
        await get_stats(update, context)
    elif action == "access":
        await get_access_matrix(update, context) 
    elif action == "export":
        await export_data(update, context)
    elif action == "alllists":
        await get_all_lists(update, context)
    elif action == "keys":
        await get_key_matrix(update, context)
    elif action == "logs":
        await get_system_logs(update, context)
    elif action == "sync":
        await refresh_metadata(update, context)
    elif action == "manage": 
        context.user_data.clear()
        await manage_db_gui(update, context)

    await query.answer()


async def get_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or codes_col is None: return
    r_c = await reminders_col.count_documents({})
    c_c = await codes_col.count_documents({})
    l_c = await logs_col.count_documents({})
    k_c = await group_keys_col.count_documents({})
    u_c = await users_col.count_documents({}) if users_col is not None else 0
    
    msg = (
        "📊 **SYSTEM AUDIT REPORT**\n"
        "───────────────────\n"
        f"📅 **Total Reminders:** `{r_c:03d}`\n"
        f"🔑 **Indexed Assets:** `{c_c:03d}`\n"
        f"🔐 **Locked Prefixes:** `{k_c:03d}`\n"
        f"📋 **Stored Logs:** `{l_c:03d}`\n"
        f"👤 **Unique Users:** `{u_c:03d}`\n"
        "───────────────────\n"
        "*Auto-cleaning active: Logs purge every 7 days.*"
    )
    await update.effective_message.reply_text(msg, parse_mode="Markdown")


async def get_access_matrix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays which users have access to locked prefixes, with active/frozen/expiration states."""
    if update.effective_user.id != ADMIN_ID or unlocked_groups_col is None: return
    try:
        cursor = unlocked_groups_col.find({})
        users_access = await cursor.to_list(length=100)
        if not users_access:
            return await update.effective_message.reply_text("👥 No users currently have access to locked media.")
            
        report = ["👥 <b>MEDIA ACCESS MATRIX</b>\n"]
        now = time.time()
        for u in users_access:
            u_id = u.get("user_id")
            u_info = await users_col.find_one({"user_id": u_id}) if users_col is not None else None
            uname = f"@{u_info.get('username')}" if u_info and u_info.get('username') else f"User {u_id}"
            
            unlocked = u.get("unlocked_prefixes", [])
            frozen = u.get("frozen_prefixes", [])
            expirations = u.get("prefix_expirations", {})
            
            if not unlocked and not frozen:
                continue
                
            prefix_statuses = []
            all_prefixes = sorted(list(set(unlocked + frozen)))
            for p_key in all_prefixes:
                parts = p_key.split('_')
                display_prefix = parts[-1] if len(parts) > 1 else p_key
                
                if p_key in frozen:
                    status = "❄️ FROZEN"
                else:
                    exp_ts = expirations.get(p_key)
                    if exp_ts:
                        remaining = int(exp_ts - now)
                        if remaining > 0:
                            hrs = remaining // 3600
                            mins = (remaining % 3600) // 60
                            status = f"🟢 ACTIVE ({hrs}h {mins}m left)"
                        else:
                            status = "⏳ EXPIRED"
                    else:
                        status = "🟢 ACTIVE (Permanent)"
                prefix_statuses.append(f"{display_prefix}: {status}")
                
            report.append(f"• <b>{html.escape(uname)}</b> (<code>{u_id}</code>):\n  " + ", ".join(prefix_statuses))
            
        report.append("\n<b>Admin Commands:</b>\n"
                      "• <code>/grant &lt;USER_ID&gt; &lt;PREFIX&gt; [DURATION]</code>\n"
                      "• <code>/revoke &lt;USER_ID&gt; &lt;PREFIX&gt;</code>\n"
                      "• <code>/freeze &lt;USER_ID&gt; &lt;PREFIX&gt;</code>\n"
                      "• <code>/unfreeze &lt;USER_ID&gt; &lt;PREFIX&gt;</code>")
        
        await update.effective_message.reply_text("\n".join(report), parse_mode="HTML")
    except Exception as e:
        logger.error(f"Access Matrix error: {e}")
        await update.effective_message.reply_text("❌ Failed to retrieve access matrix.")


async def grant_access_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to grant permanent or time-limited access to a prefix for a specific user."""
    if update.effective_user.id != ADMIN_ID or unlocked_groups_col is None: return
    args = context.args or []
    if len(args) < 2:
        return await update.message.reply_text("❌ Usage: `/grant USER_ID PREFIX [DURATION]`\nExample durations: `24h`, `7d`, `30m`", parse_mode="Markdown")
    try:
        target_user_id = int(args[0])
        prefix = args[1].upper().strip()[:3]
        
        expires_at = None
        duration_note = "Permanent"
        if len(args) >= 3:
            expires_at = parse_duration(args[2])
            if expires_at:
                duration_note = f"Expires in {args[2]}"
            else:
                return await update.message.reply_text("❌ Invalid duration. Use format like `24h`, `7d`, or `30m`.")
        
        key_record = await group_keys_col.find_one({"prefix": prefix}) if group_keys_col is not None else None
        chat_id = key_record["chat_id"] if key_record else update.effective_chat.id
        auth_key = f"{chat_id}_{prefix}"
        
        update_op = {
            "$addToSet": {"unlocked_prefixes": auth_key},
            "$pull": {"frozen_prefixes": auth_key}
        }
        if expires_at:
            update_op["$set"] = {f"prefix_expirations.{auth_key}": expires_at}
        else:
            update_op["$unset"] = {f"prefix_expirations.{auth_key}": ""}
            
        await unlocked_groups_col.update_one(
            {"user_id": target_user_id},
            update_op,
            upsert=True
        )
        await update.message.reply_text(f"✅ Granted access to `{prefix}` for user `{target_user_id}` ({duration_note}).", parse_mode="Markdown")
        await log_event(ADMIN_ID, "ADMIN", f"Granted {prefix} to {target_user_id} ({duration_note})")
        await notify_admin(context, f"👥 Admin granted `{prefix}` access to `{target_user_id}` ({duration_note}).")
    except ValueError:
        await update.message.reply_text("❌ USER_ID must be a numeric integer.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def revoke_access_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to completely revoke access to a prefix for a specific user."""
    if update.effective_user.id != ADMIN_ID or unlocked_groups_col is None: return
    args = context.args or []
    if len(args) < 2:
        return await update.message.reply_text("❌ Usage: `/revoke USER_ID PREFIX`", parse_mode="Markdown")
    try:
        target_user_id = int(args[0])
        prefix = args[1].upper().strip()[:3]
        
        user_rec = await unlocked_groups_col.find_one({"user_id": target_user_id})
        if not user_rec:
            return await update.message.reply_text(f"❌ User `{target_user_id}` has no access records.")
            
        unlocked = user_rec.get("unlocked_prefixes", [])
        frozen = user_rec.get("frozen_prefixes", [])
        
        matched_keys = [k for k in (unlocked + frozen) if k.endswith(f"_{prefix}") or k == prefix]
        if not matched_keys:
            return await update.message.reply_text(f"❌ User `{target_user_id}` does not have access to `{prefix}`.")
            
        unset_fields = {f"prefix_expirations.{k}": "" for k in matched_keys}
        await unlocked_groups_col.update_one(
            {"user_id": target_user_id},
            {
                "$pull": {
                    "unlocked_prefixes": {"$in": matched_keys},
                    "frozen_prefixes": {"$in": matched_keys}
                },
                "$unset": unset_fields
            }
        )
        await update.message.reply_text(f"🗑️ Revoked access to `{prefix}` for user `{target_user_id}`.", parse_mode="Markdown")
        await log_event(ADMIN_ID, "ADMIN", f"Revoked {prefix} from {target_user_id}")
        await notify_admin(context, f"👥 Admin revoked `{prefix}` access from `{target_user_id}`.")
    except ValueError:
        await update.message.reply_text("❌ USER_ID must be a numeric integer.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def freeze_access_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to freeze/suspend access to a prefix for a specific user."""
    if update.effective_user.id != ADMIN_ID or unlocked_groups_col is None: return
    args = context.args or []
    if len(args) < 2:
        return await update.message.reply_text("❌ Usage: `/freeze USER_ID PREFIX`", parse_mode="Markdown")
    try:
        target_user_id = int(args[0])
        prefix = args[1].upper().strip()[:3]
        
        key_record = await group_keys_col.find_one({"prefix": prefix}) if group_keys_col is not None else None
        chat_id = key_record["chat_id"] if key_record else update.effective_chat.id
        auth_key = f"{chat_id}_{prefix}"
        
        await unlocked_groups_col.update_one(
            {"user_id": target_user_id},
            {"$addToSet": {"frozen_prefixes": auth_key}},
            upsert=True
        )
        await update.message.reply_text(f"❄️ Access to `{prefix}` is now **FROZEN** for user `{target_user_id}`.", parse_mode="Markdown")
        await log_event(ADMIN_ID, "ADMIN", f"Froze {prefix} for {target_user_id}")
        await notify_admin(context, f"❄️ Admin froze `{prefix}` access for `{target_user_id}`.")
    except ValueError:
        await update.message.reply_text("❌ USER_ID must be a numeric integer.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def unfreeze_access_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to unfreeze/restore access to a prefix for a specific user."""
    if update.effective_user.id != ADMIN_ID or unlocked_groups_col is None: return
    args = context.args or []
    if len(args) < 2:
        return await update.message.reply_text("❌ Usage: `/unfreeze USER_ID PREFIX`", parse_mode="Markdown")
    try:
        target_user_id = int(args[0])
        prefix = args[1].upper().strip()[:3]
        
        user_rec = await unlocked_groups_col.find_one({"user_id": target_user_id})
        frozen = user_rec.get("frozen_prefixes", []) if user_rec else []
        matched_keys = [k for k in frozen if k.endswith(f"_{prefix}") or k == prefix]
        
        if not matched_keys:
            key_record = await group_keys_col.find_one({"prefix": prefix}) if group_keys_col is not None else None
            chat_id = key_record["chat_id"] if key_record else update.effective_chat.id
            matched_keys = [f"{chat_id}_{prefix}"]
            
        await unlocked_groups_col.update_one(
            {"user_id": target_user_id},
            {"$pull": {"frozen_prefixes": {"$in": matched_keys}}}
        )
        await update.message.reply_text(f"✅ Access to `{prefix}` is now **UNFROZEN** for user `{target_user_id}`.", parse_mode="Markdown")
        await log_event(ADMIN_ID, "ADMIN", f"Unfroze {prefix} for {target_user_id}")
        await notify_admin(context, f"✅ Admin unfroze `{prefix}` access for `{target_user_id}`.")
    except ValueError:
        await update.message.reply_text("❌ USER_ID must be a numeric integer.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def get_all_lists(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if reminders_col is None: return
    try:
        cursor = reminders_col.find({}).sort("user_id", 1)
        reminders = await cursor.to_list(length=50)
        if not reminders: return await update.effective_message.reply_text("📜 No active reminders.")
        report = ["📜 <b>GLOBAL REMINDER AUDIT</b>\n"]
        for r in reminders:
            label = html.escape(str(r.get('label', 'No Label')))
            rec = r.get('recurrence', 'once')
            report.append(f"👤 <code>{r['user_id']}</code>: {label} ({r['target_date']}) [{rec}]")
        await update.effective_message.reply_text("\n".join(report), parse_mode="HTML")
    except Exception as e:
        logger.error(f"Audit error: {e}")
        await update.effective_message.reply_text("❌ Audit failed.")


async def get_key_matrix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if codes_col is None: return
    try:
        pipeline = [{"$project": {"prefix": {"$substr": ["$code", 0, 3]}, "chat_id": 1}}, {"$group": {"_id": {"prefix": "$prefix", "chat_id": "$chat_id"}, "count": {"$sum": 1}}}]
        cursor = codes_col.aggregate(pipeline)
        results = await cursor.to_list(length=100)
        if not results: return await update.effective_message.reply_text("🗝️ No indexed groups.")
        report = ["🗝️ <b>LIVE SECRET KEY MATRIX</b>\n"]
        for r in results:
            prefix, chat_id, count = r['_id']['prefix'], r['_id']['chat_id'], r['count']
            key_record = await group_keys_col.find_one({"chat_id": chat_id, "prefix": prefix})
            passkey = html.escape(key_record["secret_key"] if key_record else "NO KEY SET")
            label_doc = await prefix_labels_col.find_one({"prefix": prefix}) if prefix_labels_col is not None else None
            label_name = html.escape(label_doc.get("name", "Unlabeled")) if label_doc else "Unlabeled"
            report.append(f"• <code>{prefix}</code> - {count:02d} items  =&gt;  <b>{label_name}</b>  &lt;=  <code>{passkey}</code>")
        await update.effective_message.reply_text("\n".join(report), parse_mode="HTML")
    except Exception as e:
        logger.error(f"Key Matrix error: {e}")
        await update.effective_message.reply_text("❌ Key Matrix failed.")


async def get_system_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if logs_col is None: return
    try:
        cursor = logs_col.find({}).sort("timestamp", -1).limit(20)
        logs = await cursor.to_list(length=20)
        if not logs: 
            return await update.effective_message.reply_text("📋 Logs empty.")
        
        report = ["📋 <b>SYSTEM ACTIVITY LOGS</b>\n"]
        for l in logs:
            time_str = l['timestamp'].strftime('%H:%M:%S')
            user = html.escape(str(l.get('username', 'Unknown')))
            action = html.escape(str(l.get('action', 'Unknown')))
            report.append(f"<code>[{time_str}]</code> {user}: {action}")
        
        await update.effective_message.reply_text("\n".join(report), parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error retrieving logs: {e}")
        await update.effective_message.reply_text("❌ Failed to retrieve logs.")


async def export_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or reminders_col is None: return
    cursor = reminders_col.find({})
    data = await cursor.to_list(length=None)
    for r in data: r['_id'] = str(r['_id'])
    file_path = f"backup_{datetime.now().strftime('%Y%m%d')}.json"
    try:
        with open(file_path, "w") as f: json.dump(data, f, indent=4)
        with open(file_path, "rb") as f:
            await update.effective_message.reply_document(document=f, filename=file_path)
    finally:
        if os.path.exists(file_path): os.remove(file_path)


async def manage_db_gui(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if codes_col is None: return
    pipeline = [{"$project": {"prefix": {"$substr": ["$code", 0, 3]}}}, {"$group": {"_id": "$prefix", "count": {"$sum": 1}}}]
    cursor = codes_col.aggregate(pipeline)
    prefixes = await cursor.to_list(length=100)
    if not prefixes: 
        await update.effective_message.reply_text("Database empty.")
        return
    keyboard = [[InlineKeyboardButton(f"🔥 Wipe {p['_id']} ({p['count']} items)", callback_data=f"pref_wipe_{p['_id']}")] for p in prefixes]
    keyboard.append([InlineKeyboardButton("🔙 Back to Console", callback_data="pal_home")])
    await update.effective_message.reply_text("🗑️ **NUKE SELECTOR**\nChoose prefix to vaporize:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def handle_manage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_user.id != ADMIN_ID: return await query.answer("Unauthorized")
    
    await query.answer()
    prefix = query.data.split('_')[2]
    
    try:
        escaped_prefix = re.escape(prefix)
        count_to_wipe = await codes_col.count_documents({"code": {"$regex": f"^{escaped_prefix}"}})
        res = await codes_col.delete_many({"code": {"$regex": f"^{escaped_prefix}"}})
        await prefix_labels_col.delete_one({"prefix": prefix})
        
        msg = f"🔥 **NUKE COMPLETE**\nPrefix `{prefix}` vaporized.\nAssets Deleted: `{res.deleted_count}/{count_to_wipe}`"
        await query.edit_message_text(msg, parse_mode="Markdown")
        await log_event(ADMIN_ID, "ADMIN", f"Nuke Success: {prefix}")
        await notify_admin(context, f"☢️ Nuke triggered for `{prefix}`. `{res.deleted_count}` items erased.")
    except Exception as e:
        await query.edit_message_text(f"❌ **NUKE FAILED**: `{e}`", parse_mode="Markdown")
        await notify_admin(context, f"🔴 NUKE FAILURE for `{prefix}`: {e}")


async def range_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or codes_col is None: return
    args = context.args
    if not args: 
        return await update.message.reply_text("❌ Usage:\nSingle: `/del CODE`\nRange: `/del PREFIX START END`", parse_mode="Markdown")
        
    try:
        if len(args) == 1:
            code = args[0].upper().strip()
            res = await codes_col.delete_one({"code": code})
            if res.deleted_count > 0:
                await update.message.reply_text(f"🗑️ vaporized `{code}`.", parse_mode="Markdown")
                await log_event(ADMIN_ID, "ADMIN", f"Single Del: {code}")
                await notify_admin(context, f"🗑️ Admin deleted `{code}`.")
            else:
                await update.message.reply_text(f"❌ `{code}` not found.")
                
        elif len(args) == 3:
            prefix, start, end = args[0].upper().strip()[:3], int(args[1]), int(args[2])
            if start < 1 or end < start or (end - start + 1) > 100:
                return await update.message.reply_text("❌ Range error: Maximum 100 items can be deleted at once.")
            target_codes = [f"{prefix}{i:03d}" for i in range(start, end + 1)]
            result = await codes_col.delete_many({"code": {"$in": target_codes}})
            await update.message.reply_text(f"🗑️ vaporized `{result.deleted_count}` items.", parse_mode="Markdown")
            await log_event(ADMIN_ID, "ADMIN", f"Range Del: {prefix}{start:03d}-{end:03d}")
            await notify_admin(context, f"🗑️ Range Delete for `{prefix}` ({result.deleted_count} items).")
        else:
            await update.message.reply_text("❌ Usage:\nSingle: `/del CODE`\nRange: `/del PREFIX START END`", parse_mode="Markdown")
    except Exception as e: 
        await update.message.reply_text(f"❌ Error: {e}")
        await notify_admin(context, f"🔴 DEL FAILURE: {e}")


async def rename_prefix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or codes_col is None: return
    args = context.args
    if len(args) < 2:
        return await update.message.reply_text("❌ Usage: `/rename_prefix OLD NEW`", parse_mode="Markdown")
    
    old_prefix = args[0].upper().strip()[:3]
    new_prefix = args[1].upper().strip()[:3]
    
    if old_prefix == new_prefix:
        return await update.message.reply_text("❌ Prefixes are the same.")

    status = await update.message.reply_text(f"🔄 **Migrating `{old_prefix}` to `{new_prefix}`...**", parse_mode="Markdown")
    
    try:
        escaped_old = re.escape(old_prefix)
        cursor = codes_col.find({"code": {"$regex": f"^{escaped_old}"}})
        count = 0
        async for record in cursor:
            old_code = record["code"]
            suffix = old_code[3:]
            new_code = f"{new_prefix}{suffix}"
            await codes_col.update_one({"_id": record["_id"]}, {"$set": {"code": new_code}})
            count += 1
            
        await group_keys_col.update_many({"prefix": old_prefix}, {"$set": {"prefix": new_prefix}})
        
        await status.edit_text(f"✅ **Migration Complete!**\nMoved `{count}` items from `{old_prefix}` to `{new_prefix}`.", parse_mode="Markdown")
        await log_event(ADMIN_ID, "ADMIN", f"Rename Prefix: {old_prefix} -> {new_prefix} ({count} items)")
        await notify_admin(context, f"🔄 Migrated `{old_prefix}` to `{new_prefix}` ({count} items).")
    except Exception as e:
        logger.error(f"Rename error: {e}")
        await status.edit_text(f"❌ **Migration Failed:** {e}")
        await notify_admin(context, f"🔴 RENAME FAILURE: {e}")


async def refresh_metadata(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Syncs missing Telegram file metadata and enriches all existing files & prefix labels with full TMDB metadata."""
    if update.effective_user.id != ADMIN_ID or codes_col is None: return
    status_msg = await update.effective_message.reply_text("🔄 **Full System Sync Initiated**\nChecking file metadata & TMDB...", parse_mode="Markdown")
    
    # 1. Sync file-level metadata from Telegram
    query_filter = {"$or": [{"file_id": {"$exists": False}}, {"file_id": None}, {"file_id": ""}, {"file_name": {"$exists": False}}]}
    total_to_sync = await codes_col.count_documents(query_filter)
    file_sync_count = 0
    
    if total_to_sync > 0:
        cursor = codes_col.find(query_filter)
        async for record in cursor:
            try:
                probe = await context.bot.forward_message(chat_id=ADMIN_ID, from_chat_id=record["chat_id"], message_id=record["message_id"])
                f_type, f_id, caption, f_name = extract_file_data(probe)
                await probe.delete()
                if f_id:
                    await codes_col.update_one({"_id": record["_id"]}, {"$set": {"file_type": f_type, "file_id": f_id, "caption": caption or "", "file_name": f_name or ""}})
                    file_sync_count += 1
                if (file_sync_count % 5 == 0) or file_sync_count == total_to_sync:
                    await status_msg.edit_text(f"🔄 **Syncing Files...**\n`{file_sync_count}/{total_to_sync}` assets updated.", parse_mode="Markdown")
                await asyncio.sleep(0.5) 
            except Exception as e:
                logger.warning(f"File probe failed for {record.get('code')}: {e}")
                continue

    # 2. Refresh TMDB metadata ONLY for titles that have explicitly linked TMDB records
    tmdb_sync_count = 0
    total_labels = 0
    if prefix_labels_col is not None:
        query_with_tmdb = {"tmdb": {"$exists": True, "$ne": None}}
        total_labels = await prefix_labels_col.count_documents(query_with_tmdb)
        if total_labels > 0:
            label_cursor = prefix_labels_col.find(query_with_tmdb)
            processed = 0
            async for label_doc in label_cursor:
                processed += 1
                name = label_doc.get("name")
                cat = label_doc.get("category", "movie")
                existing_tmdb = label_doc.get("tmdb") or {}
                query_target = str(existing_tmdb.get("tmdb_id")) if existing_tmdb.get("tmdb_id") else name
                
                if query_target:
                    tmdb_info = await fetch_tmdb_metadata(query_target, cat)
                    if tmdb_info:
                        await prefix_labels_col.update_one({"_id": label_doc["_id"]}, {"$set": {"tmdb": tmdb_info}})
                        tmdb_sync_count += 1

                if (processed % 3 == 0) or processed == total_labels:
                    await status_msg.edit_text(
                        f"🔄 **Refreshing Linked TMDB Metadata...**\n"
                        f"`{processed}/{total_labels}` titles checked.\n"
                        f"Updated: `{tmdb_sync_count}`",
                        parse_mode="Markdown"
                    )
                await asyncio.sleep(0.25)

    summary_lines = [
        "✅ **Sync Finalized!**",
        f"• Files probed/updated: `{file_sync_count}`",
        f"• Handpicked TMDB records refreshed: `{tmdb_sync_count}`",
    ]

    await status_msg.edit_text("\n".join(summary_lines), parse_mode="Markdown")
    await log_event(ADMIN_ID, "ADMIN", f"Metadata Sync: {file_sync_count} files, {tmdb_sync_count} TMDB records")
    await notify_admin(context, f"📊 Metadata Sync complete. `{file_sync_count}` files and `{tmdb_sync_count}` TMDB records enriched.")


async def auto_bulk_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or codes_col is None: return
    if len(context.args) < 3: return
    try:
        start_id, end_id, prefix = int(context.args[0]), int(context.args[1]), context.args[2].upper().strip()[:3]
        escaped_prefix = re.escape(prefix)
        exist = await codes_col.count_documents({"code": {"$regex": f"^{escaped_prefix}"}})
        curr = exist + 1
        status_msg = await update.message.reply_text(f"🔄 Probing `{prefix}` range from {start_id} to {end_id}...")
        indexed_count = 0
        for m_id in range(start_id, end_id + 1):
            try:
                probe = await context.bot.forward_message(chat_id=ADMIN_ID, from_chat_id=update.effective_chat.id, message_id=m_id)
                f_type, f_id, caption, f_name = extract_file_data(probe)
                await probe.delete()
                if f_id:
                    code_to_save = f"{prefix}{curr:03d}"
                    await codes_col.update_one({"code": code_to_save}, {"$set": {"chat_id": update.effective_chat.id, "message_id": m_id, "file_type": f_type, "file_id": f_id, "caption": caption or "", "file_name": f_name or ""}}, upsert=True)
                    indexed_count += 1
                    curr += 1
            except: continue
        await status_msg.edit_text(f"✅ **Bulk Indexing Complete!** Indexed `{indexed_count}` items for `{prefix}`.")
        await log_event(ADMIN_ID, "ADMIN", f"Autobulk: {prefix} ({indexed_count} items)")
        await notify_admin(context, f"📦 Autobulk Success for `{prefix}` ({indexed_count} items).")
    except Exception as e: 
        await update.message.reply_text(f"❌ Error during autobulk: {e}")
        await notify_admin(context, f"🔴 AUTOBULK FAILURE: {e}")


async def save_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not update.message.reply_to_message or not context.args or codes_col is None: return
    code = context.args[0].upper().strip()
    f_type, f_id, caption, f_name = extract_file_data(update.message.reply_to_message)
    if not f_id: return await update.message.reply_text("❌ No recognizable file found.")
    data = {"chat_id": update.effective_chat.id, "message_id": update.message.reply_to_message.message_id, "file_type": f_type, "file_id": f_id, "caption": caption or "", "file_name": f_name or ""}
    await codes_col.update_one({"code": code}, {"$set": data}, upsert=True)
    await update.message.reply_text(f"✅ Indexed `{code}` (Type: {f_type})")
    await notify_admin(context, f"✅ Admin indexed `{code}`.")


async def set_group_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or group_keys_col is None: return
    if len(context.args) < 2: return await update.message.reply_text("❌ Usage: `/setkey PREFIX PASSWORD`")
    prefix = context.args[0].upper().strip()[:3]
    password = context.args[1].strip()
    await group_keys_col.update_one({"chat_id": update.effective_chat.id, "prefix": prefix}, {"$set": {"secret_key": password}}, upsert=True)
    await update.message.reply_text(f"🔒 Key set for prefix `{prefix}`!")
    await notify_admin(context, f"🔒 Access key set for `{prefix}`.")


async def unset_group_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to remove password partition from a movie or series prefix."""
    if update.effective_user.id != ADMIN_ID or group_keys_col is None: return
    if not context.args:
        return await update.message.reply_text("❌ Usage: `/unsetkey PREFIX`\nExample: `/unsetkey MOV`", parse_mode="Markdown")

    prefix = context.args[0].upper().strip()[:3]
    res = await group_keys_col.delete_many({"prefix": prefix})
    if res.deleted_count > 0:
        await update.message.reply_text(f"🔓 **Partition Removed!** Prefix `{prefix}` is now completely unlocked for all users.", parse_mode="Markdown")
        await log_event(ADMIN_ID, "ADMIN", f"Unset Key: {prefix}")
        await notify_admin(context, f"🔓 Admin removed password partition from `{prefix}`.")
    else:
        await update.message.reply_text(f"ℹ️ Prefix `{prefix}` was not password-locked.")


async def set_prefix_label(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or prefix_labels_col is None: return
    if len(context.args) < 3: return await update.message.reply_text("❌ Usage: `/setlabel PREFIX MOVIE/SERIES NAME`")
    
    prefix = context.args[0].upper().strip()[:3]
    category = context.args[1].upper().strip()
    name = " ".join(context.args[2:]).strip()
    
    if category not in ["MOVIE", "SERIES"]:
        return await update.message.reply_text("❌ Category must be **MOVIE** or **SERIES**.")
        
    set_doc = {"name": name, "category": category.lower()}
    await prefix_labels_col.update_one({"prefix": prefix}, {"$set": set_doc}, upsert=True)
    await update.message.reply_text(
        f"🏷️ **Handpicked Label Set!**\n"
        f"Partition `{prefix}` [{category}] ➔ **{name}**\n\n"
        f"💡 *To link specific TMDB metadata & poster:* `/tmdb {prefix} <title or ID>`",
        parse_mode="Markdown"
    )
    await log_event(ADMIN_ID, "ADMIN", f"Set Label: {prefix} -> {name} [{category}]")
    await notify_admin(context, f"🏷️ Admin assigned label `{prefix}` ➔ {name} [{category}].")


async def delete_prefix_label(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to remove title, category, and TMDB metadata from a prefix partition."""
    if update.effective_user.id != ADMIN_ID or prefix_labels_col is None:
        return
    if not context.args:
        return await update.message.reply_text("❌ Usage: `/dellabel PREFIX`\nExample: `/dellabel MOV`", parse_mode="Markdown")

    prefix = context.args[0].upper().strip()[:3]
    rec = await prefix_labels_col.find_one({"prefix": prefix})
    if not rec:
        return await update.message.reply_text(f"ℹ️ Prefix `{prefix}` has no assigned title or category.", parse_mode="Markdown")

    old_title = rec.get("name", prefix)
    old_cat = rec.get("category", "unknown").upper()
    await prefix_labels_col.delete_one({"prefix": prefix})

    asset_count = await codes_col.count_documents({"code": {"$regex": f"^{re.escape(prefix)}"}}) if codes_col is not None else 0
    await update.message.reply_text(
        f"🗑️ **Label Removed!**\n"
        f"Partition `{prefix}` (`{old_title}` [{old_cat}]) unlinked.\n"
        f"📦 `{asset_count}` asset(s) in database remain preserved.",
        parse_mode="Markdown"
    )
    await log_event(ADMIN_ID, "ADMIN", f"Deleted label: {prefix}")
    await notify_admin(context, f"🏷️ Admin removed title/category label from `{prefix}` ({asset_count} assets kept).")


async def set_tmdb_metadata(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to specifically search and link TMDB metadata to a prefix partition."""
    if update.effective_user.id != ADMIN_ID or prefix_labels_col is None: return
    if len(context.args) < 2:
        return await update.message.reply_text(
            "❌ **Usage:** `/tmdb PREFIX <TITLE or TMDB_ID>`\n\n"
            "**Examples:**\n"
            "• `/tmdb SLV A Silent Voice (2016)`\n"
            "• `/tmdb SLV 378064` (Direct TMDB ID)\n"
            "• `/tmdb SLV tmdb:378064`",
            parse_mode="Markdown"
        )
    
    prefix = context.args[0].upper().strip()[:3]
    query = " ".join(context.args[1:]).strip()
    
    label_rec = await prefix_labels_col.find_one({"prefix": prefix})
    if not label_rec:
        return await update.message.reply_text(
            f"❌ Prefix `{prefix}` is not labeled yet!\n"
            f"Please run `/setlabel {prefix} <MOVIE|SERIES> <TITLE>` first to define its category.",
            parse_mode="Markdown"
        )
        
    category = label_rec.get("category", "movie")
    status_msg = await update.message.reply_text(f"🔍 Searching TMDB for `{query}` [{category.upper()}]...", parse_mode="Markdown")
    
    tmdb_info = await fetch_tmdb_metadata(query, category)
    if not tmdb_info:
        return await status_msg.edit_text(
            f"❌ No TMDB matches found for `{query}` under category `{category}`.\n"
            f"💡 *Tip:* Try providing the release year (e.g. `2016`) or direct TMDB ID.",
            parse_mode="Markdown"
        )
        
    await prefix_labels_col.update_one({"prefix": prefix}, {"$set": {"tmdb": tmdb_info}})
    
    year_str = f" ({tmdb_info['year']})" if tmdb_info.get("year") else ""
    genres_str = f"\n🎭 **Genres:** {tmdb_info['genres']}" if tmdb_info.get("genres") else ""
    rating_str = f"\n⭐ **Rating:** `{tmdb_info['rating']}/10`" if tmdb_info.get("rating") else ""
    poster_note = "\n🖼️ **Official Poster:** Attached" if tmdb_info.get("poster_url") else ""
    overview_snippet = f"\n\n📖 **Overview:**\n_{tmdb_info['overview'][:200]}..._" if tmdb_info.get("overview") else ""
    
    await status_msg.edit_text(
        f"✅ **TMDB Synced for `{prefix}`!**\n\n"
        f"🎬 **Matched:** **{tmdb_info['title']}**{year_str}"
        f"{rating_str}"
        f"{genres_str}"
        f"{poster_note}"
        f"{overview_snippet}\n\n"
        f"*(Official poster and metadata are now linked to `{prefix}`)*",
        parse_mode="Markdown"
    )
    await log_event(ADMIN_ID, "ADMIN", f"TMDB set for {prefix}: {tmdb_info['title']}")
    await notify_admin(context, f"🎬 TMDB metadata linked to `{prefix}`: {tmdb_info['title']}.")


async def delete_tmdb_metadata(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to remove TMDB metadata from a prefix partition."""
    if update.effective_user.id != ADMIN_ID or prefix_labels_col is None: return
    if not context.args:
        return await update.message.reply_text("❌ Usage: `/deltmdb PREFIX`", parse_mode="Markdown")
        
    prefix = context.args[0].upper().strip()[:3]
    res = await prefix_labels_col.update_one({"prefix": prefix}, {"$unset": {"tmdb": ""}})
    if res.modified_count > 0:
        await update.message.reply_text(f"🗑️ Removed TMDB metadata from `{prefix}`.", parse_mode="Markdown")
        await log_event(ADMIN_ID, "ADMIN", f"Deleted TMDB for {prefix}")
        await notify_admin(context, f"🎬 Admin cleared TMDB metadata from `{prefix}`.")
    else:
        await update.message.reply_text(f"ℹ️ Prefix `{prefix}` had no TMDB metadata.", parse_mode="Markdown")


async def set_season_mapping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or prefix_labels_col is None: return
    if len(context.args) < 4: return await update.message.reply_text("❌ Usage: `/setseason PREFIX SEASON_NUM START END`")
    
    try:
        prefix = context.args[0].upper().strip()[:3]
        sn_num = int(context.args[1])
        start = int(context.args[2])
        end = int(context.args[3])
        
        season_obj = {"num": sn_num, "start": start, "end": end}
        rec = await prefix_labels_col.find_one({"prefix": prefix})
        if not rec: return await update.message.reply_text(f"❌ Prefix `{prefix}` not labeled.")
            
        seasons = rec.get("seasons", [])
        seasons = [s for s in seasons if s["num"] != sn_num]
        seasons.append(season_obj)
        seasons.sort(key=lambda x: x["num"])
        
        await prefix_labels_col.update_one({"prefix": prefix}, {"$set": seasons})
        await update.message.reply_text(f"✅ Season {sn_num} mapped for `{prefix}`.", parse_mode="Markdown")
        
    except ValueError:
        await update.message.reply_text("❌ Numbers required for SN/START/END.")


async def delete_season_mapping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or prefix_labels_col is None: return
    if len(context.args) < 2: return await update.message.reply_text("❌ Usage: `/delseason PREFIX SEASON_NUM`")
    try:
        prefix = context.args[0].upper().strip()[:3]
        sn_num = int(context.args[1])
        rec = await prefix_labels_col.find_one({"prefix": prefix})
        if not rec or "seasons" not in rec: return await update.message.reply_text(f"❌ No seasons found.")
        seasons = [s for s in rec["seasons"] if s["num"] != sn_num]
        await prefix_labels_col.update_one({"prefix": prefix}, {"$set": {"seasons": seasons}})
        await update.message.reply_text(f"🗑️ Season {sn_num} deleted for `{prefix}`.")
    except: pass


async def clear_all_seasons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or prefix_labels_col is None: return
    if not context.args: return
    prefix = context.args[0].upper().strip()[:3]
    await prefix_labels_col.update_one({"prefix": prefix}, {"$unset": {"seasons": ""}})
    await update.message.reply_text(f"🔥 Seasons wiped for `{prefix}`.")


async def weekly_backup_job(context: ContextTypes.DEFAULT_TYPE):
    """Automated recurring job that exports database backup and sends directly to Admin on Telegram."""
    if not ADMIN_ID: return
    try:
        backup_data = {
            "created_at": datetime.now().isoformat(),
            "reminders": await reminders_col.find({}).to_list(length=None) if reminders_col is not None else [],
            "codes": await codes_col.find({}).to_list(length=None) if codes_col is not None else [],
            "prefix_labels": await prefix_labels_col.find({}).to_list(length=None) if prefix_labels_col is not None else [],
            "group_keys": await group_keys_col.find({}).to_list(length=None) if group_keys_col is not None else [],
            "unlocked_groups": await unlocked_groups_col.find({}).to_list(length=None) if unlocked_groups_col is not None else [],
        }
        for col_items in backup_data.values():
            if isinstance(col_items, list):
                for item in col_items:
                    if isinstance(item, dict) and "_id" in item:
                        item["_id"] = str(item["_id"])
                        
        file_path = f"auto_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(file_path, "w") as f:
            json.dump(backup_data, f, indent=2)
            
        with open(file_path, "rb") as f:
            await context.bot.send_document(
                chat_id=ADMIN_ID,
                document=f,
                filename=file_path,
                caption=f"📦 **Automated Weekly Backup**\n📅 `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`\nAssets: `{len(backup_data['codes'])}` | Reminders: `{len(backup_data['reminders'])}`",
                parse_mode="Markdown"
            )
        if os.path.exists(file_path):
            os.remove(file_path)
        logger.info("Weekly automated database backup delivered to admin.")
    except Exception as e:
        logger.error(f"Weekly automated backup failed: {e}")
