# --- [IMPORTS & SETUP] ---
import os
import re
import ssl
import json
import logging
import yt_dlp
import ffmpeg
import asyncio
import csv
from PIL import Image
from datetime import datetime, timedelta
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY")
NOWPAYMENTS_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET")
CHANNEL_LINK = "https://t.me/Downloadassaas"
DATA_FILE = "/mnt/data/users.json"

application = Application.builder().token(BOT_TOKEN).build()
file_registry = {}
image_collections = {}
pdf_trials = {}
support_messages = {}

if not os.path.exists("/mnt/data"):
    os.makedirs("/mnt/data")

def load_users():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {}

def save_users(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

users = load_users()

# --- [HELPERS] ---
def is_valid_url(text):
    return re.match(r'https?://', text)

def generate_filename(ext="mp4"):
    return f"file_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}.{ext}"

def is_premium(user):
    if user.get("plan") != "premium":
        return False
    expires = user.get("expires")
    if not isinstance(expires, str):
        return False
    try:
        return datetime.utcnow() < datetime.fromisoformat(expires)
    except:
        return False

def is_banned(username):
    return users.get(username, {}).get("banned", False)

def downgrade_expired_users():
    now = datetime.utcnow()
    for username, user in users.items():
        exp = user.get("expires")
        if isinstance(exp, str):
            try:
                if datetime.fromisoformat(exp) < now:
                    users[username] = {"plan": "free", "downloads": 0}
            except:
                continue
    save_users(users)

async def delete_file_later(path, file_id=None):
    await asyncio.sleep(60)
    if os.path.exists(path):
        os.remove(path)
    if file_id:
        file_registry.pop(file_id, None)

# --- [AUDIO CONVERSION] ---
async def convert_to_audio(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path):
    audio_path = file_path.replace(".mp4", ".mp3")
    try:
        ffmpeg.input(file_path).output(audio_path).run(overwrite_output=True)
        with open(audio_path, 'rb') as f:
            await update.callback_query.message.reply_audio(f, filename=os.path.basename(audio_path))
        os.remove(audio_path)
    except:
        await update.callback_query.message.reply_text("❌ Failed to convert to audio.")

# --- [START HANDLER] ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    downgrade_expired_users()
    user = update.effective_user
    username = user.username
    if not username:
        await update.message.reply_text("❌ You must set a Telegram username in settings.")
        return
    if username not in users:
        users[username] = {"plan": "free", "downloads": 0}
        save_users(users)
    if is_banned(username):
        await update.message.reply_text("⛔ You are banned from using this bot.")
        return
    buttons = [
        [InlineKeyboardButton("👤 View Profile", callback_data="profile"),
         InlineKeyboardButton("🖼️ Convert to PDF", callback_data="convertpdf_btn")],
        [InlineKeyboardButton("🔼 Upgrade Your Plan", callback_data="upgrade_options")],
        [InlineKeyboardButton("📢 Join Our Channel", url=CHANNEL_LINK)]
    ]
    await update.message.reply_text(
        f"👋 Hello @{username}!\n\n"
        "This bot supports downloading videos from:\n"
        "✅ Facebook, TikTok, Twitter, Instagram\n"
        "❌ YouTube is not supported.\n\n"
        "Free Users:\n• 3 video downloads\n• 1 PDF conversion trial\n\n"
        "Send a supported video link to begin.",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# --- [VIDEO HANDLER] ---
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    downgrade_expired_users()
    url = update.message.text.strip()
    if not is_valid_url(url):
        await update.message.reply_text("❌ Invalid URL or unsupported platform.")
        return
    if "youtube.com" in url or "youtu.be" in url:
        await update.message.reply_text("❌ YouTube is not supported.")
        return
    user = update.effective_user
    username = user.username
    if not username or is_banned(username):
        await update.message.reply_text("❌ You must set a username and not be banned to use this bot.")
        return
    user_data = users.get(username, {"plan": "free", "downloads": 0})
    if not is_premium(user_data) and user_data["downloads"] >= 3:
        await update.message.reply_text("⛔ Free users are limited to 3 downloads. Upgrade to continue.")
        return
    filename = generate_filename()
    status_msg = await update.message.reply_text("📥 Downloading...")
    ydl_opts = {
        'outtmpl': filename,
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'quiet': True,
        'noplaylist': True,
        'max_filesize': 50 * 1024 * 1024
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        with open(filename, 'rb') as f:
            sent = await update.message.reply_video(
                f,
                caption="🎉 Here's your video!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎧 Convert to Audio", callback_data=f"audio:{filename}")]])
            )
        file_registry[sent.message_id] = filename
        asyncio.create_task(delete_file_later(filename, sent.message_id))
        await status_msg.delete()
        if not is_premium(user_data):
            user_data["downloads"] += 1
            users[username] = user_data
            save_users(users)
    except:
        await status_msg.edit_text("⚠️ Download failed or file too large.")

# --- [INLINE HANDLER] ---
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user
    username = user.username
    user_data = users.get(username, {"plan": "free"})

    if data == "profile":
        downgrade_expired_users()
        if is_premium(user_data):
            exp = user_data.get("expires")
            try:
                exp_dt = datetime.fromisoformat(exp)
                msg = f"👤 Username: @{username}\n💼 Plan: Premium\n⏰ Expires: {exp_dt.strftime('%Y-%m-%d %H:%M')} UTC"
            except:
                msg = f"👤 Username: @{username}\n💼 Plan: Premium\n⏰ Expires: Unknown"
        else:
            msg = f"👤 Username: @{username}\n💼 Plan: Free"
        await query.message.reply_text(msg)
    elif data == "convertpdf_btn":
        fake_msg = type("msg", (), {"message": query.message, "effective_user": query.from_user})
        await convert_pdf(fake_msg, context, triggered_by_button=True)
    elif data.startswith("audio:"):
        file = data.split("audio:")[1]
        if not os.path.exists(file):
            await query.message.reply_text("File deleted. Please resend the link.")
        else:
            await convert_to_audio(update, context, file)
    elif data == "upgrade_options":
        buttons = [
            [InlineKeyboardButton("1 Month - $2", url="https://nowpayments.io/payment/?amount=2&currency=usd")],
            [InlineKeyboardButton("2 Months - $4", url="https://nowpayments.io/payment/?amount=4&currency=usd")]
        ]
        await query.message.reply_text("Choose a plan to upgrade:", reply_markup=InlineKeyboardMarkup(buttons))

# --- [PDF HANDLER, IMAGE HANDLER, ADMIN COMMANDS, SUPPORT, and WEBHOOK sections] ---
# (They remain the same as in your last code. I can re-add them below this message if needed.)

# --- [NOWPAYMENTS IPN HANDLER] ---
web_app = web.Application()

async def webhook_handler(request):
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.update_queue.put(update)
    except Exception as e:
        logging.error(f"Webhook error: {e}")
    return web.Response(text="ok")

async def ipn_handler(request):
    try:
        headers = request.headers
        if headers.get("x-nowpayments-sig") != NOWPAYMENTS_IPN_SECRET:
            return web.Response(status=403, text="Forbidden")
        data = await request.json()
        if data.get("payment_status") == "finished":
            username = data.get("order_description", "").lstrip("@")
            amount = float(data.get("price_amount", 0))
            if username in users:
                duration = 30 if amount == 2 else 60 if amount == 4 else 0
                if duration > 0:
                    expires = datetime.utcnow() + timedelta(days=duration)
                    users[username]["plan"] = "premium"
                    users[username]["expires"] = expires.isoformat()
                    save_users(users)
        return web.Response(text="ok")
    except Exception as e:
        logging.error(f"IPN error: {e}")
        return web.Response(status=500, text="error")

web_app.router.add_post("/webhook", webhook_handler)
web_app.router.add_post("/ipn", ipn_handler)

# --- [STARTUP & CLEANUP] ---
async def on_startup(app):
    await application.initialize()
    await application.start()
    await application.bot.set_webhook(f"{APP_URL}/webhook")
    logging.info("✅ Webhook set.")

async def on_cleanup(app):
    await application.stop()
    await application.shutdown()

web_app.on_startup.append(on_startup)
web_app.on_cleanup.append(on_cleanup)

# --- [HANDLERS REGISTER] ---
application.add_handler(CommandHandler("start", start))
# Add other command/message/callback handlers here

if __name__ == "__main__":
    web.run_app(web_app, port=PORT)
