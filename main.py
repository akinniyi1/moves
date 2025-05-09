import os
import re
import ssl
import json
import logging
import yt_dlp
import mimetypes
import requests
import ffmpeg
import asyncpg
import asyncio
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
user_states = {}
user_files = {}

# ---------- DB HELPERS ----------

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

# ---------- BOT HELPERS ----------

def is_valid_url(text):
    return re.match(r'https?://', text)

def generate_filename(ext="mp4"):
    return f"video_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}.{ext}"

async def auto_delete(file_path, user_id):
    await asyncio.sleep(60)
    if os.path.exists(file_path):
        os.remove(file_path)
    if user_id in user_files and user_files[user_id] == file_path:
        del user_files[user_id]

# ---------- HANDLERS ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update_user(user.id, {"name": user.first_name or ""})

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("👤 View Profile", callback_data="profile"),
        InlineKeyboardButton("🎵 Convert to Audio", callback_data="convert_audio")
    ]] + ([[InlineKeyboardButton("👥 Total Users", callback_data="total_users")]] if user.id == ADMIN_ID else []))

    await update.message.reply_text(
        f"👋 Hello {user.first_name or 'there'}! Send me a video link to download.",
        reply_markup=keyboard
    )

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    url = update.message.text.strip()

    if not is_valid_url(url):
        await update.message.reply_text("❌ That doesn't look like a valid link.")
        return

    if not await can_download(user.id):
        await update.message.reply_text("⛔ You've reached your daily limit.")
        return

    filename = generate_filename()
    status_msg = await update.message.reply_text("📥 Downloading video...")
    progress_state = {'last_percent': 0}

    def progress_hook(d):
        if d['status'] == 'downloading':
            total = d.get('_total_bytes_estimate') or d.get('total_bytes') or 0
            downloaded = d.get('downloaded_bytes') or 0
            if total > 0:
                percent = int(downloaded * 100 / total)
                if percent - progress_state['last_percent'] >= 10:
                    progress_state['last_percent'] = percent
                    context.application.create_task(
                        status_msg.edit_text(f"⏬ Downloading... {percent}%")
                    )

    ydl_opts = {
        'progress_hooks': [progress_hook],
        'outtmpl': filename,
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'quiet': True,
        'geo_bypass': True,
        'nocheckcertificate': True,
        'http_headers': {'User-Agent': 'Mozilla/5.0'},
        'max_filesize': 50 * 1024 * 1024  # 50 MB
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        await log_download(user.id)
        await status_msg.edit_text("✅ Download complete.")

        with open(filename, 'rb') as f:
            await update.message.reply_video(f, caption="🎉 Here's your video!")
        user_files[user.id] = filename
        context.application.create_task(auto_delete(filename, user.id))
    except Exception as e:
        logging.error(f"Download error: {e}")
        await status_msg.edit_text("⚠️ Download failed. The file may be too large.")

async def handle_inline_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    await query.answer()

    if data == "profile":
        user = await get_user(user_id)
        plan = user["plan"]
        expires = user.get("expires")
        expiry_text = f"\n⏳ Expires: {expires}" if expires else ""
        await query.message.reply_text(f"👤 Profile for {user.get('name', '')}\n💼 Plan: {plan}{expiry_text}")

    elif data == "total_users" and user_id == ADMIN_ID:
        async with db_pool.acquire() as conn:
            count = await conn.fetchval("SELECT COUNT(*) FROM users")
            await query.message.reply_text(f"👥 Total users: {count}")

    elif data == "convert_audio":
        if user_id not in user_files or not os.path.exists(user_files[user_id]):
            await query.message.reply_text("❌ The file has been deleted. Please resend the video link and convert to audio within 1 minute to avoid loss.")
            return

        video_path = user_files[user_id]
        audio_path = video_path.replace(".mp4", ".mp3")

        try:
            ffmpeg.input(video_path).output(audio_path).run(overwrite_output=True)
            with open(audio_path, 'rb') as f:
                await query.message.reply_audio(f, title="🎧 Your audio is ready!")
            os.remove(audio_path)
        except Exception as e:
            logging.error(f"Audio conversion error: {e}")
            await query.message.reply_text("⚠️ Failed to convert to audio.")

async def upgrade_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Not authorized.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /upgrade <username>")
        return
    username = context.args[0]
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("5 Days", callback_data=f"upgrade:{username}:5"),
        InlineKeyboardButton("10 Days", callback_data=f"upgrade:{username}:10"),
        InlineKeyboardButton("30 Days", callback_data=f"upgrade:{username}:30")
    ]])
    await update.message.reply_text(f"Select upgrade duration for {username}:", reply_markup=keyboard)

# ---------- WEBHOOK SETUP ----------

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
    webhook_url = f"{APP_URL}/webhook"
    await application.bot.set_webhook(webhook_url)
    logging.info(f"✅ Webhook set: {webhook_url}")

async def on_cleanup(app):
    await application.stop()
    await application.shutdown()
    await db_pool.close()

web_app.on_startup.append(on_startup)
web_app.on_cleanup.append(on_cleanup)

# ---------- REGISTER HANDLERS ----------

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("upgrade", upgrade_user))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video))
application.add_handler(CallbackQueryHandler(handle_inline_buttons))

# ---------- RUN ----------

if __name__ == "__main__":
    web.run_app(web_app, port=PORT)
