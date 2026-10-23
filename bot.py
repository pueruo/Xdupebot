import os
import io
import random
import time
import re
import subprocess
import shutil
import sqlite3
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
    return row and row[0] == "1"

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
    except:
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
        except:
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
# Image enhance (strong + no content loss)
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
# Video enhance (safe + strong + no content loss)
# ------------------------------------------------------
def enhance_video(input_path: str, output_path: str) -> bool:
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-map_metadata", "-1",
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",
        "-c:a", "aac",
        "-b:a", "192k",
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
            timeout=600,
            text=True
        )
        if result.returncode != 0:
            print("===== FFMPEG ERROR =====")
            print(result.stderr[-2000:] if result.stderr else "No stderr")
            return False
        return os.path.exists(output_path) and os.path.getsize(output_path) > 10000
    except Exception as e:
        print(f"FFmpeg Exception: {e}")
        return False

def compress_video_if_needed(path: str, unique_id: str, max_mb: float = 45.0) -> str:
    """Compress until under Telegram safe limit. Returns final path."""
    if not os.path.exists(path):
        return path
    size = os.path.getsize(path)
    limit = int(max_mb * 1024 * 1024)
    if size <= limit:
        return path
    crfs = [23, 26, 28, 30, 32, 35]
    current = path
    for i, crf in enumerate(crfs):
        out = f"comp_{unique_id}_{i}.mp4"
        cmd = [
            "ffmpeg", "-y", "-i", current,
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "faster", "-crf", str(crf),
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            out
        ]
        try:
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
            if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 10000:
                if current != path and os.path.exists(current):
                    try:
                        os.remove(current)
                    except:
                        pass
                current = out
                if os.path.getsize(current) <= limit:
                    return current
        except Exception as e:
            print(f"compress pass {i}: {e}")
    return current

async def safe_send_video(message, path: str, caption: str, unique_id: str):
    """Prefer video. On size / 413 / any send fail → document. Always try document last."""
    path = compress_video_if_needed(path, unique_id, max_mb=45.0)
    if not os.path.exists(path):
        return False
    size = os.path.getsize(path)

    # force document path for anything that looks risky
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
        err = str(e).lower()
        print(f"safe_send_video first try: {e}")
        # any failure → document
        try:
            path2 = compress_video_if_needed(path, unique_id + "_r", max_mb=38.0)
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
            print(f"document fallback failed: {e2}")
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
        except:
            pass
    except Exception as e:
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
        except:
            pass
    except Exception as e:
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
    text = f"📊 Bot Status\n\n"
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
            except:
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
        days = 30
    elif data == "buy_90":
        title = "3 Months Unlimited Access"
        description = "Get unlimited downloads for 90 days"
        payload = "plan_90"
        prices = [LabeledPrice("3 Months Unlimited", 249)]
        days = 90
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
    await update.message.reply_text(text, entities=entities, reply_markup=get_user_keyboard(update.effective_user.username if update.effective_user else None))

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
    await update.message.reply_text(text, entities=entities, reply_markup=get_user_keyboard(update.effective_user.username if update.effective_user else None))

async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    add_text("About this Bot:\n\n")
    add_text("• Send any image or video and get a cleaned unique version (no content loss).\n")
    add_text("• Helps reduce AI labels and unoriginal content strikes while keeping high quality.\n")
    add_text("• You can also send any ")
    add_custom("6201514939530618666")
    add_text(" Instagram, ")
    add_custom("6204008550298033491")
    add_text(" TikTok or ")
    add_custom("6203873525116183636")
    add_text(" Twitter/X link and I will download the media in original quality.\n\n")
    add_text("In groups: mention me with the photo, video or link.\n\n")
    add_text("For any help contact @pvnuo ")
    add_custom("6201528571756815592")

    text = "".join(text_parts)
    await update.message.reply_text(text, entities=entities, reply_markup=get_user_keyboard(update.effective_user.username if update.effective_user else None))

async def how(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "How it works:\n\n"
        "Photo mode:\n"
        "1. You send a photo\n"
        "2. I apply light unique modifications (no crop)\n"
        "3. You get a new high-quality version\n\n"
        "Video mode:\n"
        "1. You send a video\n"
        "2. I apply light unique changes (no crop)\n"
        "3. You get a high-quality unique version\n\n"
        "Link mode (Instagram / TikTok / Twitter/X):\n"
        "1. You send a link\n"
        "2. I download the media in original quality\n"
        "3. You receive it\n\n"
        "In groups: always mention me.\n\n"
        "For any help contact @pvnuo"
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
        is_reply = False
        if message.reply_to_message and message.reply_to_message.from_user:
            if (message.reply_to_message.from_user.is_bot and
                message.reply_to_message.from_user.username and
                message.reply_to_message.from_user.username.lower() == bot_username):
                is_reply = True
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
        except:
            pass
    except Exception as e:
        print(f"Image error: {e}")
        try:
            await status_msg.edit_text("❌ Failed to process the image.")
        except:
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
        is_reply = False
        if message.reply_to_message and message.reply_to_message.from_user:
            if (message.reply_to_message.from_user.is_bot and
                message.reply_to_message.from_user.username and
                message.reply_to_message.from_user.username.lower() == bot_username):
                is_reply = True
        if not mentioned and not is_reply:
            return

    status_msg = await message.reply_text("⌛")
    unique_id = str(int(time.time() * 1000))
    input_path = f"input_{unique_id}.mp4"
    output_path = f"output_{unique_id}.mp4"
    try:
        if message.video:
            file = await context.bot.get_file(message.video.file_id)
        elif message.document and message.document.mime_type and "video" in message.document.mime_type:
            file = await context.bot.get_file(message.document.file_id)
        else:
            await status_msg.edit_text("❌ Unsupported video format.")
            return

        await file.download_to_drive(input_path)
        success = enhance_video(input_path, output_path)

        if success and os.path.exists(output_path):
            ok = await safe_send_video(
                message, output_path, "✅ Here’s your modified video", unique_id
            )
            if ok:
                try:
                    await status_msg.delete()
                except:
                    pass
            else:
                await status_msg.edit_text("❌ Failed to process the video.")
        else:
            await status_msg.edit_text("❌ Failed to process the video.")
    except Exception as e:
        print(f"Video error: {e}")
        try:
            await status_msg.edit_text("❌ Failed to process the video.")
        except:
            pass
    finally:
        for p in [input_path, output_path]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except:
                    pass
        for f in os.listdir("."):
            if f.startswith(f"compressed_{unique_id}") or f.startswith(f"comp_{unique_id}"):
                try:
                    os.remove(f)
                except:
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
        print(f"Instaloader error: {e}")
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
        print(f"Download post error: {e}")
    return paths

# ------------------------------------------------------
# TikTok
# ------------------------------------------------------
async def download_tiktok(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
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

    add_text("Downloading from ")
    add_custom("6204008550298033491")
    add_text(" TikTok...")

    status_text = await update.message.reply_text("".join(text_parts), entities=entities)
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
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
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
            print(f"yt-dlp TikTok: {e}")
        has_video = any(p.lower().endswith((".mp4", ".mkv", ".webm")) for p in paths)
        if not has_video:
            try:
                cmd = [
                    "gallery-dl",
                    "--dest", folder,
                    "--filename", "{id}_{num}.{extension}",
                    "--no-mtime",
                    url
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
                if result.returncode == 0:
                    for root, _, files in os.walk(folder):
                        for f in files:
                            full = os.path.join(root, f)
                            if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mp3")):
                                paths.append(full)
            except Exception as e:
                print(f"gallery-dl error: {e}")
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
            except:
                pass
        else:
            try:
                await status_text.edit_text("❌ Failed to download. Make sure the link is public.")
                await status_emoji.delete()
            except:
                pass
    except Exception as e:
        print(f"TikTok error: {e}")
        try:
            await status_text.edit_text(f"❌ Failed to download TikTok.\n{str(e)[:120]}")
            await status_emoji.delete()
        except:
            pass
    finally:
        for f in opened_files:
            try:
                f.close()
            except:
                pass
        if os.path.exists(folder):
            try:
                shutil.rmtree(folder, ignore_errors=True)
            except:
                pass
        try:
            for item in os.listdir("."):
                if item.startswith(f"tt_{unique_id}") or item.startswith(f"temp_{unique_id}"):
                    path = os.path.join(".", item)
                    try:
                        if os.path.isdir(path):
                            shutil.rmtree(path, ignore_errors=True)
                        else:
                            os.remove(path)
                    except:
                        pass
        except:
            pass

# ------------------------------------------------------
# Twitter / X
# ------------------------------------------------------
async def download_twitter(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
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

    add_text("Downloading from ")
    add_custom("6203873525116183636")
    add_text(" Twitter/X...")

    status_text = await update.message.reply_text("".join(text_parts), entities=entities)
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
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Referer": "https://x.com/",
            },
        }
        try:
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
        except Exception as e:
            print(f"yt-dlp Twitter: {e}")
        if not paths:
            try:
                cmd = [
                    "gallery-dl",
                    "--dest", folder,
                    "--filename", "{id}_{num}.{extension}",
                    "--no-mtime",
                    url
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
                if result.returncode == 0:
                    for root, _, files in os.walk(folder):
                        for f in files:
                            full = os.path.join(root, f)
                            if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mkv", ".webm")):
                                paths.append(full)
            except Exception as e:
                print(f"gallery-dl Twitter error: {e}")
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
            except:
                pass
        else:
            try:
                await status_text.edit_text("❌ Failed to download. Make sure the link is public.")
                await status_emoji.delete()
            except:
                pass
    except Exception as e:
        print(f"Twitter error: {e}")
        try:
            await status_text.edit_text(f"❌ Failed to download Twitter/X.\n{str(e)[:120]}")
            await status_emoji.delete()
        except:
            pass
    finally:
        for f in opened_files:
            try:
                f.close()
            except:
                pass
        if os.path.exists(folder):
            try:
                shutil.rmtree(folder, ignore_errors=True)
            except:
                pass
        try:
            for item in os.listdir("."):
                if item.startswith(f"tw_{unique_id}") or item.startswith(f"temp_{unique_id}"):
                    path = os.path.join(".", item)
                    try:
                        if os.path.isdir(path):
                            shutil.rmtree(path, ignore_errors=True)
                        else:
                            os.remove(path)
                    except:
                        pass
        except:
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

    # Keyboard button handling
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
    # Admin keyboard buttons (only work if already admin-checked inside handlers)
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
        await update.message.reply_text(
            "Usage:\n"
            "/freeuser @username\n"
            "/freeuser 123456789\n\n"
            "Note: User must have started the bot at least once."
        )
        return
    if text == "Remove Free":
        await update.message.reply_text(
            "Usage:\n"
            "/removefree @username\n"
            "/removefree 123456789"
        )
        return

    if not text.startswith("http"):
        await message.reply_text(
            "About this Bot:\n\n"
            "• Just send me any image or video and I will give you a cleaned unique version.\n"
            "• The changes help reduce AI labels and unoriginal content strikes while keeping high visual quality.\n"
            "• You can also send any Instagram, TikTok or Twitter/X link and I will download the media for you in original quality.\n\n"
            "In groups: You must mention me with the photo, video or link.\n\n"
            "For any help contact @pvnuo",
            reply_markup=get_user_keyboard(username)
        )
        return

    # ===== LIMIT CHECK =====
    allowed, limit_msg = can_use_bot(user_id, username)
    if not allowed:
        await message.reply_text(limit_msg)
        return
    if message.chat.type in ["group", "supergroup"]:
        bot_username = (await context.bot.get_me()).username.lower()
        mentioned = f"@{bot_username}" in text.lower()
        is_reply = False
        if message.reply_to_message and message.reply_to_message.from_user:
            if (message.reply_to_message.from_user.is_bot and
                message.reply_to_message.from_user.username and
                message.reply_to_message.from_user.username.lower() == bot_username):
                is_reply = True
        if not mentioned and not is_reply:
            return
    lower = text.lower()
    # Twitter / X
    if "twitter.com" in lower or "x.com" in lower or "t.co" in lower:
        increment_usage(user_id)
        await download_twitter(update, context, text)
        return
    # TikTok
    if "tiktok.com" in lower or "vt.tiktok.com" in lower or "vm.tiktok.com" in lower:
        increment_usage(user_id)
        await download_tiktok(update, context, text)
        return
    # Instagram
    if "instagram.com" in lower:
        increment_usage(user_id)
        clean_url = re.sub(r"[?&](utm_|igshid|stkn|igsh)=[^&]+", "", text)
        clean_url = clean_url.split("?")[0].rstrip("/")
        
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

        add_text("Downloading from ")
        add_custom("6201514939530618666")
        add_text(" Instagram...")

        status_text = await message.reply_text("".join(text_parts), entities=entities)
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
                    "postprocessors": [{
                        "key": "FFmpegVideoConvertor",
                        "preferedformat": "mp4",
                    }],
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
                    if not paths and os.path.exists(filename):
                        paths = [filename]
            if paths:
                images = [p for p in paths if p.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]
                videos = [p for p in paths if p.lower().endswith((".mp4", ".mkv", ".webm"))]
                if images:
                    media = [InputMediaPhoto(open(p, "rb")) for p in images[:10]]
                    await message.reply_media_group(media=media)
                for v in videos:
                    await safe_send_video(
                        message, v, "✨ Instagram video", unique_id
                    )
                try:
                    await status_text.delete()
                    await status_emoji.delete()
                except:
                    pass
            else:
                try:
                    await status_text.edit_text("❌ Failed. Make sure link is public.")
                    await status_emoji.delete()
                except:
                    pass
        except Exception as e:
            print(f"IG error: {e}")
            try:
                await status_text.edit_text("❌ Failed to download.")
                await status_emoji.delete()
            except:
                pass
        finally:
            for p in paths:
                try:
                    os.remove(p)
                except:
                    pass
            folder = f"temp_{unique_id}"
            if os.path.exists(folder):
                try:
                    shutil.rmtree(folder, ignore_errors=True)
                except:
                    pass
            for f in os.listdir("."):
                if f.startswith(f"ig_{unique_id}"):
                    try:
                        os.remove(f)
                    except:
                        pass
        return
    await message.reply_text("❌ Only 📸 Instagram, 🎵 TikTok and 𝕏 Twitter/X links are supported.")

# ------------------------------------------------------
# Main
# ------------------------------------------------------
def main():
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
    # Normal commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("about", about))
    app.add_handler(CommandHandler("how", how))
    app.add_handler(CommandHandler("buy", buy))
    # Admin commands
    app.add_handler(CommandHandler("freeall", freeall))
    app.add_handler(CommandHandler("removefreeall", removefreeall))
    app.add_handler(CommandHandler("freeuser", freeuser))
    app.add_handler(CommandHandler("removefree", removefree))
    app.add_handler(CommandHandler("status", status))
    # Payment handlers
    app.add_handler(CallbackQueryHandler(buy_callback, pattern="^buy_"))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_image))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("Bot started successfully...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
