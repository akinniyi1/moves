# --- [IMPORTS & SETUP] ---
import os
import re
import ssl
import json
import logging
import yt_dlp
import ffmpeg
import asyncio
import asyncpg
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
DB_URL = os.getenv("DATABASE_URL")

application = Application.builder().token(BOT_TOKEN).build()
db_pool = None
file_registry = {}
image_collections = {}
pdf_trials = {}
music_trials = {}

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

# --- [DATABASE] ---
async def get_user(user_id):
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
        if not user:
            await conn.execute(
                "INSERT INTO users (id, name, plan, downloads, expires) VALUES ($1, $2, $3, $4, $5)",
                user_id, "", "free", json.dumps({}), None
            )
            return {"id": user_id, "name": "", "plan": "free", "downloads": {}, "expires": None}
        downloads = user["downloads"]
        if isinstance(downloads, str):
            try:
                downloads = json.loads(downloads)
            except:
                downloads = {}
        return {
            "id": user["id"],
            "name": user["name"],
            "plan": user["plan"],
            "downloads": downloads,
            "expires": user["expires"]
        }

async def update_user(user_id, data):
    async with db_pool.acquire() as conn:
        user = await get_user(user_id)
        user.update(data)
        downloads_json = json.dumps(user["downloads"])
        await conn.execute(
            """
            INSERT INTO users (id, name, plan, downloads, expires)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (id) DO UPDATE SET
              name = $2, plan = $3, downloads = $4, expires = $5
            """,
            user["id"], user["name"], user["plan"], downloads_json, user["expires"]
        )

async def can_download(user_id):
    user = await get_user(user_id)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    downloads_today = user["downloads"].get(today, 0)
    if user["plan"] == "free":
        return downloads_today < 3
    else:
        expiry = user.get("expires")
        if expiry and expiry < datetime.utcnow().date():
            await update_user(user_id, {"plan": "free", "expires": None})
            return downloads_today < 3
        return True

async def log_download(user_id):
    user = await get_user(user_id)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    downloads = user["downloads"]
    downloads[today] = downloads.get(today, 0) + 1
    await update_user(user_id, {"downloads": downloads})

# --- [VIDEO DOWNLOAD & AUDIO CONVERT] ---
async def convert_to_audio(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path):
    audio_path = file_path.replace(".mp4", ".mp3")
    try:
        ffmpeg.input(file_path).output(audio_path).run(overwrite_output=True)
        with open(audio_path, 'rb') as f:
            await update.callback_query.message.reply_audio(f, filename=os.path.basename(audio_path))
        os.remove(audio_path)
    except:
        await update.callback_query.message.reply_text("❌ Failed to convert to audio.")

# --- [HANDLERS] ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update_user(user.id, {"name": user.username or user.first_name or ""})
    buttons = [
        [InlineKeyboardButton("👤 View Profile", callback_data="profile"),
         InlineKeyboardButton("🖼️ Convert to PDF", callback_data="convertpdf_btn")],
        [InlineKeyboardButton("👥 Total Users", callback_data="total_users")] if user.id == ADMIN_ID else []
    ]
    await update.message.reply_text(
        f"👋 Hello {user.first_name or 'there'}! Send me a video link or music name.\n\n"
        "🎵 Send artist or song name to download music (1 free trial).\n"
        "📌 Free: 3 video downloads/day, 1 music download, 1 PDF trial.\n",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    url = update.message.text.strip()
    if not is_valid_url(url):
        await search_youtube_audio(update, context)
        return
    if not await can_download(user.id):
        await update.message.reply_text("⛔ Daily limit reached.")
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
        await log_download(user.id)
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
        await status_msg.edit_text("⚠️ Download failed or file too large.")

async def search_youtube_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if await get_user(user_id) and music_trials.get(user_id, 0) >= 1:
        await update.message.reply_text("⛔ Free users can download music only once.")
        return
    query = update.message.text.strip()
    if not query:
        await update.message.reply_text("❌ Please send a song or artist name.")
        return
    await update.message.reply_text(f"🎶 Searching for \"{query}\"...")
    filename = generate_filename("mp3")
    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'noplaylist': True,
        'default_search': 'ytsearch1',
        'outtmpl': filename,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([query])
        with open(filename, 'rb') as f:
            await update.message.reply_audio(f, filename=os.path.basename(filename), caption="🎧 Music downloaded!")
        asyncio.create_task(delete_file_later(filename))
        music_trials[user_id] = 1
    except Exception as e:
        logging.error(e)
        await update.message.reply_text("❌ Failed to download music.")

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    if data == "profile":
        user = await get_user(user_id)
        exp = f"\n⏳ Expires: {user['expires']}" if user["expires"] else ""
        await query.message.reply_text(f"👤 Name: {user['name']}\n💼 Plan: {user['plan']}{exp}")
    elif data == "total_users" and user_id == ADMIN_ID:
        async with db_pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM users")
            await query.message.reply_text(f"👥 Total users: {total}")
    elif data == "convertpdf_btn":
        fake_msg = type("msg", (), {"message": query.message, "effective_user": query.from_user})
        await convert_pdf(fake_msg, context, triggered_by_button=True)
    elif data.startswith("audio:"):
        file = data.split("audio:")[1]
        if not os.path.exists(file):
            await query.message.reply_text("File deleted. Please resend the link.")
        else:
            await convert_to_audio(update, context, file)

async def convert_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, triggered_by_button=False):
    user_id = update.effective_user.id
    user = await get_user(user_id)
    if user["plan"] == "free":
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

async def upgrade_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Not authorized.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /upgrade <username>")
        return
    username = context.args[0]
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("1 Day", callback_data=f"upgrade:{username}:1"),
                                      InlineKeyboardButton("5 Days", callback_data=f"upgrade:{username}:5"),
                                      InlineKeyboardButton("10 Days", callback_data=f"upgrade:{username}:10"),
                                      InlineKeyboardButton("30 Days", callback_data=f"upgrade:{username}:30")]])
    await update.message.reply_text(f"Select upgrade duration for {username}:", reply_markup=keyboard)

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
    global db_pool
    db_pool = await asyncpg.create_pool(DB_URL)
    await application.initialize()
    await application.start()
    await application.bot.set_webhook(f"{APP_URL}/webhook")
    logging.info("✅ Webhook set.")

async def on_cleanup(app):
    await application.stop()
    await application.shutdown()
    await db_pool.close()

web_app.on_startup.append(on_startup)
web_app.on_cleanup.append(on_cleanup)

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("upgrade", upgrade_user))
application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video))
application.add_handler(CallbackQueryHandler(handle_button))

if __name__ == "__main__":
    web.run_app(web_app, port=PORT)
