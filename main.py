import os
import json
import logging
from datetime import datetime, timedelta

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

# Setup logging
logging.basicConfig(level=logging.INFO)

# Constants
BOT_TOKEN = os.getenv("BOT_TOKEN")
RENDER_HOST = os.getenv("RENDER_EXTERNAL_HOSTNAME")
BASE_URL = f"https://{RENDER_HOST}/"
ADMIN_ID = 1378825382
DATA_PATH = "/mnt/data/users.json"  # Persistent disk path on Render

# Ensure users file exists
if not os.path.exists("/mnt/data"):
    os.makedirs("/mnt/data")
if not os.path.exists(DATA_PATH):
    with open(DATA_PATH, "w") as f:
        json.dump({}, f)

# Load/save helpers
def load_users():
    with open(DATA_PATH, "r") as f:
        return json.load(f)

def save_users(users):
    with open(DATA_PATH, "w") as f:
        json.dump(users, f, indent=2)

def get_user(username):
    users = load_users()
    return users.get(username, None)

def update_user(username, data):
    users = load_users()
    users[username] = {**users.get(username, {}), **data}
    save_users(users)

def reset_download_if_needed(username):
    user = get_user(username)
    if not user:
        return

    last_download = user.get("last_download")
    today = datetime.utcnow().strftime("%Y-%m-%d")

    if last_download != today:
        user["downloads"] = 0
        user["last_download"] = today
        update_user(username, user)

def check_plan_expiry(username):
    user = get_user(username)
    if user and user.get("plan") == "paid":
        expires_at = datetime.strptime(user["expires_at"], "%Y-%m-%d %H:%M:%S")
        if datetime.utcnow() > expires_at:
            user["plan"] = "free"
            user["expires_at"] = None
            update_user(username, user)

# /start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username
    if not username:
        await update.message.reply_text("Please set a Telegram username to use this bot.")
        return

    if not get_user(username):
        update_user(username, {
            "plan": "free",
            "expires_at": None,
            "downloads": 0,
            "last_download": ""
        })

    check_plan_expiry(username)
    reset_download_if_needed(username)

    keyboard = [
        [InlineKeyboardButton("Download Instagram Video", callback_data="insta")],
        [InlineKeyboardButton("Image to PDF", callback_data="pdf")],
        [InlineKeyboardButton("View Profile", callback_data="profile")]
    ]
    await update.message.reply_text(
        "Welcome! Instagram video download is supported. YouTube is not allowed.\n"
        "Free users are limited to 3 downloads daily.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# Profile View
async def view_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    username = query.from_user.username
    user = get_user(username)
    if not user:
        await query.edit_message_text("User not found.")
        return

    check_plan_expiry(username)

    plan = user.get("plan", "free")
    downloads = user.get("downloads", 0)
    expires_at = user.get("expires_at")
    expiry = datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d") if expires_at else "N/A"

    msg = f"**Username**: @{username}\n**Plan**: {plan}\n**Downloads Today**: {downloads}/3\n**Expires**: {expiry}"
    await query.edit_message_text(msg, parse_mode="Markdown")

# Instagram button handler (mocked)
async def insta_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    username = query.from_user.username
    user = get_user(username)
    check_plan_expiry(username)
    reset_download_if_needed(username)

    if user["plan"] == "free" and user["downloads"] >= 3:
        await query.edit_message_text("Free user limit (3 downloads/day) reached. Upgrade to continue.")
        return

    # Simulate download
    user["downloads"] += 1
    update_user(username, user)

    await query.edit_message_text("Video downloaded successfully. (Simulated for now)")

# Image handler placeholder
async def image_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Image received. PDF conversion coming soon.")

# Upgrade (Admin only)
async def upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Unauthorized.")
        return

    try:
        username = context.args[0]
        hours = int(context.args[1])
    except:
        await update.message.reply_text("Usage: /upgrade <username> <hours>")
        return

    expires_at = (datetime.utcnow() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    update_user(username, {"plan": "paid", "expires_at": expires_at})
    await update.message.reply_text(f"@{username} upgraded for {hours} hour(s).")

# Main
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("upgrade", upgrade))
    app.add_handler(CallbackQueryHandler(insta_handler, pattern="^insta$"))
    app.add_handler(CallbackQueryHandler(view_profile, pattern="^profile$"))
    app.add_handler(MessageHandler(filters.PHOTO, image_handler))

    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        webhook_url=BASE_URL + f"bot{BOT_TOKEN}"
    )

if __name__ == "__main__":
    main()
