"""
Основной пользовательский бот карточек (без админки).
Админка — отдельный процесс: admin_bot.py (тот же DB_NAME).
"""
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
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand, BotCommandScopeDefault,
    CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, InputMediaPhoto, KeyboardButton, Message,
    ReplyKeyboardMarkup, LinkPreviewOptions, FSInputFile,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

try:
    from card_photos import (
        sync_card_photo,
        migrate_all_card_photos,
        ensure_photo_dir,
        get_card_photo_input,
        get_default_avatar_input,
        ensure_default_avatar,
        DEFAULT_AVATAR_PATH,
        DEFAULT_AVATAR_FILE_ID,
    )
except ImportError:
    sync_card_photo = None
    migrate_all_card_photos = None
    get_card_photo_input = None
    get_default_avatar_input = None
    ensure_default_avatar = None
    DEFAULT_AVATAR_PATH = None
    DEFAULT_AVATAR_FILE_ID = "AgACAgIAAxkBAAID12qql3EFpnb2HwTCE7Yn_Ri1TQsNAAKRIGsbfHlYSVUpxfU75O60AQADAgADeAADPQQ"

    def ensure_photo_dir():
        pass

# ================= КОНФИГУРАЦИЯ =================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN не задан. Укажите его в .env или окружении.")

WEBAPP_URL = (os.getenv("WEBAPP_URL") or "").strip()
BOT_USERNAME = (os.getenv("BOT_USERNAME", "milosttbot") or "milosttbot").lstrip("@").strip()
BOT_URL = f"https://t.me/{BOT_USERNAME}"
OPEN_APP_URL = f"{BOT_URL}?startapp"


def get_app_url() -> str:
    if not WEBAPP_URL or not BOT_USERNAME:
        return ""
    return OPEN_APP_URL


DB_NAME = os.getenv("DB_NAME", "/app/data/cards_game.db")
LOG_PATH = os.getenv("LOG_PATH", "/app/data/bot.log")
COOLDOWN_SECONDS = 3 * 3600  # 3 часа
INSTANT_COST = 150
INSTANT_MIN_COST = 5
NICKNAME_COST = 300

GROUP_AUTODELETE_SECONDS = 30
STREAK_EXPIRE_SECONDS = 24 * 3600

DICE_COOLDOWN_SECONDS = 5 * 60
DICE_MIN_BALANCE = 10
DICE_SPIN_DELAY = 3
DICE_VALUE_MAP = {1: -10, 2: -5, 3: 0, 4: 5, 5: 10, 6: 15}

DUEL_EXPIRE_SECONDS = 10 * 60
DUEL_MIN_STAKE = 1

# 🎉 message effect (private chats)
EFFECT_PARTY = "5046509860389126442"

RARITIES = {
    "common": {"icon": "⚪", "name": "Обычная", "weight": 50, "reward": 10},
    "rare": {"icon": "🔵", "name": "Редкая", "weight": 20, "reward": 25},
    "epic": {"icon": "🟣", "name": "Эпическая", "weight": 15, "reward": 50},
    "mythical": {"icon": "🔴", "name": "Мифическая", "weight": 10, "reward": 75},
    "legendary": {"icon": "🟡", "name": "Легендарная", "weight": 5, "reward": 100},
}

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
PROFILE_CMD_RE = re.compile(r"^мряу\s+профиль\s*$", re.IGNORECASE | re.UNICODE)
COLLECTION_CMD_RE = re.compile(r"^мряу\s+(коллекция|карточки)\s*$", re.IGNORECASE | re.UNICODE)
TOP_CMD_RE = re.compile(r"^мряу\s+топ\s*$", re.IGNORECASE | re.UNICODE)
HELP_CMD_RE = re.compile(r"^мряу\s+помощь\s*$", re.IGNORECASE | re.UNICODE)
DUEL_CMD_RE = re.compile(r"^мряу\s+дуэль\s+(\d+)\s*$", re.IGNORECASE | re.UNICODE)

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


def line(emoji: str, label: str, value) -> str:
    """Единый формат: {emoji} {label}: {value}"""
    return f"{emoji} {label}: {value}"


def instant_cost(remaining_seconds: int) -> int:
    if remaining_seconds <= 0:
        return INSTANT_MIN_COST
    if remaining_seconds >= COOLDOWN_SECONDS:
        return INSTANT_COST
    ratio = remaining_seconds / COOLDOWN_SECONDS
    cost = INSTANT_MIN_COST + (INSTANT_COST - INSTANT_MIN_COST) * ratio
    return max(INSTANT_MIN_COST, min(INSTANT_COST, round(cost)))


def dice_delta_from_value(value: int) -> int:
    return DICE_VALUE_MAP.get(value, 0)


def role_display(role: str) -> str:
    info = ROLES.get(role, ROLES["user"])
    return f"{info['icon']} {info['name']}"


def display_name(nickname: Optional[str], user_id: int, fallback_name: Optional[str] = None) -> str:
    if nickname and str(nickname).strip():
        return str(nickname).strip()
    if fallback_name and str(fallback_name).strip():
        return str(fallback_name).strip()[:32]
    return str(user_id)


# ================= CALLBACK DATA =================
class RaritySelectCallback(CallbackData, prefix="coll_rarity"):
    rarity: str
    page: int = 0
    user_id: int = 0


class MainMenuCallback(CallbackData, prefix="coll_main"):
    user_id: int = 0


class BackToProfileCallback(CallbackData, prefix="back_to_profile"):
    user_id: int = 0


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


class OkDeleteCallback(CallbackData, prefix="ok_del"):
    pass


class HelpCallback(CallbackData, prefix="help"):
    page: int = 0


class TopRefreshCallback(CallbackData, prefix="top_refresh"):
    kind: str


class DuelAcceptCallback(CallbackData, prefix="duel_acc"):
    duel_id: int


class DuelCancelCallback(CallbackData, prefix="duel_can"):
    duel_id: int


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
            CREATE TABLE IF NOT EXISTS cards (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                rarity     TEXT NOT NULL,
                photo_id   TEXT,
                photo_path TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id          INTEGER PRIMARY KEY,
                last_claim       INTEGER DEFAULT 0,
                role             TEXT    DEFAULT 'user',
                nickname         TEXT,
                coins            INTEGER DEFAULT 0,
                registration     INTEGER DEFAULT 0,
                streak           INTEGER DEFAULT 0,
                last_streak_date INTEGER DEFAULT 0,
                streak_bonus     INTEGER DEFAULT 0,
                last_dice        INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                user_id    INTEGER,
                card_id    INTEGER,
                claim_time INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, card_id),
                FOREIGN KEY (card_id) REFERENCES cards (id) ON DELETE CASCADE
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS duels (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                challenger_id INTEGER NOT NULL,
                opponent_id   INTEGER NOT NULL,
                stake         INTEGER NOT NULL,
                status        TEXT    DEFAULT 'pending',
                created_at    INTEGER NOT NULL,
                chat_id       INTEGER,
                message_id    INTEGER
            )
        """)

        for sql in (
            "CREATE INDEX IF NOT EXISTS idx_cards_rarity ON cards(rarity)",
            "CREATE INDEX IF NOT EXISTS idx_inventory_user ON inventory(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_inventory_claim_time ON inventory(claim_time)",
            "CREATE INDEX IF NOT EXISTS idx_users_coins ON users(coins)",
            "CREATE INDEX IF NOT EXISTS idx_users_streak ON users(streak)",
            "CREATE INDEX IF NOT EXISTS idx_users_role ON users(role)",
            "CREATE INDEX IF NOT EXISTS idx_duels_status ON duels(status)",
            "CREATE INDEX IF NOT EXISTS idx_duels_created ON duels(created_at)",
        ):
            await db.execute(sql)

        # Миграции: добавить недостающие колонки, убрать устаревшие данные
        for table, column, definition in [
            ("users", "registration", "INTEGER DEFAULT 0"),
            ("users", "streak", "INTEGER DEFAULT 0"),
            ("users", "last_streak_date", "INTEGER DEFAULT 0"),
            ("users", "streak_bonus", "INTEGER DEFAULT 0"),
            ("users", "last_dice", "INTEGER DEFAULT 0"),
            ("inventory", "claim_time", "INTEGER DEFAULT 0"),
            ("cards", "photo_path", "TEXT"),
            ("cards", "photo_id", "TEXT"),
        ]:
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            except aiosqlite.OperationalError:
                pass

        # Удаляем amount из inventory если осталась (SQLite не DROP COLUMN легко — игнорируем)
        # gender / gems больше не используем


# ================= РОЛИ =================
async def get_user_role(user_id: int) -> str:
    try:
        async with get_db() as db:
            cur = await db.execute("SELECT role FROM users WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            if not row:
                return "user"
            role = row[0] or "user"
            return role if role in ROLES else "user"
    except Exception as e:
        logger.error(f"get_user_role: {e}")
        return "user"


async def is_banned(user_id: int) -> bool:
    return (await get_user_role(user_id)) == "banned"


async def get_or_create_user(user_id: int, username: Optional[str] = None, full_name: Optional[str] = None):
    async with get_db() as db:
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = await cur.fetchone()
        if not user:
            now = int(time.time())
            cur = await db.execute("SELECT COUNT(*) FROM users")
            total = (await cur.fetchone())[0]
            role = "superadmin" if total == 0 else "user"
            # nickname = NULL по умолчанию
            await db.execute(
                "INSERT INTO users (user_id, nickname, registration, streak, last_streak_date, role) "
                "VALUES (?, NULL, ?, 0, 0, ?)",
                (user_id, now, role),
            )
            if role == "superadmin":
                logger.info(f"Первый пользователь {user_id} → superadmin")
            cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            user = await cur.fetchone()
        return user


async def get_user_display(user_id: int, fallback_name: Optional[str] = None) -> str:
    try:
        async with get_db() as db:
            cur = await db.execute("SELECT nickname FROM users WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            nick = row["nickname"] if row else None
            return display_name(nick, user_id, fallback_name)
    except Exception:
        return display_name(None, user_id, fallback_name)


async def get_user_row(user_id: int):
    async with get_db() as db:
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return await cur.fetchone()


def _owner_check(callback: CallbackQuery, target_user_id: int) -> bool:
    if target_user_id and callback.from_user.id != target_user_id:
        return False
    return True


# ================= FSM =================
class NicknameSG(StatesGroup):
    pending = State()


router = Router()


# ================= КЛАВИАТУРЫ =================
def get_app_kb(is_short=False):
    app_url = get_app_url()
    if not app_url:
        return None
    text = "📱 Открыть" if is_short else "📱 Мини-приложение"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, url=app_url)]]
    )


def get_profile_kb(owner_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🃏 Коллекция", callback_data=MainMenuCallback(user_id=owner_id).pack())
    b.button(text="✏️ Ник", callback_data=NicknameCallback(action="change").pack())
    b.adjust(2)
    return b.as_markup()


def get_main_km() -> ReplyKeyboardMarkup:
    """2×2: карточка, профиль, топ, мини-приложение."""
    rows = [
        [
            KeyboardButton(text="🃏 Получить карточку"),
            KeyboardButton(text="👤 Мой профиль"),
        ],
        [
            KeyboardButton(text="🏆 Топ игроков"),
            KeyboardButton(text="📱 Мини-приложение"),
        ],
    ]
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
    )


def get_ok_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✓ Понятно", callback_data=OkDeleteCallback().pack())
    return b.as_markup()


def get_card_action_keyboard(user_id: int, balance: int = 0,
                             remaining: int = COOLDOWN_SECONDS) -> InlineKeyboardMarkup:
    cost = instant_cost(remaining)
    b = InlineKeyboardBuilder()
    if balance >= cost:
        b.button(
            text=f"⚡ Сейчас · {fmt_num(cost)} 🪙",
            callback_data=CardActionCallback(action="instant", user_id=user_id).pack(),
        )
    b.button(text="Понятно ✓", callback_data=OkDeleteCallback().pack())
    b.adjust(1)
    return b.as_markup()


def get_after_card_keyboard(user_id: int, balance: int = 0) -> InlineKeyboardMarkup:
    cost = instant_cost(COOLDOWN_SECONDS)
    builder = InlineKeyboardBuilder()
    has_instant = balance >= cost
    app_url = get_app_url()

    if has_instant:
        builder.button(
            text=f"⚡ Ещё одну · {fmt_num(cost)} 🪙",
            callback_data=CardActionCallback(action="another", user_id=user_id).pack(),
        )
    builder.button(
        text="🃏 Коллекция",
        callback_data=CardActionCallback(action="collection", user_id=user_id).pack(),
    )
    if app_url:
        builder.button(text="📱 Мини-приложение", url=app_url)

    if has_instant and app_url:
        builder.adjust(1, 2)
    elif app_url:
        builder.adjust(2)
    else:
        builder.adjust(1)
    return builder.as_markup()


def get_top_keyboard(kind: str = "coins") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=("● " if kind == "coins" else "○ ") + "🪙",
             callback_data=TopCallback(kind="coins").pack())
    b.button(text=("● " if kind == "cards" else "○ ") + "🃏",
             callback_data=TopCallback(kind="cards").pack())
    b.button(text=("● " if kind == "streak" else "○ ") + "🔥",
             callback_data=TopCallback(kind="streak").pack())
    b.button(text="↻ Обновить", callback_data=TopRefreshCallback(kind=kind).pack())
    b.adjust(3, 1)
    return b.as_markup()


# ================= ОТОБРАЖЕНИЕ =================
async def get_user_photo(bot: Bot, user_id: int):
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if photos.total_count > 0:
            return photos.photos[0][-1].file_id
    except Exception as e:
        logger.debug(f"profile photo: {e}")
    # локальный дефолт
    local = get_default_avatar_input() if get_default_avatar_input else None
    if local:
        return local
    return None


async def render_profile(bot: Bot, user_id: int, fallback_name: Optional[str] = None):
    await burn_expired_streak(user_id)
    async with get_db() as db:
        cur = await db.execute("""
            SELECT u.nickname, u.coins, u.registration, u.streak, u.streak_bonus, u.role,
                   (SELECT COUNT(*) FROM inventory i WHERE i.user_id = u.user_id) AS cards_count
            FROM users u
            WHERE u.user_id = ?
        """, (user_id,))
        row = await cur.fetchone()
        cur = await db.execute("SELECT COUNT(*) FROM cards")
        total_cards = (await cur.fetchone())[0]

    if not row:
        return None, "❌ Пользователь не найден.", None

    nickname = display_name(row["nickname"], user_id, fallback_name)
    reg_date = datetime.fromtimestamp(row["registration"] or time.time()).strftime("%d.%m.%Y")
    role = row["role"] or "user"

    caption = (
        f"👤 <b>{esc(nickname)}</b>\n"
        f"{line('🆔', 'ID', f'<code>{user_id}</code>')}\n\n"
        f"{line('🎭', 'Роль', role_display(role))}\n"
        f"{line('📅', 'С', reg_date)}\n\n"
        f"{line('🃏', 'Карточки', f'<b>{fmt_num(row['cards_count'])}</b> / {fmt_num(total_cards)}')}\n"
        f"{line('🪙', 'Монеты', f'<b>{fmt_num(row['coins'])}</b>')}\n"
        f"{line('🔥', 'Стрик', f'<b>{fmt_days(row['streak'])}</b>')}"
    )
    kb = get_profile_kb(user_id)
    photo = await get_user_photo(bot, user_id)
    return photo, caption, kb


async def render_collection(bot: Bot, user_id: int, fallback_name: Optional[str] = None):
    keyboard, total, total_in_game = await get_collection_main_keyboard(user_id)
    nickname = await get_user_display(user_id, fallback_name)
    photo = await get_user_photo(bot, user_id)
    caption = (
        f"🃏 <b>Коллекция</b>\n\n"
        f"{line('👤', 'Игрок', esc(nickname))}\n"
        f"{line('📦', 'Карточек', f'<b>{fmt_num(total)}</b> из {fmt_num(total_in_game)}')}"
    )
    return photo, caption, keyboard, total


async def show_or_edit_photo(message: Message, photo, caption: str, keyboard=None):
    if photo is None:
        try:
            await message.answer(caption, reply_markup=keyboard)
        except Exception:
            pass
        return
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
            logger.error(f"edit_media: {e}")
    try:
        await message.answer_photo(photo=photo, caption=caption, reply_markup=keyboard)
    except TelegramBadRequest as e:
        logger.error(f"answer_photo: {e}")
        try:
            await message.answer(caption, reply_markup=keyboard)
        except Exception:
            pass


# ================= ВЫДАЧА КАРТОЧКИ (без дубликатов) =================
async def issue_card(user_id: int, check_cooldown: bool = True) -> Tuple[Optional[dict], str]:
    """Выдаёт только новую (не имеющуюся) карточку. Дубликатов нет."""
    try:
        async with get_db() as db:
            cur = await db.execute("SELECT COUNT(*) FROM cards")
            total_cards = (await cur.fetchone())[0]
            if total_cards == 0:
                return None, "no_cards"

            cur = await db.execute(
                "SELECT COUNT(*) FROM inventory WHERE user_id = ?", (user_id,)
            )
            owned = (await cur.fetchone())[0]
            if owned >= total_cards:
                return None, "all_collected"

            rarities_list = list(RARITIES.keys())
            weights = [RARITIES[r]["weight"] for r in rarities_list]
            card = None
            selected_rarity = None

            for _ in range(12):
                selected_rarity = random.choices(rarities_list, weights=weights, k=1)[0]
                cur = await db.execute(
                    """SELECT id, name, photo_id, photo_path, rarity
                       FROM cards
                       WHERE rarity = ?
                         AND id NOT IN (SELECT card_id FROM inventory WHERE user_id = ?)
                       ORDER BY RANDOM() LIMIT 1""",
                    (selected_rarity, user_id),
                )
                card = await cur.fetchone()
                if card:
                    break

            if not card:
                cur = await db.execute(
                    """SELECT id, name, photo_id, photo_path, rarity
                       FROM cards
                       WHERE id NOT IN (SELECT card_id FROM inventory WHERE user_id = ?)
                       ORDER BY RANDOM() LIMIT 1""",
                    (user_id,),
                )
                card = await cur.fetchone()
                if card:
                    selected_rarity = card["rarity"]

            if not card:
                return None, "all_collected"

            card_id = card["id"]
            card_name = card["name"]
            photo_id = card["photo_id"]
            photo_path = card["photo_path"]
            base_coins = RARITIES[selected_rarity]["reward"]
            coins_earned = base_coins
            now = int(time.time())

            if check_cooldown:
                await db.execute(
                    """INSERT INTO users (user_id, last_claim, coins)
                       VALUES (?, ?, ?)
                       ON CONFLICT(user_id) DO UPDATE SET last_claim = ?,
                                                          coins = coins + ?""",
                    (user_id, now, coins_earned, now, coins_earned),
                )
            else:
                await db.execute(
                    "UPDATE users SET coins = coins + ? WHERE user_id = ?",
                    (coins_earned, user_id),
                )

            await db.execute(
                """INSERT INTO inventory (user_id, card_id, claim_time)
                   VALUES (?, ?, ?)
                   ON CONFLICT(user_id, card_id) DO UPDATE SET claim_time = ?""",
                (user_id, card_id, now, now),
            )

            cur = await db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
            balance = (await cur.fetchone())["coins"]

            return {
                "id": card_id,
                "name": card_name,
                "photo_id": photo_id,
                "photo_path": photo_path,
                "rarity": selected_rarity,
                "coins_earned": coins_earned,
                "balance": balance,
            }, "success"
    except Exception as e:
        logger.error(f"issue_card: {e}")
        return None, "error"


# ================= СТРИК =================
async def burn_expired_streak(user_id: int) -> bool:
    try:
        now_ts = int(time.time())
        async with get_db() as db:
            cur = await db.execute(
                "SELECT streak, last_claim FROM users WHERE user_id = ?", (user_id,)
            )
            row = await cur.fetchone()
            if not row:
                return False
            streak = row["streak"] or 0
            last_claim = row["last_claim"] or 0
            if streak > 0 and last_claim > 0 and (now_ts - last_claim) >= STREAK_EXPIRE_SECONDS:
                await db.execute(
                    "UPDATE users SET streak = 0, last_streak_date = 0, streak_bonus = 0 WHERE user_id = ?",
                    (user_id,),
                )
                return True
        return False
    except Exception as e:
        logger.error(f"burn_streak: {e}")
        return False


async def check_and_update_streak(user_id: int) -> Tuple[int, int, int, bool]:
    """(streak, coin_bonus, balance, streak_updated)"""
    try:
        now_ts = int(time.time())
        now = datetime.now()
        today_start = int(datetime(now.year, now.month, now.day).timestamp())
        today_end = today_start + 86400 - 1
        yesterday_start = int((now - timedelta(days=1)).replace(hour=0, minute=0, second=0).timestamp())
        yesterday_end = yesterday_start + 86400 - 1

        async with get_db() as db:
            cur = await db.execute(
                "SELECT streak, last_streak_date, streak_bonus, coins, last_claim FROM users WHERE user_id = ?",
                (user_id,),
            )
            row = await cur.fetchone()
            if not row:
                return 0, 0, 0, False

            streak = row["streak"] or 0
            last_date = row["last_streak_date"] or 0
            balance = row["coins"] or 0
            last_claim = row["last_claim"] or 0

            if last_claim > 0 and (now_ts - last_claim) >= STREAK_EXPIRE_SECONDS:
                if streak > 0:
                    await db.execute(
                        "UPDATE users SET streak = 0, last_streak_date = 0, streak_bonus = 0 WHERE user_id = ?",
                        (user_id,),
                    )
                return 0, 0, balance, False

            if today_start <= last_date <= today_end:
                return streak, 0, balance, False

            is_consecutive = streak > 0 and yesterday_start <= last_date <= yesterday_end
            new_streak = (streak + 1) if is_consecutive else 1
            new_bonus = 0 if new_streak == 1 else next(
                b for d, b in STREAK_BONUSES if new_streak <= d
            )

            await db.execute(
                "UPDATE users SET streak = ?, last_streak_date = ?, streak_bonus = ?, coins = coins + ? WHERE user_id = ?",
                (new_streak, now_ts, new_bonus, new_bonus, user_id),
            )
            cur = await db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
            new_balance = (await cur.fetchone())[0]
            return new_streak, new_bonus, new_balance, True
    except Exception as e:
        logger.error(f"update_streak: {e}")
        return 0, 0, 0, False


def _streak_message_text(streak: int, bonus: int, new_balance: int) -> str:
    if streak <= 0:
        return ""
    if streak == 1:
        return (
            "🔥 <b>Стрик начат!</b>\n\n"
            f"{line('💡', 'Подсказка', 'Заходите каждый день — стрик растёт.')}"
        )
    text = f"🔥 <b>Стрик</b>\n\n{line('📅', 'Дней', f'<b>{fmt_days(streak)}</b>')}"
    if bonus > 0:
        text += f"\n{line('🪙', 'Бонус', f'<b>+{fmt_num(bonus)}</b> → {fmt_num(new_balance)}')}"
    text += f"\n{line('💡', 'Подсказка', 'Заходите ежедневно, чтобы не потерять стрик.')}"
    return text


async def send_streak_notice(message: Message, streak: int, bonus: int, new_balance: int, streak_updated: bool):
    if not streak_updated or streak <= 0:
        return
    text = _streak_message_text(streak, bonus, new_balance)
    if not text:
        return
    try:
        sent = await message.reply(text)
        if message.chat.type != "private":
            asyncio.create_task(_auto_delete_pair(sent, None, GROUP_AUTODELETE_SECONDS))
    except Exception as e:
        logger.debug(f"streak notice: {e}")


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
        logger.debug(f"try_delete: {e}")


async def _auto_delete_pair(bot_msg: Message, user_msg: Optional[Message], delay: int):
    await asyncio.sleep(delay)
    await _try_delete(bot_msg)
    await _try_delete(user_msg)


async def reply_ephemeral(message: Message, text: str, **kwargs):
    if "reply_markup" not in kwargs:
        kwargs["reply_markup"] = get_ok_kb()
    sent = await message.reply(text, **kwargs)
    if message.chat.type != "private":
        asyncio.create_task(_auto_delete_pair(sent, message, GROUP_AUTODELETE_SECONDS))
    return sent


# ================= БАН =================
async def check_not_banned(message: Message) -> bool:
    if await is_banned(message.from_user.id):
        await reply_ephemeral(
            message,
            "🚫 <b>Вы заблокированы</b>\n\n"
            f"{line('ℹ️', 'Инфо', 'Доступ ограничен. Обратитесь к администрации.')}",
        )
        return False
    return True


async def check_not_banned_cb(callback: CallbackQuery) -> bool:
    if await is_banned(callback.from_user.id):
        await callback.answer("🚫 Вы заблокированы", show_alert=True)
        return False
    return True


# ================= СТАРТ =================
USER_COMMANDS = [
    BotCommand(command="start", description="👋 Запуск бота"),
    BotCommand(command="meow", description="🃏 Получить карточку"),
    BotCommand(command="miniapp", description="📱 Мини-приложение"),
    BotCommand(command="top", description="🏆 Топ"),
    BotCommand(command="dice", description="🎲 Кубик"),
    BotCommand(command="duel", description="⚔️ Дуэль (в группах)"),
    BotCommand(command="help", description="❓ Помощь"),
    BotCommand(command="profile", description="👤 Профиль"),
    BotCommand(command="collection", description="🃏 Коллекция"),
    BotCommand(command="nickname", description="✏️ Сменить ник"),
]


@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message):
    try:
        await get_or_create_user(
            message.from_user.id, message.from_user.username, message.from_user.full_name
        )
        if not await check_not_banned(message):
            return

        try:
            await message.answer_sticker(
                sticker="CAACAgIAAxkBAALL7WqWuuWDYuQk4iqY7tNu_-7zLZqyAAJengACoj5pSQH9iX-5QhicPQQ"
            )
        except Exception:
            pass

        welcome = (
            "👋 <b>Привет!</b>\n\n"
            f"{line('🃏', 'Карточка', 'напишите «мряу» или нажмите кнопку')}\n"
            f"{line('⏱', 'Бесплатно', 'раз в 3 часа')}\n"
            f"{line('⚡', 'Мгновенно', 'за монеты')}\n"
            f"{line('⚔️', 'Дуэль', '/duel ставка — ответом в группе')}"
        )
        await message.reply(welcome, reply_markup=get_main_km())
    except Exception as e:
        logger.error(f"cmd_start: {e}")


# ================= ПОМОЩЬ =================
HELP_PAGES = [
    {
        "title": "📖 Команды",
        "body": (
            f"<b>🃏 Получить карточку</b>\n"
            f"{line('•', 'Команды', '«мряу» · /meow · кнопка')}\n"
            f"{line('⏱', 'Бесплатно', 'раз в 3 часа')}\n"
            f"{line('⚡', 'Мгновенно', f'от {INSTANT_MIN_COST} до {INSTANT_COST} 🪙')}\n\n"
            f"<b>👤 Профиль</b>\n"
            f"{line('•', 'Команды', '«мряу профиль» · /profile')}\n\n"
            f"<b>🃏 Коллекция</b>\n"
            f"{line('•', 'Команды', '«мряу коллекция» · /collection')}"
        ),
    },
    {
        "title": "📖 Стрик и награды",
        "body": (
            f"<b>🔥 Стрик</b>\n"
            f"{line('•', 'Правило', 'карточка каждый день')}\n"
            f"{line('⏱', 'Сгорание', 'если 24 ч без карточки')}\n"
            f"{line('🪙', 'Бонус', 'монеты за дни стрика')}\n\n"
            f"<b>🃏 Редкости</b>\n"
            + "\n".join(
                line(v["icon"], v["name"], f"{v['reward']} 🪙")
                for v in RARITIES.values()
            )
        ),
    },
    {
        "title": "📖 Кубик и дуэль",
        "body": (
            f"<b>🎲 Кубик</b>\n"
            f"{line('•', 'Команды', '/dice · «мряу кубик»')}\n"
            f"{line('⏱', 'Кулдаун', '5 мин')}\n"
            f"{line('🪙', 'Минимум', f'{DICE_MIN_BALANCE} 🪙')}\n"
            f"{line('📊', '1–6', '−10 · −5 · 0 · +5 · +10 · +15')}\n\n"
            f"<b>⚔️ Дуэль</b>\n"
            f"{line('•', 'Где', 'только в группах')}\n"
            f"{line('•', 'Как', '/duel ставка — ответом на сообщение')}\n"
            f"{line('⏱', 'Заявка', '10 минут')}\n"
            f"{line('🏆', 'Банк', '100% победителю')}"
        ),
    },
    {
        "title": "📖 Профиль и топ",
        "body": (
            f"<b>✏️ Ник</b>\n"
            f"{line('•', 'Смена', f'/nickname НовыйНик · {NICKNAME_COST} 🪙')}\n"
            f"{line('•', 'Сброс', '/nickname reset · бесплатно')}\n"
            f"{line('•', 'Длина', '2–32 символа, без ссылок')}\n\n"
            f"<b>🏆 Топ</b>\n"
            f"{line('•', 'Команды', '«мряу топ» · /top')}\n"
            f"{line('•', 'Режимы', 'монеты / карточки / стрик')}"
        ),
    },
]


def get_help_keyboard(page: int) -> InlineKeyboardMarkup:
    total = len(HELP_PAGES)
    page = max(0, min(page, total - 1))
    b = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="‹", callback_data=HelpCallback(page=page - 1).pack()))
    nav.append(InlineKeyboardButton(text=f"{page + 1} / {total}", callback_data="ignore"))
    if page < total - 1:
        nav.append(InlineKeyboardButton(text="›", callback_data=HelpCallback(page=page + 1).pack()))
    b.row(*nav)
    return b.as_markup()


def build_help_text(page: int) -> str:
    total = len(HELP_PAGES)
    page = max(0, min(page, total - 1))
    p = HELP_PAGES[page]
    return f"<b>{p['title']}</b>\n{'─' * 20}\n\n{p['body']}"


@router.message(Command("help"))
@router.message(F.text.regexp(HELP_CMD_RE))
async def cmd_help(message: Message):
    if not await check_not_banned(message):
        return
    await message.reply(build_help_text(0), reply_markup=get_help_keyboard(0))


@router.callback_query(HelpCallback.filter())
async def help_page_callback(callback: CallbackQuery, callback_data: HelpCallback):
    if not await check_not_banned_cb(callback):
        return
    try:
        text = build_help_text(callback_data.page)
        kb = get_help_keyboard(callback_data.page)
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            await callback.message.answer(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        logger.error(f"help: {e}")
        await callback.answer("⚠️ Ошибка")


# ================= МИНИ-ПРИЛОЖЕНИЕ =================
@router.message(F.text == "📱 Мини-приложение")
@router.message(Command("miniapp"))
async def cmd_miniapp(message: Message):
    if not await check_not_banned(message):
        return
    app_kb = get_app_kb(is_short=True)
    if not app_kb:
        await message.reply("📱 Мини-приложение пока недоступно.")
        return
    await message.reply(
        f"📱 <b>Мини-приложение</b>\n\n"
        f"{line('ℹ️', 'Инфо', 'Откройте прямо в Telegram')}",
        reply_markup=app_kb,
    )


# ================= ПОЛУЧЕНИЕ КАРТОЧКИ =================
def _resolve_card_photo(card: dict):
    if get_card_photo_input:
        local = get_card_photo_input(card["id"], card.get("photo_path"))
        if local:
            return local
    # fallback file_id если локального нет
    if card.get("photo_id"):
        return card["photo_id"]
    return None


def _card_caption(name: str, card: dict) -> str:
    r = RARITIES[card["rarity"]]
    return (
        f"✨ <b>Новая карточка</b>\n\n"
        f"{line('🃏', 'Название', f'<b>{esc(card['name'])}</b>')}\n"
        f"{line(r['icon'], 'Редкость', r['name'])}\n"
        f"{line('🪙', 'Награда', f'+<b>{fmt_num(card['coins_earned'])}</b> → {fmt_num(card['balance'])}')}"
    )


@router.message(F.text == "🃏 Получить карточку")
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
        await get_or_create_user(user_id, message.from_user.username, message.from_user.full_name)
        nickname = await get_user_display(user_id, message.from_user.full_name)

        async with get_db() as db:
            cur = await db.execute(
                "SELECT last_claim, coins FROM users WHERE user_id = ?", (user_id,)
            )
            row = await cur.fetchone()
            last_claim = row["last_claim"] if row else 0
            balance = row["coins"] if row else 0

        time_passed = now - last_claim
        if time_passed < COOLDOWN_SECONDS:
            # Стрик обновляем даже на кулдауне (раз в день)
            streak, bonus, new_balance, streak_updated = await check_and_update_streak(user_id)
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
                f"⏳ <b>{esc(nickname)}</b>\n\n"
                f"{line('⏱', 'Следующая карточка', f'<b>{time_str}</b>')}"
            )
            await reply_ephemeral(
                message, text,
                reply_markup=get_card_action_keyboard(user_id, balance, remaining=remaining),
            )
            await send_streak_notice(message, streak, bonus, new_balance, streak_updated)
            return

        card, status = await issue_card(user_id, check_cooldown=True)

        # Стрик капает даже если карточек нет / всё собрано
        streak, bonus, new_balance, streak_updated = await check_and_update_streak(user_id)

        if status == "no_cards":
            await reply_ephemeral(
                message,
                f"❌ <b>В базе пока нет карточек</b>\n\n"
                f"{line('ℹ️', 'Инфо', 'Обратитесь к администрации.')}",
            )
            await send_streak_notice(message, streak, bonus, new_balance, streak_updated)
            return

        if status == "all_collected":
            await reply_ephemeral(
                message,
                f"🎉 <b>Все карточки собраны!</b>\n\n"
                f"{line('ℹ️', 'Инфо', 'Новых карточек пока нет.')}",
            )
            await send_streak_notice(message, streak, bonus, new_balance, streak_updated)
            return

        if status != "success" or card is None:
            await reply_ephemeral(message, "❌ Произошла ошибка. Попробуйте позже.")
            return

        caption = _card_caption(nickname, card)
        kb = get_after_card_keyboard(user_id, card["balance"])
        photo = _resolve_card_photo(card)

        effect = None
        if card["rarity"] in ("mythical", "legendary") and message.chat.type == "private":
            effect = EFFECT_PARTY

        try:
            kwargs = {"caption": caption, "reply_markup": kb}
            if effect:
                kwargs["message_effect_id"] = effect
            if photo:
                await message.reply_photo(photo=photo, **kwargs)
            else:
                await message.reply(caption, reply_markup=kb)
        except TypeError:
            # старый aiogram без message_effect_id
            if photo:
                await message.reply_photo(photo=photo, caption=caption, reply_markup=kb)
            else:
                await message.reply(caption, reply_markup=kb)
        except TelegramBadRequest as e:
            logger.error(f"reply card: {e}")
            try:
                if photo:
                    await message.reply_photo(photo=photo, caption=caption, reply_markup=kb)
                else:
                    await message.reply(caption, reply_markup=kb)
            except Exception:
                await message.reply(caption)

        await send_streak_notice(message, streak, bonus, new_balance, streak_updated)
    except Exception as e:
        logger.error(f"get_card_handler: {e}")
        await reply_ephemeral(message, "❌ Произошла ошибка. Попробуйте позже.")


# ================= КУБИК =================
@router.message(Command("dice"))
@router.message(F.text.regexp(DICE_CMD_RE))
async def dice_handler(message: Message):
    if not await check_not_banned(message):
        return
    user_id = message.from_user.id

    if rate_limited(f"dice:{user_id}", limit=6, window=15):
        await reply_ephemeral(message, "⏳ Слишком часто. Подождите.")
        return
    if message.chat.type != "private":
        if rate_limited(f"dice-chat:{message.chat.id}", limit=15, window=15):
            return

    try:
        await get_or_create_user(user_id, message.from_user.username, message.from_user.full_name)
        nickname = await get_user_display(user_id, message.from_user.full_name)
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
                    f"⚠️ <b>Недостаточно монет</b>\n\n"
                    f"{line('🪙', 'Нужно', f'<b>{fmt_num(DICE_MIN_BALANCE)}</b>')}\n"
                    f"{line('🪙', 'У вас', f'<b>{fmt_num(balance)}</b>')}",
                )
                return

            time_passed = now - last_dice
            if last_dice > 0 and time_passed < DICE_COOLDOWN_SECONDS:
                remaining = DICE_COOLDOWN_SECONDS - time_passed
                m, s = remaining // 60, remaining % 60
                time_str = f"{m} мин {s} сек" if m else f"{s} сек"
                await reply_ephemeral(
                    message,
                    f"⏳ <b>{esc(nickname)}</b>\n\n"
                    f"{line('⏱', 'Кубик через', f'<b>{time_str}</b>')}",
                )
                return

            await db.execute(
                "UPDATE users SET last_dice = ? WHERE user_id = ?", (now, user_id)
            )

        spin_msg = await message.reply_dice(emoji="🎲")
        await asyncio.sleep(DICE_SPIN_DELAY)

        dice_value = 1
        if spin_msg.dice and spin_msg.dice.value:
            dice_value = int(spin_msg.dice.value)
        delta = dice_delta_from_value(dice_value)

        async with get_db() as db:
            cur = await db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            balance = (row["coins"] or 0) if row else 0
            new_balance = max(0, balance + delta)
            if new_balance == 0 and delta < 0:
                delta = -balance
            await db.execute(
                "UPDATE users SET coins = ? WHERE user_id = ?", (new_balance, user_id)
            )

        if delta > 0:
            delta_str = f"+{fmt_num(delta)}"
            title = "🎉 Выигрыш"
        elif delta < 0:
            delta_str = f"−{fmt_num(abs(delta))}"
            title = "😔 Проигрыш"
        else:
            delta_str = "±0"
            title = "· Ничья"

        result = (
            f"🎲 <b>{title}</b>\n\n"
            f"{line('🎲', 'Выпало', f'<b>{dice_value}</b>')}\n"
            f"{line('🪙', 'Результат', f'<b>{delta_str}</b>')}\n"
            f"{line('🪙', 'Баланс', f'<b>{fmt_num(new_balance)}</b>')}"
        )
        try:
            await spin_msg.reply(result)
        except TelegramBadRequest:
            await message.reply(result)
    except Exception as e:
        logger.error(f"dice: {e}")
        await reply_ephemeral(message, "❌ Произошла ошибка.")


# ================= ДУЭЛИ =================
async def expire_old_duels():
    now = int(time.time())
    async with get_db() as db:
        await db.execute(
            "UPDATE duels SET status = 'expired' "
            "WHERE status = 'pending' AND (? - created_at) >= ?",
            (now, DUEL_EXPIRE_SECONDS),
        )


@router.message(Command("duel"))
@router.message(F.text.regexp(DUEL_CMD_RE))
async def duel_create(message: Message, command: CommandObject = None):
    if not await check_not_banned(message):
        return

    if message.chat.type == "private":
        await reply_ephemeral(message, "⚠️ Дуэли работают только в группах.")
        return

    # ставка
    stake = None
    if command and command.args:
        try:
            stake = int(command.args.strip().split()[0])
        except (ValueError, IndexError):
            pass
    if stake is None and message.text:
        m = DUEL_CMD_RE.match(message.text.strip())
        if m:
            stake = int(m.group(1))
        else:
            parts = message.text.strip().split()
            if len(parts) >= 2 and parts[0].lower() in ("/duel", "мряу"):
                try:
                    stake = int(parts[-1])
                except ValueError:
                    pass

    if stake is None or stake < DUEL_MIN_STAKE:
        await reply_ephemeral(
            message,
            f"✏️ Использование: ответом на сообщение\n"
            f"<code>/duel {DUEL_MIN_STAKE}</code> (ставка в монетах)",
        )
        return

    if not message.reply_to_message or not message.reply_to_message.from_user:
        await reply_ephemeral(message, "⚠️ Ответьте на сообщение игрока, которого вызываете.")
        return

    opponent = message.reply_to_message.from_user
    challenger = message.from_user

    if opponent.is_bot:
        await reply_ephemeral(message, "⚠️ Нельзя вызвать бота.")
        return
    if opponent.id == challenger.id:
        await reply_ephemeral(message, "⚠️ Нельзя вызвать самого себя.")
        return

    if rate_limited(f"duel:{challenger.id}", limit=5, window=30):
        await reply_ephemeral(message, "⏳ Слишком часто.")
        return

    await expire_old_duels()
    await get_or_create_user(challenger.id, challenger.username, challenger.full_name)
    await get_or_create_user(opponent.id, opponent.username, opponent.full_name)

    async with get_db() as db:
        cur = await db.execute("SELECT coins FROM users WHERE user_id = ?", (challenger.id,))
        ch_row = await cur.fetchone()
        cur = await db.execute("SELECT coins FROM users WHERE user_id = ?", (opponent.id,))
        op_row = await cur.fetchone()
        ch_coins = (ch_row["coins"] or 0) if ch_row else 0
        op_coins = (op_row["coins"] or 0) if op_row else 0

        if ch_coins < stake:
            await reply_ephemeral(
                message,
                f"⚠️ Недостаточно монет у вас.\n"
                f"{line('🪙', 'Нужно', f'<b>{fmt_num(stake)}</b>')}\n"
                f"{line('🪙', 'У вас', f'<b>{fmt_num(ch_coins)}</b>')}",
            )
            return
        if op_coins < stake:
            await reply_ephemeral(
                message,
                f"⚠️ У соперника недостаточно монет.\n"
                f"{line('🪙', 'Нужно', f'<b>{fmt_num(stake)}</b>')}\n"
                f"{line('🪙', 'У соперника', f'<b>{fmt_num(op_coins)}</b>')}",
            )
            return

        # уже есть pending дуэль между ними?
        cur = await db.execute(
            """SELECT id FROM duels
               WHERE status = 'pending'
                 AND ((challenger_id = ? AND opponent_id = ?)
                   OR (challenger_id = ? AND opponent_id = ?))""",
            (challenger.id, opponent.id, opponent.id, challenger.id),
        )
        if await cur.fetchone():
            await reply_ephemeral(message, "⚠️ Уже есть активная заявка между вами.")
            return

        now = int(time.time())
        cur = await db.execute(
            """INSERT INTO duels (challenger_id, opponent_id, stake, status, created_at, chat_id)
               VALUES (?, ?, ?, 'pending', ?, ?)""",
            (challenger.id, opponent.id, stake, now, message.chat.id),
        )
        duel_id = cur.lastrowid

    ch_name = await get_user_display(challenger.id, challenger.full_name)
    op_name = await get_user_display(opponent.id, opponent.full_name)
    bank = stake * 2

    b = InlineKeyboardBuilder()
    b.button(text="✅ Принять", callback_data=DuelAcceptCallback(duel_id=duel_id).pack())
    b.button(text="✕ Отмена", callback_data=DuelCancelCallback(duel_id=duel_id).pack())
    b.adjust(2)

    text = (
        f"⚔️ <b>Вызов на дуэль</b>\n\n"
        f"{line('👤', 'Вызывающий', esc(ch_name))}\n"
        f"{line('👤', 'Соперник', esc(op_name))}\n"
        f"{line('🪙', 'Ставка', f'<b>{fmt_num(stake)}</b>')}\n"
        f"{line('🏦', 'Банк', f'<b>{fmt_num(bank)}</b>')}\n"
        f"{line('⏱', 'Истекает', 'через 10 мин')}"
    )
    sent = await message.reply(text, reply_markup=b.as_markup())
    async with get_db() as db:
        await db.execute(
            "UPDATE duels SET message_id = ? WHERE id = ?", (sent.message_id, duel_id)
        )


@router.callback_query(DuelCancelCallback.filter())
async def duel_cancel(callback: CallbackQuery, callback_data: DuelCancelCallback):
    if not await check_not_banned_cb(callback):
        return
    duel_id = callback_data.duel_id
    user_id = callback.from_user.id

    async with get_db() as db:
        cur = await db.execute("SELECT * FROM duels WHERE id = ?", (duel_id,))
        duel = await cur.fetchone()
        if not duel:
            await callback.answer("Заявка не найдена", show_alert=True)
            return
        if duel["status"] != "pending":
            await callback.answer("Заявка уже закрыта", show_alert=True)
            return
        if user_id not in (duel["challenger_id"], duel["opponent_id"]):
            await callback.answer("Это не ваша дуэль", show_alert=True)
            return
        await db.execute(
            "UPDATE duels SET status = 'cancelled' WHERE id = ?", (duel_id,)
        )

    try:
        await callback.message.edit_text(
            f"✕ <b>Дуэль отменена</b>\n\n{line('🆔', 'ID', duel_id)}"
        )
    except TelegramBadRequest:
        pass
    await callback.answer("Отменено")


@router.callback_query(DuelAcceptCallback.filter())
async def duel_accept(callback: CallbackQuery, callback_data: DuelAcceptCallback):
    if not await check_not_banned_cb(callback):
        return
    duel_id = callback_data.duel_id
    user_id = callback.from_user.id
    await expire_old_duels()

    async with get_db() as db:
        cur = await db.execute("SELECT * FROM duels WHERE id = ?", (duel_id,))
        duel = await cur.fetchone()
        if not duel:
            await callback.answer("Заявка не найдена", show_alert=True)
            return
        if duel["status"] == "expired":
            await callback.answer("Заявка истекла", show_alert=True)
            return
        if duel["status"] != "pending":
            await callback.answer("Заявка уже закрыта", show_alert=True)
            return
        if user_id != duel["opponent_id"]:
            await callback.answer("Принять может только вызванный игрок", show_alert=True)
            return

        stake = duel["stake"]
        ch_id = duel["challenger_id"]
        op_id = duel["opponent_id"]

        cur = await db.execute("SELECT coins FROM users WHERE user_id = ?", (ch_id,))
        ch_coins = ((await cur.fetchone()) or {"coins": 0})["coins"] or 0
        cur = await db.execute("SELECT coins FROM users WHERE user_id = ?", (op_id,))
        op_coins = ((await cur.fetchone()) or {"coins": 0})["coins"] or 0

        if ch_coins < stake or op_coins < stake:
            await db.execute("UPDATE duels SET status = 'cancelled' WHERE id = ?", (duel_id,))
            await callback.answer("Недостаточно монет — дуэль отменена", show_alert=True)
            try:
                await callback.message.edit_text("✕ Дуэль отменена: недостаточно монет.")
            except TelegramBadRequest:
                pass
            return

        # списываем ставки
        await db.execute("UPDATE users SET coins = coins - ? WHERE user_id = ?", (stake, ch_id))
        await db.execute("UPDATE users SET coins = coins - ? WHERE user_id = ?", (stake, op_id))
        await db.execute("UPDATE duels SET status = 'accepted' WHERE id = ?", (duel_id,))

    await callback.answer("Дуэль началась!")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    chat_id = callback.message.chat.id
    bot = callback.bot

    # два кубика
    try:
        d1 = await bot.send_dice(chat_id=chat_id, emoji="🎲")
        await asyncio.sleep(DICE_SPIN_DELAY)
        d2 = await bot.send_dice(chat_id=chat_id, emoji="🎲")
        await asyncio.sleep(DICE_SPIN_DELAY)
        v1 = int(d1.dice.value) if d1.dice else 1
        v2 = int(d2.dice.value) if d2.dice else 1
    except Exception as e:
        logger.error(f"duel dice: {e}")
        # возврат ставок
        async with get_db() as db:
            await db.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?", (stake, ch_id))
            await db.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?", (stake, op_id))
            await db.execute("UPDATE duels SET status = 'cancelled' WHERE id = ?", (duel_id,))
        await callback.message.answer("❌ Ошибка кубиков. Ставки возвращены.")
        return

    bank = stake * 2
    ch_name = await get_user_display(ch_id)
    op_name = await get_user_display(op_id)

    if v1 > v2:
        winner_id, winner_name, loser_name = ch_id, ch_name, op_name
        result_title = "🏆 Победа"
    elif v2 > v1:
        winner_id, winner_name, loser_name = op_id, op_name, ch_name
        result_title = "🏆 Победа"
    else:
        winner_id = None
        result_title = "🤝 Ничья"

    async with get_db() as db:
        if winner_id:
            await db.execute(
                "UPDATE users SET coins = coins + ? WHERE user_id = ?", (bank, winner_id)
            )
            await db.execute("UPDATE duels SET status = 'completed' WHERE id = ?", (duel_id,))
            cur = await db.execute("SELECT coins FROM users WHERE user_id = ?", (winner_id,))
            new_bal = (await cur.fetchone())["coins"]
            text = (
                f"⚔️ <b>{result_title}</b>\n\n"
                f"{line('🎲', ch_name, f'<b>{v1}</b>')}\n"
                f"{line('🎲', op_name, f'<b>{v2}</b>')}\n"
                f"{line('🏆', 'Победитель', esc(winner_name))}\n"
                f"{line('🪙', 'Банк', f'<b>+{fmt_num(bank)}</b> → {fmt_num(new_bal)}')}"
            )
        else:
            # ничья — возврат
            await db.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?", (stake, ch_id))
            await db.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?", (stake, op_id))
            await db.execute("UPDATE duels SET status = 'completed' WHERE id = ?", (duel_id,))
            text = (
                f"⚔️ <b>{result_title}</b>\n\n"
                f"{line('🎲', ch_name, f'<b>{v1}</b>')}\n"
                f"{line('🎲', op_name, f'<b>{v2}</b>')}\n"
                f"{line('🪙', 'Ставки', 'возвращены')}"
            )

    await callback.message.answer(text)


# ================= ПРОФИЛЬ =================
@router.message(F.text == "👤 Мой профиль")
@router.message(F.text == "👤 Профиль")
@router.message(Command("profile"))
@router.message(F.text.regexp(PROFILE_CMD_RE))
async def show_profile(message: Message):
    if not await check_not_banned(message):
        return
    try:
        target = message.from_user
        if (
            message.chat.type != "private"
            and message.reply_to_message
            and message.reply_to_message.from_user
            and not message.reply_to_message.from_user.is_bot
        ):
            target = message.reply_to_message.from_user

        await get_or_create_user(target.id, target.username, target.full_name)
        if target.id != message.from_user.id:
            await get_or_create_user(
                message.from_user.id, message.from_user.username, message.from_user.full_name
            )

        photo, caption, kb = await render_profile(
            message.bot, target.id, fallback_name=target.full_name
        )
        if photo is None and "не найден" in (caption or "").lower():
            await message.reply(caption)
            return
        try:
            if photo:
                await message.reply_photo(photo=photo, caption=caption, reply_markup=kb)
            else:
                await message.reply(caption, reply_markup=kb)
        except TelegramBadRequest:
            await message.reply(caption, reply_markup=kb)
    except Exception as e:
        logger.error(f"profile: {e}")
        await message.reply("❌ Ошибка профиля.")


@router.callback_query(BackToProfileCallback.filter())
async def process_back_to_profile(callback: CallbackQuery, callback_data: BackToProfileCallback):
    if not await check_not_banned_cb(callback):
        return
    if not _owner_check(callback, callback_data.user_id):
        await callback.answer("⚠️ Не ваша кнопка")
        return
    try:
        target_id = callback_data.user_id or callback.from_user.id
        photo, caption, kb = await render_profile(
            callback.message.bot, target_id, fallback_name=callback.from_user.full_name
        )
        await show_or_edit_photo(callback.message, photo, caption, kb)
        await callback.answer()
    except Exception as e:
        logger.error(f"back profile: {e}")
        await callback.answer("⚠️ Ошибка")


# ================= НИК =================
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
async def nickname_cmd(message: Message, command: CommandObject, state: FSMContext):
    if not await check_not_banned(message):
        return
    user_id = message.from_user.id
    await get_or_create_user(user_id, message.from_user.username, message.from_user.full_name)
    arg = (command.args or "").strip()

    if arg.lower() == "reset":
        b = InlineKeyboardBuilder()
        b.button(text="✓ Подтвердить", callback_data=NickConfirmCallback(action="reset").pack())
        b.button(text="✕ Отмена", callback_data=NickConfirmCallback(action="cancel").pack())
        b.adjust(2)
        await state.set_state(NicknameSG.pending)
        await state.update_data(pending_nick=None, pending_action="reset")
        await message.reply(
            f"♻️ <b>Сбросить ник?</b>\n\n"
            f"{line('ℹ️', 'Результат', 'будет имя из Telegram или ID')}\n"
            f"{line('🪙', 'Стоимость', 'бесплатно')}",
            reply_markup=b.as_markup(),
        )
        return

    if not arg:
        await message.reply(
            f"✏️ Использование: <code>/nickname НовыйНик</code>\n"
            f"{line('🪙', 'Смена', f'{NICKNAME_COST} 🪙')}\n"
            f"{line('♻️', 'Сброс', '/nickname reset')}"
        )
        return

    new_nick = validate_nickname(arg)
    if not new_nick:
        await message.reply("❌ Неверный ник: 2–32 символа, без ссылок и @.")
        return

    row = await get_user_row(user_id)
    balance = row["coins"] if row else 0
    if balance < NICKNAME_COST:
        await message.reply(
            f"⚠️ Недостаточно монет.\n"
            f"{line('🪙', 'Нужно', f'<b>{fmt_num(NICKNAME_COST)}</b>')}\n"
            f"{line('🪙', 'У вас', f'<b>{fmt_num(balance)}</b>')}"
        )
        return

    b = InlineKeyboardBuilder()
    b.button(text="✓ Подтвердить", callback_data=NickConfirmCallback(action="apply").pack())
    b.button(text="✕ Отмена", callback_data=NickConfirmCallback(action="cancel").pack())
    b.adjust(2)
    await state.set_state(NicknameSG.pending)
    await state.update_data(pending_nick=new_nick, pending_action="apply")
    await message.reply(
        f"✏️ <b>Сменить ник?</b>\n\n"
        f"{line('👤', 'Новый ник', f'<b>{esc(new_nick)}</b>')}\n"
        f"{line('🪙', 'Стоимость', f'<b>{NICKNAME_COST}</b>')}\n"
        f"{line('🪙', 'Баланс', fmt_num(balance))}",
        reply_markup=b.as_markup(),
    )


@router.callback_query(NickConfirmCallback.filter())
async def nickname_confirm(callback: CallbackQuery, callback_data: NickConfirmCallback, state: FSMContext):
    if not await check_not_banned_cb(callback):
        return
    user_id = callback.from_user.id

    if callback_data.action == "cancel":
        await state.clear()
        await callback.answer()
        await _try_delete(callback.message)
        return

    data = await state.get_data()
    pending_action = data.get("pending_action")
    pending_nick = data.get("pending_nick")

    if callback_data.action == "reset":
        if pending_action and pending_action != "reset":
            await callback.answer("⚠️ Сессия устарела")
            await state.clear()
            return
        async with get_db() as db:
            await db.execute("UPDATE users SET nickname = NULL WHERE user_id = ?", (user_id,))
        await state.clear()
        shown = display_name(None, user_id, callback.from_user.full_name)
        await callback.message.edit_text(
            f"✓ <b>Ник сброшен</b>\n\n{line('👤', 'Отображение', esc(shown))}",
            reply_markup=get_ok_kb(),
        )
        await callback.answer()
        return

    if callback_data.action == "apply":
        new_nick = pending_nick
        if not new_nick or not validate_nickname(new_nick):
            await state.clear()
            await callback.message.edit_text("❌ Неверный ник")
            await callback.answer()
            return
        async with get_db() as db:
            cur = await db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            balance = row["coins"] if row else 0
            if balance < NICKNAME_COST:
                await state.clear()
                await callback.message.edit_text(
                    f"⚠️ Недостаточно монет ({fmt_num(NICKNAME_COST)} 🪙)."
                )
                await callback.answer()
                return
            await db.execute(
                "UPDATE users SET nickname = ?, coins = coins - ? WHERE user_id = ?",
                (new_nick, NICKNAME_COST, user_id),
            )
        await state.clear()
        await callback.message.edit_text(
            f"✓ <b>Ник изменён</b>\n\n"
            f"{line('👤', 'Ник', esc(new_nick))}\n"
            f"{line('🪙', 'Списано', f'{NICKNAME_COST}')}",
            reply_markup=get_ok_kb(),
        )
        await callback.answer()
        return
    await callback.answer()


@router.callback_query(NicknameCallback.filter(F.action == "change"))
async def change_nickname_hint(callback: CallbackQuery, callback_data: NicknameCallback):
    await callback.answer(
        f"/nickname НовыйНик ({NICKNAME_COST} 🪙) или /nickname reset",
        show_alert=True,
    )


# ================= ТОП =================
async def build_top_text(kind: str, current_user_id: int) -> str:
    await burn_expired_streak(current_user_id)
    limit = 10
    now_ts = int(time.time())
    async with get_db() as db:
        if kind == "cards":
            cur = await db.execute(
                """SELECT u.user_id, u.nickname,
                          (SELECT COUNT(*) FROM inventory i WHERE i.user_id = u.user_id) AS value
                   FROM users u
                   WHERE u.role != 'banned'
                   ORDER BY value DESC, u.user_id ASC LIMIT ?""",
                (limit,),
            )
            top = await cur.fetchall()
            cur = await db.execute(
                "SELECT COUNT(*) AS value FROM inventory WHERE user_id = ?",
                (current_user_id,),
            )
            my_value = (await cur.fetchone())["value"] or 0
            cur = await db.execute(
                """SELECT COUNT(*) + 1 FROM (
                       SELECT u.user_id,
                              (SELECT COUNT(*) FROM inventory i WHERE i.user_id = u.user_id) AS value
                       FROM users u WHERE u.role != 'banned'
                   ) WHERE value > ?""",
                (my_value,),
            )
            my_rank = (await cur.fetchone())[0]
            unit, title = "🃏", "🃏 Топ по карточкам"
        elif kind == "streak":
            effective = (
                "CASE WHEN last_claim > 0 AND (? - last_claim) >= ? "
                "THEN 0 ELSE COALESCE(streak, 0) END"
            )
            cur = await db.execute(
                f"SELECT user_id, nickname, ({effective}) AS value FROM users "
                f"WHERE role != 'banned' ORDER BY value DESC, user_id ASC LIMIT ?",
                (now_ts, STREAK_EXPIRE_SECONDS, limit),
            )
            top = await cur.fetchall()
            cur = await db.execute(
                f"SELECT ({effective}) AS value FROM users WHERE user_id = ?",
                (now_ts, STREAK_EXPIRE_SECONDS, current_user_id),
            )
            my_value = (await cur.fetchone())["value"] or 0
            cur = await db.execute(
                f"SELECT COUNT(*) + 1 FROM users WHERE ({effective}) > ? AND role != 'banned'",
                (now_ts, STREAK_EXPIRE_SECONDS, my_value),
            )
            my_rank = (await cur.fetchone())[0]
            unit, title = "🔥", "🔥 Топ по стрику"
        else:
            cur = await db.execute(
                "SELECT user_id, nickname, coins AS value FROM users "
                "WHERE role != 'banned' ORDER BY value DESC, user_id ASC LIMIT ?",
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
            unit, title = "🪙", "🪙 Топ по монетам"

        cur = await db.execute("SELECT nickname FROM users WHERE user_id = ?", (current_user_id,))
        my_row2 = await cur.fetchone()
        my_nick = display_name(
            my_row2["nickname"] if my_row2 else None, current_user_id
        )

    medals = ["🥇", "🥈", "🥉"]
    text = f"<b>{title}</b>\n{'─' * 18}\n\n"
    for i, row in enumerate(top, 1):
        medal = medals[i - 1] if i <= 3 else f"{i}."
        nick = display_name(row["nickname"], row["user_id"])
        text += f"{medal} {esc(nick)} · <b>{fmt_num(row['value'])}</b> {unit}\n"

    text += (
        f"\n{line('📌', 'Ваше место', f'<b>#{fmt_num(my_rank)}</b>')}\n"
        f"{line('👤', esc(my_nick), f'<b>{fmt_num(my_value)}</b> {unit}')}"
    )
    return text


@router.message(F.text == "🏆 Топ игроков")
@router.message(F.text == "🏆 Топ")
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
        logger.error(f"top: {e}")
        await message.reply("❌ Ошибка топа")


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
            await callback.message.edit_text(text, reply_markup=get_top_keyboard(callback_data.kind))
            await callback.answer()
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                await callback.answer("Данные не изменились")
            else:
                await callback.message.answer(text, reply_markup=get_top_keyboard(callback_data.kind))
                await callback.answer()
    except Exception as e:
        logger.error(f"switch top: {e}")
        await callback.answer("⚠️ Ошибка")


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
            await callback.message.edit_text(text, reply_markup=get_top_keyboard(callback_data.kind))
            await callback.answer("Обновлено")
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                await callback.answer("Данные не изменились")
            else:
                await callback.message.answer(text, reply_markup=get_top_keyboard(callback_data.kind))
                await callback.answer()
    except Exception as e:
        logger.error(f"refresh top: {e}")
        await callback.answer("⚠️ Ошибка")


# ================= КОЛЛЕКЦИЯ =================
async def get_collection_main_keyboard(user_id: int):
    async with get_db() as db:
        cur = await db.execute(
            """SELECT c.rarity, COUNT(*)
               FROM inventory i JOIN cards c ON i.card_id = c.id
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
                    text=f"{r_info['icon']} {r_info['name']} · {fmt_num(user_amount)}/{fmt_num(total_of_rarity)}",
                    callback_data=RaritySelectCallback(rarity=r_key, page=0, user_id=user_id).pack(),
                )
            ])
        rows.append([
            InlineKeyboardButton(
                text="‹ В профиль",
                callback_data=BackToProfileCallback(user_id=user_id).pack(),
            )
        ])
        return InlineKeyboardMarkup(inline_keyboard=rows), total_cards, total_in_game


@router.message(Command("collection"))
@router.message(F.text.regexp(COLLECTION_CMD_RE))
async def show_collection(message: Message):
    if not await check_not_banned(message):
        return
    user_id = message.from_user.id
    try:
        await get_or_create_user(user_id, message.from_user.username, message.from_user.full_name)
        photo, caption, keyboard, total = await render_collection(
            message.bot, user_id, message.from_user.full_name
        )
        if total == 0:
            await message.answer(caption, reply_markup=keyboard)
            return
        await show_or_edit_photo(message, photo, caption, keyboard)
    except Exception as e:
        logger.error(f"collection: {e}")
        await message.reply("❌ Ошибка")


@router.callback_query(RaritySelectCallback.filter())
async def process_rarity_view(callback: CallbackQuery, callback_data: RaritySelectCallback):
    if not await check_not_banned_cb(callback):
        return
    if not _owner_check(callback, callback_data.user_id):
        await callback.answer("⚠️ Не ваша кнопка")
        return
    user_id = callback_data.user_id or callback.from_user.id
    rarity, page = callback_data.rarity, callback_data.page
    try:
        async with get_db() as db:
            cur = await db.execute(
                """SELECT c.name, c.photo_id, c.photo_path, i.claim_time, c.id
                   FROM inventory i JOIN cards c ON i.card_id = c.id
                   WHERE i.user_id = ? AND c.rarity = ?
                   ORDER BY i.claim_time DESC""",
                (user_id, rarity),
            )
            cards = await cur.fetchall()

        if not cards:
            await callback.answer("Нет карточек этого типа")
            return

        total_pages = len(cards)
        page = max(0, min(page, total_pages - 1))
        card = cards[page]
        info = RARITIES.get(rarity, {})
        caption = (
            f"{line('🃏', 'Название', f'<b>{esc(card['name'])}</b>')}\n"
            f"{line(info.get('icon', '•'), 'Редкость', info.get('name', rarity))}\n"
            f"{line('🪙', 'Награда', f'+{fmt_num(info.get('reward', 0))}')}"
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
                text="‹ К редкостям",
                callback_data=MainMenuCallback(user_id=user_id).pack(),
            )],
        ])
        photo = None
        if get_card_photo_input:
            photo = get_card_photo_input(card["id"], card["photo_path"] if "photo_path" in card.keys() else None)
        if not photo and card["photo_id"]:
            photo = card["photo_id"]
        await show_or_edit_photo(callback.message, photo, caption, keyboard)
        await callback.answer()
    except Exception as e:
        logger.error(f"rarity view: {e}")
        await callback.answer("⚠️ Ошибка")


@router.callback_query(MainMenuCallback.filter())
async def process_back_to_main(callback: CallbackQuery, callback_data: MainMenuCallback):
    if not await check_not_banned_cb(callback):
        return
    if not _owner_check(callback, callback_data.user_id):
        await callback.answer("⚠️ Не ваша кнопка")
        return
    target_id = callback_data.user_id or callback.from_user.id
    photo, caption, keyboard, _ = await render_collection(
        callback.message.bot, target_id, callback.from_user.full_name
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
    user_msg = bot_msg.reply_to_message if bot_msg else None
    await _try_delete(bot_msg)
    await _try_delete(user_msg)


# ================= МГНОВЕННАЯ КАРТОЧКА =================
@router.callback_query(CardActionCallback.filter())
async def handle_card_action(callback: CallbackQuery, callback_data: CardActionCallback):
    if not await check_not_banned_cb(callback):
        return
    user_id = callback.from_user.id
    action = callback_data.action
    target = callback_data.user_id or user_id

    if user_id != target:
        await callback.answer("⚠️ Не ваша кнопка")
        return
    if rate_limited(f"card-action:{user_id}", limit=8, window=10):
        await callback.answer("Слишком часто")
        return

    try:
        nickname = await get_user_display(user_id, callback.from_user.full_name)

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
                await callback.answer("⏳ Кулдаун прошёл — получайте бесплатно!")
                return

            remaining = int(COOLDOWN_SECONDS - time_passed)
            cost = instant_cost(remaining)
            if balance < cost:
                await callback.answer(f"⚠️ Нужно {cost} 🪙, у вас {balance}")
                return

            await callback.answer(f"⏳ Карточка за {cost} 🪙...")

            async with get_db() as db:
                await db.execute(
                    "UPDATE users SET coins = coins - ?, last_claim = ? WHERE user_id = ?",
                    (cost, now, user_id),
                )

            card, status = await issue_card(user_id, check_cooldown=False)
            streak, bonus, new_balance, streak_updated = await check_and_update_streak(user_id)

            if status in ("no_cards", "all_collected"):
                async with get_db() as db:
                    await db.execute(
                        "UPDATE users SET coins = coins + ? WHERE user_id = ?", (cost, user_id)
                    )
                msg = "В базе нет карточек." if status == "no_cards" else "Все карточки уже собраны."
                await callback.message.answer(f"❌ {msg} Монеты возвращены.")
                await send_streak_notice(callback.message, streak, bonus, new_balance, streak_updated)
                return

            if status != "success" or card is None:
                async with get_db() as db:
                    await db.execute(
                        "UPDATE users SET coins = coins + ? WHERE user_id = ?", (cost, user_id)
                    )
                await callback.message.answer("❌ Ошибка. Монеты возвращены.")
                return

            caption = _card_caption(nickname, card)
            kb = get_after_card_keyboard(user_id, card["balance"])
            photo = _resolve_card_photo(card)
            effect = None
            if card["rarity"] in ("mythical", "legendary") and callback.message.chat.type == "private":
                effect = EFFECT_PARTY

            try:
                kwargs = {"caption": caption, "reply_markup": kb}
                if effect:
                    kwargs["message_effect_id"] = effect
                if photo:
                    await callback.message.answer_photo(photo=photo, **kwargs)
                else:
                    await callback.message.answer(caption, reply_markup=kb)
            except TypeError:
                if photo:
                    await callback.message.answer_photo(photo=photo, caption=caption, reply_markup=kb)
                else:
                    await callback.message.answer(caption, reply_markup=kb)
            except TelegramBadRequest:
                await callback.message.answer(caption, reply_markup=kb)

            await send_streak_notice(callback.message, streak, bonus, new_balance, streak_updated)
            return

        if action == "collection":
            photo, caption, keyboard, total = await render_collection(
                callback.message.bot, user_id, callback.from_user.full_name
            )
            if total == 0:
                await callback.message.answer(caption, reply_markup=keyboard)
            else:
                try:
                    if photo:
                        await callback.message.answer_photo(photo=photo, caption=caption, reply_markup=keyboard)
                    else:
                        await callback.message.answer(caption, reply_markup=keyboard)
                except TelegramBadRequest:
                    await callback.message.answer(caption, reply_markup=keyboard)
            await callback.answer()
    except Exception as e:
        logger.error(f"card action: {e}")
        await callback.answer("⚠️ Ошибка")


# ================= ТЕСТОВЫЕ (фото) — доступны всем, но безопасны =================
@router.message(Command("sync_my_avatar"))
async def cmd_sync_default_avatar(message: Message):
    """Тест: скачать дефолтный аватар на диск (из DEFAULT_AVATAR_FILE_ID)."""
    if not ensure_default_avatar:
        await message.reply("❌ Модуль card_photos недоступен.")
        return
    ok = await ensure_default_avatar(message.bot)
    path = str(DEFAULT_AVATAR_PATH) if DEFAULT_AVATAR_PATH else "?"
    if ok:
        await message.reply(f"✅ Дефолтный аватар на диске:\n<code>{esc(path)}</code>")
    else:
        await message.reply("❌ Не удалось скачать. Проверьте file_id / права.")


# ================= ЗАПУСК =================
async def _run_api_server():
    try:
        import uvicorn
        from api import app as fastapi_app
    except ImportError as e:
        logger.warning("API мини-аппки не запущен: %s", e)
        return
    port = int(os.getenv("PORT", os.getenv("API_PORT", "8080")))
    logger.info("🌐 API на 0.0.0.0:%s", port)
    config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=port, log_level="info", lifespan="on")
    server = uvicorn.Server(config)
    await server.serve()


async def main():
    try:
        os.makedirs(os.path.dirname(DB_NAME) or ".", exist_ok=True)
        if LOG_PATH:
            os.makedirs(os.path.dirname(LOG_PATH) or ".", exist_ok=True)
        await init_db()
        logger.info("БД инициализирована")
        try:
            ensure_photo_dir()
        except Exception as e:
            logger.warning("photo dir: %s", e)

        bot = Bot(
            token=BOT_TOKEN,
            default=DefaultBotProperties(
                parse_mode=ParseMode.HTML,
                link_preview=LinkPreviewOptions(is_disabled=True),
            ),
        )
        # попытаться скачать дефолтный аватар при старте
        if ensure_default_avatar:
            try:
                await ensure_default_avatar(bot)
            except Exception as e:
                logger.warning("default avatar: %s", e)

        dp = Dispatcher(storage=MemoryStorage())
        dp.include_router(router)
        await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
        logger.info("🤖 Пользовательский бот запущен")

        start_api = os.getenv("START_API", os.getenv("ENABLE_WEBAPP_API", "0")).strip().lower() in (
            "1", "true", "yes", "on",
        )
        if start_api:
            await asyncio.gather(dp.start_polling(bot), _run_api_server())
        else:
            await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
