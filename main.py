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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
import aiohttp
import uuid
from fpdf import FPDF

ssl._create_default_https_context = ssl._create_unverified_context
logging.basicConfig(level=logging.INFO)

BOT_TOKEN      = os.getenv("BOT_TOKEN")
APP_URL        = os.getenv("RENDER_EXTERNAL_URL")
PORT           = int(os.getenv("PORT", 10000))
ADMIN_ID       = 1378825382
CHANNEL_URL    = "https://t.me/Downloadassaas"
DATA_FILE      = "/mnt/data/users.json"
NOW_API_KEY    = os.getenv("NOWPAYMENTS_API_KEY")
NOW_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET")

application      = Application.builder().token(BOT_TOKEN).build()
file_registry    = {}
image_collections = {}
pdf_trials       = {}
support_messages = {}
pending_invoices = {}      # invoice_id -> (username, amount)
broadcast_states = {}      # admin_id -> {"stage":..., "usernames":[...]}

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
        if datetime.utcnow() < exp_dt:
            return True
        # expired → downgrade
        user["plan"] = "free"
        user["downloads"] = 0
        user.pop("expires", None)
        save_users(users)
    except:
        pass
    return False

def downgrade_expired_users():
    now = datetime.utcnow()
    for username, user in users.items():
        exp = user.get("expires")
        if isinstance(exp, str):
            try:
                if datetime.fromisoformat(exp) < now:
                    users[username] = {
                        "plan": "free", "downloads": 0,
                        "banned": user.get("banned", False),
                        "text_pdf_trial": user.get("text_pdf_trial", False),
                        "video_gif_trial": user.get("video_gif_trial", False)
                    }
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
    asyncio.create_task(cancel_invoice_later(inv_id))
    return data

async def cancel_invoice_later(inv_id):
    await asyncio.sleep(20 * 60)
    url = f"https://api.nowpayments.io/v1/invoice/{inv_id}"
    headers = {"x-api-key": NOW_API_KEY}
    async with aiohttp.ClientSession() as session:
        await session.delete(url, headers=headers)

async def ipn_handler(request):
    data = await request.json()
    if data.get("ipn_secret") != NOW_IPN_SECRET:
        return web.Response(text="invalid secret", status=400)
    if data.get("payment_status") == "finished":
        inv_id = data.get("invoice_id")
        tup = pending_invoices.pop(inv_id, None)
        if tup:
            username, amount = tup
        else:
            parts = data.get("order_id", "").split(":")
            username, amount = parts[0], float(parts[1])
        days = 30 if amount == 2.0 else 60
        exp = datetime.utcnow() + timedelta(days=days)
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
        users[username] = {
            "plan": "free",
            "downloads": 0,
            "banned": False,
            "text_pdf_trial": False,
            "video_gif_trial": False
        }
        save_users(users)

    if username and users.get(username, {}).get("banned"):
        return await update.message.reply_text("⛔ You are banned from using this bot.")

    buttons = [
        [InlineKeyboardButton("👤 View Profile", callback_data="profile"),
         InlineKeyboardButton("🖼️ Convert to PDF", callback_data="convertpdf_btn")],
        [InlineKeyboardButton("💳 Upgrade Your Plan", callback_data="upgrade_plan")],
        [InlineKeyboardButton("✉️ Text to PDF", callback_data="text_pdf")],
        [InlineKeyboardButton("📣 Join Our Channel", url=CHANNEL_URL)]
    ]
    if user.id == ADMIN_ID:
        buttons.append([InlineKeyboardButton("📢 Broadcast Usernames", callback_data="admin_broadcast")])

    await update.message.reply_text(
        f"👋 Hello @{username or user.first_name}!\n\n"
        "This bot supports downloading videos from:\n"
        "✅ Facebook, TikTok, Twitter, Instagram\n"
        "❌ YouTube is not supported.\n\n"
        "Free Users:\n• 3 video downloads\n• 1 PDF conversion trial\n• 1 Text-to-PDF trial\n\n"
        "Send a supported video link or use the menu below.",
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

    user = update.effective_user
    username = user.username
    if username and users.get(username, {}).get("banned"):
        return await update.message.reply_text("⛔ You are banned.")

    udata = users.get(username, {"plan": "free", "downloads": 0})
    if not is_premium(udata) and udata["downloads"] >= 3:
        return await update.message.reply_text("⛔ Free users limited to 3 downloads.")

    fn = generate_filename()
    status = await update.message.reply_text("📥 Downloading…")
    ydl_opts = {
        'outtmpl': fn,
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'quiet': True,
        'noplaylist': True,
        'max_filesize': 50 * 1024 * 1024
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        with open(fn, 'rb') as f:
            sent = await update.message.reply_video(
                f,
                caption="🎉 Here's your video!",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🎧 Convert to Audio", callback_data=f"audio:{fn}"),
                    InlineKeyboardButton("🎞️ Convert to GIF", callback_data=f"gif:{fn}")
                ]])
            )
        file_registry[sent.message_id] = fn
        asyncio.create_task(delete_file_later(fn, sent.message_id))
        await status.delete()
        if not is_premium(udata):
            udata["downloads"] += 1
            users[username] = udata
            save_users(users)
    except:
        await status.edit_text("⚠️ Download failed or too large.")


# --- [INLINE CALLBACK HANDLER] ---
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    username = query.from_user.username
    if username and users.get(username, {}).get("banned"):
        return await query.edit_message_text("⛔ You are banned.")

    data = query.data

    # Text-to-PDF trigger
    if data == "text_pdf":
        await query.message.reply_text("📄 Send text for PDF (1 trial).")
        context.user_data["awaiting_text_pdf"] = True
        return

    # Broadcast: collect usernames
    if data == "admin_broadcast":
        if query.from_user.id == ADMIN_ID:
            chat = str(query.from_user.id)
            broadcast_states[chat] = {"stage": "awaiting_usernames"}
            await query.message.reply_text("📋 Send usernames (space- or line-separated):")
        else:
            await query.answer("Unauthorized", show_alert=True)
        return

    # GIF conversion
    if data.startswith("gif:"):
        fn = data.split("gif:")[1]
        u = users.get(username, {})
        if not is_premium(u) and u.get("video_gif_trial"):
            return await query.message.reply_text("⛔ GIF trial used.")
        if not is_premium(u):
            users[username]["video_gif_trial"] = True
            save_users(users)
        try:
            out = f"/mnt/data/{username}_conv.gif"
            clip = ffmpeg.input(fn, ss=0, t=10).filter('fps', fps=10, scale='320:-1:flags=lanczos')
            ffmpeg.output(clip, out).run(overwrite_output=True)
            await query.message.reply_document(open(out, 'rb'), filename="converted.gif")
            os.remove(out)
        except:
            await query.message.reply_text("❌ GIF conversion failed.")
        return

    # Profile, upgrade, convertpdf_btn, audio: etc. go here as before
    if data == "profile":
        downgrade_expired_users()
        ud = users.get(username, {"plan": "free"})
        if is_premium(ud):
            exp_dt = datetime.fromisoformat(ud["expires"])
            msg = f"👤 @{username}\n💼 Premium\n⏰ Expires {exp_dt:%Y-%m-%d %H:%M} UTC"
        else:
            msg = f"👤 @{username}\n💼 Free"
        await query.message.reply_text(msg)
        return

    # implement other existing callback_data branches…

# --- [IMAGE TO PDF] ---
async def convert_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, triggered_by_button=False):
    user_id = update.effective_user.id
    username = update.effective_user.username
    if username and users.get(username, {}).get("banned"):
        return await update.message.reply_text("⛔ You are banned.")

    ud = users.get(username, {"plan": "free"})
    if not is_premium(ud):
        if pdf_trials.get(user_id, 0) >= 1:
            return await update.message.reply_text("⛔ PDF trial used.")
        pdf_trials[user_id] = 1

    imgs = image_collections.get(user_id, [])
    if not imgs:
        return await update.message.reply_text("❌ No images.")
    try:
        pil = [Image.open(i).convert("RGB") for i in imgs]
        pdf_path = generate_filename("pdf")
        pil[0].save(pdf_path, save_all=True, append_images=pil[1:])
        with open(pdf_path, 'rb') as f:
            await update.message.reply_document(f, filename="converted.pdf")
        asyncio.create_task(delete_file_later(pdf_path))
        for i in imgs: os.remove(i)
        image_collections[user_id] = []
    except:
        await update.message.reply_text("❌ PDF failed.")


# --- [PHOTO HANDLER] ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    username = update.effective_user.username
    if username and users.get(username, {}).get("banned"):
        return await update.message.reply_text("⛔ You are banned.")
    photo = update.message.photo[-1]
    f = await context.bot.get_file(photo.file_id)
    path = f"image_{datetime.utcnow():%H%M%S%f}.jpg"
    await f.download_to_drive(path)
    image_collections.setdefault(uid, []).append(path)
    await update.message.reply_text("✅ Image received. Send more or click /convertpdf")


# --- [TEXT MESSAGE HANDLER] ---
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = str(update.effective_user.id)
    st = broadcast_states.get(chat, {})

    # Receive username list
    if st.get("stage") == "awaiting_usernames" and update.effective_user.id == ADMIN_ID:
        names = update.message.text.replace('\n', ' ').split()
        broadcast_states[chat] = {"stage": "awaiting_message", "usernames": [n.lstrip("@") for n in set(names)]}
        return await update.message.reply_text("✅ Got usernames. Now send broadcast (text/photo/video/document).")

    # Perform broadcast
    if st.get("stage") == "awaiting_message" and update.effective_user.id == ADMIN_ID:
        sent = 0
        failed = 0
        for uname in st["usernames"]:
            info = users.get(uname)
            if not info or info.get("banned"):
                failed += 1
                continue
            rec = f"@{uname}"
            msg = update.message
            try:
                if msg.text:
                    await context.bot.send_message(rec, msg.text)
                elif msg.photo:
                    await context.bot.send_photo(rec, msg.photo[-1].file_id, caption=msg.caption or "")
                elif msg.video:
                    await context.bot.send_video(rec, msg.video.file_id, caption=msg.caption or "")
                elif msg.document:
                    await context.bot.send_document(rec, msg.document.file_id, caption=msg.caption or "")
                else:
                    continue
                sent += 1
            except:
                failed += 1
        del broadcast_states[chat]
        return await update.message.reply_text(f"✅ Sent to {sent}, failed {failed}.")

    # Text-to-PDF
    if context.user_data.get("awaiting_text_pdf"):
        context.user_data.pop("awaiting_text_pdf", None)
        uname = update.effective_user.username or "User"
        ud = users.get(uname, {"plan": "free"})
        if not is_premium(ud) and ud.get("text_pdf_trial"):
            return await update.message.reply_text("⛔ PDF trial used.")
        if not is_premium(ud):
            users[uname]["text_pdf_trial"] = True
            save_users(users)
        txt = update.message.text
        pdf = FPDF()
        pdf.add_page()
        pdf.set_auto_page_break(True, 15)
        pdf.set_font("Arial", size=12)
        for line in txt.splitlines():
            pdf.multi_cell(0, 10, line)
        path = f"/mnt/data/{uname}_text.pdf"
        pdf.output(path)
        return await update.message.reply_document(open(path, 'rb'), filename="converted_text.pdf")

    # Fallback support
    if update.effective_user.id != ADMIN_ID and update.message.text:
        fw = await context.bot.send_message(ADMIN_ID, f"📩 @{update.effective_user.username}: {update.message.text}")
        support_messages[fw.message_id] = update.effective_user.id
        return await update.message.reply_text("✅ Sent to admin.")


# --- [ADMIN COMMANDS] ---
async def upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if len(args) != 2:
        return await update.message.reply_text("Usage: /upgrade <username> <hours>")
    uname, hrs = args
    uname = uname.lstrip("@")
    if uname not in users:
        return await update.message.reply_text("❌ User not found.")
    try:
        h = int(hrs)
        exp = datetime.utcnow() + timedelta(hours=h)
        users[uname]["plan"] = "premium"
        users[uname]["expires"] = exp.isoformat()
        save_users(users)
        return await update.message.reply_text(f"✅ Upgraded @{uname} until {exp:%Y-%m-%d %H:%M} UTC")
    except:
        return await update.message.reply_text("❌ Invalid hours")

async def downgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if not args:
        return await update.message.reply_text("Usage: /downgrade <username>")
    uname = args[0].lstrip("@")
    if uname in users:
        users[uname].update({"plan": "free", "downloads": 0})
        save_users(users)
        return await update.message.reply_text(f"✅ Downgraded @{uname}")
    return await update.message.reply_text("❌ User not found")

async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if len(args) != 1:
        return await update.message.reply_text("Usage: /ban <username>")
    uname = args[0].lstrip("@")
    if uname in users:
        users[uname]["banned"] = True
        save_users(users)
        return await update.message.reply_text(f"⛔ Banned @{uname}")
    return await update.message.reply_text("❌ User not found")

async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if len(args) != 1:
        return await update.message.reply_text("Usage: /unban <username>")
    uname = args[0].lstrip("@")
    if uname in users and users[uname].get("banned"):
        users[uname]["banned"] = False
        save_users(users)
        return await update.message.reply_text(f"✅ Unbanned @{uname}")
    return await update.message.reply_text("❌ User not found or not banned")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    downgrade_expired_users()
    tot = len(users)
    prem = sum(1 for u in users.values() if u.get("plan") == "premium")
    free = tot - prem
    dl = sum(u.get("downloads", 0) for u in users.values())
    await update.message.reply_text(f"📊 Total:{tot} Premium:{prem} Free:{free} Downloads:{dl}")

async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    path = "/mnt/data/export.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Username", "Plan", "Expires", "Banned", "TextPDF", "VideoGIF"])
        for uname, d in users.items():
            w.writerow([
                uname,
                d.get("plan", "free"),
                d.get("expires", "N/A"),
                d.get("banned", False),
                d.get("text_pdf_trial", False),
                d.get("video_gif_trial", False)
            ])
    with open(path, "rb") as f:
        await update.message.reply_document(f, filename="users.csv")


# --- [SUPPORT SYSTEM] ---
async def support_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.reply_to_message and update.effective_user.id == ADMIN_ID:
        mid = update.message.reply_to_message.message_id
        if mid in support_messages:
            uid = support_messages[mid]
            await context.bot.send_message(uid, f"📬 Admin: {update.message.text}")

async def user_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        return
    fw = await context.bot.send_message(ADMIN_ID, f"📩 @{update.effective_user.username}: {update.message.text}")
    support_messages[fw.message_id] = update.effective_user.id
    await update.message.reply_text("✅ Sent to admin")


# --- [WEBHOOK SETUP] ---
web_app = web.Application()

async def webhook_handler(request):
    try:
        data = await request.json()
        upd = Update.de_json(data, application.bot)
        await application.update_queue.put(upd)
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
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(r'^https?://'), handle_text))
application.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^https?://'), handle_video))

if __name__ == "__main__":
    web.run_app(web_app, port=PORT)
