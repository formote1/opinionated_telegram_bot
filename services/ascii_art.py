import os
import io
import html
from telegram import Update
from telegram.ext import ContextTypes
try:
    from pyfiglet import Figlet
except ImportError:
    Figlet = None

from config import logger
from utils.sentinel import log_event, notify_admin

FLF_FONT_PATH = os.path.join(os.path.dirname(__file__), "fonts", "ansi_shadow.flf")
_flf_cache = {}

SUPPORTED_ASCII_FONTS = {
    'ansi_shadow': '3D ANSI block shadow font (User Favorite)',
    'slant': 'Slanted modern font',
    'standard': 'Standard classic ASCII',
    'banner': 'Large bold banner font',
    'digital': 'LED 7-segment digital font',
    'bubble': 'Rounded bubble letter font',
    'shadow': 'Classic shadow line font',
    'block': 'Solid full block font'
}

FONT_ALIASES = {
    'ansi': 'ansi_shadow',
    'ansi_shadow': 'ansi_shadow',
    'ansi-shadow': 'ansi_shadow',
    'ansishadow': 'ansi_shadow',
    'shadow3d': 'ansi_shadow',
    '3dshadow': 'ansi_shadow',
    'neon': 'ansi_shadow',
}


def get_flf_font(filepath: str):
    if filepath in _flf_cache:
        return _flf_cache[filepath]
    if not os.path.exists(filepath):
        return None, 0
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read().splitlines()
    header = content[0].split()
    hardblank = header[0][5] if len(header[0]) > 5 else '$'
    height = int(header[1])
    comment_lines = int(header[5])
    start = 1 + comment_lines
    chars = {}
    for code in range(32, 127):
        char_lines = []
        for h in range(height):
            idx = start + (code - 32) * height + h
            if idx < len(content):
                line = content[idx].rstrip('@').replace(hardblank, ' ')
                char_lines.append(line)
        chars[chr(code)] = char_lines
    _flf_cache[filepath] = (chars, height)
    return chars, height


def render_flf_text(text: str, filepath: str) -> str | None:
    chars, height = get_flf_font(filepath)
    if not chars:
        return None
    rows = ['' for _ in range(height)]
    for ch in text:
        ch_to_use = ch if ch in chars else (ch.upper() if ch.upper() in chars else ' ')
        glyph = chars.get(ch_to_use, [' ' * 4] * height)
        for r in range(height):
            rows[r] += glyph[r]
    return '\n'.join(r.rstrip() for r in rows)


def generate_ascii_art(text: str, font: str) -> str | None:
    target_font = FONT_ALIASES.get(font.lower(), font.lower())
    if target_font == 'ansi_shadow':
        res = render_flf_text(text, FLF_FONT_PATH)
        if res:
            return res
        if Figlet:
            try:
                return Figlet(font='ansi_shadow').renderText(text)
            except Exception:
                pass
    if Figlet:
        try:
            return Figlet(font=target_font).renderText(text)
        except Exception:
            pass
    return render_flf_text(text, FLF_FONT_PATH)


async def ascii_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Guided ASCII banner generator with style options."""
    message = update.effective_message
    user = update.effective_user
    args = context.args or []

    # Rich guidance if no arguments or help requested
    if not args or args[0].lower() in ["help", "--help", "-h"]:
        font_list = "\n".join([f"• <code>{name}</code> - {desc}" for name, desc in SUPPORTED_ASCII_FONTS.items()])
        help_text = (
            "🎨 <b>ASCII ART GENERATOR</b>\n"
            "───────────────────────────\n"
            "Convert any text into retro ASCII banner art!\n\n"
            "<b>📖 How to Use:</b>\n"
            "• <code>/ascii &lt;text&gt;</code> — Generate with <code>ansi_shadow</code> (Default)\n"
            "• <code>/ascii &lt;font&gt; &lt;text&gt;</code> — Choose custom font style\n\n"
            "<b>🔤 Available Font Styles:</b>\n"
            f"{font_list}\n\n"
            "<b>💡 Examples:</b>\n"
            "• <code>/ascii Hello</code>\n"
            "• <code>/ascii ansi_shadow Antigravity</code>\n"
            "• <code>/ascii digital 2026</code>\n"
            "• <code>/ascii slant Cyber</code>\n\n"
            "<i>(Tip: Short banners preview directly in chat; longer ones are delivered as downloadable text files.)</i>"
        )
        return await message.reply_text(help_text, parse_mode="HTML")

    first_arg = args[0].lower()
    if (first_arg in SUPPORTED_ASCII_FONTS or first_arg in FONT_ALIASES) and len(args) > 1:
        selected_font = FONT_ALIASES.get(first_arg, first_arg)
        text_content = " ".join(args[1:])
    else:
        selected_font = 'ansi_shadow'
        text_content = " ".join(args)

    if len(text_content) > 200:
        return await message.reply_text("❌ Input text is too long! Please keep it under 200 characters.")

    try:
        ascii_result = generate_ascii_art(text_content, selected_font)

        if ascii_result and ascii_result.strip():
            max_line_len = max((len(l) for l in ascii_result.splitlines()), default=0)
            if len(ascii_result) <= 3500 and max_line_len <= 52:
                escaped_art = html.escape(ascii_result)
                await message.reply_text(
                    f"🎨 <b>ASCII Art (<code>{selected_font}</code>):</b>\n<pre><code>{escaped_art}</code></pre>",
                    parse_mode="HTML"
                )
            else:
                file_stream = io.BytesIO(ascii_result.encode('utf-8'))
                file_stream.name = f"ascii_{selected_font}.txt"
                await message.reply_document(
                    document=file_stream,
                    filename=f"ascii_{selected_font}.txt",
                    caption=f"🎨 ASCII Art ({selected_font} style)\nText: {text_content[:30]}"
                )

            await log_event(user.id, user.username, f"Generated ASCII ({selected_font}: {text_content[:20]}...)")
            await notify_admin(context, f"🎨 ASCII Generated by user <code>{user.id}</code> ({selected_font}).")
        else:
            await message.reply_text("❌ Could not render that text. Try alphanumeric characters.")

    except Exception as e:
        logger.error(f"ASCII Error: {e}")
        await message.reply_text(f"❌ Failed to generate ASCII: {e}")
        await notify_admin(context, f"⚠️ ASCII Error for user {user.id}: {e}")
