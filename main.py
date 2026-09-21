import asyncio
import html
import logging
import os
import random
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional, Tuple, List

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, BotCommandScopeType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, BaseFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand, BotCommandScopeChat, BotCommandScopeDefault,
    CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, InputMediaPhoto, KeyboardButton, Message,
    ReplyKeyboardMarkup, LinkPreviewOptions, WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

# Фото карточек на диск (мини-аппка + надёжное хранение)
try:
    from card_photos import sync_card_photo, migrate_all_card_photos, ensure_photo_dir
except ImportError:
    sync_card_photo = None
    migrate_all_card_photos = None
    def ensure_photo_dir():
        pass


# ================= КОНФИГУРАЦИЯ =================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN не задан. Укажите его в .env или окружении.")

# URL мини-аппки (Vercel / GitHub Pages). Пусто = кнопка Web App не показывается.
WEBAPP_URL = (os.getenv("WEBAPP_URL") or "").strip()


DB_NAME = os.getenv("DB_NAME", "/app/data/cards_game.db")
LOG_PATH = os.getenv("LOG_PATH", "/app/data/bot.log")
COOLDOWN_SECONDS = 4 * 3600
INSTANT_COST = 150
INSTANT_MIN_COST = 5
NICKNAME_COST = 100
DUPLICATE_CHANCE = 0.25
DUPLICATE_REFUND = 0.5

GEM_TO_COINS = 100
MARKET_PRICES = {
    "common": 1,
    "rare": 3,
    "epic": 8,
    "mythical": 20,
    "legendary": 50,
}
GEM_REWARDS = {
    "mythical": 1,
    "legendary": 2,
}
STREAK_GEM_BONUSES = [(7, 5), (30, 20)]

GROUP_AUTODELETE_SECONDS = 30
STREAK_EXPIRE_SECONDS = 24 * 3600

DEFAULT_AVATAR_FILE_ID = "AgACAgIAAxkBAAID12qql3EFpnb2HwTCE7Yn_Ri1TQsNAAKRIGsbfHlYSVUpxfU75O60AQADAgADeAADPQQ"

DICE_COOLDOWN_SECONDS = 10 * 60
DICE_MIN_BALANCE = 10
DICE_SPIN_DELAY = 3

RARITIES = {
    "common": {"icon": "⚪", "name": "Обычная", "weight": 50, "reward": 10},
    "rare": {"icon": "🔵", "name": "Редкая", "weight": 20, "reward": 25},
    "epic": {"icon": "🟣", "name": "Эпическая", "weight": 15, "reward": 50},
    "mythical": {"icon": "🔴", "name": "Мифическая", "weight": 10, "reward": 75},
    "legendary": {"icon": "🟡", "name": "Легендарная", "weight": 5, "reward": 100},
}

GENDERS = {
    "male": {"icon": "♂", "name": "Мужской"},
    "female": {"icon": "♀", "name": "Женский"},
    "other": {"icon": "⚧", "name": "Другой"},
    "none": {"icon": "—", "name": "Не задан"},
}

# Роли: user | admin | superadmin | banned
ROLES = {
    "user": {"icon": "👤", "name": "Пользователь"},
    "admin": {"icon": "🛡", "name": "Администратор"},
    "superadmin": {"icon": "👑", "name": "Главный администратор"},
    "banned": {"icon": "🚫", "name": "Заблокирован"},
}

STREAK_BONUSES = [(2, 15), (7, 20), (14, 25), (30, 30), (float("inf"), 35)]

NICKNAME_RE = re.compile(r"^[\w\-. ]{2,32}$", re.UNICODE)
URL_RE = re.compile(r"(https?://|t\.me/|@\w+)", re.IGNORECASE)

DICE_CMD_RE = re.compile(r"^мряу\s+кубик\s*$", re.IGNORECASE | re.UNICODE)
DISABLED_SLOT_RE = re.compile(r"^мряу\s+ставка\b", re.IGNORECASE | re.UNICODE)
DISABLED_TRANSFER_RE = re.compile(r"^мряу\s+перевод\b", re.IGNORECASE | re.UNICODE)
PROFILE_CMD_RE = re.compile(r"^мряу\s+профиль\s*$", re.IGNORECASE | re.UNICODE)
MARKET_CMD_RE = re.compile(r"^мряу\s+маркет\s*$", re.IGNORECASE | re.UNICODE)
COLLECTION_CMD_RE = re.compile(r"^мряу\s+(коллекция|карточки)\s*$", re.IGNORECASE | re.UNICODE)
TOP_CMD_RE = re.compile(r"^мряу\s+топ\s*$", re.IGNORECASE | re.UNICODE)
HELP_CMD_RE = re.compile(r"^мряу\s+помощь\s*$", re.IGNORECASE | re.UNICODE)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


# ================= ХЕЛПЕРЫ =================
def esc(text) -> str:
    return html.escape(str(text), quote=False)


def fmt_num(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    return f"{n:,}".replace(",", "\u00a0")


def plural(n: int, one: str, few: str, many: str) -> str:
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
    return f"{fmt_num(n)} {plural(n, 'день', 'дня', 'дней')}"


def fmt_cards(n: int) -> str:
    return f"{fmt_num(n)} {plural(n, 'карточка', 'карточки', 'карточек')}"


def fmt_coins(n: int) -> str:
    return f"{fmt_num(n)} {plural(n, 'монета', 'монеты', 'монет')}"


def fmt_gems(n: int) -> str:
    return f"{fmt_num(n)} {plural(n, 'кристалл', 'кристалла', 'кристаллов')}"


def user_mention(user_id: int, nickname: str, username: Optional[str] = None) -> str:
    safe = esc(nickname)
    if username:
        return f'<a href="https://t.me/{esc(username)}">{safe}</a>'
    return f'<a href="tg://user?id={user_id}">{safe}</a>'


def instant_cost(remaining_seconds: int) -> int:
    if remaining_seconds <= 0:
        return INSTANT_MIN_COST
    if remaining_seconds >= COOLDOWN_SECONDS:
        return INSTANT_COST
    ratio = remaining_seconds / COOLDOWN_SECONDS
    cost = INSTANT_MIN_COST + (INSTANT_COST - INSTANT_MIN_COST) * ratio
    return max(INSTANT_MIN_COST, min(INSTANT_COST, round(cost)))


def roll_dice_coins() -> int:
    values = list(range(-10, 11))
    weights = []
    for v in values:
        if v > 0:
            weights.append(4 + v)
        elif v == 0:
            weights.append(6)
        else:
            weights.append(2)
    return random.choices(values, weights=weights, k=1)[0]


def role_display(role: str) -> str:
    info = ROLES.get(role, ROLES["user"])
    return f"{info['icon']}  {info['name']}"


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
    kind: str


class NickConfirmCallback(CallbackData, prefix="nickconf"):
    action: str


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
    filter_role: str = "all"  # all | user | admin | banned


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
    action: str
    amount: int = 0
    user_id: int = 0


class MarketMainCallback(CallbackData, prefix="mkt_main"):
    user_id: int = 0


class OkDeleteCallback(CallbackData, prefix="ok_del"):
    pass


class HelpCallback(CallbackData, prefix="help"):
    page: int = 0


class TopRefreshCallback(CallbackData, prefix="top_refresh"):
    kind: str


class AdminHelpCallback(CallbackData, prefix="admin_help"):
    page: int = 0


class AdminMainCallback(CallbackData, prefix="admin_main_cb"):
    pass


class AdminConfirmCallback(CallbackData, prefix="admin_confirm"):
    action: str
    target_id: int = 0
    value: str = ""


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
                "CREATE INDEX IF NOT EXISTS idx_users_role ON users(role)",
        ):
            await db.execute(sql)

        for table, column, definition in [
            ("users", "registration", "INTEGER DEFAULT 0"),
            ("users", "streak", "INTEGER DEFAULT 0"),
            ("users", "last_streak_date", "INTEGER DEFAULT 0"),
            ("users", "streak_bonus", "INTEGER DEFAULT 0"),
            ("users", "gender", "TEXT DEFAULT 'none'"),
            ("users", "gems", "INTEGER DEFAULT 0"),
            ("users", "last_dice", "INTEGER DEFAULT 0"),
            ("inventory", "claim_time", "INTEGER DEFAULT 0"),
            ("inventory", "amount", "INTEGER DEFAULT 1"),
            ("cards", "photo_path", "TEXT"),
        ]:
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            except aiosqlite.OperationalError:
                pass


# ================= РОЛИ И ПРАВА =================
async def get_user_role(user_id: int) -> str:
    try:
        async with get_db() as db:
            cur = await db.execute("SELECT role FROM users WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            if not row:
                return "user"
            role = row[0] or "user"
            if role not in ROLES:
                return "user"
            return role
    except Exception as e:
        logger.error(f"Ошибка get_user_role: {e}")
        return "user"


async def is_admin(user_id: int) -> bool:
    """Админ или главный админ."""
    role = await get_user_role(user_id)
    return role in ("admin", "superadmin")


async def is_superadmin(user_id: int) -> bool:
    return (await get_user_role(user_id)) == "superadmin"


async def is_banned(user_id: int) -> bool:
    return (await get_user_role(user_id)) == "banned"


async def set_user_role(user_id: int, role: str) -> bool:
    if role not in ROLES:
        return False
    async with get_db() as db:
        cur = await db.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        if not await cur.fetchone():
            return False
        await db.execute("UPDATE users SET role = ? WHERE user_id = ?", (role, user_id))
    return True


def default_nickname(username: Optional[str], full_name: Optional[str], user_id: int) -> str:
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
            # Первый пользователь в БД → главный администратор
            cur = await db.execute("SELECT COUNT(*) FROM users")
            total = (await cur.fetchone())[0]
            role = "superadmin" if total == 0 else "user"
            await db.execute(
                "INSERT INTO users (user_id, nickname, registration, streak, last_streak_date, role) "
                "VALUES (?, ?, ?, 0, 0, ?)",
                (user_id, nickname, now, role),
            )
            if role == "superadmin":
                logger.info(f"Первый пользователь {user_id} назначен главным администратором")
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
    if target_user_id and callback.from_user.id != target_user_id:
        return False
    return True


# ================= КОМАНДЫ МЕНЮ (динамические) =================
USER_COMMANDS = [
    BotCommand(command="start", description="👋 Запуск бота"),
    BotCommand(command="meow", description="🃏 Получить карточку"),
    BotCommand(command="profile", description="👤 Профиль"),
    BotCommand(command="collection", description="🃏 Моя коллекция"),
    BotCommand(command="market", description="🛒 Маркет"),
    BotCommand(command="top", description="🏆 Топ игроков"),
    BotCommand(command="help", description="❓ Помощь"),
    BotCommand(command="nickname", description="✏️ Сменить ник"),
    BotCommand(command="gender", description="⚧ Выбрать пол"),
]

ADMIN_EXTRA_COMMANDS = [
    BotCommand(command="admin", description="⚙️ Админ-панель"),
    BotCommand(command="adminhelp", description="🛠 Справка админа"),
    BotCommand(command="stats", description="📊 Статистика бота"),
]

SUPERADMIN_EXTRA_COMMANDS = [
    BotCommand(command="setadmin", description="👑 Назначить админа"),
    BotCommand(command="unsetadmin", description="❌ Разжаловать админа"),
    BotCommand(command="ban", description="🚫 Заблокировать"),
    BotCommand(command="unban", description="✅ Разблокировать"),
]


async def update_user_commands(bot: Bot, user_id: int, role: Optional[str] = None):
    """Обновляет меню команд для конкретного пользователя в зависимости от роли."""
    if role is None:
        role = await get_user_role(user_id)
    try:
        cmds = list(USER_COMMANDS)
        if role in ("admin", "superadmin"):
            cmds.extend(ADMIN_EXTRA_COMMANDS)
        if role == "superadmin":
            cmds.extend(SUPERADMIN_EXTRA_COMMANDS)
        scope = BotCommandScopeChat(chat_id=user_id)
        await bot.set_my_commands(cmds, scope=scope)
    except Exception as e:
        logger.debug(f"Не удалось обновить команды для {user_id}: {e}")


async def refresh_keyboard_and_commands(bot: Bot, message_or_callback, user_id: int):
    """После смены роли обновляет команды. Клавиатуру пользователь получит при /start или кнопке."""
    role = await get_user_role(user_id)
    await update_user_commands(bot, user_id, role)


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
    """Красивое главное меню админ-панели."""
    b = InlineKeyboardBuilder()
    b.button(text="➕  Добавить карточку", callback_data="admin_add_card")
    b.button(text="📜  Список карточек", callback_data=AdminCardPageCallback(page=0).pack())
    b.button(text="👥  Пользователи", callback_data=AdminUserPageCallback(page=0, filter_role="all").pack())
    b.button(text="📊  Статистика", callback_data="admin_stats_quick")
    b.button(text="🛠  Справка админа", callback_data=AdminHelpCallback(page=0).pack())
    b.adjust(1)
    return b.as_markup()


def get_profile_kb(owner_id: int):
    b = InlineKeyboardBuilder()
    b.button(text="🃏  Моя коллекция", callback_data=MainMenuCallback(user_id=owner_id).pack())
    b.button(text="⚧  Выбрать пол", callback_data=GenderCallback(value="menu", user_id=owner_id).pack())
    b.button(text=f"✏️  Сменить ник · {NICKNAME_COST} 🪙",
             callback_data=NicknameCallback(action="change", user_id=owner_id).pack())
    b.button(
        text="💎  Купить кристаллы",
        callback_data=MarketExchangeCallback(action="menu", user_id=owner_id).pack(),
    )
    b.adjust(1)
    return b.as_markup()


def get_gender_kb(current: str = "none", owner_id: int = 0) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key, info in GENDERS.items():
        mark = "● " if key == current else "○ "
        b.button(
            text=f"{mark}{info['icon']}  {info['name']}",
            callback_data=GenderCallback(value=key, user_id=owner_id).pack(),
        )
    b.button(text="‹  Назад в профиль", callback_data=BackToProfileCallback(user_id=owner_id).pack())
    b.adjust(1)
    return b.as_markup()


def get_main_km(is_staff: bool = False) -> ReplyKeyboardMarkup:
    """Основная клавиатура. Для админов/суперадминов — с кнопкой «Админ-панель»."""
    rows = [
        [KeyboardButton(text="🃏 Получить карточку"), KeyboardButton(text="👤 Профиль")],
        [KeyboardButton(text="🛒 Маркет"), KeyboardButton(text="🏆 Топ")],
    ]
    if is_staff:
        rows.append([KeyboardButton(text="⚙️ Админ-панель"), KeyboardButton(text="❓ Помощь")])
    else:
        rows.append([KeyboardButton(text="❓ Помощь")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
    )


def _instant_button(b: InlineKeyboardBuilder, user_id: int, label: str,
                    action: str, cost: int = INSTANT_COST):
    b.button(
        text=f"{label} · {fmt_num(cost)} 🪙",
        callback_data=CardActionCallback(action=action, user_id=user_id).pack(),
    )


def get_ok_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Понятно ✓", callback_data=OkDeleteCallback().pack())
    return b.as_markup()


def get_card_action_keyboard(user_id: int, balance: int = 0,
                             remaining: int = COOLDOWN_SECONDS) -> InlineKeyboardMarkup:
    cost = instant_cost(remaining)
    b = InlineKeyboardBuilder()
    if balance >= cost:
        _instant_button(b, user_id, "⚡ Получить сейчас", "instant", cost)
    b.button(text="Понятно ✓", callback_data=OkDeleteCallback().pack())
    b.adjust(1)
    return b.as_markup()


def get_after_card_keyboard(user_id: int, balance: int = 0) -> InlineKeyboardMarkup:
    cost = instant_cost(COOLDOWN_SECONDS)
    b = InlineKeyboardBuilder()
    if balance >= cost:
        _instant_button(b, user_id, "⚡ Ещё одну", "another", cost)
    b.button(
        text="🃏  Моя коллекция",
        callback_data=CardActionCallback(action="collection", user_id=user_id).pack(),
    )
    b.adjust(1)
    return b.as_markup()


def get_top_keyboard(kind: str = "coins") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=("● " if kind == "coins" else "○ ") + "🪙 Монеты",
             callback_data=TopCallback(kind="coins").pack())
    b.button(text=("● " if kind == "cards" else "○ ") + "🃏 Карты",
             callback_data=TopCallback(kind="cards").pack())
    b.button(text=("● " if kind == "streak" else "○ ") + "🔥 Стрик",
             callback_data=TopCallback(kind="streak").pack())
    b.button(text="↻  Обновить", callback_data=TopRefreshCallback(kind=kind).pack())
    b.adjust(3, 1)
    return b.as_markup()


# ================= ХЕЛПЕРЫ ОТОБРАЖЕНИЯ =================
async def get_user_photo(bot: Bot, user_id: int, nickname: str):
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if photos.total_count > 0:
            return photos.photos[0][-1].file_id
    except Exception as e:
        logger.error(f"Не удалось получить фото профиля: {e}")
    return DEFAULT_AVATAR_FILE_ID


async def render_profile(bot: Bot, user_id: int, viewer_id: Optional[int] = None):
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
    role_display_str = role_display(role)

    gender = row["gender"] or "none"
    g = GENDERS.get(gender, GENDERS["none"])
    gender_display = f"{g['icon']} {g['name']}"

    gems = row["gems"] if row["gems"] is not None else 0

    caption = (
        f"╭ <b>{esc(nickname)}</b>\n"
        f"╰ <code>{user_id}</code>\n\n"
        f"🎭  {role_display_str}\n"
        f"⚧  {gender_display}\n"
        f"📅  с <b>{reg_date}</b>\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🃏  Карточки  ·  <b>{fmt_num(row['cards_count'])}</b> / {fmt_num(total_cards)}\n"
        f"🪙  Монеты     ·  <b>{fmt_num(row['coins'])}</b>\n"
        f"💎  Кристаллы  ·  <b>{fmt_num(gems)}</b>\n"
        f"🔥  Стрик      ·  <b>{fmt_days(row['streak'])}</b>"
    )
    kb = get_profile_kb(user_id)
    return await get_user_photo(bot, user_id, nickname), caption, kb


async def render_collection(bot: Bot, user_id: int):
    keyboard, total, total_in_game = await get_collection_main_keyboard(user_id)
    nickname = await get_user_nickname(user_id)
    photo = await get_user_photo(bot, user_id, nickname)
    caption = (
        f"🃏  <b>Коллекция</b>\n"
        f"╭ {esc(nickname)}\n"
        f"╰ <b>{fmt_num(total)}</b> из {fmt_num(total_in_game)} карточек"
    )
    return photo, caption, keyboard, total


async def show_or_edit_photo(message: Message, photo, caption: str, keyboard=None):
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


# ================= RATE LIMIT =================
_rate_bucket: dict = {}
_RATE_BUCKET_MAX_KEYS = 10_000


def rate_limited(key: str, limit: int, window: float) -> bool:
    now = time.monotonic()
    bucket = _rate_bucket.setdefault(key, [])
    bucket[:] = [t for t in bucket if now - t < window]
    if len(bucket) >= limit:
        return True
    bucket.append(now)
    if len(_rate_bucket) > _RATE_BUCKET_MAX_KEYS:
        dead = [k for k, v in _rate_bucket.items() if not v]
        for k in dead[: len(dead) // 2 + 1]:
            _rate_bucket.pop(k, None)
    return False


# ================= АВТО-УДАЛЕНИЕ =================
async def _try_delete(msg: Optional[Message]):
    if msg is None:
        return
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass
    except Exception as e:
        logger.debug(f"try_delete error: {e}")


async def _auto_delete_pair(bot_msg: Message, user_msg: Optional[Message], delay: int):
    await asyncio.sleep(delay)
    await _try_delete(bot_msg)
    await _try_delete(user_msg)


async def reply_ephemeral(message: Message, text: str, **kwargs):
    if "reply_markup" not in kwargs:
        kwargs["reply_markup"] = get_ok_kb()
    sent = await message.reply(text, **kwargs)
    if message.chat.type != "private":
        asyncio.create_task(
            _auto_delete_pair(sent, message, GROUP_AUTODELETE_SECONDS)
        )
    return sent


async def answer_ephemeral(message: Message, text: str, **kwargs):
    if "reply_markup" not in kwargs:
        kwargs["reply_markup"] = get_ok_kb()
    sent = await message.answer(text, **kwargs)
    if message.chat.type != "private":
        asyncio.create_task(
            _auto_delete_pair(sent, message, GROUP_AUTODELETE_SECONDS)
        )
    return sent


# ================= БАН-ПРОВЕРКА =================
async def check_not_banned(message: Message) -> bool:
    """Возвращает False если пользователь заблокирован (и уже ответил)."""
    if await is_banned(message.from_user.id):
        await reply_ephemeral(
            message,
            "🚫  <b>Вы заблокированы</b>\n\n"
            "Доступ к боту ограничен. Если это ошибка — обратитесь к администрации.",
        )
        return False
    return True


async def check_not_banned_cb(callback: CallbackQuery) -> bool:
    if await is_banned(callback.from_user.id):
        await callback.answer("🚫 Вы заблокированы", show_alert=True)
        return False
    return True


# ================= ФИЛЬТРЫ =================
class AdminFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return await is_admin(message.from_user.id)


class SuperAdminFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return await is_superadmin(message.from_user.id)


admin_filter = AdminFilter()
superadmin_filter = SuperAdminFilter()


# ================= ПОЛЬЗОВАТЕЛЬСКИЕ ХЕНДЛЕРЫ =================
@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message):
    try:
        await get_or_create_user(
            message.from_user.id, message.from_user.username, message.from_user.full_name
        )
        if not await check_not_banned(message):
            return

        role = await get_user_role(message.from_user.id)
        is_staff = role in ("admin", "superadmin")

        # Обновляем меню команд под роль
        await update_user_commands(message.bot, message.from_user.id, role)

        try:
            await message.answer_sticker(
                sticker="CAACAgIAAxkBAALL7WqWuuWDYuQk4iqY7tNu_-7zLZqyAAJengACoj5pSQH9iX-5QhicPQQ"
            )
        except Exception as e:
            logger.warning(f"sticker error: {e}")

        welcome = (
            "👋  <b>Привет!</b>\n\n"
            "Напишите <b>«мряу»</b> или нажмите кнопку ниже —\n"
            "и получите милую карточку.\n\n"
            "<i>Бесплатно раз в 4 часа · мгновенно за монеты</i>"
        )
        if is_staff:
            welcome += f"\n\n{role_display(role)} — доступна кнопка «⚙️ Админ-панель»."

        await message.reply(welcome, reply_markup=get_main_km(is_staff=is_staff))
        if WEBAPP_URL:
            try:
                wa_kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="🃏 Открыть мини-аппку",
                        web_app=WebAppInfo(url=WEBAPP_URL),
                    )
                ]])
                await message.answer(
                    "Удобный просмотр коллекции и топа — в мини-аппке:",
                    reply_markup=wa_kb,
                )
            except Exception as e:
                logger.warning(f"webapp button: {e}")
    except Exception as e:
        logger.error(f"Ошибка в cmd_start: {e}")


HELP_PAGES = [
    {
        "title": "📖  Команды",
        "body": (
            "<b>🃏  Получить карточку</b>\n"
            "<blockquote>"
            "«мряу» · /meow · кнопка «🃏 Получить карточку»\n"
            f"Бесплатно — раз в 4 часа\n"
            f"Мгновенно — от {INSTANT_MIN_COST} до {INSTANT_COST} 🪙 "
            "(цена падает по мере истечения таймера)"
            "</blockquote>\n\n"
            "<b>👤  Профиль</b>\n"
            "<blockquote>"
            "«мряу профиль» · /profile · «👤 Профиль»\n"
            "В группе — ответом на сообщение можно открыть чужой профиль"
            "</blockquote>\n\n"
            "<b>🃏  Коллекция</b>\n"
            "<blockquote>"
            "«мряу коллекция» · «мряу карточки» · /collection\n"
            "Только своя коллекция"
            "</blockquote>\n\n"
            "<b>🛒  Маркет</b>\n"
            "<blockquote>"
            "«🛒 Маркет» · /market · «мряу маркет»\n"
            "<b>Только в личных сообщениях</b>"
            "</blockquote>"
        ),
    },
    {
        "title": "📖  Стрик и награды",
        "body": (
            "<b>🔥  Стрик</b>\n"
            "<blockquote>"
            "Заходите каждый день и получайте карточку — стрик растёт.\n"
            "Пропуск 24 часов — стрик сбрасывается.\n"
            "За стрик начисляются бонусные монеты."
            "</blockquote>\n\n"
            "<b>💎  Кристаллы за стрик</b>\n"
            "<blockquote>"
            "Один раз при достижении порога:\n"
            "·  7 дней  →  +5 💎\n"
            "·  30 дней →  +20 💎"
            "</blockquote>\n\n"
            "<b>💎  Кристаллы за карточки</b>\n"
            "<blockquote>"
            "·  Новая мифическая  →  +1 💎\n"
            "·  Новая легендарная →  +2 💎\n"
            "Дубликаты кристаллы не дают."
            "</blockquote>"
        ),
    },
    {
        "title": "📖  Маркет и кубик",
        "body": (
            "<b>🛒  Покупка карточек</b>\n"
            "<blockquote>"
            "В маркете (только ЛС) выберите редкость\n"
            "и купите недостающую карточку за кристаллы."
            "</blockquote>\n\n"
            "<b>💎  Цены</b>\n"
            "<blockquote>"
            + "\n".join(
                f"{v['icon']}  {v['name']}  ·  {MARKET_PRICES[k]} 💎"
                for k, v in RARITIES.items()
            )
            + "</blockquote>\n\n"
            "<b>💱  Обмен на кристаллы</b>\n"
            f"<blockquote>"
            f"Маркет или профиль → «Купить кристаллы»\n"
            f"Курс:  1 💎  =  {GEM_TO_COINS} 🪙"
            "</blockquote>\n\n"
            "<b>🎲  Кубик</b>\n"
            f"<blockquote>"
            f"«мряу кубик» · раз в 10 мин · от {DICE_MIN_BALANCE} 🪙\n"
            "Выпадает от −10 до +10 монет (шанс выигрыша выше)"
            "</blockquote>"
        ),
    },
    {
        "title": "📖  Профиль и топ",
        "body": (
            "<b>✏️  Смена ника</b>\n"
            f"<blockquote>"
            f"/nickname НовыйНик  ·  {NICKNAME_COST} 🪙\n"
            "/nickname reset  ·  бесплатно\n"
            "2–32 символа, без ссылок и @упоминаний"
            "</blockquote>\n\n"
            "<b>⚧  Пол</b>\n"
            "<blockquote>"
            "/gender м|ж|др|нет  или кнопка в профиле\n"
            "Необязательное поле"
            "</blockquote>\n\n"
            "<b>🏆  Топ</b>\n"
            "<blockquote>"
            "«мряу топ» · /top · «🏆 Топ»\n"
            "Переключение: монеты / карточки / стрик"
            "</blockquote>\n\n"
            "<b>🃏  Редкости</b>\n"
            "<blockquote>"
            + "\n".join(
                f"{v['icon']}  {v['name']}  ·  {v['reward']} 🪙"
                for v in RARITIES.values()
            )
            + "</blockquote>"
        ),
    },
]


def get_help_keyboard(page: int) -> InlineKeyboardMarkup:
    total = len(HELP_PAGES)
    page = max(0, min(page, total - 1))
    b = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            text="‹  Назад", callback_data=HelpCallback(page=page - 1).pack()
        ))
    nav.append(InlineKeyboardButton(
        text=f"{page + 1} / {total}", callback_data="ignore"
    ))
    if page < total - 1:
        nav.append(InlineKeyboardButton(
            text="Далее  ›", callback_data=HelpCallback(page=page + 1).pack()
        ))
    b.row(*nav)
    return b.as_markup()


def build_help_text(page: int) -> str:
    total = len(HELP_PAGES)
    page = max(0, min(page, total - 1))
    p = HELP_PAGES[page]
    return f"<b>{p['title']}</b>\n<code>{'─' * 18}</code>\n\n{p['body']}"


@router.message(Command("help"))
@router.message(F.text == "❓ Помощь")
@router.message(F.text.regexp(HELP_CMD_RE))
async def cmd_help(message: Message):
    if not await check_not_banned(message):
        return
    await message.reply(
        build_help_text(0),
        reply_markup=get_help_keyboard(0),
    )


@router.callback_query(HelpCallback.filter())
async def help_page_callback(callback: CallbackQuery, callback_data: HelpCallback):
    if not await check_not_banned_cb(callback):
        return
    try:
        text = build_help_text(callback_data.page)
        kb = get_help_keyboard(callback_data.page)
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                await callback.answer("Данные не изменились")
            else:
                await callback.message.answer(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка справки: {e}")
        await callback.answer("⚠️ Ошибка")


@router.message(F.text == "🃏 Получить карточку")
@router.message(F.text == "🀄️ Получить карточку")
@router.message(F.text.lower().strip() == "мряу")
@router.message(F.text.lower().strip() == "милость")
@router.message(Command("meow"))
async def get_card_handler(message: Message):
    if not await check_not_banned(message):
        return
    user_id = message.from_user.id
    now = int(time.time())

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
                f"⏳  <b>{mention}</b>, подождите немного\n\n"
                f"Следующая карточка через  <b>{time_str}</b>"
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
    if bonus > 0 and streak == 1:
        return (
            "\n\n<blockquote expandable>"
            "🔥 <b>Стрик начат!</b>\n"
            "Заходите каждый день — стрик растёт, а вместе с ним и награды."
            "</blockquote>"
        )
    if bonus > 0 and streak >= 2:
        text = (
            f"\n\n<blockquote expandable>"
            f"🔥 Стрик  ·  <b>{fmt_days(streak)}</b>\n"
            f"🪙 Бонус  ·  <b>+{fmt_num(bonus)}</b>  →  {fmt_num(new_balance)}"
        )
        if gem_bonus > 0:
            text += f"\n💎 Бонус  ·  <b>+{fmt_num(gem_bonus)}</b>"
        text += (
            "\n\n💡 Заходите ежедневно, чтобы не потерять стрик."
            "</blockquote>"
        )
        return text
    return ""


def _card_caption(mention: str, card: dict) -> str:
    r = RARITIES[card["rarity"]]
    if card.get("is_duplicate"):
        title = "🔁  <b>Дубликат</b>"
    else:
        title = "✨  <b>Новая карточка</b>"
    text = (
        f"{title}\n"
        f"╭ <b>{esc(card['name'])}</b>\n"
        f"╰ {r['icon']}  {r['name']}\n\n"
        f"🪙  +<b>{fmt_num(card['coins_earned'])}</b>  →  {fmt_num(card['balance'])}"
    )
    gems_earned = card.get("gems_earned") or 0
    if gems_earned > 0:
        gems_bal = card.get("gems") or 0
        text += (
            f"\n💎  +<b>{fmt_num(gems_earned)}</b>  →  {fmt_num(gems_bal)}"
        )
    return text


@router.message(F.text.regexp(DISABLED_SLOT_RE))
async def disabled_slot_handler(message: Message):
    if not await check_not_banned(message):
        return
    await reply_ephemeral(
        message,
        "⚠️ <b>Команда отключена.</b> Обратитесь к системному администратору",
    )


@router.message(F.text.regexp(DISABLED_TRANSFER_RE))
async def disabled_transfer_handler(message: Message):
    if not await check_not_banned(message):
        return
    await reply_ephemeral(
        message,
        "⚠️ <b>Команда отключена.</b> Обратитесь к системному администратору",
    )


# ================= КУБИК =================
@router.message(F.text.regexp(DICE_CMD_RE))
async def dice_handler(message: Message):
    if not await check_not_banned(message):
        return
    user_id = message.from_user.id

    if rate_limited(f"dice:{user_id}", limit=6, window=15):
        await reply_ephemeral(message, "⏳ Слишком часто. Подождите немного.")
        return

    if message.chat.type != "private":
        if rate_limited(f"dice-chat:{message.chat.id}", limit=15, window=15):
            return

    try:
        await get_or_create_user(
            user_id, message.from_user.username, message.from_user.full_name
        )
        nickname = await get_user_nickname(user_id)
        mention = user_mention(user_id, nickname, message.from_user.username)
        now = int(time.time())

        async with get_db() as db:
            cur = await db.execute(
                "SELECT coins, last_dice FROM users WHERE user_id = ?", (user_id,)
            )
            row = await cur.fetchone()
            balance = (row["coins"] or 0) if row else 0
            last_dice = (row["last_dice"] or 0) if row else 0

            if balance < DICE_MIN_BALANCE:
                await reply_ephemeral(
                    message,
                    f"⚠️  <b>Недостаточно монет</b>\n\n"
                    f"Нужно минимум  <b>{fmt_num(DICE_MIN_BALANCE)} 🪙</b>\n"
                    f"У вас  ·  <b>{fmt_num(balance)} 🪙</b>",
                )
                return

            time_passed = now - last_dice
            if last_dice > 0 and time_passed < DICE_COOLDOWN_SECONDS:
                remaining = DICE_COOLDOWN_SECONDS - time_passed
                m, s = remaining // 60, remaining % 60
                if m > 0:
                    time_str = f"{m} мин {s} сек" if s else f"{m} мин"
                else:
                    time_str = f"{s} сек"
                await reply_ephemeral(
                    message,
                    f"⏳  <b>{mention}</b>\n\n"
                    f"Кубик можно бросить через  <b>{time_str}</b>",
                )
                return

            delta = roll_dice_coins()
            new_balance = balance + delta
            if new_balance < 0:
                new_balance = 0
                delta = new_balance - balance

            await db.execute(
                "UPDATE users SET coins = ?, last_dice = ? WHERE user_id = ?",
                (new_balance, now, user_id),
            )

        spin_msg = await message.reply_dice(emoji="🎲")
        await asyncio.sleep(DICE_SPIN_DELAY)

        if delta > 0:
            delta_str = f"+{fmt_num(delta)}"
            title = "🎉  Выигрыш"
        elif delta < 0:
            delta_str = f"−{fmt_num(abs(delta))}"
            title = "😔  Проигрыш"
        else:
            delta_str = "±0"
            title = "·  Ничья"

        result_caption = (
            f"🎲  <b>{title}</b>\n\n"
            f"Результат  ·  <b>{delta_str} 🪙</b>\n"
            f"Баланс     ·  <b>{fmt_num(new_balance)}</b>"
        )

        try:
            await spin_msg.reply(result_caption)
        except TelegramBadRequest:
            await message.reply(result_caption)

    except Exception as e:
        logger.error(f"Ошибка в dice_handler: {e}")
        await reply_ephemeral(message, "❌ <b>Произошла ошибка. Попробуйте позже.</b>")


# ---------- Профиль ----------
@router.message(F.text == "👤 Профиль")
@router.message(F.text.lower().strip() == "профиль")
@router.message(Command("profile"))
@router.message(F.text.regexp(PROFILE_CMD_RE))
async def show_profile(message: Message):
    if not await check_not_banned(message):
        return
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
    if not await check_not_banned_cb(callback):
        return
    if not _owner_check(callback, callback_data.user_id):
        await callback.answer("⚠️ Кнопка предназначена не для вас")
        return
    try:
        target_id = callback_data.user_id or callback.from_user.id
        photo, caption, kb = await render_profile(
            callback.message.bot, target_id, viewer_id=callback.from_user.id
        )
        if "не найден" in (caption or "").lower():
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
    if not await check_not_banned(message):
        return
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
    if not await check_not_banned_cb(callback):
        return
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
            await callback.answer("⚠️ Сессия устарела, повторите команду")
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
    await callback.answer(
        f"Для изменения ника: /nickname НовыйНик ({NICKNAME_COST} 🪙) "
        f"или /nickname reset",
        show_alert=True,
    )


# ---------- Смена пола ----------
@router.callback_query(GenderCallback.filter(F.value == "menu"))
async def gender_menu(callback: CallbackQuery, callback_data: GenderCallback):
    if not await check_not_banned_cb(callback):
        return
    if not _owner_check(callback, callback_data.user_id):
        await callback.answer("⚠️ Кнопка предназначена не для вас")
        return
    user_id = callback_data.user_id or callback.from_user.id
    async with get_db() as db:
        cur = await db.execute("SELECT gender FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
    current = (row["gender"] if row and row["gender"] else "none")
    await callback.message.edit_caption(
        caption="⚧  <b>Выберите пол</b>\n\n"
                "<i>Необязательное поле — можно оставить «Не задан».</i>",
        reply_markup=get_gender_kb(current, owner_id=user_id),
    )
    await callback.answer()


@router.callback_query(GenderCallback.filter(F.value != "menu"))
async def gender_set(callback: CallbackQuery, callback_data: GenderCallback):
    if not await check_not_banned_cb(callback):
        return
    if not _owner_check(callback, callback_data.user_id):
        await callback.answer("⚠️ Кнопка предназначена не для вас")
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
        caption=f"✓  <b>Пол сохранён</b>\n"
                f"{g['icon']}  {g['name']}",
        reply_markup=get_gender_kb(value, owner_id=user_id),
    )
    await callback.answer("Сохранено ✓")


@router.message(Command("gender"))
async def gender_cmd(message: Message, command: Command):
    if not await check_not_banned(message):
        return
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
    limit = 10
    async with get_db() as db:
        if kind == "cards":
            cur = await db.execute(
                """SELECT u.user_id, u.nickname, COALESCE(SUM(i.amount), 0) AS value
                   FROM users u
                            LEFT JOIN inventory i ON u.user_id = i.user_id
                   WHERE u.role != 'banned'
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
                         WHERE u.role != 'banned'
                         GROUP BY u.user_id)
                   WHERE value > ?""",
                (my_value,),
            )
            my_rank = (await cur.fetchone())[0]
            unit = "🃏"
            title = "🃏  Топ по карточкам"
        elif kind == "streak":
            cur = await db.execute(
                "SELECT user_id, nickname, streak AS value FROM users "
                "WHERE role != 'banned' "
                "ORDER BY value DESC, user_id ASC LIMIT ?",
                (limit,),
            )
            top = await cur.fetchall()
            cur = await db.execute("SELECT streak AS value FROM users WHERE user_id = ?", (current_user_id,))
            my_row = await cur.fetchone()
            my_value = my_row["value"] if my_row else 0
            cur = await db.execute(
                "SELECT COUNT(*) + 1 FROM users WHERE streak > ? AND role != 'banned'", (my_value,)
            )
            my_rank = (await cur.fetchone())[0]
            unit = "🔥"
            title = "🔥  Топ по стрику"
        else:
            cur = await db.execute(
                "SELECT user_id, nickname, coins AS value FROM users "
                "WHERE role != 'banned' "
                "ORDER BY value DESC, user_id ASC LIMIT ?",
                (limit,),
            )
            top = await cur.fetchall()
            cur = await db.execute("SELECT coins AS value FROM users WHERE user_id = ?", (current_user_id,))
            my_row = await cur.fetchone()
            my_value = my_row["value"] if my_row else 0
            cur = await db.execute(
                "SELECT COUNT(*) + 1 FROM users WHERE coins > ? AND role != 'banned'", (my_value,)
            )
            my_rank = (await cur.fetchone())[0]
            unit = "🪙"
            title = "🪙  Топ по монетам"

        cur = await db.execute("SELECT nickname FROM users WHERE user_id = ?", (current_user_id,))
        my_row2 = await cur.fetchone()
        my_nick = my_row2["nickname"] if my_row2 and my_row2["nickname"] else f"User{current_user_id}"

    medals = ["🥇", "🥈", "🥉"]
    text = f"<b>{title}</b>\n<code>{'─' * 18}</code>\n\n"
    for i, row in enumerate(top, 1):
        medal = medals[i - 1] if i <= 3 else f"<code>{i:>2}.</code>"
        nick = row["nickname"] or f"User{row['user_id']}"
        mention = user_mention(row["user_id"], nick, None)
        text += f"{medal}  {mention}  ·  <b>{fmt_num(row['value'])}</b> {unit}\n"

    text += (
        f"\n<code>{'─' * 18}</code>\n"
        f"📌  Ваше место  ·  <b>#{fmt_num(my_rank)}</b>\n"
        f"    {esc(my_nick)}  ·  <b>{fmt_num(my_value)}</b> {unit}"
    )
    return text


@router.message(F.text == "🏆 Топ")
@router.message(F.text == "🏆 Топ игроков")
@router.message(Command("top"))
@router.message(F.text.regexp(TOP_CMD_RE))
async def show_top_players(message: Message):
    if not await check_not_banned(message):
        return
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
    if not await check_not_banned_cb(callback):
        return
    if rate_limited(f"top:{callback.from_user.id}", limit=5, window=5):
        await callback.answer("Слишком часто")
        return
    try:
        text = await build_top_text(callback_data.kind, callback.from_user.id)
        try:
            await callback.message.edit_text(
                text, reply_markup=get_top_keyboard(callback_data.kind)
            )
            await callback.answer()
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                await callback.answer("Данные не изменились. Попробуйте позже")
            else:
                await callback.message.answer(
                    text, reply_markup=get_top_keyboard(callback_data.kind)
                )
                await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка переключения топа: {e}")
        await callback.answer("⚠️ Произошла ошибка")


@router.callback_query(TopRefreshCallback.filter())
async def refresh_top(callback: CallbackQuery, callback_data: TopRefreshCallback):
    if not await check_not_banned_cb(callback):
        return
    if rate_limited(f"top:{callback.from_user.id}", limit=5, window=5):
        await callback.answer("Слишком часто")
        return
    try:
        text = await build_top_text(callback_data.kind, callback.from_user.id)
        try:
            await callback.message.edit_text(
                text, reply_markup=get_top_keyboard(callback_data.kind)
            )
            await callback.answer("Обновлено")
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                await callback.answer("Данные не изменились. Попробуйте позже")
            else:
                await callback.message.answer(
                    text, reply_markup=get_top_keyboard(callback_data.kind)
                )
                await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка обновления топа: {e}")
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
                    text=f"{r_info['icon']}  {r_info['name']}  ·  {fmt_num(user_amount)}/{fmt_num(total_of_rarity)}",
                    callback_data=RaritySelectCallback(rarity=r_key, page=0, user_id=user_id).pack(),
                )
            ])
        rows.append([
            InlineKeyboardButton(
                text="‹  В профиль",
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

    if callback:
        if not await check_not_banned_cb(callback):
            return
    else:
        if not await check_not_banned(message):
            return

    target_id = viewer_id

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
    if not await check_not_banned_cb(callback):
        return
    if not _owner_check(callback, callback_data.user_id):
        await callback.answer("⚠️ Кнопка предназначена не для вас")
        return
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
            f"╭ <b>{esc(card['name'])}</b>\n"
            f"╰ {info.get('icon', '')}  {info.get('name', rarity)}\n\n"
            f"🪙  Награда  ·  +{fmt_num(info.get('reward', 0))}\n"
            f"📦  У вас    ·  <b>{fmt_num(card['amount'])}</b>"
        )

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(
                text="‹",
                callback_data=RaritySelectCallback(rarity=rarity, page=page - 1, user_id=user_id).pack(),
            ))
        nav.append(InlineKeyboardButton(text=f"{page + 1} / {total_pages}", callback_data="ignore"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(
                text="›",
                callback_data=RaritySelectCallback(rarity=rarity, page=page + 1, user_id=user_id).pack(),
            ))

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            nav,
            [InlineKeyboardButton(
                text="‹  К категориям",
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
    if not await check_not_banned_cb(callback):
        return
    if not _owner_check(callback, callback_data.user_id):
        await callback.answer("⚠️ Кнопка предназначена не для вас")
        return
    target_id = callback_data.user_id or callback.from_user.id
    photo, caption, keyboard, _ = await render_collection(
        callback.message.bot, target_id
    )
    await show_or_edit_photo(callback.message, photo, caption, keyboard)
    await callback.answer()


@router.callback_query(F.data == "ignore")
async def ignore_callback(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(OkDeleteCallback.filter())
async def ok_delete_callback(callback: CallbackQuery):
    await callback.answer()
    bot_msg = callback.message
    user_msg = None
    if bot_msg and bot_msg.reply_to_message:
        user_msg = bot_msg.reply_to_message
    await _try_delete(bot_msg)
    await _try_delete(user_msg)


# ---------- Действия с карточкой ----------
@router.callback_query(CardActionCallback.filter())
async def handle_card_action(callback: CallbackQuery, callback_data: CardActionCallback):
    if not await check_not_banned_cb(callback):
        return
    user_id = callback.from_user.id
    action = callback_data.action
    target = callback_data.user_id or user_id

    if user_id != target:
        await callback.answer("⚠️ Кнопка предназначена не для вас")
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
                await callback.answer("⏳ Кулдаун уже прошёл — получайте бесплатно!")
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
    gems = await get_user_gems(user_id)
    async with get_db() as db:
        cur = await db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        coins = (row["coins"] or 0) if row else 0

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
        f"🛒  <b>Маркет</b>\n"
        f"╭ {esc(nickname)}\n"
        f"╰ 💎 <b>{fmt_num(gems)}</b>  ·  🪙 <b>{fmt_num(coins)}</b>\n\n"
        f"<i>Курс:  1 💎  =  {GEM_TO_COINS} 🪙</i>\n\n"
        f"Выберите редкость — купите недостающие карточки:"
    )

    b = InlineKeyboardBuilder()
    for r_key, r_info in RARITIES.items():
        missing = missing_by_rarity.get(r_key, 0)
        price = MARKET_PRICES.get(r_key, 0)
        if missing > 0:
            label = (
                f"{r_info['icon']}  {r_info['name']}  ·  "
                f"{fmt_num(missing)} шт · {price} 💎"
            )
        else:
            label = f"{r_info['icon']}  {r_info['name']}  ·  собрано ✓"
        b.button(
            text=label,
            callback_data=MarketRarityCallback(rarity=r_key, page=0, user_id=user_id).pack(),
        )
    b.button(
        text="💎  Купить кристаллы за монеты",
        callback_data=MarketExchangeCallback(action="menu", user_id=user_id).pack(),
    )
    b.adjust(1)
    return text, b.as_markup()


async def build_market_rarity_page(
    user_id: int, rarity: str, page: int = 0
) -> Tuple[str, InlineKeyboardMarkup, Optional[dict]]:
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
            f"{r_info.get('icon', '')}  <b>{r_info.get('name', rarity)}</b>\n\n"
            f"Все карточки этой редкости уже у вас 🎉"
        )
        b = InlineKeyboardBuilder()
        b.button(text="‹  Назад в маркет", callback_data=MarketMainCallback(user_id=user_id).pack())
        b.adjust(1)
        return text, b.as_markup(), None

    total = len(cards)
    page = max(0, min(page, total - 1))
    card = cards[page]

    text = (
        f"🛒  <b>Маркет</b>  ·  {r_info.get('icon', '')} {r_info.get('name', rarity)}\n\n"
        f"╭ <b>{esc(card['name'])}</b>\n"
        f"╰ 💎  <b>{fmt_num(price)}</b>  ·  баланс {fmt_num(gems)}\n\n"
        f"<i>{page + 1} / {total}</i>"
    )

    b = InlineKeyboardBuilder()
    can_buy = gems >= price
    buy_label = (
        f"✓  Купить · {fmt_num(price)} 💎"
        if can_buy
        else f"✗  Нужно {fmt_num(price)} 💎"
    )
    b.button(
        text=buy_label,
        callback_data=MarketBuyCallback(card_id=card["id"], user_id=user_id).pack(),
    )

    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="‹",
                callback_data=MarketRarityCallback(rarity=rarity, page=page - 1, user_id=user_id).pack(),
            )
        )
    nav.append(
        InlineKeyboardButton(text=f"{page + 1} / {total}", callback_data="ignore")
    )
    if page < total - 1:
        nav.append(
            InlineKeyboardButton(
                text="›",
                callback_data=MarketRarityCallback(rarity=rarity, page=page + 1, user_id=user_id).pack(),
            )
        )

    b.row(*nav)
    b.row(
        InlineKeyboardButton(
            text="‹  К редкостям", callback_data=MarketMainCallback(user_id=user_id).pack()
        )
    )
    return text, b.as_markup(), dict(card)


@router.message(F.text == "🛒 Маркет")
@router.message(Command("market"))
@router.message(F.text.lower().strip() == "маркет")
@router.message(F.text.regexp(MARKET_CMD_RE))
async def show_market(message: Message):
    if not await check_not_banned(message):
        return
    if message.chat.type != "private":
        await reply_ephemeral(
            message,
            "⚠️ <b>Маркет доступен только в личных сообщениях с ботом.</b>",
        )
        return
    try:
        await get_or_create_user(
            message.from_user.id,
            message.from_user.username,
            message.from_user.full_name,
        )
        if rate_limited(f"market:{message.from_user.id}", limit=5, window=8):
            return
        text, kb = await build_market_main(message.from_user.id)
        await message.reply(text, reply_markup=kb)
    except Exception as e:
        logger.error(f"Ошибка маркета: {e}")
        await message.reply("❌ <b>Ошибка маркета. Попробуйте позже.</b>")


@router.callback_query(MarketMainCallback.filter())
async def market_main_callback(callback: CallbackQuery, callback_data: MarketMainCallback):
    if not await check_not_banned_cb(callback):
        return
    if not _owner_check(callback, callback_data.user_id):
        await callback.answer("⚠️ Кнопка предназначена не для вас")
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
    if not await check_not_banned_cb(callback):
        return
    if not _owner_check(callback, callback_data.user_id):
        await callback.answer("⚠️ Кнопка предназначена не для вас")
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
    if not await check_not_banned_cb(callback):
        return
    user_id = callback.from_user.id
    card_id = callback_data.card_id
    target_id = callback_data.user_id or user_id

    if user_id != target_id:
        await callback.answer("⚠️ Кнопка предназначена не для вас")
        return

    if rate_limited(f"mkt-buy:{user_id}", limit=5, window=10):
        await callback.answer("Слишком часто")
        return

    try:
        async with get_db() as db:
            cur = await db.execute(
                "SELECT id, name, rarity, photo_id FROM cards WHERE id = ?",
                (card_id,),
            )
            card = await cur.fetchone()
            if not card:
                await callback.answer("❌ Карточка не найдена")
                return

            rarity = card["rarity"]
            price = MARKET_PRICES.get(rarity, 0)
            if price <= 0:
                await callback.answer("❌ Эту карточку нельзя купить")
                return

            cur = await db.execute(
                "SELECT amount FROM inventory WHERE user_id = ? AND card_id = ?",
                (user_id, card_id),
            )
            owned = await cur.fetchone()
            if owned:
                await callback.answer("✅ У вас уже есть эта карточка")
                return

            cur = await db.execute(
                "SELECT gems FROM users WHERE user_id = ?", (user_id,)
            )
            row = await cur.fetchone()
            gems = (row["gems"] or 0) if row else 0

            if gems < price:
                await callback.answer(
                    f"⚠️ Недостаточно кристаллов. Нужно {price} 💎, у вас {gems} 💎"
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
            f"✓  <b>Покупка успешна</b>\n\n"
            f"╭ <b>{esc(card['name'])}</b>\n"
            f"╰ {r_info.get('icon', '')}  {r_info.get('name', rarity)}\n\n"
            f"💎  −{fmt_num(price)}  →  <b>{fmt_num(new_gems)}</b>"
        )
        b = InlineKeyboardBuilder()
        b.button(
            text="🛒  Продолжить",
            callback_data=MarketRarityCallback(rarity=rarity, page=0, user_id=user_id).pack(),
        )
        b.button(text="‹  В маркет", callback_data=MarketMainCallback(user_id=user_id).pack())
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
        await callback.answer("⚠️ Ошибка покупки")


@router.callback_query(MarketExchangeCallback.filter())
async def market_exchange(callback: CallbackQuery, callback_data: MarketExchangeCallback):
    if not await check_not_banned_cb(callback):
        return
    user_id = callback.from_user.id
    action = callback_data.action
    amount = callback_data.amount
    target_id = callback_data.user_id or user_id

    if user_id != target_id:
        await callback.answer("⚠️ Кнопка предназначена не для вас")
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
                f"💱  <b>Обмен монет → кристаллы</b>\n\n"
                f"Курс  ·  <b>1 💎 = {GEM_TO_COINS} 🪙</b>\n\n"
                f"🪙  Монеты     ·  <b>{fmt_num(coins)}</b>\n"
                f"💎  Кристаллы  ·  <b>{fmt_num(gems)}</b>\n"
                f"Можно купить   ·  до <b>{fmt_num(max_buy)}</b> 💎\n\n"
                f"Выберите количество:"
            )
            b = InlineKeyboardBuilder()
            for n in (1, 5, 10, 25, 50):
                cost = n * GEM_TO_COINS
                if coins >= cost:
                    b.button(
                        text=f"+{n} 💎 · {fmt_num(cost)} 🪙",
                        callback_data=MarketExchangeCallback(
                            action="buy_gems", amount=n, user_id=user_id
                        ).pack(),
                    )
            if max_buy >= 1 and max_buy not in (1, 5, 10, 25, 50):
                cost = max_buy * GEM_TO_COINS
                b.button(
                    text=f"+{fmt_num(max_buy)} 💎 · все ({fmt_num(cost)} 🪙)",
                    callback_data=MarketExchangeCallback(
                        action="buy_gems", amount=max_buy, user_id=user_id
                    ).pack(),
                )
            b.button(text="‹  Назад", callback_data=MarketMainCallback(user_id=user_id).pack())
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
                await callback.answer("❌ Слишком много за раз")
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
                        f"⚠️ Недостаточно монет. Нужно {fmt_num(cost)} 🪙"
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
                f"✓  <b>Обмен выполнен</b>\n\n"
                f"💎  +<b>{fmt_num(amount)}</b>  →  {fmt_num(new_gems)}\n"
                f"🪙  −<b>{fmt_num(cost)}</b>  →  {fmt_num(new_coins)}"
            )
            b = InlineKeyboardBuilder()
            b.button(
                text="💱  Ещё обмен",
                callback_data=MarketExchangeCallback(action="menu", user_id=user_id).pack(),
            )
            b.button(text="‹  В маркет", callback_data=MarketMainCallback(user_id=user_id).pack())
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
        await callback.answer("⚠️ Ошибка обмена")


# ================= АДМИН-ПАНЕЛЬ =================
@router.message(Command("admin"), admin_filter)
@router.message(F.text == "⚙️ Админ-панель")
async def admin_panel(message: Message):
    if not await is_admin(message.from_user.id):
        # Если кнопка осталась у бывшего админа — убираем её
        await message.reply(
            "⚠️  <b>Доступ запрещён</b>\n\n"
            "У вас больше нет прав администратора.",
            reply_markup=get_main_km(is_staff=False),
        )
        await update_user_commands(message.bot, message.from_user.id, "user")
        return

    role = await get_user_role(message.from_user.id)
    text = (
        f"⚙️  <b>Админ-панель</b>\n"
        f"<code>{'─' * 18}</code>\n\n"
        f"Ваша роль:  {role_display(role)}\n\n"
        f"Выберите раздел:"
    )
    await message.answer(text, reply_markup=get_admin_main_kb())


@router.callback_query(AdminMainCallback.filter())
@router.callback_query(F.data == "admin_main")
async def admin_main_back(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Ошибка доступа", show_alert=True)
        return
    role = await get_user_role(call.from_user.id)
    text = (
        f"⚙️  <b>Админ-панель</b>\n"
        f"<code>{'─' * 18}</code>\n\n"
        f"Ваша роль:  {role_display(role)}\n\n"
        f"Выберите раздел:"
    )
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer(text, reply_markup=get_admin_main_kb())
    await call.answer()


@router.callback_query(F.data == "admin_add_card")
async def add_card_start(call: CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Ошибка доступа")
        return
    await state.set_state(AddCardSG.photo)
    await call.message.answer("📷  <b>Отправьте фото новой карточки</b>\n\n<code>/cancel</code> — отмена")
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
        cur = await db.execute(
            "INSERT INTO cards (name, rarity, photo_id) VALUES (?, ?, ?)",
            (name, rarity, photo_id),
        )
        card_id = cur.lastrowid
        if sync_card_photo:
            try:
                path = await sync_card_photo(message.bot, card_id, photo_id)
                await db.execute(
                    "UPDATE cards SET photo_path = ? WHERE id = ?",
                    (path, card_id),
                )
            except Exception as e:
                logger.error(f"sync_card_photo: {e}")
    await message.reply(
        f"✅  <b>Карточка добавлена</b>\n\n"
        f"╭ <b>{esc(name)}</b>\n"
        f"╰ {RARITIES[rarity]['icon']}  {RARITIES[rarity]['name']}",
        reply_markup=get_admin_main_kb(),
    )


@router.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext):
    if await state.get_state() is None:
        return
    await state.clear()
    if await is_admin(message.from_user.id):
        await message.reply("✅  <b>Операция отменена</b>", reply_markup=get_admin_main_kb())
    else:
        await message.reply("✅  <b>Операция отменена</b>")


@router.callback_query(F.data == "cancel_add_card")
async def cancel_add_card_callback(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("✅  <b>Операция отменена</b>", reply_markup=get_admin_main_kb())
    await call.answer()


@router.message(AddCardSG.photo, F.photo)
async def add_card_photo(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    await state.update_data(photo_id=message.photo[-1].file_id)
    await state.set_state(AddCardSG.name)
    await message.answer("✍️  <b>Придумайте название</b> (до 64 символов)")


@router.message(AddCardSG.name, F.text)
async def add_card_name(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    await state.update_data(name=message.text[:64])
    await state.set_state(AddCardSG.rarity)
    await message.answer("🎲  <b>Выберите редкость</b>", reply_markup=get_rarity_keyboard())


@router.callback_query(AddCardSG.rarity, F.data.startswith("set_rarity:"))
async def add_card_rarity(call: CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        await state.clear()
        await call.answer("⚠️ Ошибка доступа")
        return
    rarity = call.data.split(":")[1]
    data = await state.get_data()
    async with get_db() as db:
        cur = await db.execute(
            "INSERT INTO cards (name, rarity, photo_id) VALUES (?, ?, ?)",
            (data["name"], rarity, data["photo_id"]),
        )
        card_id = cur.lastrowid
        if sync_card_photo:
            try:
                path = await sync_card_photo(call.bot, card_id, data["photo_id"])
                await db.execute(
                    "UPDATE cards SET photo_path = ? WHERE id = ?",
                    (path, card_id),
                )
            except Exception as e:
                logger.error(f"sync_card_photo: {e}")
    r = RARITIES[rarity]
    await call.message.answer(
        f"✅  <b>Карточка добавлена</b>\n\n"
        f"╭ <b>{esc(data['name'])}</b>\n"
        f"╰ {r['icon']}  {r['name']}",
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
            return (
                "📜  <b>Карточки</b>\n<code>──────────────────</code>\n\n"
                "🔴  В базе пока нет карточек.\n\n"
                "Добавьте первую через «➕ Добавить карточку».",
                get_admin_main_kb(),
                0,
            )

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
        text = (
            f"📜  <b>Карточки</b>\n"
            f"<code>{'─' * 18}</code>\n"
            f"Стр. <b>{page + 1}</b> / {total_pages}  ·  всего <b>{fmt_num(total)}</b>\n\n"
        )
        for c in cards:
            r = RARITIES.get(c["rarity"], {})
            cur = await db.execute(
                "SELECT COUNT(DISTINCT user_id), COALESCE(SUM(amount), 0) FROM inventory WHERE card_id = ?",
                (c["id"],),
            )
            owners, instances = await cur.fetchone()
            text += (
                f"{r.get('icon', '•')}  <b>{esc(c['name'])}</b>\n"
                f"    <code>#{c['id']}</code>  ·  👥 {fmt_num(owners)}  ·  📦 {fmt_num(instances)}\n\n"
            )
            b.button(
                text=f"{r.get('icon', '')} {c['name'][:28]}",
                callback_data=AdminCardManageCallback(card_id=c["id"]).pack(),
            )

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="‹", callback_data=AdminCardPageCallback(page=page - 1).pack()))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="ignore"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="›", callback_data=AdminCardPageCallback(page=page + 1).pack()))

        b.adjust(1)
        b.row(*nav)
        b.row(InlineKeyboardButton(text="‹  В админ-меню", callback_data="admin_main"))

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
        f"🃏  <b>{esc(card['name'])}</b>\n"
        f"<code>{'─' * 18}</code>\n\n"
        f"🆔  <code>{card_id}</code>\n"
        f"{r.get('icon', '')}  Редкость  ·  <b>{r.get('name', card['rarity'])}</b>\n"
        f"👥  Владельцев  ·  <b>{fmt_num(owners)}</b>\n"
        f"📦  Экземпляров  ·  <b>{fmt_num(instances)}</b>"
    )
    b = InlineKeyboardBuilder()
    b.button(text="✏️  Изменить название", callback_data=f"edit_name:{card_id}")
    b.button(text="🎲  Изменить редкость", callback_data=f"edit_rarity:{card_id}")
    b.button(text="🖼  Изменить фото", callback_data=AdminCardEditPhotoCallback(card_id=card_id).pack())
    b.button(text="🗑  Удалить", callback_data=f"delete_card:{card_id}")
    b.button(text="‹  К списку", callback_data=AdminCardPageCallback(page=0).pack())
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
    await call.message.answer("📷  <b>Отправьте новое фото для карточки</b>\n\n<code>/cancel</code> — отмена")
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
        if sync_card_photo:
            try:
                path = await sync_card_photo(message.bot, card_id, new_photo)
                await db.execute(
                    "UPDATE cards SET photo_path = ? WHERE id = ?",
                    (path, card_id),
                )
            except Exception as e:
                logger.error(f"sync_card_photo edit: {e}")
    await message.answer(
        f"✅  <b>Фото карточки обновлено</b> (ID <code>{card_id}</code>)",
        reply_markup=get_admin_main_kb(),
    )
    await state.clear()


@router.callback_query(F.data.startswith("delete_card:"))
async def delete_card_cmd(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Ошибка доступа")
        return
    card_id = int(call.data.split(":")[1])
    async with get_db() as db:
        await db.execute("DELETE FROM cards WHERE id = ?", (card_id,))
        await db.execute("DELETE FROM inventory WHERE card_id = ?", (card_id,))
    await call.message.answer("🗑  <b>Карточка удалена</b>", reply_markup=get_admin_main_kb())
    await call.answer()


@router.callback_query(F.data.startswith("edit_name:"))
async def edit_card_name_start(call: CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Ошибка доступа")
        return
    await state.update_data(card_id=int(call.data.split(":")[1]))
    await state.set_state(EditCardSG.new_name)
    await call.message.answer("✍️  <b>Новое название:</b>")
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
        f"✅  <b>Название изменено:</b> {esc(new_name)}",
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
    b.button(text="‹  Назад", callback_data=AdminCardManageCallback(card_id=card_id).pack())
    b.adjust(2)
    await call.message.answer("🎲  <b>Новая редкость:</b>", reply_markup=b.as_markup())
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
        f"✅  <b>Редкость изменена:</b> {RARITIES[callback_data.rarity]['name']}",
        reply_markup=get_admin_main_kb(),
    )
    await call.answer()


# ================= АДМИН: ПОЛЬЗОВАТЕЛИ =================
async def build_admin_users_page(page: int = 0, filter_role: str = "all"):
    per_page = 5
    async with get_db() as db:
        if filter_role == "all":
            cur = await db.execute("SELECT COUNT(*) FROM users")
        else:
            cur = await db.execute("SELECT COUNT(*) FROM users WHERE role = ?", (filter_role,))
        total = (await cur.fetchone())[0]
        if total == 0:
            filter_label = {
                "all": "все",
                "user": "пользователи",
                "admin": "админы",
                "superadmin": "главные админы",
                "banned": "заблокированные",
            }.get(filter_role, filter_role)
            return (
                f"👥  <b>Пользователи</b>\n<code>{'─' * 18}</code>\n\n"
                f"Нет записей (фильтр: {filter_label}).",
                get_admin_main_kb(),
                0,
            )

        total_pages = (total + per_page - 1) // per_page
        page = max(0, min(page, total_pages - 1))
        offset = page * per_page

        if filter_role == "all":
            cur = await db.execute(
                """SELECT u.user_id, u.nickname, u.coins, u.gems, u.streak, u.role, u.registration, u.gender
                   FROM users u
                   ORDER BY u.registration DESC
                   LIMIT ? OFFSET ?""",
                (per_page, offset),
            )
        else:
            cur = await db.execute(
                """SELECT u.user_id, u.nickname, u.coins, u.gems, u.streak, u.role, u.registration, u.gender
                   FROM users u
                   WHERE u.role = ?
                   ORDER BY u.registration DESC
                   LIMIT ? OFFSET ?""",
                (filter_role, per_page, offset),
            )
        users = await cur.fetchall()

        b = InlineKeyboardBuilder()
        # Фильтры
        filters = [
            ("all", "Все"),
            ("user", "👤"),
            ("admin", "🛡"),
            ("banned", "🚫"),
        ]
        filter_row = []
        for fr, label in filters:
            mark = "●" if fr == filter_role else "○"
            filter_row.append(InlineKeyboardButton(
                text=f"{mark} {label}",
                callback_data=AdminUserPageCallback(page=0, filter_role=fr).pack(),
            ))
        b.row(*filter_row)

        text = (
            f"👥  <b>Пользователи</b>\n"
            f"<code>{'─' * 18}</code>\n"
            f"Стр. <b>{page + 1}</b> / {total_pages}  ·  всего <b>{fmt_num(total)}</b>\n\n"
        )
        for u in users:
            g = GENDERS.get(u["gender"] or "none", GENDERS["none"])
            role_info = ROLES.get(u["role"] or "user", ROLES["user"])
            reg = datetime.fromtimestamp(u["registration"]).strftime("%d.%m.%Y") if u["registration"] else "—"
            nick = u["nickname"] or f"User{u['user_id']}"
            text += (
                f"{role_info['icon']}  <b>{esc(nick)}</b>\n"
                f"    <code>{u['user_id']}</code>  ·  🪙 {fmt_num(u['coins'] or 0)}  ·  "
                f"💎 {fmt_num(u['gems'] or 0)}  ·  🔥 {u['streak'] or 0}\n\n"
            )
            b.button(
                text=f"{role_info['icon']} {nick[:24]}",
                callback_data=AdminUserViewCallback(user_id=u["user_id"]).pack(),
            )

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(
                text="‹",
                callback_data=AdminUserPageCallback(page=page - 1, filter_role=filter_role).pack(),
            ))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="ignore"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(
                text="›",
                callback_data=AdminUserPageCallback(page=page + 1, filter_role=filter_role).pack(),
            ))

        b.adjust(1)
        b.row(*nav)
        b.row(InlineKeyboardButton(text="‹  В админ-меню", callback_data="admin_main"))

        return text, b.as_markup(), page


@router.callback_query(AdminUserPageCallback.filter())
async def admin_users_page_callback(call: CallbackQuery, callback_data: AdminUserPageCallback):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Ошибка доступа")
        return
    text, kb, _ = await build_admin_users_page(callback_data.page, callback_data.filter_role)
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
    viewer_is_super = await is_superadmin(call.from_user.id)

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
    role = user["role"] or "user"
    g = GENDERS.get(user["gender"] or "none", GENDERS["none"])
    reg = datetime.fromtimestamp(user["registration"]).strftime("%d.%m.%Y %H:%M") if user["registration"] else "—"
    gems = user["gems"] if user["gems"] is not None else 0

    caption = (
        f"👤  <b>{esc(nick)}</b>\n"
        f"<code>{'─' * 18}</code>\n\n"
        f"🆔  <code>{user_id}</code>\n"
        f"🎭  Роль  ·  {role_display(role)}\n"
        f"⚧  Пол  ·  {g['icon']} {g['name']}\n"
        f"📅  Регистрация  ·  {reg}\n\n"
        f"🪙  Монеты  ·  <b>{fmt_num(user['coins'])}</b>\n"
        f"💎  Кристаллы  ·  <b>{fmt_num(gems)}</b>\n"
        f"🃏  Карточек  ·  <b>{fmt_num(user['cards_count'])}</b>\n"
        f"🔥  Стрик  ·  <b>{fmt_days(user['streak'])}</b>"
    )

    b = InlineKeyboardBuilder()
    b.button(text="✏️  Ник", callback_data=AdminUserActionCallback(action="nick", user_id=user_id).pack())
    b.button(text="🪙  Монеты", callback_data=AdminUserActionCallback(action="coins", user_id=user_id).pack())
    b.button(text="💎  Кристаллы", callback_data=AdminUserActionCallback(action="gems", user_id=user_id).pack())
    b.button(text="⏱  Сброс CD", callback_data=AdminUserActionCallback(action="resetcd", user_id=user_id).pack())

    # Роли и бан — с ограничениями
    if role == "banned":
        b.button(text="✅  Разблокировать", callback_data=AdminUserActionCallback(action="unban", user_id=user_id).pack())
    elif role == "user":
        b.button(text="🚫  Заблокировать", callback_data=AdminUserActionCallback(action="ban", user_id=user_id).pack())
        if viewer_is_super:
            b.button(text="🛡  Назначить админом", callback_data=AdminUserActionCallback(action="make_admin", user_id=user_id).pack())
    elif role == "admin":
        if viewer_is_super:
            b.button(text="❌  Разжаловать", callback_data=AdminUserActionCallback(action="unadmin", user_id=user_id).pack())
            b.button(text="👑  Сделать главным", callback_data=AdminUserActionCallback(action="make_super", user_id=user_id).pack())
            b.button(text="🚫  Заблокировать", callback_data=AdminUserActionCallback(action="ban", user_id=user_id).pack())
    elif role == "superadmin":
        if viewer_is_super and user_id != call.from_user.id:
            b.button(text="❌  Разжаловать", callback_data=AdminUserActionCallback(action="unadmin", user_id=user_id).pack())

    b.button(text="‹  К списку", callback_data=AdminUserPageCallback(page=0, filter_role="all").pack())
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
    viewer_id = call.from_user.id
    viewer_is_super = await is_superadmin(viewer_id)

    target_role = await get_user_role(target_id)

    # ---- Бан ----
    if action == "ban":
        if target_id == viewer_id:
            await call.answer("❌ Нельзя заблокировать себя", show_alert=True)
            return
        if target_role == "superadmin" and not viewer_is_super:
            await call.answer("❌ Недостаточно прав", show_alert=True)
            return
        if target_role == "superadmin" and viewer_is_super and target_id != viewer_id:
            # суперадмин может банить другого суперадмина только если он сам суперадмин
            pass
        await set_user_role(target_id, "banned")
        await update_user_commands(call.bot, target_id, "banned")
        await call.message.answer(
            f"🚫  <b>Пользователь заблокирован</b>\n\n"
            f"🆔  <code>{target_id}</code>",
            reply_markup=get_admin_main_kb(),
        )
        await call.answer("Заблокирован")
        return

    if action == "unban":
        await set_user_role(target_id, "user")
        await update_user_commands(call.bot, target_id, "user")
        await call.message.answer(
            f"✅  <b>Пользователь разблокирован</b>\n\n"
            f"🆔  <code>{target_id}</code>",
            reply_markup=get_admin_main_kb(),
        )
        await call.answer("Разблокирован")
        return

    # ---- Роли (только superadmin) ----
    if action == "make_admin":
        if not viewer_is_super:
            await call.answer("❌ Только главный администратор", show_alert=True)
            return
        await set_user_role(target_id, "admin")
        await update_user_commands(call.bot, target_id, "admin")
        await call.message.answer(
            f"🛡  <b>Назначен администратором</b>\n\n"
            f"🆔  <code>{target_id}</code>",
            reply_markup=get_admin_main_kb(),
        )
        await call.answer("Готово")
        return

    if action == "make_super":
        if not viewer_is_super:
            await call.answer("❌ Только главный администратор", show_alert=True)
            return
        await set_user_role(target_id, "superadmin")
        await update_user_commands(call.bot, target_id, "superadmin")
        await call.message.answer(
            f"👑  <b>Назначен главным администратором</b>\n\n"
            f"🆔  <code>{target_id}</code>",
            reply_markup=get_admin_main_kb(),
        )
        await call.answer("Готово")
        return

    if action == "unadmin":
        if not viewer_is_super:
            await call.answer("❌ Только главный администратор", show_alert=True)
            return
        if target_id == viewer_id:
            await call.answer("❌ Нельзя разжаловать себя", show_alert=True)
            return
        await set_user_role(target_id, "user")
        await update_user_commands(call.bot, target_id, "user")
        await call.message.answer(
            f"✅  <b>Права сняты</b>\n\n"
            f"🆔  <code>{target_id}</code> теперь обычный пользователь.",
            reply_markup=get_admin_main_kb(),
        )
        await call.answer("Готово")
        return

    if action == "nick":
        await call.message.answer(
            f"✏️  Чтобы изменить ник пользователя <code>{target_id}</code>:\n"
            f"<code>/setnick {target_id} НовыйНик</code>"
        )
        await call.answer()
    elif action == "coins":
        await call.message.answer(
            f"🪙  Чтобы изменить монеты пользователя <code>{target_id}</code>:\n"
            f"<code>/setcoins {target_id} 1000</code>"
        )
        await call.answer()
    elif action == "gems":
        await call.message.answer(
            f"💎  Чтобы изменить кристаллы пользователя <code>{target_id}</code>:\n"
            f"<code>/setgems {target_id} 50</code>"
        )
        await call.answer()
    elif action == "resetcd":
        async with get_db() as db:
            await db.execute("UPDATE users SET last_claim = 0 WHERE user_id = ?", (target_id,))
        await call.message.answer(
            f"⏱  <b>Кулдаун сброшен</b>\n\n"
            f"🆔  <code>{target_id}</code>",
            reply_markup=get_admin_main_kb(),
        )
        await call.answer("Сброшен")
    else:
        await call.answer()


@router.message(Command("setnick"), admin_filter)
async def admin_setnick(message: Message, command: Command):
    if message.chat.type != "private":
        await message.reply("⚠️ Только в ЛС.")
        return
    args = (command.args or "").strip().split()
    if len(args) < 2:
        await message.reply("✏️ Использование: <code>/setnick USERID НовыйНик</code>")
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
        f"✅  Ник изменён: <code>{target_id}</code>\n"
        f"Было: {esc(old or '—')} → Стало: {esc(new_nick)}"
    )


# ================= АДМИН-СПРАВКА (красивая, по страницам) =================
ADMIN_HELP_PAGES = [
    {
        "title": "🛠  Роли и права",
        "body": (
            "<b>👑  Главный администратор</b>\n"
            "<blockquote>"
            "Полный доступ. Может назначать/разжаловать админов,\n"
            "делать других главными, банить кого угодно.\n"
            "Первый пользователь в БД становится им автоматически."
            "</blockquote>\n\n"
            "<b>🛡  Администратор</b>\n"
            "<blockquote>"
            "Управление карточками, пользователями, балансами,\n"
            "статистика, бан обычных пользователей.\n"
            "Не может менять роли админов/суперадминов."
            "</blockquote>\n\n"
            "<b>🚫  Заблокированный</b>\n"
            "<blockquote>"
            "Не может пользоваться ботом.\n"
            "Сообщение о блокировке при любой команде."
            "</blockquote>"
        ),
    },
    {
        "title": "🛠  Команды управления",
        "body": (
            "<b>👑  Права</b>\n"
            "<blockquote>"
            "<code>/setadmin USERID</code> — назначить админа\n"
            "<code>/unsetadmin USERID</code> — разжаловать\n"
            "<code>/ban USERID</code> — заблокировать\n"
            "<code>/unban USERID</code> — разблокировать\n"
            "<i>Только в ЛС. /setadmin и /unsetadmin — суперадмин.</i>"
            "</blockquote>\n\n"
            "<b>🪙  Балансы</b>\n"
            "<blockquote>"
            "<code>/setcoins [USERID] N</code> — монеты\n"
            "<code>/setgems [USERID] N</code> — кристаллы\n"
            "<code>/setnick USERID Ник</code> — ник\n"
            "<code>/resetcd [USERID]</code> — сброс кулдауна"
            "</blockquote>"
        ),
    },
    {
        "title": "🛠  Карточки и данные",
        "body": (
            "<b>🃏  Карточки</b>\n"
            "<blockquote>"
            "<code>/admin</code> — панель (кнопка «⚙️ Админ-панель»)\n"
            "  ·  ➕ Добавить карточку\n"
            "  ·  📜 Список (пагинация, редактирование, удаление)\n"
            "  ·  👥 Пользователи (фильтры, профиль, действия)\n"
            "<code>/addcard Название редкость</code> + фото\n"
            "<code>/delcard ID</code> — быстрое удаление"
            "</blockquote>\n\n"
            "<b>📊  Статистика</b>\n"
            "<blockquote>"
            "<code>/stats</code> — общая статистика бота\n"
            "<code>/getusers</code> — список пользователей текстом"
            "</blockquote>"
        ),
    },
    {
        "title": "🛠  Тестовые и прочее",
        "body": (
            "<b>🧪  Тестовые</b>\n"
            "<blockquote>"
            "<code>/migrate_photos</code> — фото на диск (мини-аппка)\n<code>/migrate_crystals_to_gems</code>\n"
            "<code>/reset_all_nicknames</code>\n"
            "<code>/promote_to_mythical</code>\n"
            "<code>/getfileid</code> — file_id фото\n"
            "<code>/denominate_coins</code>\n"
            "<code>/set_registration_now</code>"
            "</blockquote>\n\n"
            "<b>⌨️  Общее</b>\n"
            "<blockquote>"
            "<code>/cancel</code> — отмена FSM\n"
            "<code>/adminhelp</code> — эта справка\n"
            "Кнопка «⚙️ Админ-панель» появляется только у админов.\n"
            "При снятии прав — исчезает при следующем /start."
            "</blockquote>"
        ),
    },
]


def get_admin_help_keyboard(page: int) -> InlineKeyboardMarkup:
    total = len(ADMIN_HELP_PAGES)
    page = max(0, min(page, total - 1))
    b = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            text="‹  Назад", callback_data=AdminHelpCallback(page=page - 1).pack()
        ))
    nav.append(InlineKeyboardButton(
        text=f"{page + 1} / {total}", callback_data="ignore"
    ))
    if page < total - 1:
        nav.append(InlineKeyboardButton(
            text="Далее  ›", callback_data=AdminHelpCallback(page=page + 1).pack()
        ))
    b.row(*nav)
    b.row(InlineKeyboardButton(text="‹  В админ-меню", callback_data="admin_main"))
    return b.as_markup()


def build_admin_help_text(page: int) -> str:
    total = len(ADMIN_HELP_PAGES)
    page = max(0, min(page, total - 1))
    p = ADMIN_HELP_PAGES[page]
    return f"<b>{p['title']}</b>\n<code>{'─' * 18}</code>\n\n{p['body']}"


@router.message(Command("adminhelp"), admin_filter)
async def admin_help(message: Message):
    await message.reply(
        build_admin_help_text(0),
        reply_markup=get_admin_help_keyboard(0),
    )


@router.callback_query(AdminHelpCallback.filter())
async def admin_help_page(callback: CallbackQuery, callback_data: AdminHelpCallback):
    if not await is_admin(callback.from_user.id):
        await callback.answer("⚠️ Ошибка доступа")
        return
    try:
        text = build_admin_help_text(callback_data.page)
        kb = get_admin_help_keyboard(callback_data.page)
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                await callback.answer("Данные не изменились")
            else:
                await callback.message.answer(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка admin help: {e}")
        await callback.answer("⚠️ Ошибка")


@router.callback_query(F.data == "admin_stats_quick")
async def admin_stats_quick(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Ошибка доступа")
        return
    # Переиспользуем логику /stats
    await call.answer()
    # Создаём фейковый message-like вызов через существующий handler
    # Проще — дублируем краткую статистику
    try:
        async with get_db() as db:
            cur = await db.execute("SELECT COUNT(*) FROM users")
            total_users = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM users WHERE role = 'banned'")
            banned = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM users WHERE role IN ('admin', 'superadmin')")
            staff = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM cards")
            total_cards = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COALESCE(SUM(coins), 0) FROM users")
            total_coins = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COALESCE(SUM(gems), 0) FROM users")
            total_gems = (await cur.fetchone())[0]

        text = (
            f"📊  <b>Краткая статистика</b>\n"
            f"<code>{'─' * 18}</code>\n\n"
            f"👥  Пользователей  ·  <b>{fmt_num(total_users)}</b>\n"
            f"🛡  Админов        ·  <b>{fmt_num(staff)}</b>\n"
            f"🚫  Заблокировано  ·  <b>{fmt_num(banned)}</b>\n\n"
            f"🃏  Карточек       ·  <b>{fmt_num(total_cards)}</b>\n"
            f"🪙  Монет в игре   ·  <b>{fmt_num(total_coins)}</b>\n"
            f"💎  Кристаллов     ·  <b>{fmt_num(total_gems)}</b>\n\n"
            f"<i>Полная статистика: /stats</i>"
        )
        b = InlineKeyboardBuilder()
        b.button(text="‹  В админ-меню", callback_data="admin_main")
        await call.message.answer(text, reply_markup=b.as_markup())
    except Exception as e:
        logger.error(f"admin_stats_quick: {e}")
        await call.message.answer("❌ Ошибка статистики")


# ================= КОМАНДЫ ПРАВ =================
@router.message(Command("setadmin"), superadmin_filter)
async def set_admin_cmd(message: Message, command: Command):
    if message.chat.type != "private":
        await message.reply("⚠️ Команда доступна только в личных сообщениях.")
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
                f"❌ <b>Пользователь не найден.</b>\n"
                f"Он должен хотя бы раз запустить бота (<code>/start</code>)."
            )
            return

        if row["role"] in ("admin", "superadmin"):
            await message.reply(
                f"ℹ️  {esc(row['nickname'] or target_id)} уже имеет права администратора "
                f"({role_display(row['role'])})."
            )
            return

        await db.execute(
            "UPDATE users SET role = 'admin' WHERE user_id = ?", (target_id,)
        )

    await update_user_commands(message.bot, target_id, "admin")
    await message.reply(
        f"✅  <b>Назначен администратором</b>\n\n"
        f"🆔  <code>{target_id}</code>\n"
        f"👤  {esc(row['nickname'] or str(target_id))}"
    )


@router.message(Command("unsetadmin"), superadmin_filter)
async def unset_admin_cmd(message: Message, command: Command):
    if message.chat.type != "private":
        await message.reply("⚠️ Команда доступна только в личных сообщениях.")
        return

    arg = (command.args or "").strip()
    if not arg:
        await message.reply(
            "✏️ Использование: <code>/unsetadmin USERID</code>"
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
            "⚠️  <b>Нельзя разжаловать самого себя.</b>\n"
            "Попросите другого главного администратора."
        )
        return

    async with get_db() as db:
        cur = await db.execute(
            "SELECT user_id, nickname, role FROM users WHERE user_id = ?", (target_id,)
        )
        row = await cur.fetchone()
        if not row:
            await message.reply("❌ <b>Пользователь не найден.</b>")
            return

        if row["role"] not in ("admin", "superadmin"):
            await message.reply(
                f"ℹ️  {esc(row['nickname'] or target_id)} не является администратором."
            )
            return

        await db.execute(
            "UPDATE users SET role = 'user' WHERE user_id = ?", (target_id,)
        )

    await update_user_commands(message.bot, target_id, "user")
    await message.reply(
        f"✅  <b>Права сняты</b>\n\n"
        f"🆔  <code>{target_id}</code>\n"
        f"👤  {esc(row['nickname'] or str(target_id))}"
    )


@router.message(Command("ban"), admin_filter)
async def ban_cmd(message: Message, command: Command):
    if message.chat.type != "private":
        await message.reply("⚠️ Только в ЛС.")
        return

    arg = (command.args or "").strip()
    if not arg:
        await message.reply("✏️ Использование: <code>/ban USERID</code>")
        return

    try:
        target_id = int(arg)
    except ValueError:
        await message.reply("❌ USERID должен быть числом.")
        return

    if target_id == message.from_user.id:
        await message.reply("❌ Нельзя заблокировать себя.")
        return

    target_role = await get_user_role(target_id)
    if target_role in ("admin", "superadmin") and not await is_superadmin(message.from_user.id):
        await message.reply("❌ Недостаточно прав для блокировки администратора.")
        return

    if target_role == "banned":
        await message.reply("ℹ️ Пользователь уже заблокирован.")
        return

    ok = await set_user_role(target_id, "banned")
    if not ok:
        await message.reply("❌ Пользователь не найден.")
        return

    await update_user_commands(message.bot, target_id, "banned")
    nick = await get_user_nickname(target_id)
    await message.reply(
        f"🚫  <b>Пользователь заблокирован</b>\n\n"
        f"🆔  <code>{target_id}</code>\n"
        f"👤  {esc(nick)}"
    )


@router.message(Command("unban"), admin_filter)
async def unban_cmd(message: Message, command: Command):
    if message.chat.type != "private":
        await message.reply("⚠️ Только в ЛС.")
        return

    arg = (command.args or "").strip()
    if not arg:
        await message.reply("✏️ Использование: <code>/unban USERID</code>")
        return

    try:
        target_id = int(arg)
    except ValueError:
        await message.reply("❌ USERID должен быть числом.")
        return

    if await get_user_role(target_id) != "banned":
        await message.reply("ℹ️ Пользователь не заблокирован.")
        return

    ok = await set_user_role(target_id, "user")
    if not ok:
        await message.reply("❌ Пользователь не найден.")
        return

    await update_user_commands(message.bot, target_id, "user")
    nick = await get_user_nickname(target_id)
    await message.reply(
        f"✅  <b>Пользователь разблокирован</b>\n\n"
        f"🆔  <code>{target_id}</code>\n"
        f"👤  {esc(nick)}"
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
                "SELECT COUNT(*) FROM users WHERE role IN ('admin', 'superadmin')"
            )
            admins = (await cur.fetchone())[0]

            cur = await db.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'banned'"
            )
            banned = (await cur.fetchone())[0]

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
                "SELECT nickname, coins FROM users WHERE role != 'banned' ORDER BY coins DESC LIMIT 1"
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
                f"📊  <b>Статистика бота</b>\n"
                f"<code>{'─' * 18}</code>\n\n"
                f"<b>👥 Пользователи</b>\n"
                f"  ·  Всего: <b>{fmt_num(total_users)}</b>\n"
                f"  ·  Активных: <b>{fmt_num(active_users)}</b>\n"
                f"  ·  Со стриком: <b>{fmt_num(streaked_users)}</b>\n"
                f"  ·  Админов: <b>{fmt_num(admins)}</b>\n"
                f"  ·  Заблокировано: <b>{fmt_num(banned)}</b>\n\n"
                f"<b>🪙 Монеты</b>\n"
                f"  ·  В обороте: <b>{fmt_num(total_coins)}</b>\n"
                f"  ·  В среднем: <b>{fmt_num(int(avg_coins))}</b>\n"
                f"  ·  Максимум: <b>{fmt_num(max_coins)}</b>\n\n"
                f"<b>💎 Кристаллы</b>\n"
                f"  ·  В обороте: <b>{fmt_num(total_gems)}</b>\n"
                f"  ·  Максимум: <b>{fmt_num(max_gems)}</b>\n\n"
                f"<b>🃏 Карточки</b>\n"
                f"  ·  Всего: <b>{fmt_num(total_cards)}</b>\n"
                + "\n".join(rarity_lines) + "\n\n"
                f"<b>🎒 Коллекции</b>\n"
                f"  ·  Экземпляров: <b>{fmt_num(total_owned)}</b>\n"
                f"  ·  Уникальных: <b>{fmt_num(unique_owned)}</b> / {fmt_num(total_cards)}\n"
                f"  ·  Коллекционеров: <b>{fmt_num(collectors)}</b>\n\n"
        )

        if top_user:
            text += (
                f"<b>🏆 Лидер по монетам</b>\n"
                f"  {esc(top_user['nickname'] or '—')} — <b>{fmt_num(top_user['coins'])}</b> 🪙\n\n"
            )
        if top_card:
            text += (
                f"<b>🔥 Популярная карточка</b>\n"
                f"  {esc(top_card['name'])} — <b>{fmt_num(top_card['cnt'])}</b> шт."
            )

        await message.reply(text)
    except Exception as e:
        logger.error(f"Ошибка /stats: {e}")
        await message.reply("❌ <b>Ошибка при сборе статистики.</b>")


@router.message(Command("setcoins"), admin_filter)
async def admin_setcoins(message: Message, command: Command):
    if message.chat.type != "private":
        await message.reply("⚠️ Команда доступна только в личных сообщениях.")
        return

    args = (command.args or "").strip().split()
    if not args:
        await message.reply(
            "✏️ Использование:\n"
            "<code>/setcoins COINS</code> — себе\n"
            "<code>/setcoins USERID COINS</code> — другому"
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
        await message.reply("❌ Слишком много аргументов.")
        return

    if target_id <= 0 or coins < 0:
        await message.reply("❌ Некорректные значения.")
        return

    async with get_db() as db:
        cur = await db.execute(
            "SELECT user_id, nickname, coins FROM users WHERE user_id = ?",
            (target_id,),
        )
        row = await cur.fetchone()
        if not row:
            await message.reply("❌ Пользователь не найден.")
            return

        old_coins = row["coins"] or 0
        await db.execute(
            "UPDATE users SET coins = ? WHERE user_id = ?", (coins, target_id)
        )

    who = "себе" if target_id == message.from_user.id else f"пользователю {esc(row['nickname'] or target_id)}"
    await message.reply(
        f"✅  <b>Баланс обновлён</b> ({who})\n\n"
        f"🆔  <code>{target_id}</code>\n"
        f"🪙  {fmt_num(old_coins)} → <b>{fmt_num(coins)}</b>"
    )


@router.message(Command("setgems"), admin_filter)
async def admin_setgems(message: Message, command: Command):
    if message.chat.type != "private":
        await message.reply("⚠️ Команда доступна только в личных сообщениях.")
        return

    args = (command.args or "").strip().split()
    if not args:
        await message.reply(
            "✏️ Использование:\n"
            "<code>/setgems N</code> — себе\n"
            "<code>/setgems USERID N</code> — другому"
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
        await message.reply("❌ Слишком много аргументов.")
        return

    if target_id <= 0 or gems < 0:
        await message.reply("❌ Некорректные значения.")
        return

    async with get_db() as db:
        cur = await db.execute(
            "SELECT user_id, nickname, gems FROM users WHERE user_id = ?",
            (target_id,),
        )
        row = await cur.fetchone()
        if not row:
            await message.reply("❌ Пользователь не найден.")
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
        f"✅  <b>Кристаллы обновлены</b> ({who})\n\n"
        f"🆔  <code>{target_id}</code>\n"
        f"💎  {fmt_num(old_gems)} → <b>{fmt_num(gems)}</b>"
    )


@router.message(Command("resetcd"), admin_filter)
async def admin_resetcd(message: Message, command: Command):
    if message.chat.type != "private":
        await message.reply("⚠️ Команда доступна только в личных сообщениях.")
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
        await message.reply("❌ Слишком много аргументов.")
        return

    if target_id <= 0:
        await message.reply("❌ Некорректный USERID.")
        return

    now = int(time.time())
    async with get_db() as db:
        cur = await db.execute(
            "SELECT user_id, nickname, last_claim FROM users WHERE user_id = ?",
            (target_id,),
        )
        row = await cur.fetchone()
        if not row:
            await message.reply("❌ Пользователь не найден.")
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
        f"✅  <b>Кулдаун сброшен</b> ({who})\n\n"
        f"🆔  <code>{target_id}</code>\n"
        f"⏱  {cd_info} — можно получать карточку сразу."
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

        lines = [f"👥  <b>Пользователи</b> (всего: {fmt_num(len(users))})\n"]
        chunk = ""
        parts = []

        for u in users:
            g = GENDERS.get(u["gender"] or "none", GENDERS["none"])
            role_info = ROLES.get(u["role"] or "user", ROLES["user"])
            reg_date = (
                datetime.fromtimestamp(u["registration"]).strftime("%d.%m.%Y")
                if u["registration"] else "—"
            )
            uid = u["user_id"]
            nick = u["nickname"] or f"User{uid}"
            line = (
                f"{role_info['icon']} <b>{esc(nick)}</b> "
                f"(<code>{uid}</code>)\n"
                f"    🪙 {fmt_num(u['coins'] or 0)} | "
                f"🃏 {fmt_num(u['cards_count'])} | "
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
            header = lines[0] if i == 0 else f"👥  <b>Пользователи</b> (продолжение {i + 1}/{len(parts)})\n"
            await message.reply(header + part)
    except Exception as e:
        logger.error(f"Ошибка /getusers: {e}")
        await message.reply("❌ <b>Ошибка при получении списка пользователей.</b>")


@router.message(Command("delcard"), admin_filter)
async def admin_delcard(message: Message, command: Command):
    if message.chat.type != "private":
        await message.reply("⚠️ Команда доступна только в личных сообщениях.")
        return

    arg = (command.args or "").strip()
    if not arg:
        await message.reply(
            "✏️ Использование: <code>/delcard ID</code>\n"
            "ID можно посмотреть в /admin → 📜 Список карточек."
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
        f"🗑  <b>Карточка удалена</b>\n\n"
        f"🆔  <code>{card_id}</code>\n"
        f"🃏  {esc(card['name'])}\n"
        f"{r_info.get('icon', '')}  {r_info.get('name', card['rarity'])}\n\n"
        f"👥  Затронуто: <b>{fmt_num(owners)}</b>\n"
        f"📦  Удалено экземпляров: <b>{fmt_num(owned_total)}</b>"
    )


# ================= [TEST] ТЕСТОВЫЕ КОМАНДЫ =================
@router.message(Command("migrate_crystals_to_gems"), admin_filter)
async def test_migrate_crystals_to_gems(message: Message):
    if message.chat.type != "private":
        await message.reply("⚠️ Только в ЛС.")
        return

    try:
        async with get_db() as db:
            cur = await db.execute("PRAGMA table_info(users)")
            cols = {row[1] for row in await cur.fetchall()}

            has_crystals = "crystals" in cols
            has_gems = "gems" in cols

            if has_gems and not has_crystals:
                await message.reply(
                    "✅  <b>[TEST] Миграция не нужна</b>\n\n"
                    "Колонка <code>gems</code> уже есть."
                )
                return

            if has_crystals and not has_gems:
                try:
                    await db.execute(
                        "ALTER TABLE users RENAME COLUMN crystals TO gems"
                    )
                    await message.reply(
                        "✅  <b>[TEST] Миграция выполнена</b>\n\n"
                        "Колонка <code>crystals</code> → <code>gems</code>."
                    )
                    return
                except Exception as e:
                    logger.warning(f"RENAME COLUMN failed, fallback: {e}")
                    await db.execute(
                        "ALTER TABLE users ADD COLUMN gems INTEGER DEFAULT 0"
                    )
                    await db.execute(
                        "UPDATE users SET gems = COALESCE(crystals, 0)"
                    )
                    await message.reply(
                        "✅  <b>[TEST] Миграция (fallback)</b>\n\n"
                        "Добавлена <code>gems</code>, данные скопированы."
                    )
                    return

            if has_crystals and has_gems:
                await db.execute(
                    "UPDATE users SET gems = COALESCE(gems, crystals, 0)"
                )
                await message.reply(
                    "✅  <b>[TEST] Миграция (merge)</b>\n\n"
                    "Данные слиты в <code>gems</code>."
                )
                return

            await db.execute(
                "ALTER TABLE users ADD COLUMN gems INTEGER DEFAULT 0"
            )
            await message.reply(
                "✅  <b>[TEST] Колонка gems создана</b>"
            )
    except Exception as e:
        logger.error(f"migrate_crystals_to_gems error: {e}")
        await message.reply(f"❌  <b>Ошибка миграции:</b> {esc(str(e))}")


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

    await message.answer(f"✅  <b>[TEST] Сброшено ников:</b> {count}")


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
    await message.answer(f"✅  <b>[TEST] Переведено в мифические:</b> {len(ids)} карточек")


@router.message(Command("getfileid"), admin_filter, F.photo)
async def test_get_file_id(message: Message):
    file_id = message.photo[-1].file_id
    await message.reply(
        f"🆔  <b>file_id:</b>\n<code>{esc(file_id)}</code>"
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
        f"✅  <b>[TEST] Деноминация выполнена</b>\n\n"
        f"👥  Пользователей: <b>{total}</b>\n"
        f"🪙  Суммарный баланс после: <b>{new_total_coins}</b>"
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
        f"✅  <b>[TEST] Дата регистрации установлена</b>\n\n"
        f"👥  Обновлено: <b>{fmt_num(affected)}</b>\n"
        f"🕐  Время: <b>{datetime.fromtimestamp(now).strftime('%d.%m.%Y %H:%M:%S')}</b>"
    )



# ================= МИГРАЦИЯ ФОТО ДЛЯ МИНИ-АППКИ =================
@router.message(Command("migrate_photos"), admin_filter)
async def cmd_migrate_photos(message: Message):
    """Скачивает все photo_id карточек на диск (card_photos/). Нужно для мини-аппки."""
    if message.chat.type != "private":
        await message.reply("⚠️ Только в ЛС.")
        return
    if not migrate_all_card_photos:
        await message.reply(
            "❌ Модуль <code>card_photos.py</code> не найден. "
            "Положи его рядом с main.py и перезапусти бота."
        )
        return
    status = await message.reply("⏳ Скачиваю фото карточек на диск…")
    try:
        async with get_db() as db:
            ok, fail = await migrate_all_card_photos(message.bot, db)
        await status.edit_text(
            f"✅  <b>Миграция фото завершена</b>\n\n"
            f"·  Успешно / уже было: <b>{ok}</b>\n"
            f"·  Ошибок: <b>{fail}</b>\n\n"
            f"Файлы: папка <code>card_photos</code> рядом с БД.\n"
            f"В мини-аппке картинки появятся после обновления."
        )
    except Exception as e:
        logger.error(f"migrate_photos: {e}")
        await status.edit_text(f"❌ Ошибка миграции: {esc(str(e))}")


# ================= ЗАПУСК =================
async def _run_api_server():
    """FastAPI для мини-аппки (тот же процесс, что и бот). Нужны fastapi + uvicorn."""
    try:
        import uvicorn
        from api import app as fastapi_app
    except ImportError as e:
        logger.warning(
            "API мини-аппки не запущен (нет fastapi/uvicorn или api.py): %s", e
        )
        return

    port = int(os.getenv("PORT", os.getenv("API_PORT", "8080")))
    logger.info("🌐 API мини-аппки на 0.0.0.0:%s", port)
    config = uvicorn.Config(
        fastapi_app,
        host="0.0.0.0",
        port=port,
        log_level="info",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    await server.serve()


async def main():
    try:
        os.makedirs(os.path.dirname(DB_NAME) or ".", exist_ok=True)
        if LOG_PATH:
            os.makedirs(os.path.dirname(LOG_PATH) or ".", exist_ok=True)
        await init_db()
        logger.info("База данных инициализирована")
        try:
            ensure_photo_dir()
        except Exception as _e:
            logger.warning("card photos dir: %s", _e)

        bot = Bot(
            token=BOT_TOKEN,
            default=DefaultBotProperties(
                parse_mode=ParseMode.HTML,
                link_preview=LinkPreviewOptions(is_disabled=True),
            ),
        )
        dp = Dispatcher(storage=MemoryStorage())
        dp.include_router(router)

        # Дефолтные команды для всех (без админских)
        await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
        logger.info("Меню команд (default) установлено")

        # START_API=1 или ENABLE_WEBAPP_API=1 — поднять FastAPI рядом с ботом (Bothost + домен)
        start_api = os.getenv("START_API", os.getenv("ENABLE_WEBAPP_API", "1")).strip() in (
            "1", "true", "yes", "on",
        )

        logger.info("🤖 Бот запущен")
        if start_api:
            await asyncio.gather(
                dp.start_polling(bot),
                _run_api_server(),
            )
        else:
            await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
