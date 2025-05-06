import os
import logging
import subprocess
import uuid
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create downloads folder
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Command handler
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"👋 Hi {update.effective_user.first_name}! Send me any video or photo link to download it.\n\nFor videos, you can also extract audio after download!")

# Process video/photo links
async def download_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    user_id = update.effective_user.id
    logger.info(f"[{user_id}] Requested: {url}")

    if not url.startswith("http"):
        await update.message.reply_text("❗️Please send a valid media link.")
        return

    # Create a unique filename
    file_id = str(uuid.uuid4())
    output_path = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")

    # Try photo first
    try:
        if any(url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']):
            photo_path = os.path.join(DOWNLOAD_DIR, f"{file_id}.jpg")
            subprocess.run(["wget", "-O", photo_path, url], check=True)
            await update.message.reply_photo(photo=open(photo_path, "rb"))
            os.remove(photo_path)
            return
    except Exception as e:
        logger.warning(f"Image download failed: {e}")

    # Otherwise, try video
    try:
        await update.message.reply_text("⏬ Downloading video, please wait...")
        result = subprocess.run([
            "yt-dlp",
            "-o", output_path,
            url
        ], capture_output=True, text=True)

        if result.returncode != 0:
            raise Exception(result.stderr)

        # Find downloaded file
        for fname in os.listdir(DOWNLOAD_DIR):
            if fname.startswith(file_id) and not fname.endswith(".part"):
                video_path = os.path.join(DOWNLOAD_DIR, fname)
                break
        else:
            raise Exception("No video file found.")

        with open(video_path, "rb") as f:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🎵 Convert to Audio", callback_data=f"audio|{video_path}")
            ]])
            await update.message.reply_video(f, caption="✅ Video downloaded.", reply_markup=keyboard)

    except Exception as e:
        logger.error(f"Video download error: {e}")
        await update.message.reply_text(f"❌ Error downloading: {e}")

# Handle inline button callback to convert video to audio
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if data.startswith("audio|"):
        video_path = data.split("|", 1)[1]
        audio_path = video_path.rsplit(".", 1)[0] + ".mp3"

        try:
            subprocess.run([
                "ffmpeg", "-i", video_path,
                "-vn", "-ab", "192k", "-ar", "44100",
                "-y", audio_path
            ], check=True)

            with open(audio_path, "rb") as f:
                await query.message.reply_audio(f, caption="🎧 Here is your audio!")
        except Exception as e:
            logger.error(f"Audio conversion error: {e}")
            await query.message.reply_text("❌ Failed to convert to audio.")

# Run the bot
if __name__ == "__main__":
    TOKEN = os.getenv("BOT_TOKEN")  # Set this in your environment or .env file
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_handler))
    app.add_handler(CallbackQueryHandler(button_handler))

    app.run_polling()
