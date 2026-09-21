"""
Хранение фото карточек на диске (рядом с БД).

Почему не только Telegram file_id:
  - в мини-аппке (браузер) file_id не открыть
  - file_id может перестать работать при смене токена / через время

Схема:
  1. При добавлении/смене фото админом бот скачивает файл через Bot API
  2. Сохраняет в CARDS_PHOTO_DIR / {card_id}.jpg
  3. В БД колонка photo_path = относительный путь (или полный)
  4. API отдаёт GET /api/card/{id}/photo

Подключение к боту: см. комментарии в sync_card_photo() и README.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Та же папка, что и БД — на Bothost обычно персистентна /app/data
_DATA_ROOT = Path(os.getenv("DB_NAME", "/app/data/cards_game.db")).resolve().parent
CARDS_PHOTO_DIR = Path(os.getenv("CARDS_PHOTO_DIR", str(_DATA_ROOT / "card_photos")))


def ensure_photo_dir() -> Path:
    CARDS_PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    return CARDS_PHOTO_DIR


def local_photo_path(card_id: int, ext: str = ".jpg") -> Path:
    ensure_photo_dir()
    # нормализуем расширение
    if not ext.startswith("."):
        ext = "." + ext
    return CARDS_PHOTO_DIR / f"{int(card_id)}{ext}"


def find_local_photo(card_id: int) -> Optional[Path]:
    """Ищет файл карточки с любым из типичных расширений."""
    ensure_photo_dir()
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        p = CARDS_PHOTO_DIR / f"{int(card_id)}{ext}"
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None


async def download_telegram_file(bot, file_id: str, dest: Path) -> Path:
    """
    Скачивает файл Telegram по file_id и пишет на диск.
    bot — экземпляр aiogram.Bot
    """
    ensure_photo_dir()
    dest.parent.mkdir(parents=True, exist_ok=True)

    file = await bot.get_file(file_id)
    # aiogram 3
    await bot.download_file(file.file_path, destination=dest)
    logger.info("Сохранено фото карточки: %s (%s bytes)", dest, dest.stat().st_size)
    return dest


async def sync_card_photo(bot, card_id: int, file_id: str) -> str:
    """
    Скачивает фото и возвращает путь для записи в БД (photo_path).

    Вызов из бота после INSERT/UPDATE cards:

        path = await sync_card_photo(message.bot, card_id, photo_id)
        await db.execute("UPDATE cards SET photo_path = ? WHERE id = ?", (path, card_id))
    """
    # Определяем расширение из file_path Telegram, иначе jpg
    ext = ".jpg"
    try:
        tg_file = await bot.get_file(file_id)
        if tg_file.file_path and "." in tg_file.file_path:
            ext = "." + tg_file.file_path.rsplit(".", 1)[-1].lower()
            if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                ext = ".jpg"
    except Exception as e:
        logger.warning("get_file failed, use .jpg: %s", e)

    dest = local_photo_path(card_id, ext)
    # удалить старые варианты с другим расширением
    for old in CARDS_PHOTO_DIR.glob(f"{int(card_id)}.*"):
        if old != dest:
            try:
                old.unlink()
            except OSError:
                pass

    await download_telegram_file(bot, file_id, dest)
    # В БД храним абсолютный путь или относительно DATA — абсолютный проще для API
    return str(dest)


async def migrate_all_card_photos(bot, db) -> tuple[int, int]:
    """
    Одноразовая миграция: для всех карточек с photo_id без локального файла — скачать.

    Использование (админ-команда в боте):

        from card_photos import migrate_all_card_photos
        ok, fail = await migrate_all_card_photos(message.bot, db)
        await message.reply(f"Скачано: {ok}, ошибок: {fail}")
    """
    ensure_photo_dir()
    # колонка photo_path — если нет, добавим
    try:
        await db.execute(
            "ALTER TABLE cards ADD COLUMN photo_path TEXT"
        )
        await db.commit()
    except Exception:
        pass  # уже есть

    cur = await db.execute("SELECT id, photo_id, photo_path FROM cards")
    rows = await cur.fetchall()
    ok = fail = 0
    for row in rows:
        card_id = row["id"] if isinstance(row, dict) or hasattr(row, "keys") else row[0]
        photo_id = row["photo_id"] if hasattr(row, "keys") else row[1]
        existing = find_local_photo(card_id)
        if existing:
            # обновить path в БД если пусто
            path_in_db = row["photo_path"] if hasattr(row, "keys") else (row[2] if len(row) > 2 else None)
            if not path_in_db:
                await db.execute(
                    "UPDATE cards SET photo_path = ? WHERE id = ?",
                    (str(existing), card_id),
                )
            ok += 1
            continue
        if not photo_id:
            fail += 1
            continue
        try:
            path = await sync_card_photo(bot, card_id, photo_id)
            await db.execute(
                "UPDATE cards SET photo_path = ? WHERE id = ?",
                (path, card_id),
            )
            ok += 1
        except Exception as e:
            logger.error("migrate card %s: %s", card_id, e)
            fail += 1
    await db.commit()
    return ok, fail
