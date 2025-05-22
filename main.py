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

# SSL workaround for yt_dlp
ssl._create_default_https_context = ssl._create_unverified_context
logging.basicConfig(level=logging.INFO)

# --- [CONFIG] ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", 10000))
ADMIN_ID = 1378825382
DATA_FILE = "/mnt/data/users.json"
NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY")
NOWPAYMENTS_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET")
CHANNEL_LINK = "https://t.me/Downloadassaas"

# Initialize
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
        return json.load(open(DATA_FILE))
    return {}

def save_users(data):
    json.dump(data, open(DATA_FILE, "w"), indent=2)

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
    if not isinstance(exp, str): return False
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
                        users[u] = {"plan":"free","downloads":0}
                        changed = True
                except:
                    pass
    if changed:
        save_users(users)

async def delete_file_later(path, mid=None):
    await asyncio.sleep(60)
    if os.path.exists(path): os.remove(path)
    if mid: file_registry.pop(mid, None)

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

# --- [START] ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    downgrade_expired_users()
    user = update.effective_user
    un = user.username
    if not un:
        return await update.message.reply_text("❌ Set a Telegram username first.")
    if un not in users:
        users[un] = {"plan":"free","downloads":0}
        save_users(users)
    if is_banned(un):
        return await update.message.reply_text("⛔ You are banned.")
    kb = [
        [InlineKeyboardButton("👤 Profile", callback_data="profile"),
         InlineKeyboardButton("🖼️ Convert to PDF", callback_data="convertpdf")],
        [InlineKeyboardButton("⬆️ Upgrade Plan", callback_data="upgrade_menu")],
        [InlineKeyboardButton("📢 Join Channel", url=CHANNEL_LINK)]
    ]
    await update.message.reply_text(
        f"Hello @{un}! Send a supported video URL or choose an option.",
        reply_markup=InlineKeyboardMarkup(kb)
    )

# --- [VIDEO] ---
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    downgrade_expired_users()
    url = update.message.text.strip()
    if not is_valid_url(url):
        return await update.message.reply_text("❌ Invalid URL.")
    if "youtube.com" in url or "youtu.be" in url:
        return await update.message.reply_text("❌ YouTube not supported.")
    un = update.effective_user.username
    if is_banned(un):
        return await update.message.reply_text("⛔ Banned.")
    ud = users.get(un,{"plan":"free","downloads":0})
    if not is_premium(ud) and ud["downloads"]>=3:
        return await update.message.reply_text("⛔ Free: 3 downloads max.")
    fn = generate_filename()
    st = await update.message.reply_text("Downloading…")
    opts = {'outtmpl':fn,'format':'bestvideo+bestaudio/best','merge_output_format':'mp4','quiet':True,'noplaylist':True,'max_filesize':50*1024*1024}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl: ydl.download([url])
        with open(fn,"rb") as f:
            sent = await update.message.reply_video(f, caption="Here you go!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎧 To Audio", callback_data=f"audio:{fn}")]]))
        file_registry[sent.message_id]=fn
        asyncio.create_task(delete_file_later(fn, sent.message_id))
        await st.delete()
        if not is_premium(ud):
            ud["downloads"]+=1; users[un]=ud; save_users(users)
    except:
        await st.edit_text("⚠️ Download failed.")

# --- [INLINE BUTTONS] ---
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query; await query.answer()
    d=query.data; un=query.from_user.username; ud=users.get(un,{"plan":"free"})
    if is_banned(un):
        return await query.message.reply_text("⛔ You are banned.")
    if d=="profile":
        downgrade_expired_users()
        if is_premium(ud):
            e=datetime.fromisoformat(ud["expires"])
            msg=f"@{un}\nPremium until {e:%Y-%m-%d %H:%M} UTC"
        else:
            msg=f"@{un}\nFree plan"
        return await query.message.reply_text(msg)
    if d=="convertpdf":
        return await convert_pdf(update, context, True)
    if d.startswith("audio:"):
        return await convert_to_audio(update, context, d.split(":",1)[1])
    if d=="upgrade_menu":
        kb=InlineKeyboardMarkup([
            [InlineKeyboardButton("1m – $2", callback_data="pay_1")],
            [InlineKeyboardButton("2m – $4", callback_data="pay_2")]
        ])
        return await query.message.reply_text("Choose plan:", reply_markup=kb)
    if d in ("pay_1","pay_2"):
        months=1 if d=="pay_1" else 2
        amount=2*months
        invoice_req={
            "price_amount":amount,
            "price_currency":"usd",
            "order_id":f"@{un}|{30*months}",
            "ipn_callback_url":f"{APP_URL}/ipn"
        }
        headers={"x-api-key":NOWPAYMENTS_API_KEY}
        async with aiohttp.ClientSession() as sess:
            r=await sess.post("https://api.nowpayments.io/v1/invoice",json=invoice_req,headers=headers)
            res=await r.json()
        if "invoice_url" in res:
            return await query.message.reply_text(f"Pay here: {res['invoice_url']}")
        logging.error("NP error: %s",res)
        return await query.message.reply_text("❌ Payment init failed.")

# --- [PDF] ---
async def convert_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, triggered_by_button=False):
    uid=update.effective_user.id; un=update.effective_user.username
    ud=users.get(un,{"plan":"free"})
    if not is_premium(ud):
        if pdf_trials.get(uid,0)>=1:
            return await update.message.reply_text("⛔ Free: 1 PDF")
        pdf_trials[uid]=1
    imgs=image_collections.get(uid,[])
    if not imgs:
        return await update.message.reply_text("❌ No images.")
    try:
        pls=[Image.open(i).convert("RGB") for i in imgs]
        out=generate_filename("pdf"); pls[0].save(out,save_all=True,append_images=pls[1:])
        with open(out,"rb") as f:
            await update.message.reply_document(f,filename="converted.pdf")
        asyncio.create_task(delete_file_later(out))
        for i in imgs: os.remove(i)
        image_collections[uid]=[]
    except:
        await update.message.reply_text("❌ PDF failed.")

# --- [IMAGES] ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    photo=update.message.photo[-1]
    f=await context.bot.get_file(photo.file_id)
    path=f"/mnt/data/img_{datetime.utcnow():%H%M%S%f}.jpg"
    await f.download_to_drive(path)
    image_collections.setdefault(uid,[]).append(path)
    await update.message.reply_text("✅ Image saved.")

# --- [ADMIN CMDS] ---
async def upgrade_cmd(update: Update, context):
    if update.effective_user.id!=ADMIN_ID: return
    if len(context.args)!=2: return await update.message.reply_text("Usage: /upgrade <user> <h>")
    u,h=context.args; h=int(h)
    exp=(datetime.utcnow()+timedelta(hours=h)).isoformat()
    users[u]={"plan":"premium","expires":exp,"downloads":0}; save_users(users)
    await update.message.reply_text(f"✅ {u} until {exp}")

async def downgrade_cmd(update: Update, context):
    if update.effective_user.id!=ADMIN_ID: return
    if not context.args: return await update.message.reply_text("Usage: /downgrade <user>")
    u=context.args[0]; users[u]={"plan":"free","downloads":0}; save_users(users)
    await update.message.reply_text(f"⬇️ {u} free")

async def ban_cmd(update,ctx):
    if update.effective_user.id!=ADMIN_ID: return
    if not ctx.args: return await update.message.reply_text("Usage: /ban <user>")
    u=ctx.args[0]; users.setdefault(u,{})["banned"]=True; save_users(users)
    await update.message.reply_text(f"⛔ {u} banned")

async def unban_cmd(update,ctx):
    if update.effective_user.id!=ADMIN_ID: return
    if not ctx.args: return await update.message.reply_text("Usage: /unban <user>")
    u=ctx.args[0]
    if users.get(u,{}).pop("banned",None)!=None: save_users(users); await update.message.reply_text(f"✅ {u} unbanned")
    else: await update.message.reply_text("❌ Not banned")

async def stats_cmd(update,ctx):
    if update.effective_user.id!=ADMIN_ID: return
    downgrade_expired_users()
    tot=len(users); pr=sum(is_premium(u) for u in users.values()); await update.message.reply_text(f"Total:{tot} Premium:{pr} Free:{tot-pr}")

async def export_cmd(update,ctx):
    if update.effective_user.id!=ADMIN_ID: return
    p="/mnt/data/export.csv"
    with open(p,"w",newline="") as f:
        w=csv.writer(f); w.writerow(["user","plan","expires","dl"])
        for u,i in users.items(): w.writerow([u,i.get("plan"),i.get("expires",""),i.get("downloads",0)])
    await update.message.reply_document(InputFile(p))

# --- [SUPPORT] ---
async def support_reply(u,c):
    m=u.message
    if m.reply_to_message and u.effective_user.id==ADMIN_ID:
        mid=m.reply_to_message.message_id
        if mid in support_messages:
            await c.bot.send_message(support_messages[mid], f"📬 {m.text}")

async def user_support(update, context):
    if update.effective_user.id==ADMIN_ID: return
    m=await context.bot.send_message(ADMIN_ID, f"📩 @{update.effective_user.username}: {update.message.text}")
    support_messages[m.message_id]=update.effective_user.id
    await update.message.reply_text("✅ Sent.")

# --- [IPN] ---
async def ipn_handler(req):
    sig=req.headers.get("x-nowpayments-sig","")
    bd=await req.text()
    if not hmac.compare_digest(hmac.new(NOWPAYMENTS_IPN_SECRET.encode(),bd.encode(),hashlib.sha512).hexdigest(),sig):
        return web.Response(status=403)
    d=await req.json()
    if d.get("payment_status")=="finished":
        od=d.get("order_description","")
        if "|" in od:
            u,days=od.split("|"); days=int(days)
            users.setdefault(u,{"downloads":0}) .update({"plan":"premium","expires":(datetime.utcnow()+timedelta(days=days)).isoformat()})
            save_users(users)
    return web.Response(text="ok")

# --- [WEBHOOK SETUP] ---
async def webhook_handler(req):
    js=await req.json(); upd=Update.de_json(js,application.bot); await application.update_queue.put(upd)
    return web.Response(text="ok")

web_app=web.Application()
web_app.router.add_post("/webhook",webhook_handler)
web_app.router.add_post("/ipn",ipn_handler)
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
application.add_handler(CommandHandler("convertpdf",lambda u,c:convert_pdf(u,c,False)))
application.add_handler(CommandHandler("upgrade",upgrade_cmd))
application.add_handler(CommandHandler("downgrade",downgrade_cmd))
application.add_handler(CommandHandler("ban",ban_cmd))
application.add_handler(CommandHandler("unban",unban_cmd))
application.add_handler(CommandHandler("stats",stats_cmd))
application.add_handler(CommandHandler("export",export_cmd))
application.add_handler(MessageHandler(filters.TEXT & filters.REPLY, support_reply))
application.add_handler(MessageHandler(filters.TEXT & ~filters.Regex(r'^https?://'), user_support))

if __name__=="__main__":
    web.run_app(web_app,port=PORT)
