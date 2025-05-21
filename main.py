# --- [IMPORTS & SETUP] ---
import os
import json
import csv
from datetime import datetime, timedelta
from uuid import uuid4
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
import uvicorn
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import aiohttp

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 5274852353  # Replace with your Telegram ID
NOWPAYMENTS_API_KEY = "7HM8ANB-3Q6MHRX-GVYJVFW-ZBGQNKW"
IPN_SECRET_KEY = "i1xhpaFVbMys4SH+t8jCB4M8AsWRMK7f"
CHANNEL_LINK = "https://t.me/Downloadassaas"
WEBHOOK_SECRET = "webhook-secret"
BASE_URL = "https://moves-qzsv.onrender.com"

USERS_FILE = "/mnt/data/users.json"

# --- [UTILITY FUNCTIONS] ---
def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_users(users):
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)

def get_user_data(username):
    users = load_users()
    return users.get(username)

def update_user(username, data):
    users = load_users()
    user = users.get(username, {})
    user.update(data)
    users[username] = user
    save_users(users)

def is_premium(user):
    expiry = user.get("premium_expiry")
    if expiry:
        try:
            expiry_dt = datetime.fromisoformat(expiry)
            if datetime.utcnow() < expiry_dt:
                return True
        except Exception:
            return False
    return False

def check_and_downgrade():
    users = load_users()
    changed = False
    for username, user in users.items():
        expiry = user.get("premium_expiry")
        if expiry:
            try:
                expiry_dt = datetime.fromisoformat(expiry)
                if datetime.utcnow() >= expiry_dt:
                    user["premium_expiry"] = None
                    changed = True
            except:
                continue
    if changed:
        save_users(users)

# --- [BOT HANDLERS] ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.effective_user.username
    if not username:
        await update.message.reply_text("You need a username to use this bot.")
        return

    check_and_downgrade()
    users = load_users()
    if username not in users:
        users[username] = {
            "downloads": 0,
            "premium_expiry": None,
            "banned": False
        }
        save_users(users)

    if users[username].get("banned"):
        await update.message.reply_text("You are banned from using this bot.")
        return

    btns = [
        [KeyboardButton("Profile")],
        [KeyboardButton("Upgrade Your Plan")],
        [KeyboardButton("Join Our Channel")]
    ]
    markup = ReplyKeyboardMarkup(btns, resize_keyboard=True)
    await update.message.reply_text("Welcome to the bot!", reply_markup=markup)

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.effective_user.username
    if not username:
        return

    check_and_downgrade()
    user = get_user_data(username)
    if not user or user.get("banned"):
        return

    msg = update.message.text
    if msg == "Profile":
        expiry = user.get("premium_expiry")
        plan = "Premium" if is_premium(user) else "Free"
        expiry_text = f"\nExpires at: {expiry}" if expiry else ""
        await update.message.reply_text(f"Your plan: {plan}{expiry_text}")
    elif msg == "Join Our Channel":
        await update.message.reply_text("Join our channel:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Join Channel", url=CHANNEL_LINK)]
        ]))
    elif msg == "Upgrade Your Plan":
        await update.message.reply_text("Choose a plan:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("1 month - $2", callback_data="pay_1m")],
            [InlineKeyboardButton("2 months - $4", callback_data="pay_2m")]
        ]))

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    username = query.from_user.username
    await query.answer()

    amount_map = {
        "pay_1m": (2, 30),
        "pay_2m": (4, 60)
    }

    if query.data in amount_map:
        amount, days = amount_map[query.data]
        invoice_id = str(uuid4())
        invoice_data = {
            "price_amount": amount,
            "price_currency": "USD",
            "order_id": invoice_id,
            "ipn_callback_url": f"{BASE_URL}/ipn",
            "success_url": "https://t.me/DownloadassaasSupport_bot",
            "cancel_url": "https://t.me/DownloadassaasSupport_bot"
        }

        async with aiohttp.ClientSession() as session:
            headers = {"x-api-key": NOWPAYMENTS_API_KEY}
            async with session.post("https://api.nowpayments.io/v1/invoice", json=invoice_data, headers=headers) as resp:
                res = await resp.json()
                if "invoice_url" in res:
                    users = load_users()
                    users[username]["invoice"] = {
                        "id": invoice_id,
                        "amount": amount,
                        "days": days,
                        "created_at": datetime.utcnow().isoformat()
                    }
                    save_users(users)
                    await query.message.reply_text(f"Pay here: {res['invoice_url']}")
                else:
                    await query.message.reply_text("Failed to create invoice. Try again later.")

# --- [COMMANDS] ---
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    check_and_downgrade()
    users = load_users()
    total = len(users)
    paid = sum(1 for u in users.values() if is_premium(u))
    free = total - paid
    downloads = sum(u.get("downloads", 0) for u in users.values())
    await update.message.reply_text(f"Total: {total}\nFree: {free}\nPaid: {paid}\nDownloads: {downloads}")

async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    users = load_users()
    path = "/mnt/data/export.csv"
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Username", "Plan"])
        for username, user in users.items():
            plan = "Paid" if is_premium(user) else "Free"
            writer.writerow([username, plan])
    await update.message.reply_document(open(path, "rb"))

async def upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        username, duration = context.args[0], int(context.args[1])
        expiry = datetime.utcnow() + timedelta(hours=duration)
        update_user(username, {"premium_expiry": expiry.isoformat()})
        await update.message.reply_text(f"Upgraded {username} for {duration} hours.")
    except:
        await update.message.reply_text("Usage: /upgrade username duration_in_hours")

async def downgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        username = context.args[0]
        update_user(username, {"premium_expiry": None})
        await update.message.reply_text(f"Downgraded {username}.")
    except:
        await update.message.reply_text("Usage: /downgrade username")

async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        username = context.args[0]
        update_user(username, {"banned": True})
        await update.message.reply_text(f"Banned {username}.")
    except:
        await update.message.reply_text("Usage: /ban username")

async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        username = context.args[0]
        update_user(username, {"banned": False})
        await update.message.reply_text(f"Unbanned {username}.")
    except:
        await update.message.reply_text("Usage: /unban username")

# --- [FASTAPI ENDPOINT] ---
app = FastAPI()

@app.post("/ipn")
async def handle_ipn(request: Request):
    data = await request.json()
    order_id = data.get("order_id")
    payment_status = data.get("payment_status")
    if payment_status != "finished":
        return PlainTextResponse("ignored")

    users = load_users()
    for username, user in users.items():
        invoice = user.get("invoice")
        if invoice and invoice["id"] == order_id:
            created = datetime.fromisoformat(invoice["created_at"])
            if datetime.utcnow() - created > timedelta(minutes=30):
                break
            expiry = datetime.utcnow() + timedelta(days=invoice["days"])
            user["premium_expiry"] = expiry.isoformat()
            user.pop("invoice", None)
            save_users(users)
            break

    return PlainTextResponse("ok")

# --- [BOT INIT] ---
app_bot = ApplicationBuilder().token(BOT_TOKEN).build()
app_bot.add_handler(CommandHandler("start", start))
app_bot.add_handler(CommandHandler("stats", stats))
app_bot.add_handler(CommandHandler("export", export))
app_bot.add_handler(CommandHandler("upgrade", upgrade))
app_bot.add_handler(CommandHandler("downgrade", downgrade))
app_bot.add_handler(CommandHandler("ban", ban))
app_bot.add_handler(CommandHandler("unban", unban))
app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
app_bot.add_handler(CallbackQueryHandler(callback_handler))

@app.on_event("startup")
async def startup():
    await app_bot.initialize()
    await app_bot.start()
    await app_bot.updater.start_polling()

@app.on_event("shutdown")
async def shutdown():
    await app_bot.updater.stop()
    await app_bot.stop()
    await app_bot.shutdown()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=10000)
