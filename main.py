"""
Точка входа. Запускает user-бот и/или admin-бот.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeDefault, LinkPreviewOptions

from config import BOT_TOKEN, ADMIN_BOT_TOKEN, DATA_DIR, LOG_DIR, SUPERADMIN_IDS
from db import init_db, is_user_bot_enabled
from photos import ensure_photo_dir

# Логирование
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")

# Уменьшаем шум
logging.getLogger("aiogram").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def run_user_bot() -> None:
    if not BOT_TOKEN:
        logger.warning("BOT_TOKEN не задан — user-бот не запускается")
        return

    from user_bot.handlers import router

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview=LinkPreviewOptions(is_disabled=True),
        ),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    commands = [
        BotCommand(command="start", description="👋 Начать"),
        BotCommand(command="meow", description="🃏 Получить карточку"),
        BotCommand(command="profile", description="👤 Профиль"),
        BotCommand(command="top", description="🏆 Топ"),
        BotCommand(command="dice", description="🎲 Кубик"),
        BotCommand(command="nick", description="✏️ Сменить ник"),
        BotCommand(command="help", description="❓ Справка"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    logger.info("🤖 User-бот запущен")
    await dp.start_polling(bot)


async def run_admin_bot() -> None:
    if not ADMIN_BOT_TOKEN:
        logger.warning("ADMIN_BOT_TOKEN не задан — admin-бот не запускается")
        return

    from admin_bot.handlers import router

    bot = Bot(
        token=ADMIN_BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview=LinkPreviewOptions(is_disabled=True),
        ),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    commands = [
        BotCommand(command="start", description="⚙️ Админ-панель"),
        BotCommand(command="stats", description="📊 Статистика"),
        BotCommand(command="top", description="🏆 Топ"),
        BotCommand(command="help", description="🛠 Справка"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    logger.info("🛡 Admin-бот запущен")
    await dp.start_polling(bot)


async def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ensure_photo_dir()

    if not BOT_TOKEN and not ADMIN_BOT_TOKEN:
        logger.error("Нет ни BOT_TOKEN, ни ADMIN_BOT_TOKEN")
        sys.exit(1)

    await init_db()
    logger.info("БД готова")
    if SUPERADMIN_IDS:
        logger.info("SUPERADMIN_IDS: %s", SUPERADMIN_IDS)
    else:
        logger.warning("SUPERADMIN_IDS пуст — назначьте суперадмина в .env")

    tasks = []

    user_on = True
    try:
        user_on = await is_user_bot_enabled()
    except Exception:
        pass

    if BOT_TOKEN and user_on:
        tasks.append(asyncio.create_task(run_user_bot(), name="user_bot"))
    elif BOT_TOKEN and not user_on:
        logger.warning("User-бот выключен через настройки")

    if ADMIN_BOT_TOKEN:
        tasks.append(asyncio.create_task(run_admin_bot(), name="admin_bot"))

    if not tasks:
        logger.error("Нечего запускать")
        sys.exit(1)

    logger.info("Запуск: %s", [t.get_name() for t in tasks])
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Остановка")
