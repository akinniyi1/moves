import os
import json
import logging
import asyncio
import csv
import uuid
from datetime import datetime, timedelta
from PIL import Image
from io import BytesIO

import yt_dlp
import psycopg2
from psycopg2.extras import RealDictCursor

from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.utils.executor import start_webhook
from aiogram.dispatcher.filters.state import State, StatesGroup

# Environment Variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.getenv("PORT", 3000))
ADMIN_ID = 1378825382

# Logging
logging.basicConfig(level=logging.INFO)

# Bot & Dispatcher
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# Database
def get_db():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

# Ensure users table exists
def create_users_table():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            plan TEXT DEFAULT 'free',
            downloads JSONB,
            expiry_date TIMESTAMP
        );
    """)
    conn.commit()
    cur.close()
    conn.close()

# FSM states
class SupportState(StatesGroup):
    waiting_for_message = State()
    waiting_for_reply = State()

# Helper: Init user
def init_user(user_id, username):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
    user = cur.fetchone()
    if not user:
        cur.execute(
            "INSERT INTO users (user_id, username, plan, downloads, expiry_date) VALUES (%s, %s, %s, %s, %s)",
            (user_id, username, 'free', json.dumps({}), None)
        )
    elif username:
        cur.execute("UPDATE users SET username = %s WHERE user_id = %s", (username, user_id))
    conn.commit()
    cur.close()
    conn.close()

# Helper: Check plan
def is_paid(user_id):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT plan, expiry_date FROM users WHERE user_id = %s", (user_id,))
    user = cur.fetchone()
    now = datetime.utcnow()
    if user and user['plan'] == 'paid' and user['expiry_date'] and user['expiry_date'] > now:
        cur.close()
        conn.close()
        return True
    elif user and user['plan'] == 'paid':
        cur.execute("UPDATE users SET plan = 'free', expiry_date = NULL WHERE user_id = %s", (user_id,))
        conn.commit()
    cur.close()
    conn.close()
    return False

# Helper: Update download count
def increment_download(user_id):
    month = datetime.utcnow().strftime("%Y-%m")
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT downloads FROM users WHERE user_id = %s", (user_id,))
    user = cur.fetchone()
    data = json.loads(user['downloads'] or '{}')
    data[month] = data.get(month, 0) + 1
    cur.execute("UPDATE users SET downloads = %s WHERE user_id = %s", (json.dumps(data), user_id))
    conn.commit()
    cur.close()
    conn.close()

# Start keyboard
def menu_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("Contact Support", callback_data="contact_support"),
        InlineKeyboardButton("Image to PDF", callback_data="img_to_pdf")
    )
    return kb

# Start
@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    init_user(message.from_user.id, message.from_user.username)
    await message.answer("Welcome! Use the buttons below.", reply_markup=menu_keyboard())

# Contact Support
@dp.callback_query_handler(lambda c: c.data == "contact_support")
async def contact_support(call: types.CallbackQuery):
    await call.message.answer("Please type your message below to contact support:")
    await SupportState.waiting_for_message.set()

@dp.message_handler(state=SupportState.waiting_for_message)
async def handle_support_msg(message: types.Message, state: FSMContext):
    username = message.from_user.username or f"id:{message.from_user.id}"
    text = f"Support message from @{username}:\n\n{message.text}"
    btn = InlineKeyboardMarkup().add(InlineKeyboardButton("Reply", callback_data=f"reply_{message.from_user.id}"))
    await bot.send_message(ADMIN_ID, text, reply_markup=btn)
    await message.reply("Your message has been sent to support.")
    await state.finish()

@dp.callback_query_handler(lambda c: c.data.startswith("reply_"))
async def prompt_reply(call: types.CallbackQuery, state: FSMContext):
    user_id = int(call.data.split("_")[1])
    await state.update_data(reply_to=user_id)
    await call.message.answer("Type your reply:")
    await SupportState.waiting_for_reply.set()

@dp.message_handler(state=SupportState.waiting_for_reply)
async def send_reply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    user_id = data['reply_to']
    try:
        await bot.send_message(user_id, f"Support reply:\n\n{message.text}")
        await message.reply("Reply sent.")
    except:
        await message.reply("Failed to send reply.")
    await state.finish()

# Download video (and convert to audio)
@dp.message_handler(lambda m: m.text and "http" in m.text)
async def download_video(message: types.Message):
    init_user(message.from_user.id, message.from_user.username)
    if not is_paid(message.from_user.id):
        month = datetime.utcnow().strftime("%Y-%m")
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT downloads FROM users WHERE user_id = %s", (message.from_user.id,))
        data = json.loads(cur.fetchone()['downloads'] or '{}')
        if data.get(month, 0) >= 3:
            await message.reply("You’ve reached your free 3 downloads this month. Upgrade to continue.")
            return
        cur.close()
        conn.close()
    url = message.text.strip()
    uid = str(uuid.uuid4())
    filename = f"{uid}.mp4"
    try:
        ydl_opts = {
            "outtmpl": filename,
            "format": "mp4",
            "noplaylist": True,
            "max_filesize": 50 * 1024 * 1024
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        await bot.send_video(message.chat.id, open(filename, "rb"),
                             reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("Convert to Audio", callback_data=f"audio_{filename}")))
        increment_download(message.from_user.id)
        asyncio.create_task(delete_file_delayed(filename))
    except Exception as e:
        await message.reply(f"Failed to download: {e}")

async def delete_file_delayed(filename):
    await asyncio.sleep(60)
    if os.path.exists(filename):
        os.remove(filename)

@dp.callback_query_handler(lambda c: c.data.startswith("audio_"))
async def convert_to_audio(call: types.CallbackQuery):
    filename = call.data.split("_", 1)[1]
    if not os.path.exists(filename):
        await call.message.reply("The file has been deleted. Please resend the link.")
        return
    mp3_file = filename.replace(".mp4", ".mp3")
    os.system(f"ffmpeg -i {filename} -vn -ab 128k -ar 44100 -y {mp3_file}")
    await bot.send_audio(call.message.chat.id, open(mp3_file, "rb"))
    os.remove(mp3_file)

# Image to PDF
user_images = {}
@dp.callback_query_handler(lambda c: c.data == "img_to_pdf")
async def ask_images(call: types.CallbackQuery):
    await call.message.answer("Send me all the images (one by one). Then type /createpdf when done.")

@dp.message_handler(content_types=types.ContentType.PHOTO)
async def collect_images(message: types.Message):
    uid = message.from_user.id
    user_images.setdefault(uid, [])
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    image_data = await bot.download_file(file.file_path)
    user_images[uid].append(Image.open(BytesIO(image_data.read())))
    await message.reply("Image saved. Send more or type /createpdf.")

@dp.message_handler(commands=["createpdf"])
async def create_pdf(message: types.Message):
    uid = message.from_user.id
    init_user(uid, message.from_user.username)
    if not is_paid(uid):
        month = datetime.utcnow().strftime("%Y-%m")
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT downloads FROM users WHERE user_id = %s", (uid,))
        data = json.loads(cur.fetchone()['downloads'] or '{}')
        if data.get(month, 0) >= 1:
            await message.reply("Image to PDF is limited for free users. Upgrade to use again.")
            return
    if uid not in user_images or not user_images[uid]:
        await message.reply("No images received.")
        return
    pdf_file = f"{uuid.uuid4()}.pdf"
    user_images[uid][0].save(pdf_file, save_all=True, append_images=user_images[uid][1:])
    await message.reply_document(InputFile(pdf_file))
    increment_download(uid)
    user_images[uid] = []
    os.remove(pdf_file)

# Upgrade (admin)
@dp.message_handler(commands=["upgrade"])
async def upgrade_user(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        _, username = message.text.split()
        kb = InlineKeyboardMarkup(row_width=3)
        for d in [1, 7, 30]:
            kb.add(InlineKeyboardButton(f"{d} day(s)", callback_data=f"upgrade_{username}_{d}"))
        await message.reply("Select duration:", reply_markup=kb)
    except:
        await message.reply("Usage: /upgrade <username>")

@dp.callback_query_handler(lambda c: c.data.startswith("upgrade_"))
async def process_upgrade(call: types.CallbackQuery):
    _, username, days = call.data.split("_")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET plan = 'paid', expiry_date = %s WHERE username = %s",
                (datetime.utcnow() + timedelta(days=int(days)), username))
    conn.commit()
    cur.close()
    conn.close()
    await call.message.reply(f"@{username} upgraded for {days} day(s).")

# Admin: /userinfo, /stats, /broadcast, /export_csv
@dp.message_handler(commands=["userinfo"])
async def userinfo(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        _, username = message.text.split()
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM users WHERE username = %s", (username,))
        user = cur.fetchone()
        await message.reply(f"User: @{user['username']}\nPlan: {user['plan']}\nExpiry: {user['expiry_date']}")
        cur.close()
        conn.close()
    except:
        await message.reply("Usage: /userinfo <username>")

@dp.message_handler(commands=["stats"])
async def stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT COUNT(*) FROM users")
    total = cur.fetchone()['count']
    cur.execute("SELECT COUNT(*) FROM users WHERE plan = 'paid'")
    paid = cur.fetchone()['count']
    cur.execute("SELECT downloads FROM users")
    downloads = 0
    for row in cur.fetchall():
        data = json.loads(row['downloads'] or '{}')
        downloads += data.get(datetime.utcnow().strftime("%Y-%m"), 0)
    await message.reply(f"Total Users: {total}\nPaid Users: {paid}\nDownloads this month: {downloads}")
    cur.close()
    conn.close()

@dp.message_handler(commands=["broadcast"])
async def broadcast(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text.replace("/broadcast", "").strip()
    if not text:
        await message.reply("Usage: /broadcast <message>")
        return
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT user_id FROM users")
    users = cur.fetchall()
    count = 0
    for u in users:
        try:
            await bot.send_message(u['user_id'], text)
            count += 1
        except:
            pass
    await message.reply(f"Broadcast sent to {count} users.")

@dp.message_handler(commands=["export_csv"])
async def export_csv(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT username, plan, expiry_date FROM users")
    rows = cur.fetchall()
    path = "/tmp/users.csv"
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Username", "Plan", "Expiry"])
        for r in rows:
            writer.writerow([r["username"], r["plan"], r["expiry_date"]])
    await bot.send_document(ADMIN_ID, open(path, "rb"))
    cur.close()
    conn.close()

# Webhook Setup
async def on_startup(dp):
    create_users_table()
    await bot.set_webhook(WEBHOOK_URL)

async def on_shutdown(dp):
    await bot.delete_webhook()

if __name__ == "__main__":
    start_webhook(
        dispatcher=dp,
        webhook_path=WEBHOOK_PATH,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        skip_updates=True,
        host=WEBAPP_HOST,
        port=WEBAPP_PORT,
    )
