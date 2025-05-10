import os
import logging
import asyncio
import tempfile
import uuid
from datetime import datetime, timedelta
from PIL import Image
import psycopg2
from psycopg2.extras import Json
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    InputMediaDocument,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Config
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 1378825382
DATABASE_URL = os.getenv("DATABASE_URL")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8443))
APP_NAME = os.getenv("RENDER_EXTERNAL_HOSTNAME")

# Logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

# PostgreSQL setup
conn = psycopg2.connect(DATABASE_URL, sslmode="require")
cursor = conn.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    username TEXT,
    plan TEXT DEFAULT 'free',
    downloads JSON DEFAULT '[]',
    pdf_trials INT DEFAULT 0,
    upgrade_expires TIMESTAMP
);
""")
conn.commit()

# PDF image storage
image_collections = {}

# --- Helper Functions ---

def get_user(user_id, username):
    cursor.execute("SELECT * FROM users WHERE user_id = %s;", (user_id,))
    user = cursor.fetchone()
    if not user:
        cursor.execute(
            "INSERT INTO users (user_id, username) VALUES (%s, %s);", (user_id, username)
        )
        conn.commit()
    else:
        if username and username != user[1]:
            cursor.execute("UPDATE users SET username = %s WHERE user_id = %s;", (username, user_id))
            conn.commit()
    return True

def update_downloads(user_id):
    cursor.execute("SELECT downloads FROM users WHERE user_id = %s;", (user_id,))
    downloads = cursor.fetchone()[0] or []
    now = datetime.utcnow().isoformat()
    downloads.append(now)
    cursor.execute("UPDATE users SET downloads = %s WHERE user_id = %s;", (Json(downloads), user_id))
    conn.commit()

def check_download_limit(user_id):
    cursor.execute("SELECT plan, downloads FROM users WHERE user_id = %s;", (user_id,))
    plan, downloads = cursor.fetchone()
    if plan == "paid":
        return True
    today = datetime.utcnow().date()
    today_downloads = [d for d in downloads if datetime.fromisoformat(d).date() == today]
    return len(today_downloads) < 3

def check_pdf_limit(user_id):
    cursor.execute("SELECT plan, pdf_trials FROM users WHERE user_id = %s;", (user_id,))
    plan, pdf_trials = cursor.fetchone()
    if plan == "paid":
        return True
    return pdf_trials < 1

def increment_pdf_trial(user_id):
    cursor.execute("UPDATE users SET pdf_trials = pdf_trials + 1 WHERE user_id = %s;", (user_id,))
    conn.commit()

def is_upgraded(user_id):
    cursor.execute("SELECT plan, upgrade_expires FROM users WHERE user_id = %s;", (user_id,))
    result = cursor.fetchone()
    if result:
        plan, expires = result
        if plan == "paid" and expires and datetime.utcnow() < expires:
            return True
    return False

def upgrade_user(username, days):
    cursor.execute("SELECT user_id FROM users WHERE username = %s;", (username,))
    result = cursor.fetchone()
    if not result:
        return False
    user_id = result[0]
    expires = datetime.utcnow() + timedelta(days=days)
    cursor.execute("UPDATE users SET plan = 'paid', upgrade_expires = %s WHERE user_id = %s;", (expires, user_id))
    conn.commit()
    return user_id

# --- Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_user(user.id, user.username)
    keyboard = [
        [InlineKeyboardButton("View Profile", callback_data="profile")],
        [InlineKeyboardButton("🖼️ Convert Image to PDF", callback_data="convertpdf_btn")]
    ]
    await update.message.reply_text(
        "👋 Welcome! This bot helps you download videos and convert images to PDF.\n\n"
        "📦 *Free Plan Limits:*\n- 50MB max video\n- 3 downloads/day\n- 1 image-to-PDF trial\n\n"
        "Use /upgrade <username> to unlock full access.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def view_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cursor.execute("SELECT username, plan, downloads, pdf_trials, upgrade_expires FROM users WHERE user_id = %s;", (user_id,))
    user = cursor.fetchone()
    if not user:
        await update.callback_query.edit_message_text("User not found.")
        return

    username, plan, downloads, pdf_trials, upgrade_expires = user
    today = datetime.utcnow().date()
    today_downloads = [d for d in downloads if datetime.fromisoformat(d).date() == today]
    status = f"👤 Username: @{username or 'N/A'}\n" \
             f"📦 Plan: {plan.upper()}\n" \
             f"⬇️ Downloads Today: {len(today_downloads)} / {'Unlimited' if plan == 'paid' else '3'}\n" \
             f"🖼️ PDF Trials Used: {pdf_trials} / {'Unlimited' if plan == 'paid' else '1'}\n"

    if plan == "paid" and upgrade_expires:
        status += f"⏳ Expires: {upgrade_expires.strftime('%Y-%m-%d %H:%M')} UTC\n"

    await update.callback_query.edit_message_text(status)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_path = f"image_{datetime.utcnow().strftime('%H%M%S%f')}.jpg"
    await file.download_to_drive(image_path)
    if user_id not in image_collections:
        image_collections[user_id] = []
    image_collections[user_id].append(image_path)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🖼️ Convert to PDF", callback_data="convertpdf_btn")]
    ])
    await update.message.reply_text(
        "✅ Image received. You can send more, or tap below to generate a PDF.",
        reply_markup=keyboard
    )

async def convert_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()
    if user_id not in image_collections or not image_collections[user_id]:
        await query.edit_message_text("❌ No images found. Please send at least one image.")
        return

    if not check_pdf_limit(user_id):
        await query.edit_message_text("❌ PDF trial limit reached. Upgrade to unlock unlimited conversions.")
        return

    image_paths = image_collections[user_id]
    image_list = []

    for path in image_paths:
        img = Image.open(path).convert("RGB")
        image_list.append(img)

    temp_pdf = f"{uuid.uuid4().hex}.pdf"
    image_list[0].save(temp_pdf, save_all=True, append_images=image_list[1:])
    await context.bot.send_document(chat_id=user_id, document=open(temp_pdf, "rb"))

    for path in image_paths:
        os.remove(path)
    image_collections[user_id] = []

    if not is_upgraded(user_id):
        increment_pdf_trial(user_id)

    os.remove(temp_pdf)
    await query.edit_message_text("✅ PDF generated successfully!")

async def handle_upgrade_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("Usage: /upgrade <username>")
        return

    username = args[0].lstrip("@")
    context.user_data["upgrade_username"] = username
    keyboard = [
        [InlineKeyboardButton("1 Day", callback_data="upgrade_1")],
        [InlineKeyboardButton("7 Days", callback_data="upgrade_7")],
        [InlineKeyboardButton("30 Days", callback_data="upgrade_30")]
    ]
    await update.message.reply_text(f"Select upgrade duration for @{username}:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_upgrade_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    username = context.user_data.get("upgrade_username")
    if not username:
        await query.edit_message_text("❌ Username missing.")
        return

    days = int(query.data.split("_")[1])
    user_id = upgrade_user(username, days)
    if not user_id:
        await query.edit_message_text("❌ User not found.")
        return

    await context.bot.send_message(chat_id=user_id, text=f"🎉 Your plan has been upgraded for {days} day(s)!")
    await query.edit_message_text(f"✅ @{username} upgraded for {days} day(s).")

# --- Main App ---

if __name__ == "__main__":
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("upgrade", handle_upgrade_command))
    app.add_handler(CallbackQueryHandler(view_profile, pattern="profile"))
    app.add_handler(CallbackQueryHandler(convert_pdf, pattern="convertpdf_btn"))
    app.add_handler(CallbackQueryHandler(handle_upgrade_selection, pattern="upgrade_"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    webhook_path = f"/{BOT_TOKEN}"
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=f"https://{APP_NAME}/{BOT_TOKEN}"
    )
