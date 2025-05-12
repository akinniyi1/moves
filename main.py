import os
import json
import logging
import asyncio
import csv
from datetime import datetime, timedelta
import psycopg2
from psycopg2.extras import RealDictCursor
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.executor import start_webhook
from aiogram.dispatcher import FSMContext
from aiogram.contrib.fsm_storage.memory import MemoryStorage

# Environment Variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.getenv("PORT", 3000))

# Admin ID
ADMIN_ID = 1378825382

# Logging
logging.basicConfig(level=logging.INFO)

# Bot Setup
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# PostgreSQL Connection
def get_db():
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    return conn

# Auto-create users table
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

# Start menu keyboard
def start_keyboard():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("Contact Support", callback_data="contact_support"))
    return kb

# Reply to user button (admin)
def reply_button(user_id):
    return InlineKeyboardMarkup().add(InlineKeyboardButton("Reply", callback_data=f"reply_to_{user_id}"))

# FSM States
from aiogram.dispatcher.filters.state import State, StatesGroup
class SupportState(StatesGroup):
    waiting_for_message = State()
    waiting_for_reply = State()

# Initialize user on any interaction
def init_user(user_id, username):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
    user = cur.fetchone()
    if not user:
        cur.execute("INSERT INTO users (user_id, username, plan, downloads, expiry_date) VALUES (%s, %s, %s, %s, %s)",
                    (user_id, username, 'free', json.dumps({}), None))
    elif username:
        cur.execute("UPDATE users SET username = %s WHERE user_id = %s", (username, user_id))
    conn.commit()
    cur.close()
    conn.close()

# /start command
@dp.message_handler(commands=["start"])
async def start_cmd(message: types.Message):
    init_user(message.from_user.id, message.from_user.username)
    await message.answer("Welcome! Choose an option below:", reply_markup=start_keyboard())

# Contact Support button handler
@dp.callback_query_handler(lambda c: c.data == "contact_support")
async def contact_support(call: types.CallbackQuery):
    await call.message.answer("Please type your message below to contact support:")
    await SupportState.waiting_for_message.set()

# Receive user message to admin
@dp.message_handler(state=SupportState.waiting_for_message)
async def receive_user_message(message: types.Message, state: FSMContext):
    username = message.from_user.username or f"id:{message.from_user.id}"
    text = f"Message from @{username}:\n\n{message.text}"
    await bot.send_message(ADMIN_ID, text, reply_markup=reply_button(message.from_user.id))
    await message.answer("Your message has been sent. You’ll receive a reply soon.")
    await state.finish()

# Admin clicks Reply to user
@dp.callback_query_handler(lambda c: c.data.startswith("reply_to_"))
async def reply_to_user_prompt(call: types.CallbackQuery, state: FSMContext):
    user_id = int(call.data.split("_")[-1])
    await state.update_data(reply_to=user_id)
    await bot.send_message(ADMIN_ID, "Type your reply to send to the user:")
    await SupportState.waiting_for_reply.set()

# Admin types the reply message
@dp.message_handler(state=SupportState.waiting_for_reply)
async def handle_admin_reply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    user_id = data.get("reply_to")
    if user_id:
        try:
            await bot.send_message(user_id, f"Support Reply:\n\n{message.text}")
            await message.answer("Reply sent successfully.")
        except:
            await message.answer("Failed to send reply. User may have blocked the bot.")
    await state.finish()

# /userinfo <username>
@dp.message_handler(commands=["userinfo"])
async def user_info(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        _, username = message.text.split()
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM users WHERE username = %s", (username,))
        user = cur.fetchone()
        cur.close()
        conn.close()
        if not user:
            await message.reply("User not found.")
            return
        await message.reply(f"Username: @{user['username']}\nPlan: {user['plan']}\nExpiry: {user['expiry_date']}")
    except:
        await message.reply("Usage: /userinfo <username>")

# /stats command
@dp.message_handler(commands=["stats"])
async def stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT COUNT(*) FROM users")
    total = cur.fetchone()['count']
    cur.execute("SELECT COUNT(*) FROM users WHERE plan = 'free'")
    free = cur.fetchone()['count']
    cur.execute("SELECT COUNT(*) FROM users WHERE plan = 'paid'")
    paid = cur.fetchone()['count']
    cur.execute("SELECT downloads FROM users")
    downloads = 0
    month = datetime.utcnow().strftime("%Y-%m")
    for row in cur.fetchall():
        user_dl = json.loads(row['downloads'] or '{}')
        downloads += user_dl.get(month, 0)
    cur.close()
    conn.close()
    await message.reply(f"Total Users: {total}\nFree: {free}\nPaid: {paid}\nMonthly Downloads: {downloads}")

# /export_csv command
@dp.message_handler(commands=["export_csv"])
async def export_csv(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT username, plan, expiry_date FROM users")
    rows = cur.fetchall()
    filename = "/tmp/user_data.csv"
    with open(filename, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Username", "Plan", "Expiry Date"])
        for row in rows:
            writer.writerow([row['username'], row['plan'], row['expiry_date']])
    cur.close()
    conn.close()
    await bot.send_document(chat_id=ADMIN_ID, document=open(filename, "rb"))

# /broadcast <message>
@dp.message_handler(commands=["broadcast"])
async def broadcast(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    msg = message.text.replace("/broadcast", "").strip()
    if not msg:
        await message.reply("Usage: /broadcast <your message>")
        return
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT user_id FROM users")
    users = cur.fetchall()
    sent = 0
    for user in users:
        try:
            await bot.send_message(user['user_id'], msg)
            sent += 1
        except:
            continue
    await message.reply(f"Broadcast sent to {sent} users.")
    cur.close()
    conn.close()

# Webhook Startup
async def on_startup(dp):
    create_users_table()
    await bot.set_webhook(WEBHOOK_URL)

async def on_shutdown(dp):
    await bot.delete_webhook()

# Main
if __name__ == '__main__':
    from aiogram import executor
    start_webhook(
        dispatcher=dp,
        webhook_path=WEBHOOK_PATH,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        skip_updates=True,
        host=WEBAPP_HOST,
        port=WEBAPP_PORT,
    )
