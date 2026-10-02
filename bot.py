import os
import logging
import shutil
import tempfile
import subprocess
import random
import time
import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageTransform
from dotenv import load_dotenv
from yt_dlp import YoutubeDL
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
FFMPEG_PATH = "ffmpeg"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

START_TEXT = """🎬 *X / Twitter Media Modifier Bot*

Send any x.com / twitter.com post link (video or image).
I download + apply strong unique modifications so Instagram treats it as original.

Just paste the link.

Help → @coolmoco"""

ABOUT_TEXT = """I only work with x.com / twitter.com links.

Send a post that contains a video or an image.
I will download it, apply unique modifications, and send the result back.

Important note about videos:
Please save the video to your device gallery first and then play it from there.
Telegram’s in-app player sometimes shows slightly different size or resolution.
The actual file quality is higher when opened from your gallery.

Need help? Contact @coolmoco"""

def enhance_image(image: Image.Image) -> Image.Image:
    if image.mode != "RGB":
        image = image.convert("RGB")

    width, height = image.size
    crop = random.uniform(0.04, 0.08)
    left = int(width * crop * random.uniform(0.2, 0.9))
    top = int(height * crop * random.uniform(0.2, 0.9))
    right = width - int(width * crop * random.uniform(0.2, 0.9))
    bottom = height - int(height * crop * random.uniform(0.2, 0.9))
    image = image.crop((left, top, right, bottom))

    angle = random.uniform(-2.5, 2.5)
    image = image.rotate(angle, resample=Image.BICUBIC, expand=False)

    w, h = image.size
    dx = random.uniform(-0.01, 0.01) * w
    dy = random.uniform(-0.01, 0.01) * h
    coeffs = ImageTransform.AffineTransform(
        (
            1 + random.uniform(-0.007, 0.007),
            random.uniform(-0.005, 0.005),
            dx,
            random.uniform(-0.005, 0.005),
            1 + random.uniform(-0.007, 0.007),
            dy,
        )
    )
    image = image.transform(image.size, Image.AFFINE, coeffs.data, resample=Image.BICUBIC)

    image = ImageEnhance.Color(image).enhance(random.uniform(1.10, 1.25))
    image = ImageEnhance.Brightness(image).enhance(random.uniform(1.02, 1.09))
    image = ImageEnhance.Contrast(image).enhance(random.uniform(1.05, 1.15))
    image = ImageEnhance.Sharpness(image).enhance(random.uniform(1.15, 1.40))

    r, g, b = image.split()
    r = r.point(lambda i: min(255, max(0, i + random.randint(-8, 9))))
    g = g.point(lambda i: min(255, max(0, i + random.randint(-6, 7))))
    b = b.point(lambda i: min(255, max(0, i + random.randint(-9, 8))))
    image = Image.merge("RGB", (r, g, b))

    arr = np.array(image).astype(np.int16)
    noise = np.random.randint(-4, 5, arr.shape)
    arr = np.clip(arr + noise, 0, 255).astype(np.uint8)
    image = Image.fromarray(arr)

    if random.random() > 0.45:
        image = image.filter(ImageFilter.GaussianBlur(radius=random.uniform(0.25, 0.6)))

    image = image.filter(
        ImageFilter.UnsharpMask(
            radius=random.uniform(1.3, 2.0),
            percent=random.randint(135, 180),
            threshold=random.randint(1, 3),
        )
    )

    scale = random.uniform(1.02, 1.07)
    image = image.resize((int(image.width * scale), int(image.height * scale)), Image.LANCZOS)
    return image

def enhance_video(input_path: str, output_path: str) -> bool:
    brightness = random.uniform(-0.04, 0.06)
    contrast = random.uniform(1.08, 1.16)
    saturation = random.uniform(1.10, 1.22)
    gamma = random.uniform(0.95, 1.06)
    hue = random.uniform(-4, 4)
    noise_str = random.randint(4, 9)
    crop_pct = random.uniform(0.010, 0.018)

    vf = (
        f"crop=iw*(1-{crop_pct}):ih*(1-{crop_pct}),"
        f"scale=-2:min(1080\\,ih),"
        f"scale=trunc(iw/2)*2:trunc(ih/2)*2,"
        f"eq=brightness={brightness}:contrast={contrast}:saturation={saturation}:gamma={gamma},"
        f"hue=h={hue},"
        f"noise=alls={noise_str}:allf=t,"
        f"unsharp=5:5:1.2:5:5:0.6,"
        f"format=yuv420p"
    )

    cmd = [
        FFMPEG_PATH, "-y",
        "-i", input_path,
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "19",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "-pix_fmt", "yuv420p",
        "-fs", "48M",
        output_path
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=150)
        if result.returncode != 0:
            vf_safe = (
                f"crop=iw*(1-{crop_pct}):ih*(1-{crop_pct}),"
                f"scale=-2:min(1080\\,ih),"
                f"scale=trunc(iw/2)*2:trunc(ih/2)*2,"
                f"eq=brightness={brightness}:contrast={contrast}:saturation={saturation}:gamma={gamma},"
                f"hue=h={hue},"
                f"unsharp=5:5:1.2:5:5:0.6,"
                f"format=yuv420p"
            )
            cmd[cmd.index("-vf") + 1] = vf_safe
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=150)
            if result.returncode != 0:
                logger.error(result.stderr[-800:] if result.stderr else "no stderr")
                return False
        return os.path.exists(output_path) and os.path.getsize(output_path) > 10000
    except Exception as e:
        logger.error(f"ffmpeg exception: {e}")
        return False

def download_media(url: str, temp_dir: str) -> str:
    ydl_opts = {
        "outtmpl": os.path.join(temp_dir, "%(id)s.%(ext)s"),
        "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "writethumbnail": True,
        "socket_timeout": 30,
        "retries": 3,
    }
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if filename and os.path.exists(filename):
                return filename
            for f in os.listdir(temp_dir):
                full = os.path.join(temp_dir, f)
                if f.lower().endswith((".mp4", ".webm", ".mkv", ".mov", ".jpg", ".jpeg", ".png", ".webp")):
                    return full
    except Exception as e:
        logger.info(f"yt-dlp: {e}")

    try:
        cmd = [
            "gallery-dl",
            "--dest", temp_dir,
            "--filename", "{id}_{num}.{extension}",
            "--no-mtime",
            url
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        if result.returncode == 0:
            for root, _, files in os.walk(temp_dir):
                for f in files:
                    full = os.path.join(root, f)
                    if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".mp4", ".webm")):
                        return full
    except Exception as e:
        logger.info(f"gallery-dl: {e}")

    raise RuntimeError("No media found in this tweet")

def process_link(url: str):
    temp_dir = tempfile.mkdtemp(prefix="x_")
    try:
        original = download_media(url, temp_dir)
        ext = os.path.splitext(original)[1].lower()

        if ext in (".mp4", ".webm", ".mkv", ".mov"):
            modified = os.path.join(temp_dir, "mod.mp4")
            ok = enhance_video(original, modified)
            if not ok:
                raise RuntimeError("video enhance failed")
        else:
            img = Image.open(original)
            result = enhance_image(img)
            modified = os.path.join(temp_dir, "mod.jpg")
            result.save(modified, format="JPEG", quality=random.randint(92, 96),
                        optimize=True, progressive=True, subsampling=0)

        return modified, temp_dir
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT, parse_mode="Markdown")

async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(ABOUT_TEXT)

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if "x.com" not in text and "twitter.com" not in text:
        await update.message.reply_text(ABOUT_TEXT)
        return

    status = await update.message.reply_text("⏳ Downloading & modifying…")
    temp_dir = None
    try:
        modified_path, temp_dir = process_link(text)
        if modified_path.endswith(".mp4"):
            with open(modified_path, "rb") as f:
                await update.message.reply_video(
                    video=f,
                    supports_streaming=True,
                    caption="✅ Unique modified version",
                    read_timeout=300,
                    write_timeout=300,
                    connect_timeout=60,
                    pool_timeout=60
                )
        else:
            with open(modified_path, "rb") as f:
                await update.message.reply_photo(
                    photo=f,
                    caption="✅ Unique modified version",
                    read_timeout=60,
                    write_timeout=60
                )
        try:
            await status.delete()
        except:
            pass
    except Exception as e:
        logger.exception("fail")
        err = str(e)
        # ignore pure timeout messages – video often still arrives
        if "Timed out" in err or "TimedOut" in err or "ReadError" in err:
            try:
                await status.delete()
            except:
                pass
            return
        if "WinError 2" in err or "No such file" in err:
            await status.edit_text("ffmpeg not found. Install + add to PATH.")
        elif "413" in err or "Too Large" in err:
            await status.edit_text("File too large for Telegram.")
        else:
            await status.edit_text(f"Failed: {err[:220]}")
    finally:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN missing in .env")

    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=60.0,
        read_timeout=300.0,
        write_timeout=300.0,
        pool_timeout=60.0
    )

    app = Application.builder().token(TOKEN).request(request).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("about", about))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()