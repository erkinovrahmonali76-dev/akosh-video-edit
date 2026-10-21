import os
import re
import shutil
import sqlite3
import logging
import tempfile
import subprocess
import asyncio
from pathlib import Path
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ==================================================
# SOZLAMALAR — Replit Secrets'dan olinadi
# ==================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip())
except ValueError:
    ADMIN_ID = 0

PAYMENT_CARD_NUMBER = os.getenv(
    "PAYMENT_CARD_NUMBER", "6262 5701 3419 2691"
).strip()

PAYMENT_CARD_HOLDER = os.getenv(
    "PAYMENT_CARD_HOLDER", "boratova zarnigor"
).strip()

PAYMENT_INFO = os.getenv(
    "PAYMENT_INFO", "To'lovdan keyin chek rasmini yuboring."
).strip()

PAYME_PAYMENT_URL = os.getenv("PAYME_PAYMENT_URL", "").strip()
CLICK_PAYMENT_URL = os.getenv("CLICK_PAYMENT_URL", "").strip()

DB_NAME = "akosh_video_edit.sqlite3"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# ==================================================
# MENYULAR VA XIZMATLAR
# ==================================================

MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["🎬 Video yasash"],
        ["🤖 AI VIDEO MONTAJ"],
        ["💳 To'lov qilish", "📦 Buyurtmalarim"],
    ],
    resize_keyboard=True,
)

SERVICE_MENU = ReplyKeyboardMarkup(
    [
        ["▶️ YouTube video montaj"],
        ["📱 Instagram video edit"],
        ["😂 Vayn edit"],
        ["⬅️ Bosh menyu"],
    ],
    resize_keyboard=True,
)

SERVICES = {
    "▶️ YouTube video montaj": (
        "YouTube video montaj (30–60 daqiqa)",
        150000,
    ),
    "📱 Instagram video edit": (
        "Instagram video edit",
        40000,
    ),
    "😂 Vayn edit": (
        "Vayn edit",
        60000,
    ),
}

STATUS_NAMES = {
    "awaiting_payment": "To'lov kutilmoqda",
    "awaiting_receipt": "Chek kutilmoqda",
    "awaiting_review": "Admin chekni tekshirmoqda",
    "awaiting_source": "Video yoki havola kutilmoqda",
    "ai_waiting_video": "AI montaj uchun video kutilmoqda",
    "ai_waiting_instruction": "AI montaj topshirig'i kutilmoqda",
    "ai_awaiting_price": "Admin narx belgilashi kutilmoqda",
    "ai_processing": "AI montaj jarayonida",
    "ai_failed": "AI montajda xatolik",
    "in_progress": "Montaj jarayonida",
    "completed": "Yakunlangan",
    "cancelled": "Bekor qilingan",
}


# ==================================================
# TO'LOV TUGMALARI
# ==================================================

def payment_buttons(order_id):
    buttons = []

    # Haqiqiy Payme havolasi kiritilgan bo'lsa ko'rsatiladi.
    if PAYME_PAYMENT_URL:
        buttons.append([
            InlineKeyboardButton(
                "🟢 Payme orqali to'lash",
                url=PAYME_PAYMENT_URL,
            )
        ])

    # Haqiqiy Click havolasi kiritilgan bo'lsa ko'rsatiladi.
    if CLICK_PAYMENT_URL:
        buttons.append([
            InlineKeyboardButton(
                "🔵 Click orqali to'lash",
                url=CLICK_PAYMENT_URL,
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "💳 Karta orqali to'lash",
            callback_data=f"card:{order_id}",
        )
    ])

    buttons.append([
        InlineKeyboardButton(
            "✅ To'lov qildim — chek yuborish",
            callback_data=f"paid:{order_id}",
        )
    ])

    return InlineKeyboardMarkup(buttons)


def receipt_buttons(order_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Qabul qilish",
                callback_data=f"approve:{order_id}",
            ),
            InlineKeyboardButton(
                "❌ Rad etish",
                callback_data=f"reject:{order_id}",
            ),
        ]
    ])


def final_video_button(order_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🎞️ Tayyor videoni yuborish",
                callback_data=f"select:{order_id}",
            )
        ]
    ])


# ==================================================
# SQLITE BAZASI
# ==================================================

def connect_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with connect_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                customer_name TEXT,
                service_name TEXT NOT NULL,
                price INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payment_method TEXT,
                receipt_file_id TEXT,
                source_text TEXT,
                source_file_id TEXT,
                source_type TEXT,
                final_file_id TEXT
            )
        """)

        # Eski baza bo'lsa, yangi ustunlarni xavfsiz qo'shishga urinamiz.
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(orders)").fetchall()
        }

        if "payment_method" not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN payment_method TEXT")
        if "edit_instruction" not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN edit_instruction TEXT")
        if "ai_mode" not in columns:
            if "music_file_id" not in columns:
                conn.execute(
                    "ALTER TABLE orders ADD COLUMN music_file_id TEXT"
                )
            if "music_source_type" not in columns:
                conn.execute(
                    "ALTER TABLE orders ADD COLUMN music_source_type TEXT"
                )

            conn.execute("ALTER TABLE orders ADD COLUMN ai_mode INTEGER NOT NULL DEFAULT 0")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS active_orders (
                user_id INTEGER PRIMARY KEY,
                order_id INTEGER NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_active_orders (
                admin_id INTEGER PRIMARY KEY,
                order_id INTEGER NOT NULL
            )
        """)


def create_order(user_id, customer_name, service_name, price):
    with connect_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO orders
            (user_id, customer_name, service_name, price, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                customer_name,
                service_name,
                price,
                "awaiting_payment",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        order_id = cursor.lastrowid

        conn.execute(
            """
            INSERT INTO active_orders (user_id, order_id)
            VALUES (?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET order_id = excluded.order_id
            """,
            (user_id, order_id),
        )

    return order_id


def create_ai_order(user_id, customer_name):
    with connect_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO orders
            (user_id, customer_name, service_name, price, status, created_at, ai_mode)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (user_id, customer_name, "AI video montaj", 0, "ai_waiting_video",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        order_id = cursor.lastrowid
        conn.execute(
            """INSERT INTO active_orders (user_id, order_id)
               VALUES (?, ?) ON CONFLICT(user_id)
               DO UPDATE SET order_id = excluded.order_id""",
            (user_id, order_id),
        )
    return order_id


def ai_admin_buttons(order_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("💰 Narx belgilash", callback_data=f"setprice:{order_id}"),
        InlineKeyboardButton("❌ Rad etish", callback_data=f"aireject:{order_id}"),
    ]])


def get_order(order_id):
    if not order_id:
        return None

    with connect_db() as conn:
        return conn.execute(
            "SELECT * FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()


def get_active_order_id(user_id):
    with connect_db() as conn:
        row = conn.execute(
            "SELECT order_id FROM active_orders WHERE user_id = ?",
            (user_id,),
        ).fetchone()

    return row["order_id"] if row else None


def set_status(order_id, status, **fields):
    allowed_fields = {
        "payment_method",
        "receipt_file_id",
        "source_text",
        "source_file_id",
        "source_type",
        "final_file_id",
        "edit_instruction",
        "ai_mode",
    }

    updates = {"status": status}

    for key, value in fields.items():
        if key in allowed_fields:
            updates[key] = value

    columns = ", ".join(f"{key} = ?" for key in updates)
    values = list(updates.values()) + [order_id]

    with connect_db() as conn:
        conn.execute(
            f"UPDATE orders SET {columns} WHERE order_id = ?",
            values,
        )


def set_admin_order(admin_id, order_id):
    with connect_db() as conn:
        conn.execute(
            """
            INSERT INTO admin_active_orders (admin_id, order_id)
            VALUES (?, ?)
            ON CONFLICT(admin_id)
            DO UPDATE SET order_id = excluded.order_id
            """,
            (admin_id, order_id),
        )


def get_admin_order_id(admin_id):
    with connect_db() as conn:
        row = conn.execute(
            "SELECT order_id FROM admin_active_orders WHERE admin_id = ?",
            (admin_id,),
        ).fetchone()

    return row["order_id"] if row else None


def order_details(order):
    payment_method = order["payment_method"] or "Ko'rsatilmagan"

    return (
        f"🧾 Buyurtma: #{order['order_id']}\n"
        f"👤 Mijoz: {order['customer_name'] or 'Noma’lum'}\n"
        f"🆔 Telegram ID: {order['user_id']}\n"
        f"🎬 Xizmat: {order['service_name']}\n"
        + (f"📝 Montaj topshirig'i: {order['edit_instruction']}\n" if "edit_instruction" in order.keys() and order["edit_instruction"] else "")
        + f"💰 Narx: {order['price']:,} so'm\n"
        f"💳 To'lov usuli: {payment_method}\n"
        f"📌 Holat: {STATUS_NAMES.get(order['status'], order['status'])}\n"
        f"🕒 Vaqt: {order['created_at']}"
    )


# ==================================================
# TO'LOV MA'LUMOTLARI
# ==================================================

async def send_payment_details(message, order_id):
    order = get_order(order_id)

    if not order:
        await message.reply_text("Buyurtma topilmadi.")
        return

    payme_status = (
        "🟢 Payme tugmasi mavjud."
        if PAYME_PAYMENT_URL
        else "🟡 Payme havolasi hali sozlanmagan."
    )

    click_status = (
        "🔵 Click tugmasi mavjud."
        if CLICK_PAYMENT_URL
        else "🟡 Click havolasi hali sozlanmagan."
    )

    await message.reply_text(
        f"💳 TO'LOV USULINI TANLANG\n\n"
        f"🧾 Buyurtma: #{order_id}\n"
        f"🎬 Xizmat: {order['service_name']}\n"
        f"💰 To'lov summasi: {order['price']:,} so'm\n\n"
        f"Karta raqami: {PAYMENT_CARD_NUMBER}\n"
        f"Karta egasi: {PAYMENT_CARD_HOLDER}\n\n"
        f"{payme_status}\n"
        f"{click_status}\n\n"
        f"{PAYMENT_INFO}\n\n"
        "To'lovdan keyin «To'lov qildim» tugmasini bosing "
        "va chek rasmini yuboring.",
        reply_markup=payment_buttons(order_id),
    )


# ==================================================
# AI VIDEO MONTAJ YORDAMCHI FUNKSIYALARI
# ==================================================

async def send_payment_to_customer(context: ContextTypes.DEFAULT_TYPE, order_id: int):
    order = get_order(order_id)
    if not order:
        return
    await context.bot.send_message(
        chat_id=order["user_id"],
        text=(
            "💳 AI VIDEO MONTAJ UCHUN TO'LOV\n\n"
            f"🧾 Buyurtma: #{order_id}\n"
            f"💰 To'lov summasi: {order['price']:,} so'm\n\n"
            f"Karta raqami: {PAYMENT_CARD_NUMBER}\n"
            f"Karta egasi: {PAYMENT_CARD_HOLDER}\n\n"
            f"{PAYMENT_INFO}\n\n"
            "To'lovdan keyin pastdagi tugmani bosing va chek rasmini yuboring."
        ),
        reply_markup=payment_buttons(order_id),
    )


def _time_to_seconds(value):
    parts = [int(p) for p in value.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return int(value)


def _escape_filter_path(path):
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _run_ffmpeg_edit(input_path: Path, output_path: Path, instruction: str, workdir: Path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError as exc:
            raise RuntimeError(
                "FFmpeg topilmadi. requirements.txt fayliga imageio-ffmpeg qo'shing."
            ) from exc

    lower = instruction.lower()
    args = [ffmpeg, "-y"]
    start, duration = None, None
    match = re.search(
        r"(\d{1,2}:\d{2}(?::\d{2})?)\s*(?:-|–|dan)\s*(\d{1,2}:\d{2}(?::\d{2})?)",
        lower,
    )
    if match:
        start = _time_to_seconds(match.group(1))
        end = _time_to_seconds(match.group(2))
        if end > start:
            duration = end - start
    else:
        match = re.search(r"(\d{1,4})\s*(?:sekund|soniya|seconds?|sec)\b", lower)
        if match and any(w in lower for w in ("kes", "qisqart", "short", "sekund", "soniya")):
            duration = int(match.group(1))
    if start is not None:
        args += ["-ss", str(start)]
    args += ["-i", str(input_path)]
    if duration and duration > 0:
        args += ["-t", str(duration)]

    vf = []
    if any(w in lower for w in ("reels", "reel", "9:16", "vertikal", "instagram format")):
        vf.append("scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,setsar=1")
    if any(w in lower for w in ("qora oq", "oq-qora", "black and white", "black-white")):
        vf.append("hue=s=0")
    if any(w in lower for w in ("trend", "trend montaj")):
        vf.append("eq=saturation=1.20:contrast=1.05")
        vf.append("fade=t=in:st=0:d=0.4")
    elif "fade" in lower or "o'tish" in lower or "otish" in lower:
        vf.append("fade=t=in:st=0:d=0.5")

    subtitle_match = re.search(r"(?:subtitr|subtitle|yozuv)\\s*:\\s*(.+)", instruction, re.IGNORECASE)
    if subtitle_match:
        subtitle_text = subtitle_match.group(1).strip()[:300].replace("-->", "→")
        srt_path = workdir / "subtitle.srt"
        srt_path.write_text(
            "1\\n00:00:00,000 --> 00:59:59,000\\n" + subtitle_text + "\\n",
            encoding="utf-8",
        )
        vf.append("subtitles='" + _escape_filter_path(srt_path) + "'")
    elif any(w in lower for w in ("avtomatik subtitr", "auto subtitle", "gaplarni subtitr", "speech subtitles", "subtitr", "subtitle")):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "Avtomatik subtitr uchun faster-whisper kerak. Replit Shell'da: pip install faster-whisper. "
                "Birinchi ishga tushishda model yuklanishi va ko'proq xotira kerak bo'lishi mumkin."
            ) from exc
        model_size = os.getenv("WHISPER_MODEL_SIZE", "tiny")
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        segments, _info = model.transcribe(str(input_path), beam_size=1)
        subtitle_lines = []
        for idx, segment in enumerate(segments, start=1):
            def fmt_srt(seconds):
                milliseconds = int((seconds % 1) * 1000)
                total = int(seconds)
                hours, remainder = divmod(total, 3600)
                minutes, seconds_int = divmod(remainder, 60)
                return f"{hours:02}:{minutes:02}:{seconds_int:02},{milliseconds:03}"
            subtitle_lines.append(
                f"{idx}\\n{fmt_srt(segment.start)} --> {fmt_srt(segment.end)}\\n"
                f"{segment.text.strip()}\\n"
            )
        if not subtitle_lines:
            raise RuntimeError("Videoda subtitr yaratish uchun nutq aniqlanmadi.")
        srt_path = workdir / "subtitle.srt"
        srt_path.write_text("\\n".join(subtitle_lines), encoding="utf-8")
        vf.append("subtitles='" + _escape_filter_path(srt_path) + "'")

    default_music = os.getenv("DEFAULT_MUSIC_PATH", "").strip()
    if not default_music:
        for candidate in (Path("music.mp3"), Path("assets") / "music.mp3"):
            if candidate.exists():
                default_music = str(candidate)
                break
    wants_music = any(w in lower for w in ("musiqa", "music", "qo'shiq", "qoshiq"))
    music_path = Path(default_music) if default_music else None
    music_added = bool(wants_music and music_path and music_path.exists())

    if vf:
        args += ["-vf", ",".join(vf)]

    if music_added:
        # music.mp3 loyihaga oldindan joylangan bo'lishi kerak.
        args += ["-stream_loop", "-1", "-i", str(music_path),
                 "-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    else:
        args += ["-map", "0:v:0", "-map", "0:a?"]

    args += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
             "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output_path)]
    result = subprocess.run(args, capture_output=True, text=True, timeout=240)
    if result.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
        detail = (result.stderr or result.stdout or "Noma'lum FFmpeg xatosi")[-1200:]
        if wants_music and not music_added:
            raise RuntimeError(
                "Musiqa qo'shish uchun litsenziyali music.mp3 faylini Replit loyihasiga qo'shing "
                "yoki DEFAULT_MUSIC_PATH secretini sozlang. " + detail
            )
        raise RuntimeError("FFmpeg montajni bajara olmadi: " + detail)
    return output_path, music_added, wants_music


async def run_ai_edit(context: ContextTypes.DEFAULT_TYPE, order_id: int):
    order = get_order(order_id)
    if not order or order["status"] != "ai_processing":
        return
    if not order["source_file_id"]:
        set_status(order_id, "ai_failed")
        await context.bot.send_message(order["user_id"], "❌ Buyurtmada video topilmadi.")
        return

    status_message = await context.bot.send_message(
        order["user_id"], f"⏳ Buyurtma #{order_id}: AI montaj boshlandi. Iltimos, kuting."
    )
    try:
        with tempfile.TemporaryDirectory(prefix="akosh_ai_") as tmp:
            workdir = Path(tmp)
            input_path = workdir / "input_video"
            output_path = workdir / "akosh_ready.mp4"
            telegram_file = await context.bot.get_file(order["source_file_id"])
            await telegram_file.download_to_drive(custom_path=str(input_path))
            if input_path.stat().st_size > 45 * 1024 * 1024:
                raise RuntimeError("Video 45 MB dan katta. Kichikroq video yuboring.")
            output_path, music_added, wants_music = await asyncio.to_thread(
                _run_ffmpeg_edit, input_path, output_path,
                order["edit_instruction"] or "", workdir
            )
            caption = f"🎉 AI montaj tayyor!\n🧾 Buyurtma: #{order_id}"
            if wants_music and not music_added:
                caption += "\n⚠️ Musiqa fayli sozlanmagani uchun musiqa qo'shilmadi."
            with output_path.open("rb") as video_file:
                await context.bot.send_video(
                    chat_id=order["user_id"], video=video_file,
                    caption=caption, supports_streaming=True,
                )
        set_status(order_id, "completed")
        await context.bot.send_message(ADMIN_ID, f"✅ AI montaj yakunlandi. Buyurtma #{order_id}")
    except Exception as exc:
        logger.exception("AI montaj xatosi, buyurtma #%s", order_id)
        set_status(order_id, "ai_failed")
        await context.bot.send_message(
            order["user_id"],
            f"❌ Buyurtma #{order_id} montajida xatolik yuz berdi. Admin tekshiradi va siz bilan bog'lanadi.",
        )
        await context.bot.send_message(
            ADMIN_ID, f"❌ AI montaj xatosi, buyurtma #{order_id}:\n{str(exc)[:2500]}"
        )
    finally:
        try:
            await status_message.delete()
        except Exception:
            pass


# ==================================================
# /START VA MENYU
# ==================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    await update.effective_message.reply_text(
        f"Assalomu alaykum, {user.first_name}!\n\n"
        "🎬 AKOSH VIDEO EDIT botiga xush kelibsiz!\n"
        "Video montajga buyurtma berish uchun menyudan tanlang.",
        reply_markup=MAIN_MENU,
    )


async def show_services(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🎬 Kerakli xizmatni tanlang:",
        reply_markup=SERVICE_MENU,
    )


async def select_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    service = message.text

    if service not in SERVICES:
        return

    service_name, price = SERVICES[service]

    order_id = create_order(
        user.id,
        user.full_name,
        service_name,
        price,
    )

    await message.reply_text(
        f"✅ Buyurtma yaratildi!\n\n"
        f"🧾 Buyurtma: #{order_id}\n"
        f"🎬 Xizmat: {service_name}\n"
        f"💰 Narx: {price:,} so'm",
        reply_markup=MAIN_MENU,
    )

    await send_payment_details(message, order_id)


async def payment_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    order_id = get_active_order_id(user.id)
    order = get_order(order_id) if order_id else None

    if not order:
        await update.effective_message.reply_text(
            "Avval 🎬 Video yasash bo'limidan xizmat tanlang.",
            reply_markup=MAIN_MENU,
        )
        return

    if order["status"] not in ("awaiting_payment", "awaiting_receipt"):
        await update.effective_message.reply_text(
            "Bu buyurtma hozir to'lov kutmayapti.",
            reply_markup=MAIN_MENU,
        )
        return

    await send_payment_details(update.effective_message, order_id)


# ==================================================
# CHEK RASMINI QABUL QILISH
# ==================================================

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user

    if not message or not user or not message.photo:
        return

    order_id = get_active_order_id(user.id)
    order = get_order(order_id) if order_id else None

    # Birinchi navbatda mijozning chek kutilayotgan buyurtmasini tekshiramiz.
    if order and order["status"] == "awaiting_receipt":
        receipt_id = message.photo[-1].file_id

        try:
            await context.bot.send_photo(
                chat_id=ADMIN_ID,
                photo=receipt_id,
                caption=(
                    "🧾 YANGI TO'LOV CHEKI\n\n"
                    + order_details(order)
                    + "\n\nChekni tekshirib, tugmalardan birini tanlang."
                ),
                reply_markup=receipt_buttons(order_id),
            )

            set_status(
                order_id,
                "awaiting_review",
                receipt_file_id=receipt_id,
            )

            await message.reply_text(
                "✅ Chekingiz adminga yuborildi.\n"
                "Admin tekshirganidan keyin buyurtma davom etadi.",
                reply_markup=MAIN_MENU,
            )
        except Exception:
            logger.exception("Chekni yuborishda xatolik")
            await message.reply_text(
                "❌ Chek adminga yuborilmadi. Admin ID va bot sozlamalarini tekshiring."
            )
        return

    if user.id == ADMIN_ID:
        await message.reply_text(
            "Admin rejimi. Buyurtmalarni ko'rish uchun /adminorders yuboring."
        )
        return

    await message.reply_text(
        "Hozir chek kutilayotgan buyurtmangiz yo'q.\n"
        "Avval xizmat tanlang va «To'lov qildim» tugmasini bosing.",
        reply_markup=MAIN_MENU,
    )


# ==================================================
# VIDEO YOKI VIDEO HAVOLASI
# ==================================================

async def media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user

    if not message or not user:
        return

    # Mijozdan manba video kutilayotgan bo'lsa, avval shu holatni ishlaymiz.
    order_id = get_active_order_id(user.id)
    order = get_order(order_id) if order_id else None

    if order and order["status"] == "ai_waiting_video":
        if message.video:
            file_id = message.video.file_id
            source_type = "video"
        elif message.document and message.document.mime_type and message.document.mime_type.startswith("video/"):
            file_id = message.document.file_id
            source_type = "document"
        else:
            await message.reply_text("Iltimos, video yuboring (video yoki video fayl).")
            return
        set_status(order_id, "ai_waiting_instruction", source_file_id=file_id, source_type=source_type)
        await message.reply_text(
            "✅ Video qabul qilindi! Endi qanday montaj kerakligini matn bilan yozing.\n\n"
            "Masalan: Reels format, kesish 00:05-00:20, qora oq effekt, subtitr: Salom!",
        )
        return

    if order and order["status"] == "awaiting_source":
        if message.video:
            file_id = message.video.file_id
            source_type = "video"
        elif message.document and message.document.mime_type:
            if not message.document.mime_type.startswith("video/"):
                await message.reply_text("Iltimos, video fayl yuboring.")
                return

            file_id = message.document.file_id
            source_type = "document"
        else:
            await message.reply_text("Iltimos, video fayl yuboring.")
            return

        try:
            caption = "🎬 MIJOZDAN VIDEO\n\n" + order_details(order)

            if source_type == "video":
                await context.bot.send_video(
                    chat_id=ADMIN_ID,
                    video=file_id,
                    caption=caption,
                    reply_markup=final_video_button(order_id),
                )
            else:
                await context.bot.send_document(
                    chat_id=ADMIN_ID,
                    document=file_id,
                    caption=caption,
                    reply_markup=final_video_button(order_id),
                )

            set_status(
                order_id,
                "in_progress",
                source_file_id=file_id,
                source_type=source_type,
            )

            await message.reply_text(
                "✅ Videongiz adminga yuborildi. Montaj tugagach, "
                "tayyor video shu bot orqali yuboriladi.",
                reply_markup=MAIN_MENU,
            )
        except Exception:
            logger.exception("Mijoz videosini adminga yuborishda xatolik")
            await message.reply_text("❌ Videoni adminga yuborib bo'lmadi.")
        return

    # Admin tanlangan buyurtma uchun tayyor videoni yuboradi.
    if user.id == ADMIN_ID:
        selected_id = get_admin_order_id(user.id)
        selected_order = get_order(selected_id) if selected_id else None

        if not selected_order or selected_order["status"] != "in_progress":
            await message.reply_text(
                "Faol buyurtma tanlanmagan.\n"
                "/adminorders yuboring va «Tayyor videoni yuborish» tugmasini bosing."
            )
            return

        try:
            if message.video:
                final_file_id = message.video.file_id
                await context.bot.send_video(
                    chat_id=selected_order["user_id"],
                    video=final_file_id,
                    caption=f"🎉 Tayyor videongiz!\nBuyurtma: #{selected_id}",
                )
            elif message.document:
                final_file_id = message.document.file_id
                await context.bot.send_document(
                    chat_id=selected_order["user_id"],
                    document=final_file_id,
                    caption=f"🎉 Tayyor videongiz!\nBuyurtma: #{selected_id}",
                )
            else:
                await message.reply_text("Video fayl yuboring.")
                return

            set_status(
                selected_id,
                "completed",
                final_file_id=final_file_id,
            )

            await message.reply_text(
                f"✅ Tayyor video mijozga yuborildi. Buyurtma #{selected_id}"
            )
        except Exception:
            logger.exception("Tayyor videoni mijozga yuborishda xatolik")
            await message.reply_text(
                "❌ Video yuborilmadi. Mijoz botda /start yuborganini tekshiring."
            )
        return

    await message.reply_text(
        "Hozir video yuborish bosqichida emassiz. "
        "Buyurtmalarni 📦 Buyurtmalarim bo'limidan tekshiring.",
        reply_markup=MAIN_MENU,
    )


# ==================================================
# TUGMALAR
# ==================================================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not query:
        return

    await query.answer()

    user = update.effective_user
    data = query.data or ""

    # Karta orqali to'lov tafsilotlari.
    if data.startswith("card:"):
        order_id = int(data.split(":", 1)[1])
        order = get_order(order_id)

        if not order or order["user_id"] != user.id:
            await query.message.reply_text("Bu buyurtma sizga tegishli emas.")
            return

        if order["status"] not in ("awaiting_payment", "awaiting_receipt"):
            await query.message.reply_text("Bu buyurtma to'lov kutmayapti.")
            return

        set_status(order_id, order["status"], payment_method="Karta")

        await query.message.reply_text(
            f"💳 Karta orqali to'lov\n\n"
            f"Buyurtma: #{order_id}\n"
            f"Summa: {order['price']:,} so'm\n"
            f"Karta: {PAYMENT_CARD_NUMBER}\n"
            f"Karta egasi: {PAYMENT_CARD_HOLDER}\n\n"
            "To'lovdan keyin «To'lov qildim — chek yuborish» tugmasini bosing."
        )
        return

    # Mijoz to'lov qilganini bildirib, chek yuborish bosqichiga o'tadi.
    if data.startswith("paid:"):
        order_id = int(data.split(":", 1)[1])
        order = get_order(order_id)

        if not order or order["user_id"] != user.id:
            await query.message.reply_text("Bu buyurtma sizga tegishli emas.")
            return

        if order["status"] not in ("awaiting_payment", "awaiting_receipt"):
            await query.message.reply_text("Bu buyurtma uchun to'lov bosqichi faol emas.")
            return

        set_status(order_id, "awaiting_receipt")

        await query.message.reply_text(
            f"📸 Buyurtma #{order_id}\n"
            "Endi to'lov chekini rasm shaklida yuboring."
        )
        return

    # Qolgan amallar faqat admin uchun.
    if user.id != ADMIN_ID:
        await query.message.reply_text("Bu tugma faqat admin uchun.")
        return

    if data.startswith("setprice:"):
        order_id = int(data.split(":", 1)[1])
        order = get_order(order_id)
        if not order or order["status"] != "ai_awaiting_price":
            await query.message.reply_text("Bu AI buyurtma narx belgilash uchun faol emas.")
            return
        context.user_data["awaiting_ai_price_order_id"] = order_id
        set_admin_order(user.id, order_id)
        await query.message.reply_text(f"💰 AI buyurtma #{order_id} narxini so'mda yuboring. Masalan: 50000")
        return

    if data.startswith("aireject:"):
        order_id = int(data.split(":", 1)[1])
        order = get_order(order_id)
        if not order or order["status"] not in ("ai_awaiting_price", "ai_waiting_instruction"):
            await query.message.reply_text("Bu buyurtmani rad etib bo'lmaydi.")
            return
        set_status(order_id, "cancelled")
        await context.bot.send_message(order["user_id"], f"❌ AI montaj buyurtmangiz #{order_id} admin tomonidan rad etildi.")
        await query.message.reply_text(f"Buyurtma #{order_id} rad etildi.")
        return

    if data.startswith("approve:"):
        order_id = int(data.split(":", 1)[1])
        order = get_order(order_id)

        if not order or order["status"] != "awaiting_review":
            await query.message.reply_text("Buyurtma topilmadi yoki ko'rib chiqilgan.")
            return

        if order["service_name"] == "AI video montaj" or ("ai_mode" in order.keys() and order["ai_mode"]):
            set_status(order_id, "ai_processing")
            await query.message.reply_text(
                f"✅ To'lov tasdiqlandi. AI montaj buyurtma #{order_id} boshlanmoqda."
            )
            await context.bot.send_message(
                order["user_id"],
                f"✅ To'lovingiz tasdiqlandi! AI montaj buyurtma #{order_id} uchun boshlandi."
            )
            await run_ai_edit(context, order_id)
            return

        set_status(order_id, "awaiting_source")

        try:
            await context.bot.send_message(
                chat_id=order["user_id"],
                text=(
                    f"✅ Buyurtma #{order_id}: admin chekingizni qabul qildi.\n\n"
                    "Endi montaj qilinadigan video faylini yoki havolasini yuboring."
                ),
                reply_markup=MAIN_MENU,
            )
            await query.message.reply_text(
                f"✅ Buyurtma #{order_id} tasdiqlandi. Mijozdan video kutilmoqda."
            )
        except Exception:
            logger.exception("Mijozga tasdiqlash xabarini yuborishda xatolik")
            await query.message.reply_text(
                "Buyurtma tasdiqlandi, lekin mijozga xabar yuborilmadi. "
                "Mijoz botda /start yuborsin."
            )
        return

    if data.startswith("reject:"):
        order_id = int(data.split(":", 1)[1])
        order = get_order(order_id)

        if not order or order["status"] != "awaiting_review":
            await query.message.reply_text("Buyurtma topilmadi yoki ko'rib chiqilgan.")
            return

        set_status(order_id, "awaiting_receipt")

        try:
            await context.bot.send_message(
                chat_id=order["user_id"],
                text=(
                    f"❌ Buyurtma #{order_id}: chek qabul qilinmadi.\n"
                    "To'lovni tekshirib, to'g'ri chekni qayta yuboring."
                ),
            )
        except Exception:
            logger.exception("Rad etish xabarida xatolik")

        await query.message.reply_text(
            f"❌ Buyurtma #{order_id} rad etildi. Mijoz chekni qayta yuborishi mumkin."
        )
        return

    if data.startswith("select:"):
        order_id = int(data.split(":", 1)[1])
        order = get_order(order_id)

        if not order or order["status"] != "in_progress":
            await query.message.reply_text(
                "Bu buyurtma tayyor video yuborish uchun mavjud emas."
            )
            return

        set_admin_order(user.id, order_id)

        await query.message.reply_text(
            f"🎞️ Buyurtma #{order_id} tanlandi.\n"
            "Endi tayyor videoni shu chatga yuboring."
        )
        return


# ==================================================
# MATN, MENYU VA HAVOLALAR
# ==================================================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user

    if not message or not user or not message.text:
        return

    text = message.text.strip()

    if user.id == ADMIN_ID and context.user_data.get("awaiting_ai_price_order_id"):
        order_id = context.user_data["awaiting_ai_price_order_id"]
        if not text.isdigit() or int(text) <= 0:
            await message.reply_text("Narxni musbat raqam bilan yuboring. Masalan: 50000")
            return
        order = get_order(order_id)
        if not order or order["status"] != "ai_awaiting_price":
            context.user_data.pop("awaiting_ai_price_order_id", None)
            await message.reply_text("Bu buyurtma narx belgilash uchun faol emas.")
            return
        with connect_db() as conn:
            conn.execute("UPDATE orders SET price = ?, status = 'awaiting_payment' WHERE order_id = ?",
                         (int(text), order_id))
        context.user_data.pop("awaiting_ai_price_order_id", None)
        await message.reply_text(f"✅ Buyurtma #{order_id} narxi {int(text):,} so'm qilib belgilandi.")
        await send_payment_to_customer(context, order_id)
        return


    if text == "🤖 AI VIDEO MONTAJ":
        order_id = create_ai_order(user.id, user.full_name)

        text = (
            "💎 <b>AKOSH AI STUDIO</b>\n"
            "<i>PREMIUM VIDEO EDITING</i>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "🤖 <b>AI VIDEO MONTAJ</b>\n"
            f"🧾 <b>Buyurtma:</b> #{order_id}\n\n"
            "🎬 <b>1-QADAM — VIDEONI YUBORING</b>\n"
            "Montaj qilinadigan videongizni yuboring.\n\n"
            "✨ <b>2-QADAM — MONTAJ USLUBI</b>\n"
            "Qanday montaj kerakligini yozing:\n\n"
            "📱 Reels 9:16 format\n"
            "⚡ Trend effektlar va glow\n"
            "✍️ Chiroyli animatsion yozuv\n"
            "🔥 Stiker va emoji\n"
            "🎵 Musiqa qo‘shish\n"
            "💬 Subtitr: Salom hammaga!\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📌 <b>Misol:</b>\n"
            "<i>Reels 9:16, 00:05–00:20 qismini kesish, "
            "trend effektlar, musiqa va subtitr qo‘shish.</i>\n\n"
            "🚀 <b>AKOSH VIDEO EDIT</b>"
        )

        await message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=MAIN_MENU,
        )
        return


    if text == "🎬 Video yasash":
        await show_services(update, context)
        return

    if text == "💳 To'lov qilish":
        await payment_menu(update, context)
        return

    if text == "📦 Buyurtmalarim":
        await my_orders(update, context)
        return

    if text == "⬅️ Bosh menyu":
        await message.reply_text("Bosh menyu:", reply_markup=MAIN_MENU)
        return

    if text in SERVICES:
        await select_service(update, context)
        return

    order_id = get_active_order_id(user.id)
    order = get_order(order_id) if order_id else None

    # AI buyurtmasi: video qabul qilingach, matnli topshiriqni adminga yuboramiz.
    if order and order["status"] == "ai_waiting_instruction":
        if len(text) < 3:
            await message.reply_text("Montaj topshirig'ini biroz batafsilroq yozing.")
            return
        set_status(order_id, "ai_awaiting_price", edit_instruction=text)
        order = get_order(order_id)
        try:
            admin_caption = "🤖 YANGI AI MONTAJ BUYURTMASI\\n\\n" + order_details(order) + "\\n\\nVideoni ko'rib, narx belgilang."
            if order["source_type"] == "document":
                await context.bot.send_document(
                    chat_id=ADMIN_ID,
                    document=order["source_file_id"],
                    caption=admin_caption,
                    reply_markup=ai_admin_buttons(order_id),
                )
            else:
                await context.bot.send_video(
                    chat_id=ADMIN_ID,
                    video=order["source_file_id"],
                    caption=admin_caption,
                    reply_markup=ai_admin_buttons(order_id),
                )
            await message.reply_text(
                f"✅ Buyurtma #{order_id} adminga yuborildi. Admin narx belgilagach, sizga to'lov xabari keladi.",
                reply_markup=MAIN_MENU,
            )
        except Exception:
            logger.exception("AI buyurtmasini adminga yuborishda xatolik")
            await message.reply_text("❌ Buyurtma adminga yuborilmadi. Keyinroq qayta urinib ko'ring.")
        return

    # Tasdiqlangan buyurtma uchun video havolasi.
    if order and order["status"] == "awaiting_source":
        if text.startswith(("https://", "http://", "www.")):
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        "🔗 MIJOZDAN VIDEO HAVOLASI\n\n"
                        + order_details(order)
                        + f"\n\nHavola: {text}"
                    ),
                    reply_markup=final_video_button(order_id),
                )

                set_status(
                    order_id,
                    "in_progress",
                    source_text=text,
                    source_type="link",
                )

                await message.reply_text(
                    "✅ Havola adminga yuborildi. Montaj tugagach, "
                    "tayyor video shu bot orqali yuboriladi.",
                    reply_markup=MAIN_MENU,
                )
            except Exception:
                logger.exception("Havolani adminga yuborishda xatolik")
                await message.reply_text("❌ Havolani adminga yuborib bo'lmadi.")
            return

        await message.reply_text(
            "Video fayl yuboring yoki to'liq video havolasini yozing."
        )
        return

    if user.id == ADMIN_ID:
        await message.reply_text(
            "Admin menyusi:\n/adminorders — faol buyurtmalar"
        )
        return

    await message.reply_text(
        "Kerakli bo'limni pastdagi menyudan tanlang.",
        reply_markup=MAIN_MENU,
    )


# ==================================================
# MIJOZ BUYURTMALARI
# ==================================================

async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    with connect_db() as conn:
        orders = conn.execute(
            """
            SELECT * FROM orders
            WHERE user_id = ?
            ORDER BY order_id DESC
            LIMIT 10
            """,
            (user.id,),
        ).fetchall()

    if not orders:
        await update.effective_message.reply_text(
            "Sizda hozircha buyurtmalar yo'q.",
            reply_markup=MAIN_MENU,
        )
        return

    lines = ["📦 BUYURTMALARIM\n"]

    for order in orders:
        status = STATUS_NAMES.get(order["status"], order["status"])
        lines.append(
            f"🧾 #{order['order_id']} — {order['service_name']}\n"
            f"💰 {order['price']:,} so'm\n"
            f"📌 {status}\n"
        )

    await update.effective_message.reply_text(
        "\n".join(lines),
        reply_markup=MAIN_MENU,
    )


# ==================================================
# ADMIN BUYURTMALARI
# ==================================================

async def adminorders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if user.id != ADMIN_ID:
        await update.effective_message.reply_text("Bu buyruq faqat admin uchun.")
        return

    with connect_db() as conn:
        orders = conn.execute(
            """
            SELECT * FROM orders
            WHERE status IN ('ai_awaiting_price', 'ai_processing', 'ai_failed', 'awaiting_review', 'awaiting_source', 'in_progress')
            ORDER BY order_id DESC
            LIMIT 30
            """
        ).fetchall()

    if not orders:
        await update.effective_message.reply_text("Hozir faol buyurtmalar yo'q.")
        return

    for order in orders:
        keyboard = None

        if order["status"] == "in_progress":
            keyboard = final_video_button(order["order_id"])
        elif order["status"] == "ai_awaiting_price":
            keyboard = ai_admin_buttons(order["order_id"])

        await update.effective_message.reply_text(
            order_details(order),
            reply_markup=keyboard,
        )


# ==================================================
# XATOLIKLAR VA ISHGA TUSHIRISH
# ==================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Botda xatolik yuz berdi", exc_info=context.error)


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN yo'q. Replit Secrets ichiga BOT_TOKEN kiriting."
        )

    if ADMIN_ID == 0:
        raise RuntimeError(
            "ADMIN_ID noto'g'ri. Adminning raqamli Telegram ID raqamini kiriting."
        )

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("adminorders", adminorders))
    app.add_handler(CallbackQueryHandler(callback_handler))

    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))

    app.add_handler(
        MessageHandler(
            filters.VIDEO | filters.Document.VIDEO,
            media_handler,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    app.add_error_handler(error_handler)

    logger.info("AKOSH VIDEO EDIT bot ishga tushmoqda...")
    app.run_polling()


if __name__ == "__main__":
    main()
