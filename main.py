import asyncio
import html
import logging
import os
import random
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional, Tuple

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, BaseFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, InputMediaPhoto, KeyboardButton, Message,
    ReplyKeyboardMarkup, LinkPreviewOptions,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

# ================= КОНФИГУРАЦИЯ =================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN не задан. Укажите его в .env или окружении.")

DB_NAME = os.getenv("DB_NAME", "/app/data/cards_game.db")
LOG_PATH = os.getenv("LOG_PATH", "/app/data/bot.log")
COOLDOWN_SECONDS = 4 * 3600
INSTANT_COST = 150  # максимум (полный кулдаун)
INSTANT_MIN_COST = 5  # минимум (кулдаун почти истёк)
NICKNAME_COST = 100
DUPLICATE_CHANCE = 0.25  # п.7 — шанс дубликата
DUPLICATE_REFUND = 0.5  # 50% от стоимости

# Кристаллы и маркет
GEM_TO_COINS = 100  # 1 кристалл = 100 монет
# Цены карточек в маркете (кристаллы) по редкости
MARKET_PRICES = {
    "common": 1,
    "rare": 3,
    "epic": 8,
    "mythical": 20,
    "legendary": 50,
}
# Кристаллы за получение карточки (не дубликат)
GEM_REWARDS = {
    "mythical": 1,
    "legendary": 2,
}
# Бонус кристаллов за стрик (начисляется ОДИН раз при достижении порога)
STREAK_GEM_BONUSES = [(7, 5), (30, 20)]  # (мин. дней, кристаллы)

# п.14 — авто-удаление служебных сообщений бота в группах (в секундах)
GROUP_AUTODELETE_SECONDS = 30
STREAK_EXPIRE_SECONDS = 24 * 3600  # стрик сбрасывается, если не получал карточку 24ч

# п.12 — заглушка вместо генерации аватарки. Замените на свой file_id.
DEFAULT_AVATAR_FILE_ID = "AgACAgIAAxkBAAID12qql3EFpnb2HwTCE7Yn_Ri1TQsNAAKRIGsbfHlYSVUpxfU75O60AQADAgADeAADPQQ"

# ================= СЛОТ-МАШИНА =================
# Команда: «мряу ставка [монеты]»
# Пример: мряу ставка 50
# Логика:
#   1. Проверяем баланс >= ставка.
#   2. Списываем ставку сразу.
#   3. Случайно выбираем эмодзи (🎲 🎯 🏀 ⚽ 🎰 🎳).
#   4. Отправляем анимированный dice.
#   5. Берём реальный value из сообщения.
#   6. По value определяем множитель и начисляем выигрыш.
SLOT_SPIN_DELAY = 4  # секунд до показа результата (анимация)

# Доступные эмодзи для слота
SLOT_EMOJIS = ["🎲", "🎯", "🏀", "⚽", "🎳", "🎰"]

RARITIES = {
    "common": {"icon": "⚪️", "name": "Обычная", "weight": 50, "reward": 10},
    "rare": {"icon": "🔵", "name": "Редкая", "weight": 20, "reward": 25},
    "epic": {"icon": "🟣", "name": "Эпическая", "weight": 15, "reward": 50},
    "mythical": {"icon": "🔴", "name": "Мифическая", "weight": 10, "reward": 75},
    "legendary": {"icon": "🟡", "name": "Легендарная", "weight": 5, "reward": 100},
}

GENDERS = {
    "male": {"icon": "♂️", "name": "Мужской"},
    "female": {"icon": "♀️", "name": "Женский"},
    "other": {"icon": "⚧️", "name": "Другой"},
    "none": {"icon": "➖", "name": "Не задан"},
}

# п.2 — стрик начисляется со 2-го дня
STREAK_BONUSES = [(2, 15), (7, 20), (14, 25), (30, 30), (float("inf"), 35)]

# п.5.2 — валидация ника
NICKNAME_RE = re.compile(r"^[\w\-. ]{2,32}$", re.UNICODE)
URL_RE = re.compile(r"(https?://|t\.me/|@\w+)", re.IGNORECASE)

# Регулярка для команды слота: «мряу ставка 123»
SLOT_CMD_RE = re.compile(
    r"^мряу\s+ставка\s+(\d+)\s*$",
    re.IGNORECASE | re.UNICODE,
)

# Регулярка для перевода: «мряу перевод 123»
TRANSFER_CMD_RE = re.compile(
    r"^мряу\s+перевод\s+(\d+)\s*$",
    re.IGNORECASE | re.UNICODE,
)

# Регулярки для просмотра чужого профиля / маркета / коллекции
PROFILE_CMD_RE = re.compile(
    r"^мряу\s+профиль\s*$",
    re.IGNORECASE | re.UNICODE,
)
MARKET_CMD_RE = re.compile(
    r"^мряу\s+маркет\s*$",
    re.IGNORECASE | re.UNICODE,
)
COLLECTION_CMD_RE = re.compile(
    r"^мряу\s+(коллекция|карточки)\s*$",
    re.IGNORECASE | re.UNICODE,
)
TOP_CMD_RE = re.compile(
    r"^мряу\s+топ\s*$",
    re.IGNORECASE | re.UNICODE,
)
HELP_CMD_RE = re.compile(
    r"^мряу\s+помощь\s*$",
    re.IGNORECASE | re.UNICODE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


# ================= ХЕЛПЕРЫ =================
def esc(text) -> str:
    """п.5.3 — экранирование любого пользовательского текста."""
    return html.escape(str(text), quote=False)


def fmt_num(n) -> str:
    """123456 -> '123 456'. Разделитель — неразрывный пробел (U+00A0)."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    return f"{n:,}".replace(",", "\u00a0")


def plural(n: int, one: str, few: str, many: str) -> str:
    """
    Русское склонение: plural(1, 'день', 'дня', 'дней') -> 'день'
    Правила: 1, 21, 31 -> one; 2-4, 22-24 -> few; 0, 5-20, 25-30 -> many
    """
    n = abs(int(n))
    if n % 100 in (11, 12, 13, 14):
        return many
    last = n % 10
    if last == 1:
        return one
    if last in (2, 3, 4):
        return few
    return many


def fmt_days(n: int) -> str:
    """1 день, 2 дня, 5 дней."""
    return f"{fmt_num(n)} {plural(n, 'день', 'дня', 'дней')}"


def fmt_cards(n: int) -> str:
    """1 карточка, 2 карточки, 5 карточек."""
    return f"{fmt_num(n)} {plural(n, 'карточка', 'карточки', 'карточек')}"


def fmt_coins(n: int) -> str:
    """1 монета, 2 монеты, 5 монет."""
    return f"{fmt_num(n)} {plural(n, 'монета', 'монеты', 'монет')}"


def fmt_gems(n: int) -> str:
    """1 кристалл, 2 кристалла, 5 кристаллов."""
    return f"{fmt_num(n)} {plural(n, 'кристалл', 'кристалла', 'кристаллов')}"


def user_mention(user_id: int, nickname: str, username: Optional[str] = None) -> str:
    """п.8 — ссылка на пользователя с экранированным ником."""
    safe = esc(nickname)
    if username:
        return f'<a href="https://t.me/{esc(username)}">{safe}</a>'
    return f'<a href="tg://user?id={user_id}">{safe}</a>'


def instant_cost(remaining_seconds: int) -> int:
    """
    Динамическая стоимость мгновенного получения.
    Полный кулдаун (remaining >= COOLDOWN_SECONDS) -> INSTANT_COST.
    Осталось 0 секунд                          -> INSTANT_MIN_COST.
    Линейная интерполяция между ними.
    """
    if remaining_seconds <= 0:
        return INSTANT_MIN_COST
    if remaining_seconds >= COOLDOWN_SECONDS:
        return INSTANT_COST
    ratio = remaining_seconds / COOLDOWN_SECONDS  # 1.0 -> 0.0
    cost = INSTANT_MIN_COST + (INSTANT_COST - INSTANT_MIN_COST) * ratio
    return max(INSTANT_MIN_COST, min(INSTANT_COST, round(cost)))
    

def evaluate_dice(emoji: str, value: int) -> Tuple[float, str]:
    """
    Определяет множитель и текст результата по реальному значению Telegram Dice.
    Возвращает (множитель, текст_результата).
    Множитель 0 = полный проигрыш ставки.
    """
    # 🎲 Кубик (1–6)
    if emoji == "🎲":
        if value == 6:
            return 3.0, "🎲 Шестёрка! x3"
        if value == 5:
            return 1.8, "🎲 Пятёрка — x1.8"
        if value == 4:
            return 1.2, "🎲 Четвёрка — x1.2"
        if value == 3:
            return 1.0, "🎲 Тройка — возврат ставки"
        return 0.0, f"🎲 Выпало {value}… ставка сгорела"

    # 🎯 Дартс (1–6, 6 = яблочко)
    if emoji == "🎯":
        if value == 6:
            return 4.0, "🎯 В яблочко!!! x4"
        if value == 5:
            return 2.0, "🎯 Почти центр! x2"
        if value == 4:
            return 1.3, "🎯 Хороший бросок — x1.3"
        if value == 3:
            return 1.0, "🎯 Тройка — возврат ставки"
        return 0.0, f"🎯 Промах ({value})… ставка сгорела"

    # 🎳 Боулинг (1–6, 6 = страйк)
    if emoji == "🎳":
        if value == 6:
            return 3.5, "🎳 СТРАЙК!!! x3.5"
        if value == 5:
            return 1.8, "🎳 Почти страйк! x1.8"
        if value == 4:
            return 1.2, "🎳 Четыре кегли — x1.2"
        if value == 3:
            return 1.0, "🎳 Три кегли — возврат ставки"
        return 0.0, f"🎳 Всего {value}… ставка сгорела"

    # 🏀 Баскетбол (1–5)
    if emoji == "🏀":
        if value == 5:
            return 2.5, "🏀 Красивый данк! x2.5"
        if value == 4:
            return 1.6, "🏀 Мяч в кольце! x1.6"
        if value == 3:
            return 1.0, "🏀 Почти… возврат ставки"
        return 0.0, f"🏀 Мимо ({value})… ставка сгорела"

    # ⚽️ / ⚽ Футбол (1–5)
    if emoji in ("⚽️", "⚽"):
        if value == 5:
            return 2.5, "⚽️ Гол!!! x2.5"
        if value == 4:
            return 1.6, "⚽️ Гол! x1.6"
        if value == 3:
            return 1.0, "⚽️ Штанга… возврат ставки"
        return 0.0, f"⚽️ Мимо ({value})… ставка сгорела"

    # 🎰 Слот-машина (1–64)
    if emoji == "🎰":
        if value == 64:
            return 5.0, "🎰 ДЖЕКПОТ!!! x5"
        if value >= 40:
            return 2.0, "🎰 Хороший выигрыш — x2"
        if value >= 20:
            return 1.0, "🎰 Ничья — возврат ставки"
        return 0.0, f"🎰 Проигрыш ({value})… ставка сгорела"

    # fallback
    return 1.0, "⚠️ Что-то пошло не так… возврат ставки"


# ================= CALLBACK DATA =================
class RaritySelectCallback(CallbackData, prefix="coll_rarity"):
    rarity: str
    page: int = 0
    user_id: int = 0


class MainMenuCallback(CallbackData, prefix="coll_main"):
    user_id: int = 0


class BackToProfileCallback(CallbackData, prefix="back_to_profile"):
    user_id: int = 0


class AdminRarityCallback(CallbackData, prefix="admin_rarity"):
    card_id: int
    rarity: str


class NicknameCallback(CallbackData, prefix="nickname"):
    action: str
    user_id: int = 0


class CardActionCallback(CallbackData, prefix="card_action"):
    action: str
    user_id: int = 0


class TopCallback(CallbackData, prefix="top"):
    kind: str  # coins | cards | streak


class NickConfirmCallback(CallbackData, prefix="nickconf"):
    action: str  # apply | reset | cancel


class GenderCallback(CallbackData, prefix="gender"):
    value: str
    user_id: int = 0


class AdminCardPageCallback(CallbackData, prefix="admin_card_page"):
    page: int


class AdminCardManageCallback(CallbackData, prefix="admin_card_manage"):
    card_id: int


class AdminCardEditPhotoCallback(CallbackData, prefix="admin_card_edit_photo"):
    card_id: int


class AdminUserPageCallback(CallbackData, prefix="admin_user_page"):
    page: int


class AdminUserViewCallback(CallbackData, prefix="admin_user_view"):
    user_id: int


class AdminUserActionCallback(CallbackData, prefix="admin_user_action"):
    action: str
    user_id: int


class MarketRarityCallback(CallbackData, prefix="mkt_rarity"):
    rarity: str
    page: int = 0
    user_id: int = 0


class MarketBuyCallback(CallbackData, prefix="mkt_buy"):
    card_id: int
    user_id: int = 0


class MarketExchangeCallback(CallbackData, prefix="mkt_ex"):
    action: str  # buy_gems | menu
    amount: int = 0
    user_id: int = 0


class MarketMainCallback(CallbackData, prefix="mkt_main"):
    user_id: int = 0


# ================= БАЗА ДАННЫХ =================
@asynccontextmanager
async def get_db():
    db = await aiosqlite.connect(DB_NAME)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON;")
    await db.execute("PRAGMA journal_mode = WAL;")
    await db.execute("PRAGMA busy_timeout = 5000;")
    try:
        yield db
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.error(f"Ошибка БД: {e}")
        raise
    finally:
        await db.close()


async def init_db():
    async with get_db() as db:
        await db.execute("""
                         CREATE TABLE IF NOT EXISTS cards
                         (
                             id       INTEGER PRIMARY KEY AUTOINCREMENT,
                             name     TEXT NOT NULL,
                             rarity   TEXT NOT NULL,
                             photo_id TEXT NOT NULL
                         )
                         """)
        await db.execute("""
                         CREATE TABLE IF NOT EXISTS users
                         (
                             user_id          INTEGER PRIMARY KEY,
                             last_claim       INTEGER DEFAULT 0,
                             role             TEXT    DEFAULT 'user',
                             nickname         TEXT,
                             coins            INTEGER DEFAULT 0,
                             gems             INTEGER DEFAULT 0,
                             registration     INTEGER DEFAULT 0,
                             streak           INTEGER DEFAULT 0,
                             last_streak_date INTEGER DEFAULT 0,
                             streak_bonus     INTEGER DEFAULT 0,
                             gender           TEXT    DEFAULT 'none'
                         )
                         """)
        await db.execute("""
                         CREATE TABLE IF NOT EXISTS inventory
                         (
                             user_id    INTEGER,
                             card_id    INTEGER,
                             claim_time INTEGER DEFAULT 0,
                             amount     INTEGER DEFAULT 1,
                             PRIMARY KEY (user_id, card_id),
                             FOREIGN KEY (card_id) REFERENCES cards (id) ON DELETE CASCADE
                         )
                         """)

        for sql in (
                "CREATE INDEX IF NOT EXISTS idx_cards_rarity ON cards(rarity)",
                "CREATE INDEX IF NOT EXISTS idx_inventory_user ON inventory(user_id)",
                "CREATE INDEX IF NOT EXISTS idx_inventory_claim_time ON inventory(claim_time)",
                "CREATE INDEX IF NOT EXISTS idx_users_coins ON users(coins)",
                "CREATE INDEX IF NOT EXISTS idx_users_streak ON users(streak)",
        ):
            await db.execute(sql)

        # Безопасные миграции
        for table, column, definition in [
            ("users", "registration", "INTEGER DEFAULT 0"),
            ("users", "streak", "INTEGER DEFAULT 0"),
            ("users", "last_streak_date", "INTEGER DEFAULT 0"),
            ("users", "streak_bonus", "INTEGER DEFAULT 0"),
            ("users", "gender", "TEXT DEFAULT 'none'"),
            ("users", "gems", "INTEGER DEFAULT 0"),
            ("inventory", "claim_time", "INTEGER DEFAULT 0"),
            ("inventory", "amount", "INTEGER DEFAULT 1"),
        ]:
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            except aiosqlite.OperationalError:
                pass


async def is_admin(user_id: int) -> bool:
    try:
        async with get_db() as db:
            cur = await db.execute("SELECT role FROM users WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            return row is not None and row[0] == "admin"
    except Exception as e:
        logger.error(f"Ошибка проверки админа: {e}")
        return False


def default_nickname(username: Optional[str], full_name: Optional[str], user_id: int) -> str:
    """п.5.1 — приоритет: имя -> username -> ID."""
    if full_name and full_name.strip():
        return full_name.strip()[:32]
    if username and username.strip():
        return username.strip()[:32]
    return str(user_id)


async def get_or_create_user(user_id: int, username: Optional[str] = None, full_name: Optional[str] = None):
    async with get_db() as db:
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = await cur.fetchone()
        if not user:
            nickname = default_nickname(username, full_name, user_id)
            now = int(time.time())
            await db.execute(
                "INSERT INTO users (user_id, nickname, registration, streak, last_streak_date) "
                "VALUES (?, ?, ?, 0, 0)",
                (user_id, nickname, now),
            )
            cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            user = await cur.fetchone()
        return user


async def get_user_nickname(user_id: int) -> str:
    try:
        async with get_db() as db:
            cur = await db.execute("SELECT nickname FROM users WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            return row["nickname"] if row and row["nickname"] else f"User{user_id}"
    except Exception as e:
        logger.error(f"Ошибка получения ника: {e}")
        return f"User{user_id}"


async def get_user_row(user_id: int):
    async with get_db() as db:
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return await cur.fetchone()


def _owner_check(callback: CallbackQuery, target_user_id: int) -> bool:
    """True если нажал владелец кнопки (или target_user_id == 0 — старые кнопки)."""
    if target_user_id and callback.from_user.id != target_user_id:
        return False
    return True


# ================= FSM =================
class AddCardSG(StatesGroup):
    photo = State()
    name = State()
    rarity = State()


class EditCardSG(StatesGroup):
    card_id = State()
    new_name = State()


class EditCardPhotoSG(StatesGroup):
    card_id = State()
    photo = State()


class NicknameSG(StatesGroup):
    """Ожидание подтверждения смены ника (ник храним в FSM, не в callback_data)."""
    pending = State()


router = Router()


# ================= КЛАВИАТУРЫ =================
def get_rarity_keyboard(callback_prefix: str = "set_rarity"):
    b = InlineKeyboardBuilder()
    for key, val in RARITIES.items():
        b.button(text=val["name"], callback_data=f"{callback_prefix}:{key}")
    b.button(text="❌ Отмена", callback_data="cancel_add_card")
    b.adjust(2)
    return b.as_markup()


def get_admin_main_kb():
    b = InlineKeyboardBuilder()
    b.button(text="➕ Добавить карточку", callback_data="admin_add_card")
    b.button(text="📜 Список карточек", callback_data=AdminCardPageCallback(page=0).pack())
    b.button(text="👥 Список пользователей", callback_data=AdminUserPageCallback(page=0).pack())
    b.adjust(1)
    return b.as_markup()


def get_profile_kb(owner_id: int):
    b = InlineKeyboardBuilder()
    b.button(text="🀄️ Мои карточки", callback_data=MainMenuCallback(user_id=owner_id).pack())
    b.button(text="⚧ Выбрать пол", callback_data=GenderCallback(value="menu", user_id=owner_id).pack())
    b.button(text=f"✏️ Сменить ник ({NICKNAME_COST} 🪙)",
             callback_data=NicknameCallback(action="change", user_id=owner_id).pack())
    b.adjust(1)
    return b.as_markup()


def get_gender_kb(current: str = "none", owner_id: int = 0) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key, info in GENDERS.items():
        mark = "✅ " if key == current else ""
        b.button(
            text=f"{mark}{info['icon']} {info['name']}",
            callback_data=GenderCallback(value=key, user_id=owner_id).pack(),
        )
    b.button(text="🔙 Назад в профиль", callback_data=BackToProfileCallback(user_id=owner_id).pack())
    b.adjust(1)
    return b.as_markup()


def get_main_km():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🀄️ Получить карточку"), KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="🛒 Маркет"), KeyboardButton(text="🏆 Топ игроков")],
            [KeyboardButton(text="❓ Помощь")],
        ],
        resize_keyboard=True,
    )


def _instant_button(b: InlineKeyboardBuilder, user_id: int, label: str,
                    action: str, cost: int = INSTANT_COST):
    b.button(
        text=f"{label} ({fmt_num(cost)} 🪙)",
        callback_data=CardActionCallback(action=action, user_id=user_id).pack(),
    )


def get_card_action_keyboard(user_id: int, balance: int = 0,
                             remaining: int = COOLDOWN_SECONDS) -> InlineKeyboardMarkup:
    cost = instant_cost(remaining)
    b = InlineKeyboardBuilder()
    if balance >= cost:
        _instant_button(b, user_id, "✨ Получить сейчас", "instant", cost)
    b.adjust(1)
    return b.as_markup()


def get_after_card_keyboard(user_id: int, balance: int = 0) -> InlineKeyboardMarkup:
    cost = instant_cost(COOLDOWN_SECONDS)
    b = InlineKeyboardBuilder()
    if balance >= cost:
        _instant_button(b, user_id, "✨ Получить ещё одну", "another", cost)
    b.button(
        text="🀄️ Мои карточки",
        callback_data=CardActionCallback(action="collection", user_id=user_id).pack(),
    )
    b.adjust(1)
    return b.as_markup()


def get_top_keyboard(kind: str = "coins") -> InlineKeyboardMarkup:
    """п.6 — быстрое переключение топов."""
    b = InlineKeyboardBuilder()
    b.button(text=("✅ " if kind == "coins" else "") + "🪙 Монеты",
             callback_data=TopCallback(kind="coins").pack())
    b.button(text=("✅ " if kind == "cards" else "") + "🀄️ Карточки",
             callback_data=TopCallback(kind="cards").pack())
    b.button(text=("✅ " if kind == "streak" else "") + "🔥 Стрик",
             callback_data=TopCallback(kind="streak").pack())
    b.adjust(3)
    return b.as_markup()


# ================= ХЕЛПЕРЫ ОТОБРАЖЕНИЯ =================
async def get_user_photo(bot: Bot, user_id: int, nickname: str):
    """п.12/п.13 — фото профиля или file_id-заглушка (без генерации)."""
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if photos.total_count > 0:
            return photos.photos[0][-1].file_id
    except Exception as e:
        logger.error(f"Не удалось получить фото профиля: {e}")
    return DEFAULT_AVATAR_FILE_ID


async def render_profile(bot: Bot, user_id: int, viewer_id: Optional[int] = None):
    """viewer_id — кто смотрит профиль. Кнопки действий только для владельца."""
    async with get_db() as db:
        cur = await db.execute("""
                               SELECT u.nickname,
                                      u.coins,
                                      u.gems,
                                      u.registration,
                                      u.streak,
                                      u.streak_bonus,
                                      u.role,
                                      u.gender,
                                      COALESCE(SUM(i.amount), 0) AS cards_count
                               FROM users u
                                        LEFT JOIN inventory i ON u.user_id = i.user_id
                               WHERE u.user_id = ?
                               GROUP BY u.user_id
                               """, (user_id,))
        row = await cur.fetchone()
        cur = await db.execute("SELECT COUNT(*) FROM cards")
        total_cards = (await cur.fetchone())[0]

    if not row:
        return DEFAULT_AVATAR_FILE_ID, "❌ Пользователь не найден в базе.", None

    nickname = row["nickname"] or f"User{user_id}"
    reg_date = datetime.fromtimestamp(row["registration"] or time.time()).strftime("%d.%m.%Y")

    role = row["role"] or "user"
    role_display = "👑 Администратор" if role == "admin" else "👤 Пользователь"

    gender = row["gender"] or "none"
    g = GENDERS.get(gender, GENDERS["none"])
    gender_display = f"{g['icon']} {g['name']}"

    gems = row["gems"] if row["gems"] is not None else 0

    caption = (
        f"👤 <b>Профиль</b> • {esc(nickname)}\n\n"
        f"🆔 ID • <code>{user_id}</code>\n"
        f"🎭 Роль • <b>{role_display}</b>\n"
        f"⚧ Пол • <b>{gender_display}</b>\n"
        f"📅 Регистрация • <b>{reg_date}</b>\n\n"
        f"🀄️ Карточек • <b>{fmt_num(row['cards_count'])} из {fmt_num(total_cards)}</b>\n"
        f"🪙 Монеты • <b>{fmt_num(row['coins'])}</b>\n"
        f"💎 Кристаллы • <b>{fmt_num(gems)}</b>\n"
        f"🔥 Стрик • <b>{fmt_days(row['streak'])}</b>"
    )
    kb = get_profile_kb(user_id) if (viewer_id is None or viewer_id == user_id) else None
    return await get_user_photo(bot, user_id, nickname), caption, kb


async def render_collection(bot: Bot, user_id: int):
    keyboard, total, total_in_game = await get_collection_main_keyboard(user_id)
    nickname = await get_user_nickname(user_id)
    photo = await get_user_photo(bot, user_id, nickname)
    caption = (
        f"🀄️ <b>Карточки</b> • {esc(nickname)}\n"
        f"Всего: {fmt_num(total)} из {fmt_num(total_in_game)}"
    )
    return photo, caption, keyboard, total


async def show_or_edit_photo(message: Message, photo, caption: str, keyboard=None):
    """п.13 — устойчиво к отсутствующим file_id."""
    if message.photo:
        try:
            await message.edit_media(
                media=InputMediaPhoto(media=photo, caption=caption),
                reply_markup=keyboard,
            )
            return
        except TelegramBadRequest:
            pass
        except Exception as e:
            logger.error(f"edit_media error: {e}")
    try:
        await message.answer_photo(photo=photo, caption=caption, reply_markup=keyboard)
    except TelegramBadRequest as e:
        logger.error(f"answer_photo bad request: {e}")
        try:
            await message.answer(caption, reply_markup=keyboard)
        except Exception:
            pass


# ================= ВЫДАЧА КАРТОЧКИ =================
async def issue_card(user_id: int, check_cooldown: bool = True) -> Tuple[Optional[dict], str]:
    """п.7 — допускаем дубликаты, но с меньшим шансом."""
    try:
        async with get_db() as db:
            cur = await db.execute("SELECT COUNT(*) FROM cards")
            total_cards = (await cur.fetchone())[0]
            if total_cards == 0:
                return None, "no_cards"

            cur = await db.execute(
                "SELECT COUNT(DISTINCT card_id) FROM inventory WHERE user_id = ?", (user_id,)
            )
            owned_unique = (await cur.fetchone())[0]
            all_collected = owned_unique >= total_cards

            rarities_list = list(RARITIES.keys())
            weights = [RARITIES[r]["weight"] for r in rarities_list]

            is_duplicate = False
            card = None
            selected_rarity = None

            want_duplicate = all_collected or (random.random() < DUPLICATE_CHANCE and owned_unique > 0)

            if want_duplicate:
                cur = await db.execute(
                    """SELECT c.id, c.name, c.photo_id, c.rarity
                       FROM inventory i
                                JOIN cards c ON i.card_id = c.id
                       WHERE i.user_id = ?
                       ORDER BY RANDOM()
                       LIMIT 1""",
                    (user_id,),
                )
                card = await cur.fetchone()
                if card:
                    selected_rarity = card["rarity"]
                    is_duplicate = True

            if not card:
                for _ in range(10):
                    selected_rarity = random.choices(rarities_list, weights=weights, k=1)[0]
                    cur = await db.execute(
                        """SELECT id, name, photo_id, rarity
                           FROM cards
                           WHERE rarity = ?
                             AND id NOT IN
                                 (SELECT card_id FROM inventory WHERE user_id = ?)
                           ORDER BY RANDOM()
                           LIMIT 1""",
                        (selected_rarity, user_id),
                    )
                    card = await cur.fetchone()
                    if card:
                        break

                if not card:
                    cur = await db.execute(
                        """SELECT id, name, photo_id, rarity
                           FROM cards
                           WHERE id NOT IN (SELECT card_id FROM inventory WHERE user_id = ?)
                           ORDER BY RANDOM()
                           LIMIT 1""",
                        (user_id,),
                    )
                    card = await cur.fetchone()
                    if card:
                        selected_rarity = card["rarity"]

            if not card:
                cur = await db.execute(
                    """SELECT c.id, c.name, c.photo_id, c.rarity
                       FROM inventory i
                                JOIN cards c ON i.card_id = c.id
                       WHERE i.user_id = ?
                       ORDER BY RANDOM()
                       LIMIT 1""",
                    (user_id,),
                )
                card = await cur.fetchone()
                if not card:
                    return None, "no_cards"
                selected_rarity = card["rarity"]
                is_duplicate = True

            card_id = card["id"]
            card_name = card["name"]
            photo_id = card["photo_id"]
            base_coins = RARITIES[selected_rarity]["reward"]
            coins_earned = int(base_coins * DUPLICATE_REFUND) if is_duplicate else base_coins
            gems_earned = 0
            if not is_duplicate:
                gems_earned = GEM_REWARDS.get(selected_rarity, 0)
            now = int(time.time())

            if check_cooldown:
                await db.execute(
                    """INSERT INTO users (user_id, last_claim, coins, gems)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(user_id) DO UPDATE SET last_claim = ?,
                                                          coins      = coins + ?,
                                                          gems       = gems + ?""",
                    (user_id, now, coins_earned, gems_earned,
                     now, coins_earned, gems_earned),
                )
            else:
                await db.execute(
                    "UPDATE users SET coins = coins + ?, gems = gems + ? WHERE user_id = ?",
                    (coins_earned, gems_earned, user_id),
                )

            await db.execute(
                """INSERT INTO inventory (user_id, card_id, claim_time, amount)
                   VALUES (?, ?, ?, 1)
                   ON CONFLICT(user_id, card_id) DO UPDATE SET amount     = amount + 1,
                                                               claim_time = ?""",
                (user_id, card_id, now, now),
            )

            cur = await db.execute(
                "SELECT coins, gems FROM users WHERE user_id = ?", (user_id,)
            )
            row_bal = await cur.fetchone()
            balance = row_bal["coins"]
            gems_balance = row_bal["gems"] or 0

            return {
                "id": card_id, "name": card_name, "photo_id": photo_id,
                "rarity": selected_rarity, "coins_earned": coins_earned,
                "gems_earned": gems_earned,
                "balance": balance, "gems": gems_balance,
                "is_duplicate": is_duplicate,
            }, "success"
    except Exception as e:
        logger.error(f"Ошибка выдачи карточки: {e}")
        return None, "error"


# ================= СТРИК =================
async def check_and_update_streak(user_id: int) -> Tuple[int, int, int, int]:
    """
    Обновляет стрик по факту захода / получения карточки.
    Авто-сброс: если с last_claim прошло >= 24ч — стрик = 0.
    Кристаллы за пороги (7/30) начисляются только ОДИН раз при пересечении порога.
    Возвращает (streak, coin_bonus, balance, gem_bonus).
    """
    try:
        now_ts = int(time.time())
        now = datetime.now()
        today_start = int(datetime(now.year, now.month, now.day).timestamp())
        today_end = today_start + 86400 - 1
        yesterday_start = int((now - timedelta(days=1)).replace(hour=0, minute=0, second=0).timestamp())
        yesterday_end = yesterday_start + 86400 - 1

        async with get_db() as db:
            cur = await db.execute(
                "SELECT streak, last_streak_date, streak_bonus, coins, last_claim "
                "FROM users WHERE user_id = ?",
                (user_id,),
            )
            row = await cur.fetchone()
            if not row:
                return 0, 0, 0, 0

            streak = row["streak"] or 0
            last_date = row["last_streak_date"] or 0
            balance = row["coins"] or 0
            last_claim = row["last_claim"] or 0

            # Авто-сброс стрика, если не получал карточку 24+ часа
            if last_claim > 0 and (now_ts - last_claim) >= STREAK_EXPIRE_SECONDS:
                if streak > 0:
                    await db.execute(
                        "UPDATE users SET streak = 0, last_streak_date = 0, streak_bonus = 0 "
                        "WHERE user_id = ?",
                        (user_id,),
                    )
                return 0, 0, balance, 0

            if today_start <= last_date <= today_end:
                return streak, 0, balance, 0

            old_streak = streak
            is_consecutive = streak > 0 and yesterday_start <= last_date <= yesterday_end
            new_streak = (streak + 1) if is_consecutive else 1

            new_bonus = 0 if new_streak == 1 else next(
                b for d, b in STREAK_BONUSES if new_streak <= d
            )

            # Кристаллы только при ПЕРВОМ пересечении порога
            gem_bonus = 0
            for days, gems_amt in STREAK_GEM_BONUSES:
                if old_streak < days <= new_streak:
                    gem_bonus += gems_amt

            await db.execute(
                "UPDATE users SET streak = ?, last_streak_date = ?, "
                "streak_bonus = ?, coins = coins + ?, gems = gems + ? "
                "WHERE user_id = ?",
                (new_streak, now_ts, new_bonus, new_bonus, gem_bonus, user_id),
            )
            cur = await db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
            new_balance = (await cur.fetchone())[0]
            return new_streak, new_bonus, new_balance, gem_bonus
    except Exception as e:
        logger.error(f"Ошибка обновления стрика: {e}")
        return 0, 0, 0, 0


# ================= RATE LIMIT (п.11) =================
_rate_bucket: dict = {}
_RATE_BUCKET_MAX_KEYS = 10_000


def rate_limited(key: str, limit: int, window: float) -> bool:
    """Простой sliding-window rate limit. Периодически чистит пустые ключи."""
    now = time.monotonic()
    bucket = _rate_bucket.setdefault(key, [])
    bucket[:] = [t for t in bucket if now - t < window]
    if not bucket and key in _rate_bucket and len(bucket) == 0:
        # оставляем ключ до общей чистки
        pass
    if len(bucket) >= limit:
        return True
    bucket.append(now)
    # Ограничиваем рост словаря
    if len(_rate_bucket) > _RATE_BUCKET_MAX_KEYS:
        dead = [k for k, v in _rate_bucket.items() if not v]
        for k in dead[: len(dead) // 2 + 1]:
            _rate_bucket.pop(k, None)
    return False


# ================= АВТО-УДАЛЕНИЕ СООБЩЕНИЯ (п.14) =================
async def _try_delete(msg: Optional[Message]):
    """Тихо пытается удалить сообщение (нет прав / уже удалено — игнор)."""
    if msg is None:
        return
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass
    except Exception as e:
        logger.debug(f"try_delete error: {e}")


async def _auto_delete_pair(bot_msg: Message, user_msg: Optional[Message], delay: int):
    """
    Через delay секунд удаляет:
      1) сообщение бота
      2) исходное сообщение пользователя (если бот — админ с правом удаления)
    Если прав нет — удаляется только сообщение бота.
    """
    await asyncio.sleep(delay)
    await _try_delete(bot_msg)
    await _try_delete(user_msg)


async def reply_ephemeral(message: Message, text: str, **kwargs):
    """
    reply + авто-удаление в группах (сообщение бота + запрос пользователя).
    Используется для кулдаунов, ставок, переводов, ошибок, рейт-лимитов.
    """
    sent = await message.reply(text, **kwargs)
    if message.chat.type != "private":
        asyncio.create_task(
            _auto_delete_pair(sent, message, GROUP_AUTODELETE_SECONDS)
        )
    return sent


async def answer_ephemeral(message: Message, text: str, **kwargs):
    """answer + авто-удаление в группах (сообщение бота + запрос пользователя)."""
    sent = await message.answer(text, **kwargs)
    if message.chat.type != "private":
        asyncio.create_task(
            _auto_delete_pair(sent, message, GROUP_AUTODELETE_SECONDS)
        )
    return sent


# ================= ПОЛЬЗОВАТЕЛЬСКИЕ ХЕНДЛЕРЫ =================
@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message):
    try:
        await get_or_create_user(
            message.from_user.id, message.from_user.username, message.from_user.full_name
        )
        try:
            await message.answer_sticker(
                sticker="CAACAgIAAxkBAALL7WqWuuWDYuQk4iqY7tNu_-7zLZqyAAJengACoj5pSQH9iX-5QhicPQQ"
            )
        except Exception as e:
            logger.warning(f"sticker error: {e}")
        await message.reply(
            "👋 Привет! Отправьте команду «мряу», чтобы получить милую карточку",
            reply_markup=get_main_km(),
        )
    except Exception as e:
        logger.error(f"Ошибка в cmd_start: {e}")


@router.message(Command("help"))
@router.message(F.text == "❓ Помощь")
@router.message(F.text.regexp(HELP_CMD_RE))
async def cmd_help(message: Message):
    market_prices = "\n".join(
        f"  {v['icon']} {v['name']} — {MARKET_PRICES[k]} 💎"
        for k, v in RARITIES.items()
    )
    text = (
            "📖 <b>Помощь</b>\n\n"
            "<b>Основные команды:</b>\n"
            "/start — запуск бота\n"
            "/meow или «мряу» — получить карточку\n"
            "«мряу ставка N» — слот-машина (ставка N монет)\n"
            "«мряу перевод N» — перевод монет (в группе, реплаем на сообщение получателя)\n"
            "«мряу профиль» — свой профиль (в группе реплаем — профиль другого)\n"
            "«мряу маркет» — маркет (в группе реплаем — маркет другого)\n"
            "«мряу коллекция» / «мряу карточки» — коллекция (реплаем — чужая)\n"
            "«мряу топ» — топ игроков\n"
            "«мряу помощь» — эта справка\n"
            "/profile — профиль\n"
            "/collection — мои карточки\n"
            "/market или «🛒 Маркет» — купить недостающие карточки\n"
            "/top — топ игроков\n"
            f"/nickname [ник] — сменить ник ({NICKNAME_COST} 🪙)\n"
            "/nickname reset — сбросить ник (бесплатно)\n"
            "/gender [м/ж/др/нет] — установить пол (необязательно)\n"
            "/help — эта справка\n\n"
            "<b>Редкости карточек:</b>\n"
            + "\n".join(
        f"{v['icon']} {v['name']} — {v['reward']} 🪙"
        for v in RARITIES.values()
    )
            + "\n\n💡 Каждые 4 часа — бесплатная карточка. Можно получить мгновенно: "
              f"цена зависит от остатка таймера (от {INSTANT_MIN_COST} до {INSTANT_COST} 🪙).\n"
              "🔥 Заходите ежедневно — за стрик начисляются бонусные монеты "
              f"и кристаллы (один раз при достижении 7 дней — +5 💎, 30 дней — +20 💎).\n\n"
              "💎 <b>Кристаллы:</b>\n"
              "  • 1 мифическая карта (новая) → +1 💎\n"
              "  • 1 легендарная карта (новая) → +2 💎\n"
              f"  • Обмен: 1 💎 = {GEM_TO_COINS} 🪙 (купить кристаллы за монеты в маркете)\n\n"
              "🛒 <b>Маркет:</b> покупайте карточки, которых у вас ещё нет, за кристаллы.\n"
              f"Цены:\n{market_prices}\n\n"
              "🎰 <b>Слот-машина:</b> напишите <code>мряу ставка 50</code>. "
              "Бот случайно выбирает игру (🎲 🎯 🏀 ⚽ 🎰 🎳) и крутит анимированный эмодзи. "
              "Выигрыш зависит от того, что реально выпало!\n\n"
              "💸 <b>Перевод монет:</b> в группе ответьте на сообщение пользователя "
              "командой <code>мряу перевод 100</code>. В личных сообщениях не работает."
    )
    await message.reply(text, reply_markup=get_main_km())


@router.message(F.text == "🀄️ Получить карточку")
@router.message(F.text.lower().strip() == "мряу")
@router.message(F.text.lower().strip() == "милость")
@router.message(Command("meow"))
async def get_card_handler(message: Message):
    user_id = message.from_user.id
    now = int(time.time())

    # Повышенные лимиты для групп
    if message.chat.type != "private":
        if rate_limited(f"card:{message.chat.id}", limit=15, window=10):
            return
    if rate_limited(f"card-user:{user_id}", limit=12, window=10):
        return

    try:
        await get_or_create_user(
            user_id, message.from_user.username, message.from_user.full_name
        )
        nickname = await get_user_nickname(user_id)
        mention = user_mention(user_id, nickname, message.from_user.username)

        async with get_db() as db:
            cur = await db.execute(
                "SELECT last_claim, coins FROM users WHERE user_id = ?", (user_id,)
            )
            row = await cur.fetchone()
            last_claim = row["last_claim"] if row else 0
            balance = row["coins"] if row else 0

        time_passed = now - last_claim
        if time_passed < COOLDOWN_SECONDS:
            streak, bonus, new_balance, gem_bonus = await check_and_update_streak(user_id)
            remaining = int(COOLDOWN_SECONDS - time_passed)
            h, m = remaining // 3600, (remaining % 3600) // 60
            s = remaining % 60

            if h > 0:
                time_str = f"{h} ч {m} мин"
            elif m > 0:
                time_str = f"{m} мин"
            else:
                time_str = f"{s} сек"

            text = (
                f"🕘 <b>{mention}</b>, придётся немного подождать!\n"
                f"Следующую карточку можно будет получить через <b>{time_str}</b>"
            )
            text += _streak_text(streak, bonus, new_balance, gem_bonus)

            await reply_ephemeral(
                message,
                text,
                reply_markup=get_card_action_keyboard(user_id, balance, remaining=remaining),
            )
            return

        card, status = await issue_card(user_id, check_cooldown=True)
        if status != "success" or card is None:
            await reply_ephemeral(message, "❌ <b>Произошла ошибка. Попробуйте позже.</b>")
            return

        streak, bonus, new_balance, gem_bonus = await check_and_update_streak(user_id)

        caption = _card_caption(mention, card)
        caption += _streak_text(streak, bonus, new_balance, gem_bonus)

        try:
            await message.reply_photo(
                photo=card["photo_id"],
                caption=caption,
                reply_markup=get_after_card_keyboard(user_id, card["balance"]),
            )
        except TelegramBadRequest as e:
            logger.error(f"reply_photo bad request: {e}")
            await message.reply(caption, reply_markup=get_after_card_keyboard(user_id, card["balance"]))
    except Exception as e:
        logger.error(f"Ошибка в get_card_handler: {e}")
        await reply_ephemeral(message, "❌ <b>Произошла ошибка. Попробуйте позже.</b>")


def _streak_text(streak: int, bonus: int, new_balance: int, gem_bonus: int = 0) -> str:
    """Формирует текст про стрик для сообщения."""
    if bonus > 0 and streak == 1:
        return (
            "\n\n<blockquote>🔥 <b>Вы начали стрик!</b>\n"
            "💡 Заходите ежедневно, чтобы продлевать стрик и получать монеты</blockquote>"
        )
    if bonus > 0 and streak >= 2:
        text = (
            f"\n\n<blockquote>🔥 Стрик • <b>{fmt_days(streak)}</b>\n"
            f"🪙 Бонус • +{fmt_num(bonus)} [{fmt_num(new_balance)}]"
        )
        if gem_bonus > 0:
            text += f"\n💎 Кристаллы • +{fmt_num(gem_bonus)}"
        text += (
            "\n💡 Заходите ежедневно, чтобы продлевать стрик и получать монеты</blockquote>"
        )
        return text
    return ""


def _card_caption(mention: str, card: dict) -> str:
    r = RARITIES[card["rarity"]]
    title = "✨ Новая карточка" if not card.get("is_duplicate") else "🔁 Дубликат"
    text = (
        f"{title} • <b>{esc(card['name'])}</b>\n\n"
        f"{r['icon']} Редкость • <b>{r['name']}</b>\n"
        f"🪙 Монеты • <b>+{fmt_num(card['coins_earned'])}</b> [{fmt_num(card['balance'])}]"
    )
    gems_earned = card.get("gems_earned") or 0
    if gems_earned > 0:
        gems_bal = card.get("gems") or 0
        text += (
            f"\n💎 Кристаллы • <b>+{fmt_num(gems_earned)}</b> "
            f"[{fmt_num(gems_bal)}]"
        )
    return text


# ================= СЛОТ-МАШИНА =================
@router.message(F.text.regexp(SLOT_CMD_RE))
async def slot_machine_handler(message: Message):
    """
    Обработчик команды «мряу ставка N».
    1. Парсим сумму ставки.
    2. Проверяем баланс.
    3. Списываем ставку.
    4. Случайно выбираем эмодзи и отправляем dice.
    5. Берём реальный value из сообщения.
    6. Определяем множитель по evaluate_dice.
    7. Начисляем выигрыш.
    """
    user_id = message.from_user.id

    if rate_limited(f"slot:{user_id}", limit=6, window=15):
        await reply_ephemeral(message, "⏳ Слишком часто. Подождите немного.")
        return

    if message.chat.type != "private":
        if rate_limited(f"slot-chat:{message.chat.id}", limit=15, window=15):
            return

    text = (message.text or "").strip()
    m = SLOT_CMD_RE.match(text)
    if not m:
        return

    try:
        bet = int(m.group(1))
    except (ValueError, IndexError):
        await reply_ephemeral(
            message,
            "❌ <b>Ставка должна быть целым числом.</b>\nПример: <code>мряу ставка 50</code>",
        )
        return

    if bet <= 0:
        await reply_ephemeral(message, "❌ <b>Ставка должна быть больше нуля.</b>")
        return

    if bet > 100_000_000_000:
        await reply_ephemeral(
            message, "❌ <b>Слишком большая ставка.</b> Максимум — 100 000 000 000 🪙"
        )
        return

    try:
        await get_or_create_user(
            user_id, message.from_user.username, message.from_user.full_name
        )
        nickname = await get_user_nickname(user_id)
        mention = user_mention(user_id, nickname, message.from_user.username)

        # --- Проверка и списание ставки ---
        async with get_db() as db:
            cur = await db.execute(
                "SELECT coins FROM users WHERE user_id = ?", (user_id,)
            )
            row = await cur.fetchone()
            balance = row["coins"] if row else 0

            if balance < bet:
                await reply_ephemeral(
                    message,
                    f"⚠️ <b>Недостаточно монет.</b>\n"
                    f"Ставка: <b>{fmt_num(bet)} 🪙</b>\n"
                    f"У вас: <b>{fmt_num(balance)} 🪙</b>",
                )
                return

            cur = await db.execute(
                "UPDATE users SET coins = coins - ? "
                "WHERE user_id = ? AND coins >= ?",
                (bet, user_id, bet),
            )
            if cur.rowcount != 1:
                await reply_ephemeral(
                    message,
                    "⚠️ <b>Недостаточно монет</b> (баланс изменился). Попробуйте ещё раз.",
                )
                return

        # --- Выбираем эмодзи и крутим ---
        emoji = random.choice(SLOT_EMOJIS)
        spin_msg = await message.reply_dice(emoji=emoji)

        # Реальный результат уже есть в spin_msg.dice
        dice_value = spin_msg.dice.value if spin_msg.dice else 1

        # Ждём окончания анимации
        await asyncio.sleep(SLOT_SPIN_DELAY)

        # --- Результат по реальному value ---
        mult, result_text = evaluate_dice(emoji, dice_value)
        win_amount = int(bet * mult)

        async with get_db() as db:
            if win_amount > 0:
                await db.execute(
                    "UPDATE users SET coins = coins + ? WHERE user_id = ?",
                    (win_amount, user_id),
                )
            cur = await db.execute(
                "SELECT coins FROM users WHERE user_id = ?", (user_id,)
            )
            final_balance = (await cur.fetchone())[0]

        # Формируем итоговое сообщение
        if mult == 0:
            delta_str = f"−{fmt_num(bet)}"
            color_emoji = "📉"
        else:
            profit = win_amount - bet
            if profit > 0:
                delta_str = f"+{fmt_num(profit)}"
                color_emoji = "📈"
            elif profit == 0:
                delta_str = "±0"
                color_emoji = "➡️"
            else:
                delta_str = f"{fmt_num(profit)}"
                color_emoji = "📉"

        result_caption = (
            f"{emoji} <b>Результат</b>\n\n"
            f"{result_text}\n\n"
            f"💰 Ставка • <b>{fmt_num(bet)} 🪙</b>\n"
            f"🎁 Выигрыш • <b>{fmt_num(win_amount)} 🪙</b>\n"
            f"{color_emoji} Итог • <b>{delta_str} 🪙</b>\n"
            f"🪙 Баланс • <b>{fmt_num(final_balance)}</b>"
        )

        try:
            result_msg = await spin_msg.reply(result_caption)
            if message.chat.type != "private":
                # Удаляем результат + исходный запрос пользователя.
                # Dice (spin_msg) тоже почистим для аккуратности.
                async def _cleanup_slot():
                    await asyncio.sleep(GROUP_AUTODELETE_SECONDS)
                    await _try_delete(result_msg)
                    await _try_delete(spin_msg)
                    await _try_delete(message)
                asyncio.create_task(_cleanup_slot())
        except TelegramBadRequest:
            await reply_ephemeral(message, result_caption)

    except Exception as e:
        logger.error(f"Ошибка в slot_machine_handler: {e}")
        # Пытаемся вернуть ставку при ошибке
        try:
            async with get_db() as db:
                await db.execute(
                    "UPDATE users SET coins = coins + ? WHERE user_id = ?",
                    (bet, user_id),
                )
        except Exception:
            pass
        await reply_ephemeral(message, "❌ <b>Произошла ошибка в слоте. Попробуйте позже.</b>")


# ================= ПЕРЕВОД МОНЕТ =================
@router.message(F.text.regexp(TRANSFER_CMD_RE))
async def transfer_coins_handler(message: Message):
    """
    Команда «мряу перевод N».
    Работает только в группах и только реплаем на сообщение получателя.
    В ЛС игнорируется (тихо).
    """
    # Только группы / супергруппы
    if message.chat.type == "private":
        return

    user_id = message.from_user.id

    if rate_limited(f"transfer:{user_id}", limit=8, window=20):
        await reply_ephemeral(message, "⏳ Слишком часто. Подождите немного.")
        return

    if rate_limited(f"transfer-chat:{message.chat.id}", limit=20, window=20):
        return

    # Должен быть реплай на сообщение пользователя
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await reply_ephemeral(
            message,
            "⚠️ <b>Ответьте на сообщение пользователя</b>, которому хотите перевести монеты.\n"
            "Пример: реплай + <code>мряу перевод 50</code>",
        )
        return

    target = message.reply_to_message.from_user

    if target.is_bot:
        await reply_ephemeral(message, "❌ Нельзя переводить монеты боту.")
        return

    if target.id == user_id:
        await reply_ephemeral(message, "❌ Нельзя перевести монеты самому себе.")
        return

    text = (message.text or "").strip()
    m = TRANSFER_CMD_RE.match(text)
    if not m:
        return

    try:
        amount = int(m.group(1))
    except (ValueError, IndexError):
        await reply_ephemeral(
            message,
            "❌ <b>Сумма должна быть целым числом.</b>\nПример: <code>мряу перевод 50</code>",
        )
        return

    if amount <= 0:
        await reply_ephemeral(message, "❌ <b>Сумма перевода должна быть больше нуля.</b>")
        return

    if amount > 100_000_000_000:
        await reply_ephemeral(
            message, "❌ <b>Слишком большая сумма.</b> Максимум — 100 000 000 000 🪙"
        )
        return

    try:
        # Создаём / обновляем обоих пользователей
        await get_or_create_user(
            user_id, message.from_user.username, message.from_user.full_name
        )
        await get_or_create_user(
            target.id, target.username, target.full_name
        )

        sender_nick = await get_user_nickname(user_id)
        receiver_nick = await get_user_nickname(target.id)
        sender_mention = user_mention(user_id, sender_nick, message.from_user.username)
        receiver_mention = user_mention(target.id, receiver_nick, target.username)

        async with get_db() as db:
            cur = await db.execute(
                "SELECT coins FROM users WHERE user_id = ?", (user_id,)
            )
            row = await cur.fetchone()
            balance = row["coins"] if row else 0

            if balance < amount:
                await reply_ephemeral(
                    message,
                    f"⚠️ <b>Недостаточно монет.</b>\n"
                    f"Нужно: <b>{fmt_num(amount)} 🪙</b>\n"
                    f"У вас: <b>{fmt_num(balance)} 🪙</b>",
                )
                return

            # Атомарный перевод: списываем только при достаточном балансе
            cur = await db.execute(
                "UPDATE users SET coins = coins - ? "
                "WHERE user_id = ? AND coins >= ?",
                (amount, user_id, amount),
            )
            if cur.rowcount != 1:
                await reply_ephemeral(
                    message,
                    "⚠️ <b>Недостаточно монет</b> (баланс изменился). Попробуйте ещё раз.",
                )
                return
            await db.execute(
                "UPDATE users SET coins = coins + ? WHERE user_id = ?",
                (amount, target.id),
            )

            cur = await db.execute(
                "SELECT coins FROM users WHERE user_id = ?", (user_id,)
            )
            new_sender_balance = (await cur.fetchone())[0]

            cur = await db.execute(
                "SELECT coins FROM users WHERE user_id = ?", (target.id,)
            )
            new_receiver_balance = (await cur.fetchone())[0]

        await reply_ephemeral(
            message,
            f"💸 <b>Перевод выполнен</b>\n\n"
            f"От: {sender_mention}\n"
            f"Кому: {receiver_mention}\n"
            f"Сумма: <b>{fmt_num(amount)} 🪙</b>\n\n"
            f"🪙 Баланс отправителя: <b>{fmt_num(new_sender_balance)}</b>\n"
            f"🪙 Баланс получателя: <b>{fmt_num(new_receiver_balance)}</b>",
        )

    except Exception as e:
        logger.error(f"Ошибка в transfer_coins_handler: {e}")
        await reply_ephemeral(
            message, "❌ <b>Произошла ошибка при переводе. Попробуйте позже.</b>"
        )


# ---------- Профиль ----------
@router.message(F.text == "👤 Профиль")
@router.message(F.text.lower().strip() == "профиль")
@router.message(Command("profile"))
@router.message(F.text.regexp(PROFILE_CMD_RE))
async def show_profile(message: Message):
    try:
        # Определяем, чей профиль показывать
        target_user = message.from_user
        if (
            message.chat.type != "private"
            and message.reply_to_message
            and message.reply_to_message.from_user
            and not message.reply_to_message.from_user.is_bot
        ):
            target_user = message.reply_to_message.from_user

        await get_or_create_user(
            target_user.id, target_user.username, target_user.full_name
        )
        # Также регистрируем того, кто смотрит (если это чужой профиль)
        if target_user.id != message.from_user.id:
            await get_or_create_user(
                message.from_user.id,
                message.from_user.username,
                message.from_user.full_name,
            )

        photo, caption, kb = await render_profile(
            message.bot, target_user.id, viewer_id=message.from_user.id
        )
        if photo == DEFAULT_AVATAR_FILE_ID and "не найден" in caption.lower():
            await message.reply(caption)
            return
        try:
            await message.reply_photo(photo=photo, caption=caption, reply_markup=kb)
        except TelegramBadRequest as e:
            logger.error(f"profile photo bad request: {e}")
            await message.reply(caption, reply_markup=kb)
    except Exception as e:
        logger.error(f"Ошибка в show_profile: {e}")
        await message.reply("❌ <b>Произошла ошибка. Попробуйте позже.</b>")


@router.callback_query(BackToProfileCallback.filter())
async def process_back_to_profile(callback: CallbackQuery, callback_data: BackToProfileCallback):
    if not _owner_check(callback, callback_data.user_id):
        await callback.answer("⚠️ Кнопка предназначена не для вас", show_alert=True)
        return
    try:
        target_id = callback_data.user_id or callback.from_user.id
        photo, caption, kb = await render_profile(
            callback.message.bot, target_id, viewer_id=callback.from_user.id
        )
        if kb is None:
            await callback.answer("Пользователь не найден")
            return
        await show_or_edit_photo(callback.message, photo, caption, kb)
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка возврата в профиль: {e}")
        await callback.answer("⚠️ Произошла ошибка")


# ---------- Смена ника ----------
def validate_nickname(raw: str) -> Optional[str]:
    raw = raw.strip()
    if not (2 <= len(raw) <= 32):
        return None
    if URL_RE.search(raw):
        return None
    if not NICKNAME_RE.match(raw):
        return None
    return raw


@router.message(Command("nickname"))
async def nickname_cmd(message: Message, command: Command, state: FSMContext):
    user_id = message.from_user.id
    await get_or_create_user(user_id, message.from_user.username, message.from_user.full_name)

    arg = (command.args or "").strip()

    if arg.lower() == "reset":
        default = default_nickname(message.from_user.username, message.from_user.full_name, user_id)
        b = InlineKeyboardBuilder()
        b.button(text="✅ Подтвердить", callback_data=NickConfirmCallback(action="reset").pack())
        b.button(text="❌ Отмена", callback_data=NickConfirmCallback(action="cancel").pack())
        b.adjust(2)
        await state.set_state(NicknameSG.pending)
        await state.update_data(pending_nick=None, pending_action="reset")
        await message.reply(
            f"♻️ <b>Сбросить ник?</b>\n\n"
            f"Будет установлен: <b>{esc(default)}</b>\n"
            f"Сброс — <b>бесплатно</b>.",
            reply_markup=b.as_markup(),
        )
        return

    if not arg:
        await message.reply(
            f"✏️ Использование: <code>/nickname НовыйНик</code>\n"
            f"Смена ника стоит <b>{NICKNAME_COST} 🪙</b>.\n"
            f"Сброс: <code>/nickname reset</code> (бесплатно)."
        )
        return

    new_nick = validate_nickname(arg)
    if not new_nick:
        await message.reply(
            "❌ <b>Неверный ник.</b> Требования: 2–32 символа, "
            "без ссылок и упоминаний."
        )
        return

    row = await get_user_row(user_id)
    balance = row["coins"] if row else 0
    if balance < NICKNAME_COST:
        await message.reply(
            f"⚠️ Недостаточно монет. Нужно <b>{fmt_num(NICKNAME_COST)} 🪙</b>, "
            f"у вас <b>{fmt_num(balance)} 🪙</b>."
        )
        return

    b = InlineKeyboardBuilder()
    b.button(
        text="✅ Подтвердить",
        callback_data=NickConfirmCallback(action="apply").pack(),
    )
    b.button(text="❌ Отмена", callback_data=NickConfirmCallback(action="cancel").pack())
    b.adjust(2)
    await state.set_state(NicknameSG.pending)
    await state.update_data(pending_nick=new_nick, pending_action="apply")
    await message.reply(
        f"✏️ <b>Сменить ник?</b>\n\n"
        f"Новый ник: <b>{esc(new_nick)}</b>\n"
        f"Стоимость: <b>{NICKNAME_COST} 🪙</b> (баланс: {fmt_num(balance)})",
        reply_markup=b.as_markup(),
    )


@router.callback_query(NickConfirmCallback.filter())
async def nickname_confirm(callback: CallbackQuery, callback_data: NickConfirmCallback, state: FSMContext):
    user_id = callback.from_user.id

    if callback_data.action == "cancel":
        await state.clear()
        await callback.message.edit_text("✅ <b>Отменено</b>")
        await callback.answer()
        return

    data = await state.get_data()
    pending_action = data.get("pending_action")
    pending_nick = data.get("pending_nick")

    if callback_data.action == "reset":
        if pending_action and pending_action != "reset":
            await callback.answer("⚠️ Сессия устарела, повторите команду", show_alert=True)
            await state.clear()
            return
        default = default_nickname(
            callback.from_user.username, callback.from_user.full_name, user_id
        )
        async with get_db() as db:
            await db.execute(
                "UPDATE users SET nickname = ? WHERE user_id = ?", (default, user_id)
            )
        await state.clear()
        await callback.message.edit_text(f"✅ <b>Ник сброшен:</b> {esc(default)}")
        await callback.answer()
        return

    if callback_data.action == "apply":
        new_nick = pending_nick
        if not new_nick or not validate_nickname(new_nick):
            await state.clear()
            await callback.message.edit_text("❌ <b>Неверный ник</b>")
            await callback.answer()
            return

        async with get_db() as db:
            cur = await db.execute(
                "SELECT coins FROM users WHERE user_id = ?", (user_id,)
            )
            row = await cur.fetchone()
            balance = row["coins"] if row else 0
            if balance < NICKNAME_COST:
                await state.clear()
                await callback.message.edit_text(
                    f"⚠️ Недостаточно монет. Нужно <b>{fmt_num(NICKNAME_COST)} 🪙</b>, "
                    f"у вас <b>{fmt_num(balance)} 🪙</b>."
                )
                await callback.answer()
                return
            await db.execute(
                "UPDATE users SET nickname = ?, coins = coins - ? WHERE user_id = ?",
                (new_nick, NICKNAME_COST, user_id),
            )
        await state.clear()
        await callback.message.edit_text(
            f"✅ <b>Ник изменён:</b> {esc(new_nick)}\n"
            f"Списано: <b>{NICKNAME_COST} 🪙</b>"
        )
        await callback.answer()
        return

    await callback.answer()


@router.callback_query(NicknameCallback.filter(F.action == "change"))
async def change_nickname_hint(callback: CallbackQuery, callback_data: NicknameCallback):
    if not _owner_check(callback, callback_data.user_id):
        await callback.answer("⚠️ Кнопка предназначена не для вас", show_alert=True)
        return
    await callback.answer(
        f"Для изменения ника используйте команду /nickname НовыйНик ({NICKNAME_COST} 🪙) "
        f"или /nickname reset для сброса",
        show_alert=True,
    )


# ---------- Смена пола ----------
@router.callback_query(GenderCallback.filter(F.value == "menu"))
async def gender_menu(callback: CallbackQuery, callback_data: GenderCallback):
    if not _owner_check(callback, callback_data.user_id):
        await callback.answer("⚠️ Кнопка предназначена не для вас", show_alert=True)
        return
    user_id = callback_data.user_id or callback.from_user.id
    async with get_db() as db:
        cur = await db.execute("SELECT gender FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
    current = (row["gender"] if row and row["gender"] else "none")
    await callback.message.edit_caption(
        caption="⚧ <b>Выберите пол</b>\n\n<i>Это необязательное поле — можно оставить «Не задан».</i>",
        reply_markup=get_gender_kb(current, owner_id=user_id),
    )
    await callback.answer()


@router.callback_query(GenderCallback.filter(F.value != "menu"))
async def gender_set(callback: CallbackQuery, callback_data: GenderCallback):
    if not _owner_check(callback, callback_data.user_id):
        await callback.answer("⚠️ Кнопка предназначена не для вас", show_alert=True)
        return
    user_id = callback_data.user_id or callback.from_user.id
    value = callback_data.value
    if value not in GENDERS:
        await callback.answer("⚠️ Неизвестное значение")
        return

    async with get_db() as db:
        await db.execute("UPDATE users SET gender = ? WHERE user_id = ?", (value, user_id))

    g = GENDERS[value]
    await callback.message.edit_caption(
        caption=f"✅ <b>Пол сохранён:</b> {g['icon']} {g['name']}\n\n"
                f"<i>Вернуться в профиль:</i>",
        reply_markup=get_gender_kb(value, owner_id=user_id),
    )
    await callback.answer("✅ Сохранено")


@router.message(Command("gender"))
async def gender_cmd(message: Message, command: Command):
    user_id = message.from_user.id
    await get_or_create_user(user_id, message.from_user.username, message.from_user.full_name)
    arg = (command.args or "").strip().lower()

    if not arg:
        async with get_db() as db:
            cur = await db.execute("SELECT gender FROM users WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
        current = (row["gender"] if row and row["gender"] else "none")
        await message.reply("⚧ <b>Выберите пол:</b>", reply_markup=get_gender_kb(current, owner_id=user_id))
        return

    aliases = {
        "м": "male", "муж": "male", "мужской": "male", "m": "male", "male": "male",
        "ж": "female", "жен": "female", "женский": "female", "f": "female", "female": "female",
        "др": "other", "другой": "other", "other": "other",
        "none": "none", "не задан": "none", "нет": "none", "сброс": "none",
    }
    value = aliases.get(arg)
    if not value:
        await message.reply(
            "✏️ Использование: <code>/gender м|ж|др|нет</code>\n"
            "Например: <code>/gender ж</code>"
        )
        return

    async with get_db() as db:
        await db.execute("UPDATE users SET gender = ? WHERE user_id = ?", (value, user_id))

    g = GENDERS[value]
    await message.reply(f"✅ <b>Пол сохранён:</b> {g['icon']} {g['name']}")


# ---------- Топ ----------
async def build_top_text(kind: str, current_user_id: int) -> str:
    """Возвращает готовый текст топа с позицией пользователя."""
    limit = 10
    async with get_db() as db:
        if kind == "cards":
            cur = await db.execute(
                """SELECT u.user_id, u.nickname, COALESCE(SUM(i.amount), 0) AS value
                   FROM users u
                            LEFT JOIN inventory i ON u.user_id = i.user_id
                   GROUP BY u.user_id
                   ORDER BY value DESC, u.user_id ASC
                   LIMIT ?""",
                (limit,),
            )
            top = await cur.fetchall()
            cur = await db.execute(
                """SELECT value
                   FROM (SELECT u.user_id, COALESCE(SUM(i.amount), 0) AS value
                         FROM users u
                                  LEFT JOIN inventory i ON u.user_id = i.user_id
                         GROUP BY u.user_id)
                   WHERE user_id = ?""",
                (current_user_id,),
            )
            my_row = await cur.fetchone()
            my_value = my_row["value"] if my_row else 0
            cur = await db.execute(
                """SELECT COUNT(*) + 1
                   FROM (SELECT u.user_id, COALESCE(SUM(i.amount), 0) AS value
                         FROM users u
                                  LEFT JOIN inventory i ON u.user_id = i.user_id
                         GROUP BY u.user_id)
                   WHERE value > ?""",
                (my_value,),
            )
            my_rank = (await cur.fetchone())[0]
            unit = "🀄️"
            title = "🀄️ Топ по карточкам"
        elif kind == "streak":
            cur = await db.execute(
                "SELECT user_id, nickname, streak AS value FROM users "
                "ORDER BY value DESC, user_id ASC LIMIT ?",
                (limit,),
            )
            top = await cur.fetchall()
            cur = await db.execute("SELECT streak AS value FROM users WHERE user_id = ?", (current_user_id,))
            my_row = await cur.fetchone()
            my_value = my_row["value"] if my_row else 0
            cur = await db.execute(
                "SELECT COUNT(*) + 1 FROM users WHERE streak > ?", (my_value,)
            )
            my_rank = (await cur.fetchone())[0]
            unit = "🔥"
            title = "🔥 Топ по стрику"
        else:  # coins
            cur = await db.execute(
                "SELECT user_id, nickname, coins AS value FROM users "
                "ORDER BY value DESC, user_id ASC LIMIT ?",
                (limit,),
            )
            top = await cur.fetchall()
            cur = await db.execute("SELECT coins AS value FROM users WHERE user_id = ?", (current_user_id,))
            my_row = await cur.fetchone()
            my_value = my_row["value"] if my_row else 0
            cur = await db.execute("SELECT COUNT(*) + 1 FROM users WHERE coins > ?", (my_value,))
            my_rank = (await cur.fetchone())[0]
            unit = "🪙"
            title = "🪙 Топ по монетам"

        cur = await db.execute("SELECT nickname FROM users WHERE user_id = ?", (current_user_id,))
        my_row2 = await cur.fetchone()
        my_nick = my_row2["nickname"] if my_row2 and my_row2["nickname"] else f"User{current_user_id}"

    medals = ["🥇", "🥈", "🥉"]
    text = f"<b>{title}</b>\n\n"
    for i, row in enumerate(top, 1):
        medal = medals[i - 1] if i <= 3 else f"{i}."
        nick = row["nickname"] or f"User{row['user_id']}"
        mention = user_mention(row["user_id"], nick, None)
        text += f"{medal} {mention} • <b>{fmt_num(row['value'])}</b> {unit}\n"

    text += f"\n📌 Ваше место: <b>#{fmt_num(my_rank)}</b> — {esc(my_nick)} • <b>{fmt_num(my_value)}</b> {unit}"
    return text


@router.message(F.text == "🏆 Топ игроков")
@router.message(Command("top"))
@router.message(F.text.regexp(TOP_CMD_RE))
async def show_top_players(message: Message):
    if rate_limited(f"top:{message.from_user.id}", limit=6, window=5):
        return
    try:
        text = await build_top_text("coins", message.from_user.id)
        await message.reply(text, reply_markup=get_top_keyboard("coins"))
    except Exception as e:
        logger.error(f"Ошибка топа: {e}")
        await message.reply("❌ <b>Ошибка топа</b>")


@router.callback_query(TopCallback.filter())
async def switch_top(callback: CallbackQuery, callback_data: TopCallback):
    if rate_limited(f"top:{callback.from_user.id}", limit=5, window=5):
        await callback.answer("Слишком часто")
        return
    try:
        text = await build_top_text(callback_data.kind, callback.from_user.id)
        try:
            await callback.message.edit_text(
                text, reply_markup=get_top_keyboard(callback_data.kind)
            )
        except TelegramBadRequest:
            await callback.message.answer(
                text, reply_markup=get_top_keyboard(callback_data.kind)
            )
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка переключения топа: {e}")
        await callback.answer("⚠️ Произошла ошибка")


# ---------- Коллекция ----------
async def get_collection_main_keyboard(user_id: int):
    async with get_db() as db:
        cur = await db.execute(
            """SELECT c.rarity, COALESCE(SUM(i.amount), 0)
               FROM inventory i
                        JOIN cards c ON i.card_id = c.id
               WHERE i.user_id = ?
               GROUP BY c.rarity""",
            (user_id,),
        )
        stats = dict(await cur.fetchall())
        cur = await db.execute("SELECT COUNT(*) FROM cards")
        total_in_game = (await cur.fetchone())[0]

        rows = []
        total_cards = sum(stats.values())
        for r_key, r_info in RARITIES.items():
            user_amount = stats.get(r_key, 0)
            if user_amount == 0:
                continue
            cur = await db.execute("SELECT COUNT(*) FROM cards WHERE rarity = ?", (r_key,))
            total_of_rarity = (await cur.fetchone())[0]
            rows.append([
                InlineKeyboardButton(
                    text=f"{r_info['icon']} {r_info['name']} ({fmt_num(user_amount)}/{fmt_num(total_of_rarity)})",
                    callback_data=RaritySelectCallback(rarity=r_key, page=0, user_id=user_id).pack(),
                )
            ])
        rows.append([
            InlineKeyboardButton(
                text="👤 Перейти в профиль",
                callback_data=BackToProfileCallback(user_id=user_id).pack(),
            )
        ])
        return InlineKeyboardMarkup(inline_keyboard=rows), total_cards, total_in_game


@router.message(Command("collection"))
@router.message(F.text.regexp(COLLECTION_CMD_RE))
@router.callback_query(F.data == "collection")
async def show_collection(event):
    callback = event if isinstance(event, CallbackQuery) else None
    message = event.message if callback else event
    viewer_id = event.from_user.id

    # Определяем владельца коллекции
    target_id = viewer_id
    if (
        not callback
        and message.chat.type != "private"
        and message.reply_to_message
        and message.reply_to_message.from_user
        and not message.reply_to_message.from_user.is_bot
    ):
        target_id = message.reply_to_message.from_user.id
        await get_or_create_user(
            target_id,
            message.reply_to_message.from_user.username,
            message.reply_to_message.from_user.full_name,
        )

    try:
        await get_or_create_user(
            viewer_id,
            event.from_user.username,
            event.from_user.full_name,
        )
        photo, caption, keyboard, total = await render_collection(message.bot, target_id)
        if callback:
            await callback.answer()
        if total == 0:
            if message.photo:
                try:
                    await message.delete()
                except Exception:
                    pass
            await message.answer(caption, reply_markup=keyboard)
            return
        await show_or_edit_photo(message, photo, caption, keyboard)
    except Exception as e:
        logger.error(f"Ошибка коллекции: {e}")
        if callback:
            await callback.answer("⚠️ Произошла ошибка")
        else:
            await message.reply("❌ <b>Ошибка</b>")


@router.callback_query(RaritySelectCallback.filter())
async def process_rarity_view(callback: CallbackQuery, callback_data: RaritySelectCallback):
    # Просмотр коллекции доступен всем (кнопки привязаны к владельцу коллекции)
    user_id = callback_data.user_id or callback.from_user.id
    rarity, page = callback_data.rarity, callback_data.page
    try:
        async with get_db() as db:
            cur = await db.execute(
                """SELECT c.name, c.photo_id, i.claim_time, c.id, i.amount
                   FROM inventory i
                            JOIN cards c ON i.card_id = c.id
                   WHERE i.user_id = ?
                     AND c.rarity = ?
                   ORDER BY i.claim_time DESC""",
                (user_id, rarity),
            )
            cards = await cur.fetchall()

        if not cards:
            await callback.answer("У вас больше нет карточек этого типа")
            return

        total_pages = len(cards)
        page = max(0, min(page, total_pages - 1))
        card = cards[page]
        info = RARITIES.get(rarity, {})
        caption = (
            f"🀄️ <b>{esc(card['name'])}</b>\n\n"
            f"{info.get('icon', '')} Редкость • <b>{info.get('name', rarity)}</b>\n"
            f"🪙 Монеты • <b>+{fmt_num(info.get('reward', 0))}</b>\n"
            f"🔢 Количество • <b>{fmt_num(card['amount'])}</b>"
        )

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(
                text="◀️",
                callback_data=RaritySelectCallback(rarity=rarity, page=page - 1, user_id=user_id).pack(),
            ))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="ignore"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(
                text="▶️",
                callback_data=RaritySelectCallback(rarity=rarity, page=page + 1, user_id=user_id).pack(),
            ))

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            nav,
            [InlineKeyboardButton(
                text="🔙 К категориям",
                callback_data=MainMenuCallback(user_id=user_id).pack(),
            )],
        ])
        await show_or_edit_photo(callback.message, card["photo_id"], caption, keyboard)
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка просмотра коллекции: {e}")
        await callback.answer("⚠️ Произошла ошибка")


@router.callback_query(MainMenuCallback.filter())
async def process_back_to_main(callback: CallbackQuery, callback_data: MainMenuCallback):
    # Навигация по чужой коллекции разрешена (только просмотр)
    target_id = callback_data.user_id or callback.from_user.id
    photo, caption, keyboard, _ = await render_collection(
        callback.message.bot, target_id
    )
    await show_or_edit_photo(callback.message, photo, caption, keyboard)
    await callback.answer()


@router.callback_query(F.data == "ignore")
async def ignore_callback(callback: CallbackQuery):
    await callback.answer()


# ---------- Действия с карточкой ----------
@router.callback_query(CardActionCallback.filter())
async def handle_card_action(callback: CallbackQuery, callback_data: CardActionCallback):
    user_id = callback.from_user.id
    action = callback_data.action
    target = callback_data.user_id or user_id

    if user_id != target:
        await callback.answer("⚠️ Кнопка предназначена не для вас", show_alert=True)
        return

    if rate_limited(f"card-action:{user_id}", limit=8, window=10):
        await callback.answer("Слишком часто")
        return

    try:
        nickname = await get_user_nickname(user_id)
        mention = user_mention(user_id, nickname, callback.from_user.username)

        if action in ("instant", "another"):
            now = int(time.time())
            async with get_db() as db:
                cur = await db.execute(
                    "SELECT coins, last_claim FROM users WHERE user_id = ?", (user_id,)
                )
                row = await cur.fetchone()
            balance = row["coins"] if row else 0
            last_claim = row["last_claim"] if row else 0

            time_passed = now - last_claim
            if time_passed >= COOLDOWN_SECONDS:
                await callback.answer("⏳ Кулдаун уже прошёл — получайте бесплатно!", show_alert=True)
                return

            remaining = int(COOLDOWN_SECONDS - time_passed)
            cost = instant_cost(remaining)

            if balance < cost:
                await callback.answer(f"⚠️ Требуется {cost} 🪙, у вас {balance} 🪙")
                return

            await callback.answer(f"⏳ Получаем карточку за {cost} 🪙...")

            async with get_db() as db:
                await db.execute(
                    "UPDATE users SET coins = coins - ?, last_claim = ? WHERE user_id = ?",
                    (cost, now, user_id),
                )

            card, status = await issue_card(user_id, check_cooldown=False)

            if status != "success" or card is None:
                async with get_db() as db:
                    await db.execute(
                        "UPDATE users SET coins = coins + ? WHERE user_id = ?",
                        (cost, user_id),
                    )
                await callback.message.answer("❌ <b>Ошибка. Монеты возвращены.</b>")
                return

            streak, bonus, new_balance, gem_bonus = await check_and_update_streak(user_id)

            caption = _card_caption(mention, card)
            caption += _streak_text(streak, bonus, new_balance, gem_bonus)

            try:
                await callback.message.answer_photo(
                    photo=card["photo_id"],
                    caption=caption,
                    reply_markup=get_after_card_keyboard(user_id, card["balance"]),
                )
            except TelegramBadRequest as e:
                logger.error(f"instant photo error: {e}")
                await callback.message.answer(caption)
            return

        if action == "collection":
            photo, caption, keyboard, total = await render_collection(
                callback.message.bot, user_id
            )
            if total == 0:
                await callback.message.answer(caption)
                await callback.answer()
                return
            try:
                await callback.message.answer_photo(photo=photo, caption=caption, reply_markup=keyboard)
            except TelegramBadRequest:
                await callback.message.answer(caption, reply_markup=keyboard)
            await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка обработки действия: {e}")
        await callback.answer("⚠️ Произошла ошибка")


# ================= МАРКЕТ =================
async def get_user_gems(user_id: int) -> int:
    async with get_db() as db:
        cur = await db.execute(
            "SELECT gems FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        return (row["gems"] or 0) if row else 0


async def build_market_main(user_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    """Главное меню маркета: баланс + категории по редкости + обмен."""
    gems = await get_user_gems(user_id)
    async with get_db() as db:
        cur = await db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        coins = (row["coins"] or 0) if row else 0

        # Сколько карточек каждой редкости ещё нет у пользователя
        missing_by_rarity = {}
        for r_key in RARITIES:
            cur = await db.execute(
                """SELECT COUNT(*) FROM cards c
                   WHERE c.rarity = ?
                     AND c.id NOT IN (
                         SELECT card_id FROM inventory WHERE user_id = ?
                     )""",
                (r_key, user_id),
            )
            missing_by_rarity[r_key] = (await cur.fetchone())[0]

    nickname = await get_user_nickname(user_id)
    text = (
        f"🛒 <b>Маркет</b> • {esc(nickname)}\n\n"
        f"💎 Кристаллы: <b>{fmt_num(gems)}</b>\n"
        f"🪙 Монеты: <b>{fmt_num(coins)}</b>\n"
        f"💱 Курс: 1 💎 = {GEM_TO_COINS} 🪙\n\n"
        f"Выберите редкость, чтобы купить недостающие карточки:"
    )

    b = InlineKeyboardBuilder()
    for r_key, r_info in RARITIES.items():
        missing = missing_by_rarity.get(r_key, 0)
        price = MARKET_PRICES.get(r_key, 0)
        if missing > 0:
            label = (
                f"{r_info['icon']} {r_info['name']} "
                f"({fmt_num(missing)}) — {price} 💎"
            )
        else:
            label = f"{r_info['icon']} {r_info['name']} — собрано ✅"
        b.button(
            text=label,
            callback_data=MarketRarityCallback(rarity=r_key, page=0, user_id=user_id).pack(),
        )
    b.button(
        text="💱 Купить кристаллы за монеты",
        callback_data=MarketExchangeCallback(action="menu", user_id=user_id).pack(),
    )
    b.adjust(1)
    return text, b.as_markup()


async def build_market_rarity_page(
    user_id: int, rarity: str, page: int = 0
) -> Tuple[str, InlineKeyboardMarkup, Optional[dict]]:
    """Список недостающих карточек выбранной редкости (по 1 на страницу с фото)."""
    per_page = 1
    price = MARKET_PRICES.get(rarity, 0)
    r_info = RARITIES.get(rarity, {})

    async with get_db() as db:
        cur = await db.execute(
            """SELECT c.id, c.name, c.photo_id, c.rarity
               FROM cards c
               WHERE c.rarity = ?
                 AND c.id NOT IN (
                     SELECT card_id FROM inventory WHERE user_id = ?
                 )
               ORDER BY c.id ASC""",
            (rarity, user_id),
        )
        cards = await cur.fetchall()
        gems = await get_user_gems(user_id)

    if not cards:
        text = (
            f"{r_info.get('icon', '')} <b>{r_info.get('name', rarity)}</b>\n\n"
            f"У вас уже есть все карточки этой редкости! 🎉"
        )
        b = InlineKeyboardBuilder()
        b.button(text="🔙 Назад в маркет", callback_data=MarketMainCallback(user_id=user_id).pack())
        b.adjust(1)
        return text, b.as_markup(), None

    total = len(cards)
    page = max(0, min(page, total - 1))
    card = cards[page]

    text = (
        f"🛒 <b>Маркет</b> • {r_info.get('icon', '')} {r_info.get('name', rarity)}\n\n"
        f"🀄️ <b>{esc(card['name'])}</b>\n"
        f"💎 Цена • <b>{fmt_num(price)}</b>\n"
        f"💎 Ваш баланс • <b>{fmt_num(gems)}</b>\n"
        f"📄 {page + 1}/{total}"
    )

    b = InlineKeyboardBuilder()
    can_buy = gems >= price
    buy_label = (
        f"✅ Купить ({fmt_num(price)} 💎)"
        if can_buy
        else f"❌ Нужно {fmt_num(price)} 💎"
    )
    b.button(
        text=buy_label,
        callback_data=MarketBuyCallback(card_id=card["id"], user_id=user_id).pack(),
    )

    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="◀️",
                callback_data=MarketRarityCallback(rarity=rarity, page=page - 1, user_id=user_id).pack(),
            )
        )
    nav.append(
        InlineKeyboardButton(text=f"{page + 1}/{total}", callback_data="ignore")
    )
    if page < total - 1:
        nav.append(
            InlineKeyboardButton(
                text="▶️",
                callback_data=MarketRarityCallback(rarity=rarity, page=page + 1, user_id=user_id).pack(),
            )
        )

    b.row(*nav)
    b.row(
        InlineKeyboardButton(
            text="🔙 К редкостям", callback_data=MarketMainCallback(user_id=user_id).pack()
        )
    )
    return text, b.as_markup(), dict(card)


@router.message(F.text == "🛒 Маркет")
@router.message(Command("market"))
@router.message(F.text.lower().strip() == "маркет")
@router.message(F.text.regexp(MARKET_CMD_RE))
async def show_market(message: Message):
    try:
        target_user = message.from_user
        if (
            message.chat.type != "private"
            and message.reply_to_message
            and message.reply_to_message.from_user
            and not message.reply_to_message.from_user.is_bot
        ):
            target_user = message.reply_to_message.from_user

        await get_or_create_user(
            target_user.id, target_user.username, target_user.full_name
        )
        if target_user.id != message.from_user.id:
            await get_or_create_user(
                message.from_user.id,
                message.from_user.username,
                message.from_user.full_name,
            )

        if rate_limited(f"market:{message.from_user.id}", limit=5, window=8):
            return
        text, kb = await build_market_main(target_user.id)
        await message.reply(text, reply_markup=kb)
    except Exception as e:
        logger.error(f"Ошибка маркета: {e}")
        await message.reply("❌ <b>Ошибка маркета. Попробуйте позже.</b>")


@router.callback_query(MarketMainCallback.filter())
async def market_main_callback(callback: CallbackQuery, callback_data: MarketMainCallback):
    if not _owner_check(callback, callback_data.user_id):
        await callback.answer("⚠️ Кнопка предназначена не для вас", show_alert=True)
        return
    try:
        target_id = callback_data.user_id or callback.from_user.id
        text, kb = await build_market_main(target_id)
        try:
            if callback.message.photo:
                await callback.message.delete()
                await callback.message.answer(text, reply_markup=kb)
            else:
                await callback.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            await callback.message.answer(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка market_main: {e}")
        await callback.answer("⚠️ Ошибка")


@router.callback_query(MarketRarityCallback.filter())
async def market_rarity_view(callback: CallbackQuery, callback_data: MarketRarityCallback):
    if not _owner_check(callback, callback_data.user_id):
        await callback.answer("⚠️ Кнопка предназначена не для вас", show_alert=True)
        return
    try:
        target_id = callback_data.user_id or callback.from_user.id
        text, kb, card = await build_market_rarity_page(
            target_id, callback_data.rarity, callback_data.page
        )
        if card and card.get("photo_id"):
            try:
                if callback.message.photo:
                    await callback.message.edit_media(
                        media=InputMediaPhoto(
                            media=card["photo_id"], caption=text
                        ),
                        reply_markup=kb,
                    )
                else:
                    try:
                        await callback.message.delete()
                    except Exception:
                        pass
                    await callback.message.answer_photo(
                        photo=card["photo_id"], caption=text, reply_markup=kb
                    )
            except TelegramBadRequest:
                await callback.message.answer(text, reply_markup=kb)
        else:
            try:
                if callback.message.photo:
                    await callback.message.delete()
                    await callback.message.answer(text, reply_markup=kb)
                else:
                    await callback.message.edit_text(text, reply_markup=kb)
            except TelegramBadRequest:
                await callback.message.answer(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка market_rarity: {e}")
        await callback.answer("⚠️ Ошибка")


@router.callback_query(MarketBuyCallback.filter())
async def market_buy_card(callback: CallbackQuery, callback_data: MarketBuyCallback):
    user_id = callback.from_user.id
    card_id = callback_data.card_id
    target_id = callback_data.user_id or user_id

    if user_id != target_id:
        await callback.answer("⚠️ Кнопка предназначена не для вас", show_alert=True)
        return

    if rate_limited(f"mkt-buy:{user_id}", limit=5, window=10):
        await callback.answer("Слишком часто", show_alert=True)
        return

    try:
        async with get_db() as db:
            cur = await db.execute(
                "SELECT id, name, rarity, photo_id FROM cards WHERE id = ?",
                (card_id,),
            )
            card = await cur.fetchone()
            if not card:
                await callback.answer("❌ Карточка не найдена", show_alert=True)
                return

            rarity = card["rarity"]
            price = MARKET_PRICES.get(rarity, 0)
            if price <= 0:
                await callback.answer("❌ Эту карточку нельзя купить", show_alert=True)
                return

            # Уже есть?
            cur = await db.execute(
                "SELECT amount FROM inventory WHERE user_id = ? AND card_id = ?",
                (user_id, card_id),
            )
            owned = await cur.fetchone()
            if owned:
                await callback.answer("✅ У вас уже есть эта карточка", show_alert=True)
                return

            cur = await db.execute(
                "SELECT gems FROM users WHERE user_id = ?", (user_id,)
            )
            row = await cur.fetchone()
            gems = (row["gems"] or 0) if row else 0

            if gems < price:
                await callback.answer(
                    f"⚠️ Недостаточно кристаллов. Нужно {price} 💎, у вас {gems} 💎",
                    show_alert=True,
                )
                return

            now = int(time.time())
            await db.execute(
                "UPDATE users SET gems = gems - ? WHERE user_id = ?",
                (price, user_id),
            )
            await db.execute(
                """INSERT INTO inventory (user_id, card_id, claim_time, amount)
                   VALUES (?, ?, ?, 1)
                   ON CONFLICT(user_id, card_id) DO UPDATE SET amount = amount + 1,
                                                               claim_time = ?""",
                (user_id, card_id, now, now),
            )
            cur = await db.execute(
                "SELECT gems FROM users WHERE user_id = ?", (user_id,)
            )
            new_gems = (await cur.fetchone())[0] or 0

        r_info = RARITIES.get(rarity, {})
        caption = (
            f"✅ <b>Покупка успешна!</b>\n\n"
            f"🀄️ <b>{esc(card['name'])}</b>\n"
            f"{r_info.get('icon', '')} {r_info.get('name', rarity)}\n"
            f"💎 Списано • <b>{fmt_num(price)}</b>\n"
            f"💎 Баланс • <b>{fmt_num(new_gems)}</b>"
        )
        b = InlineKeyboardBuilder()
        b.button(
            text="🛒 Продолжить покупки",
            callback_data=MarketRarityCallback(rarity=rarity, page=0, user_id=user_id).pack(),
        )
        b.button(text="🏠 В маркет", callback_data=MarketMainCallback(user_id=user_id).pack())
        b.adjust(1)

        try:
            await callback.message.answer_photo(
                photo=card["photo_id"], caption=caption, reply_markup=b.as_markup()
            )
        except TelegramBadRequest:
            await callback.message.answer(caption, reply_markup=b.as_markup())
        await callback.answer("✅ Куплено!")
    except Exception as e:
        logger.error(f"Ошибка покупки в маркете: {e}")
        await callback.answer("⚠️ Ошибка покупки", show_alert=True)


@router.callback_query(MarketExchangeCallback.filter())
async def market_exchange(callback: CallbackQuery, callback_data: MarketExchangeCallback):
    user_id = callback.from_user.id
    action = callback_data.action
    amount = callback_data.amount
    target_id = callback_data.user_id or user_id

    if user_id != target_id:
        await callback.answer("⚠️ Кнопка предназначена не для вас", show_alert=True)
        return

    try:
        if action == "menu":
            async with get_db() as db:
                cur = await db.execute(
                    "SELECT coins, gems FROM users WHERE user_id = ?", (user_id,)
                )
                row = await cur.fetchone()
            coins = (row["coins"] or 0) if row else 0
            gems = (row["gems"] or 0) if row else 0
            max_buy = coins // GEM_TO_COINS

            text = (
                f"💱 <b>Обмен монет на кристаллы</b>\n\n"
                f"Курс: <b>1 💎 = {GEM_TO_COINS} 🪙</b>\n"
                f"🪙 Монеты: <b>{fmt_num(coins)}</b>\n"
                f"💎 Кристаллы: <b>{fmt_num(gems)}</b>\n"
                f"Можно купить до: <b>{fmt_num(max_buy)}</b> 💎\n\n"
                f"Выберите количество:"
            )
            b = InlineKeyboardBuilder()
            for n in (1, 5, 10, 25, 50):
                cost = n * GEM_TO_COINS
                if coins >= cost:
                    b.button(
                        text=f"+{n} 💎 ({fmt_num(cost)} 🪙)",
                        callback_data=MarketExchangeCallback(
                            action="buy_gems", amount=n, user_id=user_id
                        ).pack(),
                    )
            if max_buy >= 1 and max_buy not in (1, 5, 10, 25, 50):
                cost = max_buy * GEM_TO_COINS
                b.button(
                    text=f"+{fmt_num(max_buy)} 💎 (все, {fmt_num(cost)} 🪙)",
                    callback_data=MarketExchangeCallback(
                        action="buy_gems", amount=max_buy, user_id=user_id
                    ).pack(),
                )
            b.button(text="🔙 Назад", callback_data=MarketMainCallback(user_id=user_id).pack())
            b.adjust(2)
            try:
                if callback.message.photo:
                    await callback.message.delete()
                    await callback.message.answer(text, reply_markup=b.as_markup())
                else:
                    await callback.message.edit_text(text, reply_markup=b.as_markup())
            except TelegramBadRequest:
                await callback.message.answer(text, reply_markup=b.as_markup())
            await callback.answer()
            return

        if action == "buy_gems":
            if amount <= 0:
                await callback.answer("❌ Некорректное количество")
                return
            if amount > 10_000:
                await callback.answer("❌ Слишком много за раз", show_alert=True)
                return

            cost = amount * GEM_TO_COINS
            async with get_db() as db:
                cur = await db.execute(
                    "SELECT coins, gems FROM users WHERE user_id = ?", (user_id,)
                )
                row = await cur.fetchone()
                coins = (row["coins"] or 0) if row else 0
                if coins < cost:
                    await callback.answer(
                        f"⚠️ Недостаточно монет. Нужно {fmt_num(cost)} 🪙",
                        show_alert=True,
                    )
                    return
                await db.execute(
                    "UPDATE users SET coins = coins - ?, gems = gems + ? "
                    "WHERE user_id = ?",
                    (cost, amount, user_id),
                )
                cur = await db.execute(
                    "SELECT coins, gems FROM users WHERE user_id = ?", (user_id,)
                )
                row = await cur.fetchone()
                new_coins = row["coins"] or 0
                new_gems = row["gems"] or 0

            text = (
                f"✅ <b>Обмен выполнен</b>\n\n"
                f"💎 Получено: <b>+{fmt_num(amount)}</b>\n"
                f"🪙 Списано: <b>−{fmt_num(cost)}</b>\n\n"
                f"💎 Баланс: <b>{fmt_num(new_gems)}</b>\n"
                f"🪙 Монеты: <b>{fmt_num(new_coins)}</b>"
            )
            b = InlineKeyboardBuilder()
            b.button(
                text="💱 Ещё обмен",
                callback_data=MarketExchangeCallback(action="menu", user_id=user_id).pack(),
            )
            b.button(text="🛒 В маркет", callback_data=MarketMainCallback(user_id=user_id).pack())
            b.adjust(1)
            try:
                await callback.message.edit_text(text, reply_markup=b.as_markup())
            except TelegramBadRequest:
                await callback.message.answer(text, reply_markup=b.as_markup())
            await callback.answer("✅ Готово!")
            return

        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка обмена: {e}")
        await callback.answer("⚠️ Ошибка обмена", show_alert=True)


# ================= АДМИН-ПАНЕЛЬ =================
class AdminFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return await is_admin(message.from_user.id)


admin_filter = AdminFilter()


@router.message(Command("admin"), admin_filter)
async def admin_panel(message: Message):
    await message.answer(
        "⚙️ <b>Меню администратора</b>",
        reply_markup=get_admin_main_kb(),
    )


@router.callback_query(F.data == "admin_add_card")
async def add_card_start(call: CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Ошибка доступа")
        return
    await state.set_state(AddCardSG.photo)
    await call.message.answer("📷 <b>Отправьте фото новой карточки</b> (/cancel для отмены)")
    await call.answer()


@router.message(Command("addcard"), admin_filter, F.photo)
async def quick_add_card(message: Message, command: Command):
    if not message.photo:
        return
    args = (command.args or "").strip().split()
    if len(args) < 2:
        await message.reply(
            "✏️ Использование: <code>/addcard Название редкость</code> (фото в подписи)\n"
            "Редкости: " + ", ".join(RARITIES.keys())
        )
        return
    rarity = args[-1].lower()
    name = " ".join(args[:-1])
    if rarity not in RARITIES:
        await message.reply(
            f"❌ Неизвестная редкость <b>{esc(rarity)}</b>. Доступные: "
            + ", ".join(f"<code>{k}</code>" for k in RARITIES)
        )
        return
    if len(name) > 64:
        name = name[:64]
    photo_id = message.photo[-1].file_id
    async with get_db() as db:
        await db.execute(
            "INSERT INTO cards (name, rarity, photo_id) VALUES (?, ?, ?)",
            (name, rarity, photo_id),
        )
    await message.reply(
        f"✅ <b>Карточка добавлена:</b> {esc(name)} ({RARITIES[rarity]['name']})",
        reply_markup=get_admin_main_kb(),
    )


@router.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext):
    if await state.get_state() is None:
        return
    await state.clear()
    await message.reply("✅ <b>Операция отменена</b>", reply_markup=get_admin_main_kb())


@router.callback_query(F.data == "cancel_add_card")
async def cancel_add_card_callback(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("✅ <b>Операция отменена</b>", reply_markup=get_admin_main_kb())
    await call.answer()


@router.message(AddCardSG.photo, F.photo)
async def add_card_photo(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    await state.update_data(photo_id=message.photo[-1].file_id)
    await state.set_state(AddCardSG.name)
    await message.answer("✍️ <b>Придумайте название</b>")


@router.message(AddCardSG.name, F.text)
async def add_card_name(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    await state.update_data(name=message.text[:64])
    await state.set_state(AddCardSG.rarity)
    await message.answer("🎲 <b>Выберите редкость</b>", reply_markup=get_rarity_keyboard())


@router.callback_query(AddCardSG.rarity, F.data.startswith("set_rarity:"))
async def add_card_rarity(call: CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        await state.clear()
        await call.answer("⚠️ Ошибка доступа")
        return
    rarity = call.data.split(":")[1]
    data = await state.get_data()
    async with get_db() as db:
        await db.execute(
            "INSERT INTO cards (name, rarity, photo_id) VALUES (?, ?, ?)",
            (data["name"], rarity, data["photo_id"]),
        )
    await call.message.answer(
        f"✅ <b>Карточка добавлена:</b> {esc(data['name'])}",
        reply_markup=get_admin_main_kb(),
    )
    await state.clear()
    await call.answer()


# ================= АДМИН: СПИСОК КАРТОЧЕК =================
async def build_admin_cards_page(page: int = 0):
    per_page = 5
    async with get_db() as db:
        cur = await db.execute("SELECT COUNT(*) FROM cards")
        total = (await cur.fetchone())[0]
        if total == 0:
            return "🔴 <b>В базе нет карточек.</b>", get_admin_main_kb(), 0

        total_pages = (total + per_page - 1) // per_page
        page = max(0, min(page, total_pages - 1))
        offset = page * per_page

        cur = await db.execute(
            """SELECT id, name, rarity, photo_id
               FROM cards
               ORDER BY id DESC
               LIMIT ? OFFSET ?""",
            (per_page, offset),
        )
        cards = await cur.fetchall()

        b = InlineKeyboardBuilder()
        text = f"📜 <b>Карточки</b> (стр. {page + 1}/{total_pages})\n\n"
        for c in cards:
            r = RARITIES.get(c["rarity"], {})
            cur = await db.execute(
                "SELECT COUNT(DISTINCT user_id), COALESCE(SUM(amount), 0) FROM inventory WHERE card_id = ?",
                (c["id"],),
            )
            owners, instances = await cur.fetchone()
            text += (
                f"🆔 <code>{c['id']}</code> {r.get('icon', '')} <b>{esc(c['name'])}</b>\n"
                f"   👥 {fmt_num(owners)} польз. | 📦 {fmt_num(instances)} шт.\n\n"
            )
            b.button(
                text=f"{r.get('icon', '')} {c['name']}",
                callback_data=AdminCardManageCallback(card_id=c["id"]).pack(),
            )

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=AdminCardPageCallback(page=page - 1).pack()))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="ignore"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=AdminCardPageCallback(page=page + 1).pack()))

        b.adjust(1)
        b.row(*nav)
        b.row(InlineKeyboardButton(text="🔙 Назад в админ-меню", callback_data="admin_main"))

        return text, b.as_markup(), page


@router.callback_query(AdminCardPageCallback.filter())
async def admin_cards_page_callback(call: CallbackQuery, callback_data: AdminCardPageCallback):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Ошибка доступа")
        return
    text, kb, _ = await build_admin_cards_page(callback_data.page)
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        try:
            await call.message.edit_caption(caption=text, reply_markup=kb)
        except TelegramBadRequest:
            await call.message.answer(text, reply_markup=kb)
    await call.answer()


@router.callback_query(AdminCardManageCallback.filter())
async def admin_card_manage(call: CallbackQuery, callback_data: AdminCardManageCallback):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Ошибка доступа")
        return
    card_id = callback_data.card_id
    async with get_db() as db:
        cur = await db.execute("SELECT name, rarity, photo_id FROM cards WHERE id = ?", (card_id,))
        card = await cur.fetchone()
        if not card:
            await call.answer("❌ Карточка не найдена")
            return
        cur = await db.execute(
            "SELECT COUNT(DISTINCT user_id), COALESCE(SUM(amount), 0) FROM inventory WHERE card_id = ?",
            (card_id,),
        )
        owners, instances = await cur.fetchone()

    r = RARITIES.get(card["rarity"], {})
    caption = (
        f"🀄️ <b>{esc(card['name'])}</b>\n\n"
        f"🆔 <code>{card_id}</code>\n"
        f"{r.get('icon', '')} Редкость • <b>{r.get('name', card['rarity'])}</b>\n"
        f"👥 Владельцев • <b>{fmt_num(owners)}</b>\n"
        f"📦 Экземпляров • <b>{fmt_num(instances)}</b>"
    )
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Изменить название", callback_data=f"edit_name:{card_id}")
    b.button(text="🎲 Изменить редкость", callback_data=f"edit_rarity:{card_id}")
    b.button(text="🖼 Изменить фото", callback_data=AdminCardEditPhotoCallback(card_id=card_id).pack())
    b.button(text="❌ Удалить", callback_data=f"delete_card:{card_id}")
    b.button(text="🔙 Назад к списку", callback_data=AdminCardPageCallback(page=0).pack())
    b.adjust(1)

    try:
        if call.message.photo:
            await call.message.edit_media(
                media=InputMediaPhoto(media=card["photo_id"], caption=caption),
                reply_markup=b.as_markup(),
            )
        else:
            await call.message.answer_photo(photo=card["photo_id"], caption=caption, reply_markup=b.as_markup())
    except TelegramBadRequest:
        await call.message.answer_photo(photo=card["photo_id"], caption=caption, reply_markup=b.as_markup())
    await call.answer()


@router.callback_query(AdminCardEditPhotoCallback.filter())
async def admin_card_edit_photo_start(call: CallbackQuery, callback_data: AdminCardEditPhotoCallback,
                                      state: FSMContext):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Ошибка доступа")
        return
    await state.set_state(EditCardPhotoSG.photo)
    await state.update_data(card_id=callback_data.card_id)
    await call.message.answer("📷 <b>Отправьте новое фото для карточки</b> (/cancel для отмены)")
    await call.answer()


@router.message(EditCardPhotoSG.photo, F.photo)
async def admin_card_edit_photo_save(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    card_id = data.get("card_id")
    if not card_id:
        await state.clear()
        return
    new_photo = message.photo[-1].file_id
    async with get_db() as db:
        await db.execute("UPDATE cards SET photo_id = ? WHERE id = ?", (new_photo, card_id))
    await message.answer(
        f"✅ <b>Фото карточки обновлено</b> (ID {card_id})",
        reply_markup=get_admin_main_kb(),
    )
    await state.clear()


@router.callback_query(F.data == "admin_list_cards")
async def list_cards(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Ошибка доступа")
        return
    text, kb, _ = await build_admin_cards_page(0)
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "admin_main")
async def admin_main_back(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Ошибка доступа")
        return
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer("⚙️ <b>Меню администратора</b>", reply_markup=get_admin_main_kb())
    await call.answer()


@router.callback_query(F.data.startswith("delete_card:"))
async def delete_card_cmd(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Ошибка доступа")
        return
    card_id = int(call.data.split(":")[1])
    async with get_db() as db:
        await db.execute("DELETE FROM cards WHERE id = ?", (card_id,))
        await db.execute("DELETE FROM inventory WHERE card_id = ?", (card_id,))
    await call.message.answer("🗑 <b>Карточка удалена</b>")
    await call.answer()


@router.callback_query(F.data.startswith("edit_name:"))
async def edit_card_name_start(call: CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Ошибка доступа")
        return
    await state.update_data(card_id=int(call.data.split(":")[1]))
    await state.set_state(EditCardSG.new_name)
    await call.message.answer("✍️ <b>Новое название:</b>")
    await call.answer()


@router.message(EditCardSG.new_name, F.text)
async def edit_card_name_save(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    new_name = message.text[:64]
    async with get_db() as db:
        await db.execute("UPDATE cards SET name = ? WHERE id = ?", (new_name, data["card_id"]))
    await message.answer(
        f"✅ <b>Название изменено:</b> {esc(new_name)}",
        reply_markup=get_admin_main_kb(),
    )
    await state.clear()


@router.callback_query(F.data.startswith("edit_rarity:"))
async def edit_card_rarity_start(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Ошибка доступа")
        return
    card_id = int(call.data.split(":")[1])
    b = InlineKeyboardBuilder()
    for key, info in RARITIES.items():
        b.button(
            text=info["name"],
            callback_data=AdminRarityCallback(card_id=card_id, rarity=key).pack(),
        )
    b.button(text="🔙 Назад", callback_data=AdminCardManageCallback(card_id=card_id).pack())
    b.adjust(2)
    await call.message.answer("🎲 <b>Новая редкость:</b>", reply_markup=b.as_markup())
    await call.answer()


@router.callback_query(AdminRarityCallback.filter())
async def edit_card_rarity_save(call: CallbackQuery, callback_data: AdminRarityCallback):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Ошибка доступа")
        return
    async with get_db() as db:
        await db.execute(
            "UPDATE cards SET rarity = ? WHERE id = ?",
            (callback_data.rarity, callback_data.card_id),
        )
    await call.message.answer(
        f"✅ <b>Редкость изменена:</b> {RARITIES[callback_data.rarity]['name']}",
        reply_markup=get_admin_main_kb(),
    )
    await call.answer()


# ================= АДМИН: СПИСОК ПОЛЬЗОВАТЕЛЕЙ =================
async def build_admin_users_page(page: int = 0):
    per_page = 5
    async with get_db() as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        total = (await cur.fetchone())[0]
        if total == 0:
            return "🔴 <b>Нет пользователей.</b>", get_admin_main_kb(), 0

        total_pages = (total + per_page - 1) // per_page
        page = max(0, min(page, total_pages - 1))
        offset = page * per_page

        cur = await db.execute(
            """SELECT u.user_id, u.nickname, u.coins, u.gems, u.streak, u.role, u.registration, u.gender
               FROM users u
               ORDER BY u.registration DESC
               LIMIT ? OFFSET ?""",
            (per_page, offset),
        )
        users = await cur.fetchall()

        b = InlineKeyboardBuilder()
        text = f"👥 <b>Пользователи</b> (стр. {page + 1}/{total_pages}, всего {fmt_num(total)})\n\n"
        for u in users:
            g = GENDERS.get(u["gender"] or "none", GENDERS["none"])
            role_mark = "👑" if u["role"] == "admin" else "👤"
            reg = datetime.fromtimestamp(u["registration"]).strftime("%d.%m.%Y") if u["registration"] else "—"
            nick = u["nickname"] or f"User{u['user_id']}"
            text += (
                f"{role_mark} <b>{esc(nick)}</b>\n"
                f"   🆔 <code>{u['user_id']}</code> | 🪙 {fmt_num(u['coins'] or 0)} | "
                f"💎 {fmt_num(u['gems'] or 0)} | 🔥 {u['streak'] or 0} | {g['icon']} | 📅 {reg}\n\n"
            )
            b.button(
                text=f"{role_mark} {nick}",
                callback_data=AdminUserViewCallback(user_id=u["user_id"]).pack(),
            )

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=AdminUserPageCallback(page=page - 1).pack()))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="ignore"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=AdminUserPageCallback(page=page + 1).pack()))

        b.adjust(1)
        b.row(*nav)
        b.row(InlineKeyboardButton(text="🔙 Назад в админ-меню", callback_data="admin_main"))

        return text, b.as_markup(), page


@router.callback_query(AdminUserPageCallback.filter())
async def admin_users_page_callback(call: CallbackQuery, callback_data: AdminUserPageCallback):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Ошибка доступа")
        return
    text, kb, _ = await build_admin_users_page(callback_data.page)
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()


@router.callback_query(AdminUserViewCallback.filter())
async def admin_user_view(call: CallbackQuery, callback_data: AdminUserViewCallback):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Ошибка доступа")
        return
    user_id = callback_data.user_id
    async with get_db() as db:
        cur = await db.execute(
            """SELECT u.*, COALESCE(SUM(i.amount), 0) AS cards_count
               FROM users u
                        LEFT JOIN inventory i ON u.user_id = i.user_id
               WHERE u.user_id = ?
               GROUP BY u.user_id""",
            (user_id,),
        )
        user = await cur.fetchone()
        if not user:
            await call.answer("❌ Пользователь не найден")
            return

    nick = user["nickname"] or f"User{user_id}"
    role = "👑 Администратор" if user["role"] == "admin" else "👤 Пользователь"
    g = GENDERS.get(user["gender"] or "none", GENDERS["none"])
    reg = datetime.fromtimestamp(user["registration"]).strftime("%d.%m.%Y %H:%M") if user["registration"] else "—"

    gems = user["gems"] if user["gems"] is not None else 0
    caption = (
        f"👤 <b>{esc(nick)}</b>\n\n"
        f"🆔 <code>{user_id}</code>\n"
        f"🎭 Роль: <b>{role}</b>\n"
        f"⚧ Пол: {g['icon']} {g['name']}\n"
        f"📅 Регистрация: {reg}\n"
        f"🪙 Монеты: <b>{fmt_num(user['coins'])}</b>\n"
        f"💎 Кристаллы: <b>{fmt_num(gems)}</b>\n"
        f"🀄️ Карточек: <b>{fmt_num(user['cards_count'])}</b>\n"
        f"🔥 Стрик: <b>{fmt_days(user['streak'])}</b>"
    )

    b = InlineKeyboardBuilder()
    b.button(text="✏️ Ник", callback_data=AdminUserActionCallback(action="nick", user_id=user_id).pack())
    b.button(text="🪙 Монеты", callback_data=AdminUserActionCallback(action="coins", user_id=user_id).pack())
    b.button(text="💎 Кристаллы", callback_data=AdminUserActionCallback(action="gems", user_id=user_id).pack())
    if user["role"] == "admin":
        b.button(text="❌ Разжаловать", callback_data=AdminUserActionCallback(action="unadmin", user_id=user_id).pack())
    else:
        b.button(text="👑 Назначить админом",
                 callback_data=AdminUserActionCallback(action="admin", user_id=user_id).pack())
    b.button(text="🔙 Назад к списку", callback_data=AdminUserPageCallback(page=0).pack())
    b.adjust(1)

    await call.message.answer(caption, reply_markup=b.as_markup())
    await call.answer()


@router.callback_query(AdminUserActionCallback.filter())
async def admin_user_action(call: CallbackQuery, callback_data: AdminUserActionCallback):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Ошибка доступа")
        return

    action = callback_data.action
    target_id = callback_data.user_id

    if action == "admin":
        async with get_db() as db:
            await db.execute("UPDATE users SET role = 'admin' WHERE user_id = ?", (target_id,))
        await call.message.answer(f"✅ Пользователь <code>{target_id}</code> назначен админом.")
        await call.answer()
    elif action == "unadmin":
        if target_id == call.from_user.id:
            await call.answer("❌ Нельзя разжаловать себя", show_alert=True)
            return
        async with get_db() as db:
            await db.execute("UPDATE users SET role = 'user' WHERE user_id = ?", (target_id,))
        await call.message.answer(f"✅ Пользователь <code>{target_id}</code> разжалован.")
        await call.answer()
    elif action == "nick":
        await call.message.answer(
            f"✏️ Чтобы изменить ник пользователя <code>{target_id}</code>, "
            f"выполните команду:\n<code>/setnick {target_id} НовыйНик</code>"
        )
        await call.answer()
    elif action == "coins":
        await call.message.answer(
            f"🪙 Чтобы изменить монеты пользователя <code>{target_id}</code>, "
            f"выполните команду:\n<code>/setcoins {target_id} 1000</code>"
        )
        await call.answer()
    elif action == "gems":
        await call.message.answer(
            f"💎 Чтобы изменить кристаллы пользователя <code>{target_id}</code>, "
            f"выполните команду:\n<code>/setgems {target_id} 50</code>"
        )
        await call.answer()


@router.message(Command("setnick"), admin_filter)
async def admin_setnick(message: Message, command: Command):
    if message.chat.type != "private":
        await message.reply("⚠️ Только в ЛС.")
        return
    args = (command.args or "").strip().split()
    if len(args) < 2:
        await message.reply(
            "✏️ Использование: <code>/setnick USERID НовыйНик</code>"
        )
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await message.reply("❌ USERID должен быть числом.")
        return
    new_nick_raw = " ".join(args[1:])
    new_nick = validate_nickname(new_nick_raw)
    if not new_nick:
        await message.reply("❌ Ник не проходит валидацию (2-32 символа, без ссылок).")
        return
    async with get_db() as db:
        cur = await db.execute("SELECT nickname FROM users WHERE user_id = ?", (target_id,))
        row = await cur.fetchone()
        if not row:
            await message.reply("❌ Пользователь не найден.")
            return
        old = row["nickname"]
        await db.execute("UPDATE users SET nickname = ? WHERE user_id = ?", (new_nick, target_id))
    await message.reply(
        f"✅ Ник изменён: <code>{target_id}</code>\n"
        f"Было: {esc(old or '—')} → Стало: {esc(new_nick)}"
    )


# ================= ПРОЧИЕ АДМИН-КОМАНДЫ =================
@router.message(Command("adminhelp"), admin_filter)
async def admin_help(message: Message):
    text = (
        "🛠 <b>Справка администратора</b>\n\n"

        "<b>👑 Управление правами:</b>\n"
        "<code>/setadmin USERID</code> — назначить админа\n"
        "<code>/unsetadmin USERID</code> — разжаловать админа\n"
        "<i>Только в ЛС. Нельзя разжаловать себя.</i>\n\n"

        "<b>🀄️ Управление карточками:</b>\n"
        "<code>/admin</code> — панель администратора\n"
        "  • ➕ Добавить карточку\n"
        "  • 📜 Список карточек (пагинация, статистика, редактирование, удаление, смена фото)\n"
        "  • 👥 Список пользователей (пагинация, профиль, действия)\n"
        "<code>/addcard Название редкость</code> + фото — быстрое добавление\n"
        "<code>/delcard ID</code> — быстрое удаление карточки\n\n"

        "<b>📊 Статистика и данные:</b>\n"
        "<code>/stats</code> — общая статистика бота\n"
        "<code>/getusers</code> — список пользователей (текстом)\n\n"

        "<b>🪙 Управление пользователями:</b>\n"
        "<code>/setcoins [USERID] COINS</code> — установить баланс монет\n"
        "<code>/setgems [USERID] N</code> — установить баланс кристаллов\n"
        "<code>/setnick USERID НовыйНик</code> — установить ник\n"
        "<code>/resetcd [USERID]</code> — сбросить кулдаун\n"
        "<i>Все команды работают только в ЛС.</i>\n\n"

        "<b>🧪 Тестовые команды:</b>\n"
        "<code>/migrate_crystals_to_gems</code> — переименовать колонку crystals → gems\n"
        "<code>/reset_all_nicknames</code> — сбросить ники всех\n"
        "<code>/promote_to_mythical</code> — 10 случайных epic/legendary в mythical\n"
        "<code>/getfileid</code> — file_id картинки (фото с командой в подписи)\n"
        "<code>/denominate_coins</code> — разделить баланс всех на 10\n"
        "<code>/set_registration_now</code> — проставить дату регистрации\n\n"

        "<b>⌨️ Общие команды:</b>\n"
        "<code>/cancel</code> — отменить текущую FSM-операцию\n"
        "<code>/adminhelp</code> — эта справка\n\n"

        "💡 <i>Команда скрыта из общей справки и доступна только админам.</i>"
    )
    await message.reply(text)


@router.message(Command("setadmin"), admin_filter)
async def set_admin_cmd(message: Message, command: Command):
    if message.chat.type != "private":
        await message.reply("⚠️ Команда доступна только в личных сообщениях с ботом.")
        return

    arg = (command.args or "").strip()
    if not arg:
        await message.reply(
            "✏️ Использование: <code>/setadmin USERID</code>\n"
            "Например: <code>/setadmin 123456789</code>"
        )
        return

    try:
        target_id = int(arg)
    except ValueError:
        await message.reply("❌ <b>USERID должен быть числом.</b>")
        return

    if target_id <= 0:
        await message.reply("❌ <b>Некорректный USERID.</b>")
        return

    async with get_db() as db:
        cur = await db.execute(
            "SELECT user_id, nickname, role FROM users WHERE user_id = ?", (target_id,)
        )
        row = await cur.fetchone()
        if not row:
            await message.reply(
                f"❌ <b>Пользователь не найден в базе.</b>\n"
                f"Он должен хотя бы раз запустить бота (<code>/start</code>)."
            )
            return

        if row["role"] == "admin":
            await message.reply(
                f"ℹ️ Пользователь <b>{esc(row['nickname'] or target_id)}</b> "
                f"уже является администратором."
            )
            return

        await db.execute(
            "UPDATE users SET role = 'admin' WHERE user_id = ?", (target_id,)
        )

    await message.reply(
        f"✅ <b>Пользователь назначен администратором:</b>\n"
        f"🆔 <code>{target_id}</code>\n"
        f"👤 {esc(row['nickname'] or str(target_id))}"
    )


@router.message(Command("unsetadmin"), admin_filter)
async def unset_admin_cmd(message: Message, command: Command):
    if message.chat.type != "private":
        await message.reply("⚠️ Команда доступна только в личных сообщениях с ботом.")
        return

    arg = (command.args or "").strip()
    if not arg:
        await message.reply(
            "✏️ Использование: <code>/unsetadmin USERID</code>\n"
            "Например: <code>/unsetadmin 123456789</code>"
        )
        return

    try:
        target_id = int(arg)
    except ValueError:
        await message.reply("❌ <b>USERID должен быть числом.</b>")
        return

    if target_id <= 0:
        await message.reply("❌ <b>Некорректный USERID.</b>")
        return

    if target_id == message.from_user.id:
        await message.reply(
            "⚠️ <b>Нельзя разжаловать самого себя.</b>\n"
            "Попросите другого администратора сделать это."
        )
        return

    async with get_db() as db:
        cur = await db.execute(
            "SELECT user_id, nickname, role FROM users WHERE user_id = ?", (target_id,)
        )
        row = await cur.fetchone()
        if not row:
            await message.reply(f"❌ <b>Пользователь не найден в базе.</b>")
            return

        if row["role"] != "admin":
            await message.reply(
                f"ℹ️ Пользователь <b>{esc(row['nickname'] or target_id)}</b> "
                f"не является администратором."
            )
            return

        await db.execute(
            "UPDATE users SET role = 'user' WHERE user_id = ?", (target_id,)
        )

    await message.reply(
        f"✅ <b>Пользователь разжалован:</b>\n"
        f"🆔 <code>{target_id}</code>\n"
        f"👤 {esc(row['nickname'] or str(target_id))}"
    )


@router.message(Command("stats"), admin_filter)
async def admin_stats(message: Message):
    try:
        async with get_db() as db:
            cur = await db.execute("SELECT COUNT(*) FROM users")
            total_users = (await cur.fetchone())[0]

            cur = await db.execute(
                "SELECT COUNT(*) FROM users WHERE last_claim > 0"
            )
            active_users = (await cur.fetchone())[0]

            cur = await db.execute(
                "SELECT COUNT(*) FROM users WHERE streak > 0"
            )
            streaked_users = (await cur.fetchone())[0]

            cur = await db.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin'"
            )
            admins = (await cur.fetchone())[0]

            cur = await db.execute("SELECT COALESCE(SUM(coins), 0) FROM users")
            total_coins = (await cur.fetchone())[0]

            cur = await db.execute("SELECT COALESCE(AVG(coins), 0) FROM users")
            avg_coins = (await cur.fetchone())[0]

            cur = await db.execute("SELECT COALESCE(MAX(coins), 0) FROM users")
            max_coins = (await cur.fetchone())[0]

            cur = await db.execute("SELECT COALESCE(SUM(gems), 0) FROM users")
            total_gems = (await cur.fetchone())[0]

            cur = await db.execute("SELECT COALESCE(MAX(gems), 0) FROM users")
            max_gems = (await cur.fetchone())[0]

            cur = await db.execute("SELECT COUNT(*) FROM cards")
            total_cards = (await cur.fetchone())[0]

            cur = await db.execute(
                "SELECT rarity, COUNT(*) FROM cards GROUP BY rarity"
            )
            cards_by_rarity = dict(await cur.fetchall())

            cur = await db.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM inventory"
            )
            total_owned = (await cur.fetchone())[0]

            cur = await db.execute(
                "SELECT COUNT(DISTINCT user_id) FROM inventory"
            )
            collectors = (await cur.fetchone())[0]

            cur = await db.execute(
                "SELECT COUNT(DISTINCT card_id) FROM inventory"
            )
            unique_owned = (await cur.fetchone())[0]

            cur = await db.execute(
                "SELECT nickname, coins FROM users ORDER BY coins DESC LIMIT 1"
            )
            top_user = await cur.fetchone()

            cur = await db.execute(
                """SELECT c.name, COALESCE(SUM(i.amount), 0) AS cnt
                   FROM inventory i
                            JOIN cards c ON i.card_id = c.id
                   GROUP BY c.id
                   ORDER BY cnt DESC
                   LIMIT 1"""
            )
            top_card = await cur.fetchone()

        rarity_lines = []
        for r_key, r_info in RARITIES.items():
            cnt = cards_by_rarity.get(r_key, 0)
            rarity_lines.append(f"  {r_info['icon']} {r_info['name']}: <b>{fmt_num(cnt)}</b>")

        text = (
                f"📊 <b>Статистика бота</b>\n\n"
                f"<b>👥 Пользователи:</b>\n"
                f"  • Всего: <b>{fmt_num(total_users)}</b>\n"
                f"  • Активных (получали карточки): <b>{fmt_num(active_users)}</b>\n"
                f"  • Со стриком: <b>{fmt_num(streaked_users)}</b>\n"
                f"  • Админов: <b>{fmt_num(admins)}</b>\n\n"
                f"<b>🪙 Монеты:</b>\n"
                f"  • В обороте: <b>{fmt_num(total_coins)}</b>\n"
                f"  • В среднем: <b>{fmt_num(int(avg_coins))}</b>\n"
                f"  • Максимум: <b>{fmt_num(max_coins)}</b>\n\n"
                f"<b>💎 Кристаллы:</b>\n"
                f"  • В обороте: <b>{fmt_num(total_gems)}</b>\n"
                f"  • Максимум: <b>{fmt_num(max_gems)}</b>\n\n"
                f"<b>🀄️ Карточки в игре:</b>\n"
                f"  • Всего: <b>{fmt_num(total_cards)}</b>\n"
                + "\n".join(rarity_lines) + "\n\n"
                                            f"<b>🎒 Коллекции:</b>\n"
                                            f"  • Собрано экземпляров: <b>{fmt_num(total_owned)}</b>\n"
                                            f"  • Уникальных карточек: <b>{fmt_num(unique_owned)}</b> из <b>{fmt_num(total_cards)}</b>\n"
                                            f"  • Коллекционеров: <b>{fmt_num(collectors)}</b>\n\n"
        )

        if top_user:
            text += (
                f"<b>🏆 Лидер по монетам:</b>\n"
                f"  {esc(top_user['nickname'] or '—')} — <b>{fmt_num(top_user['coins'])}</b> 🪙\n\n"
            )
        if top_card:
            text += (
                f"<b>🔥 Популярная карточка:</b>\n"
                f"  {esc(top_card['name'])} — <b>{fmt_num(top_card['cnt'])}</b> шт."
            )

        await message.reply(text)
    except Exception as e:
        logger.error(f"Ошибка /stats: {e}")
        await message.reply("❌ <b>Ошибка при сборе статистики.</b>")


@router.message(Command("setcoins"), admin_filter)
async def admin_setcoins(message: Message, command: Command):
    if message.chat.type != "private":
        await message.reply("⚠️ Команда доступна только в личных сообщениях с ботом.")
        return

    args = (command.args or "").strip().split()
    if not args:
        await message.reply(
            "✏️ Использование:\n"
            "<code>/setcoins COINS</code> — себе\n"
            "<code>/setcoins USERID COINS</code> — другому пользователю\n\n"
            "Например: <code>/setcoins 500</code> или <code>/setcoins 123456789 1000</code>"
        )
        return

    target_id = message.from_user.id

    if len(args) == 1:
        try:
            coins = int(args[0])
        except ValueError:
            await message.reply("❌ <b>COINS должно быть числом.</b>")
            return
    elif len(args) == 2:
        try:
            target_id = int(args[0])
            coins = int(args[1])
        except ValueError:
            await message.reply("❌ <b>USERID и COINS должны быть числами.</b>")
            return
    else:
        await message.reply("❌ <b>Слишком много аргументов.</b> Используйте /setcoins [USERID] COINS")
        return

    if target_id <= 0:
        await message.reply("❌ <b>Некорректный USERID.</b>")
        return
    if coins < 0:
        await message.reply("❌ <b>Баланс не может быть отрицательным.</b>")
        return

    async with get_db() as db:
        cur = await db.execute(
            "SELECT user_id, nickname, coins FROM users WHERE user_id = ?",
            (target_id,),
        )
        row = await cur.fetchone()
        if not row:
            await message.reply(
                f"❌ <b>Пользователь не найден в базе.</b>\n"
                f"Он должен хотя бы раз запустить бота (<code>/start</code>)."
            )
            return

        old_coins = row["coins"] or 0
        await db.execute(
            "UPDATE users SET coins = ? WHERE user_id = ?", (coins, target_id)
        )

    who = "себе" if target_id == message.from_user.id else f"пользователю {esc(row['nickname'] or target_id)}"
    await message.reply(
        f"✅ <b>Баланс обновлён</b> ({who}):\n"
        f"🆔 <code>{target_id}</code>\n"
        f"🪙 Было: <b>{fmt_num(old_coins)}</b> → Стало: <b>{fmt_num(coins)}</b>"
    )


@router.message(Command("setgems"), admin_filter)
async def admin_setgems(message: Message, command: Command):
    if message.chat.type != "private":
        await message.reply("⚠️ Команда доступна только в личных сообщениях с ботом.")
        return

    args = (command.args or "").strip().split()
    if not args:
        await message.reply(
            "✏️ Использование:\n"
            "<code>/setgems N</code> — себе\n"
            "<code>/setgems USERID N</code> — другому пользователю\n\n"
            "Например: <code>/setgems 50</code> или <code>/setgems 123456789 100</code>"
        )
        return

    target_id = message.from_user.id

    if len(args) == 1:
        try:
            gems = int(args[0])
        except ValueError:
            await message.reply("❌ <b>N должно быть числом.</b>")
            return
    elif len(args) == 2:
        try:
            target_id = int(args[0])
            gems = int(args[1])
        except ValueError:
            await message.reply("❌ <b>USERID и N должны быть числами.</b>")
            return
    else:
        await message.reply(
            "❌ <b>Слишком много аргументов.</b> Используйте /setgems [USERID] N"
        )
        return

    if target_id <= 0:
        await message.reply("❌ <b>Некорректный USERID.</b>")
        return
    if gems < 0:
        await message.reply("❌ <b>Баланс не может быть отрицательным.</b>")
        return

    async with get_db() as db:
        cur = await db.execute(
            "SELECT user_id, nickname, gems FROM users WHERE user_id = ?",
            (target_id,),
        )
        row = await cur.fetchone()
        if not row:
            await message.reply(
                f"❌ <b>Пользователь не найден в базе.</b>\n"
                f"Он должен хотя бы раз запустить бота (<code>/start</code>)."
            )
            return

        old_gems = row["gems"] or 0
        await db.execute(
            "UPDATE users SET gems = ? WHERE user_id = ?", (gems, target_id)
        )

    who = (
        "себе"
        if target_id == message.from_user.id
        else f"пользователю {esc(row['nickname'] or target_id)}"
    )
    await message.reply(
        f"✅ <b>Кристаллы обновлены</b> ({who}):\n"
        f"🆔 <code>{target_id}</code>\n"
        f"💎 Было: <b>{fmt_num(old_gems)}</b> → Стало: <b>{fmt_num(gems)}</b>"
    )


@router.message(Command("resetcd"), admin_filter)
async def admin_resetcd(message: Message, command: Command):
    if message.chat.type != "private":
        await message.reply("⚠️ Команда доступна только в личных сообщениях с ботом.")
        return

    args = (command.args or "").strip().split()
    target_id = message.from_user.id

    if len(args) >= 1:
        try:
            target_id = int(args[0])
        except ValueError:
            await message.reply("❌ <b>USERID должен быть числом.</b>")
            return
    if len(args) > 1:
        await message.reply("❌ <b>Слишком много аргументов.</b> Используйте /resetcd [USERID]")
        return

    if target_id <= 0:
        await message.reply("❌ <b>Некорректный USERID.</b>")
        return

    now = int(time.time())
    async with get_db() as db:
        cur = await db.execute(
            "SELECT user_id, nickname, last_claim FROM users WHERE user_id = ?",
            (target_id,),
        )
        row = await cur.fetchone()
        if not row:
            await message.reply(
                f"❌ <b>Пользователь не найден в базе.</b>\n"
                f"Он должен хотя бы раз запустить бота (<code>/start</code>)."
            )
            return

        old_last_claim = row["last_claim"] or 0
        await db.execute(
            "UPDATE users SET last_claim = 0 WHERE user_id = ?", (target_id,)
        )

    if old_last_claim == 0:
        cd_info = "кулдауна не было"
    else:
        remaining = max(0, COOLDOWN_SECONDS - (now - old_last_claim))
        if remaining > 0:
            h, m = remaining // 3600, (remaining % 3600) // 60
            cd_info = f"оставалось {h} ч {m} мин"
        else:
            cd_info = "кулдаун уже прошёл"

    who = "себе" if target_id == message.from_user.id else f"пользователю {esc(row['nickname'] or target_id)}"
    await message.reply(
        f"✅ <b>Кулдаун сброшен</b> ({who}):\n"
        f"🆔 <code>{target_id}</code>\n"
        f"⏱ {cd_info} — теперь можно получать карточку сразу."
    )


@router.message(Command("getusers"), admin_filter)
async def admin_getusers(message: Message):
    try:
        async with get_db() as db:
            cur = await db.execute(
                """SELECT u.user_id,
                          u.nickname,
                          u.coins,
                          u.streak,
                          u.role,
                          u.registration,
                          u.gender,
                          COALESCE(SUM(i.amount), 0) AS cards_count
                   FROM users u
                            LEFT JOIN inventory i ON u.user_id = i.user_id
                   GROUP BY u.user_id
                   ORDER BY u.registration DESC"""
            )
            users = await cur.fetchall()

        if not users:
            await message.reply("🔴 <b>В базе пока нет пользователей.</b>")
            return

        lines = [f"👥 <b>Пользователи</b> (всего: {fmt_num(len(users))})\n"]
        chunk = ""
        parts = []

        for u in users:
            g = GENDERS.get(u["gender"] or "none", GENDERS["none"])
            role_mark = "👑" if u["role"] == "admin" else "👤"
            reg_date = (
                datetime.fromtimestamp(u["registration"]).strftime("%d.%m.%Y")
                if u["registration"] else "—"
            )
            uid = u["user_id"]
            nick = u["nickname"] or f"User{uid}"
            line = (
                f"{role_mark} <b>{esc(nick)}</b> "
                f"(<code>{uid}</code>)\n"
                f"    🪙 {fmt_num(u['coins'] or 0)} | "
                f"🀄️ {fmt_num(u['cards_count'])} | "
                f"🔥 {fmt_num(u['streak'] or 0)} | "
                f"{g['icon']} | 📅 {reg_date}\n"
            )
            if len(chunk) + len(line) > 3500:
                parts.append(chunk)
                chunk = line
            else:
                chunk += line

        if chunk:
            parts.append(chunk)

        for i, part in enumerate(parts):
            header = lines[0] if i == 0 else f"👥 <b>Пользователи</b> (продолжение {i + 1}/{len(parts)})\n"
            await message.reply(header + part)
    except Exception as e:
        logger.error(f"Ошибка /getusers: {e}")
        await message.reply("❌ <b>Ошибка при получении списка пользователей.</b>")


@router.message(Command("delcard"), admin_filter)
async def admin_delcard(message: Message, command: Command):
    if message.chat.type != "private":
        await message.reply("⚠️ Команда доступна только в личных сообщениях с ботом.")
        return

    arg = (command.args or "").strip()
    if not arg:
        await message.reply(
            "✏️ Использование: <code>/delcard ID</code>\n"
            "Например: <code>/delcard 42</code>\n\n"
            "ID карточек можно посмотреть в <code>/admin</code> → 📜 Список карточек."
        )
        return

    try:
        card_id = int(arg)
    except ValueError:
        await message.reply("❌ <b>ID должен быть числом.</b>")
        return

    if card_id <= 0:
        await message.reply("❌ <b>Некорректный ID.</b>")
        return

    async with get_db() as db:
        cur = await db.execute(
            "SELECT id, name, rarity FROM cards WHERE id = ?", (card_id,)
        )
        card = await cur.fetchone()
        if not card:
            await message.reply(f"❌ <b>Карточка с ID {card_id} не найдена.</b>")
            return

        cur = await db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM inventory WHERE card_id = ?",
            (card_id,),
        )
        owned_total = (await cur.fetchone())[0]

        cur = await db.execute(
            "SELECT COUNT(*) FROM inventory WHERE card_id = ?", (card_id,)
        )
        owners = (await cur.fetchone())[0]

        await db.execute("DELETE FROM inventory WHERE card_id = ?", (card_id,))
        await db.execute("DELETE FROM cards WHERE id = ?", (card_id,))

    r_info = RARITIES.get(card["rarity"], {})
    await message.reply(
        f"🗑 <b>Карточка удалена</b>\n\n"
        f"🆔 <code>{card_id}</code>\n"
        f"🀄️ {esc(card['name'])}\n"
        f"{r_info.get('icon', '')} {r_info.get('name', card['rarity'])}\n\n"
        f"👥 Затронуто коллекционеров: <b>{fmt_num(owners)}</b>\n"
        f"📦 Удалено экземпляров: <b>{fmt_num(owned_total)}</b>"
    )


# ================= [TEST] ТЕСТОВЫЕ КОМАНДЫ =================

@router.message(Command("migrate_crystals_to_gems"), admin_filter)
async def test_migrate_crystals_to_gems(message: Message):
    """
    [TEST] Переименовывает колонку users.crystals → users.gems.
    Безопасно: если gems уже есть — просто копирует/удаляет crystals.
    Если crystals нет и gems есть — ничего не делает.
    """
    if message.chat.type != "private":
        await message.reply("⚠️ Только в ЛС.")
        return

    try:
        async with get_db() as db:
            cur = await db.execute("PRAGMA table_info(users)")
            cols = {row[1] for row in await cur.fetchall()}  # name is index 1

            has_crystals = "crystals" in cols
            has_gems = "gems" in cols

            if has_gems and not has_crystals:
                await message.reply(
                    "✅ <b>[TEST] Миграция не нужна</b>\n\n"
                    "Колонка <code>gems</code> уже есть, <code>crystals</code> отсутствует."
                )
                return

            if has_crystals and not has_gems:
                # SQLite 3.25+ поддерживает RENAME COLUMN
                try:
                    await db.execute(
                        "ALTER TABLE users RENAME COLUMN crystals TO gems"
                    )
                    await message.reply(
                        "✅ <b>[TEST] Миграция выполнена</b>\n\n"
                        "Колонка <code>users.crystals</code> переименована в "
                        "<code>users.gems</code>."
                    )
                    return
                except Exception as e:
                    # Fallback: создать gems, скопировать, удалить crystals через recreate
                    logger.warning(f"RENAME COLUMN failed, fallback: {e}")
                    await db.execute(
                        "ALTER TABLE users ADD COLUMN gems INTEGER DEFAULT 0"
                    )
                    await db.execute(
                        "UPDATE users SET gems = COALESCE(crystals, 0)"
                    )
                    # SQLite cannot DROP COLUMN easily on old versions — leave crystals
                    await message.reply(
                        "✅ <b>[TEST] Миграция (fallback)</b>\n\n"
                        "Добавлена колонка <code>gems</code>, данные скопированы из "
                        "<code>crystals</code>.\n"
                        f"Примечание: старую колонку <code>crystals</code> не удалось "
                        f"удалить автоматически ({esc(str(e))})."
                    )
                    return

            if has_crystals and has_gems:
                # Обе есть — сливаем: gems = COALESCE(gems, crystals, 0)
                await db.execute(
                    "UPDATE users SET gems = COALESCE(gems, crystals, 0)"
                )
                await message.reply(
                    "✅ <b>[TEST] Миграция (merge)</b>\n\n"
                    "Обе колонки существовали. Данные слиты в <code>gems</code> "
                    "(<code>gems = COALESCE(gems, crystals, 0)</code>).\n"
                    "Колонку <code>crystals</code> при желании можно удалить вручную."
                )
                return

            # Нет ни crystals, ни gems
            await db.execute(
                "ALTER TABLE users ADD COLUMN gems INTEGER DEFAULT 0"
            )
            await message.reply(
                "✅ <b>[TEST] Колонка gems создана</b>\n\n"
                "Ни <code>crystals</code>, ни <code>gems</code> не было — "
                "добавлена пустая <code>gems INTEGER DEFAULT 0</code>."
            )
    except Exception as e:
        logger.error(f"migrate_crystals_to_gems error: {e}")
        await message.reply(f"❌ <b>Ошибка миграции:</b> {esc(str(e))}")


@router.message(Command("reset_all_nicknames"), admin_filter)
async def test_reset_all_nicknames(message: Message):
    async with get_db() as db:
        cur = await db.execute("SELECT user_id FROM users")
        rows = await cur.fetchall()

    bot: Bot = message.bot
    count = 0
    for row in rows:
        uid = row["user_id"]
        full_name = None
        username = None
        try:
            chat = await bot.get_chat(uid)
            full_name = chat.full_name
            username = chat.username
        except Exception as e:
            logger.warning(f"[TEST] get_chat failed for {uid}: {e}")

        new_nick = default_nickname(username, full_name, uid)
        async with get_db() as db:
            await db.execute(
                "UPDATE users SET nickname = ? WHERE user_id = ?",
                (new_nick, uid),
            )
        count += 1

    await message.answer(f"✅ <b>[TEST] Сброшено ников:</b> {count}")


@router.message(Command("promote_to_mythical"), admin_filter)
async def test_promote_to_mythical(message: Message):
    async with get_db() as db:
        cur = await db.execute(
            "SELECT id FROM cards WHERE rarity IN ('epic', 'legendary') ORDER BY RANDOM() LIMIT 10"
        )
        ids = [r["id"] for r in await cur.fetchall()]
        if ids:
            placeholders = ",".join("?" * len(ids))
            await db.execute(
                f"UPDATE cards SET rarity = 'mythical' WHERE id IN ({placeholders})",
                ids,
            )
    await message.answer(f"✅ <b>[TEST] Переведено в мифические:</b> {len(ids)} карточек")


@router.message(Command("getfileid"), admin_filter, F.photo)
async def test_get_file_id(message: Message):
    file_id = message.photo[-1].file_id
    await message.reply(
        f"🆔 <b>file_id:</b>\n<code>{esc(file_id)}</code>"
    )


@router.message(Command("denominate_coins"), admin_filter)
async def test_denominate_coins(message: Message):
    async with get_db() as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        total = (await cur.fetchone())[0]

        await db.execute("UPDATE users SET coins = coins / 10")

        cur = await db.execute("SELECT COALESCE(SUM(coins), 0) FROM users")
        new_total_coins = (await cur.fetchone())[0]

    await message.answer(
        f"✅ <b>[TEST] Деноминация выполнена</b>\n\n"
        f"👥 Пользователей: <b>{total}</b>\n"
        f"🪙 Суммарный баланс после: <b>{new_total_coins}</b>"
    )


@router.message(Command("set_registration_now"), admin_filter)
async def test_set_registration_now(message: Message):
    now = int(time.time())
    async with get_db() as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM users WHERE registration = 0 OR registration IS NULL"
        )
        affected = (await cur.fetchone())[0]

        await db.execute(
            "UPDATE users SET registration = ? "
            "WHERE registration = 0 OR registration IS NULL",
            (now,),
        )

    await message.answer(
        f"✅ <b>[TEST] Дата регистрации установлена</b>\n\n"
        f"👥 Обновлено пользователей: <b>{fmt_num(affected)}</b>\n"
        f"🕐 Время: <b>{datetime.fromtimestamp(now).strftime('%d.%m.%Y %H:%M:%S')}</b>"
    )


# ================= ЗАПУСК =================
async def main():
    try:
        os.makedirs(os.path.dirname(DB_NAME) or ".", exist_ok=True)
        if LOG_PATH:
            os.makedirs(os.path.dirname(LOG_PATH) or ".", exist_ok=True)
        await init_db()
        logger.info("База данных инициализирована")

        bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML,
                                                                link_preview=LinkPreviewOptions(is_disabled=True), ))
        dp = Dispatcher(storage=MemoryStorage())
        dp.include_router(router)

        logger.info("🤖 Бот запущен")
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
