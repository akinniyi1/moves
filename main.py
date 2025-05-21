# --- [IMPORTS & SETUP] ---
import json
import csv
import os
import uuid
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# --- [CONFIGURATION] ---
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY")
NOWPAYMENTS_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET")
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL") + "/webhook"

DATA_FILE = "/mnt/data/users.json"
app = FastAPI()
invoice_timeout_minutes = 30

# --- [HELPER FUNCTIONS] ---

def load_data():
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except:
        return {"users": {}, "invoices": {}, "banned": []}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_user_data(user_id):
    data = load_data()
    return data["users"].get(str(user_id), {})

def is_user_premium(user_data):
    exp = user_data.get("expires_at")
    if exp:
        if datetime.utcnow() > datetime.fromisoformat(exp):
            user_data["plan"] = "Free"
            user_data.pop("expires_at", None)
            save_user_data(user_data["id"], user_data)
            return False
        return True
    return False

def save_user_data(user_id, user_data):
    data = load_data()
    data["users"][str(user_id)] = user_data
    save_data(data)

def get_total_downloads():
    data = load_data()
    return sum(user.get("downloads", 0) for user in data["users"].values())

# --- [TELEGRAM HANDLERS] ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = load_data()

    if user.username in data.get("banned", []):
        await update.message.reply_text("You are banned from using this bot.")
        return

    user_data = data["users"].get(str(user.id))
    if not user_data:
        data["users"][str(user.id)] = {
            "id": user.id,
            "username": user.username,
            "plan": "Free",
            "downloads": 0
        }
        save_data(data)

    keyboard = [
        [KeyboardButton("View Profile")],
        [KeyboardButton("Upgrade Your Plan")],
        [KeyboardButton("Join Our Channel")]
    ]
    await update.message.reply_text(
        "Welcome to the bot!",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.message.text
    data = load_data()
    user_data = data["users"].get(str(user.id), {})

    if msg == "View Profile":
        is_premium = is_user_premium(user_data)
        plan = user_data.get("plan", "Free")
        expires = user_data.get("expires_at", "N/A")
        if plan == "Free":
            expires = "N/A"
        await update.message.reply_text(
            f"Username: @{user.username}\nPlan: {plan}\nExpires: {expires}"
        )

    elif msg == "Upgrade Your Plan":
        buttons = [
            [InlineKeyboardButton("1 Month - $2", callback_data="upgrade_1")],
            [InlineKeyboardButton("2 Months - $4", callback_data="upgrade_2")]
        ]
        await update.message.reply_text("Choose a plan:", reply_markup=InlineKeyboardMarkup(buttons))

    elif msg == "Join Our Channel":
        await update.message.reply_text("Join our channel: https://t.me/Downloadassaas")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    data = load_data()

    if query.data.startswith("upgrade_"):
        months = 1 if query.data.endswith("1") else 2
        amount = 2 * months
        invoice_id = str(uuid.uuid4())

        data["invoices"][invoice_id] = {
            "user_id": user.id,
            "username": user.username,
            "amount": amount,
            "months": months,
            "created_at": datetime.utcnow().isoformat()
        }
        save_data(data)

        payment_url = f"https://nowpayments.io/payment/?iid={invoice_id}"  # Simulated
        await query.message.reply_text(
            f"Pay ${amount} using this link:\n{payment_url}\n\nThis link expires in 30 minutes."
        )

# --- [COMMANDS FOR ADMIN] ---

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    data = load_data()
    users = data["users"].values()
    total_users = len(set(u["username"] for u in users if u.get("username")))
    free = sum(1 for u in users if u.get("plan") == "Free")
    premium = sum(1 for u in users if u.get("plan") == "Premium")
    downloads = get_total_downloads()

    await update.message.reply_text(
        f"Total Users: {total_users}\nFree Users: {free}\nPremium Users: {premium}\nTotal Downloads: {downloads}"
    )

async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    data = load_data()
    filename = "/mnt/data/export.csv"
    with open(filename, "w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["Username", "Plan"])
        for u in data["users"].values():
            writer.writerow([u.get("username", "N/A"), u.get("plan", "Free")])

    await update.message.reply_document(document=open(filename, "rb"))

async def upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or len(context.args) != 2:
        return

    username, hours = context.args
    data = load_data()
    for user_id, u in data["users"].items():
        if u.get("username") == username:
            u["plan"] = "Premium"
            u["expires_at"] = (datetime.utcnow() + timedelta(hours=int(hours))).isoformat()
            save_data(data)
            await update.message.reply_text(f"{username} upgraded for {hours} hour(s).")
            return

    await update.message.reply_text("User not found.")

async def downgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or len(context.args) != 1:
        return

    username = context.args[0]
    data = load_data()
    for user_id, u in data["users"].items():
        if u.get("username") == username:
            u["plan"] = "Free"
            u.pop("expires_at", None)
            save_data(data)
            await update.message.reply_text(f"{username} downgraded.")
            return

    await update.message.reply_text("User not found.")

async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or len(context.args) != 1:
        return

    username = context.args[0]
    data = load_data()
    if username not in data["banned"]:
        data["banned"].append(username)
        save_data(data)
        await update.message.reply_text(f"{username} banned.")

async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or len(context.args) != 1:
        return

    username = context.args[0]
    data = load_data()
    if username in data["banned"]:
        data["banned"].remove(username)
        save_data(data)
        await update.message.reply_text(f"{username} unbanned.")

# --- [NOWPAYMENTS WEBHOOK HANDLER] ---

@app.post("/webhook", response_class=PlainTextResponse)
async def handle_webhook(request: Request):
    headers = request.headers
    ipn_secret = headers.get("x-nowpayments-sig")
    body = await request.json()

    if ipn_secret != NOWPAYMENTS_IPN_SECRET:
        return "Invalid signature"

    invoice_id = body.get("order_id")
    payment_status = body.get("payment_status")
    amount_paid = float(body.get("price_amount", 0))

    data = load_data()
    invoice = data["invoices"].get(invoice_id)
    if not invoice:
        return "Invoice not found"

    created_at = datetime.fromisoformat(invoice["created_at"])
    if datetime.utcnow() - created_at > timedelta(minutes=invoice_timeout_minutes):
        return "Invoice expired"

    if payment_status == "confirmed" and abs(amount_paid - invoice["amount"]) < 0.01:
        user_id = str(invoice["user_id"])
        user = data["users"].get(user_id)
        if user:
            user["plan"] = "Premium"
            days = 30 * invoice["months"]
            user["expires_at"] = (datetime.utcnow() + timedelta(days=days)).isoformat()
            data["users"][user_id] = user
            save_data(data)
        return "User upgraded"
    return "No action"

# --- [MAIN FUNCTION] ---

def main():
    app_bot = Application.builder().token(TOKEN).build()

    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CommandHandler("stats", stats))
    app_bot.add_handler(CommandHandler("export", export))
    app_bot.add_handler(CommandHandler("upgrade", upgrade))
    app_bot.add_handler(CommandHandler("downgrade", downgrade))
    app_bot.add_handler(CommandHandler("ban", ban))
    app_bot.add_handler(CommandHandler("unban", unban))
    app_bot.add_handler(MessageHandler(filters.TEXT, message_handler))
    app_bot.add_handler(CallbackQueryHandler(button_handler))

    app_bot.run_polling()

if __name__ == "__main__":
    main()
