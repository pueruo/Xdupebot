import os
import io
import random
import time
import re
import subprocess
import shutil
import sqlite3
import sys
from datetime import datetime, date, timedelta
from threading import Thread
from flask import Flask
from dotenv import load_dotenv
from telegram import (
    Update,
    InputMediaPhoto,
    LabeledPrice,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageEntity,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    filters,
    ContextTypes,
)
from PIL import Image, ImageEnhance, ImageFilter
import numpy as np
import yt_dlp
import instaloader
import requests

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not found in .env file!")

# ====================== CONFIG ======================
ADMIN_USERNAME = "pvnuo"
DAILY_FREE_LIMIT = 5
DB_FILE = "bot_data.db"
MAX_TG_DOWNLOAD_BYTES = 20 * 1024 * 1024  # 20MB Telegram Bot API limit
# ====================================================

# ------------------------------------------------------
# Database
# ------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS lifetime_free (
            user_id INTEGER PRIMARY KEY
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_usage (
            user_id INTEGER,
            usage_date TEXT,
            count INTEGER,
            PRIMARY KEY (user_id, usage_date)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS paid_users (
            user_id INTEGER PRIMARY KEY,
            expiry TEXT
        )
    """)
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('global_free', '0')")
    conn.commit()
    conn.close()

def is_global_free() -> bool:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = 'global_free'")
    row = c.fetchone()
    conn.close()
    return bool(row and row[0] == "1")

def set_global_free(enabled: bool):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('global_free', ?)", 
              ("1" if enabled else "0",))
    conn.commit()
    conn.close()

def is_lifetime_free(user_id: int) -> bool:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT 1 FROM lifetime_free WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return bool(row)

def add_lifetime_free(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO lifetime_free (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def remove_lifetime_free(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM lifetime_free WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def get_lifetime_free_list() -> list:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id FROM lifetime_free")
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def is_paid_user(user_id: int) -> bool:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT expiry FROM paid_users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return False
    try:
        expiry = date.fromisoformat(row[0])
        return expiry >= date.today()
    except Exception:
        return False

def add_paid_user(user_id: int, days: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT expiry FROM paid_users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    
    if row:
        try:
            current_expiry = date.fromisoformat(row[0])
            if current_expiry > date.today():
                new_expiry = current_expiry + timedelta(days=days)
            else:
                new_expiry = date.today() + timedelta(days=days)
        except Exception:
            new_expiry = date.today() + timedelta(days=days)
    else:
        new_expiry = date.today() + timedelta(days=days)
    
    c.execute("INSERT OR REPLACE INTO paid_users (user_id, expiry) VALUES (?, ?)", 
              (user_id, new_expiry.isoformat()))
    conn.commit()
    conn.close()
    return new_expiry

def get_today_usage(user_id: int) -> int:
    today = date.today().isoformat()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT count FROM daily_usage WHERE user_id = ? AND usage_date = ?", (user_id, today))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def increment_usage(user_id: int):
    today = date.today().isoformat()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT INTO daily_usage (user_id, usage_date, count) VALUES (?, ?, 1)
        ON CONFLICT(user_id, usage_date) DO UPDATE SET count = count + 1
    """, (user_id, today))
    conn.commit()
    conn.close()

def can_use_bot(user_id: int, username: str = None) -> tuple[bool, str]:
    if username and username.lower() == ADMIN_USERNAME.lower():
        return True, ""
    if is_global_free():
        return True, ""
    if is_lifetime_free(user_id):
        return True, ""
    if is_paid_user(user_id):
        return True, ""
    used = get_today_usage(user_id)
    if used < DAILY_FREE_LIMIT:
        return True, ""
    return False, (
        "You have used all 5 free links for today.\n"
        "Please try again tomorrow or contact @pvnuo for unlimited access.\n\n"
        "Or buy unlimited access:\n"
        "👉 /buy"
    )

# ------------------------------------------------------
# Flask keep-alive
# ------------------------------------------------------
app_flask = Flask(__name__)

@app_flask.route("/")
def home():
    return "Bot is alive and running!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app_flask.run(host="0.0.0.0", port=port)

def keep_alive():
    t = Thread(target=run_flask, daemon=True)
    t.start()

# ------------------------------------------------------
# Image enhance
# ------------------------------------------------------
def enhance_image(image: Image.Image) -> Image.Image:
    if image.mode != "RGB":
        image = image.convert("RGB")

    original_size = image.size

    data = list(image.getdata())
    clean = Image.new(image.mode, image.size)
    clean.putdata(data)

    angle = random.uniform(0.04, 0.12) * random.choice([-1, 1])
    clean = clean.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False)
    clean = clean.rotate(-angle, resample=Image.Resampling.BICUBIC, expand=False)

    w, h = clean.size
    factor = random.uniform(0.993, 0.998)
    clean = clean.resize((int(w * factor), int(h * factor)), Image.Resampling.LANCZOS)
    clean = clean.resize(original_size, Image.Resampling.LANCZOS)

    noise = Image.effect_noise(clean.size, random.uniform(1.0, 2.0))
    noise = noise.convert("L")
    noise = ImageEnhance.Brightness(noise).enhance(random.uniform(0.04, 0.09))
    clean = Image.blend(clean, noise.convert("RGB"), random.uniform(0.02, 0.045))

    r, g, b = clean.split()
    r = ImageEnhance.Brightness(r).enhance(random.uniform(0.996, 1.004))
    g = ImageEnhance.Brightness(g).enhance(random.uniform(0.996, 1.004))
    b = ImageEnhance.Brightness(b).enhance(random.uniform(0.996, 1.004))
    clean = Image.merge("RGB", (r, g, b))

    clean = clean.filter(ImageFilter.UnsharpMask(
        radius=random.uniform(0.6, 1.1),
        percent=random.randint(60, 95),
        threshold=2
    ))

    enhancer = ImageEnhance.Contrast(clean)
    clean = enhancer.enhance(random.uniform(1.015, 1.05))

    temp = io.BytesIO()
    clean.save(temp, format="PNG", optimize=True)
    temp.seek(0)
    clean = Image.open(temp).convert("RGB")

    out = io.BytesIO()
    clean.save(out, format="JPEG", quality=96, optimize=True, progressive=True, subsampling=0)
    out.seek(0)
    clean = Image.open(out).convert("RGB")

    return clean

# ------------------------------------------------------
# Video enhance
# ------------------------------------------------------
def check_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None

def enhance_video(input_path: str, output_path: str) -> bool:
    if not check_ffmpeg():
        print("CRITICAL: ffmpeg binary not found in system PATH.", flush=True)
        return False

    # preset=veryfast and threads=2 prevent OOM terminates on shared memory slices
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-map_metadata", "-1",
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-threads", "2",
        "-crf", "22",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-pix_fmt", "yuv420p",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        output_path
    ]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
            text=True
        )
        if result.returncode != 0:
            print("===== FFMPEG ERROR =====", flush=True)
            print(result.stderr[-2000:] if result.stderr else "No stderr", flush=True)
            return False
        return os.path.exists(output_path) and os.path.getsize(output_path) > 1000
    except Exception as e:
        print(f"FFmpeg Exception: {e}", flush=True)
        return False

def compress_video_if_needed(path: str, unique_id: str, max_mb: float = 45.0) -> str:
    if not os.path.exists(path) or not check_ffmpeg():
        return path
    size = os.path.getsize(path)
    limit = int(max_mb * 1024 * 1024)
    if size <= limit:
        return path
    crfs = [26, 28, 30, 32, 35]
    current = path
    for i, crf in enumerate(crfs):
        out = f"comp_{unique_id}_{i}.mp4"
        cmd = [
            "ffmpeg", "-y", "-i", current,
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "ultrafast", "-threads", "2", "-crf", str(crf),
            "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            out
        ]
        try:
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
            if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 1000:
                if current != path and os.path.exists(current):
                    try:
                        os.remove(current)
                    except Exception:
                        pass
                current = out
                if os.path.getsize(current) <= limit:
                    return current
        except Exception as e:
            print(f"compress pass {i}: {e}", flush=True)
    return current

async def safe_send_video(message, path: str, caption: str, unique_id: str):
    path = compress_video_if_needed(path, unique_id, max_mb=45.0)
    if not os.path.exists(path):
        return False
    size = os.path.getsize(path)
    force_doc = size > 40 * 1024 * 1024

    try:
        with open(path, "rb") as vid:
            if force_doc:
                await message.reply_document(
                    document=vid,
                    caption=caption + " (sent as file)",
                    filename=os.path.basename(path) or "video.mp4",
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=60,
                )
            else:
                await message.reply_video(
                    video=vid,
                    caption=caption,
                    supports_streaming=True,
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=60,
                )
        return True
    except Exception as e:
        print(f"safe_send_video failure: {e}", flush=True)
        try:
            path2 = compress_video_if_needed(path, unique_id + "_r", max_mb=35.0)
            with open(path2, "rb") as vid:
                await message.reply_document(
                    document=vid,
                    caption=caption + " (sent as file)",
                    filename=os.path.basename(path2) or "video.mp4",
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=60,
                )
            return True
        except Exception as e2:
            print(f"document fallback failed: {e2}", flush=True)
            return False

# ------------------------------------------------------
# Admin Commands
# ------------------------------------------------------
async def freeall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or (user.username or "").lower() != ADMIN_USERNAME.lower():
        return
    set_global_free(True)
    await update.message.reply_text("✅ Bot is now FREE for everyone.")

async def removefreeall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or (user.username or "").lower() != ADMIN_USERNAME.lower():
        return
    set_global_free(False)
    await update.message.reply_text("✅ Daily limit mode is now active again.")

async def freeuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or (user.username or "").lower() != ADMIN_USERNAME.lower():
        return
    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "/freeuser @username\n"
            "/freeuser 123456789\n\n"
            "Note: User must have started the bot at least once."
        )
        return
    target = context.args[0].lstrip("@")
    try:
        if target.isdigit():
            target_id = int(target)
            chat = await context.bot.get_chat(target_id)
        else:
            chat = await context.bot.get_chat(f"@{target}")
            target_id = chat.id
        add_lifetime_free(target_id)
        await update.message.reply_text(f"✅ @{chat.username or target_id} now has lifetime unlimited access.")
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text="🎉 You now have unlimited access to this bot forever!\nEnjoy."
            )
        except Exception:
            pass
    except Exception:
        await update.message.reply_text(
            f"❌ Could not find user.\n\n"
            f"Possible reasons:\n"
            f"• User has never started this bot\n"
            f"• Username is incorrect\n\n"
            f"Solution: Ask the user to send /start to the bot first, then try again.\n"
            f"Or use their numeric User ID."
        )

async def removefree(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or (user.username or "").lower() != ADMIN_USERNAME.lower():
        return
    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "/removefree @username\n"
            "/removefree 123456789"
        )
        return
    target = context.args[0].lstrip("@")
    try:
        if target.isdigit():
            target_id = int(target)
            chat = await context.bot.get_chat(target_id)
        else:
            chat = await context.bot.get_chat(f"@{target}")
            target_id = chat.id
        remove_lifetime_free(target_id)
        await update.message.reply_text(f"✅ Removed lifetime free from @{chat.username or target_id}")
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text="Your unlimited access has been removed.\nYou are now on the free daily limit (5 links per day)."
            )
        except Exception:
            pass
    except Exception:
        await update.message.reply_text(
            f"❌ Could not find user.\n\n"
            f"User must have started the bot at least once.\n"
            f"Try using their numeric User ID instead."
        )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or (user.username or "").lower() != ADMIN_USERNAME.lower():
        return
    global_free = is_global_free()
    free_list = get_lifetime_free_list()
    ffmpeg_ready = check_ffmpeg()
    text = f"📊 Bot Status\n\n"
    text += f"FFmpeg Installed: {'YES ✅' if ffmpeg_ready else 'NO ❌ (Railway nixpacks.toml needed)'}\n"
    text += f"Global Free Mode: {'ON ✅' if global_free else 'OFF (Daily limit active)'}\n"
    text += f"Daily Free Limit: {DAILY_FREE_LIMIT}\n\n"
    text += f"Lifetime Free Users ({len(free_list)}):\n"
    if free_list:
        for uid in free_list:
            username_str = ""
            try:
                chat = await context.bot.get_chat(uid)
                if chat.username:
                    username_str = f" (@{chat.username})"
                elif chat.first_name:
                    username_str = f" ({chat.first_name})"
            except Exception:
                pass
            text += f"• {uid}{username_str}\n"
    else:
        text += "None\n"
    await update.message.reply_text(text)

# ------------------------------------------------------
# Buy / Payment
# ------------------------------------------------------
async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("30 Days Unlimited — 79 ⭐", callback_data="buy_30")],
        [InlineKeyboardButton("3 Months Unlimited — 249 ⭐", callback_data="buy_90")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "💎 Choose a plan:\n\n"
        "• 30 Days Unlimited Access → 79 Stars\n"
        "• 3 Months Unlimited Access → 249 Stars\n\n"
        "———————————————\n"
        "Or pay with crypto:\n\n"
        "SOL (Solana):\n"
        "7vxg8qL8Mc3zG5TH9GoQSuxTAVnUS5BaacWkPrNL2wYe\n\n"
        "Pay $5 for 30 days trial\n"
        "Pay $12 for 3 month trial\n\n"
        "Send screenshot to @pvnuo for unlimited access.\n\n"
        "For any help contact @pvnuo",
        reply_markup=reply_markup
    )

async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    user_id = query.from_user.id
    
    if data == "buy_30":
        title = "30 Days Unlimited Access"
        description = "Get unlimited downloads for 30 days"
        payload = "plan_30"
        prices = [LabeledPrice("30 Days Unlimited", 79)]
    elif data == "buy_90":
        title = "3 Months Unlimited Access"
        description = "Get unlimited downloads for 90 days"
        payload = "plan_90"
        prices = [LabeledPrice("3 Months Unlimited", 249)]
    else:
        return
    
    await context.bot.send_invoice(
        chat_id=user_id,
        title=title,
        description=description,
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=prices,
        start_parameter="buy"
    )

async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    await query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    user_id = update.effective_user.id
    payload = payment.invoice_payload
    
    if payload == "plan_30":
        days = 30
    elif payload == "plan_90":
        days = 90
    else:
        days = 30
    
    expiry = add_paid_user(user_id, days)
    
    await update.message.reply_text(
        f"✅ Payment successful!\n\n"
        f"You now have unlimited access until {expiry.strftime('%d %b %Y')}.\n"
        f"Enjoy the bot!"
    )

# ------------------------------------------------------
# Keyboard
# ------------------------------------------------------
def get_user_keyboard(username: str = None):
    keyboard = [
        [KeyboardButton("How to use"), KeyboardButton("About")],
        [KeyboardButton("Buy"), KeyboardButton("Help")],
    ]
    if username and username.lower() == ADMIN_USERNAME.lower():
        keyboard.append([KeyboardButton("Status"), KeyboardButton("Free All")])
        keyboard.append([KeyboardButton("Remove Free All"), KeyboardButton("Free User")])
        keyboard.append([KeyboardButton("Remove Free")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ------------------------------------------------------
# Commands
# ------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text_parts = []
    entities = []
    current_offset = 0

    def add_text(s: str):
        nonlocal current_offset
        text_parts.append(s)
        current_offset += len(s.encode("utf-16-le")) // 2

    def add_custom(emoji_id: str, placeholder: str = "🔥"):
        nonlocal current_offset
        text_parts.append(placeholder)
        length = len(placeholder.encode("utf-16-le")) // 2
        entities.append(MessageEntity(
            type=MessageEntity.CUSTOM_EMOJI,
            offset=current_offset,
            length=length,
            custom_emoji_id=str(emoji_id)
        ))
        current_offset += length

    add_text("Welcome!\n\n")
    add_text("Send any image or video and I will clean it for you.\n")
    add_text("This helps remove AI labels and reduces the chance of unoriginal content strikes when you repost.\n\n")
    add_text("You can also send any ")
    add_custom("6201514939530618666")
    add_text(" Instagram, ")
    add_custom("6204008550298033491")
    add_text(" TikTok or ")
    add_custom("6203873525116183636")
    add_text(" Twitter/X link and I will download it in original quality.\n\n")
    add_text("🚨 Note: No tool can completely remove AI signatures from every file. Results vary by platform and content, but this bot works well on many images & videos.\n\n")
    add_text("In groups: mention me with the photo, video or link.\n\n")
    add_text("For any help contact @pvnuo ")
    add_custom("6201528571756815592")

    text = "".join(text_parts)
    await update.message.reply_text(
        text, 
        entities=entities, 
        reply_markup=get_user_keyboard(update.effective_user.username if update.effective_user else None)
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text_parts = []
    entities = []
    current_offset = 0

    def add_text(s: str):
        nonlocal current_offset
        text_parts.append(s)
        current_offset += len(s.encode("utf-16-le")) // 2

    def add_custom(emoji_id: str, placeholder: str = "🔥"):
        nonlocal current_offset
        text_parts.append(placeholder)
        length = len(placeholder.encode("utf-16-le")) // 2
        entities.append(MessageEntity(
            type=MessageEntity.CUSTOM_EMOJI,
            offset=current_offset,
            length=length,
            custom_emoji_id=str(emoji_id)
        ))
        current_offset += length

    add_text("Available Commands:\n\n")
    add_text("/start  – Start the bot\n")
    add_text("/help   – Show this help\n")
    add_text("/about  – About this bot\n")
    add_text("/how    – How it works\n")
    add_text("/buy    – Buy unlimited access\n\n")
    add_text("How to use:\n")
    add_text("• Send any photo → get a unique cleaned version\n")
    add_text("• Send any video → get a unique high-quality version\n")
    add_text("• Send ")
    add_custom("6201514939530618666")
    add_text(" Instagram / ")
    add_custom("6204008550298033491")
    add_text(" TikTok / ")
    add_custom("6203873525116183636")
    add_text(" Twitter/X link → download media in original quality\n\n")
    add_text("In groups: mention me with the photo, video or link.\n\n")
    add_text("For any help contact @pvnuo")

    text = "".join(text_parts)
    await update.message.reply_text(
        text, 
        entities=entities, 
        reply_markup=get_user_keyboard(update.effective_user.username if update.effective_user else None)
    )

async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "About this Bot:\n\n"
        "• Send any image or video and get a cleaned unique version (no content loss).\n"
        "• Helps reduce AI labels and unoriginal content strikes while keeping high quality.\n"
        "• You can also send Instagram, TikTok or Twitter/X links to download original media.\n\n"
        "For any help contact @pvnuo"
    )
    await update.message.reply_text(text, reply_markup=get_user_keyboard(update.effective_user.username if update.effective_user else None))

async def how(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "How it works:\n\n"
        "Photo mode: Light unique modifications applied without cropping.\n"
        "Video mode: Metadata stripped, frame timings adjusted, re-encoded in original spec.\n"
        "Links: Automatic stream discovery and download.\n\n"
        "Note: Telegram restricts direct bot downloads to files under 20 MB."
    )
    await update.message.reply_text(text, reply_markup=get_user_keyboard(update.effective_user.username if update.effective_user else None))

# ------------------------------------------------------
# Image handler
# ------------------------------------------------------
async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return
    if message.chat.type in ["group", "supergroup"]:
        bot_username = (await context.bot.get_me()).username.lower()
        caption = (message.caption or "").lower()
        mentioned = f"@{bot_username}" in caption
        is_reply = bool(
            message.reply_to_message and 
            message.reply_to_message.from_user and 
            message.reply_to_message.from_user.is_bot and 
            (message.reply_to_message.from_user.username or "").lower() == bot_username
        )
        if not mentioned and not is_reply:
            return

    status_msg = await message.reply_text("⌛")
    start_time = time.time()
    try:
        if message.photo:
            file = await context.bot.get_file(message.photo[-1].file_id)
        elif message.document and message.document.mime_type and "image" in message.document.mime_type:
            file = await context.bot.get_file(message.document.file_id)
        else:
            await status_msg.delete()
            return
        image_bytes = await file.download_as_bytearray()
        image = Image.open(io.BytesIO(image_bytes))
        result = enhance_image(image)
        output = io.BytesIO()
        quality = random.randint(95, 97)
        result.save(output, format="JPEG", quality=quality, optimize=True, progressive=True, subsampling=0)
        output.seek(0)
        elapsed = round(time.time() - start_time, 2)
        await message.reply_photo(
            photo=output,
            caption=f"✅ Unique version ready\n⏱ {elapsed}s | Quality: {quality}",
        )
        try:
            await status_msg.delete()
        except Exception:
            pass
    except Exception as e:
        print(f"Image error: {e}", flush=True)
        try:
            await status_msg.edit_text("❌ Failed to process the image.")
        except Exception:
            await message.reply_text("❌ Failed to process the image.")

# ------------------------------------------------------
# Video handler
# ------------------------------------------------------
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return
    if message.chat.type in ["group", "supergroup"]:
        bot_username = (await context.bot.get_me()).username.lower()
        caption = (message.caption or "").lower()
        mentioned = f"@{bot_username}" in caption
        is_reply = bool(
            message.reply_to_message and 
            message.reply_to_message.from_user and 
            message.reply_to_message.from_user.is_bot and 
            (message.reply_to_message.from_user.username or "").lower() == bot_username
        )
        if not mentioned and not is_reply:
            return

    status_msg = await message.reply_text("⌛ Checking file...")
    
    # Check 1: FFmpeg installation verification
    if not check_ffmpeg():
        print("ERROR: User sent video but ffmpeg is missing from Railway container.", flush=True)
        await status_msg.edit_text(
            "❌ Server configuration error: `ffmpeg` is not installed on this host.\n"
            "Deploy `nixpacks.toml` to Railway with `nixPkgs = ['ffmpeg']`."
        )
        return

    # Check 2: Telegram Bot API 20 MB size constraint
    target_obj = message.video or (message.document if message.document and "video" in (message.document.mime_type or "") else None)
    if not target_obj:
        await status_msg.edit_text("❌ Unsupported video format.")
        return

    if target_obj.file_size and target_obj.file_size > MAX_TG_DOWNLOAD_BYTES:
        size_mb = round(target_obj.file_size / (1024 * 1024), 1)
        await status_msg.edit_text(
            f"❌ File too large ({size_mb} MB).\n"
            f"Telegram Bot API limits direct bot downloads to 20 MB max.\n"
            f"Compress or trim the video before sending."
        )
        return

    await status_msg.edit_text("⌛ Processing video...")
    unique_id = str(int(time.time() * 1000))
    input_path = f"input_{unique_id}.mp4"
    output_path = f"output_{unique_id}.mp4"

    try:
        file = await context.bot.get_file(target_obj.file_id)
        await file.download_to_drive(input_path)

        success = enhance_video(input_path, output_path)

        if success and os.path.exists(output_path):
            ok = await safe_send_video(
                message, output_path, "✅ Here’s your modified video", unique_id
            )
            if ok:
                try:
                    await status_msg.delete()
                except Exception:
                    pass
            else:
                await status_msg.edit_text("❌ Failed to transmit processed video.")
        else:
            await status_msg.edit_text("❌ Transcode failed. Check codec compatibility.")
    except Exception as e:
        print(f"Video pipeline exception: {e}", flush=True)
        try:
            await status_msg.edit_text(f"❌ Failed to process video: {str(e)[:100]}")
        except Exception:
            pass
    finally:
        for p in [input_path, output_path]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
        for f in os.listdir("."):
            if f.startswith(f"compressed_{unique_id}") or f.startswith(f"comp_{unique_id}"):
                try:
                    os.remove(f)
                except Exception:
                    pass

# ------------------------------------------------------
# Instagram
# ------------------------------------------------------
def download_instagram(url: str, unique_id: str):
    L = instaloader.Instaloader(
        download_videos=True,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        post_metadata_txt_pattern="",
        max_connection_attempts=3,
    )
    shortcode = None
    if "/p/" in url:
        shortcode = url.split("/p/")[1].split("/")[0].split("?")[0]
    elif "/reel/" in url:
        shortcode = url.split("/reel/")[1].split("/")[0].split("?")[0]
    elif "/tv/" in url:
        shortcode = url.split("/tv/")[1].split("/")[0].split("?")[0]
    if not shortcode:
        return []
    try:
        post = instaloader.Post.from_shortcode(L.context, shortcode)
    except Exception as e:
        print(f"Instaloader error: {e}", flush=True)
        return []
    paths = []
    folder = f"temp_{unique_id}"
    os.makedirs(folder, exist_ok=True)
    try:
        L.download_post(post, target=folder)
        for file in os.listdir(folder):
            full = os.path.join(folder, file)
            if file.endswith((".jpg", ".jpeg", ".png", ".webp", ".mp4")):
                paths.append(full)
    except Exception as e:
        print(f"Download post error: {e}", flush=True)
    return paths

# ------------------------------------------------------
# TikTok
# ------------------------------------------------------
async def download_tiktok(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    status_text = await update.message.reply_text("Downloading from TikTok...")
    status_emoji = await update.message.reply_text("⌛")

    unique_id = str(int(time.time() * 1000))
    folder = f"tt_{unique_id}_dir"
    os.makedirs(folder, exist_ok=True)
    paths = []
    opened_files = []
    try:
        ydl_opts = {
            "outtmpl": os.path.join(folder, f"tt_{unique_id}.%(ext)s"),
            "format": "bv*[height<=1080]+ba/b[height<=1080]/best[height<=1080]/best",
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 60,
            "retries": 10,
            "fragment_retries": 10,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36",
                "Referer": "https://www.tiktok.com/",
            },
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                base = filename.rsplit(".", 1)[0]
                for ext in [".mp4", ".mkv", ".webm", ".mp3", ".m4a"]:
                    candidate = base + ext
                    if os.path.exists(candidate):
                        paths.append(candidate)
                        break
        except Exception as e:
            print(f"yt-dlp TikTok: {e}", flush=True)

        images = [p for p in paths if p.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]
        videos = [p for p in paths if p.lower().endswith((".mp4", ".mkv", ".webm"))]
        audios = [p for p in paths if p.lower().endswith((".mp3", ".m4a"))]
        success = False
        if images:
            media = []
            for p in images[:10]:
                f = open(p, "rb")
                opened_files.append(f)
                media.append(InputMediaPhoto(f))
            await update.message.reply_media_group(media=media)
            success = True
        for v in videos:
            ok = await safe_send_video(
                update.message, v, "✅ Here’s your TikTok video", unique_id
            )
            if ok:
                success = True
        if not success and audios:
            with open(audios[0], "rb") as a:
                await update.message.reply_audio(audio=a, caption="🎵 TikTok audio")
            success = True
        if success:
            try:
                await status_text.delete()
                await status_emoji.delete()
            except Exception:
                pass
        else:
            await status_text.edit_text("❌ Failed to download. Make sure the link is public.")
            await status_emoji.delete()
    except Exception as e:
        print(f"TikTok error: {e}", flush=True)
        try:
            await status_text.edit_text(f"❌ Failed to download TikTok.\n{str(e)[:120]}")
            await status_emoji.delete()
        except Exception:
            pass
    finally:
        for f in opened_files:
            try:
                f.close()
            except Exception:
                pass
        if os.path.exists(folder):
            try:
                shutil.rmtree(folder, ignore_errors=True)
            except Exception:
                pass

# ------------------------------------------------------
# Twitter / X
# ------------------------------------------------------
async def download_twitter(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    status_text = await update.message.reply_text("Downloading from Twitter/X...")
    status_emoji = await update.message.reply_text("⌛")

    unique_id = str(int(time.time() * 1000))
    folder = f"tw_{unique_id}_dir"
    os.makedirs(folder, exist_ok=True)
    paths = []
    opened_files = []
    try:
        ydl_opts = {
            "outtmpl": os.path.join(folder, f"tw_{unique_id}_%(id)s.%(ext)s"),
            "format": "bv*[height<=1080]+ba/b[height<=1080]/best[height<=1080]/best",
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 60,
            "retries": 10,
            "fragment_retries": 10,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36",
                "Referer": "https://x.com/",
            },
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if "entries" in info:
                for entry in info["entries"]:
                    if entry:
                        filename = ydl.prepare_filename(entry)
                        base = filename.rsplit(".", 1)[0]
                        for ext in [".mp4", ".mkv", ".webm", ".jpg", ".jpeg", ".png", ".webp"]:
                            candidate = base + ext
                            if os.path.exists(candidate):
                                paths.append(candidate)
            else:
                filename = ydl.prepare_filename(info)
                base = filename.rsplit(".", 1)[0]
                for ext in [".mp4", ".mkv", ".webm", ".jpg", ".jpeg", ".png", ".webp"]:
                    candidate = base + ext
                    if os.path.exists(candidate):
                        paths.append(candidate)
                        break

        images = [p for p in paths if p.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]
        videos = [p for p in paths if p.lower().endswith((".mp4", ".mkv", ".webm"))]
        success = False
        if images:
            media = []
            for p in images[:10]:
                f = open(p, "rb")
                opened_files.append(f)
                media.append(InputMediaPhoto(f))
            await update.message.reply_media_group(media=media)
            success = True
        for v in videos:
            ok = await safe_send_video(
                update.message, v, "✅ Here’s your Twitter/X video", unique_id
            )
            if ok:
                success = True
        if success:
            try:
                await status_text.delete()
                await status_emoji.delete()
            except Exception:
                pass
        else:
            await status_text.edit_text("❌ Failed to download. Make sure the link is public.")
            await status_emoji.delete()
    except Exception as e:
        print(f"Twitter error: {e}", flush=True)
        try:
            await status_text.edit_text(f"❌ Failed to download Twitter/X.\n{str(e)[:120]}")
            await status_emoji.delete()
        except Exception:
            pass
    finally:
        for f in opened_files:
            try:
                f.close()
            except Exception:
                pass
        if os.path.exists(folder):
            try:
                shutil.rmtree(folder, ignore_errors=True)
            except Exception:
                pass

# ------------------------------------------------------
# Text handler
# ------------------------------------------------------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return
    text = message.text.strip()
    user = update.effective_user
    user_id = user.id if user else 0
    username = user.username if user else None

    if text == "How to use":
        await how(update, context)
        return
    if text == "About":
        await about(update, context)
        return
    if text == "Buy":
        await buy(update, context)
        return
    if text == "Help":
        await help_command(update, context)
        return
    if text == "Status":
        await status(update, context)
        return
    if text == "Free All":
        await freeall(update, context)
        return
    if text == "Remove Free All":
        await removefreeall(update, context)
        return
    if text == "Free User":
        await update.message.reply_text("Usage:\n/freeuser @username\n/freeuser 123456789")
        return
    if text == "Remove Free":
        await update.message.reply_text("Usage:\n/removefree @username\n/removefree 123456789")
        return

    if not text.startswith("http"):
        await message.reply_text(
            "Send an image or video directly, or supply an Instagram, TikTok, or Twitter/X URL.",
            reply_markup=get_user_keyboard(username)
        )
        return

    allowed, limit_msg = can_use_bot(user_id, username)
    if not allowed:
        await message.reply_text(limit_msg)
        return

    lower = text.lower()
    if any(d in lower for d in ["twitter.com", "x.com", "t.co"]):
        increment_usage(user_id)
        await download_twitter(update, context, text)
        return
    if any(d in lower for d in ["tiktok.com", "vt.tiktok.com", "vm.tiktok.com"]):
        increment_usage(user_id)
        await download_tiktok(update, context, text)
        return
    if "instagram.com" in lower:
        increment_usage(user_id)
        clean_url = re.sub(r"[?&](utm_|igshid|stkn|igsh)=[^&]+", "", text).split("?")[0].rstrip("/")
        status_text = await message.reply_text("Downloading from Instagram...")
        status_emoji = await message.reply_text("⌛")
        
        unique_id = str(int(time.time() * 1000))
        paths = []
        try:
            paths = download_instagram(clean_url, unique_id)
            if not paths:
                ydl_opts = {
                    "outtmpl": f"ig_{unique_id}.%(ext)s",
                    "quiet": True,
                    "no_warnings": True,
                    "format": "bv*+ba/b",
                    "merge_output_format": "mp4",
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(clean_url, download=True)
                    filename = ydl.prepare_filename(info)
                    base = filename.rsplit(".", 1)[0]
                    for ext in [".mp4", ".mkv", ".webm"]:
                        candidate = base + ext
                        if os.path.exists(candidate):
                            paths.append(candidate)
                            break
            if paths:
                images = [p for p in paths if p.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]
                videos = [p for p in paths if p.lower().endswith((".mp4", ".mkv", ".webm"))]
                if images:
                    opened = [open(p, "rb") for p in images[:10]]
                    media = [InputMediaPhoto(f) for f in opened]
                    await message.reply_media_group(media=media)
                    for f in opened:
                        f.close()
                for v in videos:
                    await safe_send_video(message, v, "✨ Instagram video", unique_id)
                await status_text.delete()
                await status_emoji.delete()
            else:
                await status_text.edit_text("❌ Download failed. Ensure post is public.")
                await status_emoji.delete()
        except Exception as e:
            print(f"IG error: {e}", flush=True)
            await status_text.edit_text("❌ Download error.")
            await status_emoji.delete()
        finally:
            for p in paths:
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
            folder = f"temp_{unique_id}"
            if os.path.exists(folder):
                try:
                    shutil.rmtree(folder, ignore_errors=True)
                except Exception:
                    pass
        return

    await message.reply_text("❌ Unsupported URL platform.")

# ------------------------------------------------------
# Main
# ------------------------------------------------------
def main():
    print(f"Checking environment... FFmpeg available: {check_ffmpeg()}", flush=True)
    init_db()
    keep_alive()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(60.0)
        .read_timeout(120.0)
        .write_timeout(120.0)
        .pool_timeout(60.0)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("about", about))
    app.add_handler(CommandHandler("how", how))
    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(CommandHandler("freeall", freeall))
    app.add_handler(CommandHandler("removefreeall", removefreeall))
    app.add_handler(CommandHandler("freeuser", freeuser))
    app.add_handler(CommandHandler("removefree", removefree))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(buy_callback, pattern="^buy_"))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_image))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("Bot loop started.", flush=True)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
