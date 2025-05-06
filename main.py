import os
import ssl
import re
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
import yt_dlp

# Avoid SSL errors
ssl._create_default_https_context = ssl._create_unverified_context

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

TOKEN = os.getenv("BOT_TOKEN")

# URL checker
def is_valid_url(url):
    return re.match(r'https?://', url)

# Start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Send me any social media video link and I’ll download it for you.")

# Main handler
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if not is_valid_url(url):
        await update.message.reply_text("❌ That doesn't look like a valid link.")
        return

    await update.message.reply_text("📥 Downloading... please wait.")

    ydl_opts = {
        'outtmpl': 'video.%(ext)s',
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'quiet': True,
        'noplaylist': True,
        'nocheckcertificate': True,
        'geo_bypass': True,
        'source_address': '0.0.0.0',
        'retries': 3,
        'concurrent_fragment_downloads': 5,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0',
        },
        'no_color': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            file_path = ydl.prepare_filename(info)

        with open(file_path, 'rb') as video:
            await update.message.reply_video(video=video, caption="✅ Here's your video!")

        os.remove(file_path)

    except Exception as e:
        logging.error(f"Download failed: {e}")
        await update.message.reply_text("❌ Couldn't download the video. Try another link or wait a bit.")

# App init
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video))

    logging.info("Bot running...")

    # Background worker mode for Render
    import asyncio
    asyncio.run(app.run_polling())
