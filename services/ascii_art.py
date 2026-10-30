import asyncio
import html
import io
import os
import unicodedata

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
_figlet_cache = {}

DEFAULT_ASCII_TIMEOUT = 5.0
MAX_INPUT_LENGTH = 200
MAX_INPUT_LINES = 10
DEFAULT_MAX_WIDTH = 80
MAX_OUTPUT_LENGTH = 50_000

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
    if not filepath:
        return None, 0
    norm_path = os.path.abspath(filepath)
    if norm_path in _flf_cache:
        return _flf_cache[norm_path]
    if filepath in _flf_cache:
        return _flf_cache[filepath]
    if not os.path.exists(filepath):
        return None, 0
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read().splitlines()
        if not content:
            return None, 0
        header = content[0].split()
        if len(header) < 6:
            return None, 0
        hardblank = header[0][5] if len(header[0]) > 5 else '$'
        height = int(header[1])
        comment_lines = int(header[5])
        start = 1 + comment_lines
        if len(content) < start + (127 - 32) * height:
            return None, 0

        endmark = '@'
        if start < len(content) and content[start]:
            endmark = content[start][-1]

        chars = {}
        for code in range(32, 127):
            char_lines = []
            for h in range(height):
                idx = start + (code - 32) * height + h
                if idx < len(content):
                    line = content[idx].rstrip(endmark).replace(hardblank, ' ')
                    char_lines.append(line)
            chars[chr(code)] = char_lines
        _flf_cache[norm_path] = (chars, height)
        _flf_cache[filepath] = (chars, height)
        return chars, height
    except Exception as e:
        logger.warning(f"Failed to load FLF font {filepath}: {e}")
        return None, 0


# Preload default FLF font if available to eliminate disk I/O on requests
if os.path.exists(FLF_FONT_PATH):
    get_flf_font(FLF_FONT_PATH)


def _get_figlet(font_name: str, width: int = DEFAULT_MAX_WIDTH):
    if not Figlet:
        return None
    key = (font_name.lower(), width)
    if key not in _figlet_cache:
        try:
            _figlet_cache[key] = Figlet(font=font_name, width=width)
        except Exception:
            _figlet_cache[key] = None
    return _figlet_cache[key]


# Preload supported Figlet fonts to eliminate runtime disk I/O
if Figlet:
    for f_name in SUPPORTED_ASCII_FONTS:
        if f_name != 'ansi_shadow':
            _get_figlet(f_name, DEFAULT_MAX_WIDTH)


def _char_width(ch: str, chars: dict, height: int) -> int:
    if ch in chars:
        glyph = chars[ch]
    elif ch.upper() in chars:
        glyph = chars[ch.upper()]
    else:
        return 0
    return len(glyph[0]) if glyph else 0


def wrap_flf_line(line: str, chars: dict, height: int, max_width: int = DEFAULT_MAX_WIDTH) -> list[str]:
    """Wrap a single text line into sub-lines that don't exceed max_width columns when rendered."""
    if max_width <= 0:
        return [line]
    line = line.expandtabs(4)
    if not line:
        return [""]

    total_w = sum(_char_width(c, chars, height) for c in line)
    if total_w <= max_width:
        return [line]

    words = line.split(" ")
    wrapped_lines = []
    current_words = []
    current_w = 0
    space_w = _char_width(" ", chars, height)

    def _split_long_word(w: str) -> list[str]:
        chunks = []
        cur = ""
        cur_w = 0
        for c in w:
            cw = _char_width(c, chars, height)
            if cur_w + cw > max_width and cur:
                chunks.append(cur)
                cur = c
                cur_w = cw
            else:
                cur += c
                cur_w += cw
        if cur:
            chunks.append(cur)
        return chunks

    for word in words:
        word_w = sum(_char_width(c, chars, height) for c in word)
        needed = (space_w + word_w) if current_words else word_w
        if current_w + needed <= max_width:
            current_words.append(word)
            current_w += needed
        else:
            if current_words:
                line_str = " ".join(current_words).rstrip()
                if line_str:
                    wrapped_lines.append(line_str)
                current_words = []
                current_w = 0

            if not word:
                continue

            if word_w > max_width:
                chunks = _split_long_word(word)
                for chunk in chunks[:-1]:
                    wrapped_lines.append(chunk)
                if chunks:
                    last_chunk = chunks[-1]
                    last_w = sum(_char_width(c, chars, height) for c in last_chunk)
                    current_words = [last_chunk]
                    current_w = last_w
            else:
                current_words = [word]
                current_w = word_w

    if current_words:
        line_str = " ".join(current_words).rstrip()
        if line_str:
            wrapped_lines.append(line_str)
    return wrapped_lines or [""]


def render_flf_text(text: str, filepath: str = FLF_FONT_PATH, max_width: int = DEFAULT_MAX_WIDTH) -> str | None:
    chars, height = get_flf_font(filepath)
    if not chars or not text:
        return None

    # Normalize unicode accents and line endings
    text = unicodedata.normalize('NFKD', text)
    text = ''.join(c for c in text if not unicodedata.combining(c))
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = "".join(c for c in text if c in ('\n', '\t') or (c >= ' ' and c != '\x7f'))

    if not text.strip():
        return None

    # Defensive truncation to avoid runaway rendering
    if len(text) > MAX_INPUT_LENGTH:
        text = text[:MAX_INPUT_LENGTH]
    raw_lines = text.splitlines()[:MAX_INPUT_LINES]
    if not raw_lines:
        return None

    rendered_blocks = []
    has_rendered_any_char = False

    for raw_line in raw_lines:
        if not raw_line.strip():
            rendered_blocks.append("")
            continue

        sublines = wrap_flf_line(raw_line, chars, height, max_width) if max_width > 0 else [raw_line]
        for subline in sublines:
            if not subline.strip():
                continue

            rows = ['' for _ in range(height)]
            block_has_char = False
            for ch in subline:
                if ch in chars:
                    glyph = chars[ch]
                elif ch.upper() in chars:
                    glyph = chars[ch.upper()]
                else:
                    continue

                if ch != ' ':
                    block_has_char = True

                for r in range(height):
                    rows[r] += glyph[r] if r < len(glyph) else ''

            if block_has_char:
                has_rendered_any_char = True
                rendered_blocks.append('\n'.join(r.rstrip() for r in rows))

    if not has_rendered_any_char or not rendered_blocks:
        return None

    result = '\n'.join(rendered_blocks)
    if len(result) > MAX_OUTPUT_LENGTH:
        result = result[:MAX_OUTPUT_LENGTH] + "\n... [Output truncated]"
    return result if result.strip() else None


def generate_ascii_art(text: str, font: str = 'ansi_shadow', max_width: int = DEFAULT_MAX_WIDTH) -> str | None:
    if not text or not text.strip():
        return None

    # Normalize unicode accents and line endings
    text = unicodedata.normalize('NFKD', text)
    text = ''.join(c for c in text if not unicodedata.combining(c))
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = "".join(c for c in text if c in ('\n', '\t') or (c >= ' ' and c != '\x7f'))

    if not text.strip():
        return None

    # Defensive truncation to avoid runaway rendering
    if len(text) > MAX_INPUT_LENGTH:
        text = text[:MAX_INPUT_LENGTH]

    # Enforce line count limit for all engines
    lines = text.splitlines()[:MAX_INPUT_LINES]
    if not lines:
        return None
    text = "\n".join(lines)

    target_font = FONT_ALIASES.get(font.lower(), font.lower())
    result = None

    if target_font == 'ansi_shadow':
        result = render_flf_text(text, FLF_FONT_PATH, max_width=max_width)
        if not (result and result.strip()):
            fig = _get_figlet('ansi_shadow', width=max_width)
            if fig:
                try:
                    res = fig.renderText(text)
                    if res and res.strip():
                        result = res
                except Exception:
                    pass
    else:
        fig = _get_figlet(target_font, width=max_width)
        if fig:
            try:
                res = fig.renderText(text)
                if res and res.strip():
                    result = res
            except Exception:
                pass

        if not (result and result.strip()):
            result = render_flf_text(text, FLF_FONT_PATH, max_width=max_width)

    if not result or not result.strip():
        return None

    if len(result) > MAX_OUTPUT_LENGTH:
        result = result[:MAX_OUTPUT_LENGTH] + "\n... [Output truncated]"

    return result


async def async_generate_ascii_art(
    text: str,
    font: str = 'ansi_shadow',
    timeout: float = DEFAULT_ASCII_TIMEOUT,
    max_width: int = DEFAULT_MAX_WIDTH,
) -> str | None:
    """Safely offload CPU-bound ASCII art rendering to a worker thread with timeout guard."""
    return await asyncio.wait_for(
        asyncio.to_thread(generate_ascii_art, text, font, max_width),
        timeout=timeout,
    )


async def ascii_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Guided ASCII banner generator with style options."""
    message = update.effective_message
    user = update.effective_user
    if not message:
        return

    args = context.args
    raw_text = (
        message.text
        if isinstance(getattr(message, "text", None), str)
        else (message.caption if isinstance(getattr(message, "caption", None), str) else "")
    )

    if args is None:
        if raw_text.startswith("/"):
            parts = raw_text.split()
            args = parts[1:] if len(parts) > 1 else []
        else:
            args = []

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

    # Extract font and text, preserving newlines from raw_text when available
    selected_font = 'ansi_shadow'
    text_content = ""

    parsed_from_raw = False
    if raw_text and raw_text.startswith("/"):
        cmd_sep = None
        for i, ch in enumerate(raw_text):
            if ch in (" ", "\t", "\n", "\r"):
                cmd_sep = i
                break
        if cmd_sep is not None:
            after_cmd = raw_text[cmd_sep:].lstrip(" \t")
            if after_cmd.startswith(("\r\n", "\n")):
                selected_font = 'ansi_shadow'
                text_content = after_cmd.lstrip("\r\n")
                parsed_from_raw = True
            else:
                tokens = after_cmd.split(None, 1)
                if tokens:
                    first_tok = tokens[0].lower()
                    if (first_tok in SUPPORTED_ASCII_FONTS or first_tok in FONT_ALIASES) and len(tokens) > 1:
                        selected_font = FONT_ALIASES.get(first_tok, first_tok)
                        rest = after_cmd[len(tokens[0]):].lstrip(" \t")
                        if rest.startswith("\r\n"):
                            rest = rest[2:]
                        elif rest.startswith("\n"):
                            rest = rest[1:]
                        text_content = rest
                        parsed_from_raw = True
                    else:
                        selected_font = 'ansi_shadow'
                        text_content = after_cmd
                        parsed_from_raw = True

    if not parsed_from_raw:
        first_arg = args[0].lower() if args else ''
        if (first_arg in SUPPORTED_ASCII_FONTS or first_arg in FONT_ALIASES) and len(args) > 1:
            selected_font = FONT_ALIASES.get(first_arg, first_arg)
            text_content = " ".join(args[1:])
        else:
            selected_font = 'ansi_shadow'
            text_content = " ".join(args)

    if not text_content or not text_content.strip():
        return await message.reply_text("❌ Please provide text to generate ASCII art.")

    if len(text_content) > MAX_INPUT_LENGTH:
        return await message.reply_text(
            f"❌ Input text is too long! Please keep it under {MAX_INPUT_LENGTH} characters."
        )

    if len(text_content.splitlines()) > MAX_INPUT_LINES:
        return await message.reply_text(
            f"❌ Input has too many lines! Please keep it under {MAX_INPUT_LINES} lines."
        )

    try:
        ascii_result = await async_generate_ascii_art(
            text=text_content,
            font=selected_font,
            timeout=DEFAULT_ASCII_TIMEOUT,
            max_width=DEFAULT_MAX_WIDTH,
        )

        if ascii_result and ascii_result.strip():
            max_line_len = max((len(line) for line in ascii_result.splitlines()), default=0)
            if len(ascii_result) <= 3500 and max_line_len <= 52:
                escaped_art = html.escape(ascii_result)
                await message.reply_text(
                    f"🎨 <b>ASCII Art (<code>{selected_font}</code>):</b>\n<pre><code>{escaped_art}</code></pre>",
                    parse_mode="HTML"
                )
            else:
                file_stream = io.BytesIO(ascii_result.encode('utf-8'))
                file_stream.name = f"ascii_{selected_font}.txt"
                file_stream.seek(0)
                summary_text = text_content[:30].replace("\n", " ")
                caption = f"🎨 ASCII Art ({selected_font} style)\nText: {summary_text}"
                await message.reply_document(
                    document=file_stream,
                    filename=f"ascii_{selected_font}.txt",
                    caption=caption
                )

            uid = user.id if user else 0
            uname = user.username if user else "unknown"
            try:
                await asyncio.gather(
                    log_event(uid, uname, f"Generated ASCII ({selected_font}: {text_content[:20]}...)"),
                    notify_admin(context, f"🎨 ASCII Generated by user <code>{uid}</code> ({selected_font})."),
                    return_exceptions=True
                )
            except Exception as notify_err:
                logger.debug(f"Non-critical notification error: {notify_err}")
        else:
            await message.reply_text("❌ Could not render that text. Try alphanumeric characters.")

    except (asyncio.TimeoutError, TimeoutError):
        logger.warning(f"ASCII generation timed out for font={selected_font}")
        await message.reply_text("⏱️ ASCII art generation timed out. Please try with shorter text.")
    except Exception as e:
        logger.error(f"ASCII Error: {e}")
        await message.reply_text(f"❌ Failed to generate ASCII: {e}")
        uid = user.id if user else 0
        try:
            await notify_admin(context, f"⚠️ ASCII Error for user {uid}: {e}")
        except Exception:
            pass

