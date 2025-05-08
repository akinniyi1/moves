import os
import re
import ssl
import logging
import yt_dlp
import ffmpeg
import asyncpg
from datetime import datetime, timedelta

from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# SSL bypass for yt-dlp
ssl._create_default_https_context = ssl._create_unverified_context
logging.basicConfig(level=logging.INFO)

# Environment
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", 10000))
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = 1378825382

application = Application.builder().token(BOT_TOKEN).build()

# ---------- Database Functions ----------

async def init_db():
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id BIGINT PRIMARY KEY,
            name TEXT,
            plan TEXT DEFAULT 'free',
            downloads JSONB DEFAULT '{}'::jsonb,
            expires DATE
        )
    """)
    await conn.close()

async def get_user(user_id):
    conn = await asyncpg.connect(DATABASE_URL)
    row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
    if not row:
        await conn.execute("INSERT INTO users (id) VALUES ($1)", user_id)
        row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
    await conn.close()
    return dict(row)

async def update_user(user_id, data: dict):
    conn = await asyncpg.connect(DATABASE_URL)
    fields = []
    values = []
    for idx, (k, v) in enumerate(data.items(), start=1):
        fields.append(f"{k} = ${idx}")
        values.append(v)
    query = f"UPDATE users SET {', '.join(fields)} WHERE id = ${len(values)+1}"
    values.append(user_id)
    await conn.execute(query, *values)
    await conn.close()

async def can_download(user_id):
    user = await get_user(user_id)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    downloads = user["downloads"] or {}
    downloads_today = downloads.get(today, 0)

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
    downloads = user["downloads"] or {}
    downloads[today] = downloads.get(today, 0) + 1
    await update_user(user_id, {"downloads": downloads})

# ---------- Bot Helpers ----------

def is_valid_url(text):
    return re.match(r'https?://', text)

def convert_to_audio(video_path, audio_path):
    try:
        ffmpeg.input(video_path).output(audio_path, format='mp3').run(overwrite_output=True)
        return True
    except Exception as e:
        logging.error(f"Audio conversion failed: {e}")
        return False

# ---------- Bot Handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update_user(user.id, {"name": user.first_name or ""})

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 View Profile", callback_data="profile")],
        [InlineKeyboardButton("👥 Total Users", callback_data="total_users")] if user.id == ADMIN_ID else []
    ])

    await update.message.reply_text(
        f"👋 Hello {user.first_name or 'there'}! Send me a video link to download.\n\n"
        "🎵 After download, you can convert it to audio.\n"
        "🧾 You can also check your plan via 'View Profile'.",
        reply_markup=keyboard
    )

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    user = update.effective_user

    if not is_valid_url(url):
        await update.message.reply_text("❌ That doesn't look like a valid link.")
        return

    if not await can_download(user.id):
        await update.message.reply_text("⛔ You've reached your daily limit for downloads.")
        return

    status_msg = await update.message.reply_text("📥 Downloading video...")
    video_filename = "video.mp4"
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
                        status_msg.edit_text(f"📦 Downloading... {percent}%")
                    )

    ydl_opts = {
        'progress_hooks': [progress_hook],
        'outtmpl': video_filename,
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'quiet': True,
        'geo_bypass': True,
        'nocheckcertificate': True,
        'http_headers': {'User-Agent': 'Mozilla/5.0'}
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        await log_download(user.id)
        await status_msg.edit_text("✅ Download complete.")

        with open(video_filename, 'rb') as f:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🎵 Convert to Audio", callback_data=f"convert_audio:{video_filename}")]
            ])
            await update.message.reply_video(f, caption="🎉 Here's your video!", reply_markup=keyboard)

    except Exception as e:
        logging.error(f"Download failed: {e}")
        await status_msg.edit_text("❌ Failed to download this video.")

async def handle_audio_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    video_path = query.data.split(":", 1)[1]
    audio_path = "audio.mp3"

    if not os.path.exists(video_path):
        await query.edit_message_caption("❌ Video file not found.")
        return

    success = convert_to_audio(video_path, audio_path)
    if not success:
        await query.edit_message_caption("❌ Audio conversion failed.")
        return

    with open(audio_path, 'rb') as f:
        await query.message.reply_audio(f, caption="🎧 Here is the audio version!")

    os.remove(video_path)
    os.remove(audio_path)

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
        conn = await asyncpg.connect(DATABASE_URL)
        count = await conn.fetchval("SELECT COUNT(*) FROM users")
        await conn.close()
        await query.message.reply_text(f"👥 Total users: {count}")

    elif data.startswith("upgrade:"):
        _, username, days = data.split(":")
        days = int(days)
        conn = await asyncpg.connect(DATABASE_URL)
        row = await conn.fetchrow("SELECT * FROM users WHERE LOWER(name) = $1", username.lower())
        if row:
            expiry = (datetime.utcnow() + timedelta(days=days)).date()
            await conn.execute("UPDATE users SET plan = 'paid', expires = $1 WHERE id = $2", expiry, row["id"])
            await query.message.reply_text(f"✅ {username} upgraded for {days} days.")
        else:
            await query.message.reply_text("❌ User not found.")
        await conn.close()

async def upgrade_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Not authorized.")
        return

    if len(context.args) != 1:
        await update.message.reply_text("Usage: /upgrade <username>")
        return

    username = context.args[0]
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("5 Days", callback_data=f"upgrade:{username}:5"),
         InlineKeyboardButton("10 Days", callback_data=f"upgrade:{username}:10"),
         InlineKeyboardButton("30 Days", callback_data=f"upgrade:{username}:30")]
    ])
    await update.message.reply_text(f"Select upgrade duration for {username}:", reply_markup=keyboard)

# ---------- Webhook Setup ----------

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
    await init_db()
    await application.initialize()
    await application.start()
    webhook_url = f"{APP_URL}/webhook"
    await application.bot.set_webhook(webhook_url)
    logging.info(f"✅ Webhook set: {webhook_url}")

async def on_cleanup(app):
    await application.stop()
    await application.shutdown()

web_app.on_startup.append(on_startup)
web_app.on_cleanup.append(on_cleanup)

# ---------- Handlers Registration ----------

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("upgrade", upgrade_user))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video))
application.add_handler(CallbackQueryHandler(handle_audio_callback, pattern="^convert_audio:"))
application.add_handler(CallbackQueryHandler(handle_inline_buttons))

# ---------- Run App ----------

if __name__ == "__main__":
    web.run_app(web_app, port=PORT)
