
import asyncio
import os
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, FSInputFile
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from PIL import Image, ImageDraw, ImageFont

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"
FONTS_DIR = BASE_DIR / "fonts"
TEMPLATE_PATH = ASSETS_DIR / "template.png"

@dataclass
class Coords:
    x: int
    y: int
    w: int
    h: int

# Координаты под шаблон 946x2048 (можешь менять в config ниже)
TIME_BOX    = Coords(x=45,  y=42,  w=120, h=40)
BATT_BOX    = Coords(x=842, y=42,  w=70,  h=40)

OPID_BOX    = Coords(x=310, y=1005, w=330, h=44)
AMOUNT_LINE = Coords(x=170, y=1360, w=606, h=50)
WALLET_BOX  = Coords(x=170, y=1445, w=606, h=120)

def load_font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    """
    Загружает шрифт из ./fonts. Если файла нет — возвращает встроенный PIL шрифт.
    """
    try:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    except Exception:
        pass
    return ImageFont.load_default()

def clamp_int(v: str, lo: int, hi: int) -> int:
    n = int(re.sub(r"[^0-9]", "", v) or "0")
    return max(lo, min(hi, n))

def validate_time(s: str) -> str:
    s = s.strip()
    # допускаем 8:52, 08:52, 8.52 -> нормализуем
    s = s.replace(".", ":")
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if not m:
        raise ValueError("Формат времени должен быть HH:MM (например 08:52)")
    hh = int(m.group(1))
    mm = int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("Неверное время")
    return f"{hh:02d}:{mm:02d}"

def normalize_amount(s: str) -> str:
    """
    Принимаем 0.5589 или 0,5589. Возвращаем строку как есть (выровненная),
    без лишних пробелов, чтобы в финальной строке не было "дыр".
    """
    s = s.strip().replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d+)?", s):
        raise ValueError("Сумма должна быть числом, например 0.558938487")
    # убираем ведущие нули типа 000.5 -> 0.5
    if s.startswith("0") and len(s) > 1 and s[1].isdigit():
        # оставить как есть; безопасно
        pass
    return s

def wrap_mono(text: str, max_chars: int) -> str:
    """
    Простой перенос по количеству символов (удобно для моношрифта).
    """
    t = re.sub(r"\s+", "", text.strip())
    if not t:
        return ""
    lines = [t[i:i+max_chars] for i in range(0, len(t), max_chars)]
    return "\n".join(lines[:2])  # максимум 2 строки как в примере

def draw_centered(draw: ImageDraw.ImageDraw, box: Coords, text: str, font, fill):
    x = box.x + box.w//2
    y = box.y + box.h//2
    draw.text((x, y), text, font=font, fill=fill, anchor="mm")

def draw_left(draw: ImageDraw.ImageDraw, box: Coords, text: str, font, fill):
    draw.text((box.x, box.y), text, font=font, fill=fill)

class Form(StatesGroup):
    battery = State()
    time = State()
    amount = State()
    wallet = State()

router = Router()

@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(
        "Привет! Я сделаю карточку по шаблону.\n\n"
        "Введи процент зарядки (0–100):"
    )
    await state.set_state(Form.battery)

@router.message(Form.battery)
async def got_battery(m: Message, state: FSMContext):
    try:
        batt = clamp_int(m.text or "", 0, 100)
    except Exception:
        await m.answer("Напиши число 0–100.")
        return
    await state.update_data(battery=batt)
    await m.answer("Теперь введи время (HH:MM), например 08:52:")
    await state.set_state(Form.time)

@router.message(Form.time)
async def got_time(m: Message, state: FSMContext):
    try:
        t = validate_time(m.text or "")
    except Exception as e:
        await m.answer(f"{e}\nПопробуй ещё раз (например 08:52):")
        return
    await state.update_data(time=t)
    await m.answer("Введи сумму (например 0.558938487):")
    await state.set_state(Form.amount)

@router.message(Form.amount)
async def got_amount(m: Message, state: FSMContext):
    try:
        amt = normalize_amount(m.text or "")
    except Exception as e:
        await m.answer(f"{e}\nПопробуй ещё раз (например 0.558938487):")
        return
    await state.update_data(amount=amt)
    await m.answer("Введи адрес кошелька (одной строкой):")
    await state.set_state(Form.wallet)

@router.message(Form.wallet)
async def got_wallet(m: Message, state: FSMContext):
    wallet = (m.text or "").strip()
    if len(wallet) < 10:
        await m.answer("Адрес выглядит слишком коротким. Введи ещё раз:")
        return
    data = await state.get_data()
    batt: int = data["battery"]
    t: str = data["time"]
    amt: str = data["amount"]

    # Авто-ID операции: WD + 7 цифр
    op_id = f"WD{random.randint(1000000, 9999999)}"

    # Рендер
    out_path = BASE_DIR / "output.png"
    img = Image.open(TEMPLATE_PATH).convert("RGBA")
    draw = ImageDraw.Draw(img)

    # Fonts
    bold_font   = load_font(FONTS_DIR / "bold.ttf",   30)
    simple_font = load_font(FONTS_DIR / "simple.ttf", 28)
    mono_font   = load_font(FONTS_DIR / "mono.ttf",   24)

    # time + battery (bold)
    draw_left(draw, TIME_BOX, t, bold_font, (240,240,245,255))
    # battery centered inside box
    draw_centered(draw, BATT_BOX, str(batt), bold_font, (240,240,245,255))

    # op id (simple, blue)
    draw_centered(draw, OPID_BOX, f"#{op_id}", simple_font, (80,160,255,255))

    # amount line: рисуем ОДНОЙ строкой, чтобы не было разрыва между суммой и "TON ..."
    amount_line = f"{amt} TON на кошелёк:"
    draw_left(draw, AMOUNT_LINE, amount_line, simple_font, (150,150,155,255))

    # wallet (mono, wrap)
    wallet_wrapped = wrap_mono(wallet, max_chars=34)
    draw.multiline_text(
        (WALLET_BOX.x, WALLET_BOX.y),
        wallet_wrapped,
        font=mono_font,
        fill=(240,240,245,255),
        spacing=10,
        align="left"
    )

    img.convert("RGB").save(out_path, "PNG", optimize=True, compress_level=9)

    await m.answer_document(FSInputFile(out_path), caption="Готово ✅")
    await state.clear()

async def main():
    token = os.getenv("7996925136:AAEUIqyOK6_FmukWQOfV22EqbT4l3ZwqB3Q")
    if not token:
        raise RuntimeError("Не найден BOT_TOKEN в переменных окружения")

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
