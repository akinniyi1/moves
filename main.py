import os
import re
import ssl
import json
import logging
import yt_dlp
import requests
import ffmpeg
from datetime import datetime, timedelta

from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# --- SSL Fix for yt-dlp ---
ssl._create_default_https_context = ssl._create_unverified_context

# --- Logging ---
logging.basicConfig(level=logging.INFO)

# --- Environment ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", 10000))

# --- User Data File ---
USER_FILE = "users.json"
ADMIN_ID = 1378825382  # Replace with your actual Telegram ID

application = Application.builder().token(BOT_TOKEN).build()

# ========== HELPERS ==========

def is_valid_url(text):
    return re.match(r'https?://', text)

def is_image_url(url):
    image_ext = ('.jpg', '.jpeg', '.png', '.gif', '.webp')
    if url.lower().endswith(image_ext):
        return True
    try:
        head = requests.head(url, timeout=5)
        return head.headers.get("Content-Type", "").startswith("image/")
    except:
        return False

def convert_to_audio(video_path, audio_path):
    try:
        ffmpeg.input(video_path).output(audio_path, format='mp3').run(overwrite_output=True)
        return True
    except Exception as e:
        logging.error(f"Audio conversion failed: {e}")
        return False

def load_users():
    if not os.path.exists(USER_FILE):
        return {}
    with open(USER_FILE, "r") as f:
        return json.load(f)

def save_users(users):
    with open(USER_FILE, "w") as f:
        json.dump(users, f, indent=2)

def get_user_data(user_id, name):
    users = load_users()
    uid = str(user_id)
    if uid not in users:
        users[uid] = {
            "name": name,
            "downloads_today": 0,
            "last_download_date": None,
            "premium_until": None
        }
        save_users(users)
    return users[uid]

def update_user_data(user_id, data):
    users = load_users()
    users[str(user_id)] = data
    save_users(users)

def is_premium(user_data):
    premium_until = user_data.get("premium_until")
    if not premium_until:
        return False
    return datetime.strptime(premium_until, "%Y-%m-%d") >= datetime.utcnow()

def can_download(user_data):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if is_premium(user_data):
        return True
    if user_data["last_download_date"] != today:
        user_data["downloads_today"] = 0
        user_data["last_download_date"] = today
    return user_data["downloads_today"] < 3

def increment_download(user_data):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if user_data["last_download_date"] != today:
        user_data["downloads_today"] = 1
        user_data["last_download_date"] = today
    else:
        user_data["downloads_today"] += 1

# ========== HANDLERS ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "there"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 View Profile", callback_data="view_profile")]
    ])
    await update.message.reply_text(
        f"👋 Hello {name}! Send me any video or image link and I’ll download it for you.",
        reply_markup=keyboard
    )

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = get_user_data(user.id, user.first_name)
    plan = "Premium ✅" if is_premium(data) else "Free 🆓"
    downloads = data.get("downloads_today", 0)
    await update.message.reply_text(
        f"👤 Name: {data['name']}\n📋 Plan: {plan}\n📥 Downloads Today: {downloads}/3" if plan == "Free 🆓" else f"👤 Name: {data['name']}\n📋 Plan: {plan}"
    )

async def handle_inline_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "view_profile":
        user = query.from_user
        data = get_user_data(user.id, user.first_name)
        plan = "Premium ✅" if is_premium(data) else "Free 🆓"
        downloads = data.get("downloads_today", 0)
        message = (
            f"👤 Name: {data['name']}\n📋 Plan: {plan}\n📥 Downloads Today: {downloads}/3"
            if plan == "Free 🆓" else
            f"👤 Name: {data['name']}\n📋 Plan: {plan}"
        )
        await query.edit_message_text(message)

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return await update.message.reply_text("❌ You're not authorized.")
    users = load_users()
    await update.message.reply_text(f"👥 Total users: {len(users)}")

async def upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return await update.message.reply_text("❌ Not allowed.")
    if len(context.args) != 1:
        return await update.message.reply_text("Usage: /upgrade @username")
    
    username = context.args[0].lstrip("@").lower()
    users = load_users()
    for uid, data in users.items():
        if data['name'].lower() == username:
            data['premium_until'] = (datetime.utcnow() + timedelta(days=40)).strftime("%Y-%m-%d")
            save_users(users)
            return await update.message.reply_text(f"✅ Upgraded @{username} to premium for 40 days.")
    await update.message.reply_text("❌ User not found.")

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    user = update.effective_user
    name = user.first_name or "friend"

    user_data = get_user_data(user.id, name)

    if not can_download(user_data):
        await update.message.reply_text("⚠️ You’ve reached your daily limit of 3 downloads. Come back tomorrow.")
        return

    if not is_valid_url(url):
        await update.message.reply_text("❌ That doesn't look like a valid link.")
        return

    if is_image_url(url):
        try:
            img = requests.get(url).content
            with open("img.jpg", 'wb') as f:
                f.write(img)
            with open("img.jpg", 'rb') as f:
                await update.message.reply_photo(photo=f, caption="🖼️ Here's your image!")
            os.remove("img.jpg")
        except Exception as e:
            logging.error(f"Image error: {e}")
            await update.message.reply_text("❌ Failed to download image.")
        return

    status_msg = await update.message.reply_text(f"📥 Hi {name}, downloading...")

    filename = "video.mp4"
    progress_state = {'last_percent': 0}

    def hook(d):
        if d['status'] == 'downloading':
            total = d.get('total_bytes', 0)
            done = d.get('downloaded_bytes', 0)
            if total > 0:
                percent = int(done * 100 / total)
                if percent - progress_state['last_percent'] >= 10:
                    progress_state['last_percent'] = percent
                    context.application.create_task(
                        status_msg.edit_text(f"📦 Downloading... {percent}%")
                    )

    ydl_opts = {
        'progress_hooks': [hook],
        'outtmpl': filename,
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'quiet': True,
        'nocheckcertificate': True,
        'geo_bypass': True,
        'http_headers': {'User-Agent': 'Mozilla/5.0'},
        'postprocessors': [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4'
        }]
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        await status_msg.edit_text("✅ Done! Sending video...")

        with open(filename, 'rb') as f:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🎵 Convert to Audio", callback_data=f"audio:{filename}")]
            ])
            await update.message.reply_video(video=f, caption="🎉 Here's your video!", reply_markup=keyboard)

        increment_download(user_data)
        update_user_data(user.id, user_data)

    except Exception as e:
        logging.error(f"Download failed: {e}")
        await status_msg.edit_text("❌ Failed to download video.")

async def handle_audio_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not query.data.startswith("audio:"):
        return

    video_path = query.data.split(":", 1)[1]
    audio_path = "audio.mp3"

    if not os.path.exists(video_path):
        await query.edit_message_caption("❌ Video file not found.")
        return

    if convert_to_audio(video_path, audio_path):
        with open(audio_path, 'rb') as f:
            await query.message.reply_audio(audio=f, caption="🎧 Here's the audio version!")
        os.remove(audio_path)
    else:
        await query.edit_message_caption("❌ Audio conversion failed.")

    os.remove(video_path)

# ========== WEBHOOK ==========

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
    logging.info(f"✅ Webhook set to {APP_URL}/webhook")

async def on_cleanup(app):
    await application.stop()
    await application.shutdown()

web_app.on_startup.append(on_startup)
web_app.on_cleanup.append(on_cleanup)

# ========== COMMANDS & CALLBACKS ==========

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("profile", profile))
application.add_handler(CommandHandler("admin", admin))
application.add_handler(CommandHandler("upgrade", upgrade))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video))
application.add_handler(CallbackQueryHandler(handle_inline_buttons))
application.add_handler(CallbackQueryHandler(handle_audio_callback))

# ========== RUN ==========

if __name__ == "__main__":
    web.run_app(web_app, port=PORT)
