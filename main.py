# --- [IMPORTS & SETUP] ---
import os
import re
import ssl
import json
import logging
import yt_dlp
import ffmpeg
import asyncio
import csv
from PIL import Image
from datetime import datetime, timedelta
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

ssl._create_default_https_context = ssl._create_unverified_context
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", 10000))
ADMIN_ID = 1378825382
DATA_FILE = "/mnt/data/users.json"
NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY")
IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET")

application = Application.builder().token(BOT_TOKEN).build()
file_registry = {}
image_collections = {}
pdf_trials = {}
support_messages = {}

if not os.path.exists("/mnt/data"):
    os.makedirs("/mnt/data")

def load_users():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {}

def save_users(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

users = load_users()

# --- [HELPERS] ---
def is_valid_url(text): return re.match(r'https?://', text)

def generate_filename(ext="mp4"):
    return f"file_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}.{ext}"

def is_premium(user):
    if user.get("plan") != "premium": return False
    try:
        return datetime.utcnow() < datetime.fromisoformat(user.get("expires", ""))
    except: return False

def downgrade_expired_users():
    now = datetime.utcnow()
    for username, user in users.items():
        if user.get("plan") == "premium":
            try:
                if datetime.fromisoformat(user.get("expires", "")) < now:
                    users[username] = {"plan": "free", "downloads": 0}
            except: continue
    save_users(users)

async def delete_file_later(path, file_id=None):
    await asyncio.sleep(60)
    if os.path.exists(path): os.remove(path)
    if file_id: file_registry.pop(file_id, None)

def is_banned(username): return users.get(username, {}).get("banned", False)

# --- [START HANDLER] ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    downgrade_expired_users()
    user = update.effective_user
    username = user.username
    if not username:
        await update.message.reply_text("❌ You need a Telegram username to use this bot.")
        return
    if username not in users:
        users[username] = {"plan": "free", "downloads": 0}
        save_users(users)
    if is_banned(username):
        await update.message.reply_text("⛔ You are banned from using this bot.")
        return
    buttons = [
        [InlineKeyboardButton("👤 View Profile", callback_data="profile"),
         InlineKeyboardButton("🖼️ Convert to PDF", callback_data="convertpdf_btn")],
        [InlineKeyboardButton("⬆️ Upgrade Your Plan", callback_data="upgrade_plan")],
        [InlineKeyboardButton("📢 Join Our Channel", url="https://t.me/Downloadassaas")]
    ]
    await update.message.reply_text(
        f"👋 Hello @{username or user.first_name}!\n\n"
        "This bot supports downloading videos from:\n"
        "✅ Facebook, TikTok, Twitter, Instagram\n"
        "❌ YouTube is not supported.\n\n"
        "Free Users:\n• 3 video downloads\n• 1 PDF conversion trial\n\n"
        "Send a supported video link to begin.",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# --- [INLINE BUTTON HANDLER] ---
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    username = query.from_user.username
    user_data = users.get(username, {"plan": "free"})

    if data == "profile":
        downgrade_expired_users()
        if is_premium(user_data):
            exp_dt = datetime.fromisoformat(user_data.get("expires", ""))
            msg = f"👤 Username: @{username}\n💼 Plan: Premium\n⏰ Expires: {exp_dt.strftime('%Y-%m-%d %H:%M')} UTC"
        else:
            msg = f"👤 Username: @{username}\n💼 Plan: Free"
        await query.message.reply_text(msg)
    elif data == "convertpdf_btn":
        await convert_pdf(update, context, triggered_by_button=True)
    elif data.startswith("audio:"):
        file = data.split("audio:")[1]
        if os.path.exists(file): await convert_to_audio(update, context, file)
        else: await query.message.reply_text("File deleted. Please resend the link.")
    elif data == "upgrade_plan":
        buttons = [
            [InlineKeyboardButton("1 Month - $2", url=f"https://nowpayments.io/payment?price_amount=2&price_currency=usd&order_id=@{username}_1month")],
            [InlineKeyboardButton("2 Months - $4", url=f"https://nowpayments.io/payment?price_amount=4&price_currency=usd&order_id=@{username}_2month")]
        ]
        await query.message.reply_text("Choose a plan:", reply_markup=InlineKeyboardMarkup(buttons))

# --- [MEDIA HANDLING OMITTED FOR SPACE] ---

# Include handle_video, convert_pdf, handle_photo, convert_to_audio
# These are unchanged from your last working file — let me know if you want me to paste those too.

# --- [COMMAND: /upgrade /downgrade /ban /unban /stats /export] ---
async def upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if len(context.args) < 2: return await update.message.reply_text("Usage: /upgrade <username> <days>")
    username = context.args[0].lstrip("@")
    days = int(context.args[1])
    expires = datetime.utcnow() + timedelta(days=days)
    users[username] = {"plan": "premium", "expires": expires.isoformat(), "downloads": 0}
    save_users(users)
    await update.message.reply_text(f"✅ Upgraded @{username} for {days} days.")

async def downgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not context.args: return await update.message.reply_text("Usage: /downgrade <username>")
    username = context.args[0].lstrip("@")
    users[username] = {"plan": "free", "downloads": 0}
    save_users(users)
    await update.message.reply_text(f"⬇️ Downgraded @{username} to free plan.")

async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not context.args: return await update.message.reply_text("Usage: /ban <username>")
    username = context.args[0].lstrip("@")
    users[username] = users.get(username, {})
    users[username]["banned"] = True
    save_users(users)
    await update.message.reply_text(f"⛔ Banned @{username}")

async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not context.args: return await update.message.reply_text("Usage: /unban <username>")
    username = context.args[0].lstrip("@")
    if username in users and users[username].get("banned"):
        users[username]["banned"] = False
        save_users(users)
        await update.message.reply_text(f"✅ Unbanned @{username}")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    downgrade_expired_users()
    total = len(users)
    premium = sum(1 for u in users.values() if is_premium(u))
    await update.message.reply_text(f"📊 Total users: {total}\n💎 Premium users: {premium}")

async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    with open("/mnt/data/export.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Username", "Plan", "Expires", "Downloads"])
        for u, d in users.items():
            writer.writerow([u, d.get("plan", "free"), d.get("expires", ""), d.get("downloads", 0)])
    await update.message.reply_document(InputFile("/mnt/data/export.csv"))

# --- [NOWPAYMENTS IPN HANDLER] ---
async def ipn_handler(request):
    if request.headers.get("x-nowpayments-sig") != IPN_SECRET:
        return web.Response(status=403)
    payload = await request.json()
    order_id = payload.get("order_id", "")
    payment_status = payload.get("payment_status")
    if payment_status != "confirmed": return web.Response(text="ignored")
    match = re.match(r"@(\w+)_([12])month", order_id)
    if match:
        username, duration = match.groups()
        duration = int(duration)
        expires = datetime.utcnow() + timedelta(days=30 * duration)
        users[username] = users.get(username, {})
        users[username]["plan"] = "premium"
        users[username]["expires"] = expires.isoformat()
        users[username]["downloads"] = 0
        users[username]["invoice"] = payload
        save_users(users)
    return web.Response(text="ok")

# --- [WEBHOOK SETUP & MAIN] ---
async def webhook_handler(request):
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return web.Response(text="ok")

web_app = web.Application()
web_app.router.add_post("/webhook", webhook_handler)
web_app.router.add_post("/ipn", ipn_handler)

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("upgrade", upgrade))
application.add_handler(CommandHandler("downgrade", downgrade))
application.add_handler(CommandHandler("ban", ban))
application.add_handler(CommandHandler("unban", unban))
application.add_handler(CommandHandler("stats", stats))
application.add_handler(CommandHandler("export", export))
application.add_handler(CallbackQueryHandler(handle_button))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: handle_video(u, c)))

if __name__ == "__main__":
    web.run_app(web_app, port=PORT)
