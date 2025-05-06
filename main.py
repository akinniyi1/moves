import os
import re
import logging
import ssl
import yt_dlp
from aiohttp import web
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# SSL bypass for yt-dlp
ssl._create_default_https_context = ssl._create_unverified_context

# Logging
logging.basicConfig(level=logging.INFO)

# Load from env or use placeholders
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("RENDER_EXTERNAL_URL")  # Render auto-injects this

# Check URL validity
def is_valid_url(text):
    return re.match(r'https?://', text)

# Start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Send any video link (YouTube, TikTok, Facebook, etc.)")

# Download and reply with video
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if not is_valid_url(url):
        await update.message.reply_text("❌ That doesn't look like a valid video link.")
        return

    await update.message.reply_text("📥 Downloading video...")

    ydl_opts = {
        'quiet': True,
        'outtmpl': 'video.%(ext)s',
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'geo_bypass': True,
        'nocheckcertificate': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
        },
        'postprocessors': [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4'
        }]
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            file_path = ydl.prepare_filename(info)

        with open(file_path, 'rb') as f:
            await update.message.reply_video(video=f, caption="✅ Here's your video!")

        os.remove(file_path)

    except Exception as e:
        logging.error(f"Failed to download: {e}")
        await update.message.reply_text("❌ Failed to download. The link may be unsupported or blocked.")

# Telegram Webhook handler
async def telegram_webhook(request):
    data = await request.json()
    update = Update.de_json(data, app.bot)
    await app.process_update(update)
    return web.Response()

# Create app and register webhook
app = Application.builder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video))

# Aiohttp web server setup
web_app = web.Application()
web_app.router.add_post("/webhook", telegram_webhook)

async def on_startup(app_):
    webhook_url = f"{APP_URL}/webhook"
    await app.bot.set_webhook(webhook_url)
    print(f"🔗 Webhook set to: {webhook_url}")

web_app.on_startup.append(on_startup)

# Run aiohttp server on port 10000
if __name__ == "__main__":
    web.run_app(web_app, port=10000)
