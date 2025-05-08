import os
import re
import ssl
import json
import logging
import yt_dlp
import mimetypes
import requests
import ffmpeg
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime, timedelta

from aiohttp import web
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
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
ADMIN_ID = 1378825382

# Firebase initialization
firebase_creds = json.loads(os.getenv("FIREBASE_CREDENTIALS"))
cred = credentials.Certificate(firebase_creds)
firebase_admin.initialize_app(cred)
db = firestore.client()
users_ref = db.collection("users")

application = Application.builder().token(BOT_TOKEN).build()

# ----------- Helpers -----------

def is_valid_url(text):
    return re.match(r'https?://', text)

def is_image_url(url):
    image_extensions = ('.jpg', '.jpeg', '.png', '.gif', '.webp')
    return url.lower().endswith(image_extensions)

def convert_to_audio(video_path, audio_path):
    try:
        ffmpeg.input(video_path).output(audio_path, format='mp3').run(overwrite_output=True)
        return True
    except Exception as e:
        logging.error(f"Audio conversion failed: {e}")
        return False

def get_user(user_id):
    doc = users_ref.document(str(user_id)).get()
    if doc.exists:
        return doc.to_dict()
    return {
        "plan": "free",
        "downloads": {},
        "name": "",
        "expires": None
    }

def update_user(user_id, data):
    user_doc = users_ref.document(str(user_id))
    existing = get_user(user_id)
    existing.update(data)
    user_doc.set(existing)

def can_download(user_id):
    user = get_user(user_id)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    downloads_today = user["downloads"].get(today, 0)

    if user["plan"] == "free":
        return downloads_today < 3
    else:
        expiry = user.get("expires")
        if expiry and datetime.strptime(expiry, "%Y-%m-%d") < datetime.utcnow():
            update_user(user_id, {"plan": "free", "expires": None})
            return downloads_today < 3
        return True

def log_download(user_id):
    user = get_user(user_id)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    user["downloads"][today] = user["downloads"].get(today, 0) + 1
    update_user(user_id, {"downloads": user["downloads"]})

# ----------- Handlers -----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    update_user(user.id, {"name": user.first_name or ""})

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 View Profile", callback_data="profile")],
        [InlineKeyboardButton("👥 Total Users", callback_data="total_users")] if user.id == ADMIN_ID else []
    ])

    await update.message.reply_text(
        f"👋 Hello {user.first_name or 'there'}! Send me a video link to download.\n\n"
        "🎵 After download, you can convert it to audio.\n"
        "🧾 You can also check your plan via 'View Profile'.",
        reply_markup=keyboard
    )

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    user = update.effective_user

    if not is_valid_url(url):
        await update.message.reply_text("❌ That doesn't look like a valid link.")
        return

    if not can_download(user.id):
        await update.message.reply_text("⛔ You've reached your daily limit for downloads.")
        return

    status_msg = await update.message.reply_text("📥 Downloading video...")

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

        log_download(user.id)
        await status_msg.edit_text("✅ Download complete.")

        with open(video_filename, 'rb') as f:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🎵 Convert to Audio", callback_data=f"convert_audio:{video_filename}")]
            ])
            await update.message.reply_video(f, caption="🎉 Here's your video!", reply_markup=keyboard)

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
        await query.edit_message_caption("❌ Video file not found.")
        return

    success = convert_to_audio(video_path, audio_path)
    if not success:
        await query.edit_message_caption("❌ Audio conversion failed.")
        return

    with open(audio_path, 'rb') as f:
        await query.message.reply_audio(f, caption="🎧 Here is the audio version!")

    os.remove(video_path)
    os.remove(audio_path)

async def handle_inline_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    await query.answer()

    if data == "profile":
        user = get_user(user_id)
        plan = user["plan"]
        expires = user.get("expires")
        expiry_text = f"\n⏳ Expires: {expires}" if expires else ""
        await query.message.reply_text(
            f"👤 Profile for {user.get('name', '')}\n"
            f"💼 Plan: {plan}{expiry_text}"
        )

    elif data == "total_users" and user_id == ADMIN_ID:
        total = len(list(users_ref.stream()))
        await query.message.reply_text(f"👥 Total users: {total}")

    elif data.startswith("upgrade:"):
        _, username, days = data.split(":")
        days = int(days)
        for doc in users_ref.stream():
            u = doc.to_dict()
            if u.get("name", "").lower() == username.lower():
                expiry = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d")
                users_ref.document(doc.id).update({"plan": "paid", "expires": expiry})
                await query.message.reply_text(f"✅ {username} upgraded for {days} days.")
                return
        await query.message.reply_text("❌ User not found.")

async def upgrade_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Not authorized.")
        return

    if len(context.args) != 1:
        await update.message.reply_text("Usage: /upgrade <username>")
        return

    username = context.args[0]
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("5 Days", callback_data=f"upgrade:{username}:5"),
            InlineKeyboardButton("10 Days", callback_data=f"upgrade:{username}:10"),
            InlineKeyboardButton("30 Days", callback_data=f"upgrade:{username}:30")
        ]
    ])
    await update.message.reply_text(f"Select upgrade duration for {username}:", reply_markup=keyboard)

# ----------- Webhook Setup -----------

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
application.add_handler(CommandHandler("upgrade", upgrade_user))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video))
application.add_handler(CallbackQueryHandler(handle_audio_callback, pattern="^convert_audio:"))
application.add_handler(CallbackQueryHandler(handle_inline_buttons))

# ----------- Run -----------

if __name__ == "__main__":
    web.run_app(web_app, port=PORT)
