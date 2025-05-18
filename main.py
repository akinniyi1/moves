# --- [IMPORTS & SETUP] ---
import os
import re
import ssl
import json
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

application = Application.builder().token(BOT_TOKEN).build()
file_registry = {}
image_collections = {}
pdf_trials = {}

# --- [USER DATA STORAGE] ---
USER_DATA_FILE = "/mnt/data/users.json"

def load_user_data():
    if os.path.exists(USER_DATA_FILE):
        with open(USER_DATA_FILE, "r") as f:
            return json.load(f)
    return {}

def save_user_data(data):
    with open(USER_DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

user_data = load_user_data()

def get_user_plan(username):
    if not username or username not in user_data:
        return "free"
    plan_info = user_data[username]
    expiry = datetime.strptime(plan_info["expires_at"], "%Y-%m-%d %H:%M:%S")
    if datetime.utcnow() > expiry:
        return "free"
    return plan_info["plan"]

def set_user_plan(username, plan, hours):
    expires_at = datetime.utcnow() + timedelta(hours=hours)
    user_data[username] = {
        "plan": plan,
        "expires_at": expires_at.strftime("%Y-%m-%d %H:%M:%S")
    }
    save_user_data(user_data)

def plan_days_remaining(username):
    if username not in user_data:
        return 0
    expiry = datetime.strptime(user_data[username]["expires_at"], "%Y-%m-%d %H:%M:%S")
    delta = expiry - datetime.utcnow()
    return max(0, delta.days)

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
    buttons = [
        [InlineKeyboardButton("👤 View Profile", callback_data="profile"),
         InlineKeyboardButton("🖼️ Convert to PDF", callback_data="convertpdf_btn")],
        [InlineKeyboardButton("⏫ Upgrade Plan", callback_data="upgrade_plan")]
    ]
    await update.message.reply_text(
        f"👋 Hello @{user.username or user.first_name}!\n\n"
        "This bot supports downloading videos from:\n"
        "✅ Facebook, TikTok, Twitter\n"
        "❌ YouTube & Instagram are not supported.\n\n"
        "Limit for Free Users:\n"
        "• 3 video downloads per day\n"
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
    if "youtube.com" in url or "youtu.be" in url or "instagram.com" in url:
        await update.message.reply_text("❌ YouTube and Instagram are not supported.")
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
    except:
        await status_msg.edit_text("⚠️ Download failed or file too large or unsupported.")

# --- [INLINE BUTTON HANDLER] ---
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    username = query.from_user.username

    if data == "profile":
        plan = get_user_plan(username)
        days = plan_days_remaining(username)
        plan_str = f"{plan} (expires in {days} day(s))" if plan != "free" else "free"
        await query.message.reply_text(f"👤 Username: @{username}\n💼 Plan: {plan_str}")
    elif data == "convertpdf_btn":
        fake_msg = type("msg", (), {"message": query.message, "effective_user": query.from_user})
        await convert_pdf(fake_msg, context, triggered_by_button=True)
    elif data.startswith("audio:"):
        file = data.split("audio:")[1]
        if not os.path.exists(file):
            await query.message.reply_text("File deleted. Please resend the link.")
        else:
            await convert_to_audio(update, context, file)
    elif data == "upgrade_plan":
        buttons = [
            [InlineKeyboardButton("1 Day", callback_data="upgrade:1"),
             InlineKeyboardButton("5 Days", callback_data="upgrade:5"),
             InlineKeyboardButton("30 Days", callback_data="upgrade:30")]
        ]
        await query.message.reply_text("Choose your upgrade duration:", reply_markup=InlineKeyboardMarkup(buttons))
    elif data.startswith("upgrade:"):
        if not username:
            await query.message.reply_text("❌ You must set a username in Telegram settings.")
            return
        days = int(data.split(":")[1])
        set_user_plan(username, "premium", days * 24)
        await query.message.reply_text(f"✅ Upgraded to premium for {days} day(s).")

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
application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video))
application.add_handler(CallbackQueryHandler(handle_button))

if __name__ == "__main__":
    web.run_app(web_app, port=PORT)
