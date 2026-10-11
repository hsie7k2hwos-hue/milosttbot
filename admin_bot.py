"""
Админ-бот карточной игры (отдельный процесс, общая БД).

Запуск:
  ADMIN_BOT_TOKEN=... BOT_TOKEN=... DB_NAME=/app/data/cards_game.db python admin_bot.py

SUPERADMIN_IDS — через запятую в .env (если в БД нет superadmin).
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, BaseFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeDefault,
    CallbackQuery,
    FSInputFile,
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

try:
    from card_photos import (
        sync_card_photo,
        migrate_all_card_photos,
        ensure_photo_dir,
        get_card_photo_input,
        ensure_default_avatar,
        DEFAULT_AVATAR_PATH,
        DEFAULT_AVATAR_FILE_ID,
        reframe_all_photos,
        migrate_to_uuid_names,
        RARITY_FRAME_COLORS,
    )
except ImportError:
    sync_card_photo = None
    migrate_all_card_photos = None
    get_card_photo_input = None
    ensure_default_avatar = None
    reframe_all_photos = None
    migrate_to_uuid_names = None
    RARITY_FRAME_COLORS = {}
    DEFAULT_AVATAR_PATH = Path("/app/data/card_photos/default_avatar.jpg")
    DEFAULT_AVATAR_FILE_ID = (
        "AgACAgIAAxkBAAID12qql3EFpnb2HwTCE7Yn_Ri1TQsNAAKRIGsbfHlYSVUpxfU75O60AQADAgADeAADPQQ"
    )

    def ensure_photo_dir() -> None:
        pass


load_dotenv()

ADMIN_BOT_TOKEN = (
    os.getenv("ADMIN_BOT_TOKEN") or os.getenv("BOT_TOKEN_ADMIN") or ""
).strip()
USER_BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()

DB_NAME = os.getenv("DB_NAME", "/app/data/cards_game.db")
ADMIN_LOG_PATH = os.getenv("ADMIN_LOG_PATH", "/app/data/admin_bot.log")
USER_LOG_PATH = os.getenv("LOG_PATH", "/app/data/bot.log")
AVATARS_DIR = Path(os.getenv("AVATARS_DIR", "/app/data/user_avatars"))
API_BASE = (os.getenv("API_BASE") or os.getenv("WEBAPP_API_URL") or "").rstrip("/")
WEBAPP_URL = (os.getenv("WEBAPP_URL") or "").strip()

SUPERADMIN_IDS = {
    int(x.strip())
    for x in (os.getenv("SUPERADMIN_IDS") or os.getenv("SUPERADMIN_ID") or "").split(",")
    if x.strip().isdigit()
}

COOLDOWN_SECONDS = 3 * 3600

try:
    from game_config import DEFAULT_RARITIES, OLD_RARITY_MAP, RARITY_ORDER as _RO
    RARITIES = {k: dict(v) for k, v in DEFAULT_RARITIES.items()}
    RARITY_ORDER = list(_RO)
except ImportError:
    RARITIES = {
        "common":         {"icon": "⚪", "name": "Обычная",     "weight": 38, "reward": 10},
        "uncommon":       {"icon": "🟢", "name": "Необычная",   "weight": 20, "reward": 40},
        "valuable":       {"icon": "🟣", "name": "Ценная",      "weight": 12, "reward": 70},
        "super_valuable": {"icon": "🟠", "name": "Сверхценная", "weight": 8,  "reward": 120},
        "legendary":      {"icon": "🟡", "name": "Легендарная", "weight": 4,  "reward": 200},
        "negative":       {"icon": "⬛", "name": "Негативная",  "weight": 18, "reward": -20},
    }
    RARITY_ORDER = list(RARITIES.keys())
    OLD_RARITY_MAP = {
        "common": "common",
        "rare": "uncommon",
        "epic": "uncommon",
        "mythical": "valuable",
        "legendary": "legendary",
    }

ROLES = {
    "user":       {"icon": "👤", "name": "Пользователь"},
    "admin":      {"icon": "🛡", "name": "Администратор"},
    "superadmin": {"icon": "👑", "name": "Главный администратор"},
    "banned":     {"icon": "🚫", "name": "Заблокирован"},
}

NICKNAME_RE = re.compile(r"^[\w\-. ]{2,32}$", re.UNICODE)
URL_RE = re.compile(r"(https?://|t\.me/|@\w+)", re.IGNORECASE)

PER_PAGE = 5
AVATAR_BATCH = 25
AVATAR_DELAY = (3.0, 5.0)


def _setup_logging() -> logging.Logger:
    log = logging.getLogger("admin_bot")
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(funcName)s:%(lineno)d | %(message)s"
    )
    sh = logging.StreamHandler()
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    try:
        Path(ADMIN_LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(ADMIN_LOG_PATH, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except Exception as exc:
        log.warning("Не удалось открыть файл логов %s: %s", ADMIN_LOG_PATH, exc)
    # aiogram/aiosqlite — меньше шума в консоль
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    return log


logger = _setup_logging()


def esc(text) -> str:
    return html.escape(str(text), quote=False)


def fmt_num(n) -> str:
    try:
        return f"{int(n):,}".replace(",", "\u00a0")
    except (TypeError, ValueError):
        return str(n)


def line(emoji: str, label: str, value) -> str:
    return f"{emoji} <b>{label}:</b> {value}"


def role_display(role: str) -> str:
    info = ROLES.get(role, ROLES["user"])
    return f"{info['icon']} {info['name']}"


def display_name(nickname, user_id, fallback: Optional[str] = None) -> str:
    if nickname and str(nickname).strip():
        return str(nickname).strip()
    if fallback and str(fallback).strip():
        return str(fallback).strip()[:32]
    return str(user_id)


def bq(text: str) -> str:
    return f"<blockquote>{text}</blockquote>"


def sep(width: int = 18) -> str:
    return "─" * width


async def safe_edit(
    message: Message,
    *,
    text: Optional[str] = None,
    caption: Optional[str] = None,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    media: Optional[Union[str, FSInputFile]] = None,
) -> Message:
    try:
        if media is not None:
            await message.edit_media(
                media=InputMediaPhoto(media=media, caption=caption or ""),
                reply_markup=reply_markup,
            )
            return message
        if caption is not None and message.photo:
            await message.edit_caption(caption=caption, reply_markup=reply_markup)
            return message
        if text is not None:
            await message.edit_text(text, reply_markup=reply_markup)
            return message
        raise TelegramBadRequest("Nothing to edit")
    except TelegramBadRequest:
        try:
            await message.delete()
        except Exception:
            pass
        if media is not None:
            return await message.answer_photo(
                photo=media, caption=caption or "", reply_markup=reply_markup
            )
        return await message.answer(text or caption or "—", reply_markup=reply_markup)


# ── Callback data ──
class AdminRarityCallback(CallbackData, prefix="ar"):
    card_id: int
    rarity: str


class AdminCardFilterCallback(CallbackData, prefix="acf"):
    rarity: str


class AdminCardPageCallback(CallbackData, prefix="acp"):
    page: int
    rarity: str = "all"


class AdminCardManageCallback(CallbackData, prefix="acm"):
    card_id: int
    rarity: str = "all"


class AdminCardEditPhotoCallback(CallbackData, prefix="acep"):
    card_id: int


class AdminUserFilterCallback(CallbackData, prefix="auf"):
    role: str


class AdminUserPageCallback(CallbackData, prefix="aup"):
    page: int
    filter_role: str = "all"


class AdminUserViewCallback(CallbackData, prefix="auv"):
    user_id: int
    filter_role: str = "all"


class AdminUserActionCallback(CallbackData, prefix="aua"):
    action: str
    user_id: int


class AdminHelpCallback(CallbackData, prefix="ah"):
    page: int = 0


class AdminStatsCallback(CallbackData, prefix="ast"):
    section: str = "overview"


class AdminMainCallback(CallbackData, prefix="amain"):
    pass


# ── DB ──
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
        logger.error("DB error: %s", e)
        raise
    finally:
        await db.close()


async def init_db_minimal() -> None:
    async with get_db() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                rarity TEXT NOT NULL,
                photo_id TEXT,
                photo_path TEXT,
                photo_uuid TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                last_claim INTEGER DEFAULT 0,
                role TEXT DEFAULT 'user',
                nickname TEXT,
                coins INTEGER DEFAULT 0,
                registration INTEGER DEFAULT 0,
                streak INTEGER DEFAULT 0,
                last_streak_date INTEGER DEFAULT 0,
                streak_bonus INTEGER DEFAULT 0,
                last_dice INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                user_id INTEGER,
                card_id INTEGER,
                claim_time INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, card_id),
                FOREIGN KEY (card_id) REFERENCES cards (id) ON DELETE CASCADE
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_avatars (
                user_id INTEGER PRIMARY KEY,
                path TEXT,
                file_hash TEXT,
                updated_at INTEGER DEFAULT 0
            )
        """)
        for table, column, definition in (
            ("cards", "photo_path", "TEXT"),
            ("cards", "photo_id", "TEXT"),
            ("cards", "photo_uuid", "TEXT"),
            ("users", "last_dice", "INTEGER DEFAULT 0"),
        ):
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            except aiosqlite.OperationalError:
                pass
        try:
            await db.execute("DROP TABLE IF EXISTS duels")
        except Exception:
            pass


async def ensure_superadmin_from_env() -> None:
    if not SUPERADMIN_IDS:
        return
    async with get_db() as db:
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
            else:
                now = int(time.time())
                await db.execute(
                    "INSERT OR IGNORE INTO users (user_id, registration, role) "
                    "VALUES (?, ?, 'superadmin')",
                    (sid, now),
                )
            logger.info("SUPERADMIN_IDS: %s → superadmin", sid)


async def get_user_role(user_id: int) -> str:
    async with get_db() as db:
        cur = await db.execute("SELECT role FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        if not row:
            return "user"
        role = row[0] or "user"
        return role if role in ROLES else "user"


async def is_admin(user_id: int) -> bool:
    return (await get_user_role(user_id)) in ("admin", "superadmin")


async def is_superadmin(user_id: int) -> bool:
    return (await get_user_role(user_id)) == "superadmin"


async def set_user_role(user_id: int, role: str) -> bool:
    if role not in ROLES:
        return False
    async with get_db() as db:
        cur = await db.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        if not await cur.fetchone():
            return False
        await db.execute("UPDATE users SET role = ? WHERE user_id = ?", (role, user_id))
    return True


# ── Avatar cache ──
def avatar_path(user_id: int) -> Path:
    return AVATARS_DIR / f"{user_id}.jpg"


async def get_cached_avatar(user_id: int) -> Optional[Union[FSInputFile, str]]:
    p = avatar_path(user_id)
    if p.is_file() and p.stat().st_size > 0:
        return FSInputFile(p)
    if DEFAULT_AVATAR_PATH and Path(DEFAULT_AVATAR_PATH).is_file():
        return FSInputFile(DEFAULT_AVATAR_PATH)
    if DEFAULT_AVATAR_FILE_ID:
        return DEFAULT_AVATAR_FILE_ID
    return None


async def cache_user_avatar(user_bot: Bot, user_id: int) -> str:
    AVATARS_DIR.mkdir(parents=True, exist_ok=True)
    dest = avatar_path(user_id)
    try:
        photos = await user_bot.get_user_profile_photos(user_id, limit=1)
        if photos.total_count == 0 or not photos.photos:
            return "empty"
        file_id = photos.photos[0][-1].file_id
        tg_file = await user_bot.get_file(file_id)
        data = await user_bot.download_file(tg_file.file_path)
        content = data.read()
        new_hash = hashlib.md5(content).hexdigest()
        old_hash = None
        async with get_db() as db:
            cur = await db.execute(
                "SELECT file_hash FROM user_avatars WHERE user_id = ?", (user_id,)
            )
            row = await cur.fetchone()
            if row:
                old_hash = row["file_hash"]
        if old_hash == new_hash and dest.is_file():
            return "unchanged"
        dest.write_bytes(content)
        async with get_db() as db:
            await db.execute(
                """INSERT INTO user_avatars (user_id, path, file_hash, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     path=excluded.path,
                     file_hash=excluded.file_hash,
                     updated_at=excluded.updated_at""",
                (user_id, str(dest), new_hash, int(time.time())),
            )
        return "updated" if old_hash else "new"
    except (TelegramForbiddenError, TelegramBadRequest):
        return "empty"
    except Exception as e:
        logger.error("cache_user_avatar %s: %s", user_id, e)
        return "error"


# ── Filters / FSM ──
class AdminFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return await is_admin(message.from_user.id)


class SuperAdminFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return await is_superadmin(message.from_user.id)


admin_filter = AdminFilter()
superadmin_filter = SuperAdminFilter()


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


# ── Keyboards ──
def get_main_reply_kb(fsm_active: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="➕ Добавить"), KeyboardButton(text="📜 Карточки")],
        [KeyboardButton(text="👥 Пользователи"), KeyboardButton(text="📊 Статистика")],
        [KeyboardButton(text="🏆 Топ"), KeyboardButton(text="📥 Логи")],
        [KeyboardButton(text="🛠 Справка")],
    ]
    if fsm_active:
        rows.append([KeyboardButton(text="❌ Отмена")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите раздел…",
    )


def get_rarity_keyboard(callback_prefix: str = "set_rarity") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key, val in RARITIES.items():
        b.button(text=f"{val['icon']} {val['name']}", callback_data=f"{callback_prefix}:{key}")
    b.button(text="✕ Отмена", callback_data="cancel_add_card")
    b.adjust(1)
    return b.as_markup()


# ── Balance helpers ──
async def get_rarity_counts() -> dict[str, int]:
    async with get_db() as db:
        cur = await db.execute("SELECT rarity, COUNT(*) FROM cards GROUP BY rarity")
        return dict(await cur.fetchall())


async def recommend_rarity() -> tuple[str, str]:
    """Рекомендует редкость с наибольшим дефицитом относительно целевых весов."""
    counts = await get_rarity_counts()
    total = sum(counts.values()) or 1
    best_key = "common"
    best_deficit = -1e9
    lines = []
    for key, info in RARITIES.items():
        actual = counts.get(key, 0)
        expected_share = info["weight"] / 100.0
        actual_share = actual / total
        deficit = expected_share - actual_share
        lines.append(
            f"{info['icon']} {info['name']}: {actual} "
            f"(цель ~{info['weight']}%, сейчас {actual_share*100:.0f}%)"
        )
        if deficit > best_deficit:
            best_deficit = deficit
            best_key = key
    rec = RARITIES[best_key]
    hint = (
        f"💡 Рекомендация: <b>{rec['icon']} {rec['name']}</b>\n"
        f"(наибольший дефицит относительно весов)\n\n"
        + "\n".join(lines)
    )
    return best_key, hint


# ── Router ──
router = Router()


async def _require_admin(event: Message | CallbackQuery) -> bool:
    uid = event.from_user.id
    if await is_admin(uid):
        return True
    text = "⚠️ Нет доступа. Требуется роль admin / superadmin."
    if isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=True)
    else:
        await event.answer(text)
    return False


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if not await _require_admin(message):
        return
    role = await get_user_role(message.from_user.id)
    text = (
        f"⚙️ <b>Админ-панель</b>\n"
        f"{sep()}\n\n"
        f"{line('🎭', 'Ваша роль', role_display(role))}\n\n"
        f"Используйте меню ниже или команды."
    )
    await message.answer(text, reply_markup=get_main_reply_kb())


@router.callback_query(AdminMainCallback.filter())
@router.callback_query(F.data == "admin_main")
async def admin_main_back(call: CallbackQuery):
    if not await _require_admin(call):
        return
    role = await get_user_role(call.from_user.id)
    text = (
        f"⚙️ <b>Админ-панель</b>\n"
        f"{sep()}\n\n"
        f"{line('🎭', 'Ваша роль', role_display(role))}\n\n"
        f"Используйте меню ниже или команды."
    )
    await safe_edit(call.message, text=text)
    await call.answer()


@router.message(Command("cancel"))
@router.message(F.text == "❌ Отмена")
async def cancel_handler(message: Message, state: FSMContext):
    current = await state.get_state()
    await state.clear()
    if current:
        await message.answer("✅ Операция отменена", reply_markup=get_main_reply_kb())
    else:
        await message.answer("Нечего отменять.", reply_markup=get_main_reply_kb())


@router.callback_query(F.data == "cancel_add_card")
async def cancel_add_card_callback(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await call.message.answer("✅ Отменено", reply_markup=get_main_reply_kb())


# ── ADD CARD ──
@router.message(F.text == "➕ Добавить")
@router.callback_query(F.data == "admin_add_card")
async def add_card_start(event: Message | CallbackQuery, state: FSMContext):
    if not await _require_admin(event):
        return
    await state.set_state(AddCardSG.photo)
    _, hint = await recommend_rarity()
    text = (
        "📷 <b>Отправьте фото новой карточки</b>\n\n"
        f"{bq(hint)}\n\n"
        "Или «❌ Отмена»."
    )
    kb = get_main_reply_kb(fsm_active=True)
    if isinstance(event, CallbackQuery):
        await event.message.answer(text, reply_markup=kb)
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb)


@router.message(Command("addcard"), admin_filter, F.photo)
async def quick_add_card(message: Message, command: CommandObject):
    args = (command.args or "").strip().split()
    if len(args) < 2:
        await message.reply(
            "✏️ /addcard Название редкость (+ фото)\n"
            "Редкости: " + ", ".join(RARITIES.keys())
        )
        return

    rarity = args[-1].lower()
    name = " ".join(args[:-1])[:64]
    if rarity not in RARITIES:
        await message.reply("❌ Неизвестная редкость.")
        return

    photo_id = message.photo[-1].file_id
    photo_path = None
    photo_uuid = None

    async with get_db() as db:
        cur = await db.execute(
            "INSERT INTO cards (name, rarity, photo_id) VALUES (?, ?, ?)",
            (name, rarity, photo_id),
        )
        card_id = cur.lastrowid
        if sync_card_photo:
            try:
                result = await sync_card_photo(message.bot, card_id, photo_id, rarity=rarity)
                if result:
                    photo_uuid, photo_path = result
                    await db.execute(
                        "UPDATE cards SET photo_path = ?, photo_uuid = ? WHERE id = ?",
                        (photo_path, photo_uuid, card_id),
                    )
            except Exception as e:
                logger.error("sync_card_photo: %s", e)

    r = RARITIES[rarity]
    body = "\n".join([
        line("🃏", "Название", esc(name)),
        line(r["icon"], "Редкость", r["name"]),
        line("🆔", "ID", card_id),
        line("💾", "Файл", photo_path or "только file_id"),
        line("🖼", "Рамка", "да" if photo_path else "нет"),
    ])
    await message.reply(
        f"✅ <b>Карточка добавлена</b>\n\n{bq(body)}",
        reply_markup=get_main_reply_kb(),
    )


@router.message(AddCardSG.photo, F.photo)
async def add_card_photo(message: Message, state: FSMContext):
    if not await _require_admin(message):
        await state.clear()
        return
    await state.update_data(photo_id=message.photo[-1].file_id)
    await state.set_state(AddCardSG.name)
    await message.answer(
        "✍️ <b>Введите название</b> (до 64 символов)",
        reply_markup=get_main_reply_kb(fsm_active=True),
    )


@router.message(AddCardSG.name, F.text)
async def add_card_name(message: Message, state: FSMContext):
    if not await _require_admin(message):
        await state.clear()
        return
    if message.text.strip() in ("❌ Отмена", "/cancel"):
        return
    await state.update_data(name=message.text[:64])
    await state.set_state(AddCardSG.rarity)
    _, hint = await recommend_rarity()
    await message.answer(
        f"🎲 <b>Выберите редкость</b>\n\n{bq(hint)}",
        reply_markup=get_rarity_keyboard(),
    )


@router.callback_query(AddCardSG.rarity, F.data.startswith("set_rarity:"))
async def add_card_rarity(call: CallbackQuery, state: FSMContext):
    if not await _require_admin(call):
        await state.clear()
        return

    rarity = call.data.split(":")[1]
    data = await state.get_data()
    photo_path = None
    photo_uuid = None

    async with get_db() as db:
        cur = await db.execute(
            "INSERT INTO cards (name, rarity, photo_id) VALUES (?, ?, ?)",
            (data["name"], rarity, data["photo_id"]),
        )
        card_id = cur.lastrowid
        if sync_card_photo:
            try:
                result = await sync_card_photo(
                    call.bot, card_id, data["photo_id"], rarity=rarity
                )
                if result:
                    photo_uuid, photo_path = result
                    await db.execute(
                        "UPDATE cards SET photo_path = ?, photo_uuid = ? WHERE id = ?",
                        (photo_path, photo_uuid, card_id),
                    )
            except Exception as e:
                logger.error("sync: %s", e)

    r = RARITIES[rarity]
    body = "\n".join([
        line("🃏", "Название", esc(data["name"])),
        line(r["icon"], "Редкость", r["name"]),
        line("🆔", "ID", card_id),
        line("💾", "Файл", photo_path or "только file_id"),
        line("🖼", "Рамка", "да" if photo_path else "нет"),
    ])
    await call.message.answer(
        f"✅ <b>Карточка добавлена</b>\n\n{bq(body)}",
        reply_markup=get_main_reply_kb(),
    )
    await state.clear()
    await call.answer()


# ── CARDS list ──
async def build_card_filter_menu() -> tuple[str, InlineKeyboardMarkup]:
    async with get_db() as db:
        cur = await db.execute("SELECT rarity, COUNT(*) FROM cards GROUP BY rarity")
        counts = dict(await cur.fetchall())
        cur = await db.execute("SELECT COUNT(*) FROM cards")
        total = (await cur.fetchone())[0]

    text = (
        f"📜 <b>Карточки</b>\n"
        f"{sep()}\n\n"
        f"Всего в базе: <b>{fmt_num(total)}</b>\n\n"
        f"Выберите редкость:"
    )
    b = InlineKeyboardBuilder()
    b.button(
        text=f"📦 Все ({fmt_num(total)})",
        callback_data=AdminCardFilterCallback(rarity="all").pack(),
    )
    for key, info in RARITIES.items():
        cnt = counts.get(key, 0)
        b.button(
            text=f"{info['icon']} {info['name']} ({fmt_num(cnt)})",
            callback_data=AdminCardFilterCallback(rarity=key).pack(),
        )
    b.button(text="‹ В меню", callback_data="admin_main")
    b.adjust(1)
    return text, b.as_markup()


async def build_admin_cards_page(page: int = 0, rarity: str = "all"):
    async with get_db() as db:
        if rarity == "all":
            cur = await db.execute("SELECT COUNT(*) FROM cards")
        else:
            cur = await db.execute(
                "SELECT COUNT(*) FROM cards WHERE rarity = ?", (rarity,)
            )
        total = (await cur.fetchone())[0]

        if total == 0:
            label = RARITIES[rarity]["name"] if rarity != "all" else "все"
            return (
                f"📜 <b>Карточки</b> · {label}\n\nПусто.",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text="‹ К редкостям",
                        callback_data="cards_filter_menu",
                    )],
                    [InlineKeyboardButton(text="‹ В меню", callback_data="admin_main")],
                ]),
            )

        total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
        page = max(0, min(page, total_pages - 1))
        offset = page * PER_PAGE

        if rarity == "all":
            cur = await db.execute(
                "SELECT id, name, rarity, photo_id, photo_path FROM cards "
                "ORDER BY id DESC LIMIT ? OFFSET ?",
                (PER_PAGE, offset),
            )
        else:
            cur = await db.execute(
                "SELECT id, name, rarity, photo_id, photo_path FROM cards "
                "WHERE rarity = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                (rarity, PER_PAGE, offset),
            )
        cards = await cur.fetchall()

        b = InlineKeyboardBuilder()
        lines = []
        for c in cards:
            r = RARITIES.get(c["rarity"], {})
            cur = await db.execute(
                "SELECT COUNT(*) FROM inventory WHERE card_id = ?", (c["id"],)
            )
            owners = (await cur.fetchone())[0]
            has_file = "💾" if c["photo_path"] else "☁️"
            lines.append(
                f"{r.get('icon', '•')} <b>{esc(c['name'])}</b>\n"
                f"    #{c['id']} · 👥 {fmt_num(owners)} · {has_file}"
            )
            b.button(
                text=f"{r.get('icon', '')} {c['name'][:28]}",
                callback_data=AdminCardManageCallback(
                    card_id=c["id"], rarity=rarity
                ).pack(),
            )

        filter_label = RARITIES[rarity]["name"] if rarity != "all" else "Все"
        text = (
            f"📜 <b>Карточки</b> · {filter_label}\n"
            f"{sep()}\n"
            f"{line('📄', 'Страница', f'<b>{page + 1}</b> / {total_pages}')}\n"
            f"{line('📦', 'Найдено', f'<b>{fmt_num(total)}</b>')}\n\n"
            f"{bq(chr(10).join(lines))}"
        )

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(
                text="‹",
                callback_data=AdminCardPageCallback(page=page - 1, rarity=rarity).pack(),
            ))
        nav.append(InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}", callback_data="ignore"
        ))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(
                text="›",
                callback_data=AdminCardPageCallback(page=page + 1, rarity=rarity).pack(),
            ))

        b.adjust(1)
        b.row(*nav)
        b.row(InlineKeyboardButton(
            text="‹ К редкостям", callback_data="cards_filter_menu"
        ))
        b.row(InlineKeyboardButton(text="‹ В меню", callback_data="admin_main"))
        return text, b.as_markup()


@router.message(F.text == "📜 Карточки")
@router.callback_query(F.data == "cards_filter_menu")
async def cards_filter_entry(event: Message | CallbackQuery):
    if not await _require_admin(event):
        return
    text, kb = await build_card_filter_menu()
    if isinstance(event, CallbackQuery):
        await safe_edit(event.message, text=text, reply_markup=kb)
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb)


@router.callback_query(AdminCardFilterCallback.filter())
async def cards_filter_chosen(call: CallbackQuery, callback_data: AdminCardFilterCallback):
    if not await _require_admin(call):
        return
    text, kb = await build_admin_cards_page(0, callback_data.rarity)
    await safe_edit(call.message, text=text, reply_markup=kb)
    await call.answer()


@router.callback_query(AdminCardPageCallback.filter())
async def admin_cards_page(call: CallbackQuery, callback_data: AdminCardPageCallback):
    if not await _require_admin(call):
        return
    text, kb = await build_admin_cards_page(callback_data.page, callback_data.rarity)
    await safe_edit(call.message, text=text, reply_markup=kb)
    await call.answer()


@router.callback_query(AdminCardManageCallback.filter())
async def admin_card_manage(call: CallbackQuery, callback_data: AdminCardManageCallback):
    if not await _require_admin(call):
        return

    card_id = callback_data.card_id
    back_rarity = callback_data.rarity

    async with get_db() as db:
        cur = await db.execute(
            "SELECT name, rarity, photo_id, photo_path FROM cards WHERE id = ?",
            (card_id,),
        )
        card = await cur.fetchone()
        if not card:
            await call.answer("❌ Карточка не найдена", show_alert=True)
            return
        cur = await db.execute(
            "SELECT COUNT(*) FROM inventory WHERE card_id = ?", (card_id,)
        )
        owners = (await cur.fetchone())[0]

    r = RARITIES.get(card["rarity"], {})
    body = "\n".join([
        line("🆔", "ID", f"<code>{card_id}</code>"),
        line(r.get("icon", "•"), "Редкость", r.get("name", card["rarity"])),
        line("👥", "Владельцев", f"<b>{fmt_num(owners)}</b>"),
        line("💾", "Файл", card["photo_path"] or "нет"),
    ])
    caption = f"🃏 <b>{esc(card['name'])}</b>\n{sep(20)}\n\n{bq(body)}"

    b = InlineKeyboardBuilder()
    b.button(text="✏️ Название", callback_data=f"edit_name:{card_id}")
    b.button(text="✨ ИИ название", callback_data=f"ai_name:{card_id}")
    b.button(text="🎲 Редкость", callback_data=f"edit_rarity:{card_id}")
    b.button(text="🖼 Фото", callback_data=AdminCardEditPhotoCallback(card_id=card_id).pack())
    b.button(text="🗑 Удалить", callback_data=f"delete_card:{card_id}")
    b.button(
        text="‹ К списку",
        callback_data=AdminCardPageCallback(page=0, rarity=back_rarity).pack(),
    )
    b.adjust(1)

    photo = None
    if get_card_photo_input:
        photo = get_card_photo_input(card_id, card["photo_path"])
    if not photo and card["photo_id"]:
        photo = card["photo_id"]

    await safe_edit(
        call.message,
        text=caption if not photo else None,
        caption=caption if photo else None,
        media=photo,
        reply_markup=b.as_markup(),
    )
    await call.answer()


@router.callback_query(AdminCardEditPhotoCallback.filter())
async def admin_card_edit_photo_start(
    call: CallbackQuery, callback_data: AdminCardEditPhotoCallback, state: FSMContext
):
    if not await _require_admin(call):
        return
    await state.set_state(EditCardPhotoSG.photo)
    await state.update_data(card_id=callback_data.card_id)
    await call.message.answer(
        "📷 Отправьте новое фото\n\nИли «❌ Отмена».",
        reply_markup=get_main_reply_kb(fsm_active=True),
    )
    await call.answer()


@router.message(EditCardPhotoSG.photo, F.photo)
async def admin_card_edit_photo_save(message: Message, state: FSMContext):
    if not await _require_admin(message):
        await state.clear()
        return

    data = await state.get_data()
    card_id = data.get("card_id")
    if not card_id:
        await state.clear()
        return

    new_photo = message.photo[-1].file_id
    path_str = None
    photo_uuid = None

    async with get_db() as db:
        cur = await db.execute("SELECT rarity, photo_uuid FROM cards WHERE id = ?", (card_id,))
        row = await cur.fetchone()
        rarity = (row["rarity"] if row else "common") or "common"
        existing_uuid = row["photo_uuid"] if row else None
        await db.execute(
            "UPDATE cards SET photo_id = ? WHERE id = ?", (new_photo, card_id)
        )
        if sync_card_photo:
            try:
                result = await sync_card_photo(
                    message.bot, card_id, new_photo, rarity=rarity, existing_uuid=existing_uuid
                )
                if result:
                    photo_uuid, path_str = result
                    await db.execute(
                        "UPDATE cards SET photo_path = ?, photo_uuid = ? WHERE id = ?",
                        (path_str, photo_uuid, card_id),
                    )
            except Exception as e:
                logger.error("sync edit: %s", e)

    await message.answer(
        f"✅ Фото обновлено (ID <code>{card_id}</code>)\n"
        f"{line('💾', 'Файл', path_str or 'ошибка загрузки')}",
        reply_markup=get_main_reply_kb(),
    )
    await state.clear()


@router.callback_query(F.data.startswith("delete_card:"))
async def delete_card_cmd(call: CallbackQuery):
    if not await _require_admin(call):
        return
    card_id = int(call.data.split(":")[1])
    async with get_db() as db:
        await db.execute("DELETE FROM inventory WHERE card_id = ?", (card_id,))
        await db.execute("DELETE FROM cards WHERE id = ?", (card_id,))
    await call.message.answer("🗑 Карточка удалена", reply_markup=get_main_reply_kb())
    await call.answer()


@router.callback_query(F.data.startswith("edit_name:"))
async def edit_card_name_start(call: CallbackQuery, state: FSMContext):
    if not await _require_admin(call):
        return
    await state.update_data(card_id=int(call.data.split(":")[1]))
    await state.set_state(EditCardSG.new_name)
    await call.message.answer(
        "✍️ Введите новое название:",
        reply_markup=get_main_reply_kb(fsm_active=True),
    )
    await call.answer()


@router.message(EditCardSG.new_name, F.text)
async def edit_card_name_save(message: Message, state: FSMContext):
    if not await _require_admin(message):
        await state.clear()
        return
    if message.text.strip() in ("❌ Отмена", "/cancel"):
        return
    data = await state.get_data()
    new_name = message.text[:64]
    async with get_db() as db:
        await db.execute(
            "UPDATE cards SET name = ? WHERE id = ?", (new_name, data["card_id"])
        )
    await message.answer(
        f"✅ Название изменено: {esc(new_name)}", reply_markup=get_main_reply_kb()
    )
    await state.clear()


@router.callback_query(F.data.startswith("edit_rarity:"))
async def edit_card_rarity_start(call: CallbackQuery):
    if not await _require_admin(call):
        return
    card_id = int(call.data.split(":")[1])
    b = InlineKeyboardBuilder()
    for key, info in RARITIES.items():
        b.button(
            text=f"{info['icon']} {info['name']}",
            callback_data=AdminRarityCallback(card_id=card_id, rarity=key).pack(),
        )
    b.button(text="‹ Назад", callback_data=AdminCardManageCallback(card_id=card_id).pack())
    b.adjust(1)
    await call.message.answer("🎲 Выберите новую редкость:", reply_markup=b.as_markup())
    await call.answer()


@router.callback_query(AdminRarityCallback.filter())
async def edit_card_rarity_save(call: CallbackQuery, callback_data: AdminRarityCallback):
    if not await _require_admin(call):
        return
    async with get_db() as db:
        await db.execute(
            "UPDATE cards SET rarity = ? WHERE id = ?",
            (callback_data.rarity, callback_data.card_id),
        )
    await call.message.answer(
        f"✅ Редкость: {RARITIES[callback_data.rarity]['name']}",
        reply_markup=get_main_reply_kb(),
    )
    await call.answer()


# ── USERS (сокращённо, логика та же) ──
async def build_user_filter_menu() -> tuple[str, InlineKeyboardMarkup]:
    async with get_db() as db:
        cur = await db.execute("SELECT role, COUNT(*) FROM users GROUP BY role")
        counts = dict(await cur.fetchall())
        cur = await db.execute("SELECT COUNT(*) FROM users")
        total = (await cur.fetchone())[0]

    text = (
        f"👥 <b>Пользователи</b>\n"
        f"{sep()}\n\n"
        f"Всего: <b>{fmt_num(total)}</b>\n\n"
        f"Выберите роль:"
    )
    b = InlineKeyboardBuilder()
    b.button(
        text=f"📦 Все ({fmt_num(total)})",
        callback_data=AdminUserFilterCallback(role="all").pack(),
    )
    for key, info in ROLES.items():
        cnt = counts.get(key, 0)
        b.button(
            text=f"{info['icon']} {info['name']} ({fmt_num(cnt)})",
            callback_data=AdminUserFilterCallback(role=key).pack(),
        )
    b.button(text="‹ В меню", callback_data="admin_main")
    b.adjust(1)
    return text, b.as_markup()


async def build_admin_users_page(page: int = 0, filter_role: str = "all"):
    async with get_db() as db:
        if filter_role == "all":
            cur = await db.execute("SELECT COUNT(*) FROM users")
        else:
            cur = await db.execute(
                "SELECT COUNT(*) FROM users WHERE role = ?", (filter_role,)
            )
        total = (await cur.fetchone())[0]

        if total == 0:
            return (
                f"👥 Нет записей (фильтр: {filter_role}).",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text="‹ К ролям", callback_data="users_filter_menu"
                    )],
                    [InlineKeyboardButton(text="‹ В меню", callback_data="admin_main")],
                ]),
            )

        total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
        page = max(0, min(page, total_pages - 1))
        offset = page * PER_PAGE

        if filter_role == "all":
            cur = await db.execute(
                """SELECT user_id, nickname, coins, streak, role, registration
                   FROM users ORDER BY registration DESC LIMIT ? OFFSET ?""",
                (PER_PAGE, offset),
            )
        else:
            cur = await db.execute(
                """SELECT user_id, nickname, coins, streak, role, registration
                   FROM users WHERE role = ? ORDER BY registration DESC
                   LIMIT ? OFFSET ?""",
                (filter_role, PER_PAGE, offset),
            )
        users = await cur.fetchall()

        b = InlineKeyboardBuilder()
        lines = []
        for u in users:
            role_info = ROLES.get(u["role"] or "user", ROLES["user"])
            nick = display_name(u["nickname"], u["user_id"])
            lines.append(
                f"{role_info['icon']} <b>{esc(nick)}</b>\n"
                f"    <code>{u['user_id']}</code> · 🪙 {fmt_num(u['coins'] or 0)} · 🔥 {u['streak'] or 0}"
            )
            b.button(
                text=f"{role_info['icon']} {nick[:24]}",
                callback_data=AdminUserViewCallback(
                    user_id=u["user_id"], filter_role=filter_role
                ).pack(),
            )

        role_label = ROLES[filter_role]["name"] if filter_role != "all" else "Все"
        text = (
            f"👥 <b>Пользователи</b> · {role_label}\n"
            f"{sep()}\n"
            f"{line('📄', 'Страница', f'<b>{page + 1}</b> / {total_pages}')}\n"
            f"{line('📦', 'Найдено', f'<b>{fmt_num(total)}</b>')}\n\n"
            f"{bq(chr(10).join(lines))}"
        )

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(
                text="‹",
                callback_data=AdminUserPageCallback(
                    page=page - 1, filter_role=filter_role
                ).pack(),
            ))
        nav.append(InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}", callback_data="ignore"
        ))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(
                text="›",
                callback_data=AdminUserPageCallback(
                    page=page + 1, filter_role=filter_role
                ).pack(),
            ))

        b.adjust(1)
        b.row(*nav)
        b.row(InlineKeyboardButton(
            text="‹ К ролям", callback_data="users_filter_menu"
        ))
        b.row(InlineKeyboardButton(text="‹ В меню", callback_data="admin_main"))
        return text, b.as_markup()


@router.message(F.text == "👥 Пользователи")
@router.callback_query(F.data == "users_filter_menu")
async def users_filter_entry(event: Message | CallbackQuery):
    if not await _require_admin(event):
        return
    text, kb = await build_user_filter_menu()
    if isinstance(event, CallbackQuery):
        await safe_edit(event.message, text=text, reply_markup=kb)
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb)


@router.callback_query(AdminUserFilterCallback.filter())
async def users_filter_chosen(call: CallbackQuery, callback_data: AdminUserFilterCallback):
    if not await _require_admin(call):
        return
    text, kb = await build_admin_users_page(0, callback_data.role)
    await safe_edit(call.message, text=text, reply_markup=kb)
    await call.answer()


@router.callback_query(AdminUserPageCallback.filter())
async def admin_users_page(call: CallbackQuery, callback_data: AdminUserPageCallback):
    if not await _require_admin(call):
        return
    text, kb = await build_admin_users_page(callback_data.page, callback_data.filter_role)
    await safe_edit(call.message, text=text, reply_markup=kb)
    await call.answer()


@router.callback_query(AdminUserViewCallback.filter())
async def admin_user_view(call: CallbackQuery, callback_data: AdminUserViewCallback):
    if not await _require_admin(call):
        return

    user_id = callback_data.user_id
    back_role = callback_data.filter_role
    viewer_is_super = await is_superadmin(call.from_user.id)

    async with get_db() as db:
        cur = await db.execute(
            """SELECT u.*,
                      (SELECT COUNT(*) FROM inventory i WHERE i.user_id = u.user_id) AS cards_count
               FROM users u WHERE u.user_id = ?""",
            (user_id,),
        )
        user = await cur.fetchone()
        if not user:
            await call.answer("❌ Пользователь не найден", show_alert=True)
            return

    nick = display_name(user["nickname"], user_id)
    role = user["role"] or "user"
    reg = (
        datetime.fromtimestamp(user["registration"]).strftime("%d.%m.%Y %H:%M")
        if user["registration"]
        else "—"
    )

    body = "\n".join([
        line("🆔", "ID", f"<code>{user_id}</code>"),
        line("🎭", "Роль", role_display(role)),
        line("📅", "Регистрация", reg),
        line("🪙", "Монеты", f"<b>{fmt_num(user['coins'])}</b>"),
        line("🃏", "Карточек", f"<b>{fmt_num(user['cards_count'])}</b>"),
        line("🔥", "Стрик", f"<b>{user['streak'] or 0}</b>"),
    ])
    caption = f"👤 <b>{esc(nick)}</b>\n{sep(20)}\n\n{bq(body)}"

    b = InlineKeyboardBuilder()
    b.button(text="✏️ Ник", callback_data=AdminUserActionCallback(action="nick", user_id=user_id).pack())
    b.button(text="🪙 Монеты", callback_data=AdminUserActionCallback(action="coins", user_id=user_id).pack())
    b.button(text="⏱ Сброс CD", callback_data=AdminUserActionCallback(action="resetcd", user_id=user_id).pack())

    if role == "banned":
        b.button(text="✅ Разбан", callback_data=AdminUserActionCallback(action="unban", user_id=user_id).pack())
    elif role == "user":
        b.button(text="🚫 Бан", callback_data=AdminUserActionCallback(action="ban", user_id=user_id).pack())
        if viewer_is_super:
            b.button(text="🛡 Админ", callback_data=AdminUserActionCallback(action="make_admin", user_id=user_id).pack())
    elif role == "admin":
        if viewer_is_super:
            b.button(text="❌ Снять", callback_data=AdminUserActionCallback(action="unadmin", user_id=user_id).pack())
            b.button(text="👑 Super", callback_data=AdminUserActionCallback(action="make_super", user_id=user_id).pack())
            b.button(text="🚫 Бан", callback_data=AdminUserActionCallback(action="ban", user_id=user_id).pack())
    elif role == "superadmin":
        if viewer_is_super and user_id != call.from_user.id:
            b.button(text="❌ Снять", callback_data=AdminUserActionCallback(action="unadmin", user_id=user_id).pack())

    b.button(
        text="‹ К списку",
        callback_data=AdminUserPageCallback(page=0, filter_role=back_role).pack(),
    )
    b.adjust(1)

    photo = await get_cached_avatar(user_id)
    await safe_edit(
        call.message,
        text=caption if not photo else None,
        caption=caption if photo else None,
        media=photo,
        reply_markup=b.as_markup(),
    )
    await call.answer()


@router.callback_query(AdminUserActionCallback.filter())
async def admin_user_action(call: CallbackQuery, callback_data: AdminUserActionCallback):
    if not await _require_admin(call):
        return

    action = callback_data.action
    target_id = callback_data.user_id
    viewer_id = call.from_user.id
    viewer_is_super = await is_superadmin(viewer_id)
    target_role = await get_user_role(target_id)

    if action == "ban":
        if target_id == viewer_id:
            await call.answer("❌ Нельзя забанить себя", show_alert=True)
            return
        if target_role == "superadmin" and not viewer_is_super:
            await call.answer("❌ Недостаточно прав", show_alert=True)
            return
        await set_user_role(target_id, "banned")
        await call.message.answer(
            f"🚫 Пользователь <code>{target_id}</code> заблокирован",
            reply_markup=get_main_reply_kb(),
        )
        await call.answer("Готово")
        return

    if action == "unban":
        await set_user_role(target_id, "user")
        await call.message.answer(
            f"✅ Пользователь <code>{target_id}</code> разблокирован",
            reply_markup=get_main_reply_kb(),
        )
        await call.answer("Готово")
        return

    if action == "make_admin":
        if not viewer_is_super:
            await call.answer("❌ Только для superadmin", show_alert=True)
            return
        await set_user_role(target_id, "admin")
        await call.message.answer(
            f"🛡 <code>{target_id}</code> теперь администратор",
            reply_markup=get_main_reply_kb(),
        )
        await call.answer("Готово")
        return

    if action == "make_super":
        if not viewer_is_super:
            await call.answer("❌ Только для superadmin", show_alert=True)
            return
        await set_user_role(target_id, "superadmin")
        await call.message.answer(
            f"👑 <code>{target_id}</code> теперь главный администратор",
            reply_markup=get_main_reply_kb(),
        )
        await call.answer("Готово")
        return

    if action == "unadmin":
        if not viewer_is_super:
            await call.answer("❌ Только для superadmin", show_alert=True)
            return
        if target_id == viewer_id:
            await call.answer("❌ Нельзя снять права с себя", show_alert=True)
            return
        await set_user_role(target_id, "user")
        await call.message.answer(
            f"✅ Права сняты с <code>{target_id}</code>",
            reply_markup=get_main_reply_kb(),
        )
        await call.answer("Готово")
        return

    if action == "nick":
        await call.message.answer(f"✏️ /setnick {target_id} НовыйНик")
        await call.answer()
    elif action == "coins":
        await call.message.answer(f"🪙 /setcoins {target_id} 1000")
        await call.answer()
    elif action == "resetcd":
        async with get_db() as db:
            await db.execute(
                "UPDATE users SET last_claim = 0 WHERE user_id = ?", (target_id,)
            )
        await call.message.answer(
            f"⏱ Кулдаун сброшен для <code>{target_id}</code>",
            reply_markup=get_main_reply_kb(),
        )
        await call.answer("Готово")
    else:
        await call.answer()


# ── Commands ──
@router.message(Command("setnick"), admin_filter)
async def admin_setnick(message: Message, command: CommandObject):
    args = (command.args or "").strip().split()
    if len(args) < 2:
        await message.reply("✏️ /setnick USERID НовыйНик")
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await message.reply("❌ USERID должен быть числом")
        return

    raw = " ".join(args[1:]).strip()
    if not (2 <= len(raw) <= 32) or URL_RE.search(raw) or not NICKNAME_RE.match(raw):
        await message.reply("❌ Ник невалиден (2–32 символа, без ссылок)")
        return

    async with get_db() as db:
        cur = await db.execute(
            "SELECT nickname FROM users WHERE user_id = ?", (target_id,)
        )
        row = await cur.fetchone()
        if not row:
            await message.reply("❌ Пользователь не найден")
            return
        old = row["nickname"]
        await db.execute(
            "UPDATE users SET nickname = ? WHERE user_id = ?", (raw, target_id)
        )
    await message.reply(f"✅ {esc(old or '—')} → {esc(raw)}")


@router.message(Command("setadmin"), superadmin_filter)
async def set_admin_cmd(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg:
        await message.reply("✏️ /setadmin USERID")
        return
    try:
        target_id = int(arg)
    except ValueError:
        await message.reply("❌ USERID должен быть числом")
        return
    ok = await set_user_role(target_id, "admin")
    if not ok:
        await message.reply("❌ Пользователь не найден")
        return
    await message.reply(f"✅ Администратор: <code>{target_id}</code>")


@router.message(Command("unsetadmin"), superadmin_filter)
async def unset_admin_cmd(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg:
        await message.reply("✏️ /unsetadmin USERID")
        return
    try:
        target_id = int(arg)
    except ValueError:
        await message.reply("❌ USERID должен быть числом")
        return
    if target_id == message.from_user.id:
        await message.reply("⚠️ Нельзя снять права с себя")
        return
    ok = await set_user_role(target_id, "user")
    if not ok:
        await message.reply("❌ Пользователь не найден")
        return
    await message.reply(f"✅ Права сняты: <code>{target_id}</code>")


@router.message(Command("ban"), admin_filter)
async def ban_cmd(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg:
        await message.reply("✏️ /ban USERID")
        return
    try:
        target_id = int(arg)
    except ValueError:
        await message.reply("❌ USERID должен быть числом")
        return
    if target_id == message.from_user.id:
        await message.reply("❌ Нельзя забанить себя")
        return
    tr = await get_user_role(target_id)
    if tr in ("admin", "superadmin") and not await is_superadmin(message.from_user.id):
        await message.reply("❌ Недостаточно прав")
        return
    ok = await set_user_role(target_id, "banned")
    if not ok:
        await message.reply("❌ Пользователь не найден")
        return
    await message.reply(f"🚫 Заблокирован: <code>{target_id}</code>")


@router.message(Command("unban"), admin_filter)
async def unban_cmd(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg:
        await message.reply("✏️ /unban USERID")
        return
    try:
        target_id = int(arg)
    except ValueError:
        await message.reply("❌ USERID должен быть числом")
        return
    ok = await set_user_role(target_id, "user")
    if not ok:
        await message.reply("❌ Пользователь не найден")
        return
    await message.reply(f"✅ Разблокирован: <code>{target_id}</code>")


@router.message(Command("setcoins"), admin_filter)
async def admin_setcoins(message: Message, command: CommandObject):
    args = (command.args or "").strip().split()
    if not args:
        await message.reply("✏️ /setcoins [USERID] N")
        return

    target_id = message.from_user.id
    if len(args) == 1:
        try:
            coins = int(args[0])
        except ValueError:
            await message.reply("❌ Нужно число")
            return
    elif len(args) == 2:
        try:
            target_id = int(args[0])
            coins = int(args[1])
        except ValueError:
            await message.reply("❌ Нужны числа")
            return
    else:
        await message.reply("❌ Лишние аргументы")
        return

    if coins < 0:
        await message.reply("❌ Значение не может быть отрицательным")
        return

    async with get_db() as db:
        cur = await db.execute(
            "SELECT coins FROM users WHERE user_id = ?", (target_id,)
        )
        row = await cur.fetchone()
        if not row:
            await message.reply("❌ Пользователь не найден")
            return
        old = row["coins"] or 0
        await db.execute(
            "UPDATE users SET coins = ? WHERE user_id = ?", (coins, target_id)
        )

    body = "\n".join([
        line("🪙", "Было", fmt_num(old)),
        line("🪙", "Стало", f"<b>{fmt_num(coins)}</b>"),
    ])
    await message.reply(f"✅ Монеты <code>{target_id}</code>\n\n{bq(body)}")


@router.message(Command("resetcd"), admin_filter)
async def admin_resetcd(message: Message, command: CommandObject):
    args = (command.args or "").strip().split()
    target_id = message.from_user.id
    if args:
        try:
            target_id = int(args[0])
        except ValueError:
            await message.reply("❌ USERID должен быть числом")
            return

    async with get_db() as db:
        cur = await db.execute(
            "SELECT user_id FROM users WHERE user_id = ?", (target_id,)
        )
        if not await cur.fetchone():
            await message.reply("❌ Пользователь не найден")
            return
        await db.execute(
            "UPDATE users SET last_claim = 0 WHERE user_id = ?", (target_id,)
        )
    await message.reply(f"✅ Кулдаун сброшен: <code>{target_id}</code>")


@router.message(Command("delcard"), admin_filter)
async def admin_delcard(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg:
        await message.reply("✏️ /delcard ID")
        return
    try:
        card_id = int(arg)
    except ValueError:
        await message.reply("❌ ID должен быть числом")
        return

    async with get_db() as db:
        cur = await db.execute(
            "SELECT name FROM cards WHERE id = ?", (card_id,)
        )
        card = await cur.fetchone()
        if not card:
            await message.reply("❌ Карточка не найдена")
            return
        await db.execute("DELETE FROM inventory WHERE card_id = ?", (card_id,))
        await db.execute("DELETE FROM cards WHERE id = ?", (card_id,))
    await message.reply(f"🗑 Удалена: {esc(card['name'])} (#{card_id})")


# ── STATS / TOP / LOGS (упрощённо) ──
async def _stats_overview() -> str:
    async with get_db() as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        total_users = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM users WHERE role = 'banned'")
        banned = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT COUNT(*) FROM users WHERE role IN ('admin','superadmin')"
        )
        staff = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM cards")
        total_cards = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COALESCE(SUM(coins),0) FROM users")
        total_coins = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM inventory")
        owned = (await cur.fetchone())[0]
        cur = await db.execute("SELECT rarity, COUNT(*) FROM cards GROUP BY rarity")
        by_r = dict(await cur.fetchall())

    rarity_lines = "\n".join(
        line(v["icon"], v["name"], fmt_num(by_r.get(k, 0)))
        for k, v in RARITIES.items()
    )
    return (
        f"📊 <b>Обзор</b>\n{sep(20)}\n\n"
        f"{bq(line('👥', 'Пользователей', f'<b>{fmt_num(total_users)}</b>') + chr(10) + line('🛡', 'Админов', f'<b>{fmt_num(staff)}</b>') + chr(10) + line('🚫', 'Забанено', f'<b>{fmt_num(banned)}</b>'))}\n\n"
        f"{bq(line('🃏', 'Карточек', f'<b>{fmt_num(total_cards)}</b>') + chr(10) + rarity_lines)}\n\n"
        f"{bq(line('🪙', 'Монет в игре', f'<b>{fmt_num(total_coins)}</b>') + chr(10) + line('📦', 'В коллекциях', f'<b>{fmt_num(owned)}</b>'))}"
    )


def _stats_nav(section: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📊 Обзор", callback_data=AdminStatsCallback(section="overview").pack())
    b.button(text="‹ В меню", callback_data="admin_main")
    b.adjust(1)
    return b.as_markup()


@router.message(Command("stats"), admin_filter)
@router.message(F.text == "📊 Статистика")
async def admin_stats(message: Message):
    if not await _require_admin(message):
        return
    try:
        text = await _stats_overview()
        await message.reply(text, reply_markup=_stats_nav("overview"))
    except Exception as e:
        logger.error("stats: %s", e)
        await message.reply("❌ Ошибка статистики")


@router.callback_query(AdminStatsCallback.filter())
async def admin_stats_section(call: CallbackQuery, callback_data: AdminStatsCallback):
    if not await _require_admin(call):
        return
    text = await _stats_overview()
    await safe_edit(call.message, text=text, reply_markup=_stats_nav(callback_data.section))
    await call.answer()


@router.message(Command("top"), admin_filter)
@router.message(F.text == "🏆 Топ")
async def admin_top(message: Message):
    if not await _require_admin(message):
        return
    async with get_db() as db:
        cur = await db.execute(
            """SELECT user_id, nickname, coins,
                      (SELECT COUNT(*) FROM inventory i WHERE i.user_id = u.user_id) AS cards
               FROM users u WHERE role != 'banned' ORDER BY coins DESC LIMIT 10"""
        )
        by_coins = await cur.fetchall()

    def _fmt(rows, key, emoji):
        if not rows:
            return "Пусто"
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, r in enumerate(rows):
            medal = medals[i] if i < 3 else f"{i + 1}."
            nick = display_name(r["nickname"], r["user_id"])
            lines.append(f"{medal} {esc(nick)} — {emoji} {fmt_num(r[key] or 0)}")
        return "\n".join(lines)

    text = (
        f"🏆 <b>Топ</b>\n{sep(20)}\n\n"
        f"<b>🪙 По монетам</b>\n{bq(_fmt(by_coins, 'coins', '🪙'))}"
    )
    await message.reply(text, reply_markup=get_main_reply_kb())


@router.message(F.text == "📥 Логи")
@router.message(Command("logs"), admin_filter)
async def admin_logs(message: Message):
    if not await _require_admin(message):
        return
    files = [("admin_bot.log", ADMIN_LOG_PATH), ("bot.log", USER_LOG_PATH)]
    sent = 0
    for name, path in files:
        p = Path(path)
        if not p.is_file():
            await message.answer(f"⚠️ Файл не найден: <code>{esc(path)}</code>")
            continue
        try:
            size = p.stat().st_size
            if size == 0:
                await message.answer(f"ℹ️ {name} пуст")
                continue
            if size > 45 * 1024 * 1024:
                await message.answer(f"⚠️ {name} слишком большой")
                continue
            doc = FSInputFile(p, filename=name)
            await message.answer_document(
                document=doc,
                caption=f"📄 <b>{esc(name)}</b> · {fmt_num(size)} байт",
            )
            sent += 1
        except Exception as e:
            await message.answer(f"❌ {name}: {esc(str(e))}")
    if sent == 0:
        await message.answer("Нет доступных логов.")


# ── Test / maintenance commands ──
@router.message(Command("rename_rarities"), admin_filter)
async def cmd_rename_rarities(message: Message):
    """Переименовать старые ключи редкости (epic→uncommon, mythical→valuable)."""
    if not await _require_admin(message):
        return
    moved = 0
    async with get_db() as db:
        for old, new in OLD_RARITY_MAP.items():
            if old == new:
                continue
            cur = await db.execute(
                "UPDATE cards SET rarity = ? WHERE rarity = ?", (new, old)
            )
            moved += cur.rowcount or 0
    await message.reply(
        f"✅ Переименовано карточек: <b>{moved}</b>\n"
        f"epic→uncommon, mythical→valuable"
    )


@router.message(Command("balance_rarities"), admin_filter)
async def cmd_balance_rarities(message: Message):
    """
    Стабильный пересчёт: веса из БД → target counts →
    итеративно переносим excess → deficit (не только соседи).
    """
    if not await _require_admin(message):
        return
    rarities = await _load_rarities_db()
    global RARITIES
    RARITIES = rarities
    counts = await get_rarity_counts()
    # только известные ключи
    order = [k for k in RARITY_ORDER if k in rarities]
    total = sum(counts.get(k, 0) for k in order)
    if total < 6:
        await message.reply("Мало карточек для балансировки (нужно ≥ 6).")
        return

    weights = {k: max(0, int(rarities[k]["weight"])) for k in order}
    wsum = sum(weights.values()) or 1
    targets = {k: int(total * weights[k] / wsum) for k in order}
    # раздать остаток по наибольшим дробным частям
    remainder = total - sum(targets.values())
    fracs = sorted(
        order,
        key=lambda k: (total * weights[k] / wsum) - targets[k],
        reverse=True,
    )
    for i in range(remainder):
        targets[fracs[i % len(fracs)]] += 1

    moves: list[str] = []
    async with get_db() as db:
        # до 5 проходов
        for _pass in range(5):
            counts = {
                k: (
                    await (
                        await db.execute(
                            "SELECT COUNT(*) FROM cards WHERE rarity = ?", (k,)
                        )
                    ).fetchone()
                )[0]
                for k in order
            }
            over = [k for k in order if counts[k] > targets[k]]
            under = [k for k in order if counts[k] < targets[k]]
            if not over or not under:
                break
            progressed = False
            for src in over:
                excess = counts[src] - targets[src]
                if excess <= 0:
                    continue
                for dst in under:
                    need = targets[dst] - counts[dst]
                    if need <= 0:
                        continue
                    take = min(excess, need)
                    cur = await db.execute(
                        "SELECT id FROM cards WHERE rarity = ? ORDER BY id LIMIT ?",
                        (src, take),
                    )
                    ids = [r[0] for r in await cur.fetchall()]
                    if not ids:
                        continue
                    for cid in ids:
                        await db.execute(
                            "UPDATE cards SET rarity = ? WHERE id = ?", (dst, cid)
                        )
                        moves.append(f"#{cid}: {src} → {dst}")
                    counts[src] -= len(ids)
                    counts[dst] += len(ids)
                    excess -= len(ids)
                    progressed = True
                    if excess <= 0:
                        break
            if not progressed:
                break
        await db.commit()

    if not moves:
        await message.reply("✅ Баланс уже близок к целевому. Переносов нет.")
        return

    preview = "\n".join(moves[:30])
    more = f"\n… и ещё {len(moves) - 30}" if len(moves) > 30 else ""
    await message.reply(
        f"✅ Перенесено: <b>{len(moves)}</b>\n\n{bq(preview + more)}"
    )


async def _user_bot_fallback() -> Optional[Bot]:
    """Bot с USER_BOT_TOKEN — file_id обычно принадлежат user-боту."""
    if not USER_BOT_TOKEN:
        return None
    try:
        return Bot(token=USER_BOT_TOKEN)
    except Exception as e:
        logger.error("user bot fallback: %s", e)
        return None


@router.message(Command("frame_all"), admin_filter)
async def cmd_frame_all(message: Message):
    """Переналожить цветные рамки на все фото по текущей редкости."""
    if not await _require_admin(message):
        return
    if not reframe_all_photos:
        await message.reply("❌ card_photos.reframe_all_photos недоступен (нужен Pillow)")
        return
    status = await message.reply("⏳ Накладываю рамки (admin → user token)…")
    user_bot = await _user_bot_fallback()
    try:
        async with get_db() as db:
            ok, fail = await reframe_all_photos(
                message.bot, db, fallback_bot=user_bot
            )
        extra = "" if user_bot else "\n⚠️ BOT_TOKEN не задан — fallback user недоступен"
        await status.edit_text(
            f"✅ Рамки\n\n{bq(line('✓', 'Успешно', ok) + chr(10) + line('✗', 'Ошибки', fail))}{extra}"
        )
    except Exception as e:
        logger.error("frame_all: %s", e)
        await status.edit_text(f"❌ {esc(str(e))}")
    finally:
        if user_bot:
            await user_bot.session.close()


@router.message(Command("migrate_uuid"), admin_filter)
async def cmd_migrate_uuid(message: Message):
    """Переименовать файлы фото в UUID-имена."""
    if not await _require_admin(message):
        return
    if not migrate_to_uuid_names:
        await message.reply("❌ migrate_to_uuid_names недоступен")
        return
    status = await message.reply("⏳ Переименовываю в UUID…")
    try:
        async with get_db() as db:
            ok, fail = await migrate_to_uuid_names(db)
        await status.edit_text(
            f"✅ UUID-миграция\n\n{bq(line('✓', 'Ок', ok) + chr(10) + line('✗', 'Ошибки', fail))}"
        )
    except Exception as e:
        await status.edit_text(f"❌ {esc(str(e))}")


@router.message(Command("migrate_photos"), admin_filter)
async def cmd_migrate_photos(message: Message):
    if not migrate_all_card_photos:
        await message.reply("❌ Модуль card_photos.py не найден")
        return
    status = await message.reply("⏳ Скачиваю фото (admin → user token)…")
    user_bot = await _user_bot_fallback()
    try:
        async with get_db() as db:
            ok, fail = await migrate_all_card_photos(
                message.bot, db, fallback_bot=user_bot
            )
        extra = "" if user_bot else "\n⚠️ BOT_TOKEN не задан — fallback user недоступен"
        await status.edit_text(
            f"✅ Миграция\n\n{bq(line('✓', 'Успешно', ok) + chr(10) + line('✗', 'Ошибки', fail))}{extra}"
        )
    except Exception as e:
        await status.edit_text(f"❌ {esc(str(e))}")
    finally:
        if user_bot:
            await user_bot.session.close()


LEGACY_TABLES = (
    "gifts",
    "market_listings",
    "promo_activations",
    "promo_codes",
    "trades",
    "transactions",
    "user_achievements",
    "duels",
)

GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")


async def gemini_suggest_name(hint: str = "", old_name: str = "") -> str:
    """Одно лаконичное название карточки через Gemini REST API."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY не задан в .env")
    import json
    import urllib.request
    import urllib.error

    prompt = (
        "Придумай одно короткое красивое название для игровой карточки персонажа "
        "(1–4 слова, на русском, без кавычек и пояснений). "
    )
    if old_name:
        prompt += f"Текущее название: «{old_name}». Предложи другое, лучше. "
    if hint:
        prompt += f"Контекст/подсказка: {hint}. "
    prompt += "Ответь только названием."

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.9, "maxOutputTokens": 32},
    }).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err = e.read().decode()[:300]
        if e.code in (429, 403) or "quota" in err.lower() or "RESOURCE_EXHAUSTED" in err:
            raise RuntimeError(f"Квота Gemini исчерпана / доступ запрещён: {err}")
        raise RuntimeError(f"Gemini HTTP {e.code}: {err}")
    except Exception as e:
        raise RuntimeError(f"Gemini недоступен: {e}")

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        # одна строка, без кавычек
        text = text.split("\n")[0].strip().strip('"«»\'')
        return text[:64] or "Без названия"
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"Неожиданный ответ Gemini: {e}")


@router.message(Command("revision"), admin_filter)
async def cmd_revision(message: Message):
    """Сводка ревизии + мягкие миграции конфига."""
    if not await _require_admin(message):
        return
    from game_config import (
        APP_REVISION,
        APP_REVISION_NOTES,
        DEFAULT_RARITIES,
        NICKNAME_COST,
        DICE_VALUE_MAP,
        DICE_MIN_BALANCE,
        ensure_config_tables,
        reset_rarities_to_default,
    )

    lines = [
        f"<b>Ревизия {APP_REVISION}</b>",
        esc(APP_REVISION_NOTES),
        "",
    ]
    async with get_db() as db:
        await ensure_config_tables(db)
        cur = await db.execute("SELECT COUNT(*) FROM cards")
        n_cards = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM users")
        n_users = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = {r[0] for r in await cur.fetchall()}
        legacy = [t for t in LEGACY_TABLES if t in tables]
        # rarity drift vs default
        rar = await load_rarities(db) if False else None
    try:
        from game_config import load_rarities as _lr
        async with get_db() as db:
            rar = await _lr(db)
    except Exception:
        rar = dict(DEFAULT_RARITIES)

    drift = []
    for k, d in DEFAULT_RARITIES.items():
        cur_w = rar.get(k, {}).get("weight")
        cur_r = rar.get(k, {}).get("reward")
        if cur_w != d["weight"] or cur_r != d["reward"]:
            drift.append(
                f"{d['icon']} {k}: БД {cur_w}/{cur_r} → дефолт {d['weight']}/{d['reward']}"
            )

    lines.append(f"👥 users: {n_users} · 🃏 cards: {n_cards}")
    lines.append(f"🪙 nickname: {NICKNAME_COST} · dice min: {DICE_MIN_BALANCE}")
    lines.append(
        "🎲 dice: " + ", ".join(f"{k}:{v:+d}" for k, v in DICE_VALUE_MAP.items())
    )
    lines.append("")
    lines.append("<b>Дефолт редкостей</b>")
    for k, d in DEFAULT_RARITIES.items():
        lines.append(
            f"{d['icon']} {d['name']}: weight {d['weight']} · reward {d['reward']:+d}"
        )
    if drift:
        lines.append("")
        lines.append("⚠️ БД отличается от дефолта:")
        lines.extend(drift)
        lines.append("→ /resetrarities чтобы применить")
    else:
        lines.append("")
        lines.append("✅ rarity_config = дефолт")
    if legacy:
        lines.append(f"⚠️ legacy tables: {', '.join(legacy)} → /drop_legacy")
    else:
        lines.append("✅ legacy-таблиц нет")

    lines.append("")
    lines.append(
        "Рекомендуемый порядок: /drop_legacy → /resetrarities → "
        "/balance_rarities → /redraw_cards → /check"
    )
    await message.reply("\n".join(lines))
    logger.info("revision shown to %s", message.from_user.id)


@router.message(Command("drop_legacy"), admin_filter)
async def cmd_drop_legacy(message: Message):
    """Удалить устаревшие таблицы, не используемые кодом."""
    if not await _require_admin(message):
        return
    if not await is_superadmin(message.from_user.id):
        await message.reply("❌ Только superadmin")
        return
    dropped, kept = [], []
    async with get_db() as db:
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        existing = {r[0] for r in await cur.fetchall()}
        for t in LEGACY_TABLES:
            if t not in existing:
                kept.append(f"• {t}: нет")
                continue
            try:
                await db.execute(f"DROP TABLE IF EXISTS {t}")
                dropped.append(t)
            except Exception as e:
                kept.append(f"• {t}: ошибка {e}")
        await db.commit()
    logger.info("drop_legacy: %s", dropped)
    await message.reply(
        f"🗑 Удалено: <b>{', '.join(dropped) or '—'}</b>\n"
        + (bq("\n".join(kept)) if kept else "")
    )


@router.message(Command("ai"), admin_filter)
async def cmd_ai_name(message: Message, command: CommandObject):
    """ /ai [подсказка] — предложить название карточки (Gemini). """
    if not await _require_admin(message):
        return
    hint = (command.args or "").strip()
    status = await message.reply("✨ Gemini думает…")
    try:
        name = await gemini_suggest_name(hint=hint)
        await status.edit_text(
            f"✨ Предложение: <b>{esc(name)}</b>\n\n"
            f"Можно использовать при добавлении/редактировании карточки."
        )
    except Exception as e:
        await status.edit_text(f"❌ {esc(str(e))}")


@router.callback_query(F.data.startswith("ai_name:"))
async def cb_ai_name(call: CallbackQuery):
    if not await _require_admin(call):
        return
    card_id = int(call.data.split(":")[1])
    async with get_db() as db:
        cur = await db.execute(
            "SELECT name FROM cards WHERE id = ?", (card_id,)
        )
        row = await cur.fetchone()
    if not row:
        await call.answer("Нет карточки", show_alert=True)
        return
    await call.answer("Gemini…")
    try:
        new_name = await gemini_suggest_name(old_name=row[0])
        async with get_db() as db:
            await db.execute(
                "UPDATE cards SET name = ? WHERE id = ?", (new_name, card_id)
            )
        await call.message.answer(
            f"✨ #{card_id}: <b>{esc(row[0])}</b> → <b>{esc(new_name)}</b>"
        )
    except Exception as e:
        await call.message.answer(f"❌ {esc(str(e))}")


@router.message(Command("renamecards"), admin_filter)
async def cmd_renamecards(message: Message):
    """Массовое переименование через Gemini (с бэкапом БД)."""
    if not await _require_admin(message):
        return
    if not await is_superadmin(message.from_user.id):
        await message.reply("❌ Только superadmin")
        return
    if not GEMINI_API_KEY:
        await message.reply("❌ GEMINI_API_KEY не задан")
        return

    # бэкап
    import shutil
    from datetime import datetime as dt
    src = Path(DB_NAME)
    backup_dir = src.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    bak = backup_dir / f"pre_rename_{dt.now().strftime('%Y%m%d_%H%M%S')}.db"
    try:
        async with get_db() as db:
            await db.execute("PRAGMA wal_checkpoint(FULL);")
        shutil.copy2(src, bak)
        await message.answer_document(
            FSInputFile(bak, filename=bak.name),
            caption="💾 Бэкап перед /renamecards",
        )
    except Exception as e:
        await message.reply(f"❌ Бэкап не удался: {esc(str(e))}")
        return

    async with get_db() as db:
        cur = await db.execute("SELECT id, name FROM cards ORDER BY id")
        rows = await cur.fetchall()

    await message.reply(
        f"⏳ Переименование {len(rows)} карточек…\n"
        f"Остановка при ошибке квоты/API."
    )
    ok = fail = 0
    for row in rows:
        cid, old = row[0], row[1]
        try:
            new_name = await gemini_suggest_name(old_name=old)
            async with get_db() as db:
                await db.execute(
                    "UPDATE cards SET name = ? WHERE id = ?", (new_name, cid)
                )
            ok += 1
            await message.answer(
                f"Карточка ID {cid} «{esc(old)}» переименована: <b>{esc(new_name)}</b>"
            )
            await asyncio.sleep(1.2)
        except Exception as e:
            fail += 1
            await message.reply(
                f"⏹ Остановка на #{cid}: {esc(str(e))}\n"
                f"Успешно: {ok}, ошибок: {fail}\n"
                f"Откат: /import_db с файлом {bak.name}"
            )
            logger.error("renamecards stop: %s", e)
            return
    await message.reply(f"✅ Готово. Переименовано: {ok}")


@router.message(Command("check"), admin_filter)
async def cmd_check(message: Message):
    """Проверка БД, таблиц, API, фото карточек на диске."""
    if not await _require_admin(message):
        return

    lines: list[str] = []
    ok_all = True

    try:
        async with get_db() as db:
            cur = await db.execute("SELECT 1")
            await cur.fetchone()
        lines.append("✅ БД: доступна")
    except Exception as e:
        lines.append(f"❌ БД: {e}")
        ok_all = False
        await message.reply("\n".join(lines))
        return

    expected_tables = {
        "cards": {"id", "name", "rarity", "photo_id", "photo_path", "photo_uuid"},
        "users": {
            "user_id", "last_claim", "role", "nickname", "coins",
            "registration", "streak", "last_streak_date", "streak_bonus", "last_dice",
        },
        "inventory": {"user_id", "card_id", "claim_time"},
        "rarity_config": {"rarity_key", "weight", "reward"},
        "bot_settings": {"key", "value"},
    }

    async with get_db() as db:
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        existing = {r[0] for r in await cur.fetchall()}
        for t, cols in expected_tables.items():
            if t not in existing:
                lines.append(f"❌ Таблица {t}: отсутствует")
                ok_all = False
                continue
            cur = await db.execute(f"PRAGMA table_info({t})")
            have = {r[1] for r in await cur.fetchall()}
            missing = cols - have
            if missing:
                lines.append(f"⚠️ {t}: нет полей {', '.join(sorted(missing))}")
                ok_all = False
            else:
                lines.append(f"✅ Таблица {t}: OK")

        legacy_found = [t for t in LEGACY_TABLES if t in existing]
        if legacy_found:
            lines.append(
                f"⚠️ Лишние таблицы: {', '.join(legacy_found)} → /drop_legacy"
            )
            ok_all = False
        else:
            lines.append("✅ Лишних legacy-таблиц нет")

        # изображения карточек
        cur = await db.execute(
            "SELECT id, photo_path, photo_uuid, photo_id FROM cards"
        )
        cards = await cur.fetchall()
        missing_files = 0
        bad_size = 0
        no_path = 0
        for c in cards:
            path = c[1]
            if not path:
                no_path += 1
                continue
            fp = Path(path)
            if not fp.is_file():
                missing_files += 1
            elif fp.stat().st_size < 500:
                bad_size += 1
        lines.append(
            f"🖼 Карточки: {len(cards)} | без path: {no_path} | "
            f"файл отсутствует: {missing_files} | крошечные: {bad_size}"
        )
        if missing_files or bad_size:
            ok_all = False
            lines.append("  → /migrate_photos · /redraw_cards")

    try:
        from card_photos import ensure_photo_dir, _PHOTO_DIR, CARD_W, CARD_H
        ensure_photo_dir()
        n = len(list(_PHOTO_DIR.glob("*.jpg"))) if _PHOTO_DIR.exists() else 0
        lines.append(f"✅ Фото-папка: {_PHOTO_DIR} ({n} jpg, цель {CARD_W}×{CARD_H})")
    except Exception as e:
        lines.append(f"⚠️ Фото-папка: {e}")

    try:
        from PIL import Image  # noqa: F401
        lines.append("✅ Pillow: установлен")
    except ImportError:
        lines.append("⚠️ Pillow: нет")
        ok_all = False

    if GEMINI_API_KEY:
        lines.append(f"✅ Gemini: ключ задан (модель {GEMINI_MODEL})")
    else:
        lines.append("ℹ️ GEMINI_API_KEY не задан")

    if API_BASE:
        try:
            import urllib.request
            req = urllib.request.Request(f"{API_BASE}/health", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = resp.read().decode()[:200]
            lines.append(f"✅ API /health: {body}")
        except Exception as e:
            lines.append(f"❌ API: {e}")
            ok_all = False
    else:
        lines.append("ℹ️ API_BASE не задан")

    if WEBAPP_URL:
        lines.append(f"ℹ️ WEBAPP_URL: {WEBAPP_URL}")
    if SUPERADMIN_IDS:
        lines.append(f"ℹ️ SUPERADMIN_IDS: {', '.join(map(str, SUPERADMIN_IDS))}")
    else:
        lines.append("⚠️ SUPERADMIN_IDS пуст")

    header = "✅ Всё в порядке" if ok_all else "⚠️ Есть замечания"
    await message.reply(f"<b>{header}</b>\n\n" + "\n".join(lines))


@router.message(Command("cache_avatars"), admin_filter)
async def cmd_cache_avatars(message: Message, command: CommandObject):
    if not await _require_admin(message):
        return
    if not USER_BOT_TOKEN:
        await message.reply("❌ BOT_TOKEN не задан")
        return
    offset = 0
    args = (command.args or "").strip().split()
    if args:
        try:
            offset = max(0, int(args[0]))
        except ValueError:
            await message.reply("✏️ /cache_avatars [offset]")
            return
    status = await message.reply(f"⏳ Кэш аватарок (offset={offset})…")
    async with get_db() as db:
        cur = await db.execute(
            "SELECT user_id FROM users ORDER BY registration DESC LIMIT ? OFFSET ?",
            (AVATAR_BATCH, offset),
        )
        user_ids = [r[0] for r in await cur.fetchall()]
    if not user_ids:
        await status.edit_text("Нет пользователей.")
        return
    user_bot = Bot(token=USER_BOT_TOKEN)
    stats = {"new": 0, "updated": 0, "unchanged": 0, "empty": 0, "error": 0}
    try:
        for i, uid in enumerate(user_ids):
            result = await cache_user_avatar(user_bot, uid)
            stats[result] = stats.get(result, 0) + 1
            if i < len(user_ids) - 1:
                delay = AVATAR_DELAY[0] + (AVATAR_DELAY[1] - AVATAR_DELAY[0]) * (i % 3) / 2
                await asyncio.sleep(delay)
    finally:
        await user_bot.session.close()
    body = "\n".join(f"{k}: {v}" for k, v in stats.items())
    await status.edit_text(
        f"✅ Кэш\n{bq(body)}\n\nСлед.: /cache_avatars {offset + len(user_ids)}"
    )


@router.message(Command("getfileid"), admin_filter, F.photo)
async def test_get_file_id(message: Message):
    file_id = message.photo[-1].file_id
    await message.reply(f"🆔 <code>{esc(file_id)}</code>")


# ── Rarity config / balance / userbot / backup ──
async def _load_rarities_db() -> dict:
    from game_config import load_rarities, ensure_config_tables
    async with get_db() as db:
        await ensure_config_tables(db)
        return await load_rarities(db)


@router.message(Command("checkbalance"), admin_filter)
async def cmd_checkbalance(message: Message):
    """Проверка сбалансированности редкостей, весов и наград."""
    if not await _require_admin(message):
        return
    rarities = await _load_rarities_db()
    counts = await get_rarity_counts()
    total = sum(counts.values()) or 1
    weight_sum = sum(r["weight"] for r in rarities.values())
    lines = [
        f"Всего карточек: <b>{total}</b>",
        f"Сумма весов: <b>{weight_sum}</b> (цель 100)",
        "",
    ]
    issues = []
    for key, info in rarities.items():
        cnt = counts.get(key, 0)
        share = 100.0 * cnt / total
        target = info["weight"]
        delta = share - target
        mark = "✓" if abs(delta) < 8 else ("↑" if delta > 0 else "↓")
        lines.append(
            f"{info['icon']} <b>{info['name']}</b>\n"
            f"    карт: {cnt} ({share:.0f}%) · вес {target}% · награда {info['reward']} 🪙 {mark}"
        )
        if abs(delta) >= 12:
            issues.append(f"{info['name']}: отклонение {delta:+.0f}%")
        if info["reward"] <= 0:
            issues.append(f"{info['name']}: награда ≤ 0")
    if weight_sum != 100:
        issues.append(f"Сумма весов {weight_sum} ≠ 100")
    # sanity: среди положительных награда не убывает
    prev_r = None
    for key in RARITY_ORDER:
        r = rarities[key]["reward"]
        if r < 0:
            continue
        if prev_r is not None and r < prev_r:
            issues.append(f"Награда {key} ({r}) меньше предыдущей ({prev_r})")
        prev_r = r

    text = "⚖ <b>Баланс редкостей</b>\n" + sep(20) + "\n\n" + bq("\n".join(lines))
    if issues:
        text += "\n\n⚠️ <b>Замечания</b>\n" + bq("\n".join(issues))
    else:
        text += "\n\n✅ Существенных отклонений нет."
    text += (
        "\n\n<code>/setrarity KEY weight N</code>\n"
        "<code>/setrarity KEY reward N</code>\n"
        "<code>/resetrarities</code> — дефолт"
    )
    await message.reply(text)


@router.message(Command("setrarity"), admin_filter)
async def cmd_setrarity(message: Message, command: CommandObject):
    """ /setrarity common weight 35  |  /setrarity legendary reward 250 """
    if not await _require_admin(message):
        return
    from game_config import set_rarity_params, DEFAULT_RARITIES
    args = (command.args or "").strip().split()
    if len(args) < 3:
        await message.reply(
            "✏️ /setrarity KEY weight|reward N\n"
            "Ключи: " + ", ".join(DEFAULT_RARITIES.keys())
        )
        return
    key, field, val_s = args[0].lower(), args[1].lower(), args[2]
    if key not in DEFAULT_RARITIES:
        await message.reply("❌ Неизвестный ключ редкости")
        return
    try:
        val = int(val_s)
    except ValueError:
        await message.reply("❌ N должно быть числом")
        return
    if field not in ("weight", "reward"):
        await message.reply("❌ field: weight или reward")
        return
    async with get_db() as db:
        ok = await set_rarity_params(
            db, key,
            weight=val if field == "weight" else None,
            reward=val if field == "reward" else None,
        )
    if not ok:
        await message.reply("❌ Не удалось сохранить")
        return
    # обновить кэш в процессе
    global RARITIES
    RARITIES = await _load_rarities_db()
    await message.reply(f"✅ {key}.{field} = {val}")


@router.message(Command("resetrarities"), admin_filter)
async def cmd_resetrarities(message: Message):
    if not await _require_admin(message):
        return
    from game_config import reset_rarities_to_default
    async with get_db() as db:
        await reset_rarities_to_default(db)
    global RARITIES
    RARITIES = await _load_rarities_db()
    await message.reply("✅ Редкости сброшены на сбалансированный дефолт")


@router.message(Command("remove_rare"), admin_filter)
async def cmd_remove_rare(message: Message):
    """
    Удалить редкость «Редкая» (rare):
    cards.rarity rare → uncommon, rarity_config, дефолтные веса, перерисовка.
    """
    if not await _require_admin(message):
        return
    from game_config import reset_rarities_to_default, DEFAULT_RARITIES

    status = await message.reply("⏳ Удаляю rare → uncommon, обновляю конфиг…")
    moved = 0
    async with get_db() as db:
        cur = await db.execute(
            "UPDATE cards SET rarity = 'uncommon' WHERE rarity = 'rare'"
        )
        moved = cur.rowcount or 0
        await db.execute("DELETE FROM rarity_config WHERE rarity_key = 'rare'")
        await reset_rarities_to_default(db)

    global RARITIES
    RARITIES = await _load_rarities_db()
    RARITY_ORDER[:] = list(RARITIES.keys())

    redraw_note = ""
    user_bot = await _user_bot_fallback()
    try:
        if reframe_all_photos:
            async with get_db() as db:
                ok, fail = await reframe_all_photos(
                    message.bot, db, fallback_bot=user_bot
                )
            redraw_note = f"\nПерерисовка: ✓{ok} / ✗{fail}"
    except Exception as e:
        redraw_note = f"\nПерерисовка: ошибка {e}"
    finally:
        if user_bot:
            await user_bot.session.close()

    await status.edit_text(
        f"✅ rare удалена\n"
        f"Перенесено карточек rare→uncommon: <b>{moved}</b>\n"
        f"Конфиг сброшен на дефолт (с negative, без rare)."
        f"{redraw_note}"
    )
    logger.info("remove_rare: moved=%s", moved)


@router.message(Command("clear_logs"), admin_filter)
async def cmd_clear_logs(message: Message):
    """Очистить admin_bot.log и bot.log (с опциональным бэкапом)."""
    if not await _require_admin(message):
        return
    import shutil
    from datetime import datetime as dt

    cleared = []
    for name, path in (("admin_bot.log", ADMIN_LOG_PATH), ("bot.log", USER_LOG_PATH)):
        p = Path(path)
        if not p.is_file():
            cleared.append(f"• {name}: нет файла")
            continue
        try:
            bak = p.with_suffix(p.suffix + f".bak_{dt.now().strftime('%Y%m%d_%H%M%S')}")
            shutil.copy2(p, bak)
            p.write_text("", encoding="utf-8")
            cleared.append(f"• {name}: очищен (бэкап {bak.name})")
            logger.info("clear_logs: %s → %s", path, bak)
        except Exception as e:
            cleared.append(f"• {name}: ошибка {e}")
    await message.reply("✅ Логи\n" + bq("\n".join(cleared)))


@router.message(Command("redraw_cards"), admin_filter)
async def cmd_redraw_cards(message: Message):
    """Пересчитать оформление всех карточек по текущей редкости (рамка + звёзды)."""
    if not await _require_admin(message):
        return
    if not reframe_all_photos:
        await message.reply("❌ reframe_all_photos недоступен")
        return
    status = await message.reply(
        "⏳ Перерисовка (скачивание admin→user token + рамка + звёзды)…"
    )
    user_bot = await _user_bot_fallback()
    try:
        async with get_db() as db:
            ok, fail = await reframe_all_photos(
                message.bot, db, fallback_bot=user_bot
            )
        extra = "" if user_bot else "\n⚠️ BOT_TOKEN не задан — fallback user недоступен"
        await status.edit_text(
            f"✅ Перерисовка\n\n{bq(line('✓', 'Ок', ok) + chr(10) + line('✗', 'Ошибки', fail))}{extra}"
        )
    except Exception as e:
        await status.edit_text(f"❌ {esc(str(e))}")
    finally:
        if user_bot:
            await user_bot.session.close()


@router.message(Command("recalc_balances"), admin_filter)
async def cmd_recalc_balances(message: Message):
    """
    Пересчитать монеты пользователей = сумма reward их карточек (по текущему rarity_config).
    Не учитывает стрик/кубик/ник — только «стоимость коллекции».
    """
    if not await _require_admin(message):
        return
    rarities = await _load_rarities_db()
    status = await message.reply("⏳ Пересчёт балансов по коллекциям…")
    updated = 0
    async with get_db() as db:
        cur = await db.execute("SELECT user_id FROM users")
        users = [r[0] for r in await cur.fetchall()]
        for uid in users:
            cur = await db.execute(
                """SELECT c.rarity, COUNT(*) FROM inventory i
                   JOIN cards c ON c.id = i.card_id
                   WHERE i.user_id = ? GROUP BY c.rarity""",
                (uid,),
            )
            rows = await cur.fetchall()
            total = 0
            for rarity, cnt in rows:
                reward = rarities.get(rarity, {}).get("reward", 0)
                total += reward * cnt
            await db.execute("UPDATE users SET coins = ? WHERE user_id = ?", (total, uid))
            updated += 1
    await status.edit_text(
        f"✅ Балансы пересчитаны для <b>{updated}</b> пользователей\n"
        f"(сумма наград за карточки в коллекции)"
    )


@router.message(Command("userbot"), admin_filter)
async def cmd_userbot(message: Message, command: CommandObject):
    """ /userbot on | off | status """
    if not await _require_admin(message):
        return
    from game_config import get_setting, set_setting, is_user_bot_enabled
    arg = (command.args or "").strip().lower()
    async with get_db() as db:
        if arg in ("on", "1", "enable", "вкл"):
            await set_setting(db, "user_bot_enabled", "1")
            await message.reply(
                "✅ User-бот <b>включён</b> в настройках.\n"
                "Перезапустите процесс (<code>python main.py</code>), чтобы поднять polling."
            )
        elif arg in ("off", "0", "disable", "выкл"):
            await set_setting(db, "user_bot_enabled", "0")
            await message.reply(
                "⏸ User-бот <b>выключен</b> в настройках.\n"
                "После перезапуска <code>main.py</code> polling user-бота не стартует. "
                "Админ-бот продолжит работать."
            )
        else:
            enabled = await is_user_bot_enabled(db)
            await message.reply(
                f"User-бот: <b>{'включён ✅' if enabled else 'выключен ⏸'}</b>\n"
                f"/userbot on · /userbot off"
            )


@router.message(Command("backup"), admin_filter)
async def cmd_backup(message: Message):
    """Ручной бэкап SQLite → файл в чат."""
    if not await _require_admin(message):
        return
    import shutil
    from datetime import datetime as dt
    src = Path(DB_NAME)
    if not src.is_file():
        await message.reply("❌ Файл БД не найден")
        return
    backup_dir = src.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.now().strftime("%Y%m%d_%H%M%S")
    dest = backup_dir / f"cards_game_{stamp}.db"
    try:
        # checkpoint WAL
        async with get_db() as db:
            await db.execute("PRAGMA wal_checkpoint(FULL);")
        shutil.copy2(src, dest)
        # также journal если есть
        size = dest.stat().st_size
        doc = FSInputFile(dest, filename=dest.name)
        await message.answer_document(
            document=doc,
            caption=f"💾 <b>Бэкап БД</b>\n{line('📦', 'Размер', fmt_num(size) + ' байт')}\n"
                    f"{line('📁', 'Путь', esc(str(dest)))}",
        )
    except Exception as e:
        await message.reply(f"❌ {esc(str(e))}")


@router.message(Command("export_db"), admin_filter)
async def cmd_export_db(message: Message):
    """Экспорт БД (то же, что backup, но имя export)."""
    if not await _require_admin(message):
        return
    import shutil
    from datetime import datetime as dt
    src = Path(DB_NAME)
    if not src.is_file():
        await message.reply("❌ БД не найдена")
        return
    async with get_db() as db:
        await db.execute("PRAGMA wal_checkpoint(FULL);")
    tmp = src.parent / f"export_{dt.now().strftime('%Y%m%d_%H%M%S')}.db"
    shutil.copy2(src, tmp)
    try:
        await message.answer_document(
            FSInputFile(tmp, filename=tmp.name),
            caption="📤 Экспорт БД",
        )
    finally:
        tmp.unlink(missing_ok=True)


@router.message(Command("import_db"), admin_filter, F.document)
async def cmd_import_db(message: Message):
    """Импорт: отправьте .db файлом с подписью /import_db (осторожно!)."""
    if not await _require_admin(message):
        return
    if not await is_superadmin(message.from_user.id):
        await message.reply("❌ Только superadmin")
        return
    doc = message.document
    if not doc or not (doc.file_name or "").endswith(".db"):
        await message.reply("❌ Нужен файл .db и команда /import_db в подписи или ответе")
        return
    status = await message.reply("⏳ Импорт БД…")
    try:
        # бэкап текущей
        import shutil
        from datetime import datetime as dt
        src = Path(DB_NAME)
        backup_dir = src.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        if src.is_file():
            shutil.copy2(src, backup_dir / f"pre_import_{dt.now().strftime('%Y%m%d_%H%M%S')}.db")
        tg_file = await message.bot.get_file(doc.file_id)
        dest = Path(DB_NAME)
        await message.bot.download_file(tg_file.file_path, destination=dest)
        await status.edit_text(
            "✅ БД импортирована. Перезапустите ботов.\n"
            "Предыдущая копия лежит в backups/."
        )
    except Exception as e:
        await status.edit_text(f"❌ {esc(str(e))}")


@router.message(Command("import_db"), admin_filter)
async def cmd_import_db_hint(message: Message):
    if not await _require_admin(message):
        return
    await message.reply(
        "📥 Отправьте файл <code>.db</code> с подписью <code>/import_db</code>\n"
        "Только superadmin. Перед импортом текущая БД сохранится в backups/."
    )


# ── HELP ──
HELP_PAGES = [
    {
        "title": "Роли",
        "body": (
            f"{line('👑', 'Superadmin', 'полный доступ')}\n"
            f"{line('🛡', 'Admin', 'карточки, пользователи, статистика')}\n\n"
            "Superadmin задаётся через SUPERADMIN_IDS в .env "
            "(если в БД ещё нет ни одного)."
        ),
    },
    {
        "title": "Карточки",
        "body": (
            "➕ Добавить — фото → название → редкость (с рекомендацией баланса)\n"
            "/addcard Название редкость + фото\n"
            "/delcard ID\n"
            "/migrate_photos · /frame_all · /migrate_uuid · /redraw_cards\n"
            "/rename_rarities · /balance_rarities · /checkbalance\n"
            "/setrarity KEY weight|reward N · /resetrarities"
        ),
    },
    {
        "title": "Пользователи",
        "body": (
            "/setnick USERID Ник\n"
            "/setcoins [USERID] N\n"
            "/resetcd [USERID]\n"
            "/ban · /unban · /setadmin · /unsetadmin\n"
            "/recalc_balances — монеты = сумма наград коллекции"
        ),
    },
    {
        "title": "Сервис",
        "body": (
            "/check · /checkbalance\n"
            "/userbot on|off|status\n"
            "/backup · /export_db · /import_db\n"
            "/logs · /cache_avatars · /stats · /top"
        ),
    },
]


def get_help_keyboard(page: int) -> InlineKeyboardMarkup:
    total = len(HELP_PAGES)
    page = max(0, min(page, total - 1))
    b = InlineKeyboardBuilder()
    for i, p in enumerate(HELP_PAGES):
        mark = "● " if i == page else ""
        b.button(
            text=f"{mark}{p['title'][:18]}",
            callback_data=AdminHelpCallback(page=i).pack(),
        )
    b.adjust(2)
    b.row(InlineKeyboardButton(text="‹ В меню", callback_data="admin_main"))
    return b.as_markup()


def build_help_text(page: int) -> str:
    page = max(0, min(page, len(HELP_PAGES) - 1))
    p = HELP_PAGES[page]
    return (
        f"🛠 <b>{p['title']}</b>\n"
        f"{sep(20)}\n\n"
        f"{p['body']}\n\n"
        f"<i>Раздел {page + 1} из {len(HELP_PAGES)}</i>"
    )


@router.message(Command("help"), admin_filter)
@router.message(F.text == "🛠 Справка")
async def admin_help(message: Message):
    if not await _require_admin(message):
        return
    await message.reply(build_help_text(0), reply_markup=get_help_keyboard(0))


@router.callback_query(AdminHelpCallback.filter())
async def admin_help_page(callback: CallbackQuery, callback_data: AdminHelpCallback):
    if not await _require_admin(callback):
        return
    text = build_help_text(callback_data.page)
    kb = get_help_keyboard(callback_data.page)
    await safe_edit(callback.message, text=text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "ignore")
async def ignore_cb(callback: CallbackQuery):
    await callback.answer()


# ── Startup ──
ADMIN_COMMANDS = [
    BotCommand(command="start", description="⚙️ Панель"),
    BotCommand(command="help", description="🛠 Справка"),
    BotCommand(command="stats", description="📊 Статистика"),
    BotCommand(command="top", description="🏆 Топ"),
    BotCommand(command="logs", description="📥 Логи"),
    BotCommand(command="check", description="🩺 Проверка системы"),
    BotCommand(command="addcard", description="➕ Карточка + фото"),
    BotCommand(command="delcard", description="🗑 Удалить карточку"),
    BotCommand(command="setcoins", description="🪙 Монеты"),
    BotCommand(command="setnick", description="✏️ Ник"),
    BotCommand(command="resetcd", description="⏱ Сброс CD"),
    BotCommand(command="ban", description="🚫 Бан"),
    BotCommand(command="unban", description="✅ Разбан"),
    BotCommand(command="setadmin", description="👑 Назначить админа"),
    BotCommand(command="unsetadmin", description="❌ Снять права"),
    BotCommand(command="migrate_photos", description="💾 Фото на диск"),
    BotCommand(command="frame_all", description="🖼 Рамки на все фото"),
    BotCommand(command="migrate_uuid", description="🔑 UUID-имена файлов"),
    BotCommand(command="rename_rarities", description="🏷 Старые→новые редкости"),
    BotCommand(command="balance_rarities", description="⚖ Баланс редкостей"),
    BotCommand(command="checkbalance", description="⚖ Проверка баланса редкостей"),
    BotCommand(command="setrarity", description="✏️ Вес/награда редкости"),
    BotCommand(command="resetrarities", description="↺ Дефолт редкостей"),
    BotCommand(command="remove_rare", description="🗑 Удалить rare + redraw"),
    BotCommand(command="redraw_cards", description="🖼 Перерисовка рамок"),
    BotCommand(command="recalc_balances", description="🪙 Пересчёт монет игроков"),
    BotCommand(command="userbot", description="⏯ Вкл/выкл user-бота"),
    BotCommand(command="backup", description="💾 Бэкап БД"),
    BotCommand(command="export_db", description="📤 Экспорт БД"),
    BotCommand(command="import_db", description="📥 Импорт БД"),
    BotCommand(command="clear_logs", description="🧹 Очистить логи"),
    BotCommand(command="drop_legacy", description="🗑 Удалить лишние таблицы"),
    BotCommand(command="ai", description="✨ Gemini: название"),
    BotCommand(command="renamecards", description="✨ Gemini: все названия"),
    BotCommand(command="revision", description="📋 Ревизия / миграции"),
    BotCommand(command="cache_avatars", description="🖼 Кэш аватарок"),
]


async def run_admin_bot(token: Optional[str] = None) -> None:
    tok = (token or ADMIN_BOT_TOKEN or "").strip()
    if not tok:
        logger.warning("ADMIN_BOT_TOKEN не задан — админ-бот не запущен")
        return

    os.makedirs(os.path.dirname(DB_NAME) or ".", exist_ok=True)
    try:
        Path(ADMIN_LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    AVATARS_DIR.mkdir(parents=True, exist_ok=True)

    await init_db_minimal()
    await ensure_superadmin_from_env()
    try:
        from game_config import ensure_config_tables, load_rarities
        async with get_db() as db:
            await ensure_config_tables(db)
            global RARITIES
            RARITIES = await load_rarities(db)
    except Exception as e:
        logger.warning("rarity config: %s", e)
    try:
        ensure_photo_dir()
    except Exception:
        pass

    bot = Bot(
        token=tok,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview=LinkPreviewOptions(is_disabled=True),
        ),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeDefault())
    me = await bot.get_me()
    async with get_db() as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        n_users = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM cards")
        n_cards = (await cur.fetchone())[0]
    logger.info("=" * 48)
    logger.info("🛡 Админ-бот запущен")
    logger.info("  username : @%s (id=%s)", me.username, me.id)
    logger.info("  DB       : %s", DB_NAME)
    logger.info("  log file : %s", ADMIN_LOG_PATH)
    logger.info("  users    : %s | cards: %s", n_users, n_cards)
    logger.info("  rarities : %s", ", ".join(RARITIES.keys()))
    logger.info("  BOT_TOKEN (user fallback): %s", "yes" if USER_BOT_TOKEN else "NO")
    logger.info("=" * 48)
    await dp.start_polling(bot)


async def main() -> None:
    if not ADMIN_BOT_TOKEN:
        raise SystemExit(
            "ADMIN_BOT_TOKEN не задан. Укажите токен админ-бота в .env."
        )
    await run_admin_bot(ADMIN_BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
