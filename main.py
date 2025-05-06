import os
import re
import ssl
import logging
import yt_dlp
from aiohttp import web
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# Bypass SSL verification (fixes yt-dlp cert errors)
ssl._create_default_https_context = ssl._create_unverified_context

# Logging setup
logging.basicConfig(level=logging.INFO)

# Environment vars
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("RENDER_EXTERNAL_URL")

# Create app object
application = Application.builder().token(BOT_TOKEN).build()

# Simple URL validator
def is_valid_url(text):
    return re.match(r'https?://', text)

# /start handler
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Send me any video link and I'll download it for you!")

# Video handler
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
            filename = ydl.prepare_filename(info)

        with open(filename, 'rb') as f:
            await update.message.reply_video(video=f, caption="✅ Done!")

        os.remove(filename)

    except Exception as e:
        logging.error(f"Download failed: {e}")
        await update.message.reply_text("❌ Could not download this video.")

# Register handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video))

# aiohttp server
web_app = web.Application()

# Route POST /webhook
async def webhook_handler(request):
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.update_queue.put(update)
    except Exception as e:
        logging.error(f"Webhook error: {e}")
    return web.Response(text="ok")

web_app.router.add_post("/webhook", webhook_handler)

# Start-up logic: set webhook and initialize app
async def on_startup(app):
    await application.initialize()
    await application.start()
    webhook_url = f"{APP_URL}/webhook"
    await application.bot.set_webhook(webhook_url)
    logging.info(f"✅ Webhook set: {webhook_url}")

# Shutdown logic
async def on_cleanup(app):
    await application.stop()
    await application.shutdown()

web_app.on_startup.append(on_startup)
web_app.on_cleanup.append(on_cleanup)

# Run aiohttp app on port 10000
if __name__ == "__main__":
    web.run_app(web_app, port=10000)
