import os
import yt_dlp
import logging
import json
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes
from datetime import datetime, timedelta
import firebase_admin
from firebase_admin import credentials, firestore

# --- Firebase Setup ---
cred = credentials.Certificate("firebase.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

# --- Bot Setup ---
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 1378825382  # Replace with your Telegram ID

DAILY_LIMIT = 3

def get_user_data(user_id, username=""):
    doc_ref = db.collection("users").document(str(user_id))
    doc = doc_ref.get()
    if doc.exists:
        return doc.to_dict()
    else:
        data = {
            "username": username,
            "downloads": 0,
            "plan": "free",
            "expires": None
        }
        doc_ref.set(data)
        return data

def save_user_data(user_id, data):
    db.collection("users").document(str(user_id)).set(data)

def update_download_count(user_id, username):
    data = get_user_data(user_id, username)
    data["downloads"] += 1
    save_user_data(user_id, data)

def reset_all_downloads():
    users_ref = db.collection("users")
    for doc in users_ref.stream():
        user = doc.to_dict()
        user["downloads"] = 0
        db.collection("users").document(doc.id).set(user)

def get_total_users():
    return len(list(db.collection("users").stream()))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_user_data(user.id, user.username)

    keyboard = [
        [InlineKeyboardButton("👤 My Profile", callback_data='profile')],
        [InlineKeyboardButton("📊 Admin Panel", callback_data='admin')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(f"👋 Hello {user.first_name}, send me any video link to download.", reply_markup=reply_markup)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_data = get_user_data(user.id, user.username)

    now = datetime.utcnow()

    # Check subscription
    if user_data["plan"] != "free":
        if user_data["expires"]:
            expiry = datetime.strptime(user_data["expires"], "%Y-%m-%d")
            if expiry < now:
                user_data["plan"] = "free"
                user_data["downloads"] = 0
                user_data["expires"] = None
                await update.message.reply_text("⏰ Your plan has expired. You are now on the free plan.")

    if user_data["plan"] == "free" and user_data["downloads"] >= DAILY_LIMIT:
        await update.message.reply_text("🚫 Daily limit reached. Upgrade your plan for more downloads.")
        return

    url = update.message.text

    keyboard = [
        [InlineKeyboardButton("🎵 Convert to Audio", callback_data=f'audio|{url}')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("⏬ Processing your video...", reply_markup=reply_markup)

    try:
        ydl_opts = {
            'outtmpl': f'{user.id}.%(ext)s',
            'format': 'bestvideo+bestaudio/best',
            'noplaylist': True,
            'quiet': True
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            file_path = ydl.prepare_filename(info)
            await update.message.reply_video(video=open(file_path, 'rb'))
            os.remove(file_path)

        update_download_count(user.id, user.username)

    except Exception as e:
        logging.error(e)
        await update.message.reply_text("❌ Failed to download video. Try another link.")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    user_data = get_user_data(user.id, user.username)

    if query.data.startswith('audio|'):
        url = query.data.split('|', 1)[1]
        await query.edit_message_text("🎵 Converting to audio...")

        try:
            ydl_opts = {
                'outtmpl': f'{user.id}.%(ext)s',
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'quiet': True
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                file_path = ydl.prepare_filename(info).rsplit('.', 1)[0] + ".mp3"
                await query.message.reply_audio(audio=open(file_path, 'rb'))
                os.remove(file_path)

        except Exception as e:
            logging.error(e)
            await query.message.reply_text("❌ Failed to convert video to audio.")

    elif query.data == 'profile':
        plan = user_data['plan']
        downloads = user_data['downloads']
        expires = user_data.get("expires", "N/A")
        profile_msg = f"👤 *Username:* @{user.username or 'N/A'}\n💼 *Plan:* {plan}\n⬇️ *Downloads today:* {downloads}/{DAILY_LIMIT if plan == 'free' else '∞'}\n⏳ *Expires:* {expires}"
        await query.message.reply_text(profile_msg, parse_mode='Markdown')

    elif query.data == 'admin' and user.id == ADMIN_ID:
        total_users = get_total_users()
        buttons = [
            [InlineKeyboardButton("🔥 Upgrade 5d", callback_data="upgrade_5")],
            [InlineKeyboardButton("🔥 Upgrade 10d", callback_data="upgrade_10")],
            [InlineKeyboardButton("🔥 Upgrade 30d", callback_data="upgrade_30")],
            [InlineKeyboardButton("🔄 Reset Downloads", callback_data="reset_dl")]
        ]
        await query.message.reply_text(f"👥 Total users: {total_users}", reply_markup=InlineKeyboardMarkup(buttons))

    elif query.data.startswith("upgrade_"):
        days = int(query.data.replace("upgrade_", ""))
        context.user_data["upgrade_days"] = days
        await query.message.reply_text("✏️ Send the @username of the user to upgrade:")

    elif query.data == "reset_dl":
        reset_all_downloads()
        await query.message.reply_text("✅ All download counts reset.")

async def handle_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if "upgrade_days" not in context.user_data:
        return

    username = update.message.text.replace("@", "")
    days = context.user_data.pop("upgrade_days")

    users_ref = db.collection("users")
    query = users_ref.where("username", "==", username).limit(1).stream()
    found = False

    for doc in query:
        found = True
        user = doc.to_dict()
        expiry = datetime.utcnow() + timedelta(days=days)
        user["plan"] = "premium"
        user["downloads"] = 0
        user["expires"] = expiry.strftime("%Y-%m-%d")
        db.collection("users").document(doc.id).set(user)
        await update.message.reply_text(f"✅ Upgraded @{username} for {days} days.")
        break

    if not found:
        await update.message.reply_text("❌ User not found.")

def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & filters.User(ADMIN_ID), handle_upgrade))

    app.run_polling()

if __name__ == "__main__":
    main()
