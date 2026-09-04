import asyncio
import logging
import os
import random
import time
from datetime import datetime, timedelta
from typing import Optional, Tuple
from contextlib import asynccontextmanager

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    InputMediaPhoto,
    ReplyKeyboardMarkup,
    KeyboardButton,
    UserProfilePhotos,
    BufferedInputFile
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
import aiohttp
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont

# ================= КОНФИГУРАЦИЯ =================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_NAME = "/app/data/cards_game.db"
COOLDOWN_SECONDS = 3 * 3600  # 3 часа в секундах
INSTANT_COST = 5000  # Стоимость мгновенного получения карточки

# Редкости и их шансы
RARITIES = {
    "common": {"name": "⚪ Обычная", "weight": 60, "coins": 100},
    "rare": {"name": "🔵 Редкая", "weight": 25, "coins": 200},
    "epic": {"name": "🟣 Эпическая", "weight": 10, "coins": 300},
    "legendary": {"name": "🟡 Легендарная", "weight": 5, "coins": 500},
}

# Бонусы за стрик (день, бонус)
STREAK_BONUSES = [
    (1, 100),   # до 7 дней
    (7, 100),
    (14, 200),
    (30, 300),
    (float('inf'), 500)  # от 30 дней
]

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/app/data/bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# --- CallbackData для обработки нажатий ---
class RaritySelectCallback(CallbackData, prefix="coll_rarity"):
    rarity: str
    page: int = 0


class MainMenuCallback(CallbackData, prefix="coll_main"):
    pass


class AdminRarityCallback(CallbackData, prefix="admin_rarity"):
    card_id: int
    rarity: str


class NicknameCallback(CallbackData, prefix="nickname"):
    action: str


class CardActionCallback(CallbackData, prefix="card_action"):
    action: str  # "instant" или "collection" или "another"
    user_id: int = 0  # ID пользователя, которому адресовано сообщение


class StreakCallback(CallbackData, prefix="streak"):
    pass


# ================= РАБОТА С БАЗОЙ ДАННЫХ =================
@asynccontextmanager
async def get_db():
    """Контекстный менеджер для работы с БД (одно соединение на операцию)"""
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
    """Инициализация базы данных с индексами"""
    async with get_db() as db:
        # Таблица карточек
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cards
            (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                name     TEXT NOT NULL,
                rarity   TEXT NOT NULL,
                photo_id TEXT NOT NULL
            )
        """)
        
        # Таблица пользователей с ролью
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
                streak_bonus     INTEGER DEFAULT 0
            )
        """)
        
        # Инвентарь пользователей
        await db.execute("""
            CREATE TABLE IF NOT EXISTS inventory
            (
                user_id    INTEGER,
                card_id    INTEGER,
                claim_time INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, card_id),
                FOREIGN KEY (card_id) REFERENCES cards (id) ON DELETE CASCADE
            )
        """)
        
        # Индексы для оптимизации запросов
        await db.execute("CREATE INDEX IF NOT EXISTS idx_cards_rarity ON cards(rarity)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_inventory_user ON inventory(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_inventory_claim_time ON inventory(claim_time)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_coins ON users(coins)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_streak ON users(streak)")
        
        # Миграции
        migrations = [
            ("users", "registration", "INTEGER DEFAULT 0"),
            ("users", "streak", "INTEGER DEFAULT 0"),
            ("users", "last_streak_date", "INTEGER DEFAULT 0"),
            ("users", "streak_bonus", "INTEGER DEFAULT 0"),
            ("inventory", "claim_time", "INTEGER DEFAULT 0"),
        ]
        
        for table, column, definition in migrations:
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                logger.info(f"Миграция: добавлена колонка {column} в {table}")
            except aiosqlite.OperationalError:
                pass


async def is_admin(user_id: int) -> bool:
    """Проверка, является ли пользователь админом"""
    try:
        async with get_db() as db:
            cursor = await db.execute("SELECT role FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            return row is not None and row[0] == 'admin'
    except Exception as e:
        logger.error(f"Ошибка проверки админа: {e}")
        return False


async def get_or_create_user(user_id: int, username: str = None, full_name: str = None):
    """Получение или создание пользователя"""
    try:
        async with get_db() as db:
            cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            user = await cursor.fetchone()

            if not user:
                nickname = username or full_name or f"User{user_id}"
                now = int(time.time())
                await db.execute(
                    "INSERT INTO users (user_id, nickname, registration, streak, last_streak_date) VALUES (?, ?, ?, ?, ?)",
                    (user_id, nickname, now, 0, 0)
                )
                cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
                user = await cursor.fetchone()

            return user
    except Exception as e:
        logger.error(f"Ошибка получения/создания пользователя: {e}")
        raise


async def create_avatar_photo(initials: str) -> BufferedInputFile:
    """Создает квадратное изображение с инициалами на цветном фоне"""
    size = 200
    colors = [
        (66, 133, 244), (52, 168, 83), (251, 188, 5), (234, 67, 53),
        (156, 39, 176), (0, 188, 212), (255, 152, 0), (233, 30, 99)
    ]
    
    color_index = sum(ord(c) for c in initials) % len(colors)
    bg_color = colors[color_index]
    
    image = Image.new('RGB', (size, size), bg_color)
    draw = ImageDraw.Draw(image)
    
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 80)
    except:
        font = ImageFont.load_default()
    
    text = initials.upper()[:2]
    text_bbox = draw.textbbox((0, 0), text, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]
    x = (size - text_width) // 2
    y = (size - text_height) // 2 - 10
    
    draw.text((x, y), text, fill=(255, 255, 255), font=font)
    
    buffer = BytesIO()
    image.save(buffer, format='PNG')
    buffer.seek(0)
    
    return BufferedInputFile(buffer.read(), filename="avatar.png")


# ================= FSM (СОСТОЯНИЯ ДЛЯ АДМИНКИ) =================
class AddCardSG(StatesGroup):
    photo = State()
    name = State()
    rarity = State()


class EditCardSG(StatesGroup):
    card_id = State()
    new_name = State()


class NicknameSG(StatesGroup):
    new_nickname = State()


# ================= РОУТЕРЫ =================
router = Router()


# ================= ВСПОМОГАТЕЛЬНЫЕ КЛАВИАТУРЫ =================
def get_rarity_keyboard():
    builder = InlineKeyboardBuilder()
    for key, val in RARITIES.items():
        builder.button(text=val["name"], callback_data=f"set_rarity:{key}")
    builder.button(text="❌ Отмена", callback_data="cancel_add_card")
    builder.adjust(2)
    return builder.as_markup()


def get_admin_main_kb():
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить карточку", callback_data="admin_add_card")
    builder.button(text="📜 Список карточек", callback_data="admin_list_cards")
    builder.adjust(1)
    return builder.as_markup()


def get_profile_kb():
    builder = InlineKeyboardBuilder()
    builder.button(text="📦 Моя коллекция", callback_data="collection")
    builder.button(text="✏️ Изменить ник", callback_data=NicknameCallback(action="change").pack())
    builder.button(text="🏆 Топ игроков", callback_data="top_players")
    builder.adjust(1)
    return builder.as_markup()


def get_main_km():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🃏 Получить карточку"),
                KeyboardButton(text="👤 Профиль"),
            ],
            [
                KeyboardButton(text="🏆 Топ игроков"),
            ]
        ],
        resize_keyboard=True,
    )
    return keyboard


def get_card_action_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура для действий с карточкой при кулдауне"""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✨ Получить сейчас (5000💰)", 
        callback_data=CardActionCallback(action="instant", user_id=user_id).pack()
    )
    builder.button(
        text="📦 Моя коллекция", 
        callback_data=CardActionCallback(action="collection", user_id=user_id).pack()
    )
    builder.adjust(1)
    return builder.as_markup()


def get_after_card_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура после получения карточки"""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🃏 Получить ещё одну (5000💰)", 
        callback_data=CardActionCallback(action="another", user_id=user_id).pack()
    )
    builder.button(
        text="📦 Моя коллекция", 
        callback_data=CardActionCallback(action="collection", user_id=user_id).pack()
    )
    builder.adjust(1)
    return builder.as_markup()


# ================= ФУНКЦИЯ ВЫДАЧИ КАРТОЧКИ =================
async def issue_card(user_id: int, username: str = None, check_cooldown: bool = True) -> Tuple[dict, str]:
    """
    Выдача случайной карточки пользователю.
    Возвращает (карточка, редкость) или None если все карточки собраны.
    """
    try:
        async with get_db() as db:
            # Проверяем, все ли карточки уже получены
            cursor = await db.execute("SELECT COUNT(*) FROM cards")
            total_cards = (await cursor.fetchone())[0]
            
            cursor = await db.execute(
                "SELECT COUNT(DISTINCT card_id) FROM inventory WHERE user_id = ?",
                (user_id,)
            )
            user_cards_count = (await cursor.fetchone())[0]
            
            if user_cards_count >= total_cards:
                return None, "all_collected"
            
            # Выбор редкости на основе весов
            rarities_list = list(RARITIES.keys())
            weights = [RARITIES[r]["weight"] for r in rarities_list]
            
            # Пытаемся найти карточку выбранной редкости
            card = None
            selected_rarity = None
            
            for _ in range(10):  # До 10 попыток найти карточку
                selected_rarity = random.choices(rarities_list, weights=weights, k=1)[0]
                
                cursor = await db.execute(
                    """
                    SELECT id, name, photo_id, rarity 
                    FROM cards 
                    WHERE rarity = ? AND id NOT IN (
                        SELECT card_id FROM inventory WHERE user_id = ?
                    )
                    ORDER BY RANDOM() 
                    LIMIT 1
                    """,
                    (selected_rarity, user_id)
                )
                card = await cursor.fetchone()
                
                if card:
                    break
            
            # Если не нашли по редкости, берем любую доступную
            if not card:
                cursor = await db.execute(
                    """
                    SELECT id, name, photo_id, rarity 
                    FROM cards 
                    WHERE id NOT IN (
                        SELECT card_id FROM inventory WHERE user_id = ?
                    )
                    ORDER BY RANDOM() 
                    LIMIT 1
                    """,
                    (user_id,)
                )
                card = await cursor.fetchone()
                if card:
                    selected_rarity = card[3]
            
            if not card:
                return None, "all_collected"
            
            card_id, card_name, photo_id = card[0], card[1], card[2]
            coins_earned = RARITIES[selected_rarity]["coins"]
            now = int(time.time())
            
            # Транзакция: обновляем пользователя и добавляем карточку
            await db.execute("BEGIN TRANSACTION")
            try:
                if check_cooldown:
                    # Обычная выдача с обновлением last_claim
                    await db.execute(
                        """
                        INSERT INTO users (user_id, last_claim, coins) 
                        VALUES (?, ?, ?) 
                        ON CONFLICT(user_id) DO UPDATE SET 
                            last_claim = ?, 
                            coins = coins + ?
                        """,
                        (user_id, now, coins_earned, now, coins_earned)
                    )
                else:
                    # Мгновенная выдача без обновления last_claim (уже обновлен)
                    await db.execute(
                        """
                        UPDATE users SET coins = coins + ? WHERE user_id = ?
                        """,
                        (coins_earned, user_id)
                    )
                
                await db.execute(
                    "INSERT INTO inventory (user_id, card_id, claim_time) VALUES (?, ?, ?)",
                    (user_id, card_id, now)
                )
                await db.execute("COMMIT")
            except Exception as e:
                await db.execute("ROLLBACK")
                logger.error(f"Ошибка транзакции выдачи карточки: {e}")
                raise
            
            # Получаем обновленный баланс
            cursor = await db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
            balance_row = await cursor.fetchone()
            balance = balance_row[0] if balance_row else 0
            
            card_data = {
                "id": card_id,
                "name": card_name,
                "photo_id": photo_id,
                "rarity": selected_rarity,
                "coins_earned": coins_earned,
                "balance": balance
            }
            
            return card_data, "success"
            
    except Exception as e:
        logger.error(f"Ошибка выдачи карточки: {e}")
        return None, "error"


# ================= ФУНКЦИЯ ОБНОВЛЕНИЯ СТРИКА =================
async def check_and_update_streak(user_id: int) -> Tuple[int, int, int]:
    """
    Проверяет и обновляет стрик пользователя.
    Возвращает (текущий_стрик, бонус_за_стрик, баланс)
    Если бонус уже был начислен сегодня, возвращает (0, 0, 0)
    """
    try:
        now = datetime.now()
        today_start = int(datetime(now.year, now.month, now.day).timestamp())
        today_end = today_start + 86400 - 1
        yesterday_start = int((now - timedelta(days=1)).replace(hour=0, minute=0, second=0).timestamp())
        yesterday_end = yesterday_start + 86400 - 1
        
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT streak, last_streak_date, streak_bonus, coins FROM users WHERE user_id = ?",
                (user_id,)
            )
            row = await cursor.fetchone()
            
            if not row:
                return 0, 0, 0
            
            streak = row["streak"]
            last_streak_date = row["last_streak_date"]
            streak_bonus = row["streak_bonus"]
            balance = row["coins"]
            
            # Если сегодня уже начисляли бонус - не уведомляем повторно
            if today_start <= last_streak_date <= today_end:
                return 0, 0, 0
            
            # Проверяем, получал ли пользователь карточку сегодня
            cursor = await db.execute(
                "SELECT MAX(claim_time) FROM inventory WHERE user_id = ?",
                (user_id,)
            )
            last_claim_row = await cursor.fetchone()
            last_claim = last_claim_row[0] if last_claim_row[0] else 0
            
            # Если пользователь получал карточку сегодня
            if last_claim >= today_start:
                # Определяем новый стрик
                if streak > 0 and yesterday_start <= last_streak_date <= yesterday_end:
                    # Продолжаем стрик
                    new_streak = streak + 1
                else:
                    # Начинаем новый стрик (или после перерыва)
                    new_streak = 1
                
                # Рассчитываем бонус за стрик
                new_bonus = 0
                for days, bonus in STREAK_BONUSES:
                    if new_streak <= days:
                        new_bonus = bonus
                        break
                
                # Обновляем стрик и начисляем бонус
                await db.execute(
                    """
                    UPDATE users 
                    SET streak = ?, 
                        last_streak_date = ?, 
                        streak_bonus = ?,
                        coins = coins + ?
                    WHERE user_id = ?
                    """,
                    (new_streak, int(time.time()), new_bonus, new_bonus, user_id)
                )
                
                # Получаем обновленный баланс
                cursor = await db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
                balance_row = await cursor.fetchone()
                new_balance = balance_row[0] if balance_row else balance
                
                return new_streak, new_bonus, new_balance
            else:
                # Если не получал сегодня - сбрасываем стрик
                if streak > 0:
                    await db.execute(
                        "UPDATE users SET streak = 0, streak_bonus = 0 WHERE user_id = ?",
                        (user_id,)
                    )
                return 0, 0, 0
                
    except Exception as e:
        logger.error(f"Ошибка обновления стрика: {e}")
        return 0, 0, 0


# ================= ПОЛЬЗОВАТЕЛЬСКАЯ ЛОГИКА =================

@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message):
    try:
        await get_or_create_user(
            message.from_user.id,
            message.from_user.username,
            message.from_user.full_name
        )

        sticker_file_id = "CAACAgIAAxkBAALL7WqWuuWDYuQk4iqY7tNu_-7zLZqyAAJengACoj5pSQH9iX-5QhicPQQ"
        await message.answer_sticker(sticker=sticker_file_id)
        await message.reply(
            "<blockquote><b>👋 Привет! Отправь команду «милость», чтобы получить милую карточку</b></blockquote>",
            reply_markup=get_main_km()
        )
        logger.info(f"Пользователь {message.from_user.id} запустил бота")
    except Exception as e:
        logger.error(f"Ошибка в cmd_start: {e}")


@router.message(F.text == "🃏 Получить карточку")
@router.message(F.text.lower().strip() == "милость")
@router.message(F.text.lower().strip() == "мряу")
@router.message(Command("card"))
async def get_card_handler(message: Message):
    user_id = message.from_user.id
    now = int(time.time())
    
    try:
        # Получаем или создаем пользователя
        await get_or_create_user(
            user_id,
            message.from_user.username,
            message.from_user.full_name
        )
        
        # Проверка кулдауна
        async with get_db() as db:
            cursor = await db.execute("SELECT last_claim, coins FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            last_claim = row["last_claim"] if row else 0
            balance = row["coins"] if row else 0
        
        time_passed = now - last_claim
        if time_passed < COOLDOWN_SECONDS:
            remaining = int(COOLDOWN_SECONDS - time_passed)
            hours = remaining // 3600
            minutes = (remaining % 3600) // 60
            seconds = remaining % 60
            
            await message.reply(
                f"<blockquote>⏳ Следующую карточку можно будет получить через: <b>{hours}ч {minutes}м {seconds}с</b>\n\n"
                f"Или получи сейчас за <b>{INSTANT_COST} монет</b> (у тебя <b>{balance}</b> монет)</blockquote>",
                reply_markup=get_card_action_keyboard(user_id)
            )
            return
        
        # Выдача карточки
        card_data, status = await issue_card(user_id, message.from_user.username, check_cooldown=True)
        
        if status == "all_collected":
            await message.reply(
                "<blockquote><b>🎉 Ты собрал все доступные карточки! Ожидай добавления новых.</b></blockquote>"
            )
            return
        elif status == "error" or card_data is None:
            await message.reply(
                "<blockquote><b>❌ Произошла ошибка. Попробуйте позже.</b></blockquote>"
            )
            return
        
        # Отправляем карточку
        rarity_title = RARITIES[card_data["rarity"]]["name"]
        caption = (
            f"<blockquote><b>💙 {message.from_user.first_name}</b>, тебе выпала новая карточка: <b>{card_data['name']}</b>\n\n"
            f"🎲 Редкость: <b>{rarity_title}</b>\n"
            f"💰 Монеты: <b>+{card_data['coins_earned']} [{card_data['balance']}]</b></blockquote>"
        )
        
        await message.reply_photo(
            photo=card_data["photo_id"], 
            caption=caption, 
            reply_markup=get_after_card_keyboard(user_id)
        )
        
        # Проверяем стрик
        streak, bonus, new_balance = await check_and_update_streak(user_id)
        if bonus > 0 and streak > 0:
            await message.reply(
                f"<blockquote><b>🔥 Стрик {streak} день!\n"
                f"💰 Бонус за стрик: +{bonus} монет\n"
                f"💳 Баланс: {new_balance} монет</b></blockquote>"
            )
            
    except Exception as e:
        logger.error(f"Ошибка в get_card_handler: {e}")
        await message.reply("<blockquote><b>❌ Произошла ошибка. Попробуйте позже.</b></blockquote>")


@router.message(F.text == "👤 Профиль")
@router.message(F.text.lower().strip() == "профиль")
@router.message(Command("profile"))
async def show_profile(message: Message):
    user_id = message.from_user.id
    
    try:
        user = await get_or_create_user(
            user_id,
            message.from_user.username,
            message.from_user.full_name
        )
        
        # Получаем данные пользователя
        async with get_db() as db:
            cursor = await db.execute("""
                SELECT u.nickname, u.coins, u.registration, u.streak, u.streak_bonus,
                       COUNT(i.card_id) as cards_count
                FROM users u
                LEFT JOIN inventory i ON u.user_id = i.user_id
                WHERE u.user_id = ?
                GROUP BY u.user_id
            """, (user_id,))
            row = await cursor.fetchone()
            
            cursor = await db.execute("SELECT COUNT(*) FROM cards")
            total_cards = (await cursor.fetchone())[0]
        
        nickname = row["nickname"] or message.from_user.full_name
        coins = row["coins"]
        registration = row["registration"] or int(time.time())
        streak = row["streak"]
        streak_bonus = row["streak_bonus"]
        cards_count = row["cards_count"]
        
        reg_date = datetime.fromtimestamp(registration).strftime("%d.%m.%Y %H:%M")
        
        # Пытаемся получить фото профиля пользователя
        try:
            photos = await message.bot.get_user_profile_photos(user_id, limit=1)
            if photos.total_count > 0:
                photo = photos.photos[0][-1]
                await message.reply_photo(
                    photo=photo.file_id,
                    caption=f"<blockquote>👤 Тебя зовут <b>{nickname}</b>\n\n"
                            f"🆔 ID: <code>{user_id}</code>\n"
                            f"💰 Баланс: <b>{coins} монет</b>\n"
                            f"🃏 Карточек: <b>{cards_count}/{total_cards}</b>\n"
                            f"📅 Регистрация: <b>{reg_date}</b>\n"
                            f"🔥 Стрик: <b>{streak} дней</b> (бонус: +{streak_bonus} монет/день)</blockquote>",
                    reply_markup=get_profile_kb()
                )
                return
        except:
            pass
        
        # Если нет фото профиля, создаем аватарку с инициалами
        initials = ''.join(word[0] for word in nickname.split()[:2]) or nickname[:2]
        avatar = await create_avatar_photo(initials)
        
        await message.reply_photo(
            photo=avatar,
            caption=f"<blockquote>👤 Тебя зовут <b>{nickname}</b>\n\n"
                    f"🆔 ID: <code>{user_id}</code>\n"
                    f"💰 Баланс: <b>{coins} монет</b>\n"
                    f"🃏 Карточек: <b>{cards_count}/{total_cards}</b>\n"
                    f"📅 Регистрация: <b>{reg_date}</b>\n"
                    f"🔥 Стрик: <b>{streak} дней</b> (бонус: +{streak_bonus} монет/день)</blockquote>",
            reply_markup=get_profile_kb()
        )
        
    except Exception as e:
        logger.error(f"Ошибка в show_profile: {e}")
        await message.reply("<blockquote><b>❌ Произошла ошибка. Попробуйте позже.</b></blockquote>")


# Изменение ника
@router.callback_query(NicknameCallback.filter(F.action == "change"))
async def change_nickname_start(callback: CallbackQuery, state: FSMContext):
    if callback.message.chat.type != "private":
        await callback.answer("❗️ Изменить ник можно только в личных сообщениях с ботом", show_alert=True)
        return
    
    await state.set_state(NicknameSG.new_nickname)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="📥 Взять из Telegram", callback_data=NicknameCallback(action="from_telegram").pack())
    builder.button(text="❌ Отмена", callback_data=NicknameCallback(action="cancel").pack())
    builder.adjust(1)
    
    await callback.message.answer(
        "<blockquote><b>✏️ Введите новый ник:</b></blockquote>",
        reply_markup=builder.as_markup()
    )
    await callback.answer()


@router.callback_query(NicknameCallback.filter(F.action == "from_telegram"))
async def change_nickname_from_telegram(callback: CallbackQuery, state: FSMContext):
    if callback.message.chat.type != "private":
        await callback.answer("❗️ Изменить ник можно только в личных сообщениях с ботом", show_alert=True)
        return
    
    user = callback.from_user
    new_nickname = user.username or user.full_name or f"User{user.id}"
    
    try:
        async with get_db() as db:
            await db.execute(
                "UPDATE users SET nickname = ? WHERE user_id = ?",
                (new_nickname, user.id)
            )
        
        await callback.message.answer(
            f"<blockquote><b>✅ Ник изменен на: {new_nickname}</b></blockquote>"
        )
        await state.clear()
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка изменения ника: {e}")
        await callback.answer("Произошла ошибка", show_alert=True)


@router.callback_query(NicknameCallback.filter(F.action == "cancel"))
async def change_nickname_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("<blockquote><b>✅ Изменение ника отменено</b></blockquote>")
    await callback.answer()


@router.message(NicknameSG.new_nickname, F.text)
async def change_nickname_save(message: Message, state: FSMContext):
    if message.chat.type != "private":
        await message.reply("<blockquote><b>❌ Изменить ник можно только в личных сообщениях с ботом</b></blockquote>")
        await state.clear()
        return
    
    new_nickname = message.text.strip()
    
    if len(new_nickname) > 32:
        await message.reply("<blockquote><b>❌ Ник слишком длинный (максимум 32 символа)</b></blockquote>")
        return
    
    user_id = message.from_user.id
    
    try:
        async with get_db() as db:
            await db.execute(
                "UPDATE users SET nickname = ? WHERE user_id = ?",
                (new_nickname, user_id)
            )
        
        await message.reply(
            f"<blockquote><b>✅ Ник изменен на: {new_nickname}</b></blockquote>"
        )
        await state.clear()
    except Exception as e:
        logger.error(f"Ошибка сохранения ника: {e}")
        await message.reply("<blockquote><b>❌ Произошла ошибка</b></blockquote>")


# Топ игроков
async def get_top_players(limit: int = 10):
    """Получает топ игроков по монетам"""
    try:
        async with get_db() as db:
            cursor = await db.execute(
                """
                SELECT nickname, coins, user_id
                FROM users
                ORDER BY coins DESC
                LIMIT ?
                """,
                (limit,)
            )
            return await cursor.fetchall()
    except Exception as e:
        logger.error(f"Ошибка получения топа: {e}")
        return []


@router.message(F.text == "🏆 Топ игроков")
@router.message(Command("top"))
async def show_top_players(message: Message):
    top_players = await get_top_players(10)
    
    if not top_players:
        await message.reply("<blockquote><b>📊 Топ пока пуст</b></blockquote>")
        return
    
    text = "<blockquote><b>🏆 Топ игроков по монетам:</b>\n\n"
    
    medals = ["🥇", "🥈", "🥉"]
    for i, (nickname, coins, user_id) in enumerate(top_players, 1):
        medal = medals[i - 1] if i <= 3 else f"{i}."
        text += f"{medal} {nickname} — {coins} 💰\n"
    
    text += "</blockquote>"
    
    await message.reply(text)


# Просмотр коллекции
async def get_collection_main_keyboard(user_id: int):
    """Генерирует клавиатуру главного меню коллекции с подсчетом карточек"""
    try:
        async with get_db() as db:
            # Считаем количество карточек каждого типа у пользователя
            cursor = await db.execute(
                """
                SELECT c.rarity, COUNT(i.card_id)
                FROM inventory i
                JOIN cards c ON i.card_id = c.id
                WHERE i.user_id = ?
                GROUP BY c.rarity
                """,
                (user_id,)
            )
            stats = dict(await cursor.fetchall())
            
            # Общее количество карточек в игре
            cursor = await db.execute("SELECT COUNT(*) FROM cards")
            total_cards_in_game = (await cursor.fetchone())[0]
            
            inline_keyboard = []
            total_cards = sum(stats.values())
            
            # Создаем кнопки для всех редкостей
            for r_key, r_info in RARITIES.items():
                count = stats.get(r_key, 0)
                
                cursor = await db.execute("SELECT COUNT(*) FROM cards WHERE rarity = ?", (r_key,))
                total_of_rarity = (await cursor.fetchone())[0]
                
                btn_text = f"{r_info['name']} ({count}/{total_of_rarity})"
                inline_keyboard.append(
                    [
                        InlineKeyboardButton(
                            text=btn_text,
                            callback_data=RaritySelectCallback(
                                rarity=r_key, page=0
                            ).pack(),
                        )
                    ]
                )
            
            keyboard = (
                InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
                if inline_keyboard
                else None
            )
            return keyboard, total_cards, total_cards_in_game
    except Exception as e:
        logger.error(f"Ошибка создания клавиатуры коллекции: {e}")
        return None, 0, 0


@router.callback_query(F.data == "collection")
async def show_collection(callback: CallbackQuery):
    user_id = callback.from_user.id
    keyboard, total_cards, total_cards_in_game = await get_collection_main_keyboard(user_id)
    
    await callback.answer()
    
    if total_cards == 0:
        text = "<blockquote><b>📦 Твоя коллекция пока пуста. Отправь команду «милость», чтобы получить первую карточку</b></blockquote>"
        await callback.message.edit_text(text)
        return
    
    text = f"<blockquote><b>📦 Твоя коллекция ({total_cards}/{total_cards_in_game})</b></blockquote>"
    await callback.message.edit_text(
        text, reply_markup=keyboard
    )


@router.callback_query(RaritySelectCallback.filter())
async def process_rarity_view(
        callback: CallbackQuery, callback_data: RaritySelectCallback
):
    user_id = callback.from_user.id
    rarity = callback_data.rarity
    page = callback_data.page
    
    try:
        async with get_db() as db:
            # Получаем все карточки этой редкости у пользователя
            cursor = await db.execute(
                """
                SELECT c.name, c.photo_id, i.claim_time, c.id
                FROM inventory i
                JOIN cards c ON i.card_id = c.id
                WHERE i.user_id = ? AND c.rarity = ?
                ORDER BY i.claim_time DESC
                """,
                (user_id, rarity)
            )
            cards = await cursor.fetchall()
            
            if not cards:
                await callback.answer(
                    "У вас больше нет карточек этого типа.", show_alert=True
                )
                return
            
            total_pages = len(cards)
            
            # Защита от выхода за пределы списка
            if page >= total_pages:
                page = total_pages - 1
            elif page < 0:
                page = 0
            
            card = cards[page]
            card_name, photo_id, claim_time, card_id = card["name"], card["photo_id"], card["claim_time"], card["id"]
            rarity_name = RARITIES.get(rarity, {}).get("name", rarity)
            
            claim_date = datetime.fromtimestamp(claim_time).strftime("%d.%m.%Y %H:%M") if claim_time else "Неизвестно"
            
            caption = (
                f"<blockquote><b>🃏 {card_name}\n\n"
                f"🎲 Редкость: {rarity_name}\n"
                f"📊 Прогресс: {page + 1}/{total_pages} в этой категории\n"
                f"📅 Получена: {claim_date}</b></blockquote>"
            )
            
            nav_buttons = []
            if page > 0:
                nav_buttons.append(
                    InlineKeyboardButton(
                        text="◀️",
                        callback_data=RaritySelectCallback(
                            rarity=rarity, page=page - 1
                        ).pack(),
                    )
                )
            
            nav_buttons.append(
                InlineKeyboardButton(
                    text=f"{page + 1}/{total_pages}", callback_data="ignore"
                )
            )
            
            if page < total_pages - 1:
                nav_buttons.append(
                    InlineKeyboardButton(
                        text="▶️",
                        callback_data=RaritySelectCallback(
                            rarity=rarity, page=page + 1
                        ).pack(),
                    )
                )
            
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    nav_buttons,
                    [
                        InlineKeyboardButton(
                            text="🔙 Назад к категориям",
                            callback_data=MainMenuCallback().pack(),
                        )
                    ],
                ]
            )
            
            # Если уже открыто фото — обновляем медиа
            if callback.message.photo:
                await callback.message.edit_media(
                    media=InputMediaPhoto(media=photo_id, caption=caption),
                    reply_markup=keyboard
                )
            else:
                await callback.message.delete()
                await callback.message.answer_photo(
                    photo=photo_id, caption=caption, reply_markup=keyboard
                )
            
            await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка просмотра коллекции: {e}")
        await callback.answer("Произошла ошибка", show_alert=True)


@router.callback_query(MainMenuCallback.filter())
async def process_back_to_main(callback: CallbackQuery):
    user_id = callback.from_user.id
    keyboard, total_cards, total_cards_in_game = await get_collection_main_keyboard(user_id)
    
    text = f"<blockquote><b>📦 Твоя коллекция ({total_cards}/{total_cards_in_game})</b></blockquote>"
    
    if callback.message.photo:
        await callback.message.delete()
        await callback.message.answer(
            text, reply_markup=keyboard
        )
    else:
        await callback.message.edit_text(
            text, reply_markup=keyboard
        )
    
    await callback.answer()


@router.callback_query(F.data == "ignore")
async def ignore_callback(callback: CallbackQuery):
    await callback.answer()


# Обработка действий с карточкой
@router.callback_query(CardActionCallback.filter())
async def handle_card_action(callback: CallbackQuery, callback_data: CardActionCallback):
    user_id = callback.from_user.id
    action = callback_data.action
    target_user_id = callback_data.user_id if callback_data.user_id else user_id
    
    # Проверяем, что кнопку нажал именно тот пользователь, кому адресовано сообщение
    if user_id != target_user_id:
        await callback.answer("❗️ Эта кнопка не для вас", show_alert=True)
        return
    
    try:
        if action == "instant" or action == "another":
            # Проверяем баланс
            async with get_db() as db:
                cursor = await db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
                row = await cursor.fetchone()
                balance = row["coins"] if row else 0
            
            if balance < INSTANT_COST:
                await callback.answer(f"❗️ Недостаточно монет. Нужно {INSTANT_COST}, у тебя {balance}", show_alert=True)
                return
            
            await callback.answer("⏳ Получаем карточку...")
            
            # Списываем монеты и выдаем карточку
            async with get_db() as db:
                await db.execute("BEGIN TRANSACTION")
                try:
                    # Списываем монеты
                    await db.execute(
                        "UPDATE users SET coins = coins - ? WHERE user_id = ?",
                        (INSTANT_COST, user_id)
                    )
                    
                    # Обновляем last_claim
                    now = int(time.time())
                    await db.execute(
                        "UPDATE users SET last_claim = ? WHERE user_id = ?",
                        (now, user_id)
                    )
                    
                    await db.execute("COMMIT")
                except Exception as e:
                    await db.execute("ROLLBACK")
                    logger.error(f"Ошибка списания монет: {e}")
                    await callback.answer("Произошла ошибка", show_alert=True)
                    return
            
            # Выдаем карточку
            card_data, status = await issue_card(user_id, check_cooldown=False)
            
            if status == "all_collected":
                # Возвращаем монеты, если все карточки собраны
                async with get_db() as db:
                    await db.execute(
                        "UPDATE users SET coins = coins + ? WHERE user_id = ?",
                        (INSTANT_COST, user_id)
                    )
                await callback.message.answer(
                    "<blockquote><b>🎉 Ты собрал все доступные карточки! Монеты возвращены.</b></blockquote>"
                )
                await callback.answer()
                return
            elif status == "error" or card_data is None:
                # Возвращаем монеты при ошибке
                async with get_db() as db:
                    await db.execute(
                        "UPDATE users SET coins = coins + ? WHERE user_id = ?",
                        (INSTANT_COST, user_id)
                    )
                await callback.message.answer(
                    "<blockquote><b>❌ Произошла ошибка. Монеты возвращены.</b></blockquote>"
                )
                await callback.answer()
                return
            
            rarity_title = RARITIES[card_data["rarity"]]["name"]
            caption = (
                f"<blockquote><b>💙 {callback.from_user.first_name}</b>, ты получил новую карточку за {INSTANT_COST} монет!\n"
                f"🃏 <b>{card_data['name']}</b>\n\n"
                f"🎲 Редкость: <b>{rarity_title}</b>\n"
                f"💰 Монеты: <b>+{card_data['coins_earned']} (осталось {card_data['balance']})</b></blockquote>"
            )
            
            await callback.message.answer_photo(
                photo=card_data["photo_id"], 
                caption=caption, 
                reply_markup=get_after_card_keyboard(user_id)
            )
            
            # Проверяем стрик
            streak, bonus, new_balance = await check_and_update_streak(user_id)
            if bonus > 0 and streak > 0:
                await callback.message.answer(
                    f"<blockquote><b>🔥 Стрик {streak} день!\n"
                    f"💰 Бонус за стрик: +{bonus} монет\n"
                    f"💳 Баланс: {new_balance} монет</b></blockquote>"
                )
        
        elif action == "collection":
            keyboard, total_cards, total_cards_in_game = await get_collection_main_keyboard(user_id)
            
            if total_cards == 0:
                await callback.message.answer(
                    "<blockquote><b>📦 Твоя коллекция пока пуста. Отправь команду «милость», чтобы получить первую карточку</b></blockquote>"
                )
                await callback.answer()
                return
            
            await callback.message.answer(
                f"<blockquote><b>📦 Твоя коллекция ({total_cards}/{total_cards_in_game})</b></blockquote>",
                reply_markup=keyboard
            )
            await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка обработки действия: {e}")
        await callback.answer("Произошла ошибка", show_alert=True)


# ================= АДМИН-ПАНЕЛЬ =================

async def admin_filter(message: Message) -> bool:
    return await is_admin(message.from_user.id)


@router.message(Command("admin"), admin_filter)
async def admin_panel(message: Message):
    await message.answer("<blockquote><b>⚙️ Меню администратора</b></blockquote>", reply_markup=get_admin_main_kb())


@router.callback_query(F.data == "admin_add_card")
async def add_card_start(call: CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        await call.answer("❗️ Недостаточно прав", show_alert=True)
        return
    
    await state.set_state(AddCardSG.photo)
    await call.message.answer(
        "<blockquote><b>📷 Отправьте фото для добавления новой карточки (/cancel для отмены)</b></blockquote>")
    await call.answer()


@router.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        return
    
    await state.clear()
    await message.reply("<blockquote><b>✅ Операция отменена</b></blockquote>", reply_markup=get_admin_main_kb())


@router.callback_query(F.data == "cancel_add_card")
async def cancel_add_card_callback(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("<blockquote><b>✅ Операция отменена</b></blockquote>", reply_markup=get_admin_main_kb())
    await call.answer()


@router.message(AddCardSG.photo, F.photo)
async def add_card_photo(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    
    photo_id = message.photo[-1].file_id
    await state.update_data(photo_id=photo_id)
    await state.set_state(AddCardSG.name)
    await message.answer("<blockquote><b>✍️ Придумайте название</b></blockquote>")


@router.message(AddCardSG.name, F.text)
async def add_card_name(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    
    await state.update_data(name=message.text)
    await state.set_state(AddCardSG.rarity)
    await message.answer("<blockquote><b>🎲 Выберите редкость</b></blockquote>", reply_markup=get_rarity_keyboard())


@router.callback_query(AddCardSG.rarity, F.data.startswith("set_rarity:"))
async def add_card_rarity(call: CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        await state.clear()
        await call.answer("❗️ Недостаточно прав", show_alert=True)
        return
    
    rarity = call.data.split(":")[1]
    data = await state.get_data()
    
    try:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO cards (name, rarity, photo_id) VALUES (?, ?, ?)",
                (data["name"], rarity, data["photo_id"])
            )
        
        await call.message.answer(f"<blockquote><b>✅ Карточка {data['name']} успешно добавлена</b></blockquote>",
                                  reply_markup=get_admin_main_kb())
        await state.clear()
        await call.answer()
    except Exception as e:
        logger.error(f"Ошибка добавления карточки: {e}")
        await call.answer("Произошла ошибка", show_alert=True)


@router.callback_query(F.data == "admin_list_cards")
async def list_cards(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("❗️ Недостаточно прав", show_alert=True)
        return
    
    try:
        async with get_db() as db:
            cursor = await db.execute("SELECT id, name, rarity FROM cards")
            cards = await cursor.fetchall()
        
        if not cards:
            await call.message.answer("<blockquote><b>🔴 Вы пока не добавили ни одной карточки</b></blockquote>")
            await call.answer()
            return
        
        builder = InlineKeyboardBuilder()
        for card in cards:
            c_id, name, rarity = card[0], card[1], card[2]
            r_name = RARITIES.get(rarity, {}).get("name", rarity)
            builder.button(text=f"{name} ({r_name})", callback_data=f"card_manage:{c_id}")
        builder.adjust(1)
        
        await call.message.answer("<blockquote><b>🃏 Выберите карточку из списка</b></blockquote>",
                                  reply_markup=builder.as_markup())
        await call.answer()
    except Exception as e:
        logger.error(f"Ошибка списка карточек: {e}")
        await call.answer("Произошла ошибка", show_alert=True)


@router.callback_query(F.data.startswith("card_manage:"))
async def manage_single_card(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("❗️ Недостаточно прав", show_alert=True)
        return
    
    card_id = int(call.data.split(":")[1])
    
    try:
        async with get_db() as db:
            cursor = await db.execute("SELECT name, rarity, photo_id FROM cards WHERE id = ?", (card_id,))
            card = await cursor.fetchone()
        
        if not card:
            await call.message.answer("<blockquote><b>🔴 Карточка не найдена</b></blockquote>")
            await call.answer()
            return
        
        name, rarity, photo_id = card[0], card[1], card[2]
        r_name = RARITIES.get(rarity, {}).get("name", rarity)
        
        builder = InlineKeyboardBuilder()
        builder.button(text="✏️ Изменить название", callback_data=f"edit_name:{card_id}")
        builder.button(text="🎲 Изменить редкость", callback_data=f"edit_rarity:{card_id}")
        builder.button(text="❌ Удалить", callback_data=f"delete_card:{card_id}")
        builder.button(text="🔙 Назад", callback_data="admin_list_cards")
        builder.adjust(1)
        
        await call.message.answer_photo(
            photo=photo_id,
            caption=f"<blockquote><b>🃏 {name}\n\n🎲 Редкость: {r_name}\n🆔 ID: {card_id}</b></blockquote>",
            reply_markup=builder.as_markup()
        )
        await call.answer()
    except Exception as e:
        logger.error(f"Ошибка управления карточкой: {e}")
        await call.answer("Произошла ошибка", show_alert=True)


@router.callback_query(F.data.startswith("delete_card:"))
async def delete_card_cmd(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("❗️ Недостаточно прав", show_alert=True)
        return
    
    card_id = int(call.data.split(":")[1])
    
    try:
        async with get_db() as db:
            await db.execute("BEGIN TRANSACTION")
            try:
                await db.execute("DELETE FROM cards WHERE id = ?", (card_id,))
                await db.execute("DELETE FROM inventory WHERE card_id = ?", (card_id,))
                await db.execute("COMMIT")
            except Exception as e:
                await db.execute("ROLLBACK")
                raise e
        
        await call.message.answer("<blockquote><b>🗑 Карточка успешно удалена</b></blockquote>")
        await call.answer()
    except Exception as e:
        logger.error(f"Ошибка удаления карточки: {e}")
        await call.answer("Произошла ошибка", show_alert=True)


@router.callback_query(F.data.startswith("edit_name:"))
async def edit_card_name_start(call: CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        await call.answer("❗️ Недостаточно прав", show_alert=True)
        return
    
    card_id = int(call.data.split(":")[1])
    await state.update_data(card_id=card_id)
    await state.set_state(EditCardSG.new_name)
    await call.message.answer("<blockquote><b>✍️ Придумайте новое название для карточки</b></blockquote>")
    await call.answer()


@router.message(EditCardSG.new_name, F.text)
async def edit_card_name_save(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    
    data = await state.get_data()
    card_id = data["card_id"]
    new_name = message.text
    
    try:
        async with get_db() as db:
            await db.execute("UPDATE cards SET name = ? WHERE id = ?", (new_name, card_id))
        
        await message.answer(f"<blockquote><b>✅ Название карточки изменено на {new_name}</b></blockquote>",
                             reply_markup=get_admin_main_kb())
        await state.clear()
    except Exception as e:
        logger.error(f"Ошибка изменения названия: {e}")
        await message.answer("<blockquote><b>❌ Произошла ошибка</b></blockquote>")


@router.callback_query(F.data.startswith("edit_rarity:"))
async def edit_card_rarity_start(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("❗️ Недостаточно прав", show_alert=True)
        return
    
    card_id = int(call.data.split(":")[1])
    
    builder = InlineKeyboardBuilder()
    for rarity_key, rarity_info in RARITIES.items():
        builder.button(
            text=rarity_info["name"],
            callback_data=AdminRarityCallback(card_id=card_id, rarity=rarity_key).pack()
        )
    builder.button(text="🔙 Назад", callback_data=f"card_manage:{card_id}")
    builder.adjust(2)
    
    await call.message.answer("<blockquote><b>🎲 Выберите новую редкость для карточки</b></blockquote>",
                              reply_markup=builder.as_markup())
    await call.answer()


@router.callback_query(AdminRarityCallback.filter())
async def edit_card_rarity_save(call: CallbackQuery, callback_data: AdminRarityCallback):
    if not await is_admin(call.from_user.id):
        await call.answer("❗️ Недостаточно прав", show_alert=True)
        return
    
    card_id = callback_data.card_id
    new_rarity = callback_data.rarity
    
    try:
        async with get_db() as db:
            await db.execute("UPDATE cards SET rarity = ? WHERE id = ?", (new_rarity, card_id))
        
        rarity_name = RARITIES[new_rarity]["name"]
        await call.message.answer(f"<blockquote><b>✅ Редкость карточки изменена на {rarity_name}</b></blockquote>",
                                  reply_markup=get_admin_main_kb())
        await call.answer()
    except Exception as e:
        logger.error(f"Ошибка изменения редкости: {e}")
        await call.answer("Произошла ошибка", show_alert=True)


# ================= ЗАПУСК БОТА =================
async def main():
    try:
        await init_db()
        logger.info("База данных инициализирована")
        
        bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        dp = Dispatcher(storage=MemoryStorage())
        dp.include_router(router)
        
        logger.info("🤖 Бот успешно запущен")
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
