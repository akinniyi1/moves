import os
import re
import logging
import ssl
import yt_dlp
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# Patch SSL
ssl._create_default_https_context = ssl._create_unverified_context

# Logging
logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")

def is_valid_url(text):
    return re.match(r'https?://', text)

# Start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Send any video link (YouTube, TikTok, Facebook, Instagram, etc.)")

# Video handler
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if not is_valid_url(url):
        await update.message.reply_text("❌ That doesn't look like a valid video link.")
        return

    await update.message.reply_text("📥 Downloading video...")

    ydl_opts = {
        'quiet': True,
        'outtmpl': 'video.%(ext)s',
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'geo_bypass': True,
        'nocheckcertificate': True,
        'concurrent_fragment_downloads': 10,
        'retries': 5,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
        },
        'postprocessors': [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4'
        }]
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            file_path = ydl.prepare_filename(info)

        with open(file_path, 'rb') as f:
            await update.message.reply_video(video=f, caption="✅ Here's your video!")

        os.remove(file_path)

    except Exception as e:
        logging.error(f"Failed to download: {e}")
        await update.message.reply_text("❌ Failed to download. This might be a private, region-locked, or invalid link.")

# Main runner
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video))
    app.run_polling()
