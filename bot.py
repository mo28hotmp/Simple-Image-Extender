"""Simple Image Extender - Telegram bot.

Send an image, pick directions, a percentage and a fill colour; the bot
returns the image extended with that colour (no generation, just fill).
"""

import asyncio
import hashlib
import io
import logging
import os

from PIL import Image, ImageColor, ImageOps
from pillow_heif import register_heif_opener
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO
)
log = logging.getLogger("image-extender")

# HEIC/HEIF support (iPhone photos) on top of Pillow's built-in formats.
register_heif_opener()

# Limits keep us inside the 512 MB RAM of a free host.
Image.MAX_IMAGE_PIXELS = 40_000_000
MAX_OUTPUT_PIXELS = 30_000_000
MAX_PERCENT = 500
TG_UPLOAD_LIMIT = 49 * 1024 * 1024  # bots can upload up to 50 MB
PROCESS_SLOTS = asyncio.Semaphore(2)

DIRECTIONS, PERCENT, COLOR = range(3)
ORDER = ["top", "bottom", "left", "right"]
LABELS = {
    "top": "⬆️ Top",
    "bottom": "⬇️ Bottom",
    "left": "⬅️ Left",
    "right": "➡️ Right",
}

WELCOME = (
    "Hi! I extend images by adding blank space around them.\n\n"
    "Send me an image (as a photo or as a file, any format), then I'll ask "
    "for the directions, how much to add (in %) and the fill color.\n\n"
    "Tip: send it as a file to avoid Telegram's compression."
)


# ----------------------------------------------------------------- helpers
def padding(size, selected, percent):
    """Return (left, top, right, bottom) padding in pixels."""
    w, h = size
    return (
        round(w * percent / 100) if "left" in selected else 0,
        round(h * percent / 100) if "top" in selected else 0,
        round(w * percent / 100) if "right" in selected else 0,
        round(h * percent / 100) if "bottom" in selected else 0,
    )


def parse_percent(text):
    try:
        value = float(text.strip().replace("%", "").replace(",", "."))
    except ValueError:
        return None
    return value if 0 < value <= MAX_PERCENT else None


def parse_color(text):
    """Return an (r, g, b, a) tuple or None."""
    t = text.strip().lower()
    if t in ("transparent", "none", "clear"):
        return (0, 0, 0, 0)
    if t and len(t) in (3, 4, 6, 8) and all(c in "0123456789abcdef" for c in t):
        t = "#" + t
    try:
        c = ImageColor.getrgb(t)
    except ValueError:
        return None
    return c if len(c) == 4 else (*c, 255)


def extend_image(data, selected, percent, color):
    """Blocking work: returns (bytes, extension, (width, height))."""
    with Image.open(io.BytesIO(data)) as im:
        src_format = im.format
        im.load()  # first frame only for animated files
        img = ImageOps.exif_transpose(im)

    has_alpha = "A" in img.getbands() or "transparency" in img.info
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if has_alpha else "RGB")
    if color[3] < 255 and img.mode != "RGBA":
        img = img.convert("RGBA")

    left, top, right, bottom = padding(img.size, selected, percent)
    new_size = (img.width + left + right, img.height + top + bottom)
    if new_size[0] * new_size[1] > MAX_OUTPUT_PIXELS:
        raise ValueError("result too large")

    fill = color if img.mode == "RGBA" else color[:3]
    canvas = Image.new(img.mode, new_size, fill)
    canvas.paste(img, (left, top))  # no mask: exact pixel copy

    out = io.BytesIO()
    if canvas.mode == "RGB" and src_format in ("JPEG", "MPO", "HEIF"):
        canvas.save(out, "JPEG", quality=95)
        ext = "jpg"
    else:
        canvas.save(out, "PNG")
        ext = "png"
        if out.tell() > TG_UPLOAD_LIMIT and canvas.mode == "RGB":
            out = io.BytesIO()
            canvas.save(out, "JPEG", quality=95)
            ext = "jpg"
    if out.tell() > TG_UPLOAD_LIMIT:
        raise ValueError("result too large")
    return out.getvalue(), ext, new_size


def directions_keyboard(selected):
    def btn(key):
        mark = "✅ " if key in selected else ""
        return InlineKeyboardButton(mark + LABELS[key], callback_data=f"dir:{key}")

    return InlineKeyboardMarkup(
        [
            [btn("top")],
            [btn("left"), btn("right")],
            [btn("bottom")],
            [
                InlineKeyboardButton("All sides", callback_data="dir:all"),
                InlineKeyboardButton("Done ➡️", callback_data="dir:done"),
            ],
        ]
    )


def color_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⚪ White", callback_data="col:white"),
                InlineKeyboardButton("⚫ Black", callback_data="col:black"),
            ],
            [
                InlineKeyboardButton("🔘 Gray", callback_data="col:gray"),
                InlineKeyboardButton("🔲 Transparent", callback_data="col:transparent"),
            ],
        ]
    )


# ---------------------------------------------------------------- handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)


async def receive_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    item = msg.photo[-1] if msg.photo else msg.document
    await msg.chat.send_action(ChatAction.TYPING)

    try:
        tg_file = await item.get_file()
        data = bytes(await tg_file.download_as_bytearray())
    except BadRequest:
        await msg.reply_text(
            "I can't download that file - Telegram only lets bots read files up to 20 MB."
        )
        return ConversationHandler.END

    try:
        with Image.open(io.BytesIO(data)) as im:
            size = im.size
    except Exception:
        await msg.reply_text(
            "I couldn't read that as an image (or it's too large). "
            "Try another file."
        )
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data.update(image=data, size=size, selected=set())
    await msg.reply_text(
        f"Got it ({size[0]}×{size[1]} px). Which directions should I extend?\n"
        "Tap to toggle, then press Done.",
        reply_markup=directions_keyboard(set()),
    )
    return DIRECTIONS


async def choose_direction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    action = q.data.split(":", 1)[1]
    selected = context.user_data.setdefault("selected", set())

    if action == "done":
        if not selected:
            await q.answer("Pick at least one direction first.", show_alert=True)
            return DIRECTIONS
        await q.answer()
        names = ", ".join(LABELS[d] for d in ORDER if d in selected)
        await q.edit_message_text(
            f"Directions: {names}\n\n"
            "How much should I add? Send a percentage, e.g. 20 or 12.5\n"
            "(left/right use the image width, top/bottom use the height)."
        )
        return PERCENT

    if action == "all":
        if len(selected) == len(ORDER):
            selected.clear()
        else:
            selected.update(ORDER)
    elif action in ORDER:
        selected.symmetric_difference_update({action})

    await q.answer()
    await q.edit_message_reply_markup(directions_keyboard(selected))
    return DIRECTIONS


async def directions_hint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Please use the buttons above to pick directions.")
    return DIRECTIONS


async def receive_percent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data
    percent = parse_percent(update.message.text)
    if percent is None:
        await update.message.reply_text(
            f"Please send a number above 0 and up to {MAX_PERCENT}, like 20 or 12.5."
        )
        return PERCENT

    left, top, right, bottom = padding(ud["size"], ud["selected"], percent)
    new_w = ud["size"][0] + left + right
    new_h = ud["size"][1] + top + bottom
    if new_w * new_h > MAX_OUTPUT_PIXELS:
        await update.message.reply_text(
            f"That would give {new_w}×{new_h} px, above my "
            f"{MAX_OUTPUT_PIXELS // 1_000_000} MP limit. Try a smaller percentage."
        )
        return PERCENT

    ud["percent"] = percent
    await update.message.reply_text(
        "Now pick the fill color, or type one:\n"
        "a name (red, navy), a hex code (#ff8800) or rgb(255, 136, 0).",
        reply_markup=color_keyboard(),
    )
    return COLOR


async def color_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        await q.edit_message_reply_markup(None)
    except BadRequest:
        pass
    return await run_extend(update, context, parse_color(q.data.split(":", 1)[1]))


async def color_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    color = parse_color(update.message.text)
    if color is None:
        await update.message.reply_text(
            "I don't know that color. Try a name like red, or a hex code like #ff8800."
        )
        return COLOR
    return await run_extend(update, context, color)


async def run_extend(update: Update, context: ContextTypes.DEFAULT_TYPE, color):
    ud = context.user_data
    chat = update.effective_chat
    await chat.send_action(ChatAction.UPLOAD_DOCUMENT)
    old_size = ud["size"]

    try:
        async with PROCESS_SLOTS:
            result, ext, new_size = await asyncio.to_thread(
                extend_image, ud["image"], ud["selected"], ud["percent"], color
            )
        await chat.send_document(
            document=InputFile(io.BytesIO(result), filename=f"extended.{ext}"),
            caption=(
                f"Done! {old_size[0]}×{old_size[1]} → {new_size[0]}×{new_size[1]} px\n"
                "Send another image to start again."
            ),
        )
    except ValueError:
        await chat.send_message(
            "The result is too large for me to send. Try a smaller percentage."
        )
    except Exception:
        log.exception("Processing failed")
        await chat.send_message("Something went wrong. Please send the image again.")

    ud.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Cancelled. Send me another image any time.")
    return ConversationHandler.END


async def expired_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer(
        "This session expired. Please send the image again.", show_alert=True
    )


async def idle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me an image to get started.")


# -------------------------------------------------------------------- main
def main():
    token = os.environ["BOT_TOKEN"]

    app = (
        Application.builder()
        .token(token)
        .concurrent_updates(True)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(120)
        .build()
    )

    image_filter = filters.PHOTO | filters.Document.ALL
    text_filter = filters.TEXT & ~filters.COMMAND

    conv = ConversationHandler(
        entry_points=[MessageHandler(image_filter, receive_image)],
        states={
            DIRECTIONS: [
                CallbackQueryHandler(choose_direction, pattern=r"^dir:"),
                MessageHandler(text_filter, directions_hint),
            ],
            PERCENT: [MessageHandler(text_filter, receive_percent)],
            COLOR: [
                CallbackQueryHandler(color_button, pattern=r"^col:"),
                MessageHandler(text_filter, color_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(expired_button))
    app.add_handler(MessageHandler(text_filter, idle_text))

    base_url = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if base_url:
        # Webhook mode: lets a free host sleep when idle and wake on messages.
        path = "telegram-webhook"
        app.run_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get("PORT", "10000")),
            url_path=path,
            webhook_url=f"{base_url.rstrip('/')}/{path}",
            secret_token=hashlib.sha256(token.encode()).hexdigest(),
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
