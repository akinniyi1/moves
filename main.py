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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
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
DATA_FILE = "/mnt/data/user_data.json"

application = Application.builder().token(BOT_TOKEN).build()
file_registry = {}
image_collections = {}
support_messages = {}

# --- [HELPERS] ---
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)

def get_user_data(user):
    data = load_data()
    username = user.username
    if not username:
        return None, data
    if username not in data:
        data[username] = {"plan": "free", "expires": None, "downloads": 0}
    return data[username], data

def check_expiry(user_data):
    expires = user_data.get("expires")
    if expires and datetime.utcnow() > datetime.strptime(expires, "%Y-%m-%d %H:%M:%S"):
        user_data["plan"] = "free"
        user_data["expires"] = None

def readable_expiry(expires):
    if not expires:
        return "N/A"
    dt = datetime.strptime(expires, "%Y-%m-%d %H:%M:%S")
    return dt.strftime("%Y-%m-%d %I:%M %p")

def generate_filename(ext="mp4"):
    return f"file_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}.{ext}"

def is_valid_url(text):
    return re.match(r'https?://', text)

async def delete_file_later(path):
    await asyncio.sleep(60)
    if os.path.exists(path):
        os.remove(path)

# --- [START] ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_user_data(user)[1]  # Ensures user is saved
    buttons = [
        [InlineKeyboardButton("👤 View Profile", callback_data="profile"),
         InlineKeyboardButton("🖼️ Convert to PDF", callback_data="convertpdf_btn")],
        [InlineKeyboardButton("📩 Support", callback_data="support")],
    ]
    await update.message.reply_text(
        f"👋 Hello @{user.username or user.first_name}!\n\n"
        "This bot supports video downloads from:\n"
        "✅ Facebook, TikTok, Twitter, Instagram\n"
        "❌ YouTube not supported.\n\n"
        "Free Plan:\n• 3 video downloads max\n• 1 PDF conversion\n\n"
        "Send a video link or upload image(s) to begin.",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# --- [PROFILE] ---
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user
    user_data, all_data = get_user_data(user)
    check_expiry(user_data)

    if data == "profile":
        plan = user_data.get("plan", "free")
        exp = readable_expiry(user_data.get("expires"))
        count = user_data.get("downloads", 0)
        await query.message.reply_text(f"👤 @{user.username}\n💼 Plan: {plan}\n⏰ Expiry: {exp}\n📥 Downloads: {count}")
    elif data == "convertpdf_btn":
        await convert_pdf(update, context)
    elif data == "support":
        await query.message.reply_text("✍️ Send your message now. Admin will reply shortly.")
        support_messages[user.id] = user.username
    elif data.startswith("reply:"):
        uid = int(data.split(":")[1])
        support_messages[ADMIN_ID] = uid
        await query.message.reply_text(f"✍️ Type your reply to @{uid} now:")

# --- [VIDEO DOWNLOAD] ---
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_data, all_data = get_user_data(user)
    check_expiry(user_data)
    if user_data["plan"] == "free" and user_data["downloads"] >= 3:
        await update.message.reply_text("❌ Limit reached. Upgrade to continue downloading.")
        return

    url = update.message.text.strip()
    if not is_valid_url(url) or "youtube.com" in url or "youtu.be" in url:
        await update.message.reply_text("❌ Invalid or unsupported URL.")
        return

    filename = generate_filename()
    status_msg = await update.message.reply_text("📥 Downloading...")
    try:
        with yt_dlp.YoutubeDL({'outtmpl': filename, 'quiet': True}) as ydl:
            ydl.download([url])
        with open(filename, "rb") as f:
            sent = await update.message.reply_video(f, reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🎧 Convert to Audio", callback_data=f"audio:{filename}")]]
            ))
        asyncio.create_task(delete_file_later(filename))
        user_data["downloads"] += 1
        save_data(all_data)
        await status_msg.delete()
    except:
        await status_msg.edit_text("❌ Failed to download or file too large.")

# --- [AUDIO CONVERSION] ---
async def convert_to_audio(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path):
    audio_path = file_path.replace(".mp4", ".mp3")
    try:
        ffmpeg.input(file_path).output(audio_path).run(overwrite_output=True)
        with open(audio_path, "rb") as f:
            await update.callback_query.message.reply_audio(f)
        os.remove(audio_path)
    except:
        await update.callback_query.message.reply_text("❌ Audio conversion failed.")

# --- [IMAGE TO PDF] ---
pdf_trials = {}
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    path = generate_filename("jpg")
    await file.download_to_drive(path)
    image_collections.setdefault(user_id, []).append(path)
    asyncio.create_task(delete_file_later(path))
    await update.message.reply_text("✅ Image received. Send more or click /convertpdf to generate PDF.")

async def convert_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if pdf_trials.get(user_id):
        await update.message.reply_text("⛔ You have used your 1-time PDF trial.")
        return
    images = image_collections.get(user_id, [])
    if not images:
        await update.message.reply_text("❌ No images to convert.")
        return
    try:
        pdf = generate_filename("pdf")
        Image.open(images[0]).save(pdf, save_all=True, append_images=[Image.open(p) for p in images[1:]])
        with open(pdf, "rb") as f:
            await update.message.reply_document(f)
        os.remove(pdf)
        pdf_trials[user_id] = True
        image_collections[user_id] = []
    except:
        await update.message.reply_text("❌ Failed to create PDF.")

# --- [ADMIN COMMANDS] ---
async def upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try:
        username, hours = context.args[0].lstrip("@"), int(context.args[1])
        data = load_data()
        if username not in data:
            await update.message.reply_text("❌ User not found.")
            return
        expiry = datetime.utcnow() + timedelta(hours=hours)
        data[username]["plan"] = "premium"
        data[username]["expires"] = expiry.strftime("%Y-%m-%d %H:%M:%S")
        save_data(data)
        await update.message.reply_text(f"✅ Upgraded @{username} for {hours} hrs.")
    except:
        await update.message.reply_text("❌ Usage: /upgrade @username hours")

async def downgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try:
        username = context.args[0].lstrip("@")
        data = load_data()
        if username not in data:
            await update.message.reply_text("❌ User not found.")
            return
        data[username]["plan"] = "free"
        data[username]["expires"] = None
        save_data(data)
        await update.message.reply_text(f"✅ Downgraded @{username}")
    except:
        await update.message.reply_text("❌ Usage: /downgrade @username")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    data = load_data()
    total = len(data)
    paid = sum(1 for u in data.values() if u["plan"] == "premium")
    downloads = sum(u.get("downloads", 0) for u in data.values())
    await update.message.reply_text(f"📊 Total users: {total}\n💼 Paid: {paid}\n🆓 Free: {total-paid}\n📥 Downloads: {downloads}")

async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    data = load_data()
    path = "/mnt/data/users_export.csv"
    with open(path, "w", newline='') as f:
        w = csv.writer(f)
        w.writerow(["Username", "Plan", "Expiry", "Downloads"])
        for k, v in data.items():
            w.writerow([k, v["plan"], v.get("expires", ""), v.get("downloads", 0)])
    await update.message.reply_document(InputFile(path))

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    msg = " ".join(context.args)
    count = 0
    data = load_data()
    for username in data:
        try:
            await context.bot.send_message(chat_id=f"@{username}", text=msg)
            count += 1
        except: pass
    await update.message.reply_text(f"✅ Message sent to {count} users.")

# --- [SUPPORT REPLY SYSTEM] ---
async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id == ADMIN_ID:
        target = support_messages.get(ADMIN_ID)
        if isinstance(target, str):
            try:
                await context.bot.send_message(chat_id=f"@{target}", text=update.message.text)
                await update.message.reply_text("✅ Sent.")
            except:
                await update.message.reply_text("❌ Failed.")
        return
    if user.id in support_messages:
        admin_msg = await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"📩 Message from @{user.username}:\n\n{update.message.text}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Reply", callback_data=f"reply:{user.username}")]])
        )
        await update.message.reply_text("✅ Sent to admin.")
        support_messages.pop(user.id)

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
application.add_handler(CommandHandler("upgrade", upgrade))
application.add_handler(CommandHandler("downgrade", downgrade))
application.add_handler(CommandHandler("stats", stats))
application.add_handler(CommandHandler("export", export))
application.add_handler(CommandHandler("broadcast", broadcast))
application.add_handler(CommandHandler("convertpdf", convert_pdf))
application.add_handler(CallbackQueryHandler(handle_button))
application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video))
application.add_handler(MessageHandler(filters.TEXT, handle_messages))

if __name__ == "__main__":
    web.run_app(web_app, port=PORT)
