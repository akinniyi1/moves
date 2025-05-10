import os
import logging
import asyncio
import datetime
import tempfile
from uuid import uuid4
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    InputMediaPhoto
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import psycopg
from fpdf import FPDF

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment Variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # Full webhook URL
DB_URL = os.getenv("DATABASE_URL")
ADMIN_ID = 1378825382

# Connect to PostgreSQL
conn = asyncio.run(psycopg.AsyncConnection.connect(DB_URL))
cur = conn.cursor()

# Ensure table exists
async def setup_db():
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id BIGINT PRIMARY KEY,
            name TEXT,
            plan TEXT DEFAULT 'free',
            image_trials INT DEFAULT 0,
            upgrade_until TIMESTAMP,
            downloads JSON DEFAULT '[]'
        )
    """)
    await conn.commit()

# Helper: Get or create user
async def get_user(user_id, username):
    user = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
    if not user:
        await conn.execute(
            "INSERT INTO users (id, name) VALUES ($1, $2)",
            user_id, username.lower()
        )
        await conn.commit()
        return await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
    return user

# Helper: Update user
async def update_user(user_id, fields: dict):
    sets = ", ".join([f"{k} = ${i+2}" for i, k in enumerate(fields)])
    values = [user_id] + list(fields.values())
    await conn.execute(f"UPDATE users SET {sets} WHERE id = $1", *values)
    await conn.commit()

# Start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = (user.username or user.first_name).lower()
    await get_user(user.id, username)
    await update.message.reply_text(
        f"👋 Welcome, @{username}!\n\n"
        "This bot converts images to PDF.\n\n"
        "🆓 Free users: 1 trial image-to-PDF conversion\n"
        "💳 Use /upgrade <username> to upgrade a user (admin only).\n\n"
        "📎 Send images to get started.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🖼️ Convert to PDF", callback_data="convertpdf_btn")],
            [InlineKeyboardButton("👤 View Profile", callback_data="profile_btn")]
        ])
    )

# View profile
async def view_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    data = await get_user(user.id, (user.username or user.first_name).lower())

    upgrade_until = data["upgrade_until"]
    is_upgraded = upgrade_until and upgrade_until > datetime.datetime.utcnow()

    await query.edit_message_text(
        f"👤 User: @{data['name']}\n"
        f"📦 Plan: {'Upgraded ✅' if is_upgraded else 'Free ❌'}\n"
        f"📄 Image-to-PDF Trials Used: {data['image_trials']}/1"
    )

# Handle images
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = (user.username or user.first_name).lower()
    await get_user(user.id, username)

    if "image_buffer" not in context.user_data:
        context.user_data["image_buffer"] = []

    photo = update.message.photo[-1]
    file = await photo.get_file()
    img_path = os.path.join(tempfile.gettempdir(), f"{uuid4().hex}.jpg")
    await file.download_to_drive(img_path)
    context.user_data["image_buffer"].append(img_path)

    await update.message.reply_text(
        "✅ Image received. Send more images or convert now:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🖼️ Convert to PDF", callback_data="convertpdf_btn")]
        ])
    )

# Convert to PDF
async def convert_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    username = (user.username or user.first_name).lower()
    data = await get_user(user.id, username)

    upgrade_until = data["upgrade_until"]
    upgraded = upgrade_until and upgrade_until > datetime.datetime.utcnow()

    if not upgraded and data["image_trials"] >= 1:
        await query.edit_message_text("❌ Free trial used up. Please upgrade to continue using image-to-PDF.")
        return

    image_paths = context.user_data.get("image_buffer", [])
    if not image_paths:
        await query.edit_message_text("❗ No images found. Please send some images first.")
        return

    pdf = FPDF()
    for img_path in image_paths:
        pdf.add_page()
        pdf.image(img_path, x=10, y=10, w=190)
    pdf_path = os.path.join(tempfile.gettempdir(), f"{uuid4().hex}.pdf")
    pdf.output(pdf_path)

    await query.message.reply_document(document=open(pdf_path, "rb"), filename="converted.pdf")

    if not upgraded:
        await update_user(user.id, {"image_trials": data["image_trials"] + 1})

    context.user_data["image_buffer"] = []

# Upgrade
async def upgrade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("❌ You're not authorized to use this command.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /upgrade <username>")
        return

    username = context.args[0].lower()
    user_data = await conn.fetchrow("SELECT * FROM users WHERE name = $1", username)
    if not user_data:
        await update.message.reply_text("❌ User not found.")
        return

    buttons = [
        [InlineKeyboardButton("Upgrade 1 day", callback_data=f"upgrade_{username}_1")],
        [InlineKeyboardButton("Upgrade 7 days", callback_data=f"upgrade_{username}_7")],
        [InlineKeyboardButton("Upgrade 30 days", callback_data=f"upgrade_{username}_30")]
    ]
    await update.message.reply_text(f"Select upgrade duration for @{username}:", reply_markup=InlineKeyboardMarkup(buttons))

# Handle upgrade buttons
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "profile_btn":
        await view_profile(update, context)
    elif data == "convertpdf_btn":
        await convert_pdf(update, context)
    elif data.startswith("upgrade_"):
        _, username, days = data.split("_")
        user_data = await conn.fetchrow("SELECT * FROM users WHERE name = $1", username.lower())
        if not user_data:
            await query.edit_message_text("❌ User not found.")
            return
        days = int(days)
        now = datetime.datetime.utcnow()
        new_expiry = max(now, user_data["upgrade_until"] or now) + datetime.timedelta(days=days)
        await conn.execute("UPDATE users SET upgrade_until = $1 WHERE name = $2", new_expiry, username.lower())
        await conn.commit()
        await query.edit_message_text(f"✅ Upgraded @{username} for {days} day(s) until {new_expiry.strftime('%Y-%m-%d %H:%M')} UTC.")
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"✅ User @{username} upgraded for {days} day(s).")

# Webhook handlers
async def webhook_start(app: Application):
    await setup_db()
    await app.bot.set_my_commands([
        BotCommand("start", "Start the bot"),
        BotCommand("upgrade", "Upgrade a user (admin only)")
    ])
    await app.bot.set_webhook(WEBHOOK_URL)

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("upgrade", upgrade_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_button))

    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.getenv("PORT", 8443)),
        webhook_url=WEBHOOK_URL,
        on_startup=webhook_start
    )

if __name__ == "__main__":
    main()
