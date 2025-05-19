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
    CallbackQueryHandler, ContextTypes, filters, ConversationHandler
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
reply_context = {}

# --- [HELPERS] ---
def load_data():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_user_data(user):
    data = load_data()
    username = user.username
    if not username:
        return None
    if username not in data:
        data[username] = {
            "plan": "free",
            "expires": None,
            "downloads": 0,
            "total_downloads": 0,
            "pdf": 0
        }
        save_data(data)
    return data[username]

def update_user_data(username, key, value):
    data = load_data()
    if username in data:
        data[username][key] = value
        save_data(data)

def reset_user_plan_if_expired(username):
    data = load_data()
    if username in data and data[username]["plan"] != "free":
        if data[username]["expires"]:
            expire_dt = datetime.fromisoformat(data[username]["expires"])
            if datetime.utcnow() >= expire_dt:
                data[username]["plan"] = "free"
                data[username]["expires"] = None
                save_data(data)

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
    get_user_data(user)
    buttons = [
        [InlineKeyboardButton("👤 View Profile", callback_data="profile"),
         InlineKeyboardButton("🖼️ Convert to PDF", callback_data="convertpdf_btn")],
        [InlineKeyboardButton("📩 Contact Support", callback_data=f"support:{user.username}")]
    ]
    await update.message.reply_text(
        f"👋 Hello @{user.username or user.first_name}!\n\n"
        "This bot supports downloading videos from:\n"
        "✅ Facebook, TikTok, Twitter, Instagram\n"
        "❌ YouTube is not supported.\n\n"
        "Free Plan:\n"
        "• 3 downloads total\n"
        "• 1 PDF conversion\n\n"
        "Send a supported video link to get started.",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# --- [PROFILE VIEW] ---
async def view_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username
    reset_user_plan_if_expired(username)
    data = load_data()
    if username in data:
        info = data[username]
        plan = info["plan"]
        expiry = info["expires"]
        total = info.get("total_downloads", 0)
        if plan == "free":
            msg = f"👤 Username: @{username}\n💼 Plan: Free\n📥 Downloads: {total}"
        else:
            dt = datetime.fromisoformat(expiry)
            msg = f"👤 Username: @{username}\n💼 Plan: Premium\n⏰ Expires: {dt.strftime('%Y-%m-%d %H:%M')}\n📥 Downloads: {total}"
        await update.callback_query.message.reply_text(msg)

# --- [INLINE BUTTON HANDLER] ---
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "profile":
        await view_profile(update, context)
    elif data == "convertpdf_btn":
        await query.message.reply_text("Click /convertpdf to generate your PDF file.")
    elif data.startswith("audio:"):
        file = data.split("audio:")[1]
        if not os.path.exists(file):
            await query.message.reply_text("File deleted. Please resend the link.")
        else:
            await convert_to_audio(update, context, file)
    elif data.startswith("support:"):
        await query.message.reply_text("Please type your message and it will be sent to admin.")
    elif data.startswith("reply:"):
        target = data.split("reply:")[1]
        reply_context[update.effective_user.id] = target
        await query.message.reply_text(f"Type your reply to @{target}")

# --- [VIDEO HANDLER] ---
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username
    reset_user_plan_if_expired(username)
    user_data = get_user_data(user)
    url = update.message.text.strip()

    if not is_valid_url(url) or "youtube.com" in url or "youtu.be" in url:
        await update.message.reply_text("❌ Invalid or unsupported URL.")
        return

    if user_data["plan"] == "free" and user_data["downloads"] >= 3:
        await update.message.reply_text("⛔ You've reached your 3 free downloads. Upgrade to continue.")
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
        save_data(load_data())

        await status_msg.delete()
    except:
        await status_msg.edit_text("⚠️ Download failed or file too large.")

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
    asyncio.create_task(delete_file_later(image_path))

# --- [PDF CONVERSION] ---
async def convert_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username
    reset_user_plan_if_expired(username)
    data = get_user_data(user)

    if data["plan"] == "free" and data["pdf"] >= 1:
        await update.message.reply_text("⛔ Free users can only convert 1 PDF.")
        return

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
        data["pdf"] += 1
        save_data(load_data())
    except:
        await update.message.reply_text("❌ Failed to generate PDF.")

# --- [ADMIN COMMANDS] ---
async def upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /upgrade @username hours")
        return
    username = context.args[0].lstrip("@")
    hours = int(context.args[1])
    data = load_data()
    if username not in data:
        await update.message.reply_text("❌ User not found.")
        return
    data[username]["plan"] = "premium"
    data[username]["expires"] = (datetime.utcnow() + timedelta(hours=hours)).isoformat()
    save_data(data)
    await update.message.reply_text(f"✅ Upgraded @{username} for {hours} hour(s).")

async def downgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /downgrade @username")
        return
    username = context.args[0].lstrip("@")
    data = load_data()
    if username in data:
        data[username]["plan"] = "free"
        data[username]["expires"] = None
        save_data(data)
        await update.message.reply_text(f"✅ Downgraded @{username} to Free.")

async def export_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    data = load_data()
    path = "/mnt/data/export.csv"
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Username", "Plan", "Expires", "Total Downloads"])
        for user, info in data.items():
            writer.writerow([user, info["plan"], info["expires"], info["total_downloads"]])
    await update.message.reply_document(InputFile(path), filename="users.csv")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    data = load_data()
    total_users = len(data)
    paid = sum(1 for u in data.values() if u["plan"] != "free")
    downloads = sum(u.get("total_downloads", 0) for u in data.values())
    await update.message.reply_text(f"📊 Stats:\nTotal users: {total_users}\nPaid users: {paid}\nTotal downloads: {downloads}")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    msg = " ".join(context.args)
    if not msg:
        await update.message.reply_text("Usage: /broadcast your message")
        return
    data = load_data()
    count = 0
    for u in data:
        try:
            await context.bot.send_message(chat_id=f"@{u}", text=msg)
            count += 1
        except:
            pass
    await update.message.reply_text(f"✅ Message sent to {count} users.")

# --- [SUPPORT MESSAGES] ---
async def handle_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id == ADMIN_ID and user.id in reply_context:
        target = reply_context.pop(user.id)
        await context.bot.send_message(chat_id=f"@{target}", text=update.message.text)
        await update.message.reply_text("✅ Message sent.")
    elif user.username:
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"Message from @{user.username}:\n\n{update.message.text}",
                                       reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Reply", callback_data=f"reply:{user.username}")]]))
        await update.message.reply_text("✅ Message sent to admin.")
    else:
        await update.message.reply_text("❌ Username required to contact support.")

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
application.add_handler(CommandHandler("convertpdf", convert_pdf))
application.add_handler(CommandHandler("upgrade", upgrade))
application.add_handler(CommandHandler("downgrade", downgrade))
application.add_handler(CommandHandler("export", export_users))
application.add_handler(CommandHandler("stats", stats))
application.add_handler(CommandHandler("broadcast", broadcast))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video))
application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
application.add_handler(MessageHandler(filters.TEXT, handle_support))
application.add_handler(CallbackQueryHandler(handle_button))

if __name__ == "__main__":
    web.run_app(web_app, port=PORT)
