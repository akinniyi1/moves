import os
import re
import ssl
import json
import logging
import yt_dlp
import ffmpeg
import asyncio
import asyncpg
from datetime import datetime, timedelta
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import GetHistoryRequest

ssl._create_default_https_context = ssl._create_unverified_context
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", 10000))
ADMIN_ID = 1378825382
DB_URL = os.getenv("DATABASE_URL")

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")
tele_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

application = Application.builder().token(BOT_TOKEN).build()
db_pool = None
user_states = {}
file_registry = {}

GROUPS = [
    "thepythoncode", "programmingtalks", "learnprogramming", "pythontelegram", "codehub", "python_codes",
    "webdev", "techguide", "datasciencechat", "machinelearningchat", "developer_chat", "deeplearning_ai",
    "cyber_security_chat", "linuxchat", "freelancechat", "techupdates", "opensourcechannel", "coding4you",
    "androiddev", "flutterdev", "gamedev", "web3chat", "aihub", "djangochat", "reactjschat", "codingtalks",
    "pythonjobs", "mljobs", "remoteworkchat", "codingcommunity", "codingbuddies", "techcareers", "chatgptcommunity",
    "techstartups", "openaiupdates", "softwaretesting", "cloudcomputinghub", "devtools", "linuxcommands",
    "codingarena", "iotcommunity", "datasciencenews", "blockchainhub", "kuberneteschat", "fullstackdevs",
    "backendengineers", "frontendchat", "techmemes", "aiandyou", "programmers_life", "devopslife",
    "remoteworkers", "learnai", "javaprogramming", "typescriptdev", "vuejsclub", "nextjscommunity",
    "pythontutorials", "codingresources", "gitlabchat", "stackchat", "programminghub", "pythoncommunity",
    "coderesources", "pycoder", "sqllearning", "learnlinux", "linuxtricks", "programminghelpline",
    "devmentors", "hackathonhub", "dockercommunity", "datastructures", "leetcodesolutions", "csstudents",
    "programmingstudents", "ethicalhackers", "computerscienceclass", "devcareer", "careerindev", "aiinsights",
    "techtipsdaily", "pythonlessons", "apilearning", "developersden", "frontendtricks", "buildinpublic"
]

# ---------- DB HELPERS ----------

async def get_user(user_id):
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
        if not user:
            await conn.execute(
                "INSERT INTO users (id, name, plan, downloads, expires) VALUES ($1, $2, $3, $4, $5)",
                user_id, "", "free", json.dumps({}), None
            )
            return {"id": user_id, "name": "", "plan": "free", "downloads": {}, "expires": None}
        downloads = user["downloads"]
        if isinstance(downloads, str):
            try:
                downloads = json.loads(downloads)
            except:
                downloads = {}
        return {
            "id": user["id"],
            "name": user["name"],
            "plan": user["plan"],
            "downloads": downloads,
            "expires": user["expires"]
        }

async def update_user(user_id, data):
    async with db_pool.acquire() as conn:
        user = await get_user(user_id)
        user.update(data)
        downloads_json = json.dumps(user["downloads"])
        await conn.execute(
            """
            INSERT INTO users (id, name, plan, downloads, expires)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (id) DO UPDATE SET
              name = $2, plan = $3, downloads = $4, expires = $5
            """,
            user["id"], user["name"], user["plan"], downloads_json, user["expires"]
        )

async def can_download(user_id):
    user = await get_user(user_id)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    downloads_today = user["downloads"].get(today, 0)
    if user["plan"] == "free":
        return downloads_today < 3
    else:
        expiry = user.get("expires")
        if expiry and expiry < datetime.utcnow().date():
            await update_user(user_id, {"plan": "free", "expires": None})
            return downloads_today < 3
        return True

async def log_download(user_id):
    user = await get_user(user_id)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    downloads = user["downloads"]
    downloads[today] = downloads.get(today, 0) + 1
    await update_user(user_id, {"downloads": downloads})

# ---------- UTILS ----------

def is_valid_url(text):
    return re.match(r'https?://', text)

def generate_filename(ext="mp4"):
    return f"video_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}.{ext}"

async def delete_file_later(file_path, file_id):
    await asyncio.sleep(60)
    if os.path.exists(file_path):
        os.remove(file_path)
    file_registry.pop(file_id, None)

async def convert_to_audio(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path):
    audio_path = file_path.replace(".mp4", ".mp3")
    try:
        ffmpeg.input(file_path).output(audio_path).run(overwrite_output=True)
        with open(audio_path, 'rb') as f:
            await update.callback_query.message.reply_audio(f, filename=os.path.basename(audio_path))
        os.remove(audio_path)
    except Exception:
        await update.callback_query.message.reply_text("❌ Failed to convert to audio.")

# ---------- HANDLERS ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update_user(user.id, {"name": user.first_name or ""})
    buttons = [
        [InlineKeyboardButton("👤 View Profile", callback_data="profile")],
        [InlineKeyboardButton("🔍 Telegram Keyword Search", callback_data="keyword_search")],
        [InlineKeyboardButton("👥 Total Users", callback_data="total_users")] if user.id == ADMIN_ID else []
    ]
    await update.message.reply_text(
        f"👋 Hello {user.first_name or 'there'}! Send me a video link to download.\n\n"
        "📌 Free users: 3 downloads/day, 50MB limit.\n"
        "🎧 Use 'Convert to Audio' within 1 min or file is auto-deleted.",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    url = update.message.text.strip()
    if not is_valid_url(url):
        await update.message.reply_text("❌ That doesn't look like a valid link.")
        return
    if not await can_download(user.id):
        await update.message.reply_text("⛔ You've reached your daily limit.")
        return
    filename = generate_filename()
    status_msg = await update.message.reply_text("📥 Downloading video...")
    ydl_opts = {
        'outtmpl': filename,
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'quiet': True,
        'noplaylist': True,
        'geo_bypass': True,
        'nocheckcertificate': True,
        'http_headers': {'User-Agent': 'Mozilla/5.0'},
        'max_filesize': 50 * 1024 * 1024
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        await log_download(user.id)
        with open(filename, 'rb') as f:
            sent = await update.message.reply_video(
                f,
                caption="🎉 Here's your video!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎧 Convert to Audio", callback_data=f"audio:{filename}")]])
            )
        file_registry[sent.message_id] = filename
        asyncio.create_task(delete_file_later(filename, sent.message_id))
        await status_msg.delete()
    except Exception:
        await status_msg.edit_text("⚠️ Download failed or file too large.")

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    if data == "profile":
        user = await get_user(user_id)
        exp = f"\n⏳ Expires: {user['expires']}" if user["expires"] else ""
        await query.message.reply_text(f"👤 Name: {user['name']}\n💼 Plan: {user['plan']}{exp}")
    elif data == "total_users" and user_id == ADMIN_ID:
        async with db_pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM users")
            await query.message.reply_text(f"👥 Total users: {total}")
    elif data.startswith("audio:"):
        file = data.split("audio:")[1]
        if not os.path.exists(file):
            await query.message.reply_text("The file has been deleted. Please resend the link.")
        else:
            await convert_to_audio(update, context, file)
    elif data.startswith("upgrade:"):
        _, username, days = data.split(":")
        async with db_pool.acquire() as conn:
            user = await conn.fetchrow("SELECT * FROM users WHERE name = $1", username)
            if not user:
                await query.message.reply_text("❌ User not found.")
                return
            expiry = user["expires"] or datetime.utcnow().date()
            new_expiry = expiry + timedelta(days=int(days))
            await conn.execute("UPDATE users SET plan = $1, expires = $2 WHERE name = $3", "premium", new_expiry, username)
            await query.message.reply_text(f"✅ {username} upgraded for {days} days (expires {new_expiry})")
    elif data == "keyword_search":
        user_states[user_id] = "awaiting_keyword"
        await query.message.reply_text("🔤 Send a keyword to search Telegram public posts...")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    if user_states.get(user_id) == "awaiting_keyword":
        user_states.pop(user_id, None)
        await update.message.reply_text("🔍 Searching across public groups...")
        results = []
        try:
            await tele_client.start()
            for group in GROUPS:
                try:
                    entity = await tele_client.get_entity(group)
                    history = await tele_client(GetHistoryRequest(
                        peer=entity,
                        limit=5,
                        offset_date=None,
                        offset_id=0,
                        max_id=0,
                        min_id=0,
                        add_offset=0,
                        hash=0
                    ))
                    for msg in history.messages:
                        if text.lower() in (msg.message or "").lower():
                            link = f"https://t.me/{group}/{msg.id}"
                            snippet = msg.message.strip().split("\n")[0][:100]
                            results.append(f"📌 [{snippet}]({link})")
                        if len(results) >= 10:
                            break
                except:
                    continue
            if results:
                await update.message.reply_text("\n\n".join(results), parse_mode="Markdown", disable_web_page_preview=True)
            else:
                await update.message.reply_text("❌ No matching public posts found.")
        except Exception as e:
            await update.message.reply_text("⚠️ Failed to search. Try again later.")
    else:
        await handle_video(update, context)

async def upgrade_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Not authorized.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /upgrade <username>")
        return
    username = context.args[0]
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("1 Day", callback_data=f"upgrade:{username}:1"),
                                      InlineKeyboardButton("5 Days", callback_data=f"upgrade:{username}:5"),
                                      InlineKeyboardButton("10 Days", callback_data=f"upgrade:{username}:10"),
                                      InlineKeyboardButton("30 Days", callback_data=f"upgrade:{username}:30")]])
    await update.message.reply_text(f"Select upgrade duration for {username}:", reply_markup=keyboard)

# ---------- WEBHOOK ----------

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

async def on_startup(app):
    global db_pool
    db_pool = await asyncpg.create_pool(DB_URL)
    await application.initialize()
    await application.start()
    await application.bot.set_webhook(f"{APP_URL}/webhook")
    logging.info("✅ Bot started and webhook set.")

async def on_cleanup(app):
    await application.stop()
    await application.shutdown()
    await db_pool.close()

web_app.on_startup.append(on_startup)
web_app.on_cleanup.append(on_cleanup)

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("upgrade", upgrade_user))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
application.add_handler(CallbackQueryHandler(handle_button))

if __name__ == "__main__":
    web.run_app(web_app, port=PORT)
