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
        "👑 <b>MASTER CONSOLE (Ultimate Master)</b>\n"
        "───────────────────────\n"
        "System Status: 🌐 Operational\n"
        "───────────────────────\n"
        "📜 <b>COMPLETE COMMAND LIST:</b>\n\n"
        "<blockquote expandable>\n"
        "<b>Core Commands:</b>\n"
        "• <code>/start</code> / <code>/admin</code> - Dashboard\n"
        "• <code>/remind</code> - Setup countdown (GUI)\n"
        "• <code>/list</code> - View reminders\n"
        "• <code>/ascii</code> - Text to ASCII art\n"
        "</blockquote>\n\n"
        "<blockquote expandable>\n"
        "<b>Access Management:</b>\n"
        "• <code>/user grant &lt;USER_ID|@username&gt; &lt;PREFIX|0&gt; [DURATION]</code>\n"
        "• <code>/user revoke &lt;USER_ID|@username&gt; &lt;PREFIX|0&gt;</code>\n"
        "• <code>/user freeze &lt;USER_ID|@username&gt; [PREFIX|0] [DURATION]</code>\n"
        "• <code>/user unfreeze &lt;USER_ID|@username&gt; [PREFIX|0]</code>\n"
        "• <code>/user ban &lt;USER_ID|@username&gt;</code>\n"
        "• <code>/user unban &lt;USER_ID|@username&gt;</code>\n"
        "</blockquote>\n\n"
        "<blockquote expandable>\n"
        "<b>File Storage Engine:</b>\n"
        "• <code>/file save PREFIX</code> - Index replied file (auto 001)\n"
        "• <code>/file save PREFIX START END</code> - Forward mass index\n"
        "• <code>/file del CODE</code> - Delete single asset\n"
        "• <code>/file del PREFIX START END</code> - Surgical range delete\n"
        "• <code>/file lock PREFIX PASSWORD</code> - Secure partition with passkey\n"
        "• <code>/file unlock PREFIX</code> - Remove passkey partition\n"
        "• <code>/file mv OLD NEW</code> - Bulk migrate/rename prefix\n"
        "</blockquote>\n\n"
        "<blockquote expandable>\n"
        "<b>Media & Catalog:</b>\n"
        "• <code>/media label set PREFIX CAT TITLE</code> - Set title & category\n"
        "• <code>/media label del PREFIX</code> - Remove partition label\n"
        "• <code>/media tmdb set PREFIX QUERY/ID</code> - Link TMDB metadata\n"
        "• <code>/media tmdb del PREFIX</code> - Unlink TMDB metadata\n"
        "• <code>/media banner PREFIX toggle</code> - Flip poster / backdrop\n"
        "• <code>/media season set PREFIX SN START END</code> - Map season range\n"
        "• <code>/media season del PREFIX SN</code> - Delete specific season\n"
        "• <code>/media season clear PREFIX</code> - Wipe all seasons\n"
        "• <code>/media refresh</code> - Sync files & refresh TMDB\n"
        "</blockquote>\n\n"
        "<blockquote expandable>\n"
        "<b>File Retrieval Engine:</b>\n"
        "• <code>/get CODE</code> - Fetch specific asset\n"
        "• <code>/get PREFIX START END</code> - Sequential range delivery\n"
        "</blockquote>\n\n"
        "<blockquote expandable>\n"
        "<b>Monitoring & Backup:</b>\n"
        "• <code>/stats</code> - Live system audit report\n"
        "• <code>/export</code> - Database JSON backup\n"
        "</blockquote>\n"
        "───────────────────────"
    )
    return text, InlineKeyboardMarkup(keyboard)



async def show_admin_media_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🗄️ <b>MEDIA & CATALOG MANAGEMENT</b>\n"
        "─────────────────────────────────\n"
        "<b>Available Commands:</b>\n"
        "• <code>/media label set PREFIX CAT TITLE</code> — Handpick title & category\n"
        "• <code>/media label del PREFIX</code> — Remove partition label\n"
        "• <code>/media tmdb set PREFIX QUERY/ID</code> — Link TMDB metadata\n"
        "• <code>/media tmdb del PREFIX</code> — Unlink TMDB metadata\n"
        "• <code>/media banner PREFIX toggle</code> — Flip poster / backdrop\n"
        "• <code>/media season set PREFIX SN START END</code> — Map season episode range\n"
        "• <code>/media season del PREFIX SN</code> — Delete specific season\n"
        "• <code>/media season clear PREFIX</code> — Clear all mapped seasons\n"
        "• <code>/media refresh</code> — Sync files & refresh TMDB\n"
        "─────────────────────────────────"
    )
    keyboard = [
        [InlineKeyboardButton("🔄 Sync TMDB & Files", callback_data="pal_sync"), InlineKeyboardButton("🗑️ Manage / Nuke DB", callback_data="pal_manage")],
        [InlineKeyboardButton("🔙 Back to Main Console", callback_data="pal_home")]
    ]
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def show_admin_security_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🔐 <b>ACCESS & SECURITY MANAGEMENT</b>\n"
        "─────────────────────────────────\n"
        "<b>Available Commands:</b>\n"
        "• <code>/file lock PREFIX PASSWORD</code> — Lock prefix with passkey partition\n"
        "• <code>/file unlock PREFIX</code> — Remove passkey partition\n"
        "• <code>/user grant &lt;USER_ID|@username&gt; &lt;PREFIX|0&gt; [DURATION]</code>\n"
        "• <code>/user revoke &lt;USER_ID|@username&gt; &lt;PREFIX|0&gt;</code>\n"
        "• <code>/user freeze &lt;USER_ID|@username&gt; [PREFIX|0] [DURATION]</code>\n"
        "• <code>/user unfreeze &lt;USER_ID|@username&gt; [PREFIX|0]</code>\n"
        "• <code>/user ban &lt;USER_ID|@username&gt;</code>\n"
        "• <code>/user unban &lt;USER_ID|@username&gt;</code>\n"
        "─────────────────────────────────"
    )
    keyboard = [
        [InlineKeyboardButton("👥 Access Matrix", callback_data="pal_access"), InlineKeyboardButton("🗝️ Key Matrix", callback_data="pal_keys")],
        [InlineKeyboardButton("🔙 Back to Main Console", callback_data="pal_home")]
    ]
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


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
        await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
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
                      "• <code>/user grant &lt;USER_ID|@username&gt; &lt;PREFIX|0&gt; [DURATION]</code>\n"
                      "• <code>/user revoke &lt;USER_ID|@username&gt; &lt;PREFIX|0&gt;</code>\n"
                      "• <code>/user freeze &lt;USER_ID|@username&gt; [PREFIX|0] [DURATION]</code>\n"
                      "• <code>/user unfreeze &lt;USER_ID|@username&gt; [PREFIX|0]</code>\n"
                      "• <code>/user ban &lt;USER_ID|@username&gt;</code>")
        
        await update.effective_message.reply_text("\n".join(report), parse_mode="HTML")
    except Exception as e:
        logger.error(f"Access Matrix error: {e}")
        await update.effective_message.reply_text("❌ Failed to retrieve access matrix.")


async def resolve_target_user(identifier: str) -> tuple[int | None, str]:
    """Resolves a numeric user ID or @username into (user_id, display_name)."""
    identifier = identifier.strip()
    if identifier.startswith("@"):
        uname = identifier[1:]
        if users_col is not None:
            doc = await users_col.find_one({"username": {"$regex": f"^{re.escape(uname)}$", "$options": "i"}})
            if doc:
                return doc.get("user_id"), f"@{doc.get('username')}"
        return None, identifier
    if identifier.isdigit():
        uid = int(identifier)
        doc = await users_col.find_one({"user_id": uid}) if users_col is not None else None
        uname = f"@{doc['username']}" if (doc and doc.get("username")) else f"User {uid}"
        return uid, uname
    return None, identifier


async def user_command_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unified user access and moderation router: /user grant|revoke|freeze|unfreeze|ban|unban"""
    if update.effective_user.id != ADMIN_ID or unlocked_groups_col is None:
        return
    args = context.args or []
    if not args:
        help_text = (
            "👤 <b>USER ACCESS MANAGEMENT</b>\n\n"
            "• <code>/user grant &lt;USER_ID|@username&gt; &lt;PREFIX|0&gt; [DURATION]</code>\n"
            "• <code>/user revoke &lt;USER_ID|@username&gt; &lt;PREFIX|0&gt;</code>\n"
            "• <code>/user freeze &lt;USER_ID|@username&gt; [PREFIX|0] [DURATION]</code>\n"
            "• <code>/user unfreeze &lt;USER_ID|@username&gt; [PREFIX|0]</code>\n"
            "• <code>/user ban &lt;USER_ID|@username&gt;</code>\n"
            "• <code>/user unban &lt;USER_ID|@username&gt;</code>\n\n"
            "<i>Note: Passing 0 targets all locked partitions.</i>"
        )
        return await update.message.reply_text(help_text, parse_mode="HTML")

    sub = args[0].lower()

    # Handle syntax like /user grant revoke ...
    if sub == "grant" and len(args) > 1 and args[1].lower() in ["revoke", "freeze", "unfreeze"]:
        sub = args[1].lower()
        args = [sub] + args[2:]

    if sub == "grant":
        if len(args) < 3:
            return await update.message.reply_text("❌ Usage: <code>/user grant &lt;USER_ID|@username&gt; &lt;PREFIX|0&gt; [DURATION]</code>", parse_mode="HTML")
        uid, uname = await resolve_target_user(args[1])
        if not uid:
            return await update.message.reply_text(f"❌ User <code>{args[1]}</code> not found in database.", parse_mode="HTML")
        target_prefix = args[2].upper().strip()
        duration_str = args[3] if len(args) > 3 else None
        expires_at = parse_duration(duration_str) if duration_str else None
        duration_note = f"expires in {duration_str}" if expires_at else "Permanent"

        if target_prefix == "0":
            cursor = group_keys_col.find({}) if group_keys_col is not None else []
            keys_to_add = [f"{doc['chat_id']}_{doc['prefix']}" async for doc in cursor]
            if not keys_to_add:
                return await update.message.reply_text("ℹ️ No locked partitions currently exist.")
            update_op = {
                "$addToSet": {"unlocked_prefixes": {"$each": keys_to_add}},
                "$pull": {"frozen_prefixes": {"$each": keys_to_add}}
            }
            if expires_at:
                update_op["$set"] = {f"prefix_expirations.{k}": expires_at for k in keys_to_add}
            await unlocked_groups_col.update_one({"user_id": uid}, update_op, upsert=True)
            await update.message.reply_text(f"✅ Granted access to <b>ALL locked partitions</b> ({len(keys_to_add)}) for {uname} ({duration_note}).", parse_mode="HTML")
            await log_event(ADMIN_ID, "ADMIN", f"Granted ALL to {uid} ({duration_note})")
            await notify_admin(context, f"👥 Granted ALL partitions to {uname} ({duration_note}).")
        else:
            pfx = target_prefix[:3]
            gate = await group_keys_col.find_one({"prefix": pfx}) if group_keys_col is not None else None
            chat_id = gate["chat_id"] if gate else update.effective_chat.id
            auth_key = f"{chat_id}_{pfx}"
            update_op = {
                "$addToSet": {"unlocked_prefixes": auth_key},
                "$pull": {"frozen_prefixes": auth_key}
            }
            if expires_at:
                update_op["$set"] = {f"prefix_expirations.{auth_key}": expires_at}
            await unlocked_groups_col.update_one({"user_id": uid}, update_op, upsert=True)
            await update.message.reply_text(f"✅ Granted access to <code>{pfx}</code> for {uname} ({duration_note}).", parse_mode="HTML")
            await log_event(ADMIN_ID, "ADMIN", f"Granted {pfx} to {uid} ({duration_note})")
            await notify_admin(context, f"👥 Granted `{pfx}` to {uname} ({duration_note}).")

    elif sub == "revoke":
        if len(args) < 3:
            return await update.message.reply_text("❌ Usage: <code>/user revoke &lt;USER_ID|@username&gt; &lt;PREFIX|0&gt;</code>", parse_mode="HTML")
        uid, uname = await resolve_target_user(args[1])
        if not uid:
            return await update.message.reply_text(f"❌ User <code>{args[1]}</code> not found.", parse_mode="HTML")
        target_prefix = args[2].upper().strip()
        user_rec = await unlocked_groups_col.find_one({"user_id": uid})
        if not user_rec:
            return await update.message.reply_text(f"❌ {uname} has no access records.", parse_mode="HTML")

        unlocked = user_rec.get("unlocked_prefixes", [])
        frozen = user_rec.get("frozen_prefixes", [])
        if target_prefix == "0":
            matched_keys = list(set(unlocked + frozen))
        else:
            pfx = target_prefix[:3]
            matched_keys = [k for k in (unlocked + frozen) if k.endswith(f"_{pfx}") or k == pfx]

        if not matched_keys:
            return await update.message.reply_text(f"❌ {uname} has no matching access to revoke.", parse_mode="HTML")

        unset_fields = {f"prefix_expirations.{k}": "" for k in matched_keys}
        await unlocked_groups_col.update_one(
            {"user_id": uid},
            {
                "$pull": {
                    "unlocked_prefixes": {"$in": matched_keys},
                    "frozen_prefixes": {"$in": matched_keys}
                },
                "$unset": unset_fields
            }
        )
        target_desc = "ALL partitions" if target_prefix == "0" else f"<code>{target_prefix[:3]}</code>"
        await update.message.reply_text(f"🗑️ Revoked access to {target_desc} for {uname}.", parse_mode="HTML")
        await log_event(ADMIN_ID, "ADMIN", f"Revoked {target_prefix} from {uid}")
        await notify_admin(context, f"👥 Revoked {target_desc} from {uname}.")

    elif sub == "freeze":
        if len(args) < 2:
            return await update.message.reply_text("❌ Usage: <code>/user freeze &lt;USER_ID|@username&gt; [PREFIX|0] [DURATION]</code>", parse_mode="HTML")
        uid, uname = await resolve_target_user(args[1])
        if not uid:
            return await update.message.reply_text(f"❌ User <code>{args[1]}</code> not found.", parse_mode="HTML")

        target_prefix = "0"
        duration_str = None
        if len(args) > 2:
            if any(args[2].lower().endswith(unit) for unit in ["m", "h", "d", "w"]):
                duration_str = args[2]
            else:
                target_prefix = args[2].upper().strip()
                if len(args) > 3:
                    duration_str = args[3]

        expires_at = parse_duration(duration_str) if duration_str else None
        duration_note = f"frozen for {duration_str}" if expires_at else "Frozen Indefinitely"

        user_rec = await unlocked_groups_col.find_one({"user_id": uid})
        if not user_rec:
            return await update.message.reply_text(f"❌ {uname} has no active access to freeze.", parse_mode="HTML")

        unlocked = user_rec.get("unlocked_prefixes", [])
        if target_prefix == "0":
            matched_keys = list(unlocked)
        else:
            pfx = target_prefix[:3]
            matched_keys = [k for k in unlocked if k.endswith(f"_{pfx}") or k == pfx]

        if not matched_keys:
            return await update.message.reply_text(f"❌ {uname} has no active unlocked access matching {target_prefix}.", parse_mode="HTML")

        update_op = {
            "$pull": {"unlocked_prefixes": {"$in": matched_keys}},
            "$addToSet": {"frozen_prefixes": {"$each": matched_keys}}
        }
        if expires_at:
            update_op["$set"] = {f"prefix_expirations.{k}": expires_at for k in matched_keys}
        await unlocked_groups_col.update_one({"user_id": uid}, update_op)
        target_desc = "ALL partitions" if target_prefix == "0" else f"<code>{target_prefix[:3]}</code>"
        await update.message.reply_text(f"❄️ Froze access to {target_desc} for {uname} ({duration_note}).", parse_mode="HTML")
        await log_event(ADMIN_ID, "ADMIN", f"Froze {target_prefix} for {uid} ({duration_note})")
        await notify_admin(context, f"❄️ Froze {target_desc} for {uname} ({duration_note}).")

    elif sub == "unfreeze":
        if len(args) < 2:
            return await update.message.reply_text("❌ Usage: <code>/user unfreeze &lt;USER_ID|@username&gt; [PREFIX|0]</code>", parse_mode="HTML")
        uid, uname = await resolve_target_user(args[1])
        if not uid:
            return await update.message.reply_text(f"❌ User <code>{args[1]}</code> not found.", parse_mode="HTML")

        target_prefix = args[2].upper().strip() if len(args) > 2 else "0"
        user_rec = await unlocked_groups_col.find_one({"user_id": uid})
        if not user_rec:
            return await update.message.reply_text(f"❌ {uname} has no access records.", parse_mode="HTML")

        frozen = user_rec.get("frozen_prefixes", [])
        if target_prefix == "0":
            matched_keys = list(frozen)
        else:
            pfx = target_prefix[:3]
            matched_keys = [k for k in frozen if k.endswith(f"_{pfx}") or k == pfx]

        if not matched_keys:
            return await update.message.reply_text(f"ℹ️ {uname} has no frozen partitions matching {target_prefix}.", parse_mode="HTML")

        await unlocked_groups_col.update_one(
            {"user_id": uid},
            {
                "$pull": {"frozen_prefixes": {"$in": matched_keys}},
                "$addToSet": {"unlocked_prefixes": {"$each": matched_keys}}
            }
        )
        target_desc = "ALL partitions" if target_prefix == "0" else f"<code>{target_prefix[:3]}</code>"
        await update.message.reply_text(f"✅ Unfroze access to {target_desc} for {uname}.", parse_mode="HTML")
        await log_event(ADMIN_ID, "ADMIN", f"Unfroze {target_prefix} for {uid}")
        await notify_admin(context, f"✅ Unfroze {target_desc} for {uname}.")

    elif sub == "ban":
        if len(args) < 2:
            return await update.message.reply_text("❌ Usage: <code>/user ban &lt;USER_ID|@username&gt;</code>", parse_mode="HTML")
        uid, uname = await resolve_target_user(args[1])
        if not uid:
            return await update.message.reply_text(f"❌ User <code>{args[1]}</code> not found.", parse_mode="HTML")
        await unlocked_groups_col.update_one({"user_id": uid}, {"$set": {"is_banned": True}}, upsert=True)
        await update.message.reply_text(f"🚫 <b>User Banned:</b> {uname} (<code>{uid}</code>) is now blocked.", parse_mode="HTML")
        await log_event(ADMIN_ID, "ADMIN", f"Banned user {uid}")
        await notify_admin(context, f"🚫 Admin banned {uname} ({uid}).")

    elif sub == "unban":
        if len(args) < 2:
            return await update.message.reply_text("❌ Usage: <code>/user unban &lt;USER_ID|@username&gt;</code>", parse_mode="HTML")
        uid, uname = await resolve_target_user(args[1])
        if not uid:
            return await update.message.reply_text(f"❌ User <code>{args[1]}</code> not found.", parse_mode="HTML")
        await unlocked_groups_col.update_one({"user_id": uid}, {"$set": {"is_banned": False}})
        await update.message.reply_text(f"✅ <b>User Unbanned:</b> {uname} (<code>{uid}</code>) is unblocked.", parse_mode="HTML")
        await log_event(ADMIN_ID, "ADMIN", f"Unbanned user {uid}")
        await notify_admin(context, f"✅ Admin unbanned {uname} ({uid}).")
    else:
        await update.message.reply_text(f"❌ Unknown /user action: <code>{sub}</code>", parse_mode="HTML")


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


async def file_command_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unified file storage & partition router: /file save|del|lock|unlock|mv"""
    if update.effective_user.id != ADMIN_ID or codes_col is None:
        return
    args = context.args or []
    if not args:
        help_text = (
            "📁 <b>FILE STORAGE ENGINE</b>\n\n"
            "• <code>/file save &lt;PREFIX&gt;</code> - Index replied file (auto-increments 001, 002...)\n"
            "• <code>/file save &lt;PREFIX&gt; &lt;START&gt; &lt;END&gt;</code> - Forward mass index range\n"
            "• <code>/file del &lt;CODE&gt;</code> - Delete single asset\n"
            "• <code>/file del &lt;PREFIX&gt; &lt;START&gt; &lt;END&gt;</code> - Surgical range delete\n"
            "• <code>/file lock &lt;PREFIX&gt; &lt;PASSWORD&gt;</code> - Secure partition with passkey\n"
            "• <code>/file unlock &lt;PREFIX&gt;</code> - Remove passkey partition\n"
            "• <code>/file mv &lt;OLD&gt; &lt;NEW&gt;</code> - Bulk migrate/rename prefix"
        )
        return await update.message.reply_text(help_text, parse_mode="HTML")

    sub = args[0].lower()

    if sub == "save":
        if update.message.reply_to_message:
            if len(args) < 2:
                return await update.message.reply_text("❌ Usage: <code>/file save &lt;PREFIX&gt;</code> (replying to a file)", parse_mode="HTML")

            raw_target = args[1].upper().strip()
            if len(raw_target) > 3 and raw_target[3:].isdigit():
                code_to_save = raw_target
            else:
                prefix = raw_target[:3]
                escaped = re.escape(prefix)
                exist = await codes_col.count_documents({"code": {"$regex": f"^{escaped}"}})
                code_to_save = f"{prefix}{exist + 1:03d}"

            f_type, f_id, caption, f_name = extract_file_data(update.message.reply_to_message)
            if not f_id:
                return await update.message.reply_text("❌ No recognizable file found in replied message.", parse_mode="HTML")

            data = {
                "chat_id": update.effective_chat.id,
                "message_id": update.message.reply_to_message.message_id,
                "file_type": f_type,
                "file_id": f_id,
                "caption": caption or "",
                "file_name": f_name or ""
            }
            await codes_col.update_one({"code": code_to_save}, {"$set": data}, upsert=True)
            await update.message.reply_text(f"✅ Indexed <code>{code_to_save}</code> (Type: {f_type})", parse_mode="HTML")
            await log_event(ADMIN_ID, "ADMIN", f"Indexed {code_to_save}")
            await notify_admin(context, f"✅ Admin indexed <code>{code_to_save}</code>.")
        else:
            if len(args) < 4:
                return await update.message.reply_text(
                    "❌ <b>Usage for /file save:</b>\n"
                    "• Single: Reply to a file with <code>/file save &lt;PREFIX&gt;</code>\n"
                    "• Bulk: <code>/file save &lt;PREFIX&gt; &lt;START&gt; &lt;END&gt;</code>",
                    parse_mode="HTML"
                )
            try:
                if args[1].isdigit() and args[2].isdigit():
                    start_id, end_id = int(args[1]), int(args[2])
                    prefix = args[3].upper().strip()[:3]
                elif args[2].isdigit() and args[3].isdigit():
                    prefix = args[1].upper().strip()[:3]
                    start_id, end_id = int(args[2]), int(args[3])
                else:
                    return await update.message.reply_text("❌ Usage: <code>/file save &lt;PREFIX&gt; &lt;START_ID&gt; &lt;END_ID&gt;</code>", parse_mode="HTML")

                if start_id > end_id:
                    return await update.message.reply_text("❌ START_ID must be less than or equal to END_ID.", parse_mode="HTML")

                escaped_prefix = re.escape(prefix)
                exist = await codes_col.count_documents({"code": {"$regex": f"^{escaped_prefix}"}})
                curr = exist + 1
                status_msg = await update.message.reply_text(f"🔄 Probing <code>{prefix}</code> range from {start_id} to {end_id}...", parse_mode="HTML")
                indexed_count = 0
                for m_id in range(start_id, end_id + 1):
                    try:
                        probe = await context.bot.forward_message(chat_id=ADMIN_ID, from_chat_id=update.effective_chat.id, message_id=m_id)
                        f_type, f_id, caption, f_name = extract_file_data(probe)
                        await probe.delete()
                        if f_id:
                            code_to_save = f"{prefix}{curr:03d}"
                            await codes_col.update_one(
                                {"code": code_to_save},
                                {"$set": {
                                    "chat_id": update.effective_chat.id,
                                    "message_id": m_id,
                                    "file_type": f_type,
                                    "file_id": f_id,
                                    "caption": caption or "",
                                    "file_name": f_name or ""
                                }},
                                upsert=True
                            )
                            indexed_count += 1
                            curr += 1
                    except Exception:
                        continue
                await status_msg.edit_text(f"✅ <b>Bulk Indexing Complete!</b> Indexed <code>{indexed_count}</code> items for <code>{prefix}</code>.", parse_mode="HTML")
                await log_event(ADMIN_ID, "ADMIN", f"Bulk save: {prefix} ({indexed_count} items)")
                await notify_admin(context, f"📦 Bulk save success for <code>{prefix}</code> ({indexed_count} items).")
            except Exception as e:
                await update.message.reply_text(f"❌ Error during bulk save: {e}", parse_mode="HTML")
                await notify_admin(context, f"🔴 Bulk save failure: {e}")

    elif sub == "del":
        if len(args) == 2:
            code = args[1].upper().strip()
            res = await codes_col.delete_one({"code": code})
            if res.deleted_count > 0:
                await update.message.reply_text(f"🗑️ Vaporized <code>{code}</code>.", parse_mode="HTML")
                await log_event(ADMIN_ID, "ADMIN", f"Single Del: {code}")
                await notify_admin(context, f"🗑️ Admin deleted <code>{code}</code>.")
            else:
                await update.message.reply_text(f"❌ <code>{code}</code> not found.", parse_mode="HTML")
        elif len(args) == 4:
            try:
                prefix = args[1].upper().strip()[:3]
                start = int(args[2])
                end = int(args[3])
                if start < 1 or end < start or (end - start + 1) > 100:
                    return await update.message.reply_text("❌ Range error: Maximum 100 items can be deleted at once.", parse_mode="HTML")
                target_codes = [f"{prefix}{i:03d}" for i in range(start, end + 1)]
                result = await codes_col.delete_many({"code": {"$in": target_codes}})
                await update.message.reply_text(f"🗑️ Vaporized <code>{result.deleted_count}</code> items.", parse_mode="HTML")
                await log_event(ADMIN_ID, "ADMIN", f"Range Del: {prefix}{start:03d}-{end:03d}")
                await notify_admin(context, f"🗑️ Range Delete for <code>{prefix}</code> ({result.deleted_count} items).")
            except ValueError:
                await update.message.reply_text("❌ START and END must be numbers.", parse_mode="HTML")
        else:
            await update.message.reply_text(
                "❌ <b>Usage:</b>\n"
                "• Single: <code>/file del &lt;CODE&gt;</code>\n"
                "• Range: <code>/file del &lt;PREFIX&gt; &lt;START&gt; &lt;END&gt;</code>",
                parse_mode="HTML"
            )

    elif sub == "lock":
        if len(args) < 3:
            return await update.message.reply_text("❌ Usage: <code>/file lock &lt;PREFIX&gt; &lt;PASSWORD&gt;</code>", parse_mode="HTML")
        prefix = args[1].upper().strip()[:3]
        password = args[2].strip()
        if group_keys_col is not None:
            await group_keys_col.update_one({"chat_id": update.effective_chat.id, "prefix": prefix}, {"$set": {"secret_key": password}}, upsert=True)
        await update.message.reply_text(f"🔒 Partition <code>{prefix}</code> locked with passkey!", parse_mode="HTML")
        await log_event(ADMIN_ID, "ADMIN", f"Locked prefix: {prefix}")
        await notify_admin(context, f"🔒 Access key set for <code>{prefix}</code>.")

    elif sub == "unlock":
        if len(args) < 2:
            return await update.message.reply_text("❌ Usage: <code>/file unlock &lt;PREFIX&gt;</code>", parse_mode="HTML")
        prefix = args[1].upper().strip()[:3]
        if group_keys_col is not None:
            res = await group_keys_col.delete_many({"prefix": prefix})
            if res.deleted_count > 0:
                await update.message.reply_text(f"🔓 <b>Partition Removed!</b> Prefix <code>{prefix}</code> is now completely unlocked.", parse_mode="HTML")
                await log_event(ADMIN_ID, "ADMIN", f"Unset Key: {prefix}")
                await notify_admin(context, f"🔓 Admin removed password partition from <code>{prefix}</code>.")
            else:
                await update.message.reply_text(f"ℹ️ Prefix <code>{prefix}</code> was not password-locked.", parse_mode="HTML")

    elif sub == "mv":
        if len(args) < 3:
            return await update.message.reply_text("❌ Usage: <code>/file mv &lt;OLD&gt; &lt;NEW&gt;</code>", parse_mode="HTML")
        old_prefix = args[1].upper().strip()[:3]
        new_prefix = args[2].upper().strip()[:3]
        if old_prefix == new_prefix:
            return await update.message.reply_text("❌ Prefixes are the same.", parse_mode="HTML")

        status = await update.message.reply_text(f"🔄 <b>Migrating <code>{old_prefix}</code> to <code>{new_prefix}</code>...</b>", parse_mode="HTML")
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

            if group_keys_col is not None:
                await group_keys_col.update_many({"prefix": old_prefix}, {"$set": {"prefix": new_prefix}})
            if prefix_labels_col is not None:
                await prefix_labels_col.update_many({"prefix": old_prefix}, {"$set": {"prefix": new_prefix}})

            await status.edit_text(f"✅ <b>Migration Complete!</b>\nMoved <code>{count}</code> items from <code>{old_prefix}</code> to <code>{new_prefix}</code>.", parse_mode="HTML")
            await log_event(ADMIN_ID, "ADMIN", f"Rename Prefix: {old_prefix} -> {new_prefix} ({count} items)")
            await notify_admin(context, f"🔄 Migrated <code>{old_prefix}</code> to <code>{new_prefix}</code> ({count} items).")
        except Exception as e:
            logger.error(f"Rename error: {e}")
            await status.edit_text(f"❌ <b>Migration Failed:</b> {e}", parse_mode="HTML")
            await notify_admin(context, f"🔴 RENAME FAILURE: {e}")
    else:
        await update.message.reply_text(f"❌ Unknown /file action: <code>{sub}</code>", parse_mode="HTML")


async def media_command_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unified media catalog router: /media label|tmdb|banner|season|refresh"""
    if update.effective_user.id != ADMIN_ID or prefix_labels_col is None:
        return
    args = context.args or []
    if not args:
        help_text = (
            "🎬 <b>MEDIA & CATALOG MANAGEMENT</b>\n\n"
            "• <code>/media label set &lt;PREFIX&gt; &lt;MOVIE|SERIES&gt; &lt;TITLE&gt;</code> - Handpick title & category\n"
            "• <code>/media label del &lt;PREFIX&gt;</code> - Remove partition label\n"
            "• <code>/media tmdb set &lt;PREFIX&gt; &lt;QUERY/ID&gt;</code> - Link TMDB metadata & poster\n"
            "• <code>/media tmdb del &lt;PREFIX&gt;</code> - Unlink TMDB metadata\n"
            "• <code>/media banner &lt;PREFIX&gt; toggle</code> - Toggle official poster vs backdrop\n"
            "• <code>/media season set &lt;PREFIX&gt; &lt;SN&gt; &lt;START&gt; &lt;END&gt;</code> - Map season episode range\n"
            "• <code>/media season del &lt;PREFIX&gt; &lt;SN&gt;</code> - Delete specific season\n"
            "• <code>/media season clear &lt;PREFIX&gt;</code> - Clear all mapped seasons\n"
            "• <code>/media refresh</code> - Sync Telegram files & refresh linked TMDB"
        )
        return await update.message.reply_text(help_text, parse_mode="HTML")

    sub = args[0].lower()

    if sub == "label":
        action = args[1].lower() if len(args) > 1 else ""
        if action == "set":
            if len(args) < 5:
                return await update.message.reply_text("❌ Usage: <code>/media label set &lt;PREFIX&gt; &lt;MOVIE|SERIES&gt; &lt;TITLE&gt;</code>", parse_mode="HTML")
            prefix = args[2].upper().strip()[:3]
            category = args[3].upper().strip()
            title = " ".join(args[4:]).strip()
            if category not in ["MOVIE", "SERIES"]:
                return await update.message.reply_text("❌ Category must be <b>MOVIE</b> or <b>SERIES</b>.", parse_mode="HTML")
            set_doc = {"name": title, "category": category.lower()}
            await prefix_labels_col.update_one({"prefix": prefix}, {"$set": set_doc}, upsert=True)
            await update.message.reply_text(
                f"🏷️ <b>Handpicked Label Set!</b>\n"
                f"Partition <code>{prefix}</code> [{category}] ➔ <b>{html.escape(title)}</b>\n\n"
                f"💡 <i>To link specific TMDB metadata & poster:</i> <code>/media tmdb set {prefix} &lt;title or ID&gt;</code>",
                parse_mode="HTML"
            )
            await log_event(ADMIN_ID, "ADMIN", f"Set Label: {prefix} -> {title} [{category}]")
            await notify_admin(context, f"🏷️ Admin assigned label <code>{prefix}</code> ➔ {title} [{category}].")
        elif action == "del":
            if len(args) < 3:
                return await update.message.reply_text("❌ Usage: <code>/media label del &lt;PREFIX&gt;</code>", parse_mode="HTML")
            prefix = args[2].upper().strip()[:3]
            rec = await prefix_labels_col.find_one({"prefix": prefix})
            if not rec:
                return await update.message.reply_text(f"ℹ️ Prefix <code>{prefix}</code> has no assigned title or category.", parse_mode="HTML")
            old_title = rec.get("name", prefix)
            old_cat = rec.get("category", "unknown").upper()
            await prefix_labels_col.delete_one({"prefix": prefix})
            asset_count = await codes_col.count_documents({"code": {"$regex": f"^{re.escape(prefix)}"}}) if codes_col is not None else 0
            await update.message.reply_text(
                f"🗑️ <b>Label Removed!</b>\n"
                f"Partition <code>{prefix}</code> (<code>{html.escape(old_title)}</code> [{old_cat}]) unlinked.\n"
                f"📦 <code>{asset_count}</code> asset(s) in database remain preserved.",
                parse_mode="HTML"
            )
            await log_event(ADMIN_ID, "ADMIN", f"Deleted label: {prefix}")
            await notify_admin(context, f"🏷️ Admin removed title/category label from <code>{prefix}</code> ({asset_count} assets kept).")
        else:
            await update.message.reply_text("❌ Usage:\n• <code>/media label set &lt;PREFIX&gt; &lt;MOVIE|SERIES&gt; &lt;TITLE&gt;</code>\n• <code>/media label del &lt;PREFIX&gt;</code>", parse_mode="HTML")

    elif sub == "tmdb":
        action = args[1].lower() if len(args) > 1 else ""
        if action == "set":
            if len(args) < 4:
                return await update.message.reply_text(
                    "❌ <b>Usage:</b> <code>/media tmdb set &lt;PREFIX&gt; &lt;TITLE or TMDB_ID&gt;</code>\n\n"
                    "<b>Examples:</b>\n"
                    "• <code>/media tmdb set SLV A Silent Voice (2016)</code>\n"
                    "• <code>/media tmdb set SLV 378064</code> (Direct TMDB ID)\n"
                    "• <code>/media tmdb set SLV tmdb:378064</code>",
                    parse_mode="HTML"
                )
            prefix = args[2].upper().strip()[:3]
            query = " ".join(args[3:]).strip()
            label_rec = await prefix_labels_col.find_one({"prefix": prefix})
            if not label_rec:
                return await update.message.reply_text(
                    f"❌ Prefix <code>{prefix}</code> is not labeled yet!\n"
                    f"Please run <code>/media label set {prefix} &lt;MOVIE|SERIES&gt; &lt;TITLE&gt;</code> first to define its category.",
                    parse_mode="HTML"
                )
            category = label_rec.get("category", "movie")
            status_msg = await update.message.reply_text(f"🔍 Searching TMDB for <code>{html.escape(query)}</code> [{category.upper()}]...", parse_mode="HTML")
            tmdb_info = await fetch_tmdb_metadata(query, category)
            if not tmdb_info:
                return await status_msg.edit_text(
                    f"❌ No TMDB matches found for <code>{html.escape(query)}</code> under category <code>{category}</code>.\n"
                    f"💡 <i>Tip:</i> Try providing the release year (e.g. <code>2016</code>) or direct TMDB ID.",
                    parse_mode="HTML"
                )
            tmdb_info["pinned"] = True
            await prefix_labels_col.update_one({"prefix": prefix}, {"$set": {"tmdb": tmdb_info}})
            year_str = f" ({tmdb_info['year']})" if tmdb_info.get("year") else ""
            genres_str = f"\n🎭 <b>Genres:</b> {html.escape(tmdb_info['genres'])}" if tmdb_info.get("genres") else ""
            rating_str = f"\n⭐ <b>Rating:</b> <code>{tmdb_info['rating']}/10</code>" if tmdb_info.get("rating") else ""
            poster_note = "\n🖼️ <b>Official Poster:</b> Attached" if tmdb_info.get("poster_url") else ""
            overview_snippet = f"\n\n📖 <b>Overview:</b>\n<i>{html.escape(tmdb_info['overview'][:200])}...</i>" if tmdb_info.get("overview") else ""
            await status_msg.edit_text(
                f"✅ <b>TMDB Synced for <code>{prefix}</code>!</b>\n\n"
                f"🎬 <b>Matched:</b> <b>{html.escape(tmdb_info['title'])}</b>{year_str}"
                f"{rating_str}"
                f"{genres_str}"
                f"{poster_note}"
                f"{overview_snippet}\n\n"
                f"<i>(Official poster and metadata are now linked to <code>{prefix}</code>)</i>",
                parse_mode="HTML"
            )
            await log_event(ADMIN_ID, "ADMIN", f"TMDB set for {prefix}: {tmdb_info['title']}")
            await notify_admin(context, f"🎬 TMDB metadata linked to <code>{prefix}</code>: {tmdb_info['title']}.")
        elif action == "del":
            if len(args) < 3:
                return await update.message.reply_text("❌ Usage: <code>/media tmdb del &lt;PREFIX&gt;</code>", parse_mode="HTML")
            prefix = args[2].upper().strip()[:3]
            res = await prefix_labels_col.update_one({"prefix": prefix}, {"$unset": {"tmdb": ""}})
            if res.modified_count > 0:
                await update.message.reply_text(f"🗑️ Removed TMDB metadata from <code>{prefix}</code>.", parse_mode="HTML")
                await log_event(ADMIN_ID, "ADMIN", f"Deleted TMDB for {prefix}")
                await notify_admin(context, f"🎬 Admin cleared TMDB metadata from <code>{prefix}</code>.")
            else:
                await update.message.reply_text(f"ℹ️ Prefix <code>{prefix}</code> had no TMDB metadata.", parse_mode="HTML")
        else:
            await update.message.reply_text("❌ Usage:\n• <code>/media tmdb set &lt;PREFIX&gt; &lt;QUERY/ID&gt;</code>\n• <code>/media tmdb del &lt;PREFIX&gt;</code>", parse_mode="HTML")

    elif sub == "banner":
        if len(args) < 2:
            return await update.message.reply_text("❌ Usage: <code>/media banner &lt;PREFIX&gt; toggle</code>", parse_mode="HTML")
        if args[1].lower() == "toggle" and len(args) > 2:
            prefix = args[2].upper().strip()[:3]
        else:
            prefix = args[1].upper().strip()[:3]

        rec = await prefix_labels_col.find_one({"prefix": prefix})
        if not rec:
            return await update.message.reply_text(f"❌ Prefix <code>{prefix}</code> not found in library.", parse_mode="HTML")
        current_mode = rec.get("banner_mode", "poster")
        new_mode = "backdrop" if current_mode == "poster" else "poster"
        await prefix_labels_col.update_one({"prefix": prefix}, {"$set": {"banner_mode": new_mode}})
        await update.message.reply_text(
            f"🖼️ <b>Banner Mode Toggled!</b>\n"
            f"Partition <code>{prefix}</code> is now set to: <b>{new_mode.upper()}</b> (was {current_mode.upper()}).",
            parse_mode="HTML"
        )
        await log_event(ADMIN_ID, "ADMIN", f"Banner toggled for {prefix}: {new_mode}")
        await notify_admin(context, f"🖼️ Banner mode for <code>{prefix}</code> toggled to <b>{new_mode.upper()}</b>.")

    elif sub == "season":
        action = args[1].lower() if len(args) > 1 else ""
        if action == "set":
            if len(args) < 6:
                return await update.message.reply_text("❌ Usage: <code>/media season set &lt;PREFIX&gt; &lt;SN&gt; &lt;START&gt; &lt;END&gt;</code>", parse_mode="HTML")
            try:
                prefix = args[2].upper().strip()[:3]
                sn_num = int(args[3])
                start = int(args[4])
                end = int(args[5])
                season_obj = {"num": sn_num, "start": start, "end": end}
                rec = await prefix_labels_col.find_one({"prefix": prefix})
                if not rec:
                    return await update.message.reply_text(f"❌ Prefix <code>{prefix}</code> not labeled.", parse_mode="HTML")
                seasons = rec.get("seasons", [])
                seasons = [s for s in seasons if s["num"] != sn_num]
                seasons.append(season_obj)
                seasons.sort(key=lambda x: x["num"])
                await prefix_labels_col.update_one({"prefix": prefix}, {"$set": {"seasons": seasons}})
                await update.message.reply_text(f"✅ Season {sn_num} mapped for <code>{prefix}</code> ({start}-{end}).", parse_mode="HTML")
            except ValueError:
                await update.message.reply_text("❌ SN, START, and END must be numbers.", parse_mode="HTML")
        elif action == "del":
            if len(args) < 4:
                return await update.message.reply_text("❌ Usage: <code>/media season del &lt;PREFIX&gt; &lt;SN&gt;</code>", parse_mode="HTML")
            try:
                prefix = args[2].upper().strip()[:3]
                sn_num = int(args[3])
                rec = await prefix_labels_col.find_one({"prefix": prefix})
                if not rec or "seasons" not in rec:
                    return await update.message.reply_text(f"❌ No seasons found for <code>{prefix}</code>.", parse_mode="HTML")
                seasons = [s for s in rec["seasons"] if s["num"] != sn_num]
                await prefix_labels_col.update_one({"prefix": prefix}, {"$set": {"seasons": seasons}})
                await update.message.reply_text(f"🗑️ Season {sn_num} deleted for <code>{prefix}</code>.", parse_mode="HTML")
            except ValueError:
                await update.message.reply_text("❌ SN must be a number.", parse_mode="HTML")
        elif action == "clear":
            if len(args) < 3:
                return await update.message.reply_text("❌ Usage: <code>/media season clear &lt;PREFIX&gt;</code>", parse_mode="HTML")
            prefix = args[2].upper().strip()[:3]
            await prefix_labels_col.update_one({"prefix": prefix}, {"$unset": {"seasons": ""}})
            await update.message.reply_text(f"🔥 Seasons wiped for <code>{prefix}</code>.", parse_mode="HTML")
        else:
            await update.message.reply_text("❌ Usage:\n• <code>/media season set &lt;PREFIX&gt; &lt;SN&gt; &lt;START&gt; &lt;END&gt;</code>\n• <code>/media season del &lt;PREFIX&gt; &lt;SN&gt;</code>\n• <code>/media season clear &lt;PREFIX&gt;</code>", parse_mode="HTML")

    elif sub == "refresh":
        await refresh_metadata(update, context)

    else:
        await update.message.reply_text(f"❌ Unknown /media action: <code>{sub}</code>", parse_mode="HTML")


async def refresh_metadata(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Syncs missing Telegram file metadata and enriches existing handpicked prefix labels with TMDB metadata."""
    if update.effective_user.id != ADMIN_ID or codes_col is None:
        return
    status_msg = await update.effective_message.reply_text("🔄 <b>Full System Sync Initiated</b>\nChecking file metadata & TMDB...", parse_mode="HTML")

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
                    await status_msg.edit_text(f"🔄 <b>Syncing Files...</b>\n<code>{file_sync_count}/{total_to_sync}</code> assets updated.", parse_mode="HTML")
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
                        tmdb_info["pinned"] = True
                        await prefix_labels_col.update_one({"_id": label_doc["_id"]}, {"$set": {"tmdb": tmdb_info}})
                        tmdb_sync_count += 1

                if (processed % 3 == 0) or processed == total_labels:
                    await status_msg.edit_text(
                        f"🔄 <b>Refreshing Linked TMDB Metadata...</b>\n"
                        f"<code>{processed}/{total_labels}</code> titles checked.\n"
                        f"Updated: <code>{tmdb_sync_count}</code>",
                        parse_mode="HTML"
                    )
                await asyncio.sleep(0.25)

    summary_lines = [
        "✅ <b>Sync Finalized!</b>",
        f"• Files probed/updated: <code>{file_sync_count}</code>",
        f"• Handpicked TMDB records refreshed: <code>{tmdb_sync_count}</code>",
    ]

    await status_msg.edit_text("\n".join(summary_lines), parse_mode="HTML")
    await log_event(ADMIN_ID, "ADMIN", f"Metadata Sync: {file_sync_count} files, {tmdb_sync_count} TMDB records")
    await notify_admin(context, f"📊 Metadata Sync complete. <code>{file_sync_count}</code> files and <code>{tmdb_sync_count}</code> TMDB records enriched.")


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
