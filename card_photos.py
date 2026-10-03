"""Локальное хранение фото карточек и аватара по умолчанию.

Карточки: card_photos/{card_id}.jpg
Аватар:   card_photos/default_avatar.jpg
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from aiogram import Bot
from aiogram.types import FSInputFile

logger = logging.getLogger(__name__)

# Рядом с БД или через env
_PHOTO_DIR = Path(os.getenv("CARD_PHOTOS_DIR", "/app/data/card_photos"))
DEFAULT_AVATAR_PATH = _PHOTO_DIR / "default_avatar.jpg"
# Старый file_id из исходного бота (для однократной миграции)
DEFAULT_AVATAR_FILE_ID = "AgACAgIAAxkBAAID12qql3EFpnb2HwTCE7Yn_Ri1TQsNAAKRIGsbfHlYSVUpxfU75O60AQADAgADeAADPQQ"


def ensure_photo_dir() -> Path:
    _PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    return _PHOTO_DIR


def card_photo_path(card_id: int) -> Path:
    return _PHOTO_DIR / f"{card_id}.jpg"


def get_card_photo_input(card_id: int, photo_path: Optional[str] = None):
    """FSInputFile если файл есть, иначе None."""
    path = Path(photo_path) if photo_path else card_photo_path(card_id)
    if path.is_file() and path.stat().st_size > 0:
        return FSInputFile(str(path))
    # fallback по card_id
    p2 = card_photo_path(card_id)
    if p2.is_file() and p2.stat().st_size > 0:
        return FSInputFile(str(p2))
    return None


def get_default_avatar_input():
    if DEFAULT_AVATAR_PATH.is_file() and DEFAULT_AVATAR_PATH.stat().st_size > 0:
        return FSInputFile(str(DEFAULT_AVATAR_PATH))
    return None


async def download_telegram_file(bot: Bot, file_id: str, dest: Path) -> bool:
    try:
        ensure_photo_dir()
        tg_file = await bot.get_file(file_id)
        await bot.download_file(tg_file.file_path, destination=dest)
        return dest.is_file() and dest.stat().st_size > 0
    except Exception as e:
        logger.error("download_telegram_file %s → %s: %s", file_id[:20], dest, e)
        return False


async def sync_card_photo(bot: Bot, card_id: int, photo_id: str) -> Optional[str]:
    """Скачать photo_id на диск, вернуть путь или None."""
    dest = card_photo_path(card_id)
    ok = await download_telegram_file(bot, photo_id, dest)
    if ok:
        return str(dest)
    return None


async def migrate_all_card_photos(bot: Bot, db) -> tuple[int, int]:
    """Скачать все photo_id из cards. Возвращает (ok, fail)."""
    ensure_photo_dir()
    cur = await db.execute("SELECT id, photo_id, photo_path FROM cards")
    rows = await cur.fetchall()
    ok = fail = 0
    for row in rows:
        cid = row["id"]
        path = row["photo_path"]
        if path and Path(path).is_file():
            ok += 1
            continue
        existing = card_photo_path(cid)
        if existing.is_file():
            await db.execute(
                "UPDATE cards SET photo_path = ? WHERE id = ?",
                (str(existing), cid),
            )
            ok += 1
            continue
        photo_id = row["photo_id"]
        if not photo_id:
            fail += 1
            continue
        new_path = await sync_card_photo(bot, cid, photo_id)
        if new_path:
            await db.execute(
                "UPDATE cards SET photo_path = ? WHERE id = ?",
                (new_path, cid),
            )
            ok += 1
        else:
            fail += 1
    await db.commit()
    return ok, fail


async def ensure_default_avatar(bot: Bot, file_id: str = DEFAULT_AVATAR_FILE_ID) -> bool:
    """Скачать дефолтный аватар на диск (если ещё нет)."""
    ensure_photo_dir()
    if DEFAULT_AVATAR_PATH.is_file() and DEFAULT_AVATAR_PATH.stat().st_size > 0:
        return True
    return await download_telegram_file(bot, file_id, DEFAULT_AVATAR_PATH)
