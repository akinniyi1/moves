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
DATA_FILE = "/mnt/data/users.json"

application = Application.builder().token(BOT_TOKEN).build()
file_registry = {}
image_collections = {}
pdf_trials = {}
support_messages = {}

# --- [USER STORAGE] ---
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
    if user.get("plan") != "premium": return False
    if "expires" not in user: return False
    return datetime.utcnow() < datetime.fromisoformat(user["expires"])

async def delete_file_later(path, file_id=None):
    await asyncio.sleep(60)
    if os.path.exists(path):
        os.remove(path)
    if file_id:
        file_registry.pop(file_id, None)

def downgrade_expired_users():
    now = datetime.utcnow()
    for username, user in users.items():
        if user.get("plan") == "premium" and "expires" in user:
            if now >= datetime.fromisoformat(user["expires"]):
                users[username] = {"plan": "free", "downloads": 0}
    save_users(users)

# --- [START HANDLER] ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username
    if username and username not in users:
        users[username] = {"plan": "free", "downloads": 0}
        save_users(users)
    downgrade_expired_users()
    buttons = [
        [InlineKeyboardButton("👤 View Profile", callback_data="profile"),
         InlineKeyboardButton("🖼️ Convert to PDF", callback_data="convertpdf_btn")],
        [InlineKeyboardButton("📩 Contact Support", url="https://t.me/DownloadassaasSupport_bot")]
    ]
    await update.message.reply_text(
        f"👋 Hello @{username or user.first_name}!\n\n"
        "This bot supports downloading videos from:\n"
        "✅ Facebook, TikTok, Twitter, Instagram\n"
        "❌ YouTube is not supported.\n\n"
        "Free Users:\n• 3 video downloads\n• 1 PDF conversion trial\n\n"
        "Send a supported video link to begin.",
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
    username = user.username
    if not username:
        await update.message.reply_text("❌ Username is required to use this bot.")
        return
    user_data = users.get(username, {"plan": "free", "downloads": 0})
    downgrade_expired_users()
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

# --- [INLINE HANDLER] ---
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user
    username = user.username
    user_data = users.get(username, {"plan": "free"})
    downgrade_expired_users()

    if data == "profile":
        if is_premium(user_data):
            exp = datetime.fromisoformat(user_data["expires"])
            msg = f"👤 Username: @{username}\n💼 Plan: Premium\n⏰ Expires: {exp.strftime('%Y-%m-%d %H:%M')} UTC"
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

# --- [PDF HANDLER] ---
async def convert_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, triggered_by_button=False):
    user_id = update.effective_user.id
    username = update.effective_user.username
    user_data = users.get(username, {"plan": "free"})
    downgrade_expired_users()
    if not is_premium(user_data):
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
        asyncio.create_task(delete_file_later(pdf_path))
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
    await update.message.reply_text("✅ Image received. Send more or click /convertpdf to generate PDF.")

# --- [ADMIN COMMANDS] ---
async def upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("Usage: /upgrade <username> <hours>")
        return
    username, hours = args
    username = username.lstrip('@')
    if username not in users:
        await update.message.reply_text("❌ User not found.")
        return
    try:
        hours = int(hours)
        expires = datetime.utcnow() + timedelta(hours=hours)
        users[username]["plan"] = "premium"
        users[username]["expires"] = expires.isoformat()
        save_users(users)
        await update.message.reply_text(f"✅ Upgraded @{username} until {expires.strftime('%Y-%m-%d %H:%M')} UTC")
    except:
        await update.message.reply_text("❌ Invalid hours")

async def downgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /downgrade <username>")
        return
    username = args[0].lstrip('@')
    if username in users:
        users[username] = {"plan": "free", "downloads": 0}
        save_users(users)
        await update.message.reply_text(f"✅ Downgraded @{username} to free plan.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    downgrade_expired_users()
    total = len(users)
    premium = sum(1 for u in users.values() if is_premium(u))
    free = total - premium
    downloads = sum(u.get("downloads", 0) for u in users.values())
    await update.message.reply_text(
        f"📊 Stats:\nTotal Users: {total}\nPremium: {premium}\nFree: {free}\nDownloads: {downloads}"
    )

async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    path = "/mnt/data/export.csv"
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Username", "Plan", "Expires"])
        for uname, data in users.items():
            exp = data.get("expires", "N/A")
            writer.writerow([uname, data["plan"], exp])
    with open(path, "rb") as f:
        await update.message.reply_document(f, filename="users.csv")

# --- [SUPPORT SYSTEM] ---
async def support_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.reply_to_message and update.effective_user.id == ADMIN_ID:
        msg_id = update.message.reply_to_message.message_id
        if msg_id in support_messages:
            uid = support_messages[msg_id]
            await context.bot.send_message(chat_id=uid, text=f"📬 Admin reply:\n{update.message.text}")

async def user_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        return
    forwarded = await context.bot.send_message(
        ADMIN_ID,
        f"📩 Message from @{update.effective_user.username}:\n\n{update.message.text}"
    )
    support_messages[forwarded.message_id] = update.effective_user.id
    await update.message.reply_text("✅ Message sent. You’ll get a reply soon.")

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

# --- [HANDLERS] ---
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("upgrade", upgrade))
application.add_handler(CommandHandler("downgrade", downgrade))
application.add_handler(CommandHandler("stats", stats))
application.add_handler(CommandHandler("export", export))
application.add_handler(CommandHandler("convertpdf", lambda u, c: convert_pdf(u, c, False)))
application.add_handler(CallbackQueryHandler(handle_button))
application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
application.add_handler(MessageHandler(filters.TEXT & filters.REPLY, support_reply))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(r'^https?://'), user_support))
application.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^https?://'), handle_video))

if __name__ == "__main__":
    web.run_app(web_app, port=PORT)
