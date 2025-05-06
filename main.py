import os
import re
import ssl
import logging
import yt_dlp
import mimetypes
import requests
import ffmpeg

from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# Bypass SSL verification (fixes yt-dlp cert errors)
ssl._create_default_https_context = ssl._create_unverified_context

# Logging setup
logging.basicConfig(level=logging.INFO)

# Environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("RENDER_EXTERNAL_URL")  # Required for webhook
PORT = int(os.getenv("PORT", 10000))

application = Application.builder().token(BOT_TOKEN).build()

# ----------- Helpers -----------

def is_valid_url(text):
    return re.match(r'https?://', text)

def is_image_url(url):
    image_extensions = ('.jpg', '.jpeg', '.png', '.gif', '.webp')
    if url.lower().endswith(image_extensions):
        return True
    try:
        head = requests.head(url, timeout=5)
        content_type = head.headers.get("Content-Type", "")
        return content_type.startswith("image/")
    except:
        return False

def convert_to_audio(video_path, audio_path):
    try:
        ffmpeg.input(video_path).output(audio_path, format='mp3').run(overwrite_output=True)
        return True
    except Exception as e:
        logging.error(f"Audio conversion failed: {e}")
        return False

# ----------- Handlers -----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "there"
    await update.message.reply_text(f"👋 Hello {name}! Send me any video or photo link and I’ll download it for you.")

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    user = update.effective_user
    name = user.first_name or "friend"

    if not is_valid_url(url):
        await update.message.reply_text("❌ That doesn't look like a valid link.")
        return

    # --- Handle image ---
    if is_image_url(url):
        try:
            img_data = requests.get(url).content
            with open("image.jpg", 'wb') as f:
                f.write(img_data)
            with open("image.jpg", 'rb') as f:
                await update.message.reply_photo(photo=f, caption="🖼️ Here's the image!")
            os.remove("image.jpg")
        except Exception as e:
            logging.error(f"Image download failed: {e}")
            await update.message.reply_text("❌ Failed to download the image.")
        return

    # --- Handle video ---
    status_msg = await update.message.reply_text(f"📥 Hi {name}, starting your video download...")

    video_filename = "video.mp4"
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
        'outtmpl': video_filename,
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'quiet': True,
        'geo_bypass': True,
        'nocheckcertificate': True,
        'http_headers': {'User-Agent': 'Mozilla/5.0'},
        'postprocessors': [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4'
        }]
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        await status_msg.edit_text("✅ Download complete. Sending video...")

        with open(video_filename, 'rb') as f:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🎵 Convert to Audio", callback_data=f"convert_audio:{video_filename}")]
            ])
            await update.message.reply_video(video=f, caption="🎉 Here's your video!", reply_markup=keyboard)

    except Exception as e:
        logging.error(f"Download failed: {e}")
        await status_msg.edit_text("❌ Failed to download this video.")

async def handle_audio_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not query.data.startswith("convert_audio:"):
        return

    video_path = query.data.split(":", 1)[1]
    audio_path = "audio.mp3"

    if not os.path.exists(video_path):
        await query.edit_message_caption(caption="❌ Video file not found for conversion.")
        return

    success = convert_to_audio(video_path, audio_path)
    if not success:
        await query.edit_message_caption(caption="❌ Failed to convert to audio.")
        return

    with open(audio_path, 'rb') as f:
        await query.message.reply_audio(audio=f, caption="🎧 Here is the audio version!")

    os.remove(video_path)
    os.remove(audio_path)

# ----------- Webhook -----------

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

# ----------- Register Handlers -----------

application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video))
application.add_handler(CallbackQueryHandler(handle_audio_callback))

# ----------- Run -----------

if __name__ == "__main__":
    web.run_app(web_app, port=PORT)
