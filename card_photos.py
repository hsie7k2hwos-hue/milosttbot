"""Локальное хранение фото карточек с UUID-именами и цветными рамками по редкости.

Файлы: card_photos/{uuid}.jpg  (нельзя угадать по card_id)
В БД: cards.photo_path = полный путь или имя файла, cards.photo_uuid = uuid.
"""
from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import Optional, Tuple

from aiogram import Bot
from aiogram.types import FSInputFile

logger = logging.getLogger(__name__)

_PHOTO_DIR = Path(os.getenv("CARD_PHOTOS_DIR", "/app/data/card_photos"))
DEFAULT_AVATAR_PATH = _PHOTO_DIR / "default_avatar.jpg"
DEFAULT_AVATAR_FILE_ID = (
    "AgACAgIAAxkBAAID12qql3EFpnb2HwTCE7Yn_Ri1TQsNAAKRIGsbfHlYSVUpxfU75O60AQADAgADeAADPQQ"
)

# Цвета рамок (RGB) по ключу редкости
RARITY_FRAME_COLORS = {
    "common": (170, 170, 170),
    "rare": (70, 130, 255),
    "uncommon": (50, 200, 100),
    "valuable": (160, 80, 220),
    "super_valuable": (255, 140, 40),
    "legendary": (255, 200, 40),
}

FRAME_WIDTH = 14
OUTER_PAD = 6


def ensure_photo_dir() -> Path:
    _PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    return _PHOTO_DIR


def new_photo_uuid() -> str:
    return uuid.uuid4().hex


def photo_path_for_uuid(photo_uuid: str) -> Path:
    return _PHOTO_DIR / f"{photo_uuid}.jpg"


def get_card_photo_input(card_id: int, photo_path: Optional[str] = None):
    """FSInputFile если файл есть, иначе None. card_id не используется для поиска по имени."""
    if photo_path:
        p = Path(photo_path)
        if p.is_file() and p.stat().st_size > 0:
            return FSInputFile(str(p))
        # только имя файла
        cand = _PHOTO_DIR / p.name
        if cand.is_file() and cand.stat().st_size > 0:
            return FSInputFile(str(cand))
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
        logger.error("download_telegram_file %s → %s: %s", file_id[:24], dest, e)
        return False


def apply_rarity_frame(
    src: Path,
    dest: Path,
    rarity: str,
    frame_width: int = FRAME_WIDTH,
) -> bool:
    """Накладывает цветную рамку редкости. Требует Pillow."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        logger.warning("Pillow не установлен — рамка пропущена")
        if src != dest:
            dest.write_bytes(src.read_bytes())
        return dest.is_file()

    try:
        img = Image.open(src).convert("RGB")
        color = RARITY_FRAME_COLORS.get(rarity, (128, 128, 128))
        w, h = img.size
        pad = frame_width + OUTER_PAD
        new_w, new_h = w + 2 * pad, h + 2 * pad
        canvas = Image.new("RGB", (new_w, new_h), (30, 30, 35))
        draw = ImageDraw.Draw(canvas)
        # внешняя тонкая обводка
        draw.rectangle([0, 0, new_w - 1, new_h - 1], outline=(20, 20, 22), width=2)
        # основная цветная рамка
        draw.rectangle(
            [OUTER_PAD, OUTER_PAD, new_w - OUTER_PAD - 1, new_h - OUTER_PAD - 1],
            outline=color,
            width=frame_width,
        )
        # внутренний блик
        light = tuple(min(255, c + 40) for c in color)
        draw.rectangle(
            [
                OUTER_PAD + 2,
                OUTER_PAD + 2,
                new_w - OUTER_PAD - 3,
                new_h - OUTER_PAD - 3,
            ],
            outline=light,
            width=2,
        )
        canvas.paste(img, (pad, pad))
        ensure_photo_dir()
        canvas.save(dest, "JPEG", quality=92)
        return dest.is_file() and dest.stat().st_size > 0
    except Exception as e:
        logger.error("apply_rarity_frame: %s", e)
        return False


async def sync_card_photo(
    bot: Bot,
    card_id: int,
    photo_id: str,
    rarity: str = "common",
    existing_uuid: Optional[str] = None,
) -> Optional[Tuple[str, str]]:
    """
    Скачивает photo_id, накладывает рамку, сохраняет как UUID.jpg.
    Возвращает (photo_uuid, photo_path) или None.
    """
    ensure_photo_dir()
    photo_uuid = existing_uuid or new_photo_uuid()
    dest = photo_path_for_uuid(photo_uuid)
    tmp = _PHOTO_DIR / f"_tmp_{photo_uuid}.jpg"

    ok = await download_telegram_file(bot, photo_id, tmp)
    if not ok:
        tmp.unlink(missing_ok=True)
        return None

    framed = apply_rarity_frame(tmp, dest, rarity)
    tmp.unlink(missing_ok=True)
    if not framed:
        return None
    return photo_uuid, str(dest)


async def migrate_all_card_photos(bot: Bot, db) -> tuple[int, int]:
    """Скачать все photo_id без photo_path / без файла. (ok, fail)."""
    ensure_photo_dir()
    cur = await db.execute(
        "SELECT id, photo_id, photo_path, photo_uuid, rarity FROM cards"
    )
    rows = await cur.fetchall()
    ok = fail = 0
    for row in rows:
        path = row["photo_path"]
        if path and Path(path).is_file():
            ok += 1
            continue
        photo_id = row["photo_id"]
        if not photo_id:
            fail += 1
            continue
        rarity = row["rarity"] or "common"
        result = await sync_card_photo(
            bot, row["id"], photo_id, rarity=rarity, existing_uuid=row["photo_uuid"]
        )
        if result:
            puuid, ppath = result
            await db.execute(
                "UPDATE cards SET photo_path = ?, photo_uuid = ? WHERE id = ?",
                (ppath, puuid, row["id"]),
            )
            ok += 1
        else:
            fail += 1
    await db.commit()
    return ok, fail


async def reframe_all_photos(bot: Bot, db) -> tuple[int, int]:
    """Переналожить рамки на уже скачанные фото (по текущей редкости)."""
    ensure_photo_dir()
    cur = await db.execute(
        "SELECT id, photo_id, photo_path, photo_uuid, rarity FROM cards"
    )
    rows = await cur.fetchall()
    ok = fail = 0
    for row in rows:
        rarity = row["rarity"] or "common"
        src = None
        if row["photo_path"] and Path(row["photo_path"]).is_file():
            src = Path(row["photo_path"])
        elif row["photo_uuid"]:
            cand = photo_path_for_uuid(row["photo_uuid"])
            if cand.is_file():
                src = cand

        if src is None:
            # попробовать скачать заново
            if not row["photo_id"]:
                fail += 1
                continue
            result = await sync_card_photo(
                bot, row["id"], row["photo_id"], rarity=rarity, existing_uuid=row["photo_uuid"]
            )
            if result:
                puuid, ppath = result
                await db.execute(
                    "UPDATE cards SET photo_path = ?, photo_uuid = ? WHERE id = ?",
                    (ppath, puuid, row["id"]),
                )
                ok += 1
            else:
                fail += 1
            continue

        photo_uuid = row["photo_uuid"] or new_photo_uuid()
        dest = photo_path_for_uuid(photo_uuid)
        # если src == dest — пишем во временный
        tmp = _PHOTO_DIR / f"_reframe_{photo_uuid}.jpg"
        try:
            tmp.write_bytes(src.read_bytes())
            if apply_rarity_frame(tmp, dest, rarity):
                await db.execute(
                    "UPDATE cards SET photo_path = ?, photo_uuid = ? WHERE id = ?",
                    (str(dest), photo_uuid, row["id"]),
                )
                ok += 1
            else:
                fail += 1
        finally:
            tmp.unlink(missing_ok=True)
    await db.commit()
    return ok, fail


async def migrate_to_uuid_names(db) -> tuple[int, int]:
    """Переименовать старые {id}.jpg → {uuid}.jpg и обновить БД."""
    ensure_photo_dir()
    cur = await db.execute("SELECT id, photo_path, photo_uuid FROM cards")
    rows = await cur.fetchall()
    ok = fail = 0
    for row in rows:
        old_path = None
        if row["photo_path"] and Path(row["photo_path"]).is_file():
            old_path = Path(row["photo_path"])
        else:
            # legacy: card_id.jpg
            legacy = _PHOTO_DIR / f"{row['id']}.jpg"
            if legacy.is_file():
                old_path = legacy

        if old_path is None:
            fail += 1
            continue

        # уже UUID-имя?
        if row["photo_uuid"] and old_path.name == f"{row['photo_uuid']}.jpg":
            ok += 1
            continue

        photo_uuid = row["photo_uuid"] or new_photo_uuid()
        new_path = photo_path_for_uuid(photo_uuid)
        try:
            if old_path.resolve() != new_path.resolve():
                new_path.write_bytes(old_path.read_bytes())
                # не удаляем старый сразу — на всякий случай
            await db.execute(
                "UPDATE cards SET photo_path = ?, photo_uuid = ? WHERE id = ?",
                (str(new_path), photo_uuid, row["id"]),
            )
            ok += 1
        except Exception as e:
            logger.error("migrate uuid card %s: %s", row["id"], e)
            fail += 1
    await db.commit()
    return ok, fail


async def ensure_default_avatar(bot: Bot, file_id: str = DEFAULT_AVATAR_FILE_ID) -> bool:
    ensure_photo_dir()
    if DEFAULT_AVATAR_PATH.is_file() and DEFAULT_AVATAR_PATH.stat().st_size > 0:
        return True
    return await download_telegram_file(bot, file_id, DEFAULT_AVATAR_PATH)
