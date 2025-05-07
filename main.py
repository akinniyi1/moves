import os
import re
import ssl
import logging
import yt_dlp
import mimetypes
import requests
import ffmpeg
import json

from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# SSL workaround
ssl._create_default_https_context = ssl._create_unverified_context
logging.basicConfig(level=logging.INFO)

# Env vars
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", 10000))
USER_DB = "users.json"

# Load user data
if os.path.exists(USER_DB):
    with open(USER_DB) as f:
        users = json.load(f)
else:
    users = {}

def save_users():
    with open(USER_DB, 'w') as f:
        json.dump(users, f)

# Initialize app
application = Application.builder().token(BOT_TOKEN).build()

# ------------- Helpers -------------

def is_valid_url(text):
    return re.match(r'https?://', text)

def is_image_url(url):
    try:
        return url.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')) or \
               requests.head(url, timeout=5).headers.get("Content-Type", "").startswith("image/")
    except:
        return False

def convert_to_audio(video_path, audio_path):
    try:
        ffmpeg.input(video_path).output(audio_path, format='mp3').run(overwrite_output=True)
        return True
    except Exception as e:
        logging.error(f"Audio conversion failed: {e}")
        return False

# ------------- Handlers -------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    name = update.effective_user.first_name or "there"

    users.setdefault(user_id, {"plan": "free"})
    save_users()

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 My Profile", callback_data="profile")]
    ])
    await update.message.reply_text(
        f"👋 Hello {name}! Send me any video or photo link and I’ll download it for you.",
        reply_markup=keyboard
    )

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    name = query.from_user.first_name or "User"
    plan = users.get(user_id, {}).get("plan", "free")

    await query.message.reply_text(
        f"👤 Profile for {name}\n\n📌 Plan: {plan.title()}"
    )

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    user = update.effective_user
    name = user.first_name or "friend"

    if not is_valid_url(url):
        await update.message.reply_text("❌ That doesn't look like a valid link.")
        return

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

    status_msg = await update.message.reply_text(f"📥 Hi {name}, starting your video download...")
    video_filename = f"video_{user.id}.mp4"
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
    finally:
        if os.path.exists(video_filename):
            os.remove(video_filename)

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

    os.remove(audio_path)

async def upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text("❌ Usage: /upgrade @username")
        return

    mention = context.args[0]
    if not mention.startswith("@"):
        await update.message.reply_text("❌ Invalid username format.")
        return

    try:
        user = await application.bot.get_chat(mention)
        user_id = str(user.id)
        users[user_id] = {"plan": "premium"}
        save_users()
        await update.message.reply_text(f"✅ {mention} has been upgraded to premium.")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to upgrade user: {e}")

# ------------- Webhook -------------

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
    await application.bot.set_webhook(f"{APP_URL}/webhook")
    logging.info(f"✅ Webhook set to: {APP_URL}/webhook")

async def on_cleanup(app):
    await application.stop()
    await application.shutdown()

web_app.on_startup.append(on_startup)
web_app.on_cleanup.append(on_cleanup)

# ------------- Register -------------

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("upgrade", upgrade))
application.add_handler(CallbackQueryHandler(handle_audio_callback, pattern=r'^convert_audio:'))
application.add_handler(CallbackQueryHandler(show_profile, pattern="^profile$"))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video))

# ------------- Run App -------------

if __name__ == "__main__":
    web.run_app(web_app, port=PORT)
