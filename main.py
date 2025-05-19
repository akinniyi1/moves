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

USERS_FILE = "/mnt/data/users.json"

def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_users(users):
    with open(USERS_FILE, "w") as f:
        json.dump(users, f)

def update_user(username, key, value):
    users = load_users()
    if username not in users:
        users[username] = {"plan": "free", "downloads": 0, "total_downloads": 0, "expires": None}
    users[username][key] = value
    save_users(users)

def get_user(username):
    users = load_users()
    if username not in users:
        users[username] = {"plan": "free", "downloads": 0, "total_downloads": 0, "expires": None}
        save_users(users)
    return users[username]

def check_expiry(username):
    users = load_users()
    user = users.get(username)
    if user and user["plan"] != "free" and user["expires"]:
        expiry = datetime.strptime(user["expires"], "%Y-%m-%d %H:%M")
        if datetime.utcnow() > expiry:
            user["plan"] = "free"
            user["downloads"] = 0
            user["expires"] = None
            save_users(users)

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
    get_user(username)
    check_expiry(username)
    buttons = [
        [InlineKeyboardButton("👤 View Profile", callback_data="profile"),
         InlineKeyboardButton("🖼️ Convert to PDF", callback_data="convertpdf_btn")],
        [InlineKeyboardButton("💬 Support", callback_data="support_msg")]
    ]
    await update.message.reply_text(
        f"👋 Hello @{username or user.first_name}!\n\n"
        "This bot supports downloading videos from:\n"
        "✅ Facebook, TikTok, Twitter, Instagram\n"
        "❌ YouTube is not supported.\n\n"
        "Free User Limits:\n"
        "• 3 video downloads\n"
        "• 1 PDF conversion trial\n\n"
        "Send a supported video link to begin.",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# --- [VIDEO HANDLER] ---
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username
    check_expiry(username)
    user_data = get_user(username)
    url = update.message.text.strip()

    if not is_valid_url(url):
        await update.message.reply_text("❌ Invalid URL or unsupported platform.")
        return

    if "youtube.com" in url or "youtu.be" in url:
        await update.message.reply_text("❌ YouTube is not supported.")
        return

    if user_data["plan"] == "free" and user_data["downloads"] >= 3:
        await update.message.reply_text("⛔ Free users can only download 3 videos. Please upgrade to continue.")
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

        user_data["downloads"] += 1
        user_data["total_downloads"] += 1
        update_user(username, "downloads", user_data["downloads"])
        update_user(username, "total_downloads", user_data["total_downloads"])

        await status_msg.delete()
    except:
        await status_msg.edit_text("⚠️ Download failed or unsupported media.")

# --- [INLINE BUTTON HANDLER] ---
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    username = query.from_user.username
    check_expiry(username)

    if data == "profile":
        user = get_user(username)
        expiry_display = f"⏳ Expires: {user['expires']}" if user["plan"] != "free" else ""
        await query.message.reply_text(
            f"👤 Username: @{username}\n"
            f"💼 Plan: {user['plan']}\n"
            f"{expiry_display}\n"
            f"⬇️ Total Downloads: {user['total_downloads']}"
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
    elif data == "support_msg":
        await query.message.reply_text("💬 Please type your message. The admin will reply shortly.")

# --- [PHOTO HANDLER & PDF CONVERT] ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = update.effective_user
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_path = f"image_{datetime.utcnow().strftime('%H%M%S%f')}.jpg"
    await file.download_to_drive(image_path)
    if user_id not in image_collections:
        image_collections[user_id] = []
    image_collections[user_id].append(image_path)
    asyncio.create_task(delete_file_later(image_path))
    await update.message.reply_text("✅ Image received. Send more or click /convertpdf to generate PDF.")

async def convert_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, triggered_by_button=False):
    user_id = update.effective_user.id
    username = update.effective_user.username
    if pdf_trials.get(user_id, 0) >= 1 and get_user(username)["plan"] == "free":
        await update.message.reply_text("⛔ Free users can only convert 1 PDF. Upgrade to remove limit.")
        return
    pdf_trials[user_id] = 1
    images = image_collections.get(user_id, [])
    if not images:
        await update.message.reply_text("❌ No images found.")
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

# --- [UPGRADE / DOWNGRADE / STATS / EXPORT / BROADCAST] ---
async def upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /upgrade <username> <hours>")
        return
    username = context.args[0].lstrip('@')
    hours = int(context.args[1])
    users = load_users()
    if username not in users:
        await update.message.reply_text("❌ User not found.")
        return
    expiry = datetime.utcnow() + timedelta(hours=hours)
    users[username]["plan"] = "premium"
    users[username]["expires"] = expiry.strftime("%Y-%m-%d %H:%M")
    users[username]["downloads"] = 0
    save_users(users)
    await update.message.reply_text(f"✅ Upgraded @{username} until {expiry.strftime('%Y-%m-%d %H:%M')}")

async def downgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /downgrade <username>")
        return
    username = context.args[0].lstrip('@')
    users = load_users()
    if username not in users:
        await update.message.reply_text("❌ User not found.")
        return
    users[username]["plan"] = "free"
    users[username]["downloads"] = 0
    users[username]["expires"] = None
    save_users(users)
    await update.message.reply_text(f"⬇️ Downgraded @{username} to free plan.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    users = load_users()
    total = len(users)
    paid = sum(1 for u in users.values() if u['plan'] == 'premium')
    total_dl = sum(u.get("total_downloads", 0) for u in users.values())
    await update.message.reply_text(f"📊 Total Users: {total}\n💼 Paid Users: {paid}\n⬇️ Total Downloads: {total_dl}")

async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    users = load_users()
    path = "/mnt/data/export.csv"
    with open(path, "w") as f:
        f.write("Username,Plan,Expiry,Total Downloads\n")
        for name, data in users.items():
            f.write(f"{name},{data['plan']},{data.get('expires')},{data.get('total_downloads', 0)}\n")
    with open(path, "rb") as f:
        await update.message.reply_document(f)

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    msg = " ".join(context.args)
    if not msg:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    users = load_users()
    sent = 0
    for name in users:
        try:
            await context.bot.send_message(chat_id=f"@{name}", text=msg)
            sent += 1
        except:
            continue
    await update.message.reply_text(f"✅ Message sent to {sent} users.")

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

async def on_cleanup(app):
    await application.stop()
    await application.shutdown()

web_app.on_startup.append(on_startup)
web_app.on_cleanup.append(on_cleanup)

application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video))
application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
application.add_handler(CommandHandler("convertpdf", convert_pdf))
application.add_handler(CommandHandler("upgrade", upgrade))
application.add_handler(CommandHandler("downgrade", downgrade))
application.add_handler(CommandHandler("stats", stats))
application.add_handler(CommandHandler("export", export))
application.add_handler(CommandHandler("broadcast", broadcast))
application.add_handler(CallbackQueryHandler(handle_button))

if __name__ == "__main__":
    web.run_app(web_app, port=PORT)
