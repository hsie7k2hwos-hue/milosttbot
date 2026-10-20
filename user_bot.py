from __future__ import annotations

import asyncio
import html
import logging
import os
import random
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeDefault,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    LinkPreviewOptions,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

# ── optional photo helpers ──────────────────────────────────────────────────
try:
    from card_photos import (
        DEFAULT_AVATAR_FILE_ID,
        DEFAULT_AVATAR_PATH,
        ensure_default_avatar,
        ensure_photo_dir,
        get_card_photo_input,
        get_default_avatar_input,
        migrate_all_card_photos,
        sync_card_photo,
    )
except ImportError:
    sync_card_photo = None
    migrate_all_card_photos = None
    get_card_photo_input = None
    get_default_avatar_input = None
    ensure_default_avatar = None
    DEFAULT_AVATAR_PATH = None
    DEFAULT_AVATAR_FILE_ID = (
        "AgACAgIAAxkBAAID12qql3EFpnb2HwTCE7Yn_Ri1TQsNAAKRIGsbfHlYSVUpxfU75O60AQADAgADeAADPQQ"
    )

    def ensure_photo_dir() -> None:
        pass


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN не задан. Укажите его в .env или окружении.")

WEBAPP_URL = (os.getenv("WEBAPP_URL") or "").strip()
BOT_USERNAME = (os.getenv("BOT_USERNAME", "milosttbot") or "milosttbot").lstrip("@").strip()
BOT_URL = f"https://t.me/{BOT_USERNAME}"
OPEN_APP_URL = f"{BOT_URL}?startapp"

DB_NAME = os.getenv("DB_NAME", "/app/data/cards_game.db")
LOG_PATH = os.getenv("LOG_PATH", "/app/data/bot.log")

# ID суперадминов через запятую (если в БД ещё нет ни одного superadmin)
SUPERADMIN_IDS = {
    int(x.strip())
    for x in (os.getenv("SUPERADMIN_IDS") or os.getenv("SUPERADMIN_ID") or "").split(",")
    if x.strip().isdigit()
}

from game_config import (
    DEFAULT_RARITIES,
    COOLDOWN_SECONDS,
    NICKNAME_COST,
    DICE_COOLDOWN_SECONDS,
    DICE_MIN_BALANCE,
    DICE_SPIN_DELAY,
    DICE_VALUE_MAP,
    STREAK_BONUSES,
    streak_bonus_for,
    ROLES as _GC_ROLES,
    SUPERADMIN_IDS as _GC_SUPERADMIN_IDS,
    load_rarities,
    ensure_config_tables,
    is_user_bot_enabled,
)

GROUP_AUTODELETE_SECONDS = 30
STREAK_EXPIRE_SECONDS = 24 * 3600
EFFECT_PARTY = "5046509860389126442"

NEW_CARD_EMOJIS = ["✨", "🪄", "👏", "🎉", "🎊", "🥳"]
NEGATIVE_CARD_EMOJIS = ["☠️", "⚰️", "💀", "😵", "🪦", "🔫"]
WAIT_EMOJIS = ["⏳", "⌛️", "⏰", "🕓", "🕙", "🕒"]

RARITIES = {k: dict(v) for k, v in DEFAULT_RARITIES.items()}
ROLES = _GC_ROLES
if _GC_SUPERADMIN_IDS:
    SUPERADMIN_IDS.update(_GC_SUPERADMIN_IDS)


async def refresh_rarities() -> dict:
    global RARITIES
    async with get_db() as db:
        await ensure_config_tables(db)
        RARITIES = await load_rarities(db)
    return RARITIES

NICKNAME_RE = re.compile(r"^[\w\-. ]{2,32}$", re.UNICODE)
URL_RE = re.compile(r"(https?://|t\.me/|@\w+)", re.IGNORECASE)

DICE_CMD_RE = re.compile(r"^мряу\s+кубик\s*$", re.IGNORECASE | re.UNICODE)
PROFILE_CMD_RE = re.compile(r"^мряу\s+профиль\s*$", re.IGNORECASE | re.UNICODE)
TOP_CMD_RE = re.compile(r"^мряу\s+топ\s*$", re.IGNORECASE | re.UNICODE)
HELP_CMD_RE = re.compile(r"^мряу\s+помощь\s*$", re.IGNORECASE | re.UNICODE)
NICK_CMD_RE = re.compile(r"^мряу\s+ник\s+(.+)$", re.IGNORECASE | re.UNICODE)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════
def get_app_url() -> str:
    if not WEBAPP_URL or not BOT_USERNAME:
        return ""
    return OPEN_APP_URL


def esc(text: Any) -> str:
    return html.escape(str(text), quote=False)


def fmt_num(n: Any) -> str:
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


def fmt_coins(n: int) -> str:
    return f"{fmt_num(n)} {plural(n, 'монета', 'монеты', 'монет')}"


def line(emoji: str, label: str, value: Any) -> str:
    return f"{emoji} <b>{label}</b> · {value}"


def bq(text: str) -> str:
    return f"<blockquote>{text}</blockquote>"


def dice_delta(value: int) -> int:
    return DICE_VALUE_MAP.get(value, 0)


def role_display(role: str) -> str:
    info = ROLES.get(role, ROLES["user"])
    return f"{info['icon']} {info['name']}"


def display_name(
    nickname: Optional[str],
    user_id: int,
    fallback: Optional[str] = None,
) -> str:
    if nickname and str(nickname).strip():
        return str(nickname).strip()
    if fallback and str(fallback).strip():
        return str(fallback).strip()[:32]
    return str(user_id)


def validate_nickname(raw: str) -> Optional[str]:
    raw = raw.strip()
    if not (2 <= len(raw) <= 32):
        return None
    if URL_RE.search(raw):
        return None
    if not NICKNAME_RE.match(raw):
        return None
    return raw


def _resolve_card_photo(card: dict) -> Any:
    if get_card_photo_input:
        local = get_card_photo_input(card["id"], card.get("photo_path"))
        if local:
            return local
    return card.get("photo_id") or None


def fmt_remaining(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h} ч {m} мин"
    if m > 0:
        return f"{m} мин" if s == 0 else f"{m} мин {s} сек"
    return f"{s} сек"


# ═══════════════════════════════════════════════════════════════════════════
# CALLBACK DATA
# ═══════════════════════════════════════════════════════════════════════════
class RaritySelectCallback(CallbackData, prefix="coll_rarity"):
    rarity: str
    page: int = 0
    user_id: int = 0


class MainMenuCallback(CallbackData, prefix="coll_main"):
    user_id: int = 0


class BackToProfileCallback(CallbackData, prefix="back_to_profile"):
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
    tab: str = "cards"


class TopRefreshCallback(CallbackData, prefix="top_refresh"):
    kind: str


# ═══════════════════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════════════════
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
        logger.error("Ошибка БД: %s", e)
        raise
    finally:
        await db.close()


async def init_db() -> None:
    async with get_db() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cards (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                rarity     TEXT NOT NULL,
                photo_id   TEXT,
                photo_path TEXT,
                photo_uuid TEXT
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
            ("users", "last_dice", "INTEGER DEFAULT 0"),
            ("inventory", "claim_time", "INTEGER DEFAULT 0"),
            ("cards", "photo_path", "TEXT"),
            ("cards", "photo_id", "TEXT"),
            ("cards", "photo_uuid", "TEXT"),
        ]:
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            except aiosqlite.OperationalError:
                pass

        # Удаляем устаревшую таблицу дуэлей, если осталась
        try:
            await db.execute("DROP TABLE IF EXISTS duels")
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# USER HELPERS
# ═══════════════════════════════════════════════════════════════════════════
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
        logger.error("get_user_role: %s", e)
        return "user"


async def is_banned(user_id: int) -> bool:
    return (await get_user_role(user_id)) == "banned"


async def ensure_superadmin_from_env(db: aiosqlite.Connection) -> None:
    """Если в БД нет superadmin и задан SUPERADMIN_IDS — назначить."""
    if not SUPERADMIN_IDS:
        return
    cur = await db.execute(
        "SELECT COUNT(*) FROM users WHERE role = 'superadmin'"
    )
    if (await cur.fetchone())[0] > 0:
        return
    for sid in SUPERADMIN_IDS:
        cur = await db.execute("SELECT user_id FROM users WHERE user_id = ?", (sid,))
        if await cur.fetchone():
            await db.execute(
                "UPDATE users SET role = 'superadmin' WHERE user_id = ?", (sid,)
            )
            logger.info("SUPERADMIN_IDS: %s → superadmin", sid)
        else:
            # создаём запись, чтобы роль сохранилась до первого /start
            now = int(time.time())
            await db.execute(
                "INSERT OR IGNORE INTO users (user_id, registration, role) VALUES (?, ?, 'superadmin')",
                (sid, now),
            )
            logger.info("SUPERADMIN_IDS: создан %s как superadmin", sid)


async def get_or_create_user(
    user_id: int,
    username: Optional[str] = None,
    full_name: Optional[str] = None,
):
    async with get_db() as db:
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = await cur.fetchone()
        if user:
            return user

        now = int(time.time())
        role = "superadmin" if user_id in SUPERADMIN_IDS else "user"

        await db.execute(
            "INSERT INTO users (user_id, nickname, registration, streak, last_streak_date, role) "
            "VALUES (?, NULL, ?, 0, 0, ?)",
            (user_id, now, role),
        )
        if role == "superadmin":
            logger.info("Пользователь %s назначен superadmin (из SUPERADMIN_IDS)", user_id)

        await ensure_superadmin_from_env(db)

        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return await cur.fetchone()


async def get_user_display(user_id: int, fallback: Optional[str] = None) -> str:
    try:
        async with get_db() as db:
            cur = await db.execute("SELECT nickname FROM users WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            nick = row["nickname"] if row else None
            return display_name(nick, user_id, fallback)
    except Exception:
        return display_name(None, user_id, fallback)


async def get_user_row(user_id: int):
    async with get_db() as db:
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return await cur.fetchone()


def _owner_check(callback: CallbackQuery, target_user_id: int) -> bool:
    if target_user_id and callback.from_user.id != target_user_id:
        return False
    return True


# ═══════════════════════════════════════════════════════════════════════════
# FSM
# ═══════════════════════════════════════════════════════════════════════════
class NicknameSG(StatesGroup):
    pending = State()


router = Router()


# ═══════════════════════════════════════════════════════════════════════════
# KEYBOARDS
# ═══════════════════════════════════════════════════════════════════════════
def get_app_kb(is_short: bool = False) -> Optional[InlineKeyboardMarkup]:
    app_url = get_app_url()
    if not app_url:
        return None
    text = "📱 Открыть" if is_short else "📱 Мини-приложение"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, url=app_url)]]
    )


def get_profile_kb(owner_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(
        text="🃏 Мои карточки",
        callback_data=MainMenuCallback(user_id=owner_id).pack(),
    )
    b.adjust(1)
    return b.as_markup()


def get_main_km() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🃏 Получить карточку"), KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="🏆 Топ"), KeyboardButton(text="📱 Мини-приложение")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def get_ok_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Понятно", callback_data=OkDeleteCallback().pack())
    return b.as_markup()


def get_after_card_keyboard(user_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    app_url = get_app_url()
    builder.button(
        text="🃏 Мои карточки",
        callback_data=CardActionCallback(action="collection", user_id=user_id).pack(),
    )
    if app_url:
        builder.button(text="📱 Мини-приложение", url=app_url)
        builder.adjust(2)
    else:
        builder.adjust(1)
    return builder.as_markup()


def get_top_keyboard(kind: str = "coins") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(
        text=("● " if kind == "coins" else "○ ") + "🪙",
        callback_data=TopCallback(kind="coins").pack(),
    )
    b.button(
        text=("● " if kind == "cards" else "○ ") + "🃏",
        callback_data=TopCallback(kind="cards").pack(),
    )
    b.button(
        text=("● " if kind == "streak" else "○ ") + "🔥",
        callback_data=TopCallback(kind="streak").pack(),
    )
    b.button(text="↻ Обновить", callback_data=TopRefreshCallback(kind=kind).pack())
    b.adjust(3, 1)
    return b.as_markup()


# ═══════════════════════════════════════════════════════════════════════════
# RENDER
# ═══════════════════════════════════════════════════════════════════════════
async def get_user_photo(bot: Bot, user_id: int):
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if photos.total_count > 0:
            return photos.photos[0][-1].file_id
    except Exception as e:
        logger.debug("profile photo: %s", e)

    if get_default_avatar_input:
        local = get_default_avatar_input()
        if local:
            return local
    return None


async def render_profile(
    bot: Bot,
    user_id: int,
    fallback_name: Optional[str] = None,
    is_own: bool = True,
):
    await burn_expired_streak(user_id)

    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT u.nickname, u.coins, u.streak,
                   (SELECT COUNT(*) FROM inventory i WHERE i.user_id = u.user_id) AS cards_count
            FROM users u
            WHERE u.user_id = ?
            """,
            (user_id,),
        )
        row = await cur.fetchone()
        cur = await db.execute("SELECT COUNT(*) FROM cards")
        total_cards = (await cur.fetchone())[0]

    if not row:
        return None, "❌ Игрок не найден.", None

    nickname = display_name(row["nickname"], user_id, fallback_name)
    cards_count = row["cards_count"] or 0
    coins = row["coins"] or 0
    streak = row["streak"] or 0

    caption = (
        f"👤 <b>{esc(nickname)}</b>\n\n"
        f"🃏 Карточки · <b>{fmt_num(cards_count)}</b> / {fmt_num(total_cards)}\n"
        f"🪙 Монеты · <b>{fmt_num(coins)}</b>\n"
        f"🔥 Стрик · <b>{fmt_days(streak)}</b>"
    )
    return await get_user_photo(bot, user_id), caption, get_profile_kb(user_id)


async def render_collection(
    bot: Bot,
    user_id: int,
    fallback_name: Optional[str] = None,
):
    keyboard, total, total_in_game = await get_collection_main_keyboard(user_id)
    photo = await get_user_photo(bot, user_id)
    caption = (
        f"🃏 <b>Мои карточки</b> (<b>{fmt_num(total)}</b> / {fmt_num(total_in_game)})"
    )
    return photo, caption, keyboard, total


async def show_or_edit_photo(
    message: Message,
    photo,
    caption: str,
    keyboard=None,
) -> None:
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
            logger.error("edit_media: %s", e)

    try:
        await message.answer_photo(photo=photo, caption=caption, reply_markup=keyboard)
    except TelegramBadRequest as e:
        logger.error("answer_photo: %s", e)
        try:
            await message.answer(caption, reply_markup=keyboard)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# CARD ISSUE (без дубликатов)
# ═══════════════════════════════════════════════════════════════════════════
async def issue_card(
    user_id: int,
    check_cooldown: bool = True,
) -> tuple[Optional[dict], str]:
    """Выдаёт только новую (не имеющуюся) карточку."""
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
                    """
                    SELECT id, name, photo_id, photo_path, rarity
                    FROM cards
                    WHERE rarity = ?
                      AND id NOT IN (SELECT card_id FROM inventory WHERE user_id = ?)
                    ORDER BY RANDOM() LIMIT 1
                    """,
                    (selected_rarity, user_id),
                )
                card = await cur.fetchone()
                if card:
                    break

            if not card:
                cur = await db.execute(
                    """
                    SELECT id, name, photo_id, photo_path, rarity
                    FROM cards
                    WHERE id NOT IN (SELECT card_id FROM inventory WHERE user_id = ?)
                    ORDER BY RANDOM() LIMIT 1
                    """,
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
            base_coins = RARITIES.get(selected_rarity, RARITIES["common"])["reward"]
            coins_earned = int(base_coins)
            now = int(time.time())

            # negative: coins = max(0, coins + reward)
            cur = await db.execute(
                "SELECT coins FROM users WHERE user_id = ?", (user_id,)
            )
            row_bal = await cur.fetchone()
            old_bal = (row_bal["coins"] or 0) if row_bal else 0
            new_bal = max(0, old_bal + coins_earned)
            actual_delta = new_bal - old_bal

            if check_cooldown:
                await db.execute(
                    """
                    INSERT INTO users (user_id, last_claim, coins)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        last_claim = ?,
                        coins = ?
                    """,
                    (user_id, now, max(0, coins_earned), now, new_bal),
                )
            else:
                await db.execute(
                    "UPDATE users SET coins = ? WHERE user_id = ?",
                    (new_bal, user_id),
                )

            await db.execute(
                """
                INSERT INTO inventory (user_id, card_id, claim_time)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, card_id) DO UPDATE SET claim_time = ?
                """,
                (user_id, card_id, now, now),
            )

            balance = new_bal
            coins_earned = actual_delta if selected_rarity == "negative" else coins_earned

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
        logger.error("issue_card: %s", e)
        return None, "error"


def _card_caption(card: dict) -> str:
    r = RARITIES.get(card["rarity"], RARITIES["common"])
    name_str = f"<b>{esc(card['name'])}</b>"
    earned = int(card["coins_earned"])
    if earned < 0:
        title_emoji = random.choice(NEGATIVE_CARD_EMOJIS)
        title = f"{title_emoji} <b>Негативная карточка!</b>"
        reward_str = f"−<b>{fmt_num(abs(earned))}</b> 🪙 · всего <b>{fmt_num(card['balance'])}</b> 🪙"
    else:
        title_emoji = random.choice(NEW_CARD_EMOJIS)
        title = f"{title_emoji} <b>Новая карточка!</b>"
        reward_str = f"+<b>{fmt_num(earned)}</b> 🪙 · всего <b>{fmt_num(card['balance'])}</b> 🪙"
    rarity_line = f"💎{r.get('icon', '•')} {r.get('name', card['rarity'])}"
    return (
        f"{title}\n\n"
        f"🃏 {name_str}\n"
        f"{rarity_line}\n\n"
        f"{reward_str}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# STREAK
# ═══════════════════════════════════════════════════════════════════════════
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
                    "UPDATE users SET streak = 0, last_streak_date = 0, streak_bonus = 0 "
                    "WHERE user_id = ?",
                    (user_id,),
                )
                return True
        return False
    except Exception as e:
        logger.error("burn_streak: %s", e)
        return False


async def check_and_update_streak(user_id: int) -> tuple[int, int, int, bool]:
    """(streak, coin_bonus, balance, streak_updated)"""
    try:
        now_ts = int(time.time())
        now = datetime.now()
        today_start = int(datetime(now.year, now.month, now.day).timestamp())
        today_end = today_start + 86400 - 1
        yesterday_start = int(
            (now - timedelta(days=1)).replace(hour=0, minute=0, second=0).timestamp()
        )
        yesterday_end = yesterday_start + 86400 - 1

        async with get_db() as db:
            cur = await db.execute(
                "SELECT streak, last_streak_date, streak_bonus, coins, last_claim "
                "FROM users WHERE user_id = ?",
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
                        "UPDATE users SET streak = 0, last_streak_date = 0, streak_bonus = 0 "
                        "WHERE user_id = ?",
                        (user_id,),
                    )
                return 0, 0, balance, False

            if today_start <= last_date <= today_end:
                return streak, 0, balance, False

            is_consecutive = streak > 0 and yesterday_start <= last_date <= yesterday_end
            new_streak = (streak + 1) if is_consecutive else 1
            new_bonus = streak_bonus_for(new_streak)

            await db.execute(
                "UPDATE users SET streak = ?, last_streak_date = ?, streak_bonus = ?, "
                "coins = coins + ? WHERE user_id = ?",
                (new_streak, now_ts, new_bonus, new_bonus, user_id),
            )
            cur = await db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
            new_balance = (await cur.fetchone())[0]
            return new_streak, new_bonus, new_balance, True
    except Exception as e:
        logger.error("update_streak: %s", e)
        return 0, 0, 0, False


def _streak_message_text(streak: int, bonus: int, new_balance: int) -> str:
    if streak <= 0:
        return ""
    if streak == 1:
        return (
            f"🔥 <b>Стрик · {fmt_days(streak)}</b>\n"
            f"💡 Зайдите завтра, чтобы получить первый бонус"
        )
    text = f"🔥 <b>Стрик · {fmt_days(streak)}</b>\n"
    text += "💡 Заходите каждый день, чтобы получать бонусы"
    if bonus > 0:
        text += f"\n\n+{fmt_num(bonus)} 🪙 · всего {fmt_num(new_balance)} 🪙"
    return text


async def send_streak_notice(
    message: Message,
    streak: int,
    bonus: int,
    new_balance: int,
    streak_updated: bool,
) -> None:
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
        logger.debug("streak notice: %s", e)


# ═══════════════════════════════════════════════════════════════════════════
# RATE LIMIT / AUTO-DELETE / BAN
# ═══════════════════════════════════════════════════════════════════════════
_rate_bucket: dict[str, list[float]] = {}
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


async def _try_delete(msg: Optional[Message]) -> None:
    if msg is None:
        return
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass
    except Exception as e:
        logger.debug("try_delete: %s", e)


async def _auto_delete_pair(
    bot_msg: Message,
    user_msg: Optional[Message],
    delay: int,
) -> None:
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


async def check_not_banned(message: Message) -> bool:
    if await is_banned(message.from_user.id):
        await reply_ephemeral(
            message,
            "🚫 <b>Доступ ограничен</b>\n\n"
            "Вы заблокированы. Обратитесь к администрации.",
        )
        return False
    return True


async def check_not_banned_cb(callback: CallbackQuery) -> bool:
    if await is_banned(callback.from_user.id):
        await callback.answer("🚫 Вы заблокированы", show_alert=True)
        return False
    return True


# ═══════════════════════════════════════════════════════════════════════════
# COMMANDS LIST
# ═══════════════════════════════════════════════════════════════════════════
USER_COMMANDS = [
    BotCommand(command="start", description="👋 Начать"),
    BotCommand(command="meow", description="🃏 Получить карточку"),
    BotCommand(command="profile", description="👤 Профиль"),
    BotCommand(command="top", description="🏆 Топ игроков"),
    BotCommand(command="dice", description="🎲 Кубик"),
    BotCommand(command="nick", description="✏️ Сменить ник"),
    BotCommand(command="help", description="❓ Справка"),
    BotCommand(command="miniapp", description="📱 Мини-приложение"),
]


# ═══════════════════════════════════════════════════════════════════════════
# START
# ═══════════════════════════════════════════════════════════════════════════
@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message) -> None:
    try:
        await get_or_create_user(
            message.from_user.id,
            message.from_user.username,
            message.from_user.full_name,
        )
        if not await check_not_banned(message):
            return

        try:
            await message.answer_sticker(
                sticker="CAACAgIAAxkBAAEHZldqwbCMC03fYpuFpEWDPhN5oJrurgACS54AAswowUsJDiXc6vblaz0E"
            )
        except Exception:
            pass

        welcome = (
            "👋 <b>Добро пожаловать!</b>\n\n"
            "Собирайте коллекцию карточек, копите монеты и соревнуйтесь в топе.\n\n"
            "🃏 Карточка · «мряу» или кнопка ниже\n"
            "⏱ Кулдаун · раз в 3 часа"
        )
        await message.reply(welcome, reply_markup=get_main_km())
    except Exception as e:
        logger.error("cmd_start: %s", e)


# ═══════════════════════════════════════════════════════════════════════════
# HELP
# ═══════════════════════════════════════════════════════════════════════════
HELP_TABS = {
    "cards": {
        "label": "🃏 Карточки",
        "title": "🃏 Карточки",
        "body": (
            "Карточки — основа игры. Каждая имеет название, редкость и награду в монетах.\n\n"
            "<b>Как получить</b>\n"
            "Напишите «мряу», «милость» или /meow — либо нажмите кнопку "
            "«Получить карточку». Карточка выдаётся бесплатно раз в 3 часа.\n\n"
            "<b>Редкости и награды</b>\n"
            + "\n".join(
                f"{v['icon']} <b>{v['name']}</b> — {v['reward']} 🪙"
                for v in RARITIES.values()
            )
            + "\n\n"
            "Чем реже карточка, тем больше монет. Дубликатов нет: "
            "каждая карточка выдаётся только один раз.\n\n"
            "<b>Мои карточки</b>\n"
            "Откройте профиль и нажмите «Мои карточки»."
        ),
    },
    "coins": {
        "label": "🪙 Монеты",
        "title": "🪙 Монеты",
        "body": (
            "Монеты — внутриигровая валюта.\n\n"
            "<b>Откуда</b>\n"
            "• Награда за карточку\n"
            "• Ежедневный бонус за стрик\n"
            "• Выигрыш в кубике\n\n"
            "<b>Куда</b>\n"
            f"• Смена никнейма — {NICKNAME_COST} 🪙\n\n"
            "Баланс виден в профиле."
        ),
    },
    "streak": {
        "label": "🔥 Стрик",
        "title": "🔥 Стрик",
        "body": (
            "Стрик — серия дней подряд с хотя бы одной карточкой.\n\n"
            "Если пропустить больше 24 часов — серия сгорает.\n\n"
            "<b>Бонусы (раз в день при обновлении стрика)</b>\n"
            "• 2+ дня — +10 🪙\n"
            "• 7+ дней — +15 🪙\n"
            "• 14+ дней — +20 🪙\n"
            "• 30+ дней — +30 🪙\n"
            "• 60+ дней — +40 🪙"
        ),
    },
    "games": {
        "label": "🎲 Кубик",
        "title": "🎲 Кубик",
        "body": (
            "Команда /dice или «мряу кубик». Нужно минимум "
            f"{DICE_MIN_BALANCE} 🪙. Кулдаун — 10 минут.\n\n"
            "1 → −15 · 2 → −8 · 3 → ±0 · 4 → +8 · 5 → +15 · 6 → +25"
        ),
    },
    "profile": {
        "label": "👤 Профиль",
        "title": "👤 Профиль",
        "body": (
            "Профиль: карточки, монеты, стрик.\n\n"
            f"Смена ника: /nick НовыйНик ({NICKNAME_COST} 🪙)\n"
            "Сброс: /nick reset\n\n"
            "Топ: /top или «мряу топ»."
        ),
    },
}

HELP_TAB_ORDER = ["cards", "coins", "streak", "games", "profile"]


def get_help_keyboard(active: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key in HELP_TAB_ORDER:
        label = HELP_TABS[key]["label"]
        mark = "✓ " if key == active else ""
        b.button(
            text=f"{mark}{label}",
            callback_data=HelpCallback(tab=key).pack(),
        )
    b.adjust(2, 2, 1)
    return b.as_markup()


def build_help_text(tab: str) -> str:
    if tab not in HELP_TABS:
        tab = "cards"
    p = HELP_TABS[tab]
    return f"<b>{p['title']}</b>\n{'─' * 20}\n\n{p['body']}"


@router.message(Command("help"))
@router.message(F.text.regexp(HELP_CMD_RE))
async def cmd_help(message: Message) -> None:
    if not await check_not_banned(message):
        return
    await message.reply(build_help_text("cards"), reply_markup=get_help_keyboard("cards"))


@router.callback_query(HelpCallback.filter())
async def help_tab_callback(callback: CallbackQuery, callback_data: HelpCallback) -> None:
    if not await check_not_banned_cb(callback):
        return
    try:
        tab = callback_data.tab if callback_data.tab in HELP_TABS else "cards"
        text = build_help_text(tab)
        kb = get_help_keyboard(tab)
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                await callback.answer()
                return
            await callback.message.answer(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        logger.error("help: %s", e)
        await callback.answer("⚠️ Ошибка")


# ═══════════════════════════════════════════════════════════════════════════
# MINI-APP
# ═══════════════════════════════════════════════════════════════════════════
@router.message(F.text == "📱 Мини-приложение")
@router.message(Command("miniapp"))
async def cmd_miniapp(message: Message) -> None:
    if not await check_not_banned(message):
        return
    app_kb = get_app_kb(is_short=True)
    if not app_kb:
        await message.reply(
            "📱 <b>Мини-приложение</b>\n\n"
            "Пока недоступно. Загляните позже."
        )
        return
    await message.reply(
        "📱 <b>Мини-приложение</b>\n\n"
        "Откройте прямо в Telegram — удобнее и быстрее.",
        reply_markup=app_kb,
    )


# ═══════════════════════════════════════════════════════════════════════════
# GET CARD
# ═══════════════════════════════════════════════════════════════════════════
@router.message(F.text == "🃏 Получить карточку")
@router.message(F.text.lower().strip() == "мряу")
@router.message(F.text.lower().strip() == "милость")
@router.message(Command("meow"))
async def get_card_handler(message: Message) -> None:
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

        async with get_db() as db:
            cur = await db.execute(
                "SELECT last_claim FROM users WHERE user_id = ?", (user_id,)
            )
            row = await cur.fetchone()
            last_claim = row["last_claim"] if row else 0

        time_passed = now - last_claim
        if time_passed < COOLDOWN_SECONDS:
            streak, bonus, new_balance, streak_updated = await check_and_update_streak(user_id)
            remaining = int(COOLDOWN_SECONDS - time_passed)
            time_str = fmt_remaining(remaining)
            wait_emoji = random.choice(WAIT_EMOJIS)
            text = f"{wait_emoji} <b>Подождите {time_str}</b> для получения следующей карточки"
            await reply_ephemeral(message, text)
            await send_streak_notice(message, streak, bonus, new_balance, streak_updated)
            return

        card, status = await issue_card(user_id, check_cooldown=True)
        streak, bonus, new_balance, streak_updated = await check_and_update_streak(user_id)

        if status == "no_cards":
            await reply_ephemeral(
                message,
                "❌ <b>Карточек пока нет</b>\n\n"
                "Обратитесь к администрации — база ещё пуста.",
            )
            await send_streak_notice(message, streak, bonus, new_balance, streak_updated)
            return

        if status == "all_collected":
            await reply_ephemeral(
                message,
                "🎉 <b>Коллекция полная</b>\n\n"
                "Вы собрали все карточки. Новые появятся позже.",
            )
            await send_streak_notice(message, streak, bonus, new_balance, streak_updated)
            return

        if status != "success" or card is None:
            await reply_ephemeral(
                message,
                "❌ <b>Что-то пошло не так</b>\n\n"
                "Попробуйте ещё раз чуть позже.",
            )
            return

        caption = _card_caption(card)
        kb = get_after_card_keyboard(user_id)
        photo = _resolve_card_photo(card)

        effect = None
        if card["rarity"] in ("super_valuable", "legendary") and message.chat.type == "private":
            effect = EFFECT_PARTY

        try:
            kwargs: dict[str, Any] = {"caption": caption, "reply_markup": kb}
            if effect:
                kwargs["message_effect_id"] = effect
            if photo:
                await message.reply_photo(photo=photo, **kwargs)
            else:
                await message.reply(caption, reply_markup=kb)
        except TypeError:
            if photo:
                await message.reply_photo(photo=photo, caption=caption, reply_markup=kb)
            else:
                await message.reply(caption, reply_markup=kb)
        except TelegramBadRequest as e:
            logger.error("reply card: %s", e)
            try:
                if photo:
                    await message.reply_photo(photo=photo, caption=caption, reply_markup=kb)
                else:
                    await message.reply(caption, reply_markup=kb)
            except Exception:
                await message.reply(caption)

        await send_streak_notice(message, streak, bonus, new_balance, streak_updated)
    except Exception as e:
        logger.error("get_card_handler: %s", e)
        await reply_ephemeral(
            message,
            "❌ <b>Что-то пошло не так</b>\n\n"
            "Попробуйте ещё раз чуть позже.",
        )


# ═══════════════════════════════════════════════════════════════════════════
# DICE
# ═══════════════════════════════════════════════════════════════════════════
@router.message(Command("dice"))
@router.message(F.text.regexp(DICE_CMD_RE))
async def dice_handler(message: Message) -> None:
    if not await check_not_banned(message):
        return

    user_id = message.from_user.id

    if rate_limited(f"dice:{user_id}", limit=6, window=15):
        await reply_ephemeral(
            message,
            f"{random.choice(WAIT_EMOJIS)} <b>Слишком часто</b>\n\n"
            "Подождите немного перед следующей попыткой.",
        )
        return
    if message.chat.type != "private":
        if rate_limited(f"dice-chat:{message.chat.id}", limit=15, window=15):
            return

    try:
        await get_or_create_user(
            user_id, message.from_user.username, message.from_user.full_name
        )
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
                    "⚠️ <b>Недостаточно монет</b>\n\n"
                    f"🪙 Нужно · <b>{fmt_num(DICE_MIN_BALANCE)}</b>\n"
                    f"🪙 У вас · <b>{fmt_num(balance)}</b>",
                )
                return

            time_passed = now - last_dice
            if last_dice > 0 and time_passed < DICE_COOLDOWN_SECONDS:
                remaining = DICE_COOLDOWN_SECONDS - time_passed
                time_str = fmt_remaining(remaining)
                wait_emoji = random.choice(WAIT_EMOJIS)
                await reply_ephemeral(
                    message,
                    f"{wait_emoji} <b>Кубик недоступен</b>\n\n"
                    f"Следующий бросок через <b>{time_str}</b>.",
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
        delta = dice_delta(dice_value)

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
            title = "Ничья"

        result = (
            f"🎲 <b>{title}</b>\n\n"
            f"🎲 Выпало · <b>{dice_value}</b>\n"
            f"🪙 Итог · <b>{delta_str}</b>\n\n"
            f"🪙 У вас · <b>{fmt_num(new_balance)}</b> {plural(new_balance, 'монета', 'монеты', 'монет')}"
        )
        try:
            await spin_msg.reply(result)
        except TelegramBadRequest:
            await message.reply(result)
    except Exception as e:
        logger.error("dice: %s", e)
        await reply_ephemeral(
            message,
            "❌ <b>Ошибка</b>\n\n"
            "Попробуйте ещё раз чуть позже.",
        )


# ═══════════════════════════════════════════════════════════════════════════
# PROFILE
# ═══════════════════════════════════════════════════════════════════════════
@router.message(F.text == "👤 Профиль")
@router.message(F.text == "👤 Мой профиль")
@router.message(Command("profile"))
@router.message(F.text.regexp(PROFILE_CMD_RE))
async def show_profile(message: Message) -> None:
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
                message.from_user.id,
                message.from_user.username,
                message.from_user.full_name,
            )

        is_own = target.id == message.from_user.id
        photo, caption, kb = await render_profile(
            message.bot, target.id, fallback_name=target.full_name, is_own=is_own
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
        logger.error("profile: %s", e)
        await message.reply("❌ Ошибка профиля.")


@router.callback_query(BackToProfileCallback.filter())
async def process_back_to_profile(
    callback: CallbackQuery,
    callback_data: BackToProfileCallback,
) -> None:
    if not await check_not_banned_cb(callback):
        return
    if not _owner_check(callback, callback_data.user_id):
        await callback.answer("⚠️ Не ваша кнопка")
        return

    try:
        target_id = callback_data.user_id or callback.from_user.id
        is_own = target_id == callback.from_user.id
        photo, caption, kb = await render_profile(
            callback.message.bot,
            target_id,
            fallback_name=callback.from_user.full_name,
            is_own=is_own,
        )
        await show_or_edit_photo(callback.message, photo, caption, kb)
        await callback.answer()
    except Exception as e:
        logger.error("back profile: %s", e)
        await callback.answer("⚠️ Ошибка")


# ═══════════════════════════════════════════════════════════════════════════
# NICKNAME
# ═══════════════════════════════════════════════════════════════════════════
@router.message(Command("nick"))
@router.message(F.text.regexp(NICK_CMD_RE))
async def nickname_cmd(
    message: Message,
    command: CommandObject = None,
    state: FSMContext = None,
) -> None:
    if not await check_not_banned(message):
        return

    user_id = message.from_user.id
    await get_or_create_user(
        user_id, message.from_user.username, message.from_user.full_name
    )

    arg = ""
    if command and command.args:
        arg = command.args.strip()
    elif message.text:
        m = NICK_CMD_RE.match(message.text.strip())
        if m:
            arg = m.group(1).strip()

    if arg.lower() == "reset":
        b = InlineKeyboardBuilder()
        b.button(text="✓ Подтвердить", callback_data=NickConfirmCallback(action="reset").pack())
        b.button(text="✕ Отмена", callback_data=NickConfirmCallback(action="cancel").pack())
        b.adjust(2)
        await state.set_state(NicknameSG.pending)
        await state.update_data(pending_nick=None, pending_action="reset")
        await message.reply(
            "♻️ <b>Сбросить никнейм?</b>\n\n"
            "ℹ️ Результат · имя из Telegram\n"
            "🪙 Стоимость · бесплатно",
            reply_markup=b.as_markup(),
        )
        return

    if not arg:
        await message.reply(
            "✏️ <b>Смена никнейма</b>\n\n"
            f"<code>/nick НовыйНик</code> · {NICKNAME_COST} 🪙\n\n"
            "♻️ Сброс · /nick reset · бесплатно"
        )
        return

    new_nick = validate_nickname(arg)
    if not new_nick:
        await message.reply(
            "❌ <b>Неверный никнейм</b>\n\n"
            "2–32 символа, без ссылок и @."
        )
        return

    row = await get_user_row(user_id)
    balance = row["coins"] if row else 0
    if balance < NICKNAME_COST:
        await message.reply(
            "⚠️ <b>Недостаточно монет</b>\n\n"
            f"🪙 Нужно · <b>{fmt_num(NICKNAME_COST)}</b>\n"
            f"🪙 У вас · <b>{fmt_num(balance)}</b>"
        )
        return

    b = InlineKeyboardBuilder()
    b.button(text="✓ Подтвердить", callback_data=NickConfirmCallback(action="apply").pack())
    b.button(text="✕ Отмена", callback_data=NickConfirmCallback(action="cancel").pack())
    b.adjust(2)
    await state.set_state(NicknameSG.pending)
    await state.update_data(pending_nick=new_nick, pending_action="apply")
    await message.reply(
        "✏️ <b>Сменить никнейм?</b>\n\n"
        f"👤 Новый ник · <b>{esc(new_nick)}</b>\n"
        f"🪙 Стоимость · <b>{NICKNAME_COST}</b>\n\n"
        f"🪙 У вас · {fmt_num(balance)}",
        reply_markup=b.as_markup(),
    )


@router.callback_query(NickConfirmCallback.filter())
async def nickname_confirm(
    callback: CallbackQuery,
    callback_data: NickConfirmCallback,
    state: FSMContext,
) -> None:
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
            "✓ <b>Никнейм сброшен</b>\n\n"
            f"👤 Отображение · {esc(shown)}",
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
            "✓ <b>Никнейм изменён</b>\n\n"
            f"👤 Ник · <b>{esc(new_nick)}</b>\n"
            f"🪙 Списано · {NICKNAME_COST}",
            reply_markup=get_ok_kb(),
        )
        await callback.answer()
        return

    await callback.answer()


# ═══════════════════════════════════════════════════════════════════════════
# TOP
# ═══════════════════════════════════════════════════════════════════════════
async def build_top_text(kind: str, current_user_id: int) -> str:
    await burn_expired_streak(current_user_id)
    limit = 10
    now_ts = int(time.time())

    async with get_db() as db:
        if kind == "cards":
            cur = await db.execute(
                """
                SELECT u.user_id, u.nickname,
                       (SELECT COUNT(*) FROM inventory i WHERE i.user_id = u.user_id) AS value
                FROM users u
                WHERE u.role != 'banned'
                ORDER BY value DESC, u.user_id ASC LIMIT ?
                """,
                (limit,),
            )
            top = await cur.fetchall()
            cur = await db.execute(
                "SELECT COUNT(*) AS value FROM inventory WHERE user_id = ?",
                (current_user_id,),
            )
            my_value = (await cur.fetchone())["value"] or 0
            cur = await db.execute(
                """
                SELECT COUNT(*) + 1 FROM (
                    SELECT u.user_id,
                           (SELECT COUNT(*) FROM inventory i WHERE i.user_id = u.user_id) AS value
                    FROM users u WHERE u.role != 'banned'
                ) WHERE value > ?
                """,
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
            cur = await db.execute(
                "SELECT coins AS value FROM users WHERE user_id = ?", (current_user_id,)
            )
            my_row = await cur.fetchone()
            my_value = my_row["value"] if my_row else 0
            cur = await db.execute(
                "SELECT COUNT(*) + 1 FROM users WHERE coins > ? AND role != 'banned'",
                (my_value,),
            )
            my_rank = (await cur.fetchone())[0]
            unit, title = "🪙", "🪙 Топ по монетам"

        cur = await db.execute(
            "SELECT nickname FROM users WHERE user_id = ?", (current_user_id,)
        )
        my_row2 = await cur.fetchone()
        my_nick = display_name(
            my_row2["nickname"] if my_row2 else None, current_user_id
        )

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, row in enumerate(top, 1):
        medal = medals[i - 1] if i <= 3 else f"{i}."
        nick = display_name(row["nickname"], row["user_id"])
        lines.append(f"{medal} {esc(nick)} · <b>{fmt_num(row['value'])}</b> {unit}")

    text = (
        f"<b>{title}</b>\n"
        f"{'─' * 18}\n\n"
        f"{chr(10).join(lines)}\n\n"
        f"📌 Ваше место · <b>#{fmt_num(my_rank)}</b>\n"
        f"👤 {esc(my_nick)} · <b>{fmt_num(my_value)}</b> {unit}"
    )
    return text


@router.message(F.text == "🏆 Топ")
@router.message(F.text == "🏆 Топ игроков")
@router.message(Command("top"))
@router.message(F.text.regexp(TOP_CMD_RE))
async def show_top_players(message: Message) -> None:
    if not await check_not_banned(message):
        return
    if rate_limited(f"top:{message.from_user.id}", limit=6, window=5):
        return
    try:
        text = await build_top_text("coins", message.from_user.id)
        await message.reply(text, reply_markup=get_top_keyboard("coins"))
    except Exception as e:
        logger.error("top: %s", e)
        await message.reply("❌ Ошибка топа")


@router.callback_query(TopCallback.filter())
async def switch_top(callback: CallbackQuery, callback_data: TopCallback) -> None:
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
                await callback.answer("Данные не изменились")
            else:
                await callback.message.answer(
                    text, reply_markup=get_top_keyboard(callback_data.kind)
                )
                await callback.answer()
    except Exception as e:
        logger.error("switch top: %s", e)
        await callback.answer("⚠️ Ошибка")


@router.callback_query(TopRefreshCallback.filter())
async def refresh_top(
    callback: CallbackQuery,
    callback_data: TopRefreshCallback,
) -> None:
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
                await callback.answer("Данные не изменились")
            else:
                await callback.message.answer(
                    text, reply_markup=get_top_keyboard(callback_data.kind)
                )
                await callback.answer()
    except Exception as e:
        logger.error("refresh top: %s", e)
        await callback.answer("⚠️ Ошибка")


# ═══════════════════════════════════════════════════════════════════════════
# COLLECTION
# ═══════════════════════════════════════════════════════════════════════════
async def get_collection_main_keyboard(user_id: int):
    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT c.rarity, COUNT(*)
            FROM inventory i JOIN cards c ON i.card_id = c.id
            WHERE i.user_id = ?
            GROUP BY c.rarity
            """,
            (user_id,),
        )
        stats = dict(await cur.fetchall())
        cur = await db.execute("SELECT COUNT(*) FROM cards")
        total_in_game = (await cur.fetchone())[0]

        total_cards = sum(stats.values())
        buttons = []
        for r_key, r_info in RARITIES.items():
            user_amount = stats.get(r_key, 0)
            if user_amount == 0:
                continue
            cur = await db.execute(
                "SELECT COUNT(*) FROM cards WHERE rarity = ?", (r_key,)
            )
            total_of_rarity = (await cur.fetchone())[0]
            buttons.append(
                InlineKeyboardButton(
                    text=(
                        f"{r_info['icon']} {r_info['name'][:8]} "
                        f"{fmt_num(user_amount)}/{fmt_num(total_of_rarity)}"
                    ),
                    callback_data=RaritySelectCallback(
                        rarity=r_key, page=0, user_id=user_id
                    ).pack(),
                )
            )
        # 2 колонки (3 ряда по 2 при 6 редкостях)
        grid = []
        for i in range(0, len(buttons), 2):
            grid.append(buttons[i : i + 2])
        grid.append([
            InlineKeyboardButton(
                text="‹ В профиль",
                callback_data=BackToProfileCallback(user_id=user_id).pack(),
            )
        ])
        return InlineKeyboardMarkup(inline_keyboard=grid), total_cards, total_in_game


@router.callback_query(RaritySelectCallback.filter())
async def process_rarity_view(
    callback: CallbackQuery,
    callback_data: RaritySelectCallback,
) -> None:
    if not await check_not_banned_cb(callback):
        return
    if not _owner_check(callback, callback_data.user_id):
        await callback.answer("⚠️ Не ваша кнопка")
        return

    # Flood control: не чаще 1 EditMessageMedia / 1.2 сек на пользователя
    if rate_limited(f"coll-flip:{callback.from_user.id}", limit=1, window=1.2):
        await callback.answer("⏳ Подождите…")
        return

    user_id = callback_data.user_id or callback.from_user.id
    rarity, page = callback_data.rarity, callback_data.page

    try:
        async with get_db() as db:
            cur = await db.execute(
                """
                SELECT c.name, c.photo_id, c.photo_path, i.claim_time, c.id
                FROM inventory i JOIN cards c ON i.card_id = c.id
                WHERE i.user_id = ? AND c.rarity = ?
                ORDER BY i.claim_time DESC
                """,
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
        name_str = f"<b>{esc(card['name'])}</b>"
        rw = int(info.get("reward", 0))
        if rw < 0:
            reward_str = f"−<b>{fmt_num(abs(rw))}</b> 🪙"
        else:
            reward_str = f"+<b>{fmt_num(rw)}</b> 🪙"
        rarity_line = f"💎{info.get('icon', '•')} {info.get('name', rarity)}"
        caption = (
            f"🃏 {name_str}\n"
            f"{rarity_line}\n\n"
            f"{reward_str}"
        )

        nav = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    text="‹",
                    callback_data=RaritySelectCallback(
                        rarity=rarity, page=page - 1, user_id=user_id
                    ).pack(),
                )
            )
        nav.append(
            InlineKeyboardButton(text=f"{page + 1} / {total_pages}", callback_data="ignore")
        )
        if page < total_pages - 1:
            nav.append(
                InlineKeyboardButton(
                    text="›",
                    callback_data=RaritySelectCallback(
                        rarity=rarity, page=page + 1, user_id=user_id
                    ).pack(),
                )
            )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                nav,
                [
                    InlineKeyboardButton(
                        text="‹ К редкостям",
                        callback_data=MainMenuCallback(user_id=user_id).pack(),
                    )
                ],
            ]
        )

        photo = None
        if get_card_photo_input:
            photo = get_card_photo_input(
                card["id"],
                card["photo_path"] if "photo_path" in card.keys() else None,
            )
        if not photo and card["photo_id"]:
            photo = card["photo_id"]

        await show_or_edit_photo(callback.message, photo, caption, keyboard)
        await callback.answer()
    except Exception as e:
        logger.error("rarity view: %s", e)
        await callback.answer("⚠️ Ошибка")


@router.callback_query(MainMenuCallback.filter())
async def process_back_to_main(
    callback: CallbackQuery,
    callback_data: MainMenuCallback,
) -> None:
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
async def ignore_callback(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(OkDeleteCallback.filter())
async def ok_delete_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    bot_msg = callback.message
    user_msg = bot_msg.reply_to_message if bot_msg else None
    await _try_delete(bot_msg)
    await _try_delete(user_msg)


@router.callback_query(CardActionCallback.filter())
async def handle_card_action(
    callback: CallbackQuery,
    callback_data: CardActionCallback,
) -> None:
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
        if action == "collection":
            photo, caption, keyboard, total = await render_collection(
                callback.message.bot, user_id, callback.from_user.full_name
            )
            # Редактируем сообщение о новой карточке, а не шлём новое
            await show_or_edit_photo(callback.message, photo, caption, keyboard)
            await callback.answer()
    except Exception as e:
        logger.error("card action: %s", e)
        await callback.answer("⚠️ Ошибка")


# ═══════════════════════════════════════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════════════════════════════════════
async def _run_api_server() -> None:
    try:
        import uvicorn
        from api import app as fastapi_app
    except ImportError as e:
        logger.warning("API мини-аппки не запущен: %s", e)
        return

    port = int(os.getenv("PORT", os.getenv("API_PORT", "8080")))
    logger.info("🌐 API на 0.0.0.0:%s", port)
    config = uvicorn.Config(
        fastapi_app, host="0.0.0.0", port=port, log_level="info", lifespan="on"
    )
    server = uvicorn.Server(config)
    await server.serve()


async def main() -> None:
    try:
        os.makedirs(os.path.dirname(DB_NAME) or ".", exist_ok=True)
        if LOG_PATH:
            os.makedirs(os.path.dirname(LOG_PATH) or ".", exist_ok=True)

        await init_db()
        logger.info("БД инициализирована")

        async with get_db() as db:
            await ensure_superadmin_from_env(db)
            await ensure_config_tables(db)
            if not await is_user_bot_enabled(db):
                logger.warning("user_bot_enabled=0 — user-бот не стартует (только polling skip)")
                # всё равно можно крутить admin/api ниже
        await refresh_rarities()

        try:
            ensure_photo_dir()
        except Exception as e:
            logger.warning("photo dir: %s", e)

        tasks = []
        from_main = os.getenv("_MAIN_ENTRY") == "1"

        async with get_db() as db:
            user_on = await is_user_bot_enabled(db)

        if user_on:
            bot = Bot(
                token=BOT_TOKEN,
                default=DefaultBotProperties(
                    parse_mode=ParseMode.HTML,
                    link_preview=LinkPreviewOptions(is_disabled=True),
                ),
            )
            if ensure_default_avatar:
                try:
                    await ensure_default_avatar(bot)
                except Exception as e:
                    logger.warning("default avatar: %s", e)
            dp = Dispatcher(storage=MemoryStorage())
            dp.include_router(router)
            await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
            logger.info("🤖 Пользовательский бот запущен")
            tasks.append(dp.start_polling(bot))
        else:
            logger.warning("🤖 User-бот выключен через admin (/userbot off)")

        # admin: если не из main.py — поднимаем сами
        admin_token = (
            os.getenv("ADMIN_BOT_TOKEN") or os.getenv("BOT_TOKEN_ADMIN") or ""
        ).strip()
        if admin_token and not from_main:
            try:
                from admin_bot import run_admin_bot
                tasks.append(run_admin_bot(admin_token))
                logger.info("🛡 Админ-бот параллельно")
            except Exception as e:
                logger.error("admin_bot: %s", e)
        elif admin_token and from_main:
            # main.py мог не поднять admin, если user включён — поднимем здесь
            try:
                from admin_bot import run_admin_bot
                tasks.append(run_admin_bot(admin_token))
                logger.info("🛡 Админ-бот (из bot через main)")
            except Exception as e:
                logger.error("admin_bot: %s", e)

        start_api = os.getenv("START_API", os.getenv("ENABLE_WEBAPP_API", "0")).strip().lower() in (
            "1", "true", "yes", "on",
        )
        if start_api and not from_main:
            tasks.append(_run_api_server())

        if not tasks:
            logger.error("Нет задач для запуска (user выключен, admin нет)")
            return

        await asyncio.gather(*tasks)
    except Exception as e:
        logger.error("Критическая ошибка: %s", e)
        raise


if __name__ == "__main__":
    asyncio.run(main())
