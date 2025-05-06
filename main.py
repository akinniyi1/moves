import os
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
import yt_dlp

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

TOKEN = os.getenv("BOT_TOKEN")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎬 Send me any social media video link (YouTube, TikTok, Twitter, etc) and I’ll fetch the video for you.")

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    await update.message.reply_text("📥 Downloading, please wait...")

    ydl_opts = {
        'outtmpl': 'downloaded.%(ext)s',
        'format': 'best[ext=mp4]/best',
        'quiet': True,
        'nocheckcertificate': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            file_name = ydl.prepare_filename(info)

        # Send the downloaded video
        with open(file_name, 'rb') as f:
            await update.message.reply_video(video=f, caption="✅ Done!")

        os.remove(file_name)  # Clean up

    except Exception as e:
        logging.error(e)
        await update.message.reply_text("❌ Failed to download the video. Please check the link or try another one.")

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video))
    print("Bot is running...")
    app.run_polling()
