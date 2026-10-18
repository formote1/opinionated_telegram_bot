from telegram import Update
from telegram.ext import ContextTypes
from config import ADMIN_ID
from utils.helpers import save_user_info
from handlers.admin import admin_palette_msg

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Greeting router for start and admin commands."""
    user = update.effective_user
    await save_user_info(user)
    if user.id == ADMIN_ID:
        text, markup = await admin_palette_msg()
        await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        greet = "👋 <b>Welcome.</b>\n\n⏰ /remind - Set countdown\n📜 /list - Reminders\n📦 /get - Library\n🎨 /ascii - Art\n\n© <b>NurAziz</b>"
        await update.message.reply_text(greet, parse_mode="HTML")
