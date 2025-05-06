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

application = Application.builder().token(BOT_TOKEN).build()


def is_valid_url(text):
    return re.match(r'https?://', text)


# /start handler
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "there"
    await update.message.reply_text(f"👋 Hello {name}! Send me any video link and I’ll download it for you.")


# Main download handler
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    user = update.effective_user
    name = user.first_name or "friend"

    if not is_valid_url(url):
        await update.message.reply_text("❌ That doesn't look like a valid video link.")
        return

    # Send initial message
    status_msg = await update.message.reply_text(f"📥 Hi {name}, starting your download...")

    filename = "video.mp4"

    # Track last percentage to avoid spam
    progress_state = {'last_percent': 0}

    def progress_hook(d):
        if d['status'] == 'downloading':
            total = d.get('_total_bytes_estimate') or d.get('total_bytes') or 0
            downloaded = d.get('downloaded_bytes') or 0
            if total > 0:
                percent = int(downloaded * 100 / total)
                if percent - progress_state['last_percent'] >= 10:
                    progress_state['last_percent'] = percent
                    # Send progress update
                    context.application.create_task(
                        status_msg.edit_text(f"📦 Downloading... {percent}%")
                    )

    ydl_opts = {
        'progress_hooks': [progress_hook],
        'outtmpl': filename,
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'quiet': True,
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

        # Final progress update
        await status_msg.edit_text("✅ Download complete. Sending video...")

        with open(filename, 'rb') as f:
            await update.message.reply_video(video=f, caption="🎉 Here is your video!")

        os.remove(filename)

    except Exception as e:
        logging.error(f"Download failed: {e}")
        await status_msg.edit_text("❌ Failed to download this video.")


# Register handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video))

# aiohttp app
web_app = web.Application()


# Webhook route
async def webhook_handler(request):
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.update_queue.put(update)
    except Exception as e:
        logging.error(f"Webhook error: {e}")
    return web.Response(text="ok")


web_app.router.add_post("/webhook", webhook_handler)


# Startup and shutdown logic
async def on_startup(app):
    await application.initialize()
    await application.start()
    webhook_url = f"{APP_URL}/webhook"
    await application.bot.set_webhook(webhook_url)
    logging.info(f"✅ Webhook set: {webhook_url}")


async def on_cleanup(app):
    await application.stop()
    await application.shutdown()


web_app.on_startup.append(on_startup)
web_app.on_cleanup.append(on_cleanup)


# Run aiohttp on Render (port 10000)
if __name__ == "__main__":
    web.run_app(web_app, port=10000)
