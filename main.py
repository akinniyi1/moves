# main.py
import os
import re
import ssl
import json
import uuid
import shutil
import logging
import asyncio
import yt_dlp
import ffmpeg
import tempfile
import psycopg2
from datetime import datetime, timedelta

from telegram import (
    Bot, Update, InlineKeyboardButton,
    InlineKeyboardMarkup, InputMediaDocument
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, CallbackQueryHandler, ContextTypes
)

# === CONFIG ===
TOKEN = os.environ.get("BOT_TOKEN")
DB_URL = os.environ.get("DATABASE_URL")
ADMIN_ID = 1378825382
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", 8443))

ssl._create_default_https_context = ssl._create_unverified_context

# === DB SETUP ===
def get_conn():
    return psycopg2.connect(DB_URL, sslmode="require")

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT,
                telegram_id BIGINT UNIQUE,
                plan TEXT DEFAULT 'free',
                downloads JSONB DEFAULT '[]',
                image_trials INT DEFAULT 0,
                music_trials INT DEFAULT 0,
                referral_code TEXT,
                referred_by TEXT,
                upgrade_expiry TIMESTAMP
            )""")
            conn.commit()

# === USER HELPERS ===
def get_or_create_user(user):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE telegram_id = %s", (user.id,))
            if not cur.fetchone():
                cur.execute("""
                    INSERT INTO users (username, telegram_id, referral_code)
                    VALUES (%s, %s, %s)
                """, (user.username, user.id, str(uuid.uuid4())[:8]))
                conn.commit()

def is_upgraded(user_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT upgrade_expiry FROM users WHERE telegram_id = %s", (user_id,))
            result = cur.fetchone()
            return result and result[0] and result[0] > datetime.utcnow()

# === START & PROFILE ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_or_create_user(user)
    keyboard = [
        [InlineKeyboardButton("View Profile", callback_data="profile")],
        [InlineKeyboardButton("Image to PDF", callback_data="pdf")],
        [InlineKeyboardButton("Search Music", callback_data="music")],
    ]
    await update.message.reply_text(
        f"Welcome {user.username or user.first_name}!\n\n"
        "Free users: 3 video downloads/day, 1 image-to-PDF trial, 1 music download.\n"
        "Upgrade to remove limits.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def view_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT plan, upgrade_expiry, referral_code FROM users WHERE telegram_id = %s", (user.id,))
            plan, expiry, ref_code = cur.fetchone()
    msg = f"Username: @{user.username}\nPlan: {plan}\nReferral Code: {ref_code}"
    if expiry:
        msg += f"\nUpgrade expires: {expiry.strftime('%Y-%m-%d %H:%M:%S')} UTC"
    await update.callback_query.edit_message_text(msg)

# === IMAGE TO PDF ===
user_images = {}

async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    get_or_create_user(update.effective_user)
    if user_id not in user_images:
        user_images[user_id] = []
    file = await update.message.photo[-1].get_file()
    path = f"{tempfile.gettempdir()}/{uuid.uuid4()}.jpg"
    await file.download_to_drive(path)
    user_images[user_id].append(path)
    await update.message.reply_text("Image received. Click below to convert to PDF:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Convert to PDF", callback_data="convert_pdf")]]))

async def convert_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT image_trials FROM users WHERE telegram_id = %s", (user.id,))
            trials = cur.fetchone()[0]
            upgraded = is_upgraded(user.id)
            if trials >= 1 and not upgraded:
                await update.callback_query.answer("Trial used. Upgrade to unlock more.", show_alert=True)
                return
            if not upgraded:
                cur.execute("UPDATE users SET image_trials = image_trials + 1 WHERE telegram_id = %s", (user.id,))
                conn.commit()
    images = user_images.get(user.id, [])
    if not images:
        await update.callback_query.answer("No images to convert.")
        return
    from fpdf import FPDF
    pdf = FPDF()
    for img in images:
        pdf.add_page()
        pdf.image(img, x=10, y=10, w=180)
    output = f"{tempfile.gettempdir()}/{uuid.uuid4()}.pdf"
    pdf.output(output)
    await context.bot.send_document(chat_id=user.id, document=open(output, "rb"))
    for img in images: os.remove(img)
    os.remove(output)
    user_images[user.id] = []

# === YOUTUBE MUSIC SEARCH ===
async def music_feature(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("Send the music title or artist name you want to search:")

async def handle_music_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    keyword = update.message.text
    get_or_create_user(user)
    upgraded = is_upgraded(user.id)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT music_trials FROM users WHERE telegram_id = %s", (user.id,))
            trials = cur.fetchone()[0]
            if trials >= 1 and not upgraded:
                await update.message.reply_text("Free music download used. Upgrade to continue.")
                return
            if not upgraded:
                cur.execute("UPDATE users SET music_trials = music_trials + 1 WHERE telegram_id = %s", (user.id,))
                conn.commit()

    filename = f"{uuid.uuid4()}.mp3"
    filepath = os.path.join(tempfile.gettempdir(), filename)
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": filepath,
        "quiet": True,
        "noplaylist": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192"
        }]
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{keyword}", download=True)
            title = info['entries'][0]['title'] if 'entries' in info else info['title']
        await update.message.reply_audio(audio=open(filepath, 'rb'), title=title)
        await asyncio.sleep(60)
        os.remove(filepath)
    except Exception as e:
        await update.message.reply_text("Failed to download music.")

# === ADMIN: UPGRADE ===
async def upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if len(args) != 1:
        await update.message.reply_text("Usage: /upgrade <username>")
        return
    username = args[0]
    keyboard = [[
        InlineKeyboardButton("1 Day", callback_data=f"ug_{username}_1"),
        InlineKeyboardButton("7 Days", callback_data=f"ug_{username}_7"),
        InlineKeyboardButton("30 Days", callback_data=f"ug_{username}_30"),
    ]]
    await update.message.reply_text(f"Upgrade @{username} for how long?", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_upgrade_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    if not data.startswith("ug_"):
        return
    _, username, days = data.split("_")
    expiry = datetime.utcnow() + timedelta(days=int(days))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET plan = 'premium', upgrade_expiry = %s WHERE username = %s", (expiry, username))
            conn.commit()
    await update.callback_query.edit_message_text(f"@{username} upgraded for {days} days.")

# === MAIN ===
def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("upgrade", upgrade))
    app.add_handler(CallbackQueryHandler(view_profile, pattern="profile"))
    app.add_handler(CallbackQueryHandler(convert_pdf, pattern="convert_pdf"))
    app.add_handler(CallbackQueryHandler(music_feature, pattern="music"))
    app.add_handler(CallbackQueryHandler(handle_upgrade_button, pattern="ug_"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_image))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_music_search))

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=WEBHOOK_URL + f"/{TOKEN}"
    )

if __name__ == "__main__":
    main()
