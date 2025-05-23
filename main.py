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
from datetime import datetime, timedelta, timezone
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
import aiohttp
import uuid

ssl._create_default_https_context = ssl._create_unverified_context
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", 10000))
ADMIN_ID = 1378825382
CHANNEL_URL = "https://t.me/Downloadassaas"
DATA_FILE = "/mnt/data/users.json"
NOW_API_KEY = os.getenv("NOWPAYMENTS_API_KEY")
NOW_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET")

application = Application.builder().token(BOT_TOKEN).build()
file_registry = {}
image_collections = {}
pdf_trials = {}
support_messages = {}
pending_invoices = {}  # invoice_id -> (username, amount)

if not os.path.exists("/mnt/data"):
    os.makedirs("/mnt/data")


# --- [DATA STORE] ---
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
    return re.match(r'https?://', text)

def generate_filename(ext="mp4"):
    return f"file_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}.{ext}"

def is_premium(user):
    if user.get("plan") != "premium":
        return False
    exp = user.get("expires")
    if not isinstance(exp, str):
        return False
    try:
        exp_dt = datetime.fromisoformat(exp)
        if datetime.now(timezone.utc) < exp_dt:
            return True
        # expired → immediate downgrade
        user["plan"] = "free"
        user["downloads"] = 0
        user.pop("expires", None)
        save_users(users)
    except:
        pass
    return False

def downgrade_expired_users():
    now = datetime.now(timezone.utc)
    for username, user in users.items():
        exp = user.get("expires")
        if isinstance(exp, str):
            try:
                if datetime.fromisoformat(exp) < now:
                    users[username] = {"plan": "free", "downloads": 0}
            except:
                continue
    save_users(users)

async def delete_file_later(path, file_id=None):
    await asyncio.sleep(60)
    if os.path.exists(path):
        os.remove(path)
    if file_id:
        file_registry.pop(file_id, None)


# --- [NOWPAYMENTS INTEGRATION] ---
async def create_invoice(username, amount):
    """Create NowPayments invoice and schedule auto-cancel in 20m."""
    url = "https://api.nowpayments.io/v1/invoice"
    headers = {"x-api-key": NOW_API_KEY, "Content-Type": "application/json"}
    order_id = f"{username}:{amount}:{uuid.uuid4()}"
    payload = {
        "price_amount": amount,
        "price_currency": "usd",
        "order_id": order_id,
        "ipn_callback_url": f"{APP_URL}/ipn",
        "success_url": CHANNEL_URL
    }
    async with aiohttp.ClientSession() as session:
        resp = await session.post(url, headers=headers, json=payload)
        data = await resp.json()
    inv_id = data.get("id")
    pending_invoices[inv_id] = (username, amount)
    # schedule cancellation if unpaid
    asyncio.create_task(cancel_invoice_later(inv_id))
    return data

async def cancel_invoice_later(inv_id):
    await asyncio.sleep(20 * 60)  # 20 minutes
    url = f"https://api.nowpayments.io/v1/invoice/{inv_id}"
    headers = {"x-api-key": NOW_API_KEY}
    async with aiohttp.ClientSession() as session:
        await session.delete(url, headers=headers)

async def ipn_handler(request):
    data = await request.json()
    # verify secret
    if data.get("ipn_secret") != NOW_IPN_SECRET:
        return web.Response(text="invalid secret", status=400)
    if data.get("payment_status") == "finished":
        inv_id = data.get("invoice_id")
        tup = pending_invoices.pop(inv_id, None)
        if tup:
            username, amount = tup
        else:
            # fallback parse
            order = data.get("order_id","").split(":")
            if len(order) >= 2:
                username, amount = order[0], float(order[1])
            else:
                return web.Response(text="bad order_id", status=400)
        days = 30 if amount == 2.0 else 60
        exp = datetime.now(timezone.utc) + timedelta(days=days)
        users[username]["plan"] = "premium"
        users[username]["expires"] = exp.isoformat()
        save_users(users)
    return web.Response(text="ok")


# --- [AUDIO CONVERSION] ---
async def convert_to_audio(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path):
    audio_path = file_path.replace(".mp4", ".mp3")
    try:
        ffmpeg.input(file_path).output(audio_path).run(overwrite_output=True)
        with open(audio_path, 'rb') as f:
            await update.callback_query.message.reply_audio(f, filename=os.path.basename(audio_path))
        os.remove(audio_path)
    except:
        await update.callback_query.message.reply_text("❌ Failed to convert to audio.")


# --- [START HANDLER] ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    downgrade_expired_users()
    user = update.effective_user
    username = user.username
    if username and username not in users:
        users[username] = {"plan": "free", "downloads": 0}
        save_users(users)
    if username and users.get(username, {}).get("banned"):
        return await update.message.reply_text("⛔ You are banned from using this bot.")

    buttons = [
        [InlineKeyboardButton("👤 View Profile", callback_data="profile"),
         InlineKeyboardButton("🖼️ Convert to PDF", callback_data="convertpdf_btn")],
        [InlineKeyboardButton("💳 Upgrade Your Plan", callback_data="upgrade_plan")],
        [InlineKeyboardButton("📣 Join Our Channel", url=CHANNEL_URL)]
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


# --- [VIDEO HANDLER] ---
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    downgrade_expired_users()
    url = update.message.text.strip()
    if not is_valid_url(url):
        await update.message.reply_text("❌ Invalid URL or unsupported platform.")
        return
    if "youtube.com" in url or "youtu.be" in url:
        await update.message.reply_text("❌ YouTube is not supported.")
        return

    user = update.effective_user
    username = user.username
    if username and users.get(username, {}).get("banned"):
        return await update.message.reply_text("⛔ You are banned from using this bot.")

    user_data = users.get(username, {"plan": "free", "downloads": 0})
    if not is_premium(user_data) and user_data["downloads"] >= 3:
        await update.message.reply_text("⛔ Free users are limited to 3 downloads. Upgrade to continue.")
        return

    filename = generate_filename()
    status_msg = await update.message.reply_text("📥 Downloading...")
    ydl_opts = {
        'outtmpl': filename,
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'quiet': True,
        'noplaylist': True,
        'max_filesize': 50 * 1024 * 1024
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        with open(filename, 'rb') as f:
            sent = await update.message.reply_video(
                f,
                caption="🎉 Here's your video!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎧 Convert to Audio", callback_data=f"audio:{filename}")]])
            )
        file_registry[sent.message_id] = filename
        asyncio.create_task(delete_file_later(filename, sent.message_id))
        await status_msg.delete()
        if not is_premium(user_data):
            user_data["downloads"] += 1
            users[username] = user_data
            save_users(users)
    except:
        await status_msg.edit_text("⚠️ Download failed or file too large.")


# --- [INLINE HANDLER] ---
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    username = query.from_user.username
    if username and users.get(username, {}).get("banned"):
        return await query.edit_message_text("⛔ You are banned from using this bot.")

    data = query.data

    if data == "upgrade_plan":
        opts = [
            [InlineKeyboardButton("$2 - 1 month", callback_data="invoice_2")],
            [InlineKeyboardButton("$4 - 2 months", callback_data="invoice_4")]
        ]
        return await query.message.reply_text("Choose your plan:", reply_markup=InlineKeyboardMarkup(opts))

    if data.startswith("invoice_"):
        amount = float(data.split("_")[1])
        invoice = await create_invoice(username, amount)
        return await query.message.reply_text(f"Please pay ${amount} here:\n{invoice.get('invoice_url')}")

    # existing profile / convertpdf_btn / audio: branches unchanged
    if data == "profile":
        downgrade_expired_users()
        user_data = users.get(username, {"plan": "free"})
        if is_premium(user_data):
            exp_dt = datetime.fromisoformat(user_data["expires"])
            msg = f"👤 Username: @{username}\n💼 Plan: Premium\n⏰ Expires: {exp_dt.strftime('%Y-%m-%d %H:%M')} UTC"
        else:
            msg = f"👤 Username: @{username}\n💼 Plan: Free"
        await query.message.reply_text(msg)
    elif data == "convertpdf_btn":
        fake_msg = type("msg", (), {"message": query.message, "effective_user": query.from_user})
        await convert_pdf(fake_msg, context, triggered_by_button=True)
    elif data.startswith("audio:"):
        file = data.split("audio:")[1]
        if not os.path.exists(file):
            await query.message.reply_text("File deleted. Please resend the link.")
        else:
            await convert_to_audio(update, context, file)


# --- [PDF HANDLER] ---
async def convert_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, triggered_by_button=False):
    user_id = update.effective_user.id
    username = update.effective_user.username
    if username and users.get(username, {}).get("banned"):
        return await update.message.reply_text("⛔ You are banned from using this bot.")

    user_data = users.get(username, {"plan": "free"})
    if not is_premium(user_data):
        if pdf_trials.get(user_id, 0) >= 1:
            return await update.message.reply_text("⛔ Free users can only convert 1 PDF.")
        pdf_trials[user_id] = 1

    images = image_collections.get(user_id, [])
    if not images:
        return await update.message.reply_text("❌ No images received.")
    try:
        pil_images = [Image.open(img).convert("RGB") for img in images]
        pdf_path = generate_filename("pdf")
        pil_images[0].save(pdf_path, save_all=True, append_images=pil_images[1:])
        with open(pdf_path, 'rb') as f:
            await update.message.reply_document(f, filename="converted.pdf")
        asyncio.create_task(delete_file_later(pdf_path))
        for img in images:
            os.remove(img)
        image_collections[user_id] = []
    except:
        await update.message.reply_text("❌ Failed to generate PDF.")


# --- [IMAGE HANDLER] ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username
    if username and users.get(username, {}).get("banned"):
        return await update.message.reply_text("⛔ You are banned from using this bot.")

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_path = f"image_{datetime.utcnow().strftime('%H%M%S%f')}.jpg"
    await file.download_to_drive(image_path)
    image_collections.setdefault(user_id, []).append(image_path)
    await update.message.reply_text("✅ Image received. Send more or click /convertpdf to generate PDF.")


# --- [ADMIN COMMANDS] ---
async def upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if len(args) != 2:
        return await update.message.reply_text("Usage: /upgrade <username> <hours>")
    username, hours = args
    username = username.lstrip('@')
    if username not in users:
        return await update.message.reply_text("❌ User not found.")
    try:
        hours = int(hours)
        expires = datetime.utcnow() + timedelta(hours=hours)
        users[username]["plan"] = "premium"
        users[username]["expires"] = expires.isoformat()
        save_users(users)
        return await update.message.reply_text(f"✅ Upgraded @{username} until {expires.strftime('%Y-%m-%d %H:%M')} UTC")
    except:
        return await update.message.reply_text("❌ Invalid hours")

async def downgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if not args:
        return await update.message.reply_text("Usage: /downgrade <username>")
    username = args[0].lstrip('@')
    if username in users:
        users[username] = {"plan": "free", "downloads": 0}
        save_users(users)
        return await update.message.reply_text(f"✅ Downgraded @{username} to free plan.")

async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) != 1:
        return await update.message.reply_text("Usage: /ban <username>")
    username = context.args[0].lstrip('@')
    if username in users:
        users[username]["banned"] = True
        save_users(users)
        return await update.message.reply_text(f"⛔ Banned @{username}")
    return await update.message.reply_text("❌ User not found.")

async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) != 1:
        return await update.message.reply_text("Usage: /unban <username>")
    username = context.args[0].lstrip('@')
    if username in users and users[username].get("banned"):
        users[username]["banned"] = False
        save_users(users)
        return await update.message.reply_text(f"✅ Unbanned @{username}")
    return await update.message.reply_text("❌ User not found or not banned.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    downgrade_expired_users()
    total = len(users)
    premium = sum(1 for u in users.values() if u.get("plan") == "premium")
    free = total - premium
    downloads = sum(u.get("downloads", 0) for u in users.values())
    await update.message.reply_text(
        f"📊 Stats:\nTotal Users: {total}\nPremium: {premium}\nFree: {free}\nTotal Downloads: {downloads}"
    )

async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    path = "/mnt/data/export.csv"
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Username", "Plan", "Expires", "Banned"])
        for uname, data in users.items():
            writer.writerow([uname, data.get("plan","free"), data.get("expires","N/A"), data.get("banned", False)])
    with open(path, "rb") as f:
        await update.message.reply_document(f, filename="users.csv")


# --- [SUPPORT SYSTEM] ---
async def support_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.reply_to_message and update.effective_user.id == ADMIN_ID:
        msg_id = update.message.reply_to_message.message_id
        if msg_id in support_messages:
            uid = support_messages[msg_id]
            await context.bot.send_message(chat_id=uid, text=f"📬 Admin reply:\n{update.message.text}")

async def user_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        return
    forwarded = await context.bot.send_message(
        ADMIN_ID,
        f"📩 Message from @{update.effective_user.username}:\n\n{update.message.text}"
    )
    support_messages[forwarded.message_id] = update.effective_user.id
    await update.message.reply_text("✅ Message sent. You’ll get a reply soon.")


# --- [WEBHOOK SETUP] ---
web_app = web.Application()

async def webhook_handler(request):
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.update_queue.put(update)
    except Exception as e:
        logging.error(f"Webhook error: {e}")
    return web.Response(text="ok")

web_app.router.add_post("/webhook", webhook_handler)
web_app.router.add_post("/ipn", ipn_handler)


async def on_startup(app):
    await application.initialize()
    await application.start()
    await application.bot.set_webhook(f"{APP_URL}/webhook")
    logging.info("✅ Webhook set.")

async def on_cleanup(app):
    await application.stop()
    await application.shutdown()

web_app.on_startup.append(on_startup)
web_app.on_cleanup.append(on_cleanup)


# --- [HANDLER REGISTRATION] ---
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("upgrade", upgrade))
application.add_handler(CommandHandler("downgrade", downgrade))
application.add_handler(CommandHandler("ban", ban))
application.add_handler(CommandHandler("unban", unban))
application.add_handler(CommandHandler("stats", stats))
application.add_handler(CommandHandler("export", export))
application.add_handler(CommandHandler("convertpdf", lambda u, c: convert_pdf(u, c, False)))
application.add_handler(CallbackQueryHandler(handle_button))
application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
application.add_handler(MessageHandler(filters.TEXT & filters.REPLY, support_reply))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(r'^https?://'), user_support))
application.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^https?://'), handle_video))


if __name__ == "__main__":
    web.run_app(web_app, port=PORT)
