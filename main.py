import os
import re
import ssl
import logging
import yt_dlp
from aiohttp import web
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# Bypass SSL verification
ssl._create_default_https_context = ssl._create_unverified_context

# Logging
logging.basicConfig(level=logging.INFO)

# Load env vars
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.environ.get("PORT", 10000))

application = Application.builder().token(BOT_TOKEN).build()

# URL validator
def is_valid_url(text):
    return re.match(r'https?://', text)

def is_photo_link(url):
    return any(url.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"])

# /start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "there"
    await update.message.reply_text(f"👋 Hello {name}! Send me any video or photo link and I’ll download it for you.")

# Inline button for audio conversion
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    url = query.data.replace("audio::", "")
    filename = "audio.mp3"

    await query.edit_message_caption(caption="🎧 Converting to audio...")

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': filename,
        'quiet': True,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192'
        }]
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        with open(filename, 'rb') as f:
            await query.message.reply_audio(audio=f, caption="✅ Here is your audio!")

        os.remove(filename)

    except Exception as e:
        logging.error(f"Audio conversion failed: {e}")
        await query.message.reply_text("❌ Failed to convert to audio.")

# Handle all links
async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    name = update.effective_user.first_name or "friend"

    if not is_valid_url(url):
        await update.message.reply_text("❌ That doesn't look like a valid link.")
        return

    if is_photo_link(url):
        try:
            await update.message.reply_photo(photo=url, caption="🖼️ Here is your image!")
        except Exception as e:
            logging.error(f"Photo download failed: {e}")
            await update.message.reply_text("❌ Failed to download this image.")
        return

    # Video download logic
    status_msg = await update.message.reply_text(f"📥 Hi {name}, starting your download...")

    filename = "video.mp4"
    progress_state = {'last_percent': 0}

    def progress_hook(d):
        if d['status'] == 'downloading':
            total = d.get('_total_bytes_estimate') or d.get('total_bytes') or 0
            downloaded = d.get('downloaded_bytes') or 0
            if total > 0:
                percent = int(downloaded * 100 / total)
                if percent - progress_state['last_percent'] >= 10:
                    progress_state['last_percent'] = percent
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
            'User-Agent': 'Mozilla/5.0'
        },
        'postprocessors': [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4'
        }]
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        await status_msg.edit_text("✅ Download complete. Sending video...")

        with open(filename, 'rb') as f:
            keyboard = [[InlineKeyboardButton("🎧 Convert to Audio", callback_data=f"audio::{url}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_video(video=f, caption="🎉 Here is your video!", reply_markup=reply_markup)

        os.remove(filename)

    except Exception as e:
        logging.error(f"Download failed: {e}")
        await status_msg.edit_text("❌ Failed to download this video.")

# Telegram webhook route
web_app = web.Application()

async def webhook_handler(request):
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.update_queue.put(update)
    except Exception as e:
        logging.error(f"Webhook error: {e}")
    return web.Response(text="ok")

web_app.router.add_post("/webhook", webhook_handler)

# Startup/shutdown
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

# Register handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(button_handler))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_media))

# Run app
if __name__ == "__main__":
    web.run_app(web_app, port=PORT)
