import os
import logging
import uuid
import shutil
from datetime import datetime, timedelta
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from PIL import Image
import asyncpg
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))  # Replace with your Telegram ID
DATABASE_URL = os.getenv("DATABASE_URL")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

db_pool = None
pdf_image_store = {}

logging.basicConfig(level=logging.INFO)


async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                name TEXT,
                plan TEXT DEFAULT 'free',
                expires TIMESTAMP,
                downloads JSONB DEFAULT '{}'
            )
        """)


async def get_user(user_id):
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
        if not user:
            await conn.execute(
                "INSERT INTO users (user_id, name) VALUES ($1, $2)",
                user_id, "Unknown"
            )
            user = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
        return dict(user)


async def update_user(user_id, field, value):
    async with db_pool.acquire() as conn:
        await conn.execute(f"UPDATE users SET {field}=$1 WHERE user_id=$2", value, user_id)


def is_upgraded(user):
    return user["plan"] == "upgraded" and user["expires"] and user["expires"] > datetime.utcnow()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id)
    text = (
        f"Hi {update.effective_user.first_name}, welcome!\n\n"
        "Free users are limited to 3 video downloads per day and 1 image-to-PDF conversion.\n"
        "Upgrade to remove all limits."
    )
    buttons = [
        [InlineKeyboardButton("View Profile", callback_data="view_profile")],
        [InlineKeyboardButton("Convert Images to PDF", callback_data="convert_pdf")]
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def view_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = await get_user(query.from_user.id)
    plan = user["plan"]
    expires = user["expires"]
    text = f"**Your Profile**\n\nPlan: {plan.capitalize()}"
    if plan == "upgraded":
        text += f"\nExpires: {expires.strftime('%Y-%m-%d %H:%M:%S')}"
    buttons = [
        [
            InlineKeyboardButton("Convert Images to PDF", callback_data="convert_pdf"),
            InlineKeyboardButton("Upgrade Plan", callback_data="upgrade"),
        ]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")


async def upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    buttons = [
        [
            InlineKeyboardButton("1 Day", callback_data="upgrade_1"),
            InlineKeyboardButton("7 Days", callback_data="upgrade_7"),
            InlineKeyboardButton("30 Days", callback_data="upgrade_30")
        ]
    ]
    await query.edit_message_text("Choose upgrade duration:", reply_markup=InlineKeyboardMarkup(buttons))


async def handle_upgrade_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    duration = int(query.data.split("_")[1])
    user_id = query.from_user.id
    user = await get_user(user_id)
    new_expiry = datetime.utcnow() + timedelta(days=duration)
    await update_user(user_id, "plan", "upgraded")
    await update_user(user_id, "expires", new_expiry)
    await query.edit_message_text(f"Your plan has been upgraded for {duration} day(s).")


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id)
    if not is_upgraded(user):
        downloads = user["downloads"] or {}
        today = datetime.utcnow().strftime("%Y-%m-%d")
        if downloads.get(today, 0) >= 3:
            await update.message.reply_text("Free download limit (3/day) reached. Please upgrade.")
            return
        downloads[today] = downloads.get(today, 0) + 1
        await update_user(update.effective_user.id, "downloads", downloads)

    file = await update.message.video.get_file()
    unique_name = f"{uuid.uuid4()}.mp4"
    await file.download_to_drive(unique_name)
    with open(unique_name, "rb") as f:
        await update.message.reply_video(
            video=f,
            caption="Video downloaded.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Convert to Audio", callback_data=f"convert_audio|{unique_name}")]
            ])
        )
    context.job_queue.run_once(delete_file, 60, data=unique_name)


async def convert_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, file_path = query.data.split("|")
    if not os.path.exists(file_path):
        await query.edit_message_caption("The file has been deleted. Please resend the link to download and convert again within 1 minute.")
        return
    mp3_file = file_path.replace(".mp4", ".mp3")
    os.system(f"ffmpeg -i {file_path} -q:a 0 -map a {mp3_file} -y")
    with open(mp3_file, "rb") as f:
        await query.message.reply_audio(audio=f, caption="Here's the audio.")
    os.remove(mp3_file)


async def delete_file(context: ContextTypes.DEFAULT_TYPE):
    file_path = context.job.data
    if os.path.exists(file_path):
        os.remove(file_path)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    photo = update.message.photo[-1]
    file = await photo.get_file()
    img_dir = f"images/{user_id}"
    os.makedirs(img_dir, exist_ok=True)
    img_path = f"{img_dir}/{uuid.uuid4()}.jpg"
    await file.download_to_drive(img_path)
    pdf_image_store.setdefault(user_id, []).append(img_path)
    await update.message.reply_text("Image received. Send more or tap 'Convert to PDF'.")


async def convert_pdf_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user = await get_user(user_id)

    if not is_upgraded(user):
        downloads = user["downloads"] or {}
        today = datetime.utcnow().strftime("%Y-%m-%d")
        if downloads.get("pdf", {}).get(today, 0) >= 1:
            await query.edit_message_text("Free PDF conversion limit (1/day) reached. Please upgrade.")
            return
        downloads.setdefault("pdf", {})[today] = 1
        await update_user(user_id, "downloads", downloads)

    image_paths = pdf_image_store.get(user_id, [])
    if not image_paths:
        await query.edit_message_text("No images found. Please send images first.")
        return

    images = [Image.open(p).convert("RGB") for p in image_paths]
    pdf_path = f"{uuid.uuid4()}.pdf"
    images[0].save(pdf_path, save_all=True, append_images=images[1:])
    with open(pdf_path, "rb") as f:
        await query.message.reply_document(document=InputFile(f), filename="converted.pdf", caption="Here is your PDF.")
    os.remove(pdf_path)
    shutil.rmtree(f"images/{user_id}", ignore_errors=True)
    pdf_image_store[user_id] = []


def main():
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(view_profile, pattern="view_profile"))
    application.add_handler(CallbackQueryHandler(upgrade, pattern="upgrade$"))
    application.add_handler(CallbackQueryHandler(handle_upgrade_choice, pattern="upgrade_"))
    application.add_handler(CallbackQueryHandler(convert_audio, pattern="convert_audio"))
    application.add_handler(CallbackQueryHandler(convert_pdf_trigger, pattern="convert_pdf"))

    application.add_handler(MessageHandler(filters.VIDEO, handle_video))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    application.run_webhook(
        listen="0.0.0.0",
        port=int(os.getenv("PORT", 8443)),
        webhook_url=WEBHOOK_URL
    )


if __name__ == "__main__":
    import asyncio
    asyncio.run(init_db())
    main()
