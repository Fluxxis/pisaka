
import asyncio
import json
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
from aiogram.types import Message, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from PIL import Image, ImageDraw, ImageFont

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"
FONTS_DIR = BASE_DIR / "fonts"
TEMPLATE_PATH = ASSETS_DIR / "template.png"

CONFIG_JSON_PATH = BASE_DIR / "config.json"

def load_token_from_json(path: Path) -> str | None:
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        token = (data.get("BOT_TOKEN") or data.get("token") or "").strip()
        return token or None
    except Exception:
        return None

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

class Debug(StatesGroup):
    choosing = State()
    adjusting = State()

class Form(StatesGroup):
    battery = State()
    time = State()
    amount = State()
    wallet = State()

router = Router()

def coords_text() -> str:
    lines = ["<b>Текущие координаты:</b>"]
    for k in COORD_NAMES_ORDER:
        c = COORDS[k]
        lines.append(f"• <b>{COORD_LABELS[k]}</b> ({k}): x={c['x']} y={c['y']} w={c['w']} h={c['h']}")
    lines.append("\nВыбери, что настраивать:")
    return "\n".join(lines)

def debug_keyboard(selected: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    # выбор блока
    row = []
    for k in COORD_NAMES_ORDER:
        label = COORD_LABELS[k]
        if selected == k:
            label = f"✅ {label}"
        row.append(InlineKeyboardButton(text=label, callback_data=f"dbg:sel:{k}"))
    # split into 2 rows for readability
    rows.append(row[:3])
    rows.append(row[3:])

    rows.append([
    InlineKeyboardButton(text="✅ Применить (сохранить)", callback_data="dbg:apply"),
    InlineKeyboardButton(text="⬇️ Скачать значения (config.json)", callback_data="dbg:download")
])
    rows.append([InlineKeyboardButton(text="🎯 Показать overlay", callback_data="dbg:overlay")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def adjust_keyboard(k: str) -> InlineKeyboardMarkup:
    # controls: x/y/w/h +/- 1/5/10
    steps = [1, 5, 10]
    rows = [[InlineKeyboardButton(text="⬅️ x-", callback_data=f"dbg:adj:{k}:x:-{s}"),
             InlineKeyboardButton(text=f"{s}px", callback_data="noop"),
             InlineKeyboardButton(text="x+ ➡️", callback_data=f"dbg:adj:{k}:x:+{s}")]
            for s in steps]
    rows += [[InlineKeyboardButton(text="⬆️ y-", callback_data=f"dbg:adj:{k}:y:-{s}"),
              InlineKeyboardButton(text=f"{s}px", callback_data="noop"),
              InlineKeyboardButton(text="y+ ⬇️", callback_data=f"dbg:adj:{k}:y:+{s}")]
             for s in steps]
    rows.append([
        InlineKeyboardButton(text="➖ w", callback_data=f"dbg:adj:{k}:w:-10"),
        InlineKeyboardButton(text="➕ w", callback_data=f"dbg:adj:{k}:w:+10"),
        InlineKeyboardButton(text="➖ h", callback_data=f"dbg:adj:{k}:h:-10"),
        InlineKeyboardButton(text="➕ h", callback_data=f"dbg:adj:{k}:h:+10"),
    ])
    rows.append([
    InlineKeyboardButton(text="✅ Применить (сохранить)", callback_data="dbg:apply"),
    InlineKeyboardButton(text="⬅️ Назад", callback_data="dbg:back")
])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def render_debug_overlay() -> Path:
    img = Image.open(TEMPLATE_PATH).convert("RGBA")
    draw = ImageDraw.Draw(img)
    label_font = ImageFont.load_default()

    def box(name: str, c: Coords, color=(255, 0, 0, 255)):
        draw.rectangle([c.x, c.y, c.x + c.w, c.y + c.h], outline=color, width=3)
        label = f"{name}  x={c.x} y={c.y} w={c.w} h={c.h}"
        tb = draw.textbbox((0,0), label, font=label_font)
        tw, th = tb[2]-tb[0], tb[3]-tb[1]
        pad = 3
        bx0, by0 = c.x, max(0, c.y - th - 2*pad)
        draw.rectangle([bx0, by0, bx0 + tw + 2*pad, by0 + th + 2*pad], fill=(0,0,0,170))
        draw.text((bx0 + pad, by0 + pad), label, font=label_font, fill=(255,255,255,255))

    colors = {
        "TIME_BOX": (255, 80, 80, 255),
        "BATT_BOX": (255, 180, 80, 255),
        "OPID_BOX": (80, 200, 255, 255),
        "AMOUNT_LINE": (150, 255, 150, 255),
        "WALLET_BOX": (200, 120, 255, 255),
    }
    for k in COORD_NAMES_ORDER:
        box(k, get_box(k), colors.get(k, (255,0,0,255)))

    out = BASE_DIR / "debug_overlay.png"
    img.convert("RGB").save(out, "PNG", optimize=True, compress_level=9)
    return out

@router.message(F.text == "/debug")
async def debug(m: Message, state: FSMContext):
    await state.set_state(Debug.choosing)
    await state.update_data(debug_selected=None)
    await m.answer(coords_text(), reply_markup=debug_keyboard(None))

@router.callback_query(F.data == "noop")
async def noop_cb(cq: CallbackQuery):
    await cq.answer()

@router.callback_query(F.data.startswith("dbg:"), Debug.choosing)
async def debug_choose_cb(cq: CallbackQuery, state: FSMContext):
    parts = cq.data.split(":")
    action = parts[1]

    if action == "sel":
        k = parts[2]
        await state.update_data(debug_selected=k)
        await state.set_state(Debug.adjusting)
        await cq.message.edit_text(
            f"<b>Настройка:</b> {COORD_LABELS[k]} ({k})\n"
            f"x={COORDS[k]['x']} y={COORDS[k]['y']} w={COORDS[k]['w']} h={COORDS[k]['h']}\n\n"
            "Жми кнопки, чтобы двигать/менять размер.",
            reply_markup=adjust_keyboard(k)
        )
        await cq.answer()
        return

    if action == "apply":
        save_coords_to_json(CONFIG_JSON_PATH, COORDS)
        refresh_boxes()
        await cq.answer("Сохранено ✅", show_alert=False)
        # обновим текст, чтобы было видно, что актуально
        await cq.message.edit_text(coords_text(), reply_markup=debug_keyboard(None))
        return

    if action == "download":
        # сохраняем на диск и отправляем
        save_coords_to_json(CONFIG_JSON_PATH, COORDS)
        await cq.answer("Готовлю файл…")
        await cq.message.answer_document(FSInputFile(CONFIG_JSON_PATH), caption="Вот обновлённый config.json ✅")
        return

    if action == "overlay":
        if not TEMPLATE_PATH.exists():
            await cq.answer("Не найден template.png", show_alert=True)
            return
        p = render_debug_overlay()
        await cq.answer("Готово")
        await cq.message.answer_document(FSInputFile(p), caption="Overlay ✅")
        return

    await cq.answer()

@router.callback_query(F.data.startswith("dbg:"), Debug.adjusting)
async def debug_adjust_cb(cq: CallbackQuery, state: FSMContext):
    parts = cq.data.split(":")
    action = parts[1]
    if action == "apply":
        save_coords_to_json(CONFIG_JSON_PATH, COORDS)
        refresh_boxes()
        await cq.answer("Сохранено ✅")
        return

    if action == "back":
        await state.set_state(Debug.choosing)
        await state.update_data(debug_selected=None)
        await cq.message.edit_text(coords_text(), reply_markup=debug_keyboard(None))
        await cq.answer()
        return

    if action == "adj":
        k, field, delta_s = parts[2], parts[3], parts[4]
        delta = int(delta_s.replace("+",""))
        # apply
        COORDS[k][field] = int(COORDS[k][field]) + delta
        # clamp
        if field in ("w", "h"):
            COORDS[k][field] = max(1, COORDS[k][field])
        else:
            COORDS[k][field] = max(0, COORDS[k][field])
        refresh_boxes()
        # update message
        await cq.message.edit_text(
            f"<b>Настройка:</b> {COORD_LABELS[k]} ({k})\n"
            f"x={COORDS[k]['x']} y={COORDS[k]['y']} w={COORDS[k]['w']} h={COORDS[k]['h']}\n\n"
            "Жми кнопки, чтобы двигать/менять размер.\n"
            "Когда готово — нажми «⬅️ Назад», затем «⬇️ Скачать значения».",
            reply_markup=adjust_keyboard(k)
        )
        await cq.answer()
        return

    await cq.answer()


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
    draw_left(draw, get_box("TIME_BOX"), t, bold_font, (240,240,245,255))
    # battery centered inside box
    draw_centered(draw, get_box("BATT_BOX"), str(batt), bold_font, (240,240,245,255))

    # op id (simple, blue)
    draw_centered(draw, get_box("OPID_BOX"), f"#{op_id}", simple_font, (80,160,255,255))

    # amount line: рисуем ОДНОЙ строкой, чтобы не было разрыва между суммой и "TON ..."
    amount_line = f"{amt} TON на кошелёк:"
    draw_left(draw, get_box("AMOUNT_LINE"), amount_line, simple_font, (150,150,155,255))

    # wallet (mono, wrap)
    wallet_wrapped = wrap_mono(wallet, max_chars=34)
    draw.multiline_text(
        (get_box("WALLET_BOX").x, get_box("WALLET_BOX").y),
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
    token = load_token_from_json(CONFIG_JSON_PATH) or os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Не найден BOT_TOKEN: добавь его в config.json (BOT_TOKEN) или в переменные окружения")

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
