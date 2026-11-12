import asyncio
import hmac
import logging
import os
import re
import sqlite3
from contextlib import asynccontextmanager, closing
from functools import lru_cache
from io import BytesIO

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from fastapi import FastAPI, HTTPException, Request
from PIL import Image, ImageDraw, ImageFont, ImageOps

# ============== SOZLAMALAR ==============
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
DB_PATH = os.getenv("DB_PATH", "bot_data.sqlite3")
FREE_LIMIT = 3
MAX_PHOTO_BYTES = 10 * 1024 * 1024

try:
    ADMIN_ID = int((os.getenv("ADMIN_ID", "0") or "0").strip())
except ValueError:
    ADMIN_ID = 0

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sotuvchi-card")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing")
if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET environment variable is missing")
if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", WEBHOOK_SECRET):
    raise RuntimeError("WEBHOOK_SECRET faqat A-Z, a-z, 0-9, _ va - belgilaridan iborat bo'lishi kerak")
if not RENDER_EXTERNAL_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL environment variable is missing")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ============== MA'LUMOTLAR BAZASI ==============
def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    folder = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(folder, exist_ok=True)
    with closing(db_connect()) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                cards_used INTEGER NOT NULL DEFAULT 0,
                bonus_cards INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def ensure_user(user_id, username="", full_name=""):
    with closing(db_connect()) as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, full_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=COALESCE(NULLIF(excluded.username, ''), users.username),
                full_name=COALESCE(NULLIF(excluded.full_name, ''), users.full_name)
            """,
            (user_id, username or "", full_name or ""),
        )
        conn.commit()


def quota_info(user_id):
    ensure_user(user_id)
    with closing(db_connect()) as conn:
        row = conn.execute(
            "SELECT cards_used, bonus_cards FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()
    total = FREE_LIMIT + row["bonus_cards"]
    return row["cards_used"], total, max(0, total - row["cards_used"])


def try_consume_quota(user_id):
    """Limitni atomik tarzda 1 taga kamaytiradi. Limit qolmagan bo'lsa False qaytaradi."""
    ensure_user(user_id)
    with closing(db_connect()) as conn:
        cur = conn.execute(
            "UPDATE users SET cards_used = cards_used + 1 "
            "WHERE user_id=? AND cards_used < ? + bonus_cards",
            (user_id, FREE_LIMIT),
        )
        conn.commit()
        return cur.rowcount > 0


def refund_quota(user_id):
    with closing(db_connect()) as conn:
        conn.execute(
            "UPDATE users SET cards_used = MAX(cards_used - 1, 0) WHERE user_id=?",
            (user_id,),
        )
        conn.commit()


def add_bonus(user_id, count):
    ensure_user(user_id)
    with closing(db_connect()) as conn:
        conn.execute(
            "UPDATE users SET bonus_cards = bonus_cards + ? WHERE user_id=?",
            (count, user_id),
        )
        conn.commit()


def get_stats():
    with closing(db_connect()) as conn:
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        cards = conn.execute("SELECT COALESCE(SUM(cards_used), 0) FROM users").fetchone()[0]
    return users, cards


# ============== SEO MATNLAR ==============
def clean_text(value):
    return re.sub(r"\s+", " ", (value or "").strip())


def split_facts(details, limit):
    parts = re.split(r"[,;\n]+", details or "")
    facts = [clean_text(x).strip(" .;:-") for x in parts]
    return [x for x in facts if x][:limit]


def make_seo(product_name, details, marketplace):
    name = clean_text(product_name)
    detail_text = clean_text(details)

    title = name[:90]
    if len(title) < 90 and detail_text:
        extra = detail_text.split(",")[0].strip(" .;:-")
        if extra and extra.lower() not in title.lower():
            title = f"{title} — {extra}"[:100]

    facts = split_facts(details, 8)
    description = f"{name} — mahsulot haqida qisqacha ma’lumot."
    if facts:
        description += "\n\nAsosiy xususiyatlari:\n" + "\n".join(f"• {x}" for x in facts)
    description += (
        "\n\nMa’lumotlar sotuvchi kiritgan ma’lumotlar asosida tayyorlandi. "
        "Xarid qilishdan oldin o‘lcham, rang va komplektatsiyani tekshiring."
    )

    words = re.findall(r"[^\W_]{3,}", f"{name} {detail_text}")
    stop = {"uchun", "bilan", "yoki", "the", "and", "with", "from", "market"}
    keywords = []
    for word in words:
        w = word.lower()
        if w not in stop and w not in keywords:
            keywords.append(w)
    keywords = keywords[:15]
    hashtags = " ".join("#" + x for x in keywords[:8])

    return {
        "title": title,
        "description": description,
        "keywords": ", ".join(keywords),
        "hashtags": hashtags,
        "marketplace": marketplace,
    }


# ============== INFOGRAFIKA ==============
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@lru_cache(maxsize=64)
def load_font(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    lib = "LiberationSans-Bold.ttf" if bold else "LiberationSans-Regular.ttf"
    candidates = [
        os.path.join(BASE_DIR, "fonts", name),  # repozitoriyga qo'shilgan shrift (tavsiya etiladi)
        f"/usr/share/fonts/truetype/dejavu/{name}",
        f"/usr/share/fonts/truetype/liberation2/{lib}",
        f"/usr/share/fonts/truetype/liberation/{lib}",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def text_width(draw, text, font):
    return draw.textlength(text, font=font)


def fit_font(draw, text, max_width, start_size, min_size, bold=False):
    size = start_size
    while size > min_size and text_width(draw, text, load_font(size, bold)) > max_width:
        size -= 2
    return load_font(size, bold)


def wrap_lines(draw, text, font, max_width):
    words = clean_text(text).split()
    lines, current = [], ""
    for word in words:
        test = f"{current} {word}".strip()
        if text_width(draw, test, font) <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def make_infographic(product_name, details, marketplace, photo_bytes=None):
    width, height = 1080, 1350
    image = Image.new("RGB", (width, height), "#EEF2F7")
    draw = ImageDraw.Draw(image)

    # 1. Sarlavha bloki
    draw.rectangle((0, 0, width, 180), fill="#111827")
    title = clean_text(product_name).upper()[:60]
    title_font = fit_font(draw, title, width - 120, 52, 26, bold=True)
    draw.text((60, 35), title, font=title_font, fill="#FFFFFF")
    draw.text((60, 108), clean_text(marketplace).upper(), font=load_font(24), fill="#9CA3AF")

    # 2. Asosiy oq karta
    draw.rounded_rectangle(
        (40, 210, width - 40, 1310), radius=28, fill="#FFFFFF", outline="#E5E7EB", width=2
    )

    # 3. Mahsulot rasmi (o'ng tomon)
    panel = (width - 540, 300, width - 70, 1050)
    panel_w, panel_h = panel[2] - panel[0], panel[3] - panel[1]
    draw.rounded_rectangle(panel, radius=20, fill="#F3F4F6")

    placed = False
    if photo_bytes:
        try:
            with Image.open(BytesIO(photo_bytes)) as src:
                src = ImageOps.exif_transpose(src).convert("RGB")
                fitted = ImageOps.contain(src, (panel_w - 20, panel_h - 20))
            x = panel[0] + (panel_w - fitted.width) // 2
            y = panel[1] + (panel_h - fitted.height) // 2
            image.paste(fitted, (x, y))
            placed = True
        except Exception:
            logger.exception("Image processing error")
    if not placed:
        label = "MAHSULOT RASMI"
        font = load_font(28, True)
        tw = text_width(draw, label, font)
        draw.text((panel[0] + (panel_w - tw) / 2, (panel[1] + panel[3]) / 2 - 14), label, font=font, fill="#9CA3AF")

    # 4. Chap tomon: xususiyatlar
    facts = split_facts(details, 4)
    fact_font = load_font(22, True)
    start_y = 250
    for i, fact in enumerate(facts):
        box_y = start_y + i * 190
        if box_y + 160 > 1100:
            break
        draw.rounded_rectangle(
            (65, box_y, 510, box_y + 160), radius=16, fill="#F9FAFB", outline="#E5E7EB", width=1
        )
        draw.rounded_rectangle((85, box_y + 20, 125, box_y + 60), radius=10, fill="#10B981")
        num_font = load_font(22, True)
        num = str(i + 1)
        draw.text((105 - text_width(draw, num, num_font) / 2, box_y + 26), num, font=num_font, fill="#FFFFFF")

        lines = wrap_lines(draw, fact, fact_font, 350)
        if len(lines) > 4:
            lines = lines[:4]
            lines[-1] = lines[-1].rstrip(" .,") + "…"
        text_y = box_y + 20
        for line in lines:
            draw.text((140, text_y), line, font=fact_font, fill="#1F2937")
            text_y += 30

    # 5. Pastki qism
    draw.rounded_rectangle(
        (65, 1140, width - 65, 1260), radius=16, fill="#F0FDF4", outline="#BBF7D0", width=1
    )
    draw.text((90, 1165), "Muhim eslatma", font=load_font(22, True), fill="#166534")
    draw.text(
        (90, 1203),
        "Xarid qilishdan oldin barcha xususiyatlarni tekshiring",
        font=load_font(18),
        fill="#15803D",
    )

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


# ============== TELEGRAM BOT ==============
class CardFlow(StatesGroup):
    marketplace = State()
    product_name = State()
    details = State()
    photo = State()


BTN_CREATE = "🛍 Karta yaratish"
BTN_LIMIT = "📊 Mening limitim"
BTN_HELP = "ℹ️ Yordam"
BTN_CANCEL = "⬅️ Bekor qilish"
BTN_NOPHOTO = "⏭ Rasmsiz yaratish"

main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_CREATE)],
        [KeyboardButton(text=BTN_LIMIT), KeyboardButton(text=BTN_HELP)],
    ],
    resize_keyboard=True,
)
market_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Uzum Market"), KeyboardButton(text="Wildberries")],
        [KeyboardButton(text="Yandex Market"), KeyboardButton(text=BTN_CANCEL)],
    ],
    resize_keyboard=True,
)
photo_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_NOPHOTO)], [KeyboardButton(text=BTN_CANCEL)]],
    resize_keyboard=True,
)


async def send_long(message: Message, text: str, **kwargs):
    """Telegram 4096 belgi limitidan oshmaslik uchun bo'laklab yuboradi."""
    while len(text) > 4000:
        cut = text.rfind("\n", 0, 4000)
        if cut <= 0:
            cut = 4000
        await message.answer(text[:cut])
        text = text[cut:].lstrip("\n")
    await message.answer(text, **kwargs)


# --- Umumiy buyruqlar (holatdan oldin ro'yxatdan o'tadi, shuning uchun har doim ishlaydi) ---
@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    user = message.from_user
    await asyncio.to_thread(ensure_user, user.id, user.username or "", user.full_name or "")
    _, total, left = await asyncio.to_thread(quota_info, user.id)
    await message.answer(
        "Assalomu alaykum! 👋\n"
        "Sotuvchi Card PRO botiga xush kelibsiz.\n\n"
        "Bot yuborgan rasmingiz va xususiyatlaringiz asosida:\n"
        "✅ Professional infografika dizayn\n"
        "✅ SEO sarlavha va tavsif\n"
        "✅ Kalit so‘zlar va hashtaglar tayyorlab beradi.\n\n"
        f"Sinov limiti: {left}/{total} ta karta.\n"
        "Boshlash uchun «🛍 Karta yaratish» tugmasini bosing.",
        reply_markup=main_kb,
    )


@dp.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Jarayon bekor qilindi.", reply_markup=main_kb)


@dp.message(F.text == BTN_CANCEL)
async def cancel_button(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Bekor qilindi.", reply_markup=main_kb)


@dp.message(F.text == BTN_HELP)
async def help_message(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Qanday ishlaydi?\n"
        "1. Marketplace tanlang.\n"
        "2. Mahsulot nomini yozing.\n"
        "3. Xususiyatlarini vergul bilan kiriting.\n"
        "4. Mahsulot rasmini yuboring — bot uni infografikaga joylaydi.",
        reply_markup=main_kb,
    )


@dp.message(F.text == BTN_LIMIT)
async def my_limit(message: Message, state: FSMContext):
    await state.clear()
    used, total, left = await asyncio.to_thread(quota_info, message.from_user.id)
    await message.answer(
        f"📊 Hisobingiz:\nIshlatilgan: {used}\nJami limit: {total}\nQolgan: {left}",
        reply_markup=main_kb,
    )


@dp.message(F.text == BTN_CREATE)
async def create_start(message: Message, state: FSMContext):
    await state.clear()
    _, _, left = await asyncio.to_thread(quota_info, message.from_user.id)
    if left <= 0:
        await message.answer(
            "Bepul sinov limitingiz tugadi. Administrator bilan bog‘laning.",
            reply_markup=main_kb,
        )
        return
    await state.set_state(CardFlow.marketplace)
    await message.answer("Marketplace tanlang:", reply_markup=market_kb)


@dp.message(Command("stats"))
async def admin_stats(message: Message):
    if ADMIN_ID and message.from_user.id == ADMIN_ID:
        users, cards = await asyncio.to_thread(get_stats)
        await message.answer(f"📊 Foydalanuvchilar: {users}\nKartalar: {cards}")
    else:
        await message.answer("Bu buyruq faqat administrator uchun.")


@dp.message(Command("addquota"))
async def admin_addquota(message: Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        await message.answer("Bu buyruq faqat administrator uchun.")
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.answer("Foydalanish: /addquota USER_ID MIQDOR")
        return
    user_id, count = int(parts[1]), int(parts[2])
    if count < 1 or count > 10000:
        await message.answer("Miqdor 1 dan 10000 gacha bo‘lsin.")
        return
    await asyncio.to_thread(add_bonus, user_id, count)
    await message.answer(f"✅ {user_id} foydalanuvchisiga {count} ta karta qo‘shildi.")


# --- Karta yaratish bosqichlari ---
@dp.message(CardFlow.marketplace)
async def choose_market(message: Message, state: FSMContext):
    allowed = ["Uzum Market", "Wildberries", "Yandex Market"]
    if message.text not in allowed:
        await message.answer("Iltimos, tugmalardan birini tanlang.")
        return
    await state.update_data(marketplace=message.text)
    await state.set_state(CardFlow.product_name)
    await message.answer(
        "Mahsulot nomini yozing (masalan: MINOXIDIL):",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(CardFlow.product_name)
async def product_name(message: Message, state: FSMContext):
    name = clean_text(message.text)
    if len(name) < 2 or len(name) > 150:
        await message.answer("Mahsulot nomi matn ko‘rinishida va 2–150 belgi bo‘lsin. Qayta kiriting.")
        return
    await state.update_data(product_name=name)
    await state.set_state(CardFlow.details)
    await message.answer(
        "Mahsulot xususiyatlarini kiriting (vergul bilan ajrating).\n"
        "Masalan: Soch o'sishini rag'batlantiradi, 3 oy ichida natija, 100% original, AQShda ishlab chiqarilgan."
    )


@dp.message(CardFlow.details)
async def product_details(message: Message, state: FSMContext):
    details = (message.text or "").strip()
    if not details:
        await message.answer("Iltimos, xususiyatlarni matn ko‘rinishida yozing.")
        return
    if len(details) > 1000:
        await message.answer("Xususiyatlar 1000 belgidan oshmasin.")
        return
    await state.update_data(details=details)
    await state.set_state(CardFlow.photo)
    await message.answer(
        "Mahsulot rasmini yuboring yoki «⏭ Rasmsiz yaratish» tugmasini bosing.",
        reply_markup=photo_kb,
    )


async def finish_card(message: Message, state: FSMContext, photo_bytes=None):
    user_id = message.from_user.id
    data = await state.get_data()
    if not data.get("product_name") or not data.get("marketplace"):
        await state.clear()
        await message.answer(
            "Ma’lumotlar topilmadi (bot qayta ishga tushgan bo‘lishi mumkin). Qaytadan boshlang.",
            reply_markup=main_kb,
        )
        return

    if not await asyncio.to_thread(try_consume_quota, user_id):
        await state.clear()
        await message.answer("Bepul limitingiz tugagan.", reply_markup=main_kb)
        return

    await message.answer("⏳ Infografika va SEO matnlar tayyorlanmoqda...", reply_markup=ReplyKeyboardRemove())
    try:
        details = data.get("details", "")
        seo = make_seo(data["product_name"], details, data["marketplace"])
        image_io = await asyncio.to_thread(
            make_infographic, data["product_name"], details, data["marketplace"], photo_bytes
        )
        await message.answer_photo(
            BufferedInputFile(image_io.getvalue(), filename="sotuvchi_card.png"),
            caption="🖼 Tayyor infografika",
        )
        await send_long(
            message,
            f"🛍 {seo['marketplace']}\n\n"
            f"📌 SEO SARLAVHA:\n{seo['title']}\n\n"
            f"📝 TAVSIF:\n{seo['description']}\n\n"
            f"🔎 KALIT SO‘ZLAR:\n{seo['keywords'] or 'Kiritilmagan'}\n\n"
            f"#️⃣ HASHTAGLAR:\n{seo['hashtags'] or 'Kiritilmagan'}\n\n"
            "⚠️ Joylashtirishdan oldin barchasini ko'zdan kechirib chiqing.",
        )
    except Exception:
        logger.exception("Card generation failed")
        await asyncio.to_thread(refund_quota, user_id)
        await state.clear()
        await message.answer(
            "Kechirasiz, xatolik yuz berdi. Limitingiz qaytarildi, qaytadan urinib ko‘ring.",
            reply_markup=main_kb,
        )
        return

    _, total, left = await asyncio.to_thread(quota_info, user_id)
    await state.clear()
    await message.answer(f"✅ Tayyor! Qolgan bepul kartalar: {left}/{total}", reply_markup=main_kb)


@dp.message(CardFlow.photo, F.photo)
async def receive_photo(message: Message, state: FSMContext, bot: Bot):
    photo = message.photo[-1]
    if photo.file_size and photo.file_size > MAX_PHOTO_BYTES:
        await message.answer("Rasm hajmi 10 MB dan oshmasligi kerak.")
        return
    try:
        file = await bot.get_file(photo.file_id)
        buffer = BytesIO()
        await bot.download_file(file.file_path, destination=buffer)
    except Exception:
        logger.exception("Photo download failed")
        await message.answer("Rasmni yuklab olib bo‘lmadi. Qayta yuboring.")
        return
    await finish_card(message, state, buffer.getvalue())


@dp.message(CardFlow.photo, F.text == BTN_NOPHOTO)
async def no_photo(message: Message, state: FSMContext):
    await finish_card(message, state)


@dp.message(CardFlow.photo)
async def photo_wrong_input(message: Message):
    await message.answer("Iltimos, rasm yuboring yoki «⏭ Rasmsiz yaratish» tugmasini bosing.")


@dp.message()
async def fallback(message: Message):
    await message.answer("Menyudagi tugmalardan foydalaning yoki /start yuboring.", reply_markup=main_kb)


# ============== FASTAPI WEBHOOK ==============
WEBHOOK_PATH = f"/telegram/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(init_db)
    await bot.set_webhook(
        url=WEBHOOK_URL,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=dp.resolve_used_update_types(),
    )
    logger.info("Telegram webhook configured.")
    yield
    await bot.session.close()


app = FastAPI(title="Sotuvchi Card PRO", lifespan=lifespan)


@app.get("/")
async def home():
    return {"status": "ok", "service": "Sotuvchi Card PRO"}


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(supplied.encode("utf-8"), WEBHOOK_SECRET.encode("utf-8")):
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        payload = await request.json()
        update = Update.model_validate(payload, context={"bot": bot})
        await dp.feed_update(bot, update)
    except Exception:
        # 500 qaytarsak Telegram shu update'ni qayta-qayta yuboradi, shuning uchun faqat log yozamiz
        logger.exception("Telegram update processing failed")
    return {"ok": True}
