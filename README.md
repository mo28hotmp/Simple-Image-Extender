# Simple Image Extender

A Telegram bot that extends an image by adding blank space around it. No AI generation: it just fills the new area with a color you choose.

## How it works

1. Send the bot an image, as a photo or as a file. Any format Pillow can open works (JPEG, PNG, WebP, BMP, TIFF, HEIC and more). Animated files use their first frame.
2. Pick one or more directions: top, bottom, left, right.
3. Send a percentage, for example `20` or `12.5`. Left and right use the image width, top and bottom use the height.
4. Pick a fill color with a button, or type a color name, a hex code (`#ff8800`) or `rgb(255, 136, 0)`. Choose "Transparent" for a transparent border (output is PNG). You can also take the color from the picture itself:
   - **Pick a spot**: the bot shows your picture with a 5×5 grid (A1–E5). Tap a cell to use the color at its dot, or type an exact position in % from the top-left, like `40 25` (x then y). The color is averaged over a tiny area around the spot, so single noisy pixels don't matter.
   - `dominant` uses the picture's most common color.
   - `edge` averages the borders on the sides being extended.
5. The bot sends back the extended image as a file, so Telegram doesn't compress it.

Commands: `/start`, `/help`, `/cancel`.

## Limits

- Telegram lets bots download files up to 20 MB.
- The result is capped at 30 megapixels and 50 MB so it fits on a free host.
- Percentages go up to 500.
- JPEG and HEIC inputs are returned as JPEG (quality 95). Everything else is returned as PNG.

## Deploying for free on Render

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. On [render.com](https://render.com), create a new **Blueprint** from this repo. It reads `render.yaml` and sets up a free web service.
3. When asked, set the `BOT_TOKEN` environment variable to your token.
4. Deploy. The bot registers its own webhook on startup, so nothing else is needed.

Render's free web services sleep after 15 minutes without traffic. The first message after a quiet period can take about a minute to get a reply, and a conversation in progress is lost if the service sleeps.

## Configuration

| Variable | Required | Purpose |
|---|---|---|
| `BOT_TOKEN` | yes | Token from BotFather |
| `WEBHOOK_URL` | no | Public base URL, if hosting somewhere other than Render. Render's `RENDER_EXTERNAL_URL` is used automatically. |
| `PORT` | no | Port to listen on (Render sets this) |

If neither `WEBHOOK_URL` nor `RENDER_EXTERNAL_URL` is set, the bot falls back to polling, which is handy for testing on your own machine.

## Files

- `bot.py` is the bot.
- `requirements.txt` lists the Python dependencies.
- `render.yaml` is the Render blueprint.
