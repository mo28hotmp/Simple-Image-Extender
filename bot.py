"""Simple Image Extender - Telegram bot.

Send an image, pick directions, a percentage and a fill colour; the bot
returns the image extended with that colour (no generation, just fill).
"""

import asyncio
import hashlib
import io
import logging
import os

from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageOps
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

DIRECTIONS, PERCENT, COLOR, PICK, SAMPLE = range(5)
GRID_COLS, GRID_ROWS = 5, 5
GRID_LETTERS = "ABCDEFGH"
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
    """Return an (r, g, b, a) tuple, "dominant"/"edge", or None."""
    t = text.strip().lower()
    if t in ("transparent", "none", "clear"):
        return (0, 0, 0, 0)
    if t in ("dominant", "edge", "edges"):
        return "edge" if t.startswith("edge") else t
    if t and len(t) in (3, 4, 6, 8) and all(c in "0123456789abcdef" for c in t):
        t = "#" + t
    try:
        c = ImageColor.getrgb(t)
    except ValueError:
        return None
    return c if len(c) == 4 else (*c, 255)


def parse_point(text):
    """'40 25' or '40, 25' (percent from the top-left) -> ("spot", fx, fy)."""
    parts = text.replace("%", " ").replace(",", " ").replace(";", " ").split()
    if len(parts) != 2:
        return None
    try:
        x, y = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if not (0 <= x <= 100 and 0 <= y <= 100):
        return None
    return ("spot", x / 100, y / 100)


def load_oriented(data):
    """Open image bytes, apply EXIF rotation. Returns (image, source format)."""
    with Image.open(io.BytesIO(data)) as im:
        src_format = im.format
        im.load()  # first frame only for animated files
        img = ImageOps.exif_transpose(im)
    return img, src_format


def spot_color(img, fx, fy):
    """Average color of a tiny area (about 2% of the image) around a point."""
    rgb = img.convert("RGB")
    w, h = rgb.size
    cx = min(w - 1, max(0, round(fx * (w - 1))))
    cy = min(h - 1, max(0, round(fy * (h - 1))))
    r = max(1, round(min(w, h) * 0.01))
    box = (max(0, cx - r), max(0, cy - r), min(w, cx + r + 1), min(h, cy + r + 1))
    px = rgb.crop(box).resize((1, 1), Image.BOX).getpixel((0, 0))
    return (*px, 255)


def grid_preview(data):
    """JPEG of the picture with a labelled grid; dots mark the sampled spots."""
    img, _ = load_oriented(data)
    img = img.convert("RGB")
    img.thumbnail((1000, 1000))
    w, h = img.size
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=max(14, min(w, h) // 20))

    for i in range(1, GRID_COLS):
        x = round(w * i / GRID_COLS)
        draw.line([(x, 0), (x, h)], fill="black", width=3)
        draw.line([(x, 0), (x, h)], fill="white", width=1)
    for j in range(1, GRID_ROWS):
        y = round(h * j / GRID_ROWS)
        draw.line([(0, y), (w, y)], fill="black", width=3)
        draw.line([(0, y), (w, y)], fill="white", width=1)

    dot = max(4, min(w, h) // 100)
    for r in range(GRID_ROWS):
        for c in range(GRID_COLS):
            x0, y0 = w * c / GRID_COLS, h * r / GRID_ROWS
            draw.text(
                (x0 + 6, y0 + 4),
                f"{GRID_LETTERS[c]}{r + 1}",
                font=font,
                fill="white",
                stroke_width=2,
                stroke_fill="black",
            )
            cx = w * (c + 0.5) / GRID_COLS
            cy = h * (r + 0.5) / GRID_ROWS
            draw.ellipse(
                (cx - dot, cy - dot, cx + dot, cy + dot),
                fill="white",
                outline="black",
                width=2,
            )

    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()


def average_color(data):
    """Average color of a whole (sample) image; transparent pixels are ignored."""
    img, _ = load_oriented(data)
    if "A" in img.getbands() or "transparency" in img.info:
        small = img.convert("RGBA")
        small.thumbnail((64, 64), Image.BOX)
        total = r = g = b = 0
        for pr, pg, pb, pa in small.getdata():
            total += pa
            r += pr * pa
            g += pg * pa
            b += pb * pa
        if total == 0:
            raise ValueError("fully transparent")
        return (round(r / total), round(g / total), round(b / total), 255)
    px = img.convert("RGB").resize((1, 1), Image.BOX).getpixel((0, 0))
    return (*px, 255)


def dominant_color(img):
    """Most common color of the picture (after reducing it to 8 colors)."""
    small = img.convert("RGB")
    small.thumbnail((128, 128))
    q = small.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
    _, index = max(q.getcolors())
    palette = q.getpalette()
    return (*palette[index * 3 : index * 3 + 3], 255)


def edge_color(img, selected):
    """Average color of the border strips on the sides being extended."""
    rgb = img.convert("RGB")
    w, h = rgb.size
    tx, ty = max(1, round(w * 0.02)), max(1, round(h * 0.02))
    strips = {
        "top": (0, 0, w, ty),
        "bottom": (0, h - ty, w, h),
        "left": (0, 0, tx, h),
        "right": (w - tx, 0, w, h),
    }
    samples = [
        rgb.crop(box).resize((1, 1), Image.BOX).getpixel((0, 0))
        for side, box in strips.items()
        if side in selected
    ]
    avg = tuple(round(sum(c[i] for c in samples) / len(samples)) for i in range(3))
    return (*avg, 255)


def extend_image(data, selected, percent, color):
    """Blocking work: returns (bytes, extension, (width, height), color)."""
    img, src_format = load_oriented(data)

    if color == "dominant":
        color = dominant_color(img)
    elif color == "edge":
        color = edge_color(img, selected)
    elif color[0] == "spot":
        color = spot_color(img, color[1], color[2])

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
    return out.getvalue(), ext, new_size, color


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


def pick_keyboard():
    rows = [
        [
            InlineKeyboardButton(
                f"{GRID_LETTERS[c]}{r + 1}", callback_data=f"pick:{c},{r}"
            )
            for c in range(GRID_COLS)
        ]
        for r in range(GRID_ROWS)
    ]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="pick:back")])
    return InlineKeyboardMarkup(rows)


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
            [
                InlineKeyboardButton(
                    "📍 Pick a spot on the picture", callback_data="col:pick"
                )
            ],
            [
                InlineKeyboardButton(
                    "📎 Send a part of the picture", callback_data="col:sample"
                )
            ],
            [
                InlineKeyboardButton("🎨 Dominant", callback_data="col:dominant"),
                InlineKeyboardButton("🖼 Edges", callback_data="col:edge"),
            ],
        ]
    )


# ---------------------------------------------------------------- handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)


async def download_attachment(msg):
    item = msg.photo[-1] if msg.photo else msg.document
    tg_file = await item.get_file()
    return bytes(await tg_file.download_as_bytearray())


async def receive_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # While the bot waits for a sample image, a new image is that sample.
    if context.user_data.get("awaiting_sample"):
        return await receive_sample(update, context)

    msg = update.message
    await msg.chat.send_action(ChatAction.TYPING)

    try:
        data = await download_attachment(msg)
    except BadRequest:
        await msg.reply_text(
            "I can't download that file - Telegram only lets bots read files up to 20 MB."
        )
        return ConversationHandler.END

    try:
        size = await asyncio.to_thread(lambda: load_oriented(data)[0].size)
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


async def ask_color(chat):
    await chat.send_message(
        "Now pick the fill color, or type one:\n"
        "a name (red, navy), a hex code (#ff8800) or rgb(255, 136, 0).\n\n"
        "Or take it from the picture: 📍 pick a spot on a grid, 📎 send me a "
        "small part of it to average, use its dominant color, or the average "
        "of the edges you're extending.",
        reply_markup=color_keyboard(),
    )


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
    await ask_color(update.effective_chat)
    return COLOR


async def color_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        await q.edit_message_reply_markup(None)
    except BadRequest:
        pass
    choice = q.data.split(":", 1)[1]
    if choice == "pick":
        return await show_picker(update, context)
    if choice == "sample":
        return await ask_sample(update, context)
    return await run_extend(update, context, parse_color(choice))


async def show_picker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await chat.send_action(ChatAction.UPLOAD_PHOTO)
    async with PROCESS_SLOTS:
        preview = await asyncio.to_thread(grid_preview, context.user_data["image"])
    await chat.send_photo(
        photo=InputFile(io.BytesIO(preview), filename="grid.jpg"),
        caption=(
            "Tap a cell to use the color at its dot.\n"
            "For an exact spot, type its position in % from the top-left, "
            "like 40 25 (x then y)."
        ),
        reply_markup=pick_keyboard(),
    )
    return PICK


async def ask_sample(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_sample"] = True
    await update.effective_chat.send_message(
        "Send me an image of just the part whose color you want, for example a "
        "small crop of the sky. I'll average all its colors and use that as the "
        "fill.\n\nTip: send it as a file to avoid compression.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back", callback_data="sample:back")]]
        ),
    )
    return SAMPLE


async def sample_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data.pop("awaiting_sample", None)
    try:
        await q.edit_message_reply_markup(None)
    except BadRequest:
        pass
    await ask_color(update.effective_chat)
    return COLOR


async def sample_hint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Please send an image, or tap Back above.")
    return SAMPLE


async def receive_sample(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    await msg.chat.send_action(ChatAction.TYPING)
    try:
        data = await download_attachment(msg)
    except BadRequest:
        await msg.reply_text(
            "I can't download that file - Telegram only lets bots read files up to 20 MB."
        )
        return SAMPLE
    try:
        async with PROCESS_SLOTS:
            color = await asyncio.to_thread(average_color, data)
    except Exception:
        await msg.reply_text(
            "I couldn't read a color from that. Send another image, or tap Back."
        )
        return SAMPLE
    context.user_data.pop("awaiting_sample", None)
    return await run_extend(update, context, color)


async def pick_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        await q.edit_message_reply_markup(None)
    except BadRequest:
        pass
    arg = q.data.split(":", 1)[1]
    if arg == "back":
        await ask_color(update.effective_chat)
        return COLOR
    c, r = (int(v) for v in arg.split(","))
    spot = ("spot", (c + 0.5) / GRID_COLS, (r + 0.5) / GRID_ROWS)
    return await run_extend(update, context, spot)


async def pick_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    spot = parse_point(update.message.text)
    if spot is None:
        await update.message.reply_text(
            "Send two numbers from 0 to 100, like 40 25 (x then y, in % from the "
            "top-left), or tap a cell above."
        )
        return PICK
    return await run_extend(update, context, spot)


async def color_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    color = parse_color(update.message.text)
    if color is None:
        await update.message.reply_text(
            "I don't know that color. Try a name like red, a hex code like #ff8800, "
            "or \"dominant\" / \"edges\"."
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
            result, ext, new_size, used = await asyncio.to_thread(
                extend_image, ud["image"], ud["selected"], ud["percent"], color
            )
        fill_label = (
            "transparent"
            if used[3] == 0
            else "#{:02x}{:02x}{:02x}".format(*used[:3])
        )
        await chat.send_document(
            document=InputFile(io.BytesIO(result), filename=f"extended.{ext}"),
            caption=(
                f"Done! {old_size[0]}×{old_size[1]} → {new_size[0]}×{new_size[1]} px\n"
                f"Fill: {fill_label}\n"
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
            PICK: [
                CallbackQueryHandler(pick_button, pattern=r"^pick:"),
                MessageHandler(text_filter, pick_text),
            ],
            SAMPLE: [
                CallbackQueryHandler(sample_back, pattern=r"^sample:"),
                MessageHandler(image_filter, receive_sample),
                MessageHandler(text_filter, sample_hint),
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
