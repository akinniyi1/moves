import os
import json
import logging
import asyncio
import aiohttp
from datetime import datetime, timedelta

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, CallbackQueryHandler, ContextTypes
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}/"

# Persistent JSON path (make sure you have persistent disk mounted here)
DATA_FILE = "/data/users.json"

# Admin ID
ADMIN_ID = 1378825382

# Setup logging
logging.basicConfig(level=logging.INFO)

# Ensure the JSON file exists
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, 'w') as f:
        json.dump({}, f)

# Load users
def load_users():
    with open(DATA_FILE, 'r') as f:
        return json.load(f)

# Save users
def save_users(users):
    with open(DATA_FILE, 'w') as f:
        json.dump(users, f, indent=4)

# Get user
def get_user(username):
    users = load_users()
    return users.get(username, None)

# Update user
def update_user(username, data):
    users = load_users()
    users[username] = {**users.get(username, {}), **data}
    save_users(users)

# Check plan expiry
def is_expired(user):
    if user.get("plan") != "upgraded":
        return True
    expiry = user.get("expires_at")
    if not expiry:
        return True
    return datetime.utcnow() > datetime.strptime(expiry, "%Y-%m-%d %H:%M:%S")

# Entry point
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.effective_user.username
    if not username:
        await update.message.reply_text("You must set a Telegram username to use this bot.")
        return

    user = get_user(username)
    if not user:
        update_user(username, {
            "downloads": 0,
            "plan": "free",
            "expires_at": None
        })

    keyboard = [
        [InlineKeyboardButton("Image to PDF", callback_data="pdf")],
        [InlineKeyboardButton("View Profile", callback_data="profile")]
    ]
    await update.message.reply_text(
        "Welcome! You can convert images to PDF and download videos.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# Profile
async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    username = query.from_user.username
    user = get_user(username)
    if not user:
        await query.edit_message_text("User not found.")
        return

    if is_expired(user):
        update_user(username, {"plan": "free", "expires_at": None})

    plan = get_user(username).get("plan", "free")
    downloads = get_user(username).get("downloads", 0)
    expires = get_user(username).get("expires_at", "N/A")

    text = f"**Username**: @{username}\n**Plan**: {plan}\n**Downloads**: {downloads}\n**Expires**: {expires}"
    await query.edit_message_text(text, parse_mode="Markdown")

# PDF init
image_collections = {}

async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.effective_user.username
    if not username:
        return

    user = get_user(username)
    if is_expired(user) and user.get("plan") != "free":
        update_user(username, {"plan": "free", "expires_at": None})

    if user["plan"] == "free" and image_collections.get(username):
        await update.message.reply_text("Free users can only create 1 PDF. Upgrade your plan.")
        return

    if username not in image_collections:
        image_collections[username] = []

    photo = update.message.photo[-1]
    file_id = photo.file_id
    image_collections[username].append(file_id)

    await update.message.reply_text(
        "Image received. Click below to generate PDF.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Generate PDF", callback_data="generate_pdf")]])
    )

# Generate PDF
from fpdf import FPDF
from PIL import Image
from io import BytesIO

async def generate_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    username = query.from_user.username
    await query.answer()

    if username not in image_collections:
        await query.edit_message_text("No images received yet.")
        return

    images = []
    for file_id in image_collections[username]:
        file = await context.bot.get_file(file_id)
        async with aiohttp.ClientSession() as session:
            async with session.get(file.file_path) as resp:
                img = Image.open(BytesIO(await resp.read())).convert("RGB")
                images.append(img)

    pdf_path = f"/data/{username}_output.pdf"
    images[0].save(pdf_path, save_all=True, append_images=images[1:])

    await query.message.reply_document(document=open(pdf_path, 'rb'), filename=f"{username}_output.pdf")

    del image_collections[username]

# Upgrade user
async def upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Unauthorized.")
        return

    try:
        username = context.args[0]
    except:
        await update.message.reply_text("Usage: /upgrade <username>")
        return

    keyboard = [[
        InlineKeyboardButton("1 Day", callback_data=f"upgrade_{username}_1")
    ], [
        InlineKeyboardButton("7 Days", callback_data=f"upgrade_{username}_7")
    ]]
    await update.message.reply_text(f"Select upgrade duration for @{username}", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_upgrade_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("_")
    if len(data) != 3:
        return
    _, username, days = data
    days = int(days)

    expires_at = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    update_user(username, {"plan": "upgraded", "expires_at": expires_at})

    await query.edit_message_text(f"@{username} upgraded for {days} day(s). Expires at {expires_at}.")

# Handlers
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).webhook_url(BASE_URL).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("upgrade", upgrade))
    app.add_handler(CallbackQueryHandler(profile, pattern="^profile$"))
    app.add_handler(CallbackQueryHandler(generate_pdf, pattern="^generate_pdf$"))
    app.add_handler(CallbackQueryHandler(handle_upgrade_callback, pattern=r"^upgrade_"))
    app.add_handler(CallbackQueryHandler(profile, pattern="^pdf$"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_image))

    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        webhook_url=BASE_URL
    )

if __name__ == "__main__":
    main()
