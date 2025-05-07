import os
import re
import ssl
import json
import logging
import yt_dlp
import mimetypes
import requests
import ffmpeg
from datetime import datetime, timedelta
from aiohttp import web
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# SSL fix
ssl._create_default_https_context = ssl._create_unverified_context
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", 10000))
ADMIN_ID = 1378825382

USER_DATA_FILE = "users.json"

application = Application.builder().token(BOT_TOKEN).build()

# ---------- Helpers ----------

def load_users():
    if not os.path.exists(USER_DATA_FILE):
        return {}
    with open(USER_DATA_FILE, "r") as f:
        return json.load(f)

def save_users(users):
    with open(USER_DATA_FILE, "w") as f:
        json.dump(users, f)

def get_user_data(user_id):
    users = load_users()
    if str(user_id) not in users:
        users[str(user_id)] = {
            "name": "",
            "plan": "free",
            "downloads_today": 0,
            "last_used": str(datetime.utcnow().date()),
            "expiry": None
        }
        save_users(users)
    return users[str(user_id)]

def update_user(user_id, data):
    users = load_users()
    users[str(user_id)].update(data)
    save_users(users)

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

def check_and_reset_limit(user_id):
    user = get_user_data(user_id)
    today = str(datetime.utcnow().date())
    if user["last_used"] != today:
        update_user(user_id, {"downloads_today": 0, "last_used": today})
        user["downloads_today"] = 0
    return user

# ---------- Handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = user.first_name or "friend"

    user_data = get_user_data(user.id)
    user_data["name"] = name
    update_user(user.id, user_data)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 View Profile", callback_data="profile")],
        [InlineKeyboardButton("📊 Total Users", callback_data="total_users")] if user.id == ADMIN_ID else []
    ])

    await update.message.reply_text(
        f"👋 Hello {name}!\nSend me any video or image link to download it.",
        reply_markup=keyboard
    )

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    data = check_and_reset_limit(user.id)
    expiry = data.get("expiry")

    profile = (
        f"👤 Name: {user.first_name}\n"
        f"📦 Plan: {data['plan'].capitalize()}\n"
        f"📥 Downloads Today: {data['downloads_today']} / {'Unlimited' if data['plan'] != 'free' else 3}"
    )
    if expiry:
        profile += f"\n🕒 Expires: {expiry}"

    await query.edit_message_text(profile)

async def total_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        await query.edit_message_text("❌ Not authorized.")
        return

    users = load_users()
    count = len(users)
    await query.edit_message_text(f"👥 Total users: {count}")

async def upgrade_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        await query.edit_message_text("❌ Not authorized.")
        return

    _, username, days = query.data.split(":")
    users = load_users()

    for uid, udata in users.items():
        if udata.get("name", "").lower() == username.lower():
            expiry_date = datetime.utcnow() + timedelta(days=int(days))
            udata["plan"] = "premium"
            udata["expiry"] = str(expiry_date.date())
            save_users(users)
            await query.edit_message_text(f"✅ Upgraded {username} to {days}-day premium.")
            return

    await query.edit_message_text("❌ Username not found.")

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    user = update.effective_user

    user_data = check_and_reset_limit(user.id)

    if user_data["plan"] == "free" and user_data["downloads_today"] >= 3:
        await update.message.reply_text("🚫 You've reached your 3 daily download limit.")
        return

    # Upgrade expired?
    if user_data["plan"] == "premium":
        expiry = user_data.get("expiry")
        if expiry and datetime.strptime(expiry, "%Y-%m-%d").date() < datetime.utcnow().date():
            update_user(user.id, {"plan": "free", "expiry": None})
            user_data["plan"] = "free"

    if not is_valid_url(url):
        await update.message.reply_text("❌ Invalid link.")
        return

    if is_image_url(url):
        try:
            img_data = requests.get(url).content
            with open("image.jpg", 'wb') as f:
                f.write(img_data)
            with open("image.jpg", 'rb') as f:
                await update.message.reply_photo(photo=f)
            os.remove("image.jpg")
        except Exception as e:
            logging.error(e)
            await update.message.reply_text("❌ Failed to download image.")
        return

    status_msg = await update.message.reply_text("📥 Downloading video...")

    filename = "video.mp4"
    progress_state = {'last_percent': 0}

    def progress_hook(d):
        if d['status'] == 'downloading':
            total = d.get('_total_bytes_estimate') or d.get('total_bytes') or 0
            downloaded = d.get('downloaded_bytes') or 0
            if total:
                percent = int(downloaded * 100 / total)
                if percent - progress_state['last_percent'] >= 10:
                    progress_state['last_percent'] = percent
                    context.application.create_task(
                        status_msg.edit_text(f"⏳ Downloading... {percent}%")
                    )

    ydl_opts = {
        'progress_hooks': [progress_hook],
        'outtmpl': filename,
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'quiet': True,
        'nocheckcertificate': True,
        'http_headers': {'User-Agent': 'Mozilla/5.0'},
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        await status_msg.edit_text("✅ Sending video...")

        with open(filename, 'rb') as f:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🎵 Convert to Audio", callback_data=f"convert_audio:{filename}")]
            ])
            await update.message.reply_video(video=f, caption="🎥 Done!", reply_markup=keyboard)

        # Increment count
        user_data["downloads_today"] += 1
        update_user(user.id, user_data)

    except Exception as e:
        logging.error(e)
        await status_msg.edit_text("❌ Failed to download video.")

async def handle_audio_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not query.data.startswith("convert_audio:"):
        return

    video_path = query.data.split(":", 1)[1]
    audio_path = "audio.mp3"

    if not os.path.exists(video_path):
        await query.edit_message_caption(caption="❌ Video file not found.")
        return

    success = convert_to_audio(video_path, audio_path)
    if not success:
        await query.edit_message_caption(caption="❌ Audio conversion failed.")
        return

    with open(audio_path, 'rb') as f:
        await query.message.reply_audio(audio=f, caption="🎧 Your audio is ready!")

    os.remove(video_path)
    os.remove(audio_path)

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
    await application.bot.set_webhook(f"{APP_URL}/webhook")
    logging.info(f"Webhook set: {APP_URL}/webhook")

async def on_cleanup(app):
    await application.stop()
    await application.shutdown()

web_app.on_startup.append(on_startup)
web_app.on_cleanup.append(on_cleanup)

# ----------- Register Handlers -----------

application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video))
application.add_handler(CallbackQueryHandler(handle_audio_callback))
application.add_handler(CallbackQueryHandler(show_profile, pattern="^profile$"))
application.add_handler(CallbackQueryHandler(total_users, pattern="^total_users$"))
application.add_handler(CallbackQueryHandler(upgrade_user, pattern="^upgrade:"))

# ----------- Run -----------

if __name__ == "__main__":
    web.run_app(web_app, port=PORT)
