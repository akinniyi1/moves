# --- [IMPORTS & SETUP] ---
import os
import re
import ssl
import logging
import yt_dlp
import ffmpeg
import csv
import json
import asyncio
from PIL import Image
from datetime import datetime, timedelta
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
    ConversationHandler
)

ssl._create_default_https_context = ssl._create_unverified_context
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", 10000))
ADMIN_ID = 1378825382
DATA_PATH = "/mnt/data/user_data.json"

application = Application.builder().token(BOT_TOKEN).build()
file_registry = {}
image_collections = {}
pdf_trials = {}
UPGRADE_USERNAME = 1

# --- [STORAGE UTILS] ---
def load_users():
    if os.path.exists(DATA_PATH):
        with open(DATA_PATH, "r") as f:
            return json.load(f)
    return {}

def save_users(data):
    with open(DATA_PATH, "w") as f:
        json.dump(data, f, indent=2)

def get_user(username):
    users = load_users()
    return users.get(username.lower())

def set_user(username, key, value):
    users = load_users()
    user = users.get(username.lower(), {"plan": "free", "downloads": 0})
    user[key] = value
    users[username.lower()] = user
    save_users(users)

def increment_download(username):
    users = load_users()
    user = users.get(username.lower(), {"plan": "free", "downloads": 0})
    user["downloads"] = user.get("downloads", 0) + 1
    users[username.lower()] = user
    save_users(users)

def user_is_premium(username):
    user = get_user(username)
    if not user: return False
    if user["plan"] != "premium": return False
    expires = datetime.strptime(user["expires_at"], "%Y-%m-%d %H:%M:%S")
    if datetime.utcnow() > expires:
        set_user(username, "plan", "free")
        return False
    return True

def format_plan(user):
    if not user or user["plan"] == "free":
        return "free"
    expires = datetime.strptime(user["expires_at"], "%Y-%m-%d %H:%M:%S")
    left = (expires - datetime.utcnow()).total_seconds() / 3600
    return f"premium ({int(left // 24)} day(s) left)"

# --- [HELPERS] ---
def is_valid_url(text):
    return re.match(r'https?://', text)

def generate_filename(ext="mp4"):
    return f"file_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}.{ext}"

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
    user = update.effective_user
    username = user.username
    if username:
        users = load_users()
        if username.lower() not in users:
            users[username.lower()] = {"plan": "free", "downloads": 0}
            save_users(users)

    buttons = [
        [InlineKeyboardButton("👤 View Profile", callback_data="profile"),
         InlineKeyboardButton("🖼️ Convert to PDF", callback_data="convertpdf_btn")]
    ]
    if user.id == ADMIN_ID:
        buttons.append([InlineKeyboardButton("🛠️ Upgrade Plan", callback_data="admin_upgrade")])

    await update.message.reply_text(
        f"👋 Hello @{username or user.first_name}!\n\n"
        "This bot supports downloading videos from:\n"
        "✅ Facebook, TikTok, Twitter\n"
        "❌ YouTube & Instagram are not supported.\n\n"
        "Free Users:\n"
        "• Max 3 total video downloads\n"
        "• 1 free PDF conversion\n\n"
        "Send a video link to get started.",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# --- [VIDEO HANDLER] ---
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if not is_valid_url(url):
        await update.message.reply_text("❌ Invalid URL or unsupported platform.")
        return

    if "youtube.com" in url or "youtu.be" in url or "instagram.com" in url:
        await update.message.reply_text("❌ YouTube and Instagram are not supported.")
        return

    username = update.effective_user.username
    if not username:
        await update.message.reply_text("❌ You must set a Telegram @username to use this bot.")
        return

    if not user_is_premium(username):
        user = get_user(username)
        if user["downloads"] >= 3:
            await update.message.reply_text("⛔ Free users can only download 3 videos. Upgrade to continue.")
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
        increment_download(username)
    except:
        await status_msg.edit_text("⚠️ Download failed or file too large or unsupported.")

# --- [INLINE BUTTON HANDLER] ---
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "profile":
        user = update.effective_user
        udata = get_user(user.username)
        await query.message.reply_text(f"👤 Username: @{user.username}\n💼 Plan: {format_plan(udata)}")

    elif data == "convertpdf_btn":
        fake_msg = type("msg", (), {"message": query.message, "effective_user": query.from_user})
        await convert_pdf(fake_msg, context, triggered_by_button=True)

    elif data == "admin_upgrade" and query.from_user.id == ADMIN_ID:
        await query.message.reply_text("Send the @username of the user to upgrade:", reply_markup=ReplyKeyboardRemove())
        return UPGRADE_USERNAME

    elif data.startswith("audio:"):
        file = data.split("audio:")[1]
        if not os.path.exists(file):
            await query.message.reply_text("File deleted. Please resend the link.")
        else:
            await convert_to_audio(update, context, file)

# --- [PDF CONVERSION] ---
async def convert_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, triggered_by_button=False):
    user_id = update.effective_user.id
    if pdf_trials.get(user_id, 0) >= 1:
        await update.message.reply_text("⛔ Free users can only convert 1 PDF.")
        return
    pdf_trials[user_id] = 1
    images = image_collections.get(user_id, [])
    if not images:
        await update.message.reply_text("❌ No images received.")
        return
    try:
        pil_images = [Image.open(img).convert("RGB") for img in images]
        pdf_path = generate_filename("pdf")
        pil_images[0].save(pdf_path, save_all=True, append_images=pil_images[1:])
        with open(pdf_path, 'rb') as f:
            await update.message.reply_document(f, filename="converted.pdf")
        os.remove(pdf_path)
        for img in images:
            os.remove(img)
        image_collections[user_id] = []
    except:
        await update.message.reply_text("❌ Failed to generate PDF.")

# --- [IMAGE HANDLER] ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_path = f"image_{datetime.utcnow().strftime('%H%M%S%f')}.jpg"
    await file.download_to_drive(image_path)
    if user_id not in image_collections:
        image_collections[user_id] = []
    image_collections[user_id].append(image_path)
    await update.message.reply_text("✅ Image received. Send more or convert to PDF.")

# --- [UPGRADE FLOW] ---
async def upgrade_receive_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.message.text.strip().lstrip("@")
    if not username:
        await update.message.reply_text("Invalid username.")
        return ConversationHandler.END

    buttons = [
        [InlineKeyboardButton("1 day", callback_data=f"upgrade:{username}:24"),
         InlineKeyboardButton("5 days", callback_data=f"upgrade:{username}:120"),
         InlineKeyboardButton("30 days", callback_data=f"upgrade:{username}:720")]
    ]
    await update.message.reply_text(f"Select upgrade duration for @{username}:", reply_markup=InlineKeyboardMarkup(buttons))
    return ConversationHandler.END

@application.callback_query_handler
async def inline_upgrade_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    if data.startswith("upgrade:"):
        _, username, hours = data.split(":")
        expires = datetime.utcnow() + timedelta(hours=int(hours))
        set_user(username, "plan", "premium")
        set_user(username, "expires_at", expires.strftime("%Y-%m-%d %H:%M:%S"))
        await update.callback_query.message.reply_text(f"✅ @{username} upgraded for {int(hours)//24} day(s).")

# --- [ADMIN COMMANDS] ---
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    users = load_users()
    free = sum(1 for u in users.values() if u["plan"] == "free")
    paid = sum(1 for u in users.values() if u["plan"] == "premium")
    await update.message.reply_text(f"📊 Total Users: {len(users)}\n🆓 Free: {free}\n💎 Paid: {paid}")

async def export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    users = load_users()
    path = "/mnt/data/users_export.csv"
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["username", "plan", "downloads", "expires_at"])
        for uname, data in users.items():
            writer.writerow([uname, data.get("plan"), data.get("downloads"), data.get("expires_at", "")])
    with open(path, "rb") as f:
        await update.message.reply_document(f, filename="users.csv")

# --- [WEBHOOK SETUP] ---
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
    logging.info("✅ Webhook set.")

async def on_cleanup(app):
    await application.stop()
    await application.shutdown()

web_app.on_startup.append(on_startup)
web_app.on_cleanup.append(on_cleanup)

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("stats", stats))
application.add_handler(CommandHandler("export", export_csv))
application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video))
application.add_handler(CallbackQueryHandler(handle_button))
application.add_handler(ConversationHandler(
    entry_points=[CallbackQueryHandler(handle_button, pattern="^admin_upgrade$")],
    states={UPGRADE_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, upgrade_receive_username)]},
    fallbacks=[],
    allow_reentry=True
))

if __name__ == "__main__":
    web.run_app(web_app, port=PORT)
