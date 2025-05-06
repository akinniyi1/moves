import os
import logging
import ssl
import re
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
import yt_dlp

# Set up SSL context to avoid CERTIFICATE_VERIFY_FAILED
ssl._create_default_https_context = ssl._create_unverified_context

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Get token from environment
TOKEN = os.getenv("BOT_TOKEN")

# URL validation function
def is_valid_url(url: str) -> bool:
    return re.match(r'https?://', url) is not None

# Start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hello! Send me any social media video link (YouTube, TikTok, Instagram, Twitter, Facebook) and I’ll download it for you."
    )

# Handle video links
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if not is_valid_url(url):
        await update.message.reply_text("❌ That doesn't look like a valid link. Please send a valid video URL.")
        return

    await update.message.reply_text("📥 Downloading video, please wait...")

    ydl_opts = {
        'outtmpl': 'downloaded.%(ext)s',
        'format': 'best[ext=mp4]/best',
        'quiet': True,
        'nocheckcertificate': True,
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'geo_bypass': True,
        'source_address': '0.0.0.0',
        'no_color': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            file_name = ydl.prepare_filename(info)

        # Send the video back to user
        with open(file_name, 'rb') as video:
            await update.message.reply_video(video=video, caption="✅ Here's your video!")

        os.remove(file_name)

    except Exception as e:
        logging.error(f"Download error: {e}")
        await update.message.reply_text("❌ Failed to download the video. Try another link or check the URL.")

# Start bot
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video))
    print("Bot is running...")
    app.run_polling()
