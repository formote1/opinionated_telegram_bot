import asyncio
import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import ContextTypes

from config import ADMIN_ID, PORT, RENDER_URL
from handlers.admin import admin_palette_msg
from utils.auth import generate_auth_pin, generate_auth_token
from utils.helpers import save_user_info


async def web_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generates a secure temporary login link and PIN code for the web companion application."""
    user = update.effective_user
    if not user or not update.message:
        return
    asyncio.create_task(save_user_info(user))
    token = generate_auth_token(user.id, user.username)
    pin = generate_auth_pin(user.id, user.username)
    base_url = RENDER_URL.rstrip('/') if RENDER_URL else f"http://localhost:{PORT}"
    login_url = f"{base_url}/login?token={token}"
    auth_url = f"{base_url}/auth?token={token}"

    buttons = []
    if login_url.startswith("https://"):
        buttons.append([InlineKeyboardButton("🌐 Open Web App", web_app=WebAppInfo(url=login_url))])
    buttons.append([InlineKeyboardButton("🔗 Open in Browser", url=login_url)])

    text = (
        "🌐 <b>Web Companion Access</b>\n\n"
        "Your personalized web dashboard is ready! You can:\n"
        "• 🎨 Generate and download custom ASCII art\n"
        "• ⏰ Manage, snooze, and create countdown reminders\n"
        "• 📦 Browse media catalog with high-res TMDB posters\n"
        "• 🚀 One-click deliver movies and episodes directly to Telegram\n"
    )
    if ADMIN_ID and user.id == ADMIN_ID:
        text += "• 👑 Access master admin controls, matrices, and logs\n"

    text += (
        f"\n🔑 <b>One-Time Login PIN:</b> <code>{pin}</code>\n"
        "<i>(Valid for 10 minutes — paste into Web App login box)</i>\n\n"
        "🔗 <b>Direct Web Link:</b>\n"
        f"<code>{html.escape(auth_url)}</code>\n\n"
        "🔐 <i>This link is valid for 1 hour.</i>"
    )

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="HTML"
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Greeting router for start and admin commands."""
    user = update.effective_user
    asyncio.create_task(save_user_info(user))
    if user.id == ADMIN_ID:
        text, markup = await admin_palette_msg()
        await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        greet = "👋 <b>Welcome.</b>\n\n⏰ /remind - Set countdown\n📜 /list - Reminders\n📦 /get - Library\n🎨 /ascii - Art\n🌐 /web - Web Companion\n\n© <b>NurAziz</b>"
        await update.message.reply_text(greet, parse_mode="HTML")

