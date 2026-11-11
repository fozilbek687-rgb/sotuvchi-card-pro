import os
import re
import hmac
import sqlite3
import logging
from io import BytesIO
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, Update, ReplyKeyboardMarkup, KeyboardButton,
    ReplyKeyboardRemove, BufferedInputFile
)
from PIL import Image, ImageDraw, ImageFont, ImageOps

# ============== SETTINGS ==============
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
DB_PATH = os.getenv("DB_PATH", "bot_data.sqlite3")
FREE_LIMIT = 3

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sotuvchi-card")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing")
if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET environment variable is missing")
if not RENDER_EXTERNAL_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL environment variable is missing")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ============== DATABASE ==============
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                cards_used INTEGER NOT NULL DEFAULT 0,
                bonus_cards INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

def ensure_user(user_id, username="", full_name=""):
    with db_connect() as conn:
        conn.execute("""
            INSERT INTO users (user_id, username, full_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username, full_name=excluded.full_name
        """, (user_id, username or "", full_name or ""))
        conn.commit()

def quota_info(user_id):
    ensure_user(user_id)
    with db_connect() as conn:
        row = conn.execute(
            "SELECT cards_used, bonus_cards FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()
    total = FREE_LIMIT + row["bonus_cards"]
    return row["cards_used"], total, max(0, total - row["cards_used"])

def add_card_usage(user_id):
    with db_connect() as conn:
        conn.execute(
            "UPDATE users SET cards_used=cards_used+1 WHERE user_id=?",
            (user_id,)
        )
        conn.commit()

def add_bonus(user_id, count):
    ensure_user(user_id)
    with db_connect() as conn:
        conn.execute(
            "UPDATE users SET bonus_cards=bonus_cards+? WHERE user_id=?",
            (count, user_id)
        )
        conn.commit()

def get_stats():
    with db_connect() as conn:
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        cards = conn.execute("SELECT COALESCE(SUM(cards_used),0) FROM users").fetchone()[0]
    return users, cards

# ============== SEO + INFOGRAPHIC ==============
def clean_text(value):
    return re.sub(r"\s+", " ", (value or "").strip())

def make_seo(product_name, details, marketplace):
    name = clean_text(product_name)
    detail_text = clean_text(details)
    title = name[:90]
    if len(title) < 90 and detail_text:
        extra = detail_text.split(",")[0].strip()
        if extra and extra.lower() not in title.lower():
            title = f"{title} — {extra}"[:100]

    facts = [x.strip(" .;:-") for x in re.split(r"[,;\n]+", detail_text) if x.strip()][:8]
    description = (
        f"{name} — kundalik foydalanish uchun mahsulot. "
        "Buyurtma berishdan oldin mahsulot tavsifi va xususiyatlarini tekshiring."
    )
    if facts:
        description += "\n\nAsosiy xususiyatlari:\n" + "\n".join(f"• {x}" for x in facts)
    description += (
        "\n\nMa’lumotlar sotuvchi kiritgan ma’lumotlar asosida tayyorlandi. "
        "Xarid qilishdan oldin o‘lcham, rang va komplektatsiyani tekshiring."
    )

    words = re.findall(r"[A-Za-zА-Яа-яЁёЎўҚқҒғҲҳ0-9]{3,}", f"{name} {detail_text}")
    stop = {"uchun", "bilan", "yoki", "the", "and", "with", "from", "market"}
    keywords = []
    for word in words:
        w = word.lower()
        if w not in stop and w not in keywords:
            keywords.append(w)
    keywords = keywords[:15]
    hashtags = " ".join(
        "#" + re.sub(r"[^A-Za-zА-Яа-яЁёЎўҚқҒғҲҳ0-9]", "", x)
        for x in keywords[:8]
    )
    return {
        "title": title,
        "description": description,
        "keywords": ", ".join(keywords),
        "hashtags": hashtags,
        "marketplace": marketplace,
    }

def load_font(size, bold=False):
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for path in font_paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()

def wrap_lines(draw, text, font, max_width):
    words = clean_text(text).split()
    lines, current = [], ""
    for word in words:
        test = f"{current} {word}".strip()
        if draw.textbbox((0, 0), test, font=font)[2] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines

def make_infographic(product_name, details, photo_bytes=None):
    width, height = 1080, 1350
    image = Image.new("RGB", (width, height), "#F4F6FA")
    draw = ImageDraw.Draw(image)

    draw.rounded_rectangle((35, 35, width-35, 190), radius=36, fill="#14213D")
    draw.text((75, 65), "SOTUVCHI CARD", font=load_font(34, True), fill="white")
    draw.text((75, 115), "MAHSULOT KARTOCHKASI", font=load_font(25), fill="#DDE7FF")

    draw.rounded_rectangle((55, 225, width-55, 920), radius=34, fill="white")
    if photo_bytes:
        try:
            source = Image.open(BytesIO(photo_bytes)).convert("RGB")
            fitted = ImageOps.contain(source, (880, 630))
            x = (width - fitted.width) // 2
            y = 255 + (630 - fitted.height) // 2
            image.paste(fitted, (x, y))
        except Exception:
            draw.text((100, 520), "RASMNI OCHIB BO‘LMADI",
                      font=load_font(30, True), fill="#9AA4B2")
    else:
        draw.text((100, 500), "MAHSULOT RASMI",
                  font=load_font(42, True), fill="#9AA4B2")
        draw.text((100, 565), "Rasm yuborilmagan",
                  font=load_font(27), fill="#9AA4B2")

    title_font = load_font(43, True)
    y = 965
    for line in wrap_lines(draw, product_name, title_font, 920)[:2]:
        draw.text((65, y), line, font=title_font, fill="#14213D")
        y += 58

    facts = [x.strip(" .;:-") for x in re.split(r"[,;\n]+", details or "") if x.strip()][:4]
    y = max(1100, y + 12)
    for fact in facts:
        if y > 1280:
            break
        draw.ellipse((75, y+10, 95, y+30), fill="#16A36A")
        for line in wrap_lines(draw, fact, load_font(25), 850)[:1]:
            draw.text((115, y), line, font=load_font(25), fill="#354052")
        y += 48

    draw.text((65, 1300), "Xususiyatlar sotuvchi kiritgan ma’lumotlardan olindi",
              font=load_font(17), fill="#687386")
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

main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🛍 Karta yaratish")],
        [KeyboardButton(text="📊 Mening limitim"), KeyboardButton(text="ℹ️ Yordam")]
    ], resize_keyboard=True
)
market_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Uzum Market"), KeyboardButton(text="Wildberries")],
        [KeyboardButton(text="Yandex Market"), KeyboardButton(text="⬅️ Bekor qilish")]
    ], resize_keyboard=True
)
photo_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="⏭ Rasmsiz yaratish")],
        [KeyboardButton(text="⬅️ Bekor qilish")]
    ], resize_keyboard=True
)

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    user = message.from_user
    ensure_user(user.id, user.username or "", user.full_name or "")
    _, total, left = quota_info(user.id)
    await message.answer(
        "Assalomu alaykum! 👋\n"
        "Sotuvchi Card PRO botiga xush kelibsiz.\n\n"
        "Bot mahsulot nomi va siz bergan xususiyatlar asosida:\n"
        "✅ SEO sarlavha\n✅ Mahsulot tavsifi\n"
        "✅ Kalit so‘zlar va hashtaglar\n✅ PNG infografika\n\n"
        f"Sinov limiti: {left}/{total} ta karta.\n"
        "Boshlash uchun «🛍 Karta yaratish» tugmasini bosing.",
        reply_markup=main_kb
    )

@dp.message(F.text == "ℹ️ Yordam")
async def help_message(message: Message):
    await message.answer(
        "Qanday ishlaydi?\n1. Marketplace tanlang.\n"
        "2. Mahsulot nomini yozing.\n3. Xususiyatlarini kiriting.\n"
        "4. Rasm yuboring yoki rasmsiz yarating.\n\n"
        "Eslatma: SEO matn shablon asosida tuziladi. "
        "Bot marketplace'ga avtomatik mahsulot joylamaydi."
    )

@dp.message(F.text == "📊 Mening limitim")
async def my_limit(message: Message):
    used, total, left = quota_info(message.from_user.id)
    await message.answer(
        f"📊 Hisobingiz:\nIshlatilgan: {used}\nJami limit: {total}\nQolgan: {left}"
    )

@dp.message(F.text == "🛍 Karta yaratish")
async def create_start(message: Message, state: FSMContext):
    _, _, left = quota_info(message.from_user.id)
    if left <= 0:
        await message.answer("Bepul sinov limitingiz tugadi. Administrator bilan bog‘laning.")
        return
    await state.set_state(CardFlow.marketplace)
    await message.answer("Marketplace tanlang:", reply_markup=market_kb)

@dp.message(CardFlow.marketplace)
async def choose_market(message: Message, state: FSMContext):
    if message.text == "⬅️ Bekor qilish":
        await state.clear()
        await message.answer("Bekor qilindi.", reply_markup=main_kb)
        return
    allowed = ["Uzum Market", "Wildberries", "Yandex Market"]
    if message.text not in allowed:
        await message.answer("Iltimos, tugmalardan birini tanlang.")
        return
    await state.update_data(marketplace=message.text)
    await state.set_state(CardFlow.product_name)
    await message.answer("Mahsulot nomini yozing:", reply_markup=ReplyKeyboardRemove())

@dp.message(CardFlow.product_name)
async def product_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if len(name) < 2 or len(name) > 150:
        await message.answer("Mahsulot nomi 2–150 belgi bo‘lsin. Qayta kiriting.")
        return
    await state.update_data(product_name=name)
    await state.set_state(CardFlow.details)
    await message.answer(
        "Mahsulot xususiyatlarini kiriting.\n"
        "Masalan: 100% paxta, qora rang, o‘lchami 42–46."
    )

@dp.message(CardFlow.details)
async def product_details(message: Message, state: FSMContext):
    details = (message.text or "").strip()
    if len(details) > 1000:
        await message.answer("Xususiyatlar 1000 belgidan oshmasin.")
        return
    await state.update_data(details=details)
    await state.set_state(CardFlow.photo)
    await message.answer(
        "Mahsulot rasmini yuboring yoki «⏭ Rasmsiz yaratish» tugmasini bosing.",
        reply_markup=photo_kb
    )

async def finish_card(message: Message, state: FSMContext, photo_bytes=None):
    data = await state.get_data()
    _, _, left = quota_info(message.from_user.id)
    if left <= 0:
        await state.clear()
        await message.answer("Bepul limitingiz tugagan.", reply_markup=main_kb)
        return
    await message.answer("⏳ Kartochka tayyorlanmoqda...")
    seo = make_seo(data["product_name"], data.get("details", ""), data["marketplace"])
    image_io = make_infographic(data["product_name"], data.get("details", ""), photo_bytes)
    await message.answer_photo(
        BufferedInputFile(image_io.getvalue(), filename="sotuvchi_card.png"),
        caption="🖼 Tayyor infografika"
    )
    await message.answer(
        f"🛍 {seo['marketplace']}\n\n"
        f"📌 SEO SARLAVHA:\n{seo['title']}\n\n"
        f"📝 TAVSIF:\n{seo['description']}\n\n"
        f"🔎 KALIT SO‘ZLAR:\n{seo['keywords'] or 'Kiritilmagan'}\n\n"
        f"#️⃣ HASHTAGLAR:\n{seo['hashtags'] or 'Kiritilmagan'}\n\n"
        "⚠️ Matn shablon asosida tayyorlandi. Joylashdan oldin tekshiring."
    )
    add_card_usage(message.from_user.id)
    _, total, left = quota_info(message.from_user.id)
    await state.clear()
    await message.answer(f"✅ Tayyor! Qolgan bepul kartalar: {left}/{total}", reply_markup=main_kb)

@dp.message(CardFlow.photo, F.photo)
async def receive_photo(message: Message, state: FSMContext, bot: Bot):
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    buffer = BytesIO()
    await bot.download_file(file.file_path, destination=buffer)
    await finish_card(message, state, buffer.getvalue())

@dp.message(CardFlow.photo, F.text == "⏭ Rasmsiz yaratish")
async def no_photo(message: Message, state: FSMContext):
    await finish_card(message, state)

@dp.message(F.text == "⬅️ Bekor qilish")
async def cancel_button(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Bekor qilindi.", reply_markup=main_kb)

@dp.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Jarayon bekor qilindi.", reply_markup=main_kb)

@dp.message(Command("stats"))
async def admin_stats(message: Message):
    if ADMIN_ID and message.from_user.id == ADMIN_ID:
        users, cards = get_stats()
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
    add_bonus(user_id, count)
    await message.answer(f"✅ {user_id} foydalanuvchisiga {count} ta karta qo‘shildi.")

@dp.message()
async def fallback(message: Message):
    await message.answer("Menyudagi tugmalardan foydalaning yoki /start yuboring.", reply_markup=main_kb)

# ============== FASTAPI WEBHOOK ==============
WEBHOOK_PATH = f"/telegram/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await bot.set_webhook(
        url=WEBHOOK_URL,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=dp.resolve_used_update_types()
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
    if not hmac.compare_digest(supplied, WEBHOOK_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        payload = await request.json()
        update = Update.model_validate(payload, context={"bot": bot})
        await dp.feed_update(bot, update)
    except Exception:
        logger.exception("Telegram update processing failed")
        raise HTTPException(status_code=500, detail="Update processing failed")
    return {"ok": True}
