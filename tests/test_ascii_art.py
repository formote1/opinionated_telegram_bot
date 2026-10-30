import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.ascii_art import (
    DEFAULT_MAX_WIDTH,
    FLF_FONT_PATH,
    FONT_ALIASES,
    MAX_INPUT_LENGTH,
    MAX_INPUT_LINES,
    MAX_OUTPUT_LENGTH,
    SUPPORTED_ASCII_FONTS,
    _char_width,
    _figlet_cache,
    _flf_cache,
    _get_figlet,
    ascii_command_handler,
    async_generate_ascii_art,
    generate_ascii_art,
    get_flf_font,
    render_flf_text,
    wrap_flf_line,
)


def test_ascii_art_generation():
    art = generate_ascii_art("TEST", "ansi_shadow")
    assert art is not None
    assert len(art) > 0
    assert "TEST" not in art or len(art) > 10


def test_font_aliases():
    assert FONT_ALIASES.get("ansi") == "ansi_shadow"
    assert FONT_ALIASES.get("3dshadow") == "ansi_shadow"
    assert "slant" in SUPPORTED_ASCII_FONTS


def test_empty_and_whitespace_input():
    assert generate_ascii_art("", "ansi_shadow") is None
    assert generate_ascii_art("    ", "slant") is None
    assert generate_ascii_art("\n\t  \n", "standard") is None
    assert render_flf_text("", FLF_FONT_PATH) is None
    assert render_flf_text("   \n  ", FLF_FONT_PATH) is None


def test_all_supported_fonts():
    for font in SUPPORTED_ASCII_FONTS:
        art = generate_ascii_art("HI", font)
        assert art is not None, f"Font {font} failed to render"
        assert len(art.strip()) > 0


def test_unknown_font_fallback():
    # If font is not supported, it should gracefully fall back to default FLF
    art = generate_ascii_art("FALLBACK", "completely_unknown_font_xyz")
    assert art is not None
    assert len(art.strip()) > 0


def test_flf_font_loading_and_caching():
    assert DEFAULT_MAX_WIDTH == 80
    chars, height = get_flf_font(FLF_FONT_PATH)
    assert height > 0
    assert "A" in chars
    assert " " in chars
    assert FLF_FONT_PATH in _flf_cache or os.path.abspath(FLF_FONT_PATH) in _flf_cache

    # Repeated call returns cached object
    chars2, height2 = get_flf_font(FLF_FONT_PATH)
    assert chars2 is chars
    assert height2 == height

    # Non-existent file
    assert get_flf_font("/non/existent/path.flf") == (None, 0)
    assert get_flf_font("") == (None, 0)

    # Corrupted / invalid file handling
    with tempfile.NamedTemporaryFile("w", delete=False) as tf:
        tf.write("corrupted flf header")
        tf_path = tf.name
    try:
        assert get_flf_font(tf_path) == (None, 0)
    finally:
        os.remove(tf_path)


def test_figlet_caching():
    _figlet_cache.clear()
    fig1 = _get_figlet("slant", 80)
    fig2 = _get_figlet("slant", 80)
    assert fig1 is fig2
    # Invalid font returns None
    assert _get_figlet("definitely_nonexistent_font_123") is None


def test_wrap_flf_line():
    chars, height = get_flf_font(FLF_FONT_PATH)
    assert chars is not None

    # Empty string
    assert wrap_flf_line("", chars, height, 80) == [""]

    # Short line under width
    short = "HELLO"
    assert wrap_flf_line(short, chars, height, 80) == [short]

    # Sentence that wraps on word boundaries
    sentence = "The quick brown fox jumps over the lazy dog and runs away"
    wrapped = wrap_flf_line(sentence, chars, height, 80)
    assert len(wrapped) > 1
    for line in wrapped:
        line_w = sum(_char_width(c, chars, height) for c in line)
        assert line_w <= 80

    # Long unbroken word that wraps on character boundaries
    unbroken = "X" * 40
    wrapped_unbroken = wrap_flf_line(unbroken, chars, height, 80)
    assert len(wrapped_unbroken) > 1
    for chunk in wrapped_unbroken:
        chunk_w = sum(_char_width(c, chars, height) for c in chunk)
        assert chunk_w <= 80


def test_render_flf_text_wrapping_and_limits():
    # Long text exceeding MAX_INPUT_LENGTH is truncated safely
    huge_text = "A" * (MAX_INPUT_LENGTH + 100)
    res = render_flf_text(huge_text, FLF_FONT_PATH, max_width=80)
    assert res is not None
    assert len(res) > 0

    # Text with many lines is truncated to MAX_INPUT_LINES safely
    multiline = "\n".join([f"Line {i}" for i in range(MAX_INPUT_LINES + 20)])
    res_multi = render_flf_text(multiline, FLF_FONT_PATH, max_width=80)
    assert res_multi is not None

    # Missing font file
    assert render_flf_text("hello", "/non/existent/font.flf") is None


def test_generate_ascii_art_large_text():
    # Verify bigger text renders quickly without hanging
    big_text = "The quick brown fox jumps over the lazy dog! " * 4
    art = generate_ascii_art(big_text, "ansi_shadow")
    assert art is not None
    assert len(art) > 0

    art_slant = generate_ascii_art(big_text, "slant")
    assert art_slant is not None
    assert len(art_slant) > 0


@pytest.mark.asyncio
async def test_async_generate_ascii_art_success():
    res = await async_generate_ascii_art("ASYNC", "slant")
    assert res is not None
    assert len(res) > 0


@pytest.mark.asyncio
async def test_async_generate_ascii_art_timeout():
    # Simulate a hanging generate_ascii_art
    def slow_render(*args, **kwargs):
        import time
        time.sleep(0.2)
        return "art"

    with patch("services.ascii_art.generate_ascii_art", side_effect=slow_render):
        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            await async_generate_ascii_art("TEXT", "slant", timeout=0.05)


@pytest.mark.asyncio
async def test_ascii_command_handler_help():
    update = MagicMock()
    context = MagicMock()
    message = AsyncMock()
    update.effective_message = message
    update.effective_user = MagicMock(id=123, username="tester")

    # No args -> help
    context.args = []
    await ascii_command_handler(update, context)
    message.reply_text.assert_awaited_once()
    assert "ASCII ART GENERATOR" in message.reply_text.call_args[0][0]

    # --help -> help
    message.reset_mock()
    context.args = ["--help"]
    await ascii_command_handler(update, context)
    message.reply_text.assert_awaited_once()
    assert "ASCII ART GENERATOR" in message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_ascii_command_handler_validation_limits():
    update = MagicMock()
    context = MagicMock()
    message = AsyncMock()
    update.effective_message = message
    update.effective_user = MagicMock(id=123, username="tester")

    # Text too long (> 200 chars)
    context.args = ["A" * 201]
    await ascii_command_handler(update, context)
    assert "too long" in message.reply_text.call_args[0][0]

    # Too many lines (> 10 lines)
    message.reset_mock()
    context.args = [("line\n" * 12)]
    await ascii_command_handler(update, context)
    assert "too many lines" in message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_ascii_command_handler_inline_delivery():
    update = MagicMock()
    context = MagicMock()
    message = AsyncMock()
    update.effective_message = message
    update.effective_user = MagicMock(id=123, username="tester")

    # Short word renders <= 52 line length and <= 3500 chars -> inline <pre>
    context.args = ["TEST"]
    with patch("services.ascii_art.log_event", AsyncMock()), patch("services.ascii_art.notify_admin", AsyncMock()):
        await ascii_command_handler(update, context)
    message.reply_text.assert_awaited_once()
    assert "<pre><code>" in message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_ascii_command_handler_document_delivery():
    update = MagicMock()
    context = MagicMock()
    message = AsyncMock()
    update.effective_message = message
    update.effective_user = MagicMock(id=123, username="tester")

    # Wide/long text triggers file delivery
    context.args = ["slant", "A very wide sentence that will definitely exceed fifty-two characters"]
    with patch("services.ascii_art.log_event", AsyncMock()), patch("services.ascii_art.notify_admin", AsyncMock()):
        await ascii_command_handler(update, context)
    message.reply_document.assert_awaited_once()
    assert message.reply_document.call_args[1]["filename"].startswith("ascii_")


@pytest.mark.asyncio
async def test_ascii_command_handler_unrenderable():
    update = MagicMock()
    context = MagicMock()
    message = AsyncMock()
    update.effective_message = message
    update.effective_user = MagicMock(id=123, username="tester")

    # Emojis only cannot be rendered as ASCII art
    context.args = ["🚀🚀🚀"]
    await ascii_command_handler(update, context)
    assert "Could not render" in message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_ascii_command_handler_timeout_handling():
    update = MagicMock()
    context = MagicMock()
    message = AsyncMock()
    update.effective_message = message
    update.effective_user = MagicMock(id=123, username="tester")

    context.args = ["Hello"]
    with patch("services.ascii_art.async_generate_ascii_art", side_effect=TimeoutError()):
        await ascii_command_handler(update, context)
    assert "timed out" in message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_ascii_command_handler_error_handling():
    update = MagicMock()
    context = MagicMock()
    message = AsyncMock()
    update.effective_message = message
    update.effective_user = MagicMock(id=123, username="tester")

    context.args = ["Hello"]
    with patch("services.ascii_art.async_generate_ascii_art", side_effect=RuntimeError("Test crash")), \
         patch("services.ascii_art.notify_admin", AsyncMock()):
        await ascii_command_handler(update, context)
    assert "Failed to generate ASCII" in message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_ascii_command_handler_context_args_none():
    update = MagicMock()
    context = MagicMock()
    message = AsyncMock()
    message.text = "/ascii slant Awesome"
    message.caption = None
    update.effective_message = message
    update.effective_user = MagicMock(id=123, username="tester")

    # context.args is None (like in MessageHandler)
    context.args = None
    with patch("services.ascii_art.log_event", AsyncMock()), patch("services.ascii_art.notify_admin", AsyncMock()):
        await ascii_command_handler(update, context)
    # Should successfully process and either reply_text or reply_document
    assert message.reply_text.called or message.reply_document.called
    if message.reply_text.called:
        assert "<pre><code>" in message.reply_text.call_args[0][0] or "ASCII Art" in message.reply_text.call_args[0][0]


def test_unicode_accent_normalization():
    art = generate_ascii_art("Café résumé naïve", "ansi_shadow")
    assert art is not None
    assert len(art.strip()) > 0


def test_wrap_flf_line_zero_and_negative_width():
    chars, height = get_flf_font(FLF_FONT_PATH)
    assert wrap_flf_line("HELLO WORLD", chars, height, max_width=0) == ["HELLO WORLD"]
    assert wrap_flf_line("HELLO WORLD", chars, height, max_width=-5) == ["HELLO WORLD"]


def test_wrap_flf_line_trailing_whitespace_no_phantom_lines():
    chars, height = get_flf_font(FLF_FONT_PATH)
    line = "word1 word2" + " " * 30
    wrapped = wrap_flf_line(line, chars, height, max_width=50)
    for sub in wrapped:
        assert sub.strip() != ""

    res = render_flf_text("  A   B  ", FLF_FONT_PATH, max_width=40)
    assert res is not None
    assert not res.endswith("\n\n\n\n\n\n")


def test_generate_ascii_art_multiline_figlet_limit():
    multiline_text = "\n".join([f"Line {i}" for i in range(25)])
    art = generate_ascii_art(multiline_text, "slant")
    assert art is not None
    lines = art.splitlines()
    assert len(lines) <= 10 * 8


def test_generate_ascii_art_output_length_cap():
    big_input = "WWWWW\n" * 10
    art = generate_ascii_art(big_input, "banner")
    if art:
        assert len(art) <= MAX_OUTPUT_LENGTH + 50


def test_control_characters_filtered():
    art = generate_ascii_art("HELLO\x00\x01\x1b[31mWORLD", "ansi_shadow")
    assert art is not None
    assert len(art.strip()) > 0


def test_get_flf_font_incomplete_file():
    with tempfile.NamedTemporaryFile("w", delete=False) as tf:
        tf.write("flf2a$ 6 5 20 15 1 0 0\nComment line\nTruncated content\n")
        tf_path = tf.name
    try:
        assert get_flf_font(tf_path) == (None, 0)
    finally:
        os.remove(tf_path)


@pytest.mark.asyncio
async def test_ascii_command_handler_multiline_raw_text():
    update = MagicMock()
    context = MagicMock()
    message = AsyncMock()
    message.text = "/ascii slant\nFirst Line\nSecond Line"
    message.caption = None
    update.effective_message = message
    update.effective_user = MagicMock(id=123, username="tester")
    context.args = None

    with patch("services.ascii_art.log_event", AsyncMock()), patch("services.ascii_art.notify_admin", AsyncMock()):
        await ascii_command_handler(update, context)

    assert message.reply_text.called or message.reply_document.called
    if message.reply_document.called:
        assert "ascii_slant.txt" in message.reply_document.call_args[1]["filename"]


@pytest.mark.asyncio
async def test_ascii_command_handler_multiline_default_font():
    update = MagicMock()
    context = MagicMock()
    message = AsyncMock()
    message.text = "/ascii\nFirst Line\nSecond Line"
    message.caption = None
    update.effective_message = message
    update.effective_user = MagicMock(id=123, username="tester")
    context.args = None

    with patch("services.ascii_art.log_event", AsyncMock()), patch("services.ascii_art.notify_admin", AsyncMock()):
        await ascii_command_handler(update, context)

    assert message.reply_text.called or message.reply_document.called
    if message.reply_document.called:
        assert "ascii_ansi_shadow.txt" in message.reply_document.call_args[1]["filename"]


@pytest.mark.asyncio
async def test_ascii_command_handler_notification_failure_resilience():
    update = MagicMock()
    context = MagicMock()
    message = AsyncMock()
    update.effective_message = message
    update.effective_user = MagicMock(id=123, username="tester")
    context.args = ["RESILIENT"]

    with patch("services.ascii_art.log_event", AsyncMock(side_effect=RuntimeError("DB dead"))), \
         patch("services.ascii_art.notify_admin", AsyncMock(side_effect=RuntimeError("Admin unreachable"))):
        await ascii_command_handler(update, context)

    assert message.reply_text.called or message.reply_document.called
    if message.reply_text.called:
        assert "Failed to generate ASCII" not in message.reply_text.call_args[0][0]

