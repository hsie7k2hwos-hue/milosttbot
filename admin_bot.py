"""
Админ-бот (отдельный процесс, тот же DB_NAME / volume).

Запуск:
  ADMIN_BOT_TOKEN=... BOT_TOKEN=... DB_NAME=/app/data/cards_game.db python admin_bot.py

Или в .env:
  ADMIN_BOT_TOKEN=xxx
  DB_NAME=/app/data/cards_game.db

Пользовательский бот и админ-бот должны монтировать одну и ту же папку данных
(SQLite WAL позволяет двум процессам работать с одной БД).
"""
import asyncio
import html
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, BaseFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand, BotCommandScopeDefault,
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, Message, LinkPreviewOptions,
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
    )
except ImportError:
    sync_card_photo = None
    migrate_all_card_photos = None
    get_card_photo_input = None
    ensure_default_avatar = None
    DEFAULT_AVATAR_PATH = None
    DEFAULT_AVATAR_FILE_ID = "AgACAgIAAxkBAAID12qql3EFpnb2HwTCE7Yn_Ri1TQsNAAKRIGsbfHlYSVUpxfU75O60AQADAgADeAADPQQ"

    def ensure_photo_dir():
        pass

load_dotenv()

ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN") or os.getenv("BOT_TOKEN_ADMIN")
if not ADMIN_BOT_TOKEN:
    raise SystemExit(
        "ADMIN_BOT_TOKEN не задан. Укажите токен админ-бота в .env "
        "(отдельный бот от пользовательского)."
    )

DB_NAME = os.getenv("DB_NAME", "/app/data/cards_game.db")
LOG_PATH = os.getenv("ADMIN_LOG_PATH", os.getenv("LOG_PATH", "/app/data/admin_bot.log"))
COOLDOWN_SECONDS = 3 * 3600

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

NICKNAME_RE = __import__("re").compile(r"^[\w\-. ]{2,32}$", __import__("re").UNICODE)
URL_RE = __import__("re").compile(r"(https?://|t\.me/|@\w+)", __import__("re").IGNORECASE)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()],
)
logger = logging.getLogger("admin_bot")


def esc(text) -> str:
    return html.escape(str(text), quote=False)


def fmt_num(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    return f"{n:,}".replace(",", "\u00a0")


def line(emoji: str, label: str, value) -> str:
    return f"{emoji} {label}: {value}"


def role_display(role: str) -> str:
    info = ROLES.get(role, ROLES["user"])
    return f"{info['icon']} {info['name']}"


def display_name(nickname, user_id, fallback=None) -> str:
    if nickname and str(nickname).strip():
        return str(nickname).strip()
    if fallback and str(fallback).strip():
        return str(fallback).strip()[:32]
    return str(user_id)


# ================= CALLBACK =================
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


# ================= DB =================
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
        logger.error(f"DB: {e}")
        raise
    finally:
        await db.close()


async def init_db_minimal():
    """Таблицы уже создаёт user-бот; на всякий случай дублируем IF NOT EXISTS."""
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
        for table, column, definition in [
            ("cards", "photo_path", "TEXT"),
            ("cards", "photo_id", "TEXT"),
            ("users", "last_dice", "INTEGER DEFAULT 0"),
        ]:
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


class AdminFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return await is_admin(message.from_user.id)


class SuperAdminFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return await is_superadmin(message.from_user.id)


admin_filter = AdminFilter()
superadmin_filter = SuperAdminFilter()


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


router = Router()


# ================= КЛАВИАТУРЫ =================
def get_rarity_keyboard(callback_prefix: str = "set_rarity"):
    b = InlineKeyboardBuilder()
    for key, val in RARITIES.items():
        b.button(text=f"{val['icon']} {val['name']}", callback_data=f"{callback_prefix}:{key}")
    b.button(text="✕ Отмена", callback_data="cancel_add_card")
    b.adjust(2, 2, 1, 1)
    return b.as_markup()


def get_admin_main_kb():
    b = InlineKeyboardBuilder()
    b.button(text="➕ Добавить", callback_data="admin_add_card")
    b.button(text="📜 Карточки", callback_data=AdminCardPageCallback(page=0).pack())
    b.button(text="👥 Пользователи", callback_data=AdminUserPageCallback(page=0, filter_role="all").pack())
    b.button(text="📊 Статистика", callback_data="admin_stats_quick")
    b.button(text="🛠 Справка", callback_data=AdminHelpCallback(page=0).pack())
    b.adjust(2, 2, 1)
    return b.as_markup()


# ================= ПАНЕЛЬ =================
@router.message(Command("start"))
@router.message(Command("admin"))
async def admin_panel(message: Message):
    if not await is_admin(message.from_user.id):
        await message.reply("⚠️ Нет доступа. Нужна роль admin/superadmin в общей БД.")
        return
    role = await get_user_role(message.from_user.id)
    text = (
        f"⚙️ <b>Админ-панель</b>\n"
        f"{'─' * 18}\n\n"
        f"{line('🎭', 'Роль', role_display(role))}\n\n"
        f"Выберите раздел:"
    )
    await message.answer(text, reply_markup=get_admin_main_kb())


@router.callback_query(AdminMainCallback.filter())
@router.callback_query(F.data == "admin_main")
async def admin_main_back(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Нет доступа", show_alert=True)
        return
    role = await get_user_role(call.from_user.id)
    text = (
        f"⚙️ <b>Админ-панель</b>\n"
        f"{'─' * 18}\n\n"
        f"{line('🎭', 'Роль', role_display(role))}\n\n"
        f"Выберите раздел:"
    )
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer(text, reply_markup=get_admin_main_kb())
    await call.answer()


# ================= ДОБАВЛЕНИЕ КАРТОЧКИ =================
@router.callback_query(F.data == "admin_add_card")
async def add_card_start(call: CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Нет доступа")
        return
    await state.set_state(AddCardSG.photo)
    await call.message.answer("📷 <b>Отправьте фото новой карточки</b>\n\n<code>/cancel</code> — отмена")
    await call.answer()


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
                    await db.execute("UPDATE cards SET photo_path = ? WHERE id = ?", (path, card_id))
                    photo_path = path
            except Exception as e:
                logger.error(f"sync_card_photo: {e}")
    await message.reply(
        f"✓ <b>Карточка добавлена</b>\n\n"
        f"{line('🃏', 'Название', esc(name))}\n"
        f"{line(RARITIES[rarity]['icon'], 'Редкость', RARITIES[rarity]['name'])}\n"
        f"{line('🆔', 'ID', card_id)}\n"
        f"{line('💾', 'Файл', photo_path or 'только file_id')}",
        reply_markup=get_admin_main_kb(),
    )


@router.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext):
    if await state.get_state() is None:
        return
    await state.clear()
    await message.answer("✓ Операция отменена", reply_markup=get_admin_main_kb())


@router.callback_query(F.data == "cancel_add_card")
async def cancel_add_card_callback(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await call.message.answer("✓ Отменено", reply_markup=get_admin_main_kb())


@router.message(AddCardSG.photo, F.photo)
async def add_card_photo(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    await state.update_data(photo_id=message.photo[-1].file_id)
    await state.set_state(AddCardSG.name)
    await message.answer("✍️ <b>Название</b> (до 64 символов)")


@router.message(AddCardSG.name, F.text)
async def add_card_name(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    await state.update_data(name=message.text[:64])
    await state.set_state(AddCardSG.rarity)
    await message.answer("🎲 <b>Редкость</b>", reply_markup=get_rarity_keyboard())


@router.callback_query(AddCardSG.rarity, F.data.startswith("set_rarity:"))
async def add_card_rarity(call: CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        await state.clear()
        await call.answer("⚠️ Нет доступа")
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
                    await db.execute("UPDATE cards SET photo_path = ? WHERE id = ?", (path, card_id))
                    photo_path = path
            except Exception as e:
                logger.error(f"sync: {e}")
    r = RARITIES[rarity]
    await call.message.answer(
        f"✓ <b>Карточка добавлена</b>\n\n"
        f"{line('🃏', 'Название', esc(data['name']))}\n"
        f"{line(r['icon'], 'Редкость', r['name'])}\n"
        f"{line('🆔', 'ID', card_id)}\n"
        f"{line('💾', 'Файл', photo_path or 'только file_id')}",
        reply_markup=get_admin_main_kb(),
    )
    await state.clear()
    await call.answer()


# ================= СПИСОК КАРТОЧЕК =================
async def build_admin_cards_page(page: int = 0):
    per_page = 5
    async with get_db() as db:
        cur = await db.execute("SELECT COUNT(*) FROM cards")
        total = (await cur.fetchone())[0]
        if total == 0:
            return (
                "📜 <b>Карточки</b>\n\nВ базе пусто. Добавьте через «➕ Добавить».",
                get_admin_main_kb(),
                0,
            )
        total_pages = (total + per_page - 1) // per_page
        page = max(0, min(page, total_pages - 1))
        offset = page * per_page
        cur = await db.execute(
            "SELECT id, name, rarity, photo_id, photo_path FROM cards ORDER BY id DESC LIMIT ? OFFSET ?",
            (per_page, offset),
        )
        cards = await cur.fetchall()

        b = InlineKeyboardBuilder()
        text = (
            f"📜 <b>Карточки</b>\n"
            f"{'─' * 18}\n"
            f"{line('📄', 'Стр.', f'<b>{page + 1}</b> / {total_pages}')}\n"
            f"{line('📦', 'Всего', f'<b>{fmt_num(total)}</b>')}\n\n"
        )
        for c in cards:
            r = RARITIES.get(c["rarity"], {})
            cur = await db.execute(
                "SELECT COUNT(*), COALESCE(COUNT(*), 0) FROM inventory WHERE card_id = ?",
                (c["id"],),
            )
            owners = (await cur.fetchone())[0]
            has_file = "💾" if c["photo_path"] else "☁️"
            text += (
                f"{r.get('icon', '•')} <b>{esc(c['name'])}</b>\n"
                f"    #{c['id']} · 👥 {fmt_num(owners)} · {has_file}\n\n"
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
        b.row(InlineKeyboardButton(text="‹ В меню", callback_data="admin_main"))
        return text, b.as_markup(), page


@router.callback_query(AdminCardPageCallback.filter())
async def admin_cards_page_callback(call: CallbackQuery, callback_data: AdminCardPageCallback):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Нет доступа")
        return
    text, kb, _ = await build_admin_cards_page(callback_data.page)
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()


@router.callback_query(AdminCardManageCallback.filter())
async def admin_card_manage(call: CallbackQuery, callback_data: AdminCardManageCallback):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Нет доступа")
        return
    card_id = callback_data.card_id
    async with get_db() as db:
        cur = await db.execute(
            "SELECT name, rarity, photo_id, photo_path FROM cards WHERE id = ?", (card_id,)
        )
        card = await cur.fetchone()
        if not card:
            await call.answer("❌ Не найдена")
            return
        cur = await db.execute(
            "SELECT COUNT(*) FROM inventory WHERE card_id = ?", (card_id,)
        )
        owners = (await cur.fetchone())[0]

    r = RARITIES.get(card["rarity"], {})
    caption = (
        f"🃏 <b>{esc(card['name'])}</b>\n"
        f"{'─' * 20}\n\n"
        f"{line('🆔', 'ID', f'<code>{card_id}</code>')}\n"
        f"{line(r.get('icon', '•'), 'Редкость', r.get('name', card['rarity']))}\n"
        f"{line('👥', 'Владельцев', f'<b>{fmt_num(owners)}</b>')}\n"
        f"{line('💾', 'Файл', card['photo_path'] or 'нет')}"
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
                await call.message.answer_photo(photo=photo, caption=caption, reply_markup=b.as_markup())
        else:
            await call.message.answer(caption, reply_markup=b.as_markup())
    except TelegramBadRequest:
        await call.message.answer(caption, reply_markup=b.as_markup())
    await call.answer()


@router.callback_query(AdminCardEditPhotoCallback.filter())
async def admin_card_edit_photo_start(call: CallbackQuery, callback_data: AdminCardEditPhotoCallback, state: FSMContext):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Нет доступа")
        return
    await state.set_state(EditCardPhotoSG.photo)
    await state.update_data(card_id=callback_data.card_id)
    await call.message.answer("📷 Новое фото\n\n<code>/cancel</code>")
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
    path_str = None
    async with get_db() as db:
        await db.execute("UPDATE cards SET photo_id = ? WHERE id = ?", (new_photo, card_id))
        if sync_card_photo:
            try:
                path = await sync_card_photo(message.bot, card_id, new_photo)
                if path:
                    await db.execute("UPDATE cards SET photo_path = ? WHERE id = ?", (path, card_id))
                    path_str = path
            except Exception as e:
                logger.error(f"sync edit: {e}")
    await message.answer(
        f"✅ Фото обновлено (ID <code>{card_id}</code>)\n"
        f"{line('💾', 'Файл', path_str or 'ошибка загрузки')}",
        reply_markup=get_admin_main_kb(),
    )
    await state.clear()


@router.callback_query(F.data.startswith("delete_card:"))
async def delete_card_cmd(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Нет доступа")
        return
    card_id = int(call.data.split(":")[1])
    async with get_db() as db:
        await db.execute("DELETE FROM inventory WHERE card_id = ?", (card_id,))
        await db.execute("DELETE FROM cards WHERE id = ?", (card_id,))
    await call.message.answer("🗑 Карточка удалена", reply_markup=get_admin_main_kb())
    await call.answer()


@router.callback_query(F.data.startswith("edit_name:"))
async def edit_card_name_start(call: CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Нет доступа")
        return
    await state.update_data(card_id=int(call.data.split(":")[1]))
    await state.set_state(EditCardSG.new_name)
    await call.message.answer("✍️ Новое название:")
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
    await message.answer(f"✅ Название: {esc(new_name)}", reply_markup=get_admin_main_kb())
    await state.clear()


@router.callback_query(F.data.startswith("edit_rarity:"))
async def edit_card_rarity_start(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Нет доступа")
        return
    card_id = int(call.data.split(":")[1])
    b = InlineKeyboardBuilder()
    for key, info in RARITIES.items():
        b.button(text=info["name"], callback_data=AdminRarityCallback(card_id=card_id, rarity=key).pack())
    b.button(text="‹ Назад", callback_data=AdminCardManageCallback(card_id=card_id).pack())
    b.adjust(2)
    await call.message.answer("🎲 Новая редкость:", reply_markup=b.as_markup())
    await call.answer()


@router.callback_query(AdminRarityCallback.filter())
async def edit_card_rarity_save(call: CallbackQuery, callback_data: AdminRarityCallback):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Нет доступа")
        return
    async with get_db() as db:
        await db.execute(
            "UPDATE cards SET rarity = ? WHERE id = ?",
            (callback_data.rarity, callback_data.card_id),
        )
    await call.message.answer(
        f"✅ Редкость: {RARITIES[callback_data.rarity]['name']}",
        reply_markup=get_admin_main_kb(),
    )
    await call.answer()


# ================= ПОЛЬЗОВАТЕЛИ =================
async def build_admin_users_page(page: int = 0, filter_role: str = "all"):
    per_page = 5
    async with get_db() as db:
        if filter_role == "all":
            cur = await db.execute("SELECT COUNT(*) FROM users")
        else:
            cur = await db.execute("SELECT COUNT(*) FROM users WHERE role = ?", (filter_role,))
        total = (await cur.fetchone())[0]
        if total == 0:
            return (
                f"👥 Нет записей (фильтр: {filter_role}).",
                get_admin_main_kb(),
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
                   FROM users WHERE role = ? ORDER BY registration DESC LIMIT ? OFFSET ?""",
                (filter_role, per_page, offset),
            )
        users = await cur.fetchall()

        b = InlineKeyboardBuilder()
        filters = [("all", "Все"), ("user", "👤"), ("admin", "🛡"), ("banned", "🚫")]
        filter_row = []
        for fr, label in filters:
            mark = "●" if fr == filter_role else "○"
            filter_row.append(InlineKeyboardButton(
                text=f"{mark} {label}",
                callback_data=AdminUserPageCallback(page=0, filter_role=fr).pack(),
            ))
        b.row(*filter_row)

        text = (
            f"👥 <b>Пользователи</b>\n"
            f"{'─' * 18}\n"
            f"{line('📄', 'Стр.', f'<b>{page + 1}</b> / {total_pages}')}\n"
            f"{line('📦', 'Всего', f'<b>{fmt_num(total)}</b>')}\n\n"
        )
        for u in users:
            role_info = ROLES.get(u["role"] or "user", ROLES["user"])
            nick = display_name(u["nickname"], u["user_id"])
            text += (
                f"{role_info['icon']} <b>{esc(nick)}</b>\n"
                f"    <code>{u['user_id']}</code> · 🪙 {fmt_num(u['coins'] or 0)} · 🔥 {u['streak'] or 0}\n\n"
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
        b.row(InlineKeyboardButton(text="‹ В меню", callback_data="admin_main"))
        return text, b.as_markup(), page


@router.callback_query(AdminUserPageCallback.filter())
async def admin_users_page_callback(call: CallbackQuery, callback_data: AdminUserPageCallback):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Нет доступа")
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
        await call.answer("⚠️ Нет доступа")
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
            await call.answer("❌ Не найден")
            return

    nick = display_name(user["nickname"], user_id)
    role = user["role"] or "user"
    reg = (
        datetime.fromtimestamp(user["registration"]).strftime("%d.%m.%Y %H:%M")
        if user["registration"] else "—"
    )
    caption = (
        f"👤 <b>{esc(nick)}</b>\n"
        f"{'─' * 20}\n\n"
        f"{line('🆔', 'ID', f'<code>{user_id}</code>')}\n"
        f"{line('🎭', 'Роль', role_display(role))}\n"
        f"{line('📅', 'Регистрация', reg)}\n\n"
        f"{line('🪙', 'Монеты', f'<b>{fmt_num(user['coins'])}</b>')}\n"
        f"{line('🃏', 'Карточек', f'<b>{fmt_num(user['cards_count'])}</b>')}\n"
        f"{line('🔥', 'Стрик', f'<b>{user['streak'] or 0}</b>')}"
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
    await call.message.answer(caption, reply_markup=b.as_markup())
    await call.answer()


@router.callback_query(AdminUserActionCallback.filter())
async def admin_user_action(call: CallbackQuery, callback_data: AdminUserActionCallback):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Нет доступа")
        return

    action = callback_data.action
    target_id = callback_data.user_id
    viewer_id = call.from_user.id
    viewer_is_super = await is_superadmin(viewer_id)
    target_role = await get_user_role(target_id)

    if action == "ban":
        if target_id == viewer_id:
            await call.answer("❌ Нельзя себя", show_alert=True)
            return
        if target_role == "superadmin" and not viewer_is_super:
            await call.answer("❌ Недостаточно прав", show_alert=True)
            return
        await set_user_role(target_id, "banned")
        await call.message.answer(f"🚫 Забанен <code>{target_id}</code>", reply_markup=get_admin_main_kb())
        await call.answer("OK")
        return

    if action == "unban":
        await set_user_role(target_id, "user")
        await call.message.answer(f"✅ Разбан <code>{target_id}</code>", reply_markup=get_admin_main_kb())
        await call.answer("OK")
        return

    if action == "make_admin":
        if not viewer_is_super:
            await call.answer("❌ Только superadmin", show_alert=True)
            return
        await set_user_role(target_id, "admin")
        await call.message.answer(f"🛡 Админ <code>{target_id}</code>", reply_markup=get_admin_main_kb())
        await call.answer("OK")
        return

    if action == "make_super":
        if not viewer_is_super:
            await call.answer("❌ Только superadmin", show_alert=True)
            return
        await set_user_role(target_id, "superadmin")
        await call.message.answer(f"👑 Superadmin <code>{target_id}</code>", reply_markup=get_admin_main_kb())
        await call.answer("OK")
        return

    if action == "unadmin":
        if not viewer_is_super:
            await call.answer("❌ Только superadmin", show_alert=True)
            return
        if target_id == viewer_id:
            await call.answer("❌ Нельзя себя", show_alert=True)
            return
        await set_user_role(target_id, "user")
        await call.message.answer(f"✅ Права сняты <code>{target_id}</code>", reply_markup=get_admin_main_kb())
        await call.answer("OK")
        return

    if action == "nick":
        await call.message.answer(f"✏️ <code>/setnick {target_id} НовыйНик</code>")
        await call.answer()
    elif action == "coins":
        await call.message.answer(f"🪙 <code>/setcoins {target_id} 1000</code>")
        await call.answer()
    elif action == "resetcd":
        async with get_db() as db:
            await db.execute("UPDATE users SET last_claim = 0 WHERE user_id = ?", (target_id,))
        await call.message.answer(f"⏱ CD сброшен <code>{target_id}</code>", reply_markup=get_admin_main_kb())
        await call.answer("OK")
    else:
        await call.answer()


# ================= КОМАНДЫ =================
@router.message(Command("setnick"), admin_filter)
async def admin_setnick(message: Message, command: CommandObject):
    args = (command.args or "").strip().split()
    if len(args) < 2:
        await message.reply("✏️ <code>/setnick USERID НовыйНик</code>")
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await message.reply("❌ USERID — число")
        return
    raw = " ".join(args[1:]).strip()
    if not (2 <= len(raw) <= 32) or URL_RE.search(raw) or not NICKNAME_RE.match(raw):
        await message.reply("❌ Ник невалиден")
        return
    async with get_db() as db:
        cur = await db.execute("SELECT nickname FROM users WHERE user_id = ?", (target_id,))
        row = await cur.fetchone()
        if not row:
            await message.reply("❌ Пользователь не найден")
            return
        old = row["nickname"]
        await db.execute("UPDATE users SET nickname = ? WHERE user_id = ?", (raw, target_id))
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
        await message.reply("❌ USERID — число")
        return
    ok = await set_user_role(target_id, "admin")
    if not ok:
        await message.reply("❌ Пользователь не найден (должен зайти в user-бота)")
        return
    await message.reply(f"✅ Админ: <code>{target_id}</code>")


@router.message(Command("unsetadmin"), superadmin_filter)
async def unset_admin_cmd(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg:
        await message.reply("✏️ <code>/unsetadmin USERID</code>")
        return
    try:
        target_id = int(arg)
    except ValueError:
        await message.reply("❌ USERID — число")
        return
    if target_id == message.from_user.id:
        await message.reply("⚠️ Нельзя снять себя")
        return
    ok = await set_user_role(target_id, "user")
    if not ok:
        await message.reply("❌ Не найден")
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
        await message.reply("❌ USERID — число")
        return
    if target_id == message.from_user.id:
        await message.reply("❌ Нельзя себя")
        return
    tr = await get_user_role(target_id)
    if tr in ("admin", "superadmin") and not await is_superadmin(message.from_user.id):
        await message.reply("❌ Недостаточно прав")
        return
    ok = await set_user_role(target_id, "banned")
    if not ok:
        await message.reply("❌ Не найден")
        return
    await message.reply(f"🚫 Бан: <code>{target_id}</code>")


@router.message(Command("unban"), admin_filter)
async def unban_cmd(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg:
        await message.reply("✏️ <code>/unban USERID</code>")
        return
    try:
        target_id = int(arg)
    except ValueError:
        await message.reply("❌ USERID — число")
        return
    ok = await set_user_role(target_id, "user")
    if not ok:
        await message.reply("❌ Не найден")
        return
    await message.reply(f"✅ Разбан: <code>{target_id}</code>")


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
            await message.reply("❌ Число")
            return
    elif len(args) == 2:
        try:
            target_id = int(args[0])
            coins = int(args[1])
        except ValueError:
            await message.reply("❌ Числа")
            return
    else:
        await message.reply("❌ Лишние аргументы")
        return
    if coins < 0:
        await message.reply("❌ >= 0")
        return
    async with get_db() as db:
        cur = await db.execute("SELECT coins, nickname FROM users WHERE user_id = ?", (target_id,))
        row = await cur.fetchone()
        if not row:
            await message.reply("❌ Не найден")
            return
        old = row["coins"] or 0
        await db.execute("UPDATE users SET coins = ? WHERE user_id = ?", (coins, target_id))
    await message.reply(
        f"✅ Монеты <code>{target_id}</code>\n"
        f"{line('🪙', 'Было', fmt_num(old))}\n"
        f"{line('🪙', 'Стало', f'<b>{fmt_num(coins)}</b>')}"
    )


@router.message(Command("resetcd"), admin_filter)
async def admin_resetcd(message: Message, command: CommandObject):
    args = (command.args or "").strip().split()
    target_id = message.from_user.id
    if args:
        try:
            target_id = int(args[0])
        except ValueError:
            await message.reply("❌ USERID")
            return
    async with get_db() as db:
        cur = await db.execute("SELECT user_id FROM users WHERE user_id = ?", (target_id,))
        if not await cur.fetchone():
            await message.reply("❌ Не найден")
            return
        await db.execute("UPDATE users SET last_claim = 0 WHERE user_id = ?", (target_id,))
    await message.reply(f"✅ CD сброшен: <code>{target_id}</code>")


@router.message(Command("delcard"), admin_filter)
async def admin_delcard(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg:
        await message.reply("✏️ <code>/delcard ID</code>")
        return
    try:
        card_id = int(arg)
    except ValueError:
        await message.reply("❌ ID — число")
        return
    async with get_db() as db:
        cur = await db.execute("SELECT name, rarity FROM cards WHERE id = ?", (card_id,))
        card = await cur.fetchone()
        if not card:
            await message.reply("❌ Не найдена")
            return
        await db.execute("DELETE FROM inventory WHERE card_id = ?", (card_id,))
        await db.execute("DELETE FROM cards WHERE id = ?", (card_id,))
    await message.reply(f"🗑 Удалена: {esc(card['name'])} (#{card_id})")


@router.message(Command("stats"), admin_filter)
async def admin_stats(message: Message):
    try:
        async with get_db() as db:
            cur = await db.execute("SELECT COUNT(*) FROM users")
            total_users = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM users WHERE role = 'banned'")
            banned = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM users WHERE role IN ('admin','superadmin')")
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
        text = (
            f"📊 <b>Статистика</b>\n{'─' * 20}\n\n"
            f"{line('👥', 'Пользователей', f'<b>{fmt_num(total_users)}</b>')}\n"
            f"{line('🛡', 'Админов', f'<b>{fmt_num(staff)}</b>')}\n"
            f"{line('🚫', 'Забанено', f'<b>{fmt_num(banned)}</b>')}\n\n"
            f"{line('🃏', 'Карточек', f'<b>{fmt_num(total_cards)}</b>')}\n"
            f"{rarity_lines}\n\n"
            f"{line('🪙', 'Монет в игре', f'<b>{fmt_num(total_coins)}</b>')}\n"
            f"{line('📦', 'В коллекциях', f'<b>{fmt_num(owned)}</b>')}"
        )
        await message.reply(text)
    except Exception as e:
        logger.error(f"stats: {e}")
        await message.reply("❌ Ошибка статистики")


@router.callback_query(F.data == "admin_stats_quick")
async def admin_stats_quick(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Нет доступа")
        return
    await call.answer()
    # reuse
    class Fake:
        pass
    # просто вызовем логику через message
    try:
        async with get_db() as db:
            cur = await db.execute("SELECT COUNT(*) FROM users")
            total_users = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM cards")
            total_cards = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COALESCE(SUM(coins),0) FROM users")
            total_coins = (await cur.fetchone())[0]
        text = (
            f"📊 <b>Кратко</b>\n\n"
            f"{line('👥', 'Юзеры', fmt_num(total_users))}\n"
            f"{line('🃏', 'Карточки', fmt_num(total_cards))}\n"
            f"{line('🪙', 'Монеты', fmt_num(total_coins))}\n\n"
            f"<i>/stats — полная</i>"
        )
        b = InlineKeyboardBuilder()
        b.button(text="‹ В меню", callback_data="admin_main")
        await call.message.answer(text, reply_markup=b.as_markup())
    except Exception as e:
        logger.error(f"stats quick: {e}")


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


# ================= МИГРАЦИЯ ФОТО =================
@router.message(Command("migrate_photos"), admin_filter)
async def cmd_migrate_photos(message: Message):
    if not migrate_all_card_photos:
        await message.reply("❌ card_photos.py не найден")
        return
    status = await message.reply("⏳ Скачиваю фото…")
    try:
        async with get_db() as db:
            ok, fail = await migrate_all_card_photos(message.bot, db)
        await status.edit_text(
            f"✅ Миграция\n\n"
            f"{line('✓', 'OK', ok)}\n"
            f"{line('✗', 'Ошибки', fail)}"
        )
    except Exception as e:
        logger.error(f"migrate: {e}")
        await status.edit_text(f"❌ {esc(str(e))}")


@router.message(Command("sync_default_avatar"), admin_filter)
async def cmd_sync_default_avatar(message: Message):
    if not ensure_default_avatar:
        await message.reply("❌ card_photos недоступен")
        return
    ok = await ensure_default_avatar(message.bot)
    path = str(DEFAULT_AVATAR_PATH) if DEFAULT_AVATAR_PATH else "?"
    if ok:
        await message.reply(f"✅ Аватар:\n<code>{esc(path)}</code>")
    else:
        await message.reply("❌ Не удалось скачать")


@router.message(Command("getfileid"), admin_filter, F.photo)
async def test_get_file_id(message: Message):
    file_id = message.photo[-1].file_id
    await message.reply(f"🆔 <code>{esc(file_id)}</code>")


# ================= СПРАВКА =================
ADMIN_HELP_PAGES = [
    {
        "title": "🛠 Роли",
        "body": (
            f"{line('👑', 'Superadmin', 'полный доступ, роли')}\n"
            f"{line('🛡', 'Admin', 'карточки, юзеры, балансы')}\n"
            f"{line('🚫', 'Banned', 'блок в user-боте')}\n\n"
            f"Первый юзер в user-боте → superadmin автоматически."
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
        nav.append(InlineKeyboardButton(text="‹", callback_data=AdminHelpCallback(page=page - 1).pack()))
    nav.append(InlineKeyboardButton(text=f"{page + 1} / {total}", callback_data="ignore"))
    if page < total - 1:
        nav.append(InlineKeyboardButton(text="›", callback_data=AdminHelpCallback(page=page + 1).pack()))
    b.row(*nav)
    b.row(InlineKeyboardButton(text="‹ В меню", callback_data="admin_main"))
    return b.as_markup()


def build_admin_help_text(page: int) -> str:
    page = max(0, min(page, len(ADMIN_HELP_PAGES) - 1))
    p = ADMIN_HELP_PAGES[page]
    return f"<b>{p['title']}</b>\n{'─' * 20}\n\n{p['body']}"


@router.message(Command("adminhelp"), admin_filter)
async def admin_help(message: Message):
    await message.reply(build_admin_help_text(0), reply_markup=get_admin_help_keyboard(0))


@router.callback_query(AdminHelpCallback.filter())
async def admin_help_page(callback: CallbackQuery, callback_data: AdminHelpCallback):
    if not await is_admin(callback.from_user.id):
        await callback.answer("⚠️ Нет доступа")
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


# ================= START =================
ADMIN_COMMANDS = [
    BotCommand(command="start", description="⚙️ Панель"),
    BotCommand(command="admin", description="⚙️ Панель"),
    BotCommand(command="adminhelp", description="🛠 Справка"),
    BotCommand(command="stats", description="📊 Статистика"),
    BotCommand(command="addcard", description="➕ Карточка + фото"),
    BotCommand(command="delcard", description="🗑 Удалить карточку"),
    BotCommand(command="setcoins", description="🪙 Монеты"),
    BotCommand(command="setnick", description="✏️ Ник"),
    BotCommand(command="resetcd", description="⏱ Сброс CD"),
    BotCommand(command="ban", description="🚫 Бан"),
    BotCommand(command="unban", description="✅ Разбан"),
    BotCommand(command="setadmin", description="👑 Админ"),
    BotCommand(command="unsetadmin", description="❌ Снять"),
    BotCommand(command="migrate_photos", description="💾 Фото на диск"),
    BotCommand(command="sync_default_avatar", description="🖼 Дефолт-аватар"),
    BotCommand(command="getusers", description="👥 Список"),
]


async def main():
    os.makedirs(os.path.dirname(DB_NAME) or ".", exist_ok=True)
    if LOG_PATH:
        os.makedirs(os.path.dirname(LOG_PATH) or ".", exist_ok=True)
    await init_db_minimal()
    try:
        ensure_photo_dir()
    except Exception:
        pass

    bot = Bot(
        token=ADMIN_BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview=LinkPreviewOptions(is_disabled=True),
        ),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeDefault())
    logger.info("🛡 Админ-бот запущен (DB=%s)", DB_NAME)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
