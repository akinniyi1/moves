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
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
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
DATA_FILE = "/mnt/data/users.json"

application = Application.builder().token(BOT_TOKEN).build()

file_registry = {}
image_collections = {}
support_messages = {}

# --- [USER DATA HELPERS] ---
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)

def ensure_user(user):
    data = load_data()
    if user.username not in data:
        data[user.username] = {
            "plan": "free",
            "downloads": 0,
            "pdf_used": False,
            "expires": None,
            "total_downloads": 0
        }
        save_data(data)

def get_user(username):
    return load_data().get(username, {})

def update_user(username, new_data):
    data = load_data()
    data[username] = new_data
    save_data(data)

def check_expiry(username):
    data = load_data()
    user = data.get(username)
    if user and user["plan"] == "premium" and user["expires"]:
        if datetime.utcnow() > datetime.fromisoformat(user["expires"]):
            user["plan"] = "free"
            user["downloads"] = 0
            user["pdf_used"] = False
            user["expires"] = None
            update_user(username, user)

def get_expiry_display(user_data):
    if user_data["plan"] == "premium" and user_data["expires"]:
        dt = datetime.fromisoformat(user_data["expires"])
        return f"premium (expires: {dt.strftime('%Y-%m-%d %H:%M')})"
    return "free"

# --- [START] ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)
    buttons = [
        [InlineKeyboardButton("👤 View Profile", callback_data="profile"),
         InlineKeyboardButton("🖼️ Convert to PDF", callback_data="convertpdf_btn")],
        [InlineKeyboardButton("🆘 Contact Support", callback_data="support_btn")]
    ]
    await update.message.reply_text(
        f"👋 Hello @{user.username}!\n\n"
        "This bot supports:\n"
        "✅ Facebook, TikTok, Twitter, Instagram\n"
        "❌ YouTube is not supported\n\n"
        "Free Users:\n"
        "• 3 video downloads total\n"
        "• 1 PDF conversion trial\n\n"
        "Send a supported video link to begin.",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# --- [VIDEO DOWNLOAD] ---
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)
    check_expiry(user.username)
    user_data = get_user(user.username)

    url = update.message.text.strip()
    if not re.match(r'https?://', url):
        return await update.message.reply_text("❌ Invalid URL or unsupported platform.")
    if "youtube.com" in url or "youtu.be" in url:
        return await update.message.reply_text("❌ YouTube is not supported.")
    if user_data["plan"] == "free" and user_data["downloads"] >= 3:
        return await update.message.reply_text("⛔ Free users can only download 3 videos. Upgrade to continue.")

    filename = f"file_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}.mp4"
    msg = await update.message.reply_text("📥 Downloading...")
    try:
        with yt_dlp.YoutubeDL({
            'outtmpl': filename,
            'format': 'bestvideo+bestaudio/best',
            'merge_output_format': 'mp4',
            'quiet': True,
            'noplaylist': True,
            'max_filesize': 50 * 1024 * 1024
        }) as ydl:
            ydl.download([url])
        with open(filename, "rb") as f:
            sent = await update.message.reply_video(
                f,
                caption="🎉 Here's your video!",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎧 Convert to Audio", callback_data=f"audio:{filename}")]
                ])
            )
        file_registry[sent.message_id] = filename
        asyncio.create_task(delete_file_later(filename, sent.message_id))
        user_data["downloads"] += 1
        user_data["total_downloads"] += 1
        update_user(user.username, user_data)
        await msg.delete()
    except:
        await msg.edit_text("❌ Failed to download or file too large.")

# --- [INLINE BUTTONS] ---
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    ensure_user(user)
    check_expiry(user.username)
    data = query.data

    if data == "profile":
        user_data = get_user(user.username)
        await query.message.reply_text(
            f"👤 @{user.username}\n"
            f"💼 Plan: {get_expiry_display(user_data)}\n"
            f"⬇️ Total Downloads: {user_data['total_downloads']}"
        )
    elif data == "convertpdf_btn":
        fake_msg = type("msg", (), {"message": query.message, "effective_user": user})
        await convert_pdf(fake_msg, context)
    elif data.startswith("audio:"):
        file = data.split("audio:")[1]
        if not os.path.exists(file):
            await query.message.reply_text("File deleted. Please resend link.")
        else:
            await convert_to_audio(update, context, file)
    elif data == "support_btn":
        support_messages[user.id] = user.username
        await query.message.reply_text("✍️ Please send your message for admin.")
    elif data.startswith("reply:"):
        uid = int(data.split("reply:")[1])
        await context.bot.send_message(uid, f"💬 Admin: {query.message.text}")
        await query.message.reply_text("✅ Replied.")

# --- [CONVERT TO AUDIO] ---
async def convert_to_audio(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path):
    audio_path = file_path.replace(".mp4", ".mp3")
    try:
        ffmpeg.input(file_path).output(audio_path).run(overwrite_output=True)
        with open(audio_path, "rb") as f:
            await update.callback_query.message.reply_audio(f)
        os.remove(audio_path)
    except:
        await update.callback_query.message.reply_text("❌ Failed to convert.")

# --- [DELETE FILE] ---
async def delete_file_later(path, file_id=None):
    await asyncio.sleep(60)
    if os.path.exists(path):
        os.remove(path)
    if file_id:
        file_registry.pop(file_id, None)

# --- [PHOTO -> PDF] ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    file = await context.bot.get_file(update.message.photo[-1].file_id)
    path = f"image_{datetime.utcnow().strftime('%H%M%S%f')}.jpg"
    await file.download_to_drive(path)
    image_collections.setdefault(user_id, []).append(path)
    asyncio.create_task(delete_file_later(path))
    await update.message.reply_text("✅ Image received. Send more or click /convertpdf to generate PDF.")

async def convert_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    uname = update.effective_user.username
    ensure_user(update.effective_user)
    user_data = get_user(uname)

    if user_data["pdf_used"]:
        return await update.message.reply_text("⛔ Free users can only convert 1 PDF.")
    imgs = image_collections.get(uid, [])
    if not imgs:
        return await update.message.reply_text("❌ No images received.")
    try:
        pdf_path = f"file_{datetime.utcnow().strftime('%H%M%S%f')}.pdf"
        [Image.open(p).convert("RGB") for p in imgs][0].save(
            pdf_path, save_all=True, append_images=[Image.open(p).convert("RGB") for p in imgs][1:]
        )
        with open(pdf_path, "rb") as f:
            await update.message.reply_document(f, filename="converted.pdf")
        for img in imgs:
            os.remove(img)
        os.remove(pdf_path)
        image_collections[uid] = []
        user_data["pdf_used"] = True
        update_user(uname, user_data)
    except:
        await update.message.reply_text("❌ PDF generation failed.")

# --- [ADMIN COMMANDS] ---
async def upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        uname = context.args[0].lstrip("@")
        hours = int(context.args[1])
        data = get_user(uname)
        data["plan"] = "premium"
        data["expires"] = (datetime.utcnow() + timedelta(hours=hours)).isoformat()
        update_user(uname, data)
        await update.message.reply_text(f"✅ Upgraded @{uname} for {hours} hour(s).")
    except:
        await update.message.reply_text("❌ Usage: /upgrade username hours")

async def downgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        uname = context.args[0].lstrip("@")
        data = get_user(uname)
        data.update({"plan": "free", "downloads": 0, "pdf_used": False, "expires": None})
        update_user(uname, data)
        await update.message.reply_text(f"✅ Downgraded @{uname}.")
    except:
        await update.message.reply_text("❌ Usage: /downgrade username")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    data = load_data()
    total = len(data)
    free = sum(1 for u in data.values() if u["plan"] == "free")
    paid = total - free
    downloads = sum(u.get("total_downloads", 0) for u in data.values())
    await update.message.reply_text(
        f"📊 Stats:\nTotal Users: {total}\nFree: {free}\nPremium: {paid}\nDownloads: {downloads}"
    )

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    msg = " ".join(context.args)
    data = load_data()
    sent = 0
    for uname in data:
        try:
            await context.bot.send_message(chat_id=f"@{uname}", text=msg)
            sent += 1
        except:
            continue
    await update.message.reply_text(f"✅ Sent to {sent} users.")

async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    import csv
    path = "/mnt/data/export.csv"
    with open(path, "w", newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["Username", "Plan", "Expires", "Downloads"])
        for uname, u in load_data().items():
            writer.writerow([uname, u["plan"], u["expires"], u["total_downloads"]])
    await update.message.reply_document(InputFile(path, filename="users.csv"))

# --- [SUPPORT] ---
async def handle_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in support_messages:
        msg = update.message.text
        btn = InlineKeyboardMarkup([[InlineKeyboardButton("Reply User", callback_data=f"reply:{uid}")]])
        await context.bot.send_message(ADMIN_ID, f"✉️ Message from @{support_messages[uid]}:\n{msg}", reply_markup=btn)
        await update.message.reply_text("✅ Message sent to admin.")
        del support_messages[uid]

# --- [WEBHOOK SETUP] ---
web_app = web.Application()

async def webhook_handler(request):
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.update_queue.put(update)
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
application.add_handler(CommandHandler("upgrade", upgrade))
application.add_handler(CommandHandler("downgrade", downgrade))
application.add_handler(CommandHandler("stats", stats))
application.add_handler(CommandHandler("broadcast", broadcast))
application.add_handler(CommandHandler("export", export))
application.add_handler(CommandHandler("convertpdf", convert_pdf))
application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_support))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video))
application.add_handler(CallbackQueryHandler(handle_button))

if __name__ == "__main__":
    web.run_app(web_app, port=PORT)
