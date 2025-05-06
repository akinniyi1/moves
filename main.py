import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import yt_dlp
import aiohttp
import ffmpeg
import tempfile

BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# Progress Hook
def progress_hook(d):
    if d['status'] == 'downloading':
        percent = d.get('_percent_str', '').strip()
        speed = d.get('_speed_str', '').strip()
        eta = d.get('eta', '')
        logging.info(f"Downloading: {percent} at {speed} ETA: {eta}s")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Welcome! Send me a video or photo link to download.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    temp_dir = tempfile.mkdtemp()
    ydl_opts = {
        'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
        'format': 'bestvideo+bestaudio/best',
        'progress_hooks': [progress_hook],
        'quiet': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            file_path = ydl.prepare_filename(info)

        with open(file_path, 'rb') as f:
            sent_msg = await update.message.reply_video(video=f, caption="✅ Video downloaded!")

        # Inline button to convert to audio
        keyboard = [
            [InlineKeyboardButton("🎧 Convert to Audio", callback_data=f"audio|{file_path}")]
        ]
        await update.message.reply_text("Choose an action:", reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def handle_audio_convert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("|")
    if data[0] != "audio":
        return

    file_path = data[1]
    audio_path = file_path.rsplit(".", 1)[0] + ".mp3"

    try:
        (
            ffmpeg
            .input(file_path)
            .output(audio_path, format='mp3', acodec='libmp3lame')
            .run(quiet=True, overwrite_output=True)
        )

        with open(audio_path, 'rb') as f:
            await query.message.reply_audio(audio=f, caption="🎧 Audio version")

    except Exception as e:
        await query.message.reply_text(f"❌ Audio conversion failed: {str(e)}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    file_path = os.path.join(tempfile.mkdtemp(), "photo.jpg")
    await file.download_to_drive(file_path)

    with open(file_path, "rb") as f:
        await update.message.reply_photo(photo=f, caption="✅ Photo downloaded!")

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_audio_convert))

    app.run_polling()
