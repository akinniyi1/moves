# --- [IMPORTS & SETUP] ---
import os
import re
import ssl
import json
import csv
import logging
import yt_dlp
import ffmpeg
import asyncio
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
USER_DATA_PATH = "/mnt/data/users.json"

application = Application.builder().token(BOT_TOKEN).build()
file_registry = {}
image_collections = {}
pdf_trials = {}

# --- [HELPERS: USER DATA] ---
def load_users():
    if os.path.exists(USER_DATA_PATH):
        with open(USER_DATA_PATH, 'r') as f:
            return json.load(f)
    return {}

def save_users(data):
    with open(USER_DATA_PATH, 'w') as f:
        json.dump(data, f)

def get_user(username):
    users = load_users()
    return users.get(username)

def update_user(username, field, value):
    users = load_users()
    if username not in users:
        users[username] = {"plan": "free", "downloads": 0, "expires": None}
    users[username][field] = value
    save_users(users)

def is_upgraded(username):
    user = get_user(username)
    if not user: return False
    if user["plan"] == "paid" and user["expires"]:
        if datetime.utcnow() < datetime.fromisoformat(user["expires"]):
            return True
        else:
            update_user(username, "plan", "free")
            update_user(username, "expires", None)
    return False

def can_download(username):
    user = get_user(username)
    if not user: return True
    return user["downloads"] < 3 or is_upgraded(username)

def increment_download(username):
    user = get_user(username)
    if not user:
        update_user(username, "downloads", 1)
    else:
        count = user.get("downloads", 0)
        update_user(username, "downloads", count + 1)

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
    if user.username:
        update_user(user.username, "plan", "free")
    buttons = [[InlineKeyboardButton("👤 View Profile", callback_data="profile")]]
    await update.message.reply_text(
        f"👋 Hello @{user.username or user.first_name}!\n\n"
        "This bot supports downloading videos from:\n"
        "✅ Facebook, TikTok, Twitter\n"
        "❌ YouTube & Instagram are not supported.\n\n"
        "Free plan:\n"
        "• Max 3 video downloads (lifetime)\n"
        "• 1 PDF trial\n\n"
        "Send a supported link to begin.",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# --- [VIDEO HANDLER] ---
def is_valid_url(text):
    return re.match(r'https?://', text)

def generate_filename(ext="mp4"):
    return f"file_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}.{ext}"

async def delete_file_later(path, file_id=None):
    await asyncio.sleep(60)
    if os.path.exists(path): os.remove(path)
    if file_id: file_registry.pop(file_id, None)

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username
    url = update.message.text.strip()

    if not is_valid_url(url):
        await update.message.reply_text("❌ Invalid URL.")
        return
    if "youtube.com" in url or "youtu.be" in url or "instagram.com" in url:
        await update.message.reply_text("❌ YouTube and Instagram are not supported.")
        return
    if not is_upgraded(username) and not can_download(username):
        await update.message.reply_text("⛔ You have reached your 3-download limit. Upgrade to continue.")
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
        await status_msg.edit_text("⚠️ Download failed or too large.")

# --- [INLINE BUTTONS] ---
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user

    if data == "profile":
        u = get_user(user.username)
        plan = u["plan"] if u else "free"
        expiry = u.get("expires")
        expiry_display = datetime.fromisoformat(expiry).strftime("%Y-%m-%d") if expiry else "N/A"
        await query.message.reply_text(f"👤 Username: @{user.username}\n💼 Plan: {plan}\n⏰ Expiry: {expiry_display}")
    elif data.startswith("audio:"):
        file = data.split("audio:")[1]
        if not os.path.exists(file):
            await query.message.reply_text("File deleted. Please resend link.")
        else:
            await convert_to_audio(update, context, file)

# --- [PDF & IMAGE HANDLING] ---
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
        for img in images: os.remove(img)
        image_collections[user_id] = []
    except:
        await update.message.reply_text("❌ Failed to generate PDF.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_path = f"image_{datetime.utcnow().strftime('%H%M%S%f')}.jpg"
    await file.download_to_drive(image_path)
    image_collections.setdefault(user_id, []).append(image_path)
    await update.message.reply_text("✅ Image received. Send more or use /pdf to convert.")

# --- [UPGRADE, STATS, EXPORT COMMANDS] ---
async def upgrade_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return await update.message.reply_text("⛔ Admins only.")
    if len(context.args) != 2:
        return await update.message.reply_text("Usage: /upgrade <username> <hours>")
    username = context.args[0].lstrip("@")
    hours = int(context.args[1])
    expires = datetime.utcnow() + timedelta(hours=hours)
    update_user(username, "plan", "paid")
    update_user(username, "expires", expires.isoformat())
    await update.message.reply_text(f"✅ Upgraded @{username} for {hours}h.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    users = load_users()
    total = len(users)
    paid = sum(1 for u in users.values() if u["plan"] == "paid")
    free = total - paid
    downloads = sum(u.get("downloads", 0) for u in users.values())
    await update.message.reply_text(f"📊 Total: {total}\n🆓 Free: {free}\n💼 Paid: {paid}\n⬇️ Downloads: {downloads}")

async def export_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    users = load_users()
    filepath = "/mnt/data/users.csv"
    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Username", "Plan", "Expires"])
        for username, data in users.items():
            writer.writerow([username, data.get("plan"), data.get("expires")])
    with open(filepath, "rb") as f:
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
application.add_handler(CommandHandler("upgrade", upgrade_user))
application.add_handler(CommandHandler("stats", stats))
application.add_handler(CommandHandler("export", export_users))
application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video))
application.add_handler(CallbackQueryHandler(handle_button))

if __name__ == "__main__":
    web.run_app(web_app, port=PORT)
