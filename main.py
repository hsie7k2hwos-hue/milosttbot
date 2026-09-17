import asyncio
import html
import logging
import os
import random
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from io import BytesIO
from typing import Optional, Tuple

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, InputMediaPhoto, KeyboardButton, Message,
    ReplyKeyboardMarkup, LinkPreviewOptions,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

# PIL больше не нужен — заглушка через file_id (п.12)
# from PIL import Image, ImageDraw, ImageFont

# ================= КОНФИГУРАЦИЯ =================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_NAME = "/app/data/cards_game.db"
COOLDOWN_SECONDS = 4 * 3600
INSTANT_COST = 150  # максимум (полный кулдаун)
INSTANT_MIN_COST = 5  # минимум (кулдаун почти истёк)
NICKNAME_COST = 100
DUPLICATE_CHANCE = 0.25  # п.7 — шанс дубликата
DUPLICATE_REFUND = 0.5  # 50% от стоимости

# п.14 — авто-удаление сообщений бота о кулдауне в группах (в секундах)
GROUP_AUTODELETE_SECONDS = 30

# п.12 — заглушка вместо генерации аватарки. Замените на свой file_id.
DEFAULT_AVATAR_FILE_ID = "AgACAgIAAxkBAAID12qql3EFpnb2HwTCE7Yn_Ri1TQsNAAKRIGsbfHlYSVUpxfU75O60AQADAgADeAADPQQ"

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("/app/data/bot.log"), logging.StreamHandler()],
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
    # f"{n:,}" даёт '123,456' с запятыми — заменяем на неразрывный пробел
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


# ================= CALLBACK DATA =================
class RaritySelectCallback(CallbackData, prefix="coll_rarity"):
    rarity: str
    page: int = 0


class MainMenuCallback(CallbackData, prefix="coll_main"):
    pass


class BackToProfileCallback(CallbackData, prefix="back_to_profile"):
    pass


class AdminRarityCallback(CallbackData, prefix="admin_rarity"):
    card_id: int
    rarity: str


class NicknameCallback(CallbackData, prefix="nickname"):
    action: str


class CardActionCallback(CallbackData, prefix="card_action"):
    action: str
    user_id: int = 0


class TopCallback(CallbackData, prefix="top"):
    kind: str  # coins | cards | streak


class NickConfirmCallback(CallbackData, prefix="nickconf"):
    action: str  # apply | reset | cancel
    value: str = ""  # для apply — новый ник (url-safe? используем как есть)


class GenderCallback(CallbackData, prefix="gender"):
    value: str


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

        # Безопасные миграции (п."важно" — не ломаем прод)
        for table, column, definition in [
            ("users", "registration", "INTEGER DEFAULT 0"),
            ("users", "streak", "INTEGER DEFAULT 0"),
            ("users", "last_streak_date", "INTEGER DEFAULT 0"),
            ("users", "streak_bonus", "INTEGER DEFAULT 0"),
            ("users", "gender", "TEXT DEFAULT 'none'"),
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


async def get_or_create_user(user_id: int, username: str = None, full_name: str = None):
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


# ================= FSM =================
class AddCardSG(StatesGroup):
    photo = State()
    name = State()
    rarity = State()


class EditCardSG(StatesGroup):
    card_id = State()
    new_name = State()


router = Router()


# ================= КЛАВИАТУРЫ =================
def get_rarity_keyboard():
    b = InlineKeyboardBuilder()
    for key, val in RARITIES.items():
        b.button(text=val["name"], callback_data=f"set_rarity:{key}")
    b.button(text="❌ Отмена", callback_data="cancel_add_card")
    b.adjust(2)
    return b.as_markup()


def get_admin_main_kb():
    b = InlineKeyboardBuilder()
    b.button(text="➕ Добавить карточку", callback_data="admin_add_card")
    b.button(text="📜 Список карточек", callback_data="admin_list_cards")
    b.adjust(1)
    return b.as_markup()


def get_profile_kb():
    b = InlineKeyboardBuilder()
    b.button(text="🀄️ Мои карточки", callback_data="collection")
    b.button(text="⚧ Выбрать пол", callback_data=GenderCallback(value="menu").pack())
    b.button(text=f"✏️ Сменить ник ({NICKNAME_COST} 🪙)",
             callback_data=NicknameCallback(action="change").pack())
    b.adjust(1)
    return b.as_markup()


def get_gender_kb(current: str = "none") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key, info in GENDERS.items():
        mark = "✅ " if key == current else ""
        b.button(
            text=f"{mark}{info['icon']} {info['name']}",
            callback_data=GenderCallback(value=key).pack(),
        )
    b.button(text="🔙 Назад в профиль", callback_data=BackToProfileCallback().pack())
    b.adjust(1)
    return b.as_markup()


def get_main_km():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🀄️ Получить карточку"), KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="🏆 Топ игроков"), KeyboardButton(text="❓ Помощь")],
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
    # После выдачи карточки кулдаун полный -> максимальная цена
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


async def render_profile(bot: Bot, user_id: int):
    async with get_db() as db:
        cur = await db.execute("""
                               SELECT u.nickname,
                                      u.coins,
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

    nickname = row["nickname"] or f"User{user_id}"
    reg_date = datetime.fromtimestamp(row["registration"] or time.time()).strftime("%d.%m.%Y")

    role = row["role"] or "user"
    role_display = "👑 Администратор" if role == "admin" else "👤 Пользователь"

    gender = row["gender"] or "none"
    g = GENDERS.get(gender, GENDERS["none"])
    gender_display = f"{g['icon']} {g['name']}"

    caption = (
        f"👤 <b>Профиль</b> • {esc(nickname)}\n\n"
        f"🆔 ID • <code>{user_id}</code>\n"
        f"🎭 Роль • <b>{role_display}</b>\n"
        f"⚧ Пол • <b>{gender_display}</b>\n"
        f"📅 Регистрация • <b>{reg_date}</b>\n\n"
        f"🀄️ Карточек • <b>{fmt_num(row['cards_count'])} из {fmt_num(total_cards)}</b>\n"
        f"🪙 Монеты • <b>{fmt_num(row['coins'])}</b>\n"
        f"🔥 Стрик • <b>{fmt_days(row['streak'])}</b>"
    )
    return await get_user_photo(bot, user_id, nickname), caption, get_profile_kb()


async def render_collection(bot: Bot, user_id: int):
    keyboard, total, total_in_game = await get_collection_main_keyboard(user_id)
    nickname = await get_user_nickname(user_id)
    photo = await get_user_photo(bot, user_id, nickname)
    caption = (
        f"🀄️ <b>Ваши карточки</b>\n"
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

            # п.7 — если не всё собрано, стараемся дать новую, но с шансом DUPLICATE_CHANCE — дубликат
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
                # ищем новую карточку (не в инвентаре)
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
                    # fallback — любая не в инвентаре
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
                # всё собрано — берём случайную из инвентаря
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

            card_id, card_name, photo_id = card[0], card[1], card[2]
            base_coins = RARITIES[selected_rarity]["reward"]
            coins_earned = int(base_coins * DUPLICATE_REFUND) if is_duplicate else base_coins
            now = int(time.time())

            if check_cooldown:
                await db.execute(
                    """INSERT INTO users (user_id, last_claim, coins)
                       VALUES (?, ?, ?)
                       ON CONFLICT(user_id) DO UPDATE SET last_claim = ?,
                                                          coins      = coins + ?""",
                    (user_id, now, coins_earned, now, coins_earned),
                )
            else:
                await db.execute(
                    "UPDATE users SET coins = coins + ? WHERE user_id = ?",
                    (coins_earned, user_id),
                )

            # п.7.2 — учитываем количество
            await db.execute(
                """INSERT INTO inventory (user_id, card_id, claim_time, amount)
                   VALUES (?, ?, ?, 1)
                   ON CONFLICT(user_id, card_id) DO UPDATE SET amount     = amount + 1,
                                                               claim_time = ?""",
                (user_id, card_id, now, now),
            )

            cur = await db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
            balance = (await cur.fetchone())[0]

            return {
                "id": card_id, "name": card_name, "photo_id": photo_id,
                "rarity": selected_rarity, "coins_earned": coins_earned,
                "balance": balance, "is_duplicate": is_duplicate,
            }, "success"
    except Exception as e:
        logger.error(f"Ошибка выдачи карточки: {e}")
        return None, "error"


# ================= СТРИК =================
async def check_and_update_streak(user_id: int) -> Tuple[int, int, int]:
    """
    Обновляет стрик по факту захода (вызывается при любом получении карточки,
    в т.ч. на кулдауне). Возвращает (streak, bonus, balance):

      - первый заход за сегодня и streak стал 1  -> (1, 0, balance)
      - первый заход за сегодня и streak >= 2    -> (new_streak, new_bonus, new_balance)
      - заход уже был сегодня                    -> (current_streak, 0, balance)
      - пользователь не найден / ошибка          -> (0, 0, 0)
    """
    try:
        now = datetime.now()
        today_start = int(datetime(now.year, now.month, now.day).timestamp())
        today_end = today_start + 86400 - 1
        yesterday_start = int((now - timedelta(days=1)).replace(hour=0, minute=0, second=0).timestamp())
        yesterday_end = yesterday_start + 86400 - 1

        async with get_db() as db:
            cur = await db.execute(
                "SELECT streak, last_streak_date, streak_bonus, coins "
                "FROM users WHERE user_id = ?",
                (user_id,),
            )
            row = await cur.fetchone()
            if not row:
                return 0, 0, 0

            streak = row["streak"] or 0
            last_date = row["last_streak_date"] or 0
            balance = row["coins"] or 0

            # Сегодня уже заходил — просто возвращаем текущий стрик, без бонуса.
            # last_date == today означает, что стрик уже учтён сегодня.
            if today_start <= last_date <= today_end:
                return streak, 0, balance

            # Новый день. Считаем новый стрик.
            new_streak = (
                streak + 1
                if streak > 0 and yesterday_start <= last_date <= yesterday_end
                else 1
            )

            # п.2 — за 1-й день бонус не начисляется
            new_bonus = 0 if new_streak == 1 else next(
                b for d, b in STREAK_BONUSES if new_streak <= d
            )

            await db.execute(
                "UPDATE users SET streak = ?, last_streak_date = ?, "
                "streak_bonus = ?, coins = coins + ? WHERE user_id = ?",
                (new_streak, int(time.time()), new_bonus, new_bonus, user_id),
            )
            cur = await db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
            new_balance = (await cur.fetchone())[0]
            return new_streak, new_bonus, new_balance
    except Exception as e:
        logger.error(f"Ошибка обновления стрика: {e}")
        return 0, 0, 0


# ================= RATE LIMIT (п.11) =================
_rate_bucket: dict = {}


def rate_limited(key: str, limit: int, window: float) -> bool:
    now = time.monotonic()
    bucket = _rate_bucket.setdefault(key, [])
    bucket[:] = [t for t in bucket if now - t < window]
    if len(bucket) >= limit:
        return True
    bucket.append(now)
    return False


# ================= АВТО-УДАЛЕНИЕ СООБЩЕНИЯ (п.14) =================

async def _auto_delete(message: Message, delay: int):
    """Удаляет сообщение через delay секунд (тихо игнорирует ошибки)."""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    except Exception as e:
        logger.debug(f"auto_delete error: {e}")


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
async def cmd_help(message: Message):
    # п.3 — команда /help
    text = (
            "📖 <b>Помощь</b>\n\n"
            "<b>Основные команды:</b>\n"
            "/start — запуск бота\n"
            "/meow или «мряу» — получить карточку\n"
            "/profile — профиль\n"
            "/collection — мои карточки\n"
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
              "🔥 Заходите ежедневно — за стрик начисляются бонусные монеты."
    )
    await message.reply(text, reply_markup=get_main_km())


@router.message(F.text == "🀄️ Получить карточку")
@router.message(F.text.lower().strip() == "мряу")
@router.message(F.text.lower().strip() == "милость")
@router.message(Command("meow"))
async def get_card_handler(message: Message):
    user_id = message.from_user.id
    now = int(time.time())

    # п.11 — rate limit для групп
    if message.chat.type != "private":
        if rate_limited(f"card:{message.chat.id}", limit=5, window=10):
            return
    if rate_limited(f"card-user:{user_id}", limit=10, window=10):
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
            streak, bonus, new_balance = await check_and_update_streak(user_id)
            remaining = int(COOLDOWN_SECONDS - time_passed)
            h, m = remaining // 3600, (remaining % 3600) // 60
            s = remaining % 60

            # Формируем строку с оставшимся временем:
            # - если есть часы — показываем часы и минуты
            # - если часов нет, но есть минуты — показываем только минуты
            # - если и минут нет — показываем только секунды
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
            text += _streak_text(streak, bonus, new_balance)

            sent = await message.reply(
                text,
                reply_markup=get_card_action_keyboard(user_id, balance, remaining=remaining),
            )

            # п.14 — в группах авто-удаление сообщения о кулдауне через 30 секунд
            if message.chat.type != "private":
                asyncio.create_task(_auto_delete(sent, GROUP_AUTODELETE_SECONDS))
            return

        card, status = await issue_card(user_id, check_cooldown=True)
        if status != "success" or card is None:
            await message.reply("❌ <b>Произошла ошибка. Попробуйте позже.</b>")
            return

        streak, bonus, new_balance = await check_and_update_streak(user_id)

        caption = _card_caption(mention, card)
        caption += _streak_text(streak, bonus, new_balance)

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
        await message.reply("❌ <b>Произошла ошибка. Попробуйте позже.</b>")


def _streak_text(streak: int, bonus: int, new_balance: int) -> str:
    """Формирует текст про стрик для сообщения."""
    if bonus > 0 and streak == 1:
        return (
            "\n\n<blockquote>🔥 <b>Вы начали стрик!</b>\n"
            "💡 Заходите ежедневно, чтобы продлевать стрик и получать монеты</blockquote>"
        )
    if bonus > 0 and streak >= 2:
        return (
            f"\n\n<blockquote>🔥 Стрик • <b>{fmt_days(streak)}</b>\n"
            f"🪙 Бонус • +{fmt_num(bonus)} [{fmt_num(new_balance)}]\n"
            f"💡 Заходите ежедневно, чтобы продлевать стрик и получать монеты</blockquote>"
        )
    return ""


def _card_caption(mention: str, card: dict) -> str:
    r = RARITIES[card["rarity"]]
    title = "✨ Новая карточка" if not card.get("is_duplicate") else "🔁 Дубликат"
    return (
        f"{title} • <b>{esc(card['name'])}</b>\n\n"
        f"{r['icon']} Редкость • <b>{r['name']}</b>\n"
        f"🪙 Монеты • <b>+{fmt_num(card['coins_earned'])}</b> [{fmt_num(card['balance'])}]"
    )


# ---------- Профиль ----------
@router.message(F.text == "👤 Профиль")
@router.message(F.text.lower().strip() == "профиль")
@router.message(Command("profile"))
async def show_profile(message: Message):
    try:
        await get_or_create_user(
            message.from_user.id, message.from_user.username, message.from_user.full_name
        )
        photo, caption, kb = await render_profile(message.bot, message.from_user.id)
        try:
            await message.reply_photo(photo=photo, caption=caption, reply_markup=kb)
        except TelegramBadRequest as e:
            logger.error(f"profile photo bad request: {e}")
            await message.reply(caption, reply_markup=kb)
    except Exception as e:
        logger.error(f"Ошибка в show_profile: {e}")
        await message.reply("❌ <b>Произошла ошибка. Попробуйте позже.</b>")


@router.callback_query(BackToProfileCallback.filter())
async def process_back_to_profile(callback: CallbackQuery):
    try:
        photo, caption, kb = await render_profile(callback.message.bot, callback.from_user.id)
        await show_or_edit_photo(callback.message, photo, caption, kb)
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка возврата в профиль: {e}")
        await callback.answer("⚠️ Произошла ошибка")


# ---------- Смена ника (п.5.4/5.5/5.6) ----------
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
async def nickname_cmd(message: Message, command: Command):
    user_id = message.from_user.id
    await get_or_create_user(user_id, message.from_user.username, message.from_user.full_name)

    arg = (command.args or "").strip()

    # /nickname reset
    if arg.lower() == "reset":
        default = default_nickname(message.from_user.username, message.from_user.full_name, user_id)
        b = InlineKeyboardBuilder()
        b.button(text="✅ Подтвердить", callback_data=NickConfirmCallback(action="reset").pack())
        b.button(text="❌ Отмена", callback_data=NickConfirmCallback(action="cancel").pack())
        b.adjust(2)
        await message.reply(
            f"♻️ <b>Сбросить ник?</b>\n\n"
            f"Будет установлен: <b>{esc(default)}</b>\n"
            f"Сброс — <b>бесплатно</b>.",
            reply_markup=b.as_markup(),
        )
        return

    # /nickname [ник]
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

    # проверяем баланс
    row = await get_user_row(user_id)
    balance = row["coins"] if row else 0
    if balance < NICKNAME_COST:
        await message.reply(
            f"⚠️ Недостаточно монет. Нужно <b>{fmt_num(NICKNAME_COST)} 🪙</b>, у вас <b>{fmt_num(balance)} 🪙</b>."
        )
        return

    b = InlineKeyboardBuilder()
    # value передаём как есть — callback_data должна быть короткой, ник <=32
    b.button(
        text="✅ Подтвердить",
        callback_data=NickConfirmCallback(action="apply", value=new_nick).pack(),
    )
    b.button(text="❌ Отмена", callback_data=NickConfirmCallback(action="cancel").pack())
    b.adjust(2)
    await message.reply(
        f"✏️ <b>Сменить ник?</b>\n\n"
        f"Новый ник: <b>{esc(new_nick)}</b>\n"
        f"Стоимость: <b>{NICKNAME_COST} 🪙</b> (баланс: {balance})",
        reply_markup=b.as_markup(),
    )


@router.callback_query(NickConfirmCallback.filter())
async def nickname_confirm(callback: CallbackQuery, callback_data: NickConfirmCallback):
    user_id = callback.from_user.id

    if callback_data.action == "cancel":
        await callback.message.edit_text("✅ <b>Отменено</b>")
        await callback.answer()
        return

    if callback_data.action == "reset":
        default = default_nickname(
            callback.from_user.username, callback.from_user.full_name, user_id
        )
        async with get_db() as db:
            await db.execute("UPDATE users SET nickname = ? WHERE user_id = ?", (default, user_id))
        await callback.message.edit_text(f"✅ <b>Ник сброшен:</b> {esc(default)}")
        await callback.answer()
        return

    if callback_data.action == "apply":
        new_nick = callback_data.value
        if not validate_nickname(new_nick):
            await callback.message.edit_text("❌ <b>Неверный ник</b>")
            await callback.answer()
            return

        async with get_db() as db:
            cur = await db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            balance = row["coins"] if row else 0
            if balance < NICKNAME_COST:
                await callback.message.edit_text(
                    f"⚠️ Недостаточно монет. Нужно <b>{fmt_num(NICKNAME_COST)} 🪙</b>, у вас <b>{fmt_num(balance)} 🪙</b>."
                )
                await callback.answer()
                return
            await db.execute(
                "UPDATE users SET nickname = ?, coins = coins - ? WHERE user_id = ?",
                (new_nick, NICKNAME_COST, user_id),
            )
        await callback.message.edit_text(
            f"✅ <b>Ник изменён:</b> {esc(new_nick)}\n"
            f"Списано: <b>{NICKNAME_COST} 🪙</b>"
        )
        await callback.answer()


# Старый вход «Изменить ник» из профиля — теперь просто подсказка
@router.callback_query(NicknameCallback.filter(F.action == "change"))
async def change_nickname_hint(callback: CallbackQuery):
    await callback.answer(
        f"Для изменения ника используйте команду /nickname НовыйНик ({NICKNAME_COST} 🪙) "
        f"или /nickname reset для сброса",
        show_alert=True,
    )


# ---------- Смена пола ----------
@router.callback_query(GenderCallback.filter(F.value == "menu"))
async def gender_menu(callback: CallbackQuery):
    user_id = callback.from_user.id
    async with get_db() as db:
        cur = await db.execute("SELECT gender FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
    current = (row["gender"] if row and row["gender"] else "none")
    await callback.message.edit_caption(
        caption="⚧ <b>Выберите пол</b>\n\n<i>Это необязательное поле — можно оставить «Не задан».</i>",
        reply_markup=get_gender_kb(current),
    )
    await callback.answer()


@router.callback_query(GenderCallback.filter(F.value != "menu"))
async def gender_set(callback: CallbackQuery, callback_data: GenderCallback):
    user_id = callback.from_user.id
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
        reply_markup=get_gender_kb(value),
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
        await message.reply("⚧ <b>Выберите пол:</b>", reply_markup=get_gender_kb(current))
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


# ---------- Топ (п.6) ----------
async def build_top_text(kind: str, current_user_id: int) -> str:
    """Возвращает готовый текст топа с позицией пользователя."""
    limit = 10
    async with get_db() as db:
        if kind == "cards":
            # топ по сумме amount
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

        # текущий ник пользователя для отображения
        cur = await db.execute("SELECT nickname FROM users WHERE user_id = ?", (current_user_id,))
        my_row2 = await cur.fetchone()
        my_nick = my_row2["nickname"] if my_row2 and my_row2["nickname"] else f"User{current_user_id}"

    medals = ["🥇", "🥈", "🥉"]
    text = f"<b>{title}</b>\n\n"
    for i, row in enumerate(top, 1):
        medal = medals[i - 1] if i <= 3 else f"{i}."
        nick = row["nickname"] or f"User{row['user_id']}"
        # п.8 — упоминание. username у нас нет в БД, даём ссылку по id.
        mention = user_mention(row["user_id"], nick, None)
        text += f"{medal} {mention} • <b>{fmt_num(row['value'])}</b> {unit}\n"

    text += f"\n📌 Ваше место: <b>#{fmt_num(my_rank)}</b> — {esc(my_nick)} • <b>{fmt_num(my_value)}</b> {unit}"
    return text


@router.message(F.text == "🏆 Топ игроков")
@router.message(Command("top"))
async def show_top_players(message: Message):
    if rate_limited(f"top:{message.from_user.id}", limit=3, window=5):
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
            # Пропускаем редкость, если у пользователя 0 карточек
            if user_amount == 0:
                continue
            cur = await db.execute("SELECT COUNT(*) FROM cards WHERE rarity = ?", (r_key,))
            total_of_rarity = (await cur.fetchone())[0]
            rows.append([
                InlineKeyboardButton(
                    text=f"{r_info['icon']} {r_info['name']} ({fmt_num(user_amount)}/{fmt_num(total_of_rarity)})",
                    callback_data=RaritySelectCallback(rarity=r_key, page=0).pack(),
                )
            ])
        rows.append([
            InlineKeyboardButton(
                text="👤 Перейти в профиль",
                callback_data=BackToProfileCallback().pack(),
            )
        ])
        return InlineKeyboardMarkup(inline_keyboard=rows), total_cards, total_in_game


@router.message(Command("collection"))
@router.callback_query(F.data == "collection")
async def show_collection(event):
    callback = event if isinstance(event, CallbackQuery) else None
    message = event.message if callback else event
    user_id = event.from_user.id
    try:
        photo, caption, keyboard, total = await render_collection(message.bot, user_id)
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
    user_id, rarity, page = callback.from_user.id, callback_data.rarity, callback_data.page
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
        # п.7.2 — количество
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
                callback_data=RaritySelectCallback(rarity=rarity, page=page - 1).pack(),
            ))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="ignore"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(
                text="▶️",
                callback_data=RaritySelectCallback(rarity=rarity, page=page + 1).pack(),
            ))

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            nav,
            [InlineKeyboardButton(text="🔙 К категориям", callback_data=MainMenuCallback().pack())],
        ])
        await show_or_edit_photo(callback.message, card["photo_id"], caption, keyboard)
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка просмотра коллекции: {e}")
        await callback.answer("⚠️ Произошла ошибка")


@router.callback_query(MainMenuCallback.filter())
async def process_back_to_main(callback: CallbackQuery):
    photo, caption, keyboard, _ = await render_collection(
        callback.message.bot, callback.from_user.id
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
        await callback.answer("⚠️ Кнопка предназначена не для вас")
        return

    if rate_limited(f"card-action:{user_id}", limit=6, window=10):
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

            # Если кулдаун уже прошёл — пользователь мог бы получить карточку бесплатно.
            # Тогда просто перенаправляем на обычную выдачу.
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

            streak, bonus, new_balance = await check_and_update_streak(user_id)

            caption = _card_caption(mention, card)
            caption += _streak_text(streak, bonus, new_balance)

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


# ================= АДМИН-ПАНЕЛЬ =================
async def admin_filter(message: Message) -> bool:
    return await is_admin(message.from_user.id)


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


@router.callback_query(F.data == "admin_list_cards")
async def list_cards(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Ошибка доступа")
        return
    async with get_db() as db:
        cur = await db.execute("SELECT id, name, rarity FROM cards")
        cards = await cur.fetchall()
    if not cards:
        await call.message.answer("🔴 <b>Пока нет карточек</b>")
        await call.answer()
        return
    b = InlineKeyboardBuilder()
    for c_id, name, rarity in cards:
        r_name = RARITIES.get(rarity, {}).get("name", rarity)
        b.button(text=f"{name} ({r_name})", callback_data=f"card_manage:{c_id}")
    b.adjust(1)
    await call.message.answer("🀄️ <b>Выберите карточку</b>", reply_markup=b.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("card_manage:"))
async def manage_single_card(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⚠️ Ошибка доступа")
        return
    card_id = int(call.data.split(":")[1])
    async with get_db() as db:
        cur = await db.execute("SELECT name, rarity, photo_id FROM cards WHERE id = ?", (card_id,))
        card = await cur.fetchone()
    if not card:
        await call.message.answer("🔴 <b>Карточка не найдена</b>")
        await call.answer()
        return
    r_name = RARITIES.get(card["rarity"], {}).get("name", card["rarity"])
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Изменить название", callback_data=f"edit_name:{card_id}")
    b.button(text="🎲 Изменить редкость", callback_data=f"edit_rarity:{card_id}")
    b.button(text="❌ Удалить", callback_data=f"delete_card:{card_id}")
    b.button(text="🔙 Назад", callback_data="admin_list_cards")
    b.adjust(1)
    try:
        await call.message.answer_photo(
            photo=card["photo_id"],
            caption=f"🀄️ <b>{esc(card['name'])}</b>\n\n🎲 <b>{r_name}</b>\n🆔 {card_id}",
            reply_markup=b.as_markup(),
        )
    except TelegramBadRequest as e:
        logger.error(f"manage card photo error: {e}")
        await call.message.answer(
            f"🀄️ <b>{esc(card['name'])}</b>\n\n🎲 <b>{r_name}</b>\n🆔 {card_id}",
            reply_markup=b.as_markup(),
        )
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
    b.button(text="🔙 Назад", callback_data=f"card_manage:{card_id}")
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


@router.message(Command("adminhelp"), admin_filter)
async def admin_help(message: Message):
    """
    Скрытая справка для администраторов.
    Не упоминается в общей /help. Доступна только role='admin'.
    """
    text = (
        "🛠 <b>Справка администратора</b>\n\n"

        "<b>👑 Управление правами:</b>\n"
        "<code>/setadmin USERID</code> — назначить админа\n"
        "<code>/unsetadmin USERID</code> — разжаловать админа\n"
        "<i>Только в ЛС. Нельзя разжаловать себя.</i>\n\n"

        "<b>🀄️ Управление карточками:</b>\n"
        "<code>/admin</code> — панель администратора\n"
        "  • ➕ Добавить карточку\n"
        "  • 📜 Список карточек (редактирование/удаление)\n\n"

        "<b>🧪 Тестовые команды:</b>\n"
        "<code>/reset_all_nicknames</code> — сбросить ники всех "
        "(Имя → username → ID)\n"
        "<code>/promote_to_mythical</code> — перевести 10 случайных "
        "epic/legendary в mythical\n"
        "<code>/getfileid</code> — получить file_id картинки "
        "(отправьте фото с командой в подписи)\n"
        "<code>/denominate_coins</code> — разделить баланс всех на 10\n\n"

        "<b>⌨️ Общие команды:</b>\n"
        "<code>/cancel</code> — отменить текущую FSM-операцию\n"
        "<code>/adminhelp</code> — эта справка\n\n"

        "💡 <i>Команда скрыта из общей справки и доступна только "
        "пользователям с ролью admin.</i>"
    )
    await message.reply(text)


@router.message(Command("setadmin"), admin_filter)
async def set_admin_cmd(message: Message, command: Command):
    """
    /setadmin USERID — назначить пользователя администратором.
    Доступно только текущим админам.
    """
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
    """
    /unsetadmin USERID — разжаловать администратора.
    Доступно только текущим админам.
    """
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

    # Защита от разжалования самого себя — иначе можно потерять доступ
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


# ================= [TEST] ТЕСТОВЫЕ КОМАНДЫ =================

# [TEST] п.5 — сброс ников у всех пользователей по той же логике,
# что и для новых: Имя -> Юзернейм -> ID
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
            # Если Telegram недоступен для конкретного пользователя —
            # ничего страшного, просто перейдём к ID
            logger.warning(f"[TEST] get_chat failed for {uid}: {e}")

        new_nick = default_nickname(username, full_name, uid)
        async with get_db() as db:
            await db.execute(
                "UPDATE users SET nickname = ? WHERE user_id = ?",
                (new_nick, uid),
            )
        count += 1

    await message.answer(f"✅ <b>[TEST] Сброшено ников:</b> {count}")


# [TEST] п.10 — перевод части эпических/легендарных в мифическую
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


# [TEST] Получить file_id отправленной картинки (для DEFAULT_AVATAR_FILE_ID)
@router.message(Command("getfileid"), admin_filter, F.photo)
async def test_get_file_id(message: Message):
    # Берём самое большое по размеру фото
    file_id = message.photo[-1].file_id
    await message.reply(
        f"🆔 <b>file_id:</b>\n<code>{esc(file_id)}</code>"
    )


# [TEST] Деноминация: урезать баланс каждого пользователя в 10 раз
@router.message(Command("denominate_coins"), admin_filter)
async def test_denominate_coins(message: Message):
    async with get_db() as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        total = (await cur.fetchone())[0]

        # Целочисленное деление на 10 (округление вниз).
        # Если нужно округление к ближайшему — замените на
        # CAST(ROUND(coins / 10.0) AS INTEGER).
        await db.execute("UPDATE users SET coins = coins / 10")

        cur = await db.execute("SELECT COALESCE(SUM(coins), 0) FROM users")
        new_total_coins = (await cur.fetchone())[0]

    await message.answer(
        f"✅ <b>[TEST] Деноминация выполнена</b>\n\n"
        f"👥 Пользователей: <b>{total}</b>\n"
        f"🪙 Суммарный баланс после: <b>{new_total_coins}</b>"
    )


# [TEST] Проставить дату регистрации текущим временем тем,
# у кого она не установлена (registration = 0)
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
