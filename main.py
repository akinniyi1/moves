# --- [ IMPORT & SETUP ] ---
import os
import json
import logging
import uuid
import yt_dlp
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.executor import start_webhook
from aiogram.dispatcher.filters import Command
from aiogram.contrib.middlewares.logging import LoggingMiddleware

API_TOKEN = 'YOUR_BOT_TOKEN'
ADMIN_ID = 1378825382
WEBHOOK_HOST = 'https://your-render-webhook-url.com'
WEBHOOK_PATH = f'/webhook/{API_TOKEN}'
WEBHOOK_URL = f'{WEBHOOK_HOST}{WEBHOOK_PATH}'

bot = Bot(token=API_TOKEN, parse_mode='HTML')
dp = Dispatcher(bot)
dp.middleware.setup(LoggingMiddleware())

DATA_FILE = '/mnt/data/users.json'
DOWNLOADS_FOLDER = '/mnt/data/downloads'
os.makedirs(DOWNLOADS_FOLDER, exist_ok=True)

# --- [ USER DATA HELPERS ] ---
def load_users():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_users(data):
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def normalize_username(username):
    return username.lower() if username else None

def get_user(username):
    users = load_users()
    return users.get(normalize_username(username), {})

def update_user(username, updates):
    username = normalize_username(username)
    users = load_users()
    if username not in users:
        users[username] = {"downloads": 0, "plan": "free", "expiry": None}
    users[username].update(updates)
    save_users(users)

def is_paid(username):
    user = get_user(username)
    if user.get("plan") == "paid" and user.get("expiry"):
        expiry = datetime.strptime(user["expiry"], "%Y-%m-%d %H:%M:%S")
        if expiry > datetime.utcnow():
            return True
        else:
            update_user(username, {"plan": "free", "expiry": None})
    return False

def can_download(username):
    user = get_user(username)
    if is_paid(username):
        return True
    return user.get("downloads", 0) < 3

def increment_download(username):
    username = normalize_username(username)
    users = load_users()
    users[username]["downloads"] = users[username].get("downloads", 0) + 1
    save_users(users)

# --- [ START & PROFILE ] ---
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    username = normalize_username(message.from_user.username)
    update_user(username, {})
    kb = InlineKeyboardMarkup().add(
        InlineKeyboardButton("View Profile", callback_data="view_profile"),
        InlineKeyboardButton("Support", callback_data="support"),
    )
    await message.answer("Welcome! Send an Instagram link to download (max 50MB).\nFree users get 3 downloads/day.", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data == "view_profile")
async def view_profile(callback: types.CallbackQuery):
    username = normalize_username(callback.from_user.username)
    user = get_user(username)
    status = user.get("plan", "free")
    expiry = user.get("expiry")
    if status == "paid" and expiry:
        msg = f"Username: {username}\nPlan: Paid\nExpires: {expiry}"
    else:
        msg = f"Username: {username}\nPlan: Free"
    await callback.message.edit_text(msg, reply_markup=callback.message.reply_markup)

# --- [ SUPPORT SYSTEM ] ---
pending_support = {}

@dp.callback_query_handler(lambda c: c.data == "support")
async def support_start(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    pending_support[user_id] = True
    await callback.message.answer("Please type your message for support:")

@dp.message_handler(lambda msg: msg.from_user.id in pending_support)
async def support_message(msg: types.Message):
    user_id = msg.from_user.id
    username = normalize_username(msg.from_user.username)
    del pending_support[user_id]
    text = f"Message from @{username}:\n\n{msg.text}"
    kb = InlineKeyboardMarkup().add(
        InlineKeyboardButton(f"Reply to @{username}", callback_data=f"reply_{username}")
    )
    await bot.send_message(ADMIN_ID, text, reply_markup=kb)
    await msg.reply("Your message has been sent. You'll get a response soon.")

@dp.callback_query_handler(lambda c: c.data.startswith("reply_"))
async def admin_reply_prompt(callback: types.CallbackQuery):
    target_user = callback.data.split("_")[1]
    pending_support[callback.from_user.id] = target_user
    await callback.message.answer(f"Type your reply to @{target_user}:")

@dp.message_handler(lambda msg: msg.from_user.id == ADMIN_ID and msg.from_user.id in pending_support)
async def admin_reply_send(msg: types.Message):
    target_user = pending_support.pop(msg.from_user.id)
    await bot.send_message(f"@{target_user}", f"Reply from admin:\n\n{msg.text}")
    await msg.reply("Reply sent.")

# --- [ UPGRADE SYSTEM ] ---
@dp.message_handler(commands=['upgrade'])
async def upgrade_user(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    parts = msg.text.split()
    if len(parts) != 2:
        await msg.reply("Usage: /upgrade <username>")
        return
    target = normalize_username(parts[1])
    kb = InlineKeyboardMarkup()
    for h in [1, 6, 24, 72, 168]:
        kb.add(InlineKeyboardButton(f"{h} hour(s)", callback_data=f"upgrade_{target}_{h}"))
    await msg.reply(f"Select upgrade duration for @{target}:", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data.startswith("upgrade_"))
async def confirm_upgrade(callback: types.CallbackQuery):
    _, user, hours = callback.data.split("_")
    hours = int(hours)
    expiry = (datetime.utcnow() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    update_user(user, {"plan": "paid", "expiry": expiry})
    await callback.message.edit_text(f"@{user} upgraded for {hours} hour(s). Expires at {expiry}")
    await bot.send_message(f"@{user}", f"Your plan has been upgraded for {hours} hour(s).")

# --- [ CSV EXPORT & STATS ] ---
@dp.message_handler(commands=['stats'])
async def stats(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    users = load_users()
    total = len(users)
    paid = sum(1 for u in users.values() if u.get("plan") == "paid" and u.get("expiry") and datetime.strptime(u["expiry"], "%Y-%m-%d %H:%M:%S") > datetime.utcnow())
    downloads = sum(u.get("downloads", 0) for u in users.values())
    await msg.reply(f"Total users: {total}\nPaid users: {paid}\nTotal downloads: {downloads}")

@dp.message_handler(commands=['broadcast'])
async def broadcast(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.reply("Send the broadcast message text:")

@dp.message_handler(lambda m: m.reply_to_message and m.reply_to_message.text.startswith("Send the broadcast"))
async def send_broadcast(msg: types.Message):
    users = load_users()
    for user in users:
        try:
            await bot.send_message(f"@{user}", msg.text)
        except:
            pass
    await msg.reply("Broadcast sent.")

# --- [ VIDEO DOWNLOAD / AUDIO CONVERT ] ---
@dp.message_handler(lambda msg: 'instagram.com' in msg.text.lower())
async def download_video(msg: types.Message):
    username = normalize_username(msg.from_user.username)
    if not can_download(username):
        await msg.reply("Free download limit reached. Please upgrade to continue.")
        return
    url = msg.text.strip()
    uid = str(uuid.uuid4())
    filepath = os.path.join(DOWNLOADS_FOLDER, f"{uid}.mp4")
    ydl_opts = {'outtmpl': filepath, 'format': 'mp4', 'noplaylist': True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        with open(filepath, 'rb') as f:
            btn = InlineKeyboardMarkup().add(
                InlineKeyboardButton("Convert to Audio", callback_data=f"audio_{uid}")
            )
            await msg.reply_video(f, reply_markup=btn)
        increment_download(username)
        asyncio.create_task(auto_delete(filepath, 60))
    except Exception as e:
        await msg.reply("Download failed. Ensure the link is valid.")

@dp.callback_query_handler(lambda c: c.data.startswith("audio_"))
async def convert_audio(callback: types.CallbackQuery):
    uid = callback.data.split("_")[1]
    video_path = os.path.join(DOWNLOADS_FOLDER, f"{uid}.mp4")
    audio_path = os.path.join(DOWNLOADS_FOLDER, f"{uid}.mp3")
    if not os.path.exists(video_path):
        await callback.message.reply("The file has been deleted. Please resend the link.")
        return
    os.system(f'ffmpeg -i "{video_path}" -vn -ab 128k -ar 44100 -y "{audio_path}"')
    with open(audio_path, 'rb') as a:
        await callback.message.reply_audio(a)
    os.remove(audio_path)

async def auto_delete(path, delay):
    await asyncio.sleep(delay)
    if os.path.exists(path):
        os.remove(path)

# --- [ IMAGE TO PDF ] ---
photo_cache = {}

@dp.message_handler(content_types=types.ContentType.PHOTO)
async def receive_photo(msg: types.Message):
    username = normalize_username(msg.from_user.username)
    if not is_paid(username) and username in photo_cache:
        await msg.reply("Free trial used. Upgrade to use PDF feature again.")
        return
    file_id = msg.photo[-1].file_id
    if username not in photo_cache:
        photo_cache[username] = []
    photo_cache[username].append(file_id)
    btn = InlineKeyboardMarkup().add(InlineKeyboardButton("Convert to PDF", callback_data=f"pdf_{username}"))
    await msg.reply("Photo received.", reply_markup=btn)

@dp.callback_query_handler(lambda c: c.data.startswith("pdf_"))
async def convert_to_pdf(callback: types.CallbackQuery):
    username = callback.data.split("_")[1]
    photos = photo_cache.get(username, [])
    if not photos:
        await callback.message.reply("No photos to convert.")
        return
    media = []
    for fid in photos:
        file = await bot.get_file(fid)
        path = file.file_path
        downloaded = await bot.download_file(path)
        local_path = os.path.join(DOWNLOADS_FOLDER, f"{fid}.jpg")
        with open(local_path, 'wb') as f:
            f.write(downloaded.read())
        media.append(local_path)
    from fpdf import FPDF
    pdf = FPDF()
    for img in media:
        pdf.add_page()
        pdf.image(img, x=10, y=10, w=190)
    pdf_path = os.path.join(DOWNLOADS_FOLDER, f"{username}.pdf")
    pdf.output(pdf_path)
    with open(pdf_path, 'rb') as p:
        await callback.message.reply_document(p)
    for img in media:
        os.remove(img)
    os.remove(pdf_path)
    del photo_cache[username]

# --- [ WEBHOOK SETUP ] ---
async def on_startup(dp):
    await bot.set_webhook(WEBHOOK_URL)

async def on_shutdown(dp):
    await bot.delete_webhook()

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    start_webhook(
        dispatcher=dp,
        webhook_path=WEBHOOK_PATH,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        skip_updates=True,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
    )
