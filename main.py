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

ssl._create_default_https_context = ssl._create_unverified_context
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", 10000))
ADMIN_ID = 1378825382
USER_DATA_FILE = "users.json"
application = Application.builder().token(BOT_TOKEN).build()

def load_user_data():
    if not os.path.exists(USER_DATA_FILE):
        return {}
    with open(USER_DATA_FILE, "r") as f:
        return json.load(f)

def save_user_data(data):
    with open(USER_DATA_FILE, "w") as f:
        json.dump(data, f)

def get_user_info(user):
    users = load_user_data()
    user_id = str(user.id)
    if user_id not in users:
        users[user_id] = {
            "first_name": user.first_name,
            "username": user.username,
            "plan": "free",
            "downloads": 0,
            "last_download": datetime.now().strftime("%Y-%m-%d"),
            "expiry": ""
        }
        save_user_data(users)
    return users[user_id]

def update_user_info(user, field, value):
    users = load_user_data()
    user_id = str(user.id)
    if user_id in users:
        users[user_id][field] = value
        save_user_data(users)

def can_download(user):
    info = get_user_info(user)
    today = datetime.now().strftime("%Y-%m-%d")
    if info["plan"] == "free":
        if info["last_download"] != today:
            update_user_info(user, "downloads", 0)
            update_user_info(user, "last_download", today)
        return info["downloads"] < 3
    elif info["plan"] in ["5", "10", "30"]:
        if info["expiry"]:
            expiry_date = datetime.strptime(info["expiry"], "%Y-%m-%d")
            if datetime.now() > expiry_date:
                update_user_info(user, "plan", "free")
                return can_download(user)
        return True
    return False

def increment_download(user):
    info = get_user_info(user)
    info["downloads"] += 1
    update_user_info(user, "downloads", info["downloads"])

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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_user_info(user)
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👤 View Profile", callback_data="profile"),
            InlineKeyboardButton("📊 Total Users", callback_data="total_users")
        ]
    ])
    await update.message.reply_text(
        f"👋 Hello {user.first_name}!\nSend me any video or photo link and I’ll download it for you.",
        reply_markup=keyboard
    )

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    user = update.effective_user
    if not is_valid_url(url):
        await update.message.reply_text("❌ That doesn't look like a valid link.")
        return
    if not can_download(user):
        await update.message.reply_text("🚫 You’ve reached your daily download limit. Upgrade your plan to download more.")
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

    status_msg = await update.message.reply_text("📥 Downloading your video...")
    video_filename = "video.mp4"

    ydl_opts = {
        'format': 'best[height<=480]',
        'outtmpl': video_filename,
        'quiet': True,
        'noplaylist': True
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        increment_download(user)

        video_size = os.path.getsize(video_filename)
        with open(video_filename, 'rb') as f:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🎵 Convert to Audio", callback_data=f"convert_audio:{video_filename}")]
            ])
            if video_size <= 49 * 1024 * 1024:
                await update.message.reply_video(video=f, caption="🎉 Here's your video!", reply_markup=keyboard)
            else:
                await update.message.reply_document(document=f, caption="🎉 Here's your video! (Sent as file because it's over 50MB)", reply_markup=keyboard)

        await status_msg.delete()
        os.remove(video_filename)

    except Exception as e:
        logging.error(f"Download error: {e}")
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
    os.remove(audio_path)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    users = load_user_data()

    if query.data == "profile":
        info = get_user_info(user)
        await query.message.reply_text(
            f"👤 Name: {info['first_name']}\nPlan: {info['plan']}\nDownloads today: {info['downloads']}"
        )

    elif query.data == "total_users" and user.id == ADMIN_ID:
        await query.message.reply_text(f"📊 Total Users: {len(users)}")

async def upgrade_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /upgrade FirstName")
        return
    first_name = args[0]
    users = load_user_data()
    target_id = None
    for uid, data in users.items():
        if data["first_name"] == first_name:
            target_id = uid
            break
    if not target_id:
        await update.message.reply_text("User not found.")
        return
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("5 Days", callback_data=f"upgrade:{target_id}:5"),
            InlineKeyboardButton("10 Days", callback_data=f"upgrade:{target_id}:10"),
            InlineKeyboardButton("30 Days", callback_data=f"upgrade:{target_id}:30")
        ]
    ])
    await update.message.reply_text(f"Select plan for {first_name}", reply_markup=keyboard)

async def upgrade_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not query.data.startswith("upgrade:"):
        return
    _, uid, days = query.data.split(":")
    users = load_user_data()
    if uid in users:
        users[uid]["plan"] = days
        users[uid]["expiry"] = (datetime.now() + timedelta(days=int(days))).strftime("%Y-%m-%d")
        save_user_data(users)
        await query.message.reply_text(f"✅ User upgraded to {days}-day plan.")

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

application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video))
application.add_handler(CommandHandler("upgrade", upgrade_command))
application.add_handler(CallbackQueryHandler(handle_callback))
application.add_handler(CallbackQueryHandler(handle_audio_callback, pattern="^convert_audio:"))
application.add_handler(CallbackQueryHandler(upgrade_callback, pattern="^upgrade:"))

if __name__ == "__main__":
    web.run_app(web_app, port=PORT)
