import asyncio
import logging
import os
import random
import time
from datetime import datetime, timedelta
from typing import Optional, Tuple

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
    action: str  # "instant" или "collection"


class StreakCallback(CallbackData, prefix="streak"):
    pass


# ================= РАБОТА С БАЗОЙ ДАННЫХ =================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
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
                             user_id        INTEGER PRIMARY KEY,
                             last_claim     INTEGER DEFAULT 0,
                             role           TEXT    DEFAULT 'user',
                             nickname       TEXT,
                             coins          INTEGER DEFAULT 0,
                             registration   INTEGER DEFAULT 0,
                             streak         INTEGER DEFAULT 0,
                             last_streak_date INTEGER DEFAULT 0,
                             streak_bonus   INTEGER DEFAULT 0
                         )
                         """)
        # Инвентарь пользователей (без count)
        await db.execute("""
                         CREATE TABLE IF NOT EXISTS inventory
                         (
                             user_id   INTEGER,
                             card_id   INTEGER,
                             claim_time INTEGER DEFAULT 0,
                             PRIMARY KEY (user_id, card_id),
                             FOREIGN KEY (card_id) REFERENCES cards (id) ON DELETE CASCADE
                         )
                         """)
        
        # Добавляем новые колонки если их нет
        try:
            await db.execute("ALTER TABLE users ADD COLUMN registration INTEGER DEFAULT 0")
        except:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN streak INTEGER DEFAULT 0")
        except:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN last_streak_date INTEGER DEFAULT 0")
        except:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN streak_bonus INTEGER DEFAULT 0")
        except:
            pass
        try:
            await db.execute("ALTER TABLE inventory ADD COLUMN claim_time INTEGER DEFAULT 0")
        except:
            pass
        
        await db.commit()


# Функция для проверки, является ли пользователь админом
async def is_admin(user_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        async with db.execute("SELECT role FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row is not None and row[0] == 'admin'


# Функция для получения соединения с БД
async def get_db():
    db = await aiosqlite.connect(DB_NAME)
    await db.execute("PRAGMA foreign_keys = ON;")
    return db


# Функция для получения или создания пользователя
async def get_or_create_user(user_id: int, username: str = None, full_name: str = None):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            user = await cursor.fetchone()

        if not user:
            nickname = username or full_name or f"User{user_id}"
            now = int(time.time())
            await db.execute(
                "INSERT INTO users (user_id, nickname, registration, streak, last_streak_date) VALUES (?, ?, ?, ?, ?)",
                (user_id, nickname, now, 0, 0)
            )
            await db.commit()
            async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
                user = await cursor.fetchone()

        return user


# Функция для создания аватарки с инициалами
async def create_avatar_photo(initials: str) -> BufferedInputFile:
    """Создает квадратное изображение с инициалами на цветном фоне"""
    size = 200
    colors = [
        (66, 133, 244), (52, 168, 83), (251, 188, 5), (234, 67, 53),
        (156, 39, 176), (0, 188, 212), (255, 152, 0), (233, 30, 99)
    ]
    
    # Выбираем цвет на основе инициалов
    color_index = sum(ord(c) for c in initials) % len(colors)
    bg_color = colors[color_index]
    
    # Создаем изображение
    image = Image.new('RGB', (size, size), bg_color)
    draw = ImageDraw.Draw(image)
    
    # Пытаемся загрузить шрифт, если нет - используем дефолтный
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 80)
    except:
        font = ImageFont.load_default()
    
    # Рисуем инициалы
    text = initials.upper()[:2]
    text_bbox = draw.textbbox((0, 0), text, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]
    x = (size - text_width) // 2
    y = (size - text_height) // 2 - 10
    
    draw.text((x, y), text, fill=(255, 255, 255), font=font)
    
    # Сохраняем в буфер
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
    """Клавиатура для действий с карточкой"""
    builder = InlineKeyboardBuilder()
    builder.button(text="✨ Получить сейчас (5000💰)", callback_data=CardActionCallback(action="instant").pack())
    builder.button(text="📦 Моя коллекция", callback_data=CardActionCallback(action="collection").pack())
    builder.adjust(1)
    return builder.as_markup()


# --- Вспомогательные функции ---
async def get_collection_main_keyboard(user_id: int):
    """Генерирует клавиатуру главного меню коллекции с подсчетом карточек"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        # Считаем количество карточек каждого типа у пользователя
        async with db.execute(
                """
                SELECT c.rarity, COUNT(i.card_id)
                FROM inventory i
                         JOIN cards c ON i.card_id = c.id
                WHERE i.user_id = ?
                GROUP BY c.rarity
                """,
                (user_id,),
        ) as cursor:
            stats = dict(await cursor.fetchall())
        
        # Общее количество карточек в игре
        async with db.execute("SELECT COUNT(*) FROM cards") as cursor:
            total_cards_in_game = (await cursor.fetchone())[0]

    inline_keyboard = []
    total_cards = sum(stats.values())

    # Создаем кнопки для всех редкостей
    for r_key, r_info in RARITIES.items():
        count = stats.get(r_key, 0)
        # Получаем общее количество карточек этой редкости
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT COUNT(*) FROM cards WHERE rarity = ?", (r_key,)) as cursor:
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


async def get_top_players(limit: int = 10):
    """Получает топ игроков по монетам"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        async with db.execute(
                """
                SELECT nickname, coins, user_id
                FROM users
                ORDER BY coins DESC
                LIMIT ?
                """,
                (limit,)
        ) as cursor:
            return await cursor.fetchall()


async def check_and_update_streak(user_id: int) -> Tuple[int, int]:
    """
    Проверяет и обновляет стрик пользователя.
    Возвращает (текущий_стрик, бонус_за_стрик)
    """
    now = datetime.now()
    today_start = int(datetime(now.year, now.month, now.day).timestamp())
    today_end = today_start + 86400 - 1
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        async with db.execute(
            "SELECT streak, last_streak_date, streak_bonus FROM users WHERE user_id = ?",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return 0, 0
            
            streak, last_streak_date, streak_bonus = row
            
            # Если сегодня уже начисляли бонус
            if last_streak_date >= today_start and last_streak_date <= today_end:
                return streak, streak_bonus
            
            # Проверяем, получал ли пользователь карточку сегодня
            async with db.execute(
                "SELECT MAX(claim_time) FROM inventory WHERE user_id = ?",
                (user_id,)
            ) as cursor2:
                last_claim_row = await cursor2.fetchone()
                last_claim = last_claim_row[0] if last_claim_row[0] else 0
            
            # Если пользователь получал карточку сегодня
            if last_claim >= today_start:
                # Проверяем, был ли получен вчера (для поддержания стрика)
                yesterday_start = int((now - timedelta(days=1)).replace(hour=0, minute=0, second=0).timestamp())
                yesterday_end = yesterday_start + 86400 - 1
                
                # Если стрик был и последний раз получал вчера или сегодня
                if streak > 0 and last_streak_date >= yesterday_start:
                    streak += 1
                elif streak == 0:
                    streak = 1
                else:
                    # Был пропуск - сбрасываем стрик
                    streak = 1
                
                # Рассчитываем бонус за стрик
                streak_bonus = 0
                for days, bonus in STREAK_BONUSES:
                    if streak <= days:
                        streak_bonus = bonus
                        break
                
                # Начисляем бонус
                await db.execute(
                    "UPDATE users SET coins = coins + ?, streak = ?, last_streak_date = ?, streak_bonus = ? WHERE user_id = ?",
                    (streak_bonus, streak, int(time.time()), streak_bonus, user_id)
                )
                await db.commit()
                
                return streak, streak_bonus
            else:
                # Если не получал сегодня - сбрасываем стрик
                streak = 0
                streak_bonus = 0
                await db.execute(
                    "UPDATE users SET streak = ?, streak_bonus = ? WHERE user_id = ?",
                    (0, 0, user_id)
                )
                await db.commit()
                return 0, 0


# ================= ПОЛЬЗОВАТЕЛЬСКАЯ ЛОГИКА =================

@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message):
    # Создаем пользователя при первом запуске
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


# Выдача карточки по ключевому слову
@router.message(F.text == "🃏 Получить карточку")
@router.message(F.text.lower().strip() == "милость")
@router.message(F.text.lower().strip() == "мряу")
@router.message(Command("card"))
async def get_card_handler(message: Message):
    user_id = message.from_user.id
    now = int(time.time())

    # Получаем или создаем пользователя
    await get_or_create_user(
        user_id,
        message.from_user.username,
        message.from_user.full_name
    )

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        # Проверка кулдауна
        async with db.execute("SELECT last_claim FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            last_claim = row[0] if row else 0

        time_passed = now - last_claim
        if time_passed < COOLDOWN_SECONDS:
            remaining = int(COOLDOWN_SECONDS - time_passed)

            hours = remaining // 3600
            minutes = (remaining % 3600) // 60
            seconds = remaining % 60

            # Проверяем баланс для мгновенного получения
            async with db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,)) as cursor:
                balance_row = await cursor.fetchone()
                balance = balance_row[0] if balance_row else 0
            
            await message.reply(
                f"<blockquote>⏳ Следующую карточку можно будет получить через: <b>{hours}ч {minutes}м {seconds}с</b>\n\n"
                f"Или получи сейчас за <b>{INSTANT_COST} монет</b> (у тебя <b>{balance}</b> монет)</blockquote>",
                reply_markup=get_card_action_keyboard(user_id)
            )
            return

        # Проверяем, остались ли карточки
        async with db.execute("SELECT COUNT(*) FROM cards") as cursor:
            total_cards = (await cursor.fetchone())[0]
        
        if total_cards == 0:
            await message.reply(
                "<blockquote><b>😔 В базе пока нет ни одной карточки. Попросите админа добавить их</b></blockquote>"
            )
            return
        
        # Проверяем, не все ли карточки уже получены
        async with db.execute(
            "SELECT COUNT(DISTINCT card_id) FROM inventory WHERE user_id = ?",
            (user_id,)
        ) as cursor:
            user_cards_count = (await cursor.fetchone())[0]
        
        if user_cards_count >= total_cards:
            await message.reply(
                "<blockquote><b>🎉 Ты собрал все доступные карточки! Ожидай добавления новых.</b></blockquote>"
            )
            return

        # Выбор редкости на основе весов
        rarities_list = list(RARITIES.keys())
        weights = [RARITIES[r]["weight"] for r in rarities_list]
        
        # Ищем доступные карточки этой редкости
        available_cards = []
        for _ in range(10):  # Пытаемся найти карточку до 10 раз
            selected_rarity = random.choices(rarities_list, weights=weights, k=1)[0]
            
            # Получаем случайную карту выбранной редкости, которой нет у пользователя
            async with db.execute(
                """
                SELECT id, name, photo_id 
                FROM cards 
                WHERE rarity = ? AND id NOT IN (
                    SELECT card_id FROM inventory WHERE user_id = ?
                )
                ORDER BY RANDOM() 
                LIMIT 1
                """,
                (selected_rarity, user_id)
            ) as cursor:
                card = await cursor.fetchone()
            
            if card:
                available_cards.append((card, selected_rarity))
                break
        
        # Если не нашли карточку нужной редкости, пробуем любую
        if not available_cards:
            async with db.execute(
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
            ) as cursor:
                card = await cursor.fetchone()
                if card:
                    available_cards.append((card, card[3]))
        
        if not available_cards:
            await message.reply(
                "<blockquote><b>🎉 Ты собрал все доступные карточки! Ожидай добавления новых.</b></blockquote>"
            )
            return
        
        card, selected_rarity = available_cards[0]
        card_id, card_name, photo_id = card[0], card[1], card[2]
        coins_earned = RARITIES[selected_rarity]["coins"]

        # Обновляем таймер юзера и начисляем монеты
        await db.execute(
            "INSERT INTO users (user_id, last_claim, coins) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_claim = ?, coins = coins + ?",
            (user_id, now, coins_earned, now, coins_earned)
        )

        # Добавляем карту в инвентарь
        await db.execute(
            "INSERT INTO inventory (user_id, card_id, claim_time) VALUES (?, ?, ?)",
            (user_id, card_id, now)
        )
        await db.commit()

        # Получаем обновленный баланс
        async with db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,)) as cursor:
            balance = (await cursor.fetchone())[0]

    rarity_title = RARITIES[selected_rarity]["name"]
    caption = (
        f"<blockquote><b>💙 {message.from_user.first_name}</b>, тебе выпала новая карточка: <b>{card_name}</b>\n\n"
        f"🎲 Редкость: <b>{rarity_title}</b>\n"
        f"💰 Монеты: <b>+{coins_earned} [{balance}]</b></blockquote>"
    )
    
    # Создаем клавиатуру с действиями
    action_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Моя коллекция", callback_data=CardActionCallback(action="collection").pack())]
    ])
    
    await message.reply_photo(photo=photo_id, caption=caption, reply_markup=action_kb)
    
    # Проверяем стрик
    streak, bonus = await check_and_update_streak(user_id)
    if bonus > 0 and streak > 0:
        await message.reply(
            f"<blockquote><b>🔥 Стрик {streak} день!\n"
            f"💰 Бонус за стрик: +{bonus} монет</b></blockquote>"
        )


@router.message(F.text == "👤 Профиль")
@router.message(F.text.lower().strip() == "профиль")
@router.message(Command("profile"))
async def show_profile(message: Message):
    user_id = message.from_user.id

    # Получаем или создаем пользователя
    user = await get_or_create_user(
        user_id,
        message.from_user.username,
        message.from_user.full_name
    )

    nickname = user[3] or message.from_user.full_name
    coins = user[4] if len(user) > 4 else 0
    registration = user[5] if len(user) > 5 and user[5] else int(time.time())
    streak = user[6] if len(user) > 6 else 0
    streak_bonus = user[8] if len(user) > 8 else 0

    # Получаем количество карточек в коллекции
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM inventory WHERE user_id = ?",
            (user_id,)
        ) as cursor:
            cards_count = (await cursor.fetchone())[0]
        
        async with db.execute(
            "SELECT COUNT(*) FROM cards"
        ) as cursor:
            total_cards = (await cursor.fetchone())[0]

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
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute(
            "UPDATE users SET nickname = ? WHERE user_id = ?",
            (new_nickname, user.id)
        )
        await db.commit()
    
    await callback.message.answer(
        f"<blockquote><b>✅ Ник изменен на: {new_nickname}</b></blockquote>"
    )
    await state.clear()
    await callback.answer()


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

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute(
            "UPDATE users SET nickname = ? WHERE user_id = ?",
            (new_nickname, user_id)
        )
        await db.commit()

    await message.reply(
        f"<blockquote><b>✅ Ник изменен на: {new_nickname}</b></blockquote>"
    )
    await state.clear()


# Топ игроков
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


@router.callback_query(F.data == "top_players")
async def show_top_players_callback(callback: CallbackQuery):
    top_players = await get_top_players(10)

    if not top_players:
        await callback.message.answer("<blockquote><b>📊 Топ пока пуст</b></blockquote>")
        await callback.answer()
        return

    text = "<blockquote><b>🏆 Топ игроков по монетам:</b>\n\n"

    medals = ["🥇", "🥈", "🥉"]
    for i, (nickname, coins, user_id) in enumerate(top_players, 1):
        medal = medals[i - 1] if i <= 3 else f"{i}."
        text += f"{medal} {nickname} — {coins} 💰\n"

    text += "</blockquote>"

    await callback.message.answer(text)
    await callback.answer()


# Просмотр коллекции
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

    # Получаем все уникальные карточки этой редкости у пользователя
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        async with db.execute(
                """
                SELECT c.name, c.photo_id, i.claim_time, c.id
                FROM inventory i
                         JOIN cards c ON i.card_id = c.id
                WHERE i.user_id = ?
                  AND c.rarity = ?
                ORDER BY i.claim_time DESC
                """,
                (user_id, rarity),
        ) as cursor:
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

    card_name, photo_id, claim_time, card_id = cards[page]
    rarity_name = RARITIES.get(rarity, {}).get("name", rarity)
    
    # Получаем общее количество карточек этой редкости в игре
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM cards WHERE rarity = ?", (rarity,)) as cursor:
            total_of_rarity = (await cursor.fetchone())[0]
    
    # Получаем количество карточек этой редкости у пользователя
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM inventory i JOIN cards c ON i.card_id = c.id WHERE i.user_id = ? AND c.rarity = ?",
            (user_id, rarity)
        ) as cursor:
            user_rarity_count = (await cursor.fetchone())[0]

    claim_date = datetime.fromtimestamp(claim_time).strftime("%d.%m.%Y %H:%M") if claim_time else "Неизвестно"

    # Формируем текст под карточкой
    caption = (
        f"<blockquote><b>🃏 {card_name}\n\n"
        f"🎲 Редкость: {rarity_name}\n"
        f"📊 Прогресс: {page + 1}/{user_rarity_count} в этой категории\n"
        f"📅 Получена: {claim_date}</b></blockquote>"
    )

    # Строим кнопки пагинации
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


# 3. Возврат в главное меню коллекции
@router.callback_query(MainMenuCallback.filter())
async def process_back_to_main(callback: CallbackQuery):
    user_id = callback.from_user.id
    keyboard, total_cards, total_cards_in_game = await get_collection_main_keyboard(user_id)

    text = f"<blockquote><b>📦 Твоя коллекция ({total_cards}/{total_cards_in_game})</b></blockquote>"

    # Если мы были в режиме фото, удаляем фото-сообщение и отправляем новое текстовое
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


# Пустышка для кнопки с нумерацией (чтобы при клике ничего не происходило)
@router.callback_query(F.data == "ignore")
async def ignore_callback(callback: CallbackQuery):
    await callback.answer()


# Обработка действий с карточкой
@router.callback_query(CardActionCallback.filter())
async def handle_card_action(callback: CallbackQuery, callback_data: CardActionCallback):
    user_id = callback.from_user.id
    action = callback_data.action
    
    if action == "instant":
        # Проверяем, что нажал именно тот пользователь
        if callback.message.chat.type != "private":
            # Проверяем, что сообщение адресовано этому пользователю
            if callback.message.reply_to_message and callback.message.reply_to_message.from_user.id != user_id:
                await callback.answer("❗️ Эта кнопка не для вас", show_alert=True)
                return
        
        # Проверяем баланс
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                balance = row[0] if row else 0
        
        if balance < INSTANT_COST:
            await callback.answer(f"❗️ Недостаточно монет. Нужно {INSTANT_COST}, у тебя {balance}", show_alert=True)
            return
        
        # Проверяем, не все ли карточки уже получены
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT COUNT(*) FROM cards") as cursor:
                total_cards = (await cursor.fetchone())[0]
            
            async with db.execute(
                "SELECT COUNT(DISTINCT card_id) FROM inventory WHERE user_id = ?",
                (user_id,)
            ) as cursor:
                user_cards_count = (await cursor.fetchone())[0]
        
        if user_cards_count >= total_cards:
            await callback.message.answer(
                "<blockquote><b>🎉 Ты собрал все доступные карточки! Ожидай добавления новых.</b></blockquote>"
            )
            await callback.answer()
            return
        
        # Выдаем карточку
        await callback.answer("⏳ Получаем карточку...")
        
        # Выбираем редкость и карточку
        rarities_list = list(RARITIES.keys())
        weights = [RARITIES[r]["weight"] for r in rarities_list]
        
        for _ in range(10):
            selected_rarity = random.choices(rarities_list, weights=weights, k=1)[0]
            
            async with aiosqlite.connect(DB_NAME) as db:
                async with db.execute(
                    """
                    SELECT id, name, photo_id 
                    FROM cards 
                    WHERE rarity = ? AND id NOT IN (
                        SELECT card_id FROM inventory WHERE user_id = ?
                    )
                    ORDER BY RANDOM() 
                    LIMIT 1
                    """,
                    (selected_rarity, user_id)
                ) as cursor:
                    card = await cursor.fetchone()
            
            if card:
                break
        else:
            # Если не нашли по редкости, берем любую
            async with aiosqlite.connect(DB_NAME) as db:
                async with db.execute(
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
                ) as cursor:
                    card = await cursor.fetchone()
        
        if not card:
            await callback.message.answer(
                "<blockquote><b>🎉 Ты собрал все доступные карточки! Ожидай добавления новых.</b></blockquote>"
            )
            await callback.answer()
            return
        
        now = int(time.time())
        card_id, card_name, photo_id = card[0], card[1], card[2]
        selected_rarity = card[3] if len(card) > 3 else "common"
        coins_earned = RARITIES[selected_rarity]["coins"]
        
        async with aiosqlite.connect(DB_NAME) as db:
            # Списываем монеты
            await db.execute(
                "UPDATE users SET coins = coins - ? WHERE user_id = ?",
                (INSTANT_COST, user_id)
            )
            # Начисляем монеты за карточку
            await db.execute(
                "UPDATE users SET coins = coins + ? WHERE user_id = ?",
                (coins_earned, user_id)
            )
            # Обновляем last_claim
            await db.execute(
                "UPDATE users SET last_claim = ? WHERE user_id = ?",
                (now, user_id)
            )
            # Добавляем в инвентарь
            await db.execute(
                "INSERT INTO inventory (user_id, card_id, claim_time) VALUES (?, ?, ?)",
                (user_id, card_id, now)
            )
            await db.commit()
            
            # Получаем новый баланс
            async with db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,)) as cursor:
                balance = (await cursor.fetchone())[0]
        
        rarity_title = RARITIES[selected_rarity]["name"]
        caption = (
            f"<blockquote><b>💙 {callback.from_user.first_name}</b>, ты получил новую карточку за {INSTANT_COST} монет!\n"
            f"🃏 <b>{card_name}</b>\n\n"
            f"🎲 Редкость: <b>{rarity_title}</b>\n"
            f"💰 Монеты: <b>+{coins_earned} (осталось {balance})</b></blockquote>"
        )
        
        action_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📦 Моя коллекция", callback_data=CardActionCallback(action="collection").pack())]
        ])
        
        await callback.message.answer_photo(photo=photo_id, caption=caption, reply_markup=action_kb)
        
        # Проверяем стрик
        streak, bonus = await check_and_update_streak(user_id)
        if bonus > 0 and streak > 0:
            await callback.message.answer(
                f"<blockquote><b>🔥 Стрик {streak} день!\n"
                f"💰 Бонус за стрик: +{bonus} монет</b></blockquote>"
            )
    
    elif action == "collection":
        # Показываем коллекцию
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


# ================= АДМИН-ПАНЕЛЬ =================

# Фильтр для проверки прав администратора
async def admin_filter(message: Message) -> bool:
    return await is_admin(message.from_user.id)


# Вход в админку
@router.message(Command("admin"), admin_filter)
async def admin_panel(message: Message):
    await message.answer("<blockquote><b>⚙️ Меню администратора</b></blockquote>", reply_markup=get_admin_main_kb())


# 1. Добавление карточки: Начало
@router.callback_query(F.data == "admin_add_card")
async def add_card_start(call: CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        await call.answer("❗️ Недостаточно прав", show_alert=True)
        return

    await state.set_state(AddCardSG.photo)
    await call.message.answer(
        "<blockquote><b>📷 Отправьте фото для добавления новой карточки (/cancel для отмены)</b></blockquote>")
    await call.answer()


# Отмена добавления карточки
@router.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        return

    await state.clear()
    await message.reply("<blockquote><b>✅ Операция отменена</b></blockquote>", reply_markup=get_admin_main_kb())


# Отмена через callback
@router.callback_query(F.data == "cancel_add_card")
async def cancel_add_card_callback(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("<blockquote><b>✅ Операция отменена</b></blockquote>", reply_markup=get_admin_main_kb())
    await call.answer()


# Добавление: Получение фото
@router.message(AddCardSG.photo, F.photo)
async def add_card_photo(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return

    photo_id = message.photo[-1].file_id
    await state.update_data(photo_id=photo_id)
    await state.set_state(AddCardSG.name)
    await message.answer("<blockquote><b>✍️ Придумайте название</b></blockquote>")


# Добавление: Получение имени
@router.message(AddCardSG.name, F.text)
async def add_card_name(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return

    await state.update_data(name=message.text)
    await state.set_state(AddCardSG.rarity)
    await message.answer("<blockquote><b>🎲 Выберите редкость</b></blockquote>", reply_markup=get_rarity_keyboard())


# Добавление: Выбор редкости и сохранение
@router.callback_query(AddCardSG.rarity, F.data.startswith("set_rarity:"))
async def add_card_rarity(call: CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        await state.clear()
        await call.answer("❗️ Недостаточно прав", show_alert=True)
        return

    rarity = call.data.split(":")[1]
    data = await state.get_data()

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute(
            "INSERT INTO cards (name, rarity, photo_id) VALUES (?, ?, ?)",
            (data["name"], rarity, data["photo_id"])
        )
        await db.commit()

    await call.message.answer(f"<blockquote><b>✅ Карточка {data['name']} успешно добавлена</b></blockquote>",
                              reply_markup=get_admin_main_kb())
    await state.clear()
    await call.answer()


# 2. Просмотр и управление карточками
@router.callback_query(F.data == "admin_list_cards")
async def list_cards(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("❗️ Недостаточно прав", show_alert=True)
        return

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        async with db.execute("SELECT id, name, rarity FROM cards") as cursor:
            cards = await cursor.fetchall()

    if not cards:
        await call.message.answer("<blockquote><b>🔴 Вы пока не добавили ни одной карточки</b></blockquote>")
        await call.answer()
        return

    builder = InlineKeyboardBuilder()
    for c_id, name, rarity in cards:
        r_name = RARITIES.get(rarity, {}).get("name", rarity)
        builder.button(text=f"{name} ({r_name})", callback_data=f"card_manage:{c_id}")
    builder.adjust(1)

    await call.message.answer("<blockquote><b>🃏 Выберите карточку из списка</b></blockquote>",
                              reply_markup=builder.as_markup())
    await call.answer()


# Меню отдельной карточки
@router.callback_query(F.data.startswith("card_manage:"))
async def manage_single_card(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("❗️ Недостаточно прав", show_alert=True)
        return

    card_id = int(call.data.split(":")[1])

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        async with db.execute("SELECT name, rarity, photo_id FROM cards WHERE id = ?", (card_id,)) as cursor:
            card = await cursor.fetchone()

    if not card:
        await call.message.answer("<blockquote><b>🔴 Карточка не найдена</b></blockquote>")
        await call.answer()
        return

    name, rarity, photo_id = card
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


# Удаление карточки
@router.callback_query(F.data.startswith("delete_card:"))
async def delete_card_cmd(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("❗️ Недостаточно прав", show_alert=True)
        return

    card_id = int(call.data.split(":")[1])

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("DELETE FROM cards WHERE id = ?", (card_id,))
        await db.execute("DELETE FROM inventory WHERE card_id = ?", (card_id,))
        await db.commit()

    await call.message.answer("<blockquote><b>🗑 Карточка успешно удалена</b></blockquote>")
    await call.answer()


# Редактирование карточки: Запрос нового названия
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


# Редактирование карточки: Сохранение нового названия
@router.message(EditCardSG.new_name, F.text)
async def edit_card_name_save(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return

    data = await state.get_data()
    card_id = data["card_id"]
    new_name = message.text

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("UPDATE cards SET name = ? WHERE id = ?", (new_name, card_id))
        await db.commit()

    await message.answer(f"<blockquote><b>✅ Название карточки изменено на {new_name}</b></blockquote>",
                         reply_markup=get_admin_main_kb())
    await state.clear()


# Редактирование редкости карточки
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


# Сохранение новой редкости
@router.callback_query(AdminRarityCallback.filter())
async def edit_card_rarity_save(call: CallbackQuery, callback_data: AdminRarityCallback):
    if not await is_admin(call.from_user.id):
        await call.answer("❗️ Недостаточно прав", show_alert=True)
        return

    card_id = callback_data.card_id
    new_rarity = callback_data.rarity

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("UPDATE cards SET rarity = ? WHERE id = ?", (new_rarity, card_id))
        await db.commit()

    rarity_name = RARITIES[new_rarity]["name"]
    await call.message.answer(f"<blockquote><b>✅ Редкость карточки изменена на {rarity_name}</b></blockquote>",
                              reply_markup=get_admin_main_kb())
    await call.answer()


# ================= ЗАПУСК БОТА =================
async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    print("🤖 Бот успешно запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
