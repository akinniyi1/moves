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

# Disable SSL verification for yt-dlp
ssl._create_default_https_context = ssl._create_unverified_context
logging.basicConfig(level=logging.INFO)

# --- [CONFIG] ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", 10000))
ADMIN_ID = 1378825382
DATA_FILE = "/mnt/data/users.json"
NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY")
NOWPAYMENTS_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET")
CHANNEL_LINK = "https://t.me/Downloadassaas"

# Initialize bot application
application = Application.builder().token(BOT_TOKEN).build()

# In-memory registries
file_registry = {}
image_collections = {}
pdf_trials = {}
support_messages = {}

# Ensure data directory exists
if not os.path.exists("/mnt/data"):
    os.makedirs("/mnt/data")

# --- [USER STORAGE] ---
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
    expires = user.get("expires")
    if not isinstance(expires, str):
        return False
    try:
        return datetime.utcnow() < datetime.fromisoformat(expires)
    except:
        return False

def is_banned(username):
    return users.get(username, {}).get("banned", False)

def downgrade_expired_users():
    now = datetime.utcnow()
    changed = False
    for username, user in list(users.items()):
        if user.get("plan") == "premium":
            exp = user.get("expires")
            if isinstance(exp, str):
                try:
                    if datetime.fromisoformat(exp) < now:
                        users[username] = {"plan": "free", "downloads": 0}
                        changed = True
                except:
                    pass
    if changed:
        save_users(users)

async def delete_file_later(path, file_id=None):
    await asyncio.sleep(60)
    if os.path.exists(path):
        os.remove(path)
    if file_id:
        file_registry.pop(file_id, None)

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

    if not username:
        return await update.message.reply_text("❌ Please set a Telegram username in your profile.")

    if username not in users:
        users[username] = {"plan": "free", "downloads": 0}
        save_users(users)

    if is_banned(username):
        return await update.message.reply_text("⛔ You are banned from using this bot.")

    buttons = [
        [InlineKeyboardButton("👤 View Profile", callback_data="profile"),
         InlineKeyboardButton("🖼️ Convert to PDF", callback_data="convertpdf_btn")],
        [InlineKeyboardButton("⬆️ Upgrade Your Plan", callback_data="upgrade_options")],
        [InlineKeyboardButton("📢 Join Our Channel", url=CHANNEL_LINK)]
    ]
    await update.message.reply_text(
        f"👋 Hello @{username}!\n\n"
        "This bot supports video downloads from:\n"
        "✅ Facebook, TikTok, Twitter, Instagram\n"
        "❌ YouTube is not supported.\n\n"
        "Free plan:\n"
        "• 3 downloads\n"
        "• 1 PDF trial\n\n"
        "Send a supported video URL to get started.",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# --- [VIDEO HANDLER] ---
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    downgrade_expired_users()
    text = update.message.text.strip()
    if not is_valid_url(text):
        return await update.message.reply_text("❌ Invalid URL.")
    if "youtube.com" in text or "youtu.be" in text:
        return await update.message.reply_text("❌ YouTube is not supported.")

    user = update.effective_user
    username = user.username
    if is_banned(username):
        return await update.message.reply_text("⛔ You are banned.")

    user_data = users.get(username, {"plan": "free", "downloads": 0})
    if not is_premium(user_data) and user_data["downloads"] >= 3:
        return await update.message.reply_text("⛔ Free users: 3 downloads max. Upgrade to premium.")

    filename = generate_filename()
    status = await update.message.reply_text("📥 Downloading...")
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
            ydl.download([text])
        with open(filename, 'rb') as f:
            sent = await update.message.reply_video(
                f, caption="🎉 Here’s your video!",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎧 To Audio", callback_data=f"audio:{filename}")]
                ])
            )
        file_registry[sent.message_id] = filename
        asyncio.create_task(delete_file_later(filename, sent.message_id))
        await status.delete()
        if not is_premium(user_data):
            user_data["downloads"] += 1
            users[username] = user_data
            save_users(users)
    except Exception:
        await status.edit_text("⚠️ Download failed or too large.")

# --- [INLINE BUTTON HANDLER] ---
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    username = query.from_user.username
    user_data = users.get(username, {"plan": "free"})

    if is_banned(username):
        return await query.message.reply_text("⛔ You are banned.")

    if data == "profile":
        downgrade_expired_users()
        if is_premium(user_data):
            exp = datetime.fromisoformat(user_data["expires"])
            msg = f"👤 @{username}\n💼 Premium\n⏰ Expires {exp:%Y-%m-%d %H:%M} UTC"
        else:
            msg = f"👤 @{username}\n💼 Free"
        return await query.message.reply_text(msg)

    if data == "convertpdf_btn":
        return await convert_pdf(update, context, triggered_by_button=True)

    if data.startswith("audio:"):
        _, fname = data.split(":", 1)
        return await convert_to_audio(update, context, fname)

    if data == "upgrade_options":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("1 month – $2", url=f"https://nowpayments.io/payment?price_amount=2&price_currency=usd&order_description=@{username}|30")],
            [InlineKeyboardButton("2 months – $4", url=f"https://nowpayments.io/payment?price_amount=4&price_currency=usd&order_description=@{username}|60")]
        ])
        return await query.message.reply_text("Choose your plan:", reply_markup=kb)

# --- [PDF HANDLER] ---
async def convert_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, triggered_by_button=False):
    user = update.effective_user
    uid = user.id
    uname = user.username
    user_data = users.get(uname, {"plan": "free"})
    if not is_premium(user_data):
        if pdf_trials.get(uid, 0) >= 1:
            return await update.message.reply_text("⛔ Free users: 1 PDF only.")
        pdf_trials[uid] = 1

    imgs = image_collections.get(uid, [])
    if not imgs:
        return await update.message.reply_text("❌ No images received.")
    try:
        pil_imgs = [Image.open(i).convert("RGB") for i in imgs]
        out = generate_filename("pdf")
        pil_imgs[0].save(out, save_all=True, append_images=pil_imgs[1:])
        with open(out, 'rb') as f:
            await update.message.reply_document(f, filename="converted.pdf")
        asyncio.create_task(delete_file_later(out))
        for i in imgs: os.remove(i)
        image_collections[uid] = []
    except:
        await update.message.reply_text("❌ PDF generation failed.")

# --- [IMAGE HANDLER] ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    photo = update.message.photo[-1]
    f = await context.bot.get_file(photo.file_id)
    path = f"/mnt/data/image_{datetime.utcnow():%H%M%S%f}.jpg"
    await f.download_to_drive(path)
    image_collections.setdefault(uid, []).append(path)
    await update.message.reply_text("✅ Image saved. Send more or tap Convert to PDF.")

# --- [ADMIN COMMANDS] ---
async def upgrade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if len(context.args) != 2:
        return await update.message.reply_text("Usage: /upgrade <username> <hours>")
    uname, hrs = context.args
    uname = uname.lstrip("@")
    if uname not in users:
        return await update.message.reply_text("❌ User not found.")
    try:
        h = int(hrs)
        exp = datetime.utcnow() + timedelta(hours=h)
        users[uname].update({"plan": "premium", "expires": exp.isoformat(), "downloads": 0})
        save_users(users)
        await update.message.reply_text(f"✅ @{uname} premium until {exp:%Y-%m-%d %H:%M} UTC")
    except:
        await update.message.reply_text("❌ Invalid hours.")

async def downgrade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not context.args:
        return await update.message.reply_text("Usage: /downgrade <username>")
    uname = context.args[0].lstrip("@")
    if uname in users:
        users[uname] = {"plan": "free", "downloads": 0}
        save_users(users)
        await update.message.reply_text(f"✅ @{uname} downgraded.")
    else:
        await update.message.reply_text("❌ User not found.")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    downgrade_expired_users()
    total = len(users)
    premium = sum(1 for u in users.values() if is_premium(u))
    free = total - premium
    dl = sum(u.get("downloads", 0) for u in users.values())
    await update.message.reply_text(f"📊 Total: {total}\n💎 Premium: {premium}\n👤 Free: {free}\n⬇️ Downloads: {dl}")

async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    p = "/mnt/data/export.csv"
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Username","Plan","Expires","Downloads"])
        for uname,u in users.items():
            w.writerow([uname, u.get("plan","free"), u.get("expires",""), u.get("downloads",0)])
    await update.message.reply_document(InputFile(p), filename="users.csv")

async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not context.args:
        return await update.message.reply_text("Usage: /ban <username>")
    uname = context.args[0].lstrip("@")
    users.setdefault(uname,{})["banned"] = True
    save_users(users)
    await update.message.reply_text(f"⛔ @{uname} banned.")

async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not context.args:
        return await update.message.reply_text("Usage: /unban <username>")
    uname = context.args[0].lstrip("@")
    if users.get(uname,{}).pop("banned",None) is not None:
        save_users(users)
        await update.message.reply_text(f"✅ @{uname} unbanned.")
    else:
        await update.message.reply_text("❌ User not banned.")

# --- [SUPPORT SYSTEM] ---
async def support_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.reply_to_message and update.effective_user.id == ADMIN_ID:
        mid = update.message.reply_to_message.message_id
        if mid in support_messages:
            uid = support_messages[mid]
            await context.bot.send_message(chat_id=uid, text=f"📬 Admin reply:\n{update.message.text}")

async def user_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID: return
    msg = await context.bot.send_message(ADMIN_ID, f"📩 From @{update.effective_user.username}:\n\n{update.message.text}")
    support_messages[msg.message_id] = update.effective_user.id
    await update.message.reply_text("✅ Message sent to admin.")

# --- [NOWPAYMENTS IPN HANDLER] ---
async def ipn_handler(request):
    sig = request.headers.get("x-nowpayments-sig")
    body = await request.text()
    expected = hmac.new(NOWPAYMENTS_IPN_SECRET.encode(), body.encode(), hashlib.sha512).hexdigest()
    if not hmac.compare_digest(sig or "", expected):
        return web.Response(status=403)
    data = await request.json()
    if data.get("payment_status") == "finished":
        desc = data.get("order_description","")
        if "|" in desc:
            uname, days = desc.split("|")
            days = int(days)
            exp = datetime.utcnow() + timedelta(days=days)
            users.setdefault(uname,{"downloads":0}) .update({"plan":"premium","expires":exp.isoformat()})
            save_users(users)
    return web.Response(text="ok")

# --- [WEBHOOK SETUP & MAIN] ---
async def webhook_handler(request):
    d = await request.json()
    upd = Update.de_json(d, application.bot)
    await application.update_queue.put(upd)
    return web.Response(text="ok")

web_app = web.Application()
web_app.router.add_post("/webhook", webhook_handler)
web_app.router.add_post("/ipn", ipn_handler)

web_app.on_startup.append(lambda app: application.initialize())
web_app.on_startup.append(lambda app: application.start())
web_app.on_startup.append(lambda app: application.bot.set_webhook(f"{APP_URL}/webhook"))
web_app.on_cleanup.append(lambda app: application.stop())
web_app.on_cleanup.append(lambda app: application.shutdown())

application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video))
application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
application.add_handler(CallbackQueryHandler(handle_button))
application.add_handler(CommandHandler("convertpdf", lambda u,c: convert_pdf(u,c,False)))
application.add_handler(CommandHandler("upgrade", upgrade_cmd))
application.add_handler(CommandHandler("downgrade", downgrade_cmd))
application.add_handler(CommandHandler("stats", stats_cmd))
application.add_handler(CommandHandler("export", export_cmd))
application.add_handler(CommandHandler("ban", ban_cmd))
application.add_handler(CommandHandler("unban", unban_cmd))
application.add_handler(MessageHandler(filters.TEXT & filters.REPLY, support_reply))
application.add_handler(MessageHandler(filters.TEXT & ~filters.Regex(r'^https?://'), user_support))

if __name__ == "__main__":
    web.run_app(web_app, port=PORT)
