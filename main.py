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
ADMIN_ID = 1378825382   # ← Your Admin ID
APP_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", 10000))
DATA_FILE = "/mnt/data/users.json"
NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY")
NOWPAYMENTS_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET")
CHANNEL_LINK = "https://t.me/Downloadassaas"

application = Application.builder().token(BOT_TOKEN).build()

file_registry = {}
image_collections = {}
pdf_trials = {}
support_messages = {}

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
    return bool(re.match(r'https?://', text))

def generate_filename(ext="mp4"):
    return f"file_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}.{ext}"

def is_premium(user):
    if user.get("plan") != "premium":
        return False
    exp = user.get("expires")
    if not isinstance(exp, str):
        return False
    try:
        return datetime.utcnow() < datetime.fromisoformat(exp)
    except:
        return False

def is_banned(username):
    return users.get(username, {}).get("banned", False)

def downgrade_expired_users():
    now = datetime.utcnow()
    changed = False
    for u, info in list(users.items()):
        if info.get("plan") == "premium":
            exp = info.get("expires")
            if isinstance(exp, str):
                try:
                    if datetime.fromisoformat(exp) < now:
                        users[u] = {"plan": "free", "downloads": 0}
                        changed = True
                except:
                    pass
    if changed:
        save_users(users)

async def delete_file_later(path, mid=None):
    await asyncio.sleep(60)
    if os.path.exists(path):
        os.remove(path)
    if mid:
        file_registry.pop(mid, None)

# --- [AUDIO CONVERSION] ---
async def convert_to_audio(update: Update, context: ContextTypes.DEFAULT_TYPE, path):
    audio = path.replace(".mp4", ".mp3")
    try:
        ffmpeg.input(path).output(audio).run(overwrite_output=True)
        with open(audio, "rb") as f:
            await update.callback_query.message.reply_audio(f, filename=os.path.basename(audio))
        os.remove(audio)
    except:
        await update.callback_query.message.reply_text("❌ Audio conversion failed.")

# --- [START HANDLER] ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    downgrade_expired_users()
    user = update.effective_user
    un = user.username
    if not un:
        return await update.message.reply_text("❌ Please set a Telegram username in your profile.")
    if un not in users:
        users[un] = {"plan": "free", "downloads": 0}
        save_users(users)
    if is_banned(un):
        return await update.message.reply_text("⛔ You are banned from using this bot.")

    # Full welcome message
    buttons = [
        [InlineKeyboardButton("👤 View Profile", callback_data="profile"),
         InlineKeyboardButton("🖼️ Convert to PDF", callback_data="convertpdf")],
        [InlineKeyboardButton("⬆️ Upgrade Your Plan", callback_data="upgrade_menu")],
        [InlineKeyboardButton("📢 Join Our Channel", url=CHANNEL_LINK)]
    ]
    await update.message.reply_text(
        f"👋 Hello @{un}!\n\n"
        "This bot supports downloading videos from:\n"
        "✅ Facebook, TikTok, Twitter, Instagram\n"
        "❌ YouTube is not supported.\n\n"
        "Free Plan:\n• 3 video downloads\n• 1 PDF conversion trial\n\n"
        "Send a supported video link to begin.",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# --- [VIDEO HANDLER] ---
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    downgrade_expired_users()
    url = update.message.text.strip()
    if not is_valid_url(url):
        return await update.message.reply_text("❌ Invalid URL.")
    if "youtube.com" in url or "youtu.be" in url:
        return await update.message.reply_text("❌ YouTube not supported.")

    un = update.effective_user.username
    if is_banned(un):
        return await update.message.reply_text("⛔ You are banned.")

    ud = users.get(un, {"plan": "free", "downloads": 0})
    if not is_premium(ud) and ud["downloads"] >= 3:
        return await update.message.reply_text("⛔ Free: 3 downloads max.")

    fn = generate_filename()
    st = await update.message.reply_text("📥 Downloading...")
    opts = {'outtmpl': fn, 'format': 'bestvideo+bestaudio/best',
            'merge_output_format': 'mp4', 'quiet': True,
            'noplaylist': True, 'max_filesize': 50 * 1024 * 1024}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        with open(fn, "rb") as f:
            sent = await update.message.reply_video(
                f, caption="🎉 Here’s your video!",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🎧 To Audio", callback_data=f"audio:{fn}")]]
                )
            )
        file_registry[sent.message_id] = fn
        asyncio.create_task(delete_file_later(fn, sent.message_id))
        await st.delete()
        if not is_premium(ud):
            ud["downloads"] += 1
            users[un] = ud
            save_users(users)
    except:
        await st.edit_text("⚠️ Download failed or too large.")

# --- [INLINE HANDLER] ---
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data
    un = q.from_user.username
    ud = users.get(un, {"plan": "free"})

    if is_banned(un):
        return await q.message.reply_text("⛔ You are banned.")

    if d == "profile":
        downgrade_expired_users()
        if is_premium(ud):
            exp = datetime.fromisoformat(ud["expires"])
            msg = f"👤 @{un}\n💎 Premium until {exp:%Y-%m-%d %H:%M} UTC"
        else:
            msg = f"👤 @{un}\n👤 Free Plan"
        return await q.message.reply_text(msg)

    if d == "convertpdf":
        return await convert_pdf(update, context, True)

    if d.startswith("audio:"):
        _, p = d.split(":", 1)
        return await convert_to_audio(update, context, p)

    if d == "upgrade_menu":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("1 month – $2", callback_data="pay_2_1")],
            [InlineKeyboardButton("2 months – $4", callback_data="pay_4_2")]
        ])
        return await q.message.reply_text("Choose:", reply_markup=kb)

    if d in ("pay_2_1", "pay_4_2"):
        months = 1 if d == "pay_2_1" else 2
        amount = 2 * months
        invoice = {
            "price_amount": amount,
            "price_currency": "usd",
            "order_id": f"{un}|{30*months}",
            "ipn_callback_url": f"{APP_URL}/ipn"
        }
        headers = {"x-api-key": NOWPAYMENTS_API_KEY}
        async with aiohttp.ClientSession() as sess:
            r = await sess.post("https://api.nowpayments.io/v1/invoice", json=invoice, headers=headers)
            res = await r.json()
        if "invoice_url" in res:
            return await q.message.reply_text(f"Pay here: {res['invoice_url']}")
        logging.error("NP err: %s", res)
        return await q.message.reply_text("❌ Payment init failed.")

# --- [PDF HANDLER] ---
async def convert_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, triggered_by_button=False):
    msg = update.callback_query.message if triggered_by_button else update.message
    uid = update.effective_user.id
    un = update.effective_user.username
    ud = users.get(un, {"plan": "free"})
    if not is_premium(ud):
        if pdf_trials.get(uid, 0) >= 1:
            return await msg.reply_text("⛔ Free: 1 PDF")
        pdf_trials[uid] = 1

    imgs = image_collections.get(uid, [])
    if not imgs:
        return await msg.reply_text("❌ No images received.")

    try:
        pdf_path = generate_filename("pdf")
        pages = [Image.open(p).convert("RGB") for p in imgs]
        pages[0].save(pdf_path, save_all=True, append_images=pages[1:])
        with open(pdf_path, "rb") as f:
            await msg.reply_document(f, filename="converted.pdf")
        asyncio.create_task(delete_file_later(pdf_path))
        for p in imgs: os.remove(p)
        image_collections[uid] = []
    except:
        await msg.reply_text("❌ PDF failed.")

# --- [IMAGE HANDLER] ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    img = update.message.photo[-1]
    f = await context.bot.get_file(img.file_id)
    path = f"/mnt/data/{uuid.uuid4()}.jpg"
    await f.download_to_drive(path)
    image_collections.setdefault(uid, []).append(path)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Convert to PDF", callback_data="convertpdf")]])
    await update.message.reply_text("✅ Image saved. Convert now?", reply_markup=kb)

# --- [ADMIN COMMANDS] ---
async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    data = load_users()
    path = "/mnt/data/export.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Username","Plan","Expires","Downloads","Banned"])
        for u,info in users.items():
            w.writerow([u, info.get("plan"), info.get("expires",""), info.get("downloads",0), info.get("banned",False)])
    await update.message.reply_document(InputFile(path))

# You can re-add /stats, /upgrade, /downgrade, /ban, /unban here as before—
# they already check ADMIN_ID and use save_users/load_users.

# --- [IPN HANDLER] ---
async def ipn_handler(request):
    sig = request.headers.get("x-nowpayments-sig","")
    body = await request.text()
    expected = hmac.new(NOWPAYMENTS_IPN_SECRET.encode(), body.encode(), hashlib.sha512).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return web.Response(status=403)
    data = await request.json()
    if data.get("payment_status") == "finished":
        od = data.get("order_description","")
        if "|" in od:
            uname, days = od.split("|")
            days = int(days)
            exp = datetime.utcnow() + timedelta(days=int(days))
            users.setdefault(uname,{"downloads":0}) .update({"plan":"premium","expires":exp.isoformat()})
            save_users(users)
    return web.Response(text="ok")

# --- [WEBHOOK SETUP] ---
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

# --- [HANDLERS] ---
application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video))
application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
application.add_handler(CallbackQueryHandler(handle_button))
application.add_handler(CommandHandler("export", export_cmd))
# re-add your other admin commands here...

if __name__ == "__main__":
    web.run_app(web_app, port=PORT)
