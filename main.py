from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("main")

DB_NAME = os.getenv("DB_NAME", "/app/data/cards_game.db")
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
ADMIN_BOT_TOKEN = (
    os.getenv("ADMIN_BOT_TOKEN") or os.getenv("BOT_TOKEN_ADMIN") or ""
).strip()
START_API = os.getenv("START_API", os.getenv("ENABLE_WEBAPP_API", "0")).strip().lower() in (
    "1", "true", "yes", "on",
)


async def check_environment() -> bool:
    """Проверка токенов, БД, обязательных таблиц. Возвращает False при критической ошибке."""
    ok = True
    if not BOT_TOKEN and not ADMIN_BOT_TOKEN:
        logger.error("Нет ни BOT_TOKEN, ни ADMIN_BOT_TOKEN")
        return False
    if not BOT_TOKEN:
        logger.warning("BOT_TOKEN не задан — user-бот не запустится")
    if not ADMIN_BOT_TOKEN:
        logger.warning("ADMIN_BOT_TOKEN не задан — admin-бот не запустится")

    Path(DB_NAME).parent.mkdir(parents=True, exist_ok=True)

    try:
        import aiosqlite
        from game_config import ensure_config_tables, SUPERADMIN_IDS

        db = await aiosqlite.connect(DB_NAME)
        try:
            await db.execute("PRAGMA journal_mode = WAL;")
            # минимальные таблицы
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
            for table, col, defn in [
                ("cards", "photo_path", "TEXT"),
                ("cards", "photo_uuid", "TEXT"),
                ("cards", "photo_id", "TEXT"),
                ("users", "last_dice", "INTEGER DEFAULT 0"),
            ]:
                try:
                    await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
                except Exception:
                    pass
            try:
                await db.execute("DROP TABLE IF EXISTS duels")
            except Exception:
                pass
            await ensure_config_tables(db)
            await db.commit()
            logger.info("БД OK: %s", DB_NAME)
            if SUPERADMIN_IDS:
                logger.info("SUPERADMIN_IDS: %s", SUPERADMIN_IDS)
            else:
                logger.warning("SUPERADMIN_IDS пуст — назначьте суперадмина в .env")
        finally:
            await db.close()
    except Exception as e:
        logger.error("БД ошибка: %s", e)
        ok = False

    try:
        from card_photos import ensure_photo_dir
        p = ensure_photo_dir()
        logger.info("Фото-папка: %s", p)
    except Exception as e:
        logger.warning("card_photos: %s", e)

    try:
        from PIL import Image  # noqa: F401
        logger.info("Pillow OK")
    except ImportError:
        logger.warning("Pillow не установлен — рамки карточек недоступны")

    return ok


async def run_api() -> None:
    try:
        import uvicorn
        from api import app as fastapi_app
    except ImportError as e:
        logger.warning("API не запущен: %s", e)
        return
    port = int(os.getenv("PORT", os.getenv("API_PORT", "8080")))
    logger.info("API → 0.0.0.0:%s", port)
    config = uvicorn.Config(
        fastapi_app, host="0.0.0.0", port=port, log_level="info", lifespan="on"
    )
    server = uvicorn.Server(config)
    await server.serve()


async def run_user_bot() -> None:
    if not BOT_TOKEN:
        return
    # init_db + polling из bot
    from user_bot import main as bot_main
    await bot_main()


async def run_admin_only() -> None:
    """Только админ-бот (когда user отключён через настройку, но main всё равно может поднять оба)."""
    if not ADMIN_BOT_TOKEN:
        return
    from admin_bot import run_admin_bot
    await run_admin_bot(ADMIN_BOT_TOKEN)


async def main() -> None:
    if not await check_environment():
        sys.exit(1)

    tasks = []

    # User-бот: проверяем флаг в БД
    user_enabled = True
    try:
        import aiosqlite
        from game_config import is_user_bot_enabled
        db = await aiosqlite.connect(DB_NAME)
        try:
            user_enabled = await is_user_bot_enabled(db)
        finally:
            await db.close()
    except Exception as e:
        logger.warning("Не удалось прочитать user_bot_enabled: %s", e)

    if BOT_TOKEN and user_enabled:
        # bot.main() сам поднимает admin если ADMIN_BOT_TOKEN задан
        # чтобы не дублировать admin — либо bot.main, либо раздельно
        # Здесь: если есть admin token, запускаем bot без встроенного admin, admin отдельно
        os.environ.setdefault("_MAIN_ENTRY", "1")
        tasks.append(asyncio.create_task(run_user_bot(), name="user_bot"))
        logger.info("User-бот: включён")
    elif BOT_TOKEN and not user_enabled:
        logger.warning("User-бот ВЫКЛЮЧЕН (bot_settings.user_bot_enabled=0)")
        if ADMIN_BOT_TOKEN:
            tasks.append(asyncio.create_task(run_admin_only(), name="admin_bot"))
    elif not BOT_TOKEN and ADMIN_BOT_TOKEN:
        tasks.append(asyncio.create_task(run_admin_only(), name="admin_bot"))

    if START_API:
        tasks.append(asyncio.create_task(run_api(), name="api"))

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
