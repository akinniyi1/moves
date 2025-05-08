import logging
import json
import datetime
import os
import yt_dlp
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, CallbackContext, CallbackQueryHandler

# Firebase Admin SDK
import firebase_admin
from firebase_admin import credentials, db

# Init Firebase
cred = credentials.Certificate("firebase_key.json")
firebase_admin.initialize_app(cred, {
    'databaseURL': 'https://videobot-2bd87-default-rtdb.firebaseio.com/'
})

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Plans
PLANS = {
    "free": {"limit": 3},
    "5_days": {"limit": 9999, "days": 5},
    "10_days": {"limit": 9999, "days": 10},
    "30_days": {"limit": 9999, "days": 30},
}

# Utils
def get_user_ref(user_id):
    return db.reference(f'users/{user_id}')

def get_user_data(user_id):
    ref = get_user_ref(user_id)
    data = ref.get()
    if not data:
        data = {
            "username": "",
            "downloads_today": 0,
            "last_download": "",
            "plan": "free",
            "expiry": ""
        }
        ref.set(data)
    return data

def update_user_data(user_id, data):
    ref = get_user_ref(user_id)
    ref.update(data)

def reset_daily_count_if_needed(user_id):
    user_data = get_user_data(user_id)
    today = str(datetime.date.today())
    if user_data["last_download"] != today:
        update_user_data(user_id, {"downloads_today": 0, "last_download": today})

def user_limit_reached(user_id):
    user_data = get_user_data(user_id)
    reset_daily_count_if_needed(user_id)
    plan = user_data["plan"]
    limit = PLANS.get(plan, PLANS["free"])["limit"]
    return user_data["downloads_today"] >= limit

def increment_download(user_id):
    user_data = get_user_data(user_id)
    user_data["downloads_today"] += 1
    update_user_data(user_id, {"downloads_today": user_data["downloads_today"]})

def is_admin(user_id):
    return str(user_id) == "1378825382"

# Handlers
async def start(update: Update, context: CallbackContext):
    user = update.effective_user
    get_user_data(user.id)
    keyboard = [
        [InlineKeyboardButton("👤 Profile", callback_data="profile")],
    ]
    if is_admin(user.id):
        keyboard.append([InlineKeyboardButton("👥 View Users", callback_data="view_users")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(f"Welcome {user.first_name}! 👋", reply_markup=reply_markup)

async def handle_message(update: Update, context: CallbackContext):
    user = update.effective_user
    text = update.message.text
    if "http" not in text:
        return

    if user_limit_reached(user.id):
        await update.message.reply_text("❌ Daily limit reached. Upgrade your plan to download more.")
        return

    msg = await update.message.reply_text("⏬ Downloading...")
    try:
        ydl_opts = {'outtmpl': 'video.%(ext)s'}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(text, download=True)
            filename = ydl.prepare_filename(info)

        with open(filename, 'rb') as f:
            await update.message.reply_video(f)
        increment_download(user.id)

        keyboard = [[InlineKeyboardButton("🎵 Convert to Audio", callback_data=f"audio|{filename}")]]
        await update.message.reply_text("✅ Done!", reply_markup=InlineKeyboardMarkup(keyboard))

        await msg.delete()
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def button_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user

    if data.startswith("audio|"):
        filename = data.split("|")[1]
        mp3_name = filename.rsplit(".", 1)[0] + ".mp3"

        os.system(f"ffmpeg -i \"{filename}\" -q:a 0 -map a \"{mp3_name}\" -y")
        with open(mp3_name, 'rb') as f:
            await query.message.reply_audio(f)
        await query.message.reply_text("✅ Converted to audio.")
        return

    if data == "profile":
        user_data = get_user_data(user.id)
        plan = user_data["plan"]
        expiry = user_data.get("expiry", "N/A")
        downloads = user_data["downloads_today"]
        await query.message.reply_text(f"👤 *Profile*\nUsername: {user.username}\nPlan: {plan}\nExpiry: {expiry}\nDownloads today: {downloads}", parse_mode="Markdown")
        return

    if data == "view_users" and is_admin(user.id):
        ref = db.reference('users')
        users = ref.get()
        total = len(users) if users else 0
        await query.message.reply_text(f"👥 Total Users: {total}")
        for uid, u in users.items():
            uname = u.get("username", "N/A")
            await query.message.reply_text(f"👤 {uname} ({uid})", reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🎫 5 Days", callback_data=f"upgrade|{uid}|5_days"),
                    InlineKeyboardButton("🎫 10 Days", callback_data=f"upgrade|{uid}|10_days"),
                    InlineKeyboardButton("🎫 30 Days", callback_data=f"upgrade|{uid}|30_days"),
                ]
            ]))
        return

    if data.startswith("upgrade|") and is_admin(user.id):
        _, uid, plan = data.split("|")
        expiry = (datetime.date.today() + datetime.timedelta(days=PLANS[plan]["days"])).isoformat()
        update_user_data(uid, {"plan": plan, "expiry": expiry})
        await query.message.reply_text(f"✅ Upgraded {uid} to {plan} plan until {expiry}")
        return

# App init
async def main():
    TOKEN = os.environ.get("BOT_TOKEN")
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("Bot is running...")
    await app.run_polling()

if __name__ == '__main__':
    asyncio.run(main())
