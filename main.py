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
import hmac
import hashlib
import aiohttp
import uuid
from PIL import Image
from datetime import datetime, timedelta
from aiohttp import web
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, InputFile
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# SSL workaround
ssl._create_default_https_context = ssl._create_unverified_context
logging.basicConfig(level=logging.INFO)

# --- [CONFIGURATION] ---
BOT_TOKEN             = os.getenv("BOT_TOKEN")
ADMIN_ID              = 1378825382       # ← Your admin Telegram ID
APP_URL               = os.getenv("RENDER_EXTERNAL_URL")
PORT                  = int(os.getenv("PORT", "10000"))
DATA_FILE             = "/mnt/data/users.json"
NOWPAYMENTS_API_KEY   = os.getenv("NOWPAYMENTS_API_KEY")
NOWPAYMENTS_IPN_SECRET= os.getenv("NOWPAYMENTS_IPN_SECRET")
CHANNEL_LINK          = "https://t.me/Downloadassaas"

# Ensure storage directory exists
if not os.path.exists("/mnt/data"):
    os.makedirs("/mnt/data")

# Initialize bot application
application = Application.builder().token(BOT_TOKEN).build()

# In-memory registries
file_registry     = {}
image_collections = {}
pdf_trials        = {}
support_messages  = {}

# --- [USER DATA STORAGE] ---
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
def is_valid_url(text):
    return bool(re.match(r'https?://', text))

def gen_filename(ext="mp4"):
    return f"{uuid.uuid4().hex}.{ext}"

def is_premium(uname):
    u = users.get(uname, {})
    if u.get("plan") != "premium":
        return False
    try:
        return datetime.utcnow() < datetime.fromisoformat(u["expires"])
    except:
        return False

def is_banned(uname):
    return users.get(uname, {}).get("banned", False)

def downgrade_expired():
    now = datetime.utcnow()
    changed = False
    for uname, u in list(users.items()):
        if u.get("plan") == "premium":
            exp = u.get("expires")
            if exp and datetime.fromisoformat(exp) < now:
                users[uname] = {"plan":"free","downloads":0}
                changed = True
    if changed:
        save_users(users)

async def cleanup_file(path, mid=None):
    await asyncio.sleep(60)
    if os.path.exists(path):
        os.remove(path)
    if mid:
        file_registry.pop(mid, None)

# --- [KEYBOARDS] ---
def main_menu():
    return ReplyKeyboardMarkup([
        [KeyboardButton("Convert to PDF")],
        [KeyboardButton("Upgrade Your Plan")],
        [KeyboardButton("Join Our Channel")]
    ], resize_keyboard=True)

# --- [START HANDLER] ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    downgrade_expired()
    user = update.effective_user
    uname = user.username or str(user.id)
    if uname not in users:
        users[uname] = {"plan":"free","downloads":0}
        save_users(users)
    if is_banned(uname):
        return await update.message.reply_text("⛔ You are banned from using this bot.")
    text = (
        f"👋 Hello @{uname}!\n\n"
        "Welcome to your multi-tool bot.\n"
        "• Convert images to PDF\n"
        "• Upgrade for unlimited\n\n"
        "Choose from the menu below:"
    )
    await update.message.reply_text(text, reply_markup=main_menu())

# --- [PDF HANDLERS] ---
async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    uname = update.effective_user.username or str(user_id)
    if is_banned(uname): return
    photo = update.message.photo[-1]
    f = await context.bot.get_file(photo.file_id)
    path = f"/mnt/data/{uuid.uuid4().hex}.jpg"
    await f.download_to_drive(path)
    image_collections.setdefault(user_id, []).append(path)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Convert to PDF", callback_data="make_pdf")]])
    await update.message.reply_text("Image saved.", reply_markup=kb)

async def make_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    imgs = image_collections.get(user_id, [])
    if not imgs:
        return await query.edit_message_text("❌ No images to convert.")
    try:
        from PIL import Image as PImage
        pages = [PImage.open(i).convert("RGB") for i in imgs]
        pdf_path = f"/mnt/data/{uuid.uuid4().hex}.pdf"
        pages[0].save(pdf_path, save_all=True, append_images=pages[1:])
        await context.bot.send_document(chat_id=user_id, document=open(pdf_path, "rb"))
    except Exception as e:
        await context.bot.send_message(chat_id=user_id, text=f"Error: {e}")
    finally:
        for p in imgs:
            os.remove(p)
        image_collections[user_id] = []

# --- [TEXT HANDLER & UPGRADE MENU] ---
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id
    uname = update.effective_user.username or str(user_id)
    if is_banned(uname): return

    if text == "Convert to PDF":
        return await update.message.reply_text("Please send images to convert.")
    if text == "Join Our Channel":
        return await update.message.reply_text(f"Join here: {CHANNEL_LINK}")
    if text == "Upgrade Your Plan":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("1 month – $2", callback_data="pay_1")],
            [InlineKeyboardButton("2 months – $4", callback_data="pay_2")]
        ])
        return await update.message.reply_text("Select plan:", reply_markup=kb)

# --- [PAYMENT CALLBACK] ---
async def pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    plan = query.data  # "pay_1" or "pay_2"
    months = 1 if plan=="pay_1" else 2
    amount = 2 * months
    order_id = f"{user_id}|{30*months}"
    invoice = {
        "price_amount": amount,
        "price_currency": "usd",
        "order_id": order_id,
        "ipn_callback_url": f"{APP_URL}/ipn"
    }
    headers = {"x-api-key": NOWPAYMENTS_API_KEY}
    async with aiohttp.ClientSession() as sess:
        resp = await sess.post("https://api.nowpayments.io/v1/invoice", json=invoice, headers=headers)
        data = await resp.json()
    if data.get("invoice_url"):
        await query.edit_message_text(f"Pay here: {data['invoice_url']}")
    else:
        logging.error("NowPayments error: %s", data)
        await query.edit_message_text("❌ Could not create payment.")

# --- [IPN HANDLER] ---
async def ipn_handler(request):
    sig = request.headers.get("x-nowpayments-sig","")
    body = await request.text()
    if not hmac.compare_digest(
        hmac.new(NOWPAYMENTS_IPN_SECRET.encode(), body.encode(), hashlib.sha512).hexdigest(), sig
    ):
        return web.Response(status=403)
    data = await request.json()
    if data.get("payment_status")=="finished":
        oid = data.get("order_id","")
        if "|" in oid:
            uid, days = oid.split("|")
            days = int(days)
            exp = (datetime.utcnow() + timedelta(days=days)).isoformat()
            users.setdefault(uid,{"downloads":0}).update({"plan":"premium","expires":exp})
            save_users(users)
    return web.Response(text="ok")

# --- [EXPORT CSV] ---
async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    p = "/mnt/data/export.csv"
    with open(p,"w",newline="") as f:
        w = csv.writer(f)
        w.writerow(["Username","Plan","Expires","Downloads","Banned"])
        for u,info in users.items():
            w.writerow([u, info.get("plan"), info.get("expires",""), info.get("downloads",0), info.get("banned",False)])
    await update.message.reply_document(InputFile(p), filename="users.csv")

# --- [STATS] ---
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    total = len(users)
    premium = sum(1 for u in users.values() if u.get("plan")=="premium")
    await update.message.reply_text(f"Total users: {total}\nPremium users: {premium}")

# --- [BAN / UNBAN] ---
async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        return await update.message.reply_text("Usage: /ban <username>")
    uname = context.args[0]
    users.setdefault(uname,{"downloads":0})["banned"] = True
    save_users(users)
    await update.message.reply_text(f"⛔ {uname} banned.")

async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        return await update.message.reply_text("Usage: /unban <username>")
    uname = context.args[0]
    if users.get(uname,{}).pop("banned", None) is not None:
        save_users(users)
        await update.message.reply_text(f"✅ {uname} unbanned.")
    else:
        await update.message.reply_text("User was not banned.")

# --- [WEBHOOK SETUP] ---
async def webhook(request):
    data = await request.json()
    upd = Update.de_json(data, application.bot)
    await application.process_update(upd)
    return web.Response(text="ok")

web_app = web.Application()
web_app.router.add_post("/webhook", webhook)
web_app.router.add_post("/ipn", ipn_handler)

web_app.on_startup.append(lambda app: application.initialize())
web_app.on_startup.append(lambda app: application.start())
web_app.on_startup.append(lambda app: application.bot.set_webhook(f"{APP_URL}/webhook"))
web_app.on_cleanup.append(lambda app: application.stop())
web_app.on_cleanup.append(lambda app: application.shutdown())

# --- [REGISTER HANDLERS] ---
application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.PHOTO, photo_handler))
application.add_handler(CallbackQueryHandler(make_pdf, pattern="make_pdf"))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
application.add_handler(CallbackQueryHandler(pay_callback, pattern="pay_"))
application.add_handler(CommandHandler("export", export_cmd))
application.add_handler(CommandHandler("stats", stats_cmd))
application.add_handler(CommandHandler("ban", ban_cmd))
application.add_handler(CommandHandler("unban", unban_cmd))

# --- [RUN APPLICATION] ---
if __name__ == "__main__":
    web.run_app(web_app, port=PORT)
