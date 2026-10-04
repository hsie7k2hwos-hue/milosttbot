"""
Админ-бот карточной игры (отдельный процесс, общая БД).

Запуск:
  ADMIN_BOT_TOKEN=... DB_NAME=/app/data/cards_game.db python admin_bot.py

Требования:
  - Пользовательский и админ-бот монтируют одну папку данных.
  - SQLite WAL позволяет одновременный доступ.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

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
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    LinkPreviewOptions,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Optional photo helpers (shared with user-bot)
# ---------------------------------------------------------------------------
try:
    from card_photos import (
        sync_card_photo,
        migrate_all_card_photos,
        ensure_photo_dir,
        get_card_photo_input,
        ensure_default_avatar,
        DEFAULT_AVATAR_PATH,
        DEFAULT_AVATAR_FILE_ID,
    )
except ImportError:
    sync_card_photo = None
    migrate_all_card_photos = None
    get_card_photo_input = None
    ensure_default_avatar = None
    DEFAULT_AVATAR_PATH = Path("/app/data/card_photos/default_avatar.jpg")
    DEFAULT_AVATAR_FILE_ID = (
        "AgACAgIAAxkBAAID12qql3EFpnb2HwTCE7Yn_Ri1TQsNAAKRIGsbfHlYSVUpxfU75O60AQADAgADeAADPQQ"
    )

    def ensure_photo_dir() -> None:
        pass


# ===========================================================================
# CONFIG
# ===========================================================================
load_dotenv()

ADMIN_BOT_TOKEN = (
    os.getenv("ADMIN_BOT_TOKEN") or os.getenv("BOT_TOKEN_ADMIN") or ""
).strip()

DB_NAME = os.getenv("DB_NAME", "/app/data/cards_game.db")
ADMIN_LOG_PATH = os.getenv("ADMIN_LOG_PATH", "/app/data/admin_bot.log")
USER_LOG_PATH = os.getenv("LOG_PATH", "/app/data/bot.log")
COOLDOWN_SECONDS = 3 * 3600

RARITIES = {
    "common":    {"icon": "⚪", "name": "Обычная",     "weight": 50, "reward": 10},
    "rare":      {"icon": "🔵", "name": "Редкая",      "weight": 20, "reward": 25},
    "epic":      {"icon": "🟣", "name": "Эпическая",   "weight": 15, "reward": 50},
    "mythical":  {"icon": "🔴", "name": "Мифическая",  "weight": 10, "reward": 75},
    "legendary": {"icon": "🟡", "name": "Легендарная", "weight": 5,  "reward": 100},
}

ROLES = {
    "user":       {"icon": "👤", "name": "Пользователь"},
    "admin":      {"icon": "🛡", "name": "Администратор"},
    "superadmin": {"icon": "👑", "name": "Главный администратор"},
    "banned":     {"icon": "🚫", "name": "Заблокирован"},
}

NICKNAME_RE = re.compile(r"^[\w\-. ]{2,32}$", re.UNICODE)
URL_RE = re.compile(r"(https?://|t\.me/|@\w+)", re.IGNORECASE)

# ===========================================================================
# LOGGING (исправлено: гарантированно пишем в файл)
# ===========================================================================
def _setup_logging() -> logging.Logger:
    log = logging.getLogger("admin_bot")
    log.setLevel(logging.INFO)
    log.handlers.clear()

    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")

    # Console
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(sh)

    # File
    try:
        Path(ADMIN_LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(ADMIN_LOG_PATH, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except Exception as exc:
        log.warning("Не удалось открыть файл логов %s: %s", ADMIN_LOG_PATH, exc)

    return log


logger = _setup_logging()


# ===========================================================================
# HELPERS
# ===========================================================================
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
    """Оборачивает текст в blockquote для красивого вывода."""
    return f"<blockquote>{text}</blockquote>"


def sep(width: int = 18) -> str:
    return "─" * width


# ===========================================================================
# CALLBACK DATA
# ===========================================================================
class AdminRarityCallback(CallbackData, prefix="ar"):
    card_id: int
    rarity: str


class AdminCardPageCallback(CallbackData, prefix="acp"):
    page: int


class AdminCardManageCallback(CallbackData, prefix="acm"):
    card_id: int


class AdminCardEditPhotoCallback(CallbackData, prefix="acep"):
    card_id: int


class AdminUserPageCallback(CallbackData, prefix="aup"):
    page: int
    filter_role: str = "all"


class AdminUserViewCallback(CallbackData, prefix="auv"):
    user_id: int


class AdminUserActionCallback(CallbackData, prefix="aua"):
    action: str
    user_id: int


class AdminHelpCallback(CallbackData, prefix="ah"):
    page: int = 0


class AdminMainCallback(CallbackData, prefix="amain"):
    pass


# ===========================================================================
# DATABASE
# ===========================================================================
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
    """Таблицы уже создаёт user-бот; на всякий случай — IF NOT EXISTS."""
    async with get_db() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                rarity TEXT NOT NULL,
                photo_id TEXT,
                photo_path TEXT
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
        for table, column, definition in (
            ("cards", "photo_path", "TEXT"),
            ("cards", "photo_id", "TEXT"),
            ("users", "last_dice", "INTEGER DEFAULT 0"),
        ):
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            except aiosqlite.OperationalError:
                pass


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


async def get_user_nickname(user_id: int) -> str:
    async with get_db() as db:
        cur = await db.execute("SELECT nickname FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return display_name(row["nickname"] if row else None, user_id)


# ===========================================================================
# FILTERS
# ===========================================================================
class AdminFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return await is_admin(message.from_user.id)


class SuperAdminFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return await is_superadmin(message.from_user.id)


admin_filter = AdminFilter()
superadmin_filter = SuperAdminFilter()


# ===========================================================================
# FSM
# ===========================================================================
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


# ===========================================================================
# KEYBOARDS
# ===========================================================================
def get_main_reply_kb() -> ReplyKeyboardMarkup:
    """Постоянное reply-меню навигации (т.к. бот выделен отдельно)."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="➕ Добавить"),
                KeyboardButton(text="📜 Карточки"),
            ],
            [
                KeyboardButton(text="👥 Пользователи"),
                KeyboardButton(text="📊 Статистика"),
            ],
            [
                KeyboardButton(text="🛠 Справка"),
                KeyboardButton(text="📥 Логи"),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите раздел…",
    )


def get_rarity_keyboard(callback_prefix: str = "set_rarity") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key, val in RARITIES.items():
        b.button(text=f"{val['icon']} {val['name']}", callback_data=f"{callback_prefix}:{key}")
    b.button(text="✕ Отмена", callback_data="cancel_add_card")
    b.adjust(1)  # один столбец — не растягивается
    return b.as_markup()


def get_cancel_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✕ Отмена", callback_data="cancel_add_card")
    return b.as_markup()


# ===========================================================================
# ROUTER
# ===========================================================================
router = Router()


# ===========================================================================
# ACCESS HELPER
# ===========================================================================
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


# ===========================================================================
# PANEL / START
# ===========================================================================
@router.message(Command("start"))
@router.message(Command("admin"))
@router.message(F.text == "⚙️ Панель")
async def admin_panel(message: Message):
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
    try:
        await call.message.edit_text(text)
    except TelegramBadRequest:
        await call.message.answer(text, reply_markup=get_main_reply_kb())
    await call.answer()


# ===========================================================================
# ADD CARD
# ===========================================================================
@router.message(F.text == "➕ Добавить")
@router.callback_query(F.data == "admin_add_card")
async def add_card_start(event: Message | CallbackQuery, state: FSMContext):
    if not await _require_admin(event):
        return
    await state.set_state(AddCardSG.photo)
    text = "📷 <b>Отправьте фото новой карточки</b>\n\n<code>/cancel</code> — отмена"
    if isinstance(event, CallbackQuery):
        await event.message.answer(text)
        await event.answer()
    else:
        await event.answer(text)


@router.message(Command("addcard"), admin_filter, F.photo)
async def quick_add_card(message: Message, command: CommandObject):
    if not message.photo:
        return
    args = (command.args or "").strip().split()
    if len(args) < 2:
        await message.reply(
            "✏️ <code>/addcard Название редкость</code> (+ фото)\n"
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

    async with get_db() as db:
        cur = await db.execute(
            "INSERT INTO cards (name, rarity, photo_id) VALUES (?, ?, ?)",
            (name, rarity, photo_id),
        )
        card_id = cur.lastrowid
        if sync_card_photo:
            try:
                path = await sync_card_photo(message.bot, card_id, photo_id)
                if path:
                    await db.execute(
                        "UPDATE cards SET photo_path = ? WHERE id = ?", (path, card_id)
                    )
                    photo_path = path
            except Exception as e:
                logger.error("sync_card_photo: %s", e)

    r = RARITIES[rarity]
    await message.reply(
        f"✅ <b>Карточка добавлена</b>\n\n"
        f"{bq(line('🃏', 'Название', esc(name)) + chr(10) + line(r['icon'], 'Редкость', r['name']) + chr(10) + line('🆔', 'ID', card_id) + chr(10) + line('💾', 'Файл', photo_path or 'только file_id'))}",
        reply_markup=get_main_reply_kb(),
    )


@router.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext):
    if await state.get_state() is None:
        return
    await state.clear()
    await message.answer("✅ Операция отменена", reply_markup=get_main_reply_kb())


@router.callback_query(F.data == "cancel_add_card")
async def cancel_add_card_callback(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await call.message.answer("✅ Отменено", reply_markup=get_main_reply_kb())


@router.message(AddCardSG.photo, F.photo)
async def add_card_photo(message: Message, state: FSMContext):
    if not await _require_admin(message):
        await state.clear()
        return
    await state.update_data(photo_id=message.photo[-1].file_id)
    await state.set_state(AddCardSG.name)
    await message.answer("✍️ <b>Введите название</b> (до 64 символов)")


@router.message(AddCardSG.name, F.text)
async def add_card_name(message: Message, state: FSMContext):
    if not await _require_admin(message):
        await state.clear()
        return
    await state.update_data(name=message.text[:64])
    await state.set_state(AddCardSG.rarity)
    await message.answer("🎲 <b>Выберите редкость</b>", reply_markup=get_rarity_keyboard())


@router.callback_query(AddCardSG.rarity, F.data.startswith("set_rarity:"))
async def add_card_rarity(call: CallbackQuery, state: FSMContext):
    if not await _require_admin(call):
        await state.clear()
        return

    rarity = call.data.split(":")[1]
    data = await state.get_data()
    photo_path = None

    async with get_db() as db:
        cur = await db.execute(
            "INSERT INTO cards (name, rarity, photo_id) VALUES (?, ?, ?)",
            (data["name"], rarity, data["photo_id"]),
        )
        card_id = cur.lastrowid
        if sync_card_photo:
            try:
                path = await sync_card_photo(call.bot, card_id, data["photo_id"])
                if path:
                    await db.execute(
                        "UPDATE cards SET photo_path = ? WHERE id = ?", (path, card_id)
                    )
                    photo_path = path
            except Exception as e:
                logger.error("sync: %s", e)

    r = RARITIES[rarity]
    await call.message.answer(
        f"✅ <b>Карточка добавлена</b>\n\n"
        f"{bq(line('🃏', 'Название', esc(data['name'])) + chr(10) + line(r['icon'], 'Редкость', r['name']) + chr(10) + line('🆔', 'ID', card_id) + chr(10) + line('💾', 'Файл', photo_path or 'только file_id'))}",
        reply_markup=get_main_reply_kb(),
    )
    await state.clear()
    await call.answer()


# ===========================================================================
# CARDS LIST & MANAGE
# ===========================================================================
async def build_admin_cards_page(page: int = 0):
    per_page = 5
    async with get_db() as db:
        cur = await db.execute("SELECT COUNT(*) FROM cards")
        total = (await cur.fetchone())[0]

        if total == 0:
            return (
                "📜 <b>Карточки</b>\n\nБаза пуста. Добавьте через «➕ Добавить».",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="‹ В меню", callback_data="admin_main")]
                ]),
                0,
            )

        total_pages = max(1, (total + per_page - 1) // per_page)
        page = max(0, min(page, total_pages - 1))
        offset = page * per_page

        cur = await db.execute(
            "SELECT id, name, rarity, photo_id, photo_path FROM cards "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (per_page, offset),
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
                callback_data=AdminCardManageCallback(card_id=c["id"]).pack(),
            )

        text = (
            f"📜 <b>Карточки</b>\n"
            f"{sep()}\n"
            f"{line('📄', 'Страница', f'<b>{page + 1}</b> / {total_pages}')}\n"
            f"{line('📦', 'Всего', f'<b>{fmt_num(total)}</b>')}\n\n"
            f"{bq(chr(10).join(lines))}"
        )

        # Navigation row
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(
                text="‹", callback_data=AdminCardPageCallback(page=page - 1).pack()
            ))
        nav.append(InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}", callback_data="ignore"
        ))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(
                text="›", callback_data=AdminCardPageCallback(page=page + 1).pack()
            ))

        b.adjust(1)
        b.row(*nav)
        b.row(InlineKeyboardButton(text="‹ В меню", callback_data="admin_main"))
        return text, b.as_markup(), page


@router.message(F.text == "📜 Карточки")
@router.callback_query(AdminCardPageCallback.filter())
async def admin_cards_page(event: Message | CallbackQuery, callback_data: AdminCardPageCallback = None):
    if not await _require_admin(event):
        return

    page = callback_data.page if callback_data else 0
    text, kb, _ = await build_admin_cards_page(page)

    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            await event.message.answer(text, reply_markup=kb)
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb)


@router.callback_query(AdminCardManageCallback.filter())
async def admin_card_manage(call: CallbackQuery, callback_data: AdminCardManageCallback):
    if not await _require_admin(call):
        return

    card_id = callback_data.card_id
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
    caption = (
        f"🃏 <b>{esc(card['name'])}</b>\n"
        f"{sep(20)}\n\n"
        f"{bq(line('🆔', 'ID', f'<code>{card_id}</code>') + chr(10) + line(r.get('icon', '•'), 'Редкость', r.get('name', card['rarity'])) + chr(10) + line('👥', 'Владельцев', f'<b>{fmt_num(owners)}</b>') + chr(10) + line('💾', 'Файл', card['photo_path'] or 'нет'))}"
    )

    b = InlineKeyboardBuilder()
    b.button(text="✏️ Название", callback_data=f"edit_name:{card_id}")
    b.button(text="🎲 Редкость", callback_data=f"edit_rarity:{card_id}")
    b.button(text="🖼 Фото", callback_data=AdminCardEditPhotoCallback(card_id=card_id).pack())
    b.button(text="🗑 Удалить", callback_data=f"delete_card:{card_id}")
    b.button(text="‹ К списку", callback_data=AdminCardPageCallback(page=0).pack())
    b.adjust(1)

    photo = None
    if get_card_photo_input:
        photo = get_card_photo_input(card_id, card["photo_path"])
    if not photo and card["photo_id"]:
        photo = card["photo_id"]

    try:
        if photo:
            if call.message.photo:
                await call.message.edit_media(
                    media=InputMediaPhoto(media=photo, caption=caption),
                    reply_markup=b.as_markup(),
                )
            else:
                await call.message.answer_photo(
                    photo=photo, caption=caption, reply_markup=b.as_markup()
                )
        else:
            if call.message.photo:
                await call.message.edit_caption(caption=caption, reply_markup=b.as_markup())
            else:
                try:
                    await call.message.edit_text(caption, reply_markup=b.as_markup())
                except TelegramBadRequest:
                    await call.message.answer(caption, reply_markup=b.as_markup())
    except TelegramBadRequest:
        await call.message.answer(caption, reply_markup=b.as_markup())
    await call.answer()


@router.callback_query(AdminCardEditPhotoCallback.filter())
async def admin_card_edit_photo_start(
    call: CallbackQuery, callback_data: AdminCardEditPhotoCallback, state: FSMContext
):
    if not await _require_admin(call):
        return
    await state.set_state(EditCardPhotoSG.photo)
    await state.update_data(card_id=callback_data.card_id)
    await call.message.answer("📷 Отправьте новое фото\n\n<code>/cancel</code>")
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

    async with get_db() as db:
        await db.execute(
            "UPDATE cards SET photo_id = ? WHERE id = ?", (new_photo, card_id)
        )
        if sync_card_photo:
            try:
                path = await sync_card_photo(message.bot, card_id, new_photo)
                if path:
                    await db.execute(
                        "UPDATE cards SET photo_path = ? WHERE id = ?", (path, card_id)
                    )
                    path_str = path
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
    await call.message.answer("✍️ Введите новое название:")
    await call.answer()


@router.message(EditCardSG.new_name, F.text)
async def edit_card_name_save(message: Message, state: FSMContext):
    if not await _require_admin(message):
        await state.clear()
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


# ===========================================================================
# USERS
# ===========================================================================
async def build_admin_users_page(page: int = 0, filter_role: str = "all"):
    per_page = 5
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
                    [InlineKeyboardButton(text="‹ В меню", callback_data="admin_main")]
                ]),
                0,
            )

        total_pages = max(1, (total + per_page - 1) // per_page)
        page = max(0, min(page, total_pages - 1))
        offset = page * per_page

        if filter_role == "all":
            cur = await db.execute(
                """SELECT user_id, nickname, coins, streak, role, registration
                   FROM users ORDER BY registration DESC LIMIT ? OFFSET ?""",
                (per_page, offset),
            )
        else:
            cur = await db.execute(
                """SELECT user_id, nickname, coins, streak, role, registration
                   FROM users WHERE role = ? ORDER BY registration DESC
                   LIMIT ? OFFSET ?""",
                (filter_role, per_page, offset),
            )
        users = await cur.fetchall()

        b = InlineKeyboardBuilder()

        # Фильтры — один столбец
        filters = [
            ("all", "Все"),
            ("user", "👤 Пользователи"),
            ("admin", "🛡 Админы"),
            ("banned", "🚫 Забаненные"),
        ]
        for fr, label in filters:
            mark = "●" if fr == filter_role else "○"
            b.button(
                text=f"{mark} {label}",
                callback_data=AdminUserPageCallback(page=0, filter_role=fr).pack(),
            )

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
                callback_data=AdminUserViewCallback(user_id=u["user_id"]).pack(),
            )

        text = (
            f"👥 <b>Пользователи</b>\n"
            f"{sep()}\n"
            f"{line('📄', 'Страница', f'<b>{page + 1}</b> / {total_pages}')}\n"
            f"{line('📦', 'Всего', f'<b>{fmt_num(total)}</b>')}\n\n"
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

        b.adjust(1)  # всё в один столбец
        b.row(*nav)
        b.row(InlineKeyboardButton(text="‹ В меню", callback_data="admin_main"))
        return text, b.as_markup(), page


@router.message(F.text == "👥 Пользователи")
@router.callback_query(AdminUserPageCallback.filter())
async def admin_users_page(
    event: Message | CallbackQuery, callback_data: AdminUserPageCallback = None
):
    if not await _require_admin(event):
        return

    page = callback_data.page if callback_data else 0
    filter_role = callback_data.filter_role if callback_data else "all"
    text, kb, _ = await build_admin_users_page(page, filter_role)

    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            await event.message.answer(text, reply_markup=kb)
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb)


async def _get_profile_photo(bot: Bot, user_id: int):
    """Пытается получить фото профиля пользователя. Fallback → default avatar."""
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if photos.total_count > 0 and photos.photos:
            # Берём самое большое доступное фото
            return photos.photos[0][-1].file_id
    except (TelegramBadRequest, TelegramForbiddenError, Exception) as e:
        logger.debug("Не удалось получить фото профиля %s: %s", user_id, e)

    # Fallback 1: DEFAULT_AVATAR_PATH / file
    candidates = []
    if DEFAULT_AVATAR_PATH:
        candidates.append(Path(DEFAULT_AVATAR_PATH))
    candidates.append(Path("/app/data/card_photos/default_avatar.jpg"))

    for p in candidates:
        try:
            if p.is_file():
                return FSInputFile(p)
        except Exception:
            continue

    # Fallback 2: заранее известный file_id
    if DEFAULT_AVATAR_FILE_ID:
        return DEFAULT_AVATAR_FILE_ID

    return None


@router.callback_query(AdminUserViewCallback.filter())
async def admin_user_view(call: CallbackQuery, callback_data: AdminUserViewCallback):
    if not await _require_admin(call):
        return

    user_id = callback_data.user_id
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

    inner = (
        line("🆔", "ID", f"<code>{user_id}</code>")
        + "\n"
        + line("🎭", "Роль", role_display(role))
        + "\n"
        + line("📅", "Регистрация", reg)
        + "\n"
        + line("🪙", "Монеты", f"<b>{fmt_num(user['coins'])}</b>")
        + "\n"
        + line("🃏", "Карточек", f"<b>{fmt_num(user['cards_count'])}</b>")
        + "\n"
        + line("🔥", "Стрик", f"<b>{user['streak'] or 0}</b>")
    )
    caption = (
        f"👤 <b>{esc(nick)}</b>\n"
        f"{sep(20)}\n\n"
        f"{bq(inner)}"
    )

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

    b.button(text="‹ К списку", callback_data=AdminUserPageCallback(page=0, filter_role="all").pack())
    b.adjust(1)

    photo = await _get_profile_photo(call.bot, user_id)

    try:
        if photo:
            if call.message.photo:
                await call.message.edit_media(
                    media=InputMediaPhoto(media=photo, caption=caption),
                    reply_markup=b.as_markup(),
                )
            else:
                await call.message.answer_photo(
                    photo=photo, caption=caption, reply_markup=b.as_markup()
                )
        else:
            try:
                await call.message.edit_text(caption, reply_markup=b.as_markup())
            except TelegramBadRequest:
                await call.message.answer(caption, reply_markup=b.as_markup())
    except TelegramBadRequest:
        await call.message.answer(caption, reply_markup=b.as_markup())
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
        await call.message.answer(f"✏️ <code>/setnick {target_id} НовыйНик</code>")
        await call.answer()
    elif action == "coins":
        await call.message.answer(f"🪙 <code>/setcoins {target_id} 1000</code>")
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


# ===========================================================================
# COMMANDS
# ===========================================================================
@router.message(Command("setnick"), admin_filter)
async def admin_setnick(message: Message, command: CommandObject):
    args = (command.args or "").strip().split()
    if len(args) < 2:
        await message.reply("✏️ <code>/setnick USERID НовыйНик</code>")
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
        await message.reply("✏️ <code>/setadmin USERID</code>")
        return
    try:
        target_id = int(arg)
    except ValueError:
        await message.reply("❌ USERID должен быть числом")
        return
    ok = await set_user_role(target_id, "admin")
    if not ok:
        await message.reply("❌ Пользователь не найден (должен зайти в user-бота)")
        return
    await message.reply(f"✅ Администратор: <code>{target_id}</code>")


@router.message(Command("unsetadmin"), superadmin_filter)
async def unset_admin_cmd(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg:
        await message.reply("✏️ <code>/unsetadmin USERID</code>")
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
        await message.reply("✏️ <code>/ban USERID</code>")
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
        await message.reply("✏️ <code>/unban USERID</code>")
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
        await message.reply("✏️ <code>/setcoins [USERID] N</code>")
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
            "SELECT coins, nickname FROM users WHERE user_id = ?", (target_id,)
        )
        row = await cur.fetchone()
        if not row:
            await message.reply("❌ Пользователь не найден")
            return
        old = row["coins"] or 0
        await db.execute(
            "UPDATE users SET coins = ? WHERE user_id = ?", (coins, target_id)
        )

    await message.reply(
        f"✅ Монеты <code>{target_id}</code>\n"
        f"{bq(line('🪙', 'Было', fmt_num(old)) + chr(10) + line('🪙', 'Стало', f'<b>{fmt_num(coins)}</b>'))}"
    )


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
        await message.reply("✏️ <code>/delcard ID</code>")
        return
    try:
        card_id = int(arg)
    except ValueError:
        await message.reply("❌ ID должен быть числом")
        return

    async with get_db() as db:
        cur = await db.execute(
            "SELECT name, rarity FROM cards WHERE id = ?", (card_id,)
        )
        card = await cur.fetchone()
        if not card:
            await message.reply("❌ Карточка не найдена")
            return
        await db.execute("DELETE FROM inventory WHERE card_id = ?", (card_id,))
        await db.execute("DELETE FROM cards WHERE id = ?", (card_id,))
    await message.reply(f"🗑 Удалена: {esc(card['name'])} (#{card_id})")


# ===========================================================================
# STATS
# ===========================================================================
async def _build_stats_text(full: bool = True) -> str:
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

    if not full:
        return (
            f"📊 <b>Краткая статистика</b>\n\n"
            f"{bq(line('👥', 'Пользователи', fmt_num(total_users)) + chr(10) + line('🃏', 'Карточки', fmt_num(total_cards)) + chr(10) + line('🪙', 'Монеты', fmt_num(total_coins)))}\n\n"
            f"<i>/stats — полная статистика</i>"
        )

    rarity_lines = "\n".join(
        line(v["icon"], v["name"], fmt_num(by_r.get(k, 0)))
        for k, v in RARITIES.items()
    )
    return (
        f"📊 <b>Статистика</b>\n{sep(20)}\n\n"
        f"{bq(line('👥', 'Пользователей', f'<b>{fmt_num(total_users)}</b>') + chr(10) + line('🛡', 'Админов', f'<b>{fmt_num(staff)}</b>') + chr(10) + line('🚫', 'Забанено', f'<b>{fmt_num(banned)}</b>'))}\n\n"
        f"{bq(line('🃏', 'Карточек', f'<b>{fmt_num(total_cards)}</b>') + chr(10) + rarity_lines)}\n\n"
        f"{bq(line('🪙', 'Монет в игре', f'<b>{fmt_num(total_coins)}</b>') + chr(10) + line('📦', 'В коллекциях', f'<b>{fmt_num(owned)}</b>'))}"
    )


@router.message(Command("stats"), admin_filter)
@router.message(F.text == "📊 Статистика")
async def admin_stats(message: Message):
    if not await _require_admin(message):
        return
    try:
        text = await _build_stats_text(full=True)
        await message.reply(text, reply_markup=get_main_reply_kb())
    except Exception as e:
        logger.error("stats: %s", e)
        await message.reply("❌ Ошибка при получении статистики")


@router.callback_query(F.data == "admin_stats_quick")
async def admin_stats_quick(call: CallbackQuery):
    if not await _require_admin(call):
        return
    await call.answer()
    try:
        text = await _build_stats_text(full=False)
        b = InlineKeyboardBuilder()
        b.button(text="‹ В меню", callback_data="admin_main")
        await call.message.answer(text, reply_markup=b.as_markup())
    except Exception as e:
        logger.error("stats quick: %s", e)


@router.message(Command("getusers"), admin_filter)
async def admin_getusers(message: Message):
    async with get_db() as db:
        cur = await db.execute(
            """SELECT u.user_id, u.nickname, u.coins, u.streak, u.role, u.registration,
                      (SELECT COUNT(*) FROM inventory i WHERE i.user_id = u.user_id) AS cards_count
               FROM users u ORDER BY u.registration DESC"""
        )
        users = await cur.fetchall()

    if not users:
        await message.reply("Пусто")
        return

    chunk = f"👥 Всего: {fmt_num(len(users))}\n\n"
    for u in users:
        role_info = ROLES.get(u["role"] or "user", ROLES["user"])
        nick = display_name(u["nickname"], u["user_id"])
        line_s = (
            f"{role_info['icon']} <b>{esc(nick)}</b> (<code>{u['user_id']}</code>)\n"
            f"    🪙 {fmt_num(u['coins'] or 0)} | 🃏 {fmt_num(u['cards_count'])} | 🔥 {u['streak'] or 0}\n"
        )
        if len(chunk) + len(line_s) > 3500:
            await message.reply(chunk)
            chunk = line_s
        else:
            chunk += line_s
    if chunk:
        await message.reply(chunk)


# ===========================================================================
# LOGS DOWNLOAD
# ===========================================================================
@router.message(F.text == "📥 Логи")
@router.message(Command("logs"), admin_filter)
async def admin_logs(message: Message):
    if not await _require_admin(message):
        return

    files = [
        ("admin_bot.log", ADMIN_LOG_PATH),
        ("bot.log", USER_LOG_PATH),
    ]

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
            # Ограничение Telegram ~50 МБ, но для логов обычно достаточно
            if size > 45 * 1024 * 1024:
                await message.answer(f"⚠️ {name} слишком большой ({fmt_num(size)} байт)")
                continue
            doc = FSInputFile(p, filename=name)
            await message.answer_document(
                document=doc,
                caption=f"📄 <b>{esc(name)}</b>\n{line('📦', 'Размер', fmt_num(size) + ' байт')}",
            )
            sent += 1
        except Exception as e:
            logger.error("Не удалось отправить %s: %s", path, e)
            await message.answer(f"❌ Ошибка при отправке {name}: {esc(str(e))}")

    if sent == 0:
        await message.answer("Нет доступных логов для выгрузки.")


# ===========================================================================
# PHOTO MIGRATION / UTILS
# ===========================================================================
@router.message(Command("migrate_photos"), admin_filter)
async def cmd_migrate_photos(message: Message):
    if not migrate_all_card_photos:
        await message.reply("❌ Модуль card_photos.py не найден")
        return
    status = await message.reply("⏳ Скачиваю фото…")
    try:
        async with get_db() as db:
            ok, fail = await migrate_all_card_photos(message.bot, db)
        await status.edit_text(
            f"✅ Миграция завершена\n\n"
            f"{bq(line('✓', 'Успешно', ok) + chr(10) + line('✗', 'Ошибки', fail))}"
        )
    except Exception as e:
        logger.error("migrate: %s", e)
        await status.edit_text(f"❌ {esc(str(e))}")


@router.message(Command("sync_default_avatar"), admin_filter)
async def cmd_sync_default_avatar(message: Message):
    if not ensure_default_avatar:
        await message.reply("❌ card_photos недоступен")
        return
    ok = await ensure_default_avatar(message.bot)
    path = str(DEFAULT_AVATAR_PATH) if DEFAULT_AVATAR_PATH else "?"
    if ok:
        await message.reply(f"✅ Аватар синхронизирован:\n<code>{esc(path)}</code>")
    else:
        await message.reply("❌ Не удалось скачать аватар")


@router.message(Command("getfileid"), admin_filter, F.photo)
async def test_get_file_id(message: Message):
    file_id = message.photo[-1].file_id
    await message.reply(f"🆔 <code>{esc(file_id)}</code>")


# ===========================================================================
# HELP
# ===========================================================================
ADMIN_HELP_PAGES = [
    {
        "title": "🛠 Роли",
        "body": (
            f"{line('👑', 'Superadmin', 'полный доступ, управление ролями')}\n"
            f"{line('🛡', 'Admin', 'карточки, пользователи, балансы')}\n"
            f"{line('🚫', 'Banned', 'блок в user-боте')}\n\n"
            f"Первый пользователь в user-боте автоматически становится superadmin."
        ),
    },
    {
        "title": "🛠 Команды",
        "body": (
            f"<code>/setadmin USERID</code>\n"
            f"<code>/unsetadmin USERID</code>\n"
            f"<code>/ban USERID</code>\n"
            f"<code>/unban USERID</code>\n"
            f"<code>/setcoins [USERID] N</code>\n"
            f"<code>/setnick USERID Ник</code>\n"
            f"<code>/resetcd [USERID]</code>\n"
            f"<code>/delcard ID</code>\n"
            f"<code>/addcard Имя редкость</code> + фото\n"
            f"<code>/stats</code> · <code>/getusers</code>\n"
            f"<code>/logs</code>\n"
            f"<code>/migrate_photos</code>\n"
            f"<code>/sync_default_avatar</code>"
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
            text="‹", callback_data=AdminHelpCallback(page=page - 1).pack()
        ))
    nav.append(InlineKeyboardButton(
        text=f"{page + 1} / {total}", callback_data="ignore"
    ))
    if page < total - 1:
        nav.append(InlineKeyboardButton(
            text="›", callback_data=AdminHelpCallback(page=page + 1).pack()
        ))
    b.row(*nav)
    b.row(InlineKeyboardButton(text="‹ В меню", callback_data="admin_main"))
    return b.as_markup()


def build_admin_help_text(page: int) -> str:
    page = max(0, min(page, len(ADMIN_HELP_PAGES) - 1))
    p = ADMIN_HELP_PAGES[page]
    return f"<b>{p['title']}</b>\n{sep(20)}\n\n{p['body']}"


@router.message(Command("adminhelp"), admin_filter)
@router.message(F.text == "🛠 Справка")
async def admin_help(message: Message):
    if not await _require_admin(message):
        return
    await message.reply(
        build_admin_help_text(0), reply_markup=get_admin_help_keyboard(0)
    )


@router.callback_query(AdminHelpCallback.filter())
async def admin_help_page(callback: CallbackQuery, callback_data: AdminHelpCallback):
    if not await _require_admin(callback):
        return
    text = build_admin_help_text(callback_data.page)
    kb = get_admin_help_keyboard(callback_data.page)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "ignore")
async def ignore_cb(callback: CallbackQuery):
    await callback.answer()


# ===========================================================================
# STARTUP
# ===========================================================================
ADMIN_COMMANDS = [
    BotCommand(command="start", description="⚙️ Панель"),
    BotCommand(command="admin", description="⚙️ Панель"),
    BotCommand(command="adminhelp", description="🛠 Справка"),
    BotCommand(command="stats", description="📊 Статистика"),
    BotCommand(command="logs", description="📥 Выгрузить логи"),
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
    BotCommand(command="sync_default_avatar", description="🖼 Дефолт-аватар"),
    BotCommand(command="getusers", description="👥 Список пользователей"),
]


async def run_admin_bot(token: Optional[str] = None) -> None:
    """Запуск polling админ-бота. Можно вызывать из bot.py в том же процессе."""
    tok = (token or ADMIN_BOT_TOKEN or "").strip()
    if not tok:
        logger.warning("ADMIN_BOT_TOKEN не задан — админ-бот не запущен")
        return

    os.makedirs(os.path.dirname(DB_NAME) or ".", exist_ok=True)
    try:
        Path(ADMIN_LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    await init_db_minimal()
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
    logger.info("🛡 Админ-бот запущен (DB=%s, log=%s)", DB_NAME, ADMIN_LOG_PATH)
    await dp.start_polling(bot)


async def main() -> None:
    if not ADMIN_BOT_TOKEN:
        raise SystemExit(
            "ADMIN_BOT_TOKEN не задан. Укажите токен админ-бота в .env "
            "(отдельный бот от пользовательского)."
        )
    await run_admin_bot(ADMIN_BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
