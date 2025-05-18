# --- [IMPORTS & SETUP] ---
import os
import re
import ssl
import logging
import yt_dlp
import ffmpeg
import asyncio
import json
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
DATA_FILE = "/mnt/data/users.json"
CSV_FILE = "/mnt/data/users.csv"

application = Application.builder().token(BOT_TOKEN).build()
file_registry = {}
image_collections = {}

# --- [STORAGE HELPERS] ---
def load_users():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_users(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)

def get_user(username):
    users = load_users()
    return users.get(username.lower())

def update_user(username, field, value):
    username = username.lower()
    users = load_users()
    if username not in users:
        users[username] = {"plan": "free", "expires": None, "downloads": 0, "pdf": 0}
    users[username][field] = value
    save_users(users)

def check_plan(username):
    users = load_users()
    user = users.get(username.lower())
    if not user:
        return "free"
    if user["plan"] != "free":
        if datetime.utcnow().timestamp() > user["expires"]:
            users[username.lower()]["plan"] = "free"
            save_users(users)
            return "free"
        return user["plan"]
    return "free"

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
    username = user.username or f"id{user.id}"
    update_user(username, "plan", check_plan(username))
    buttons = [
        [
            InlineKeyboardButton("👤 View Profile", callback_data="profile"),
            InlineKeyboardButton("🖼️ Convert to PDF", callback_data="convertpdf_btn")
        ]
    ]
    await update.message.reply_text(
        f"👋 Hello @{username}!\n\n"
        "This bot supports downloading videos from:\n"
        "✅ Facebook, TikTok, Twitter, Instagram\n"
        "❌ YouTube is not supported.\n\n"
        "Limit for Free Users:\n"
        "• 3 video downloads total\n"
        "• 1 PDF conversion trial\n\n"
        "Send a supported video link to get started.",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# --- [VIDEO HANDLER] ---
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if not is_valid_url(url):
        await update.message.reply_text("❌ Invalid URL or unsupported platform.")
        return
    if "youtube.com" in url or "youtu.be" in url:
        await update.message.reply_text("❌ YouTube is not supported.")
        return

    user = update.effective_user
    username = user.username or f"id{user.id}"
    plan = check_plan(username)
    users = load_users()
    user_data = users.get(username.lower(), {"downloads": 0, "plan": "free"})

    if plan == "free" and user_data.get("downloads", 0) >= 3:
        await update.message.reply_text("⛔ Free users can only download 3 videos. Please upgrade.")
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
        user_data["downloads"] = user_data.get("downloads", 0) + 1
        update_user(username, "downloads", user_data["downloads"])
    except:
        await status_msg.edit_text("⚠️ Download failed or file too large or unsupported.")

# --- [INLINE BUTTON HANDLER] ---
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    username = query.from_user.username or f"id{query.from_user.id}"

    if data == "profile":
        users = load_users()
        user_data = users.get(username.lower(), {"plan": "free", "downloads": 0})
        plan = user_data.get("plan", "free")
        expires = user_data.get("expires")
        exp_date = datetime.utcfromtimestamp(expires).strftime('%Y-%m-%d %H:%M') if expires else "N/A"
        await query.message.reply_text(
            f"👤 Username: @{username}\n"
            f"💼 Plan: {plan}\n"
            f"⏰ Expires: {exp_date}\n"
            f"📦 Downloads: {user_data.get('downloads', 0)}"
        )
    elif data == "convertpdf_btn":
        fake_msg = type("msg", (), {"message": query.message, "effective_user": query.from_user})
        await convert_pdf(fake_msg, context, triggered_by_button=True)
    elif data.startswith("audio:"):
        file = data.split("audio:")[1]
        if not os.path.exists(file):
            await query.message.reply_text("File deleted. Please resend the link.")
        else:
            await convert_to_audio(update, context, file)

# --- [PDF CONVERSION] ---
async def convert_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, triggered_by_button=False):
    user = update.effective_user
    username = user.username or f"id{user.id}"
    users = load_users()
    user_data = users.get(username.lower(), {})
    if user_data.get("pdf", 0) >= 1 and user_data.get("plan") == "free":
        await update.message.reply_text("⛔ Free users can only convert 1 PDF.")
        return
    users[username.lower()]["pdf"] = 1
    save_users(users)

    images = image_collections.get(user.id, [])
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
        image_collections[user.id] = []
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

# --- [ADMIN COMMANDS] ---
async def upgrade_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /upgrade <username> <hours>")
        return
    username = context.args[0].replace("@", "").lower()
    hours = int(context.args[1])
    expires = datetime.utcnow() + timedelta(hours=hours)
    update_user(username, "plan", "premium")
    update_user(username, "expires", expires.timestamp())
    await update.message.reply_text(f"✅ Upgraded @{username} for {hours} hours.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    users = load_users()
    free = sum(1 for u in users.values() if u["plan"] == "free")
    paid = sum(1 for u in users.values() if u["plan"] != "free")
    total_downloads = sum(u.get("downloads", 0) for u in users.values())
    await update.message.reply_text(
        f"📊 Stats:\nFree Users: {free}\nPaid Users: {paid}\nTotal Downloads: {total_downloads}"
    )

async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    users = load_users()
    with open(CSV_FILE, "w", newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["Username", "Plan", "Expires"])
        for uname, data in users.items():
            expiry = datetime.utcfromtimestamp(data["expires"]).strftime('%Y-%m-%d %H:%M') if data["expires"] else "N/A"
            writer.writerow([uname, data["plan"], expiry])
    with open(CSV_FILE, "rb") as f:
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
application.add_handler(CommandHandler("upgrade", upgrade_command))
application.add_handler(CommandHandler("stats", stats_command))
application.add_handler(CommandHandler("export", export_command))
application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video))
application.add_handler(CallbackQueryHandler(handle_button))

if __name__ == "__main__":
    web.run_app(web_app, port=PORT)
