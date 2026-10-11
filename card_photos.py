"""Локальное хранение фото карточек с UUID-именами и оформлением по редкости.

Файлы: card_photos/{uuid}.jpg
Оформление: цветная рамка с ромбическими акцентами + ряд звёзд (1–6 по редкости).
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

# Цвета по редкости (RGB)
RARITY_FRAME_COLORS = {
    "common": (180, 180, 185),
    "uncommon": (48, 200, 110),
    "valuable": (168, 90, 230),
    "super_valuable": (255, 145, 45),
    "legendary": (255, 205, 50),
    "negative": (90, 20, 25),
}

RARITY_STARS = {
    "common": 1,
    "uncommon": 2,
    "valuable": 3,
    "super_valuable": 4,
    "legendary": 5,
    "negative": 1,
}

BORDER = 18
CORNER_SIZE = 28
STAR_AREA = 36
BG = (22, 22, 28)

# Единый вертикальный формат карточки (2:3)
CARD_W = 720
CARD_H = 1080
FILL_BG = (28, 28, 34)


def ensure_photo_dir() -> Path:
    _PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    return _PHOTO_DIR


def new_photo_uuid() -> str:
    return uuid.uuid4().hex


def photo_path_for_uuid(photo_uuid: str) -> Path:
    return _PHOTO_DIR / f"{photo_uuid}.jpg"


def get_card_photo_input(card_id: int, photo_path: Optional[str] = None):
    if photo_path:
        p = Path(photo_path)
        if p.is_file() and p.stat().st_size > 0:
            return FSInputFile(str(p))
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
        logger.warning("download via primary bot failed %s: %s", file_id[:24], e)
        return False


async def download_telegram_file_any(
    bots: list,
    file_id: str,
    dest: Path,
) -> bool:
    """Пробует скачать file_id по очереди ботами (admin → user)."""
    for i, bot in enumerate(bots):
        if bot is None:
            continue
        ok = await download_telegram_file(bot, file_id, dest)
        if ok:
            if i > 0:
                logger.info("download OK via fallback bot #%s for %s", i, file_id[:24])
            return True
    logger.error("download failed for all bots: %s", file_id[:24])
    return False


def _lighten(rgb: tuple, amount: int = 50) -> tuple:
    return tuple(min(255, c + amount) for c in rgb)


def _darken(rgb: tuple, amount: int = 50) -> tuple:
    return tuple(max(0, c - amount) for c in rgb)


def _draw_star(draw, cx: int, cy: int, outer_r: int, color: tuple, fill: tuple):
    """Пятиконечная звезда."""
    import math
    pts = []
    for i in range(10):
        angle = math.radians(-90 + i * 36)
        r = outer_r if i % 2 == 0 else outer_r * 0.42
        pts.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    draw.polygon(pts, fill=fill, outline=color)


def _draw_diamond(draw, cx: int, cy: int, size: int, fill, outline, width: int = 2):
    """Ромб с центром (cx, cy)."""
    half = size // 2
    pts = [
        (cx, cy - half),
        (cx + half, cy),
        (cx, cy + half),
        (cx - half, cy),
    ]
    draw.polygon(pts, fill=fill, outline=outline)
    if width > 1:
        # внутренний блик
        inner = max(4, size // 3)
        h = inner // 2
        pts2 = [
            (cx, cy - h),
            (cx + h, cy),
            (cx, cy + h),
            (cx - h, cy),
        ]
        draw.polygon(pts2, outline=_lighten(outline if isinstance(outline, tuple) else fill, 30))



def _rgba_to_rgb(img):
    """Прозрачность → мягкий градиент из средних цветов, не чёрный прямоугольник."""
    from PIL import Image, ImageFilter, ImageDraw
    if img.mode != "RGBA":
        return img.convert("RGB") if img.mode != "RGB" else img

    w, h = img.size
    # фон: сильный blur уменьшенной копии + лёгкий градиент сверху/снизу
    tiny = img.convert("RGB").resize(
        (max(8, w // 16), max(8, h // 16)), Image.Resampling.BILINEAR
    )
    bg = tiny.resize((w, h), Image.Resampling.BILINEAR).filter(
        ImageFilter.GaussianBlur(radius=12)
    )
    # затемнение краёв для «стеклянного» вида
    overlay = Image.new("RGB", (w, h), (20, 20, 28))
    bg = Image.blend(bg, overlay, 0.25)
    bg.paste(img, mask=img.split()[3])
    return bg


def _blur_fill_canvas(resized, canvas_size=(CARD_W, CARD_H)):
    """Letterbox: размытый zoom-фон (переливка) + чёткое фото по центру."""
    from PIL import Image, ImageFilter, ImageEnhance
    cw, ch = canvas_size
    nw, nh = resized.size

    # фон: cover-scale + сильный blur + чуть затемнить
    cover = max(cw / max(1, nw), ch / max(1, nh)) * 1.15
    bw, bh = max(1, int(nw * cover)), max(1, int(nh * cover))
    bg = resized.resize((bw, bh), Image.Resampling.BILINEAR)
    left, top = (bw - cw) // 2, (bh - ch) // 2
    bg = bg.crop((left, top, left + cw, top + ch))
    bg = bg.filter(ImageFilter.GaussianBlur(radius=28))
    bg = ImageEnhance.Brightness(bg).enhance(0.55)
    bg = ImageEnhance.Color(bg).enhance(1.15)

    # мягкий вертикальный виньет
    vignette = Image.new("L", (cw, ch), 0)
    from PIL import ImageDraw
    vd = ImageDraw.Draw(vignette)
    for y in range(ch):
        # ярче в центре, темнее у краёв
        t = abs(y - ch / 2) / (ch / 2)
        alpha = int(40 + 80 * (t ** 1.5))
        vd.line([(0, y), (cw, y)], fill=min(180, alpha))
    dark = Image.new("RGB", (cw, ch), (12, 12, 18))
    bg = Image.composite(dark, bg, vignette)

    ox, oy = (cw - nw) // 2, (ch - nh) // 2
    bg.paste(resized, (ox, oy))
    return bg


def normalize_to_portrait(img):
    """Единый вертикальный CARD_W×CARD_H с красивой «переливкой» вместо чёрных полей."""
    from PIL import Image
    img = _rgba_to_rgb(img)

    w, h = img.size
    target_ratio = CARD_W / CARD_H
    src_ratio = w / h if h else 1

    # почти 2:3 — center crop
    if abs(src_ratio - target_ratio) < 0.08:
        if src_ratio > target_ratio:
            new_w = int(h * target_ratio)
            left = (w - new_w) // 2
            img = img.crop((left, 0, left + new_w, h))
        else:
            new_h = int(w / target_ratio)
            top = (h - new_h) // 2
            img = img.crop((0, top, w, top + new_h))
        return img.resize((CARD_W, CARD_H), Image.Resampling.LANCZOS)

    # letterbox + blur fill
    scale = min(CARD_W / w, CARD_H / h)
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    resized = img.resize((nw, nh), Image.Resampling.LANCZOS)
    return _blur_fill_canvas(resized, (CARD_W, CARD_H))


def apply_rarity_frame(
    src: Path,
    dest: Path,
    rarity: str,
    frame_width: int = BORDER,
) -> bool:
    """Нормализация 720×1080 + цветная рамка + ромбы + звёзды."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        logger.warning("Pillow не установлен — рамка пропущена")
        if src != dest:
            dest.write_bytes(src.read_bytes())
        return dest.is_file()

    try:
        img = Image.open(src)
        img = normalize_to_portrait(img)

        color = RARITY_FRAME_COLORS.get(rarity, (128, 128, 128))
        light = _lighten(color, 55)
        dark = _darken(color, 40)
        n_stars = RARITY_STARS.get(rarity, 1)

        w, h = img.size  # CARD_W, CARD_H
        pad = frame_width + 8
        star_h = STAR_AREA
        new_w = w + 2 * pad
        new_h = h + 2 * pad + star_h

        canvas = Image.new("RGB", (new_w, new_h), BG)
        draw = ImageDraw.Draw(canvas)

        draw.rectangle([0, 0, new_w - 1, new_h - 1], outline=(12, 12, 16), width=3)
        draw.rectangle([4, 4, new_w - 5, 4 + frame_width], fill=color)
        draw.rectangle([4, 4, 4 + frame_width, new_h - star_h - 5], fill=color)
        draw.rectangle([new_w - 5 - frame_width, 4, new_w - 5, new_h - star_h - 5], fill=color)
        draw.rectangle(
            [4, new_h - star_h - 5 - frame_width, new_w - 5, new_h - star_h - 5],
            fill=color,
        )

        inner = 4 + frame_width
        draw.rectangle(
            [inner, inner, new_w - inner - 1, new_h - star_h - inner - 1],
            outline=light, width=2,
        )
        draw.rectangle(
            [inner + 2, inner + 2, new_w - inner - 3, new_h - star_h - inner - 3],
            outline=dark, width=1,
        )

        cs = CORNER_SIZE
        corners = [
            (pad // 2 + 4, pad // 2 + 4),
            (new_w - pad // 2 - 4, pad // 2 + 4),
            (pad // 2 + 4, new_h - star_h - pad // 2 - 4),
            (new_w - pad // 2 - 4, new_h - star_h - pad // 2 - 4),
        ]
        for cx, cy in corners:
            _draw_diamond(draw, cx, cy, cs, fill=dark, outline=light)
            _draw_diamond(draw, cx, cy, cs // 2, fill=color, outline=light)

        canvas.paste(img, (pad, pad))

        star_top = new_h - star_h
        draw.rectangle([0, star_top, new_w, new_h], fill=(18, 18, 24))
        draw.line([(pad, star_top + 2), (new_w - pad, star_top + 2)], fill=color, width=2)

        star_r = 11
        gap = 8
        total_w = n_stars * (star_r * 2) + max(0, n_stars - 1) * gap
        start_x = (new_w - total_w) // 2 + star_r
        cy = star_top + star_h // 2 + 2
        for i in range(n_stars):
            cx = start_x + i * (star_r * 2 + gap)
            _draw_star(draw, cx, cy, star_r, color=light, fill=color)
            draw.ellipse([cx - 2, cy - 4, cx + 2, cy], fill=light)

        ensure_photo_dir()
        canvas.save(dest, "JPEG", quality=93, optimize=True)
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
    fallback_bot: Optional[Bot] = None,
) -> Optional[Tuple[str, str]]:
    """Скачать → рамка → UUID.jpg. Сначала bot, при ошибке — fallback_bot (user)."""
    ensure_photo_dir()
    photo_uuid = existing_uuid or new_photo_uuid()
    dest = photo_path_for_uuid(photo_uuid)
    tmp = _PHOTO_DIR / f"_tmp_{photo_uuid}.jpg"

    bots = [bot]
    if fallback_bot is not None and fallback_bot is not bot:
        bots.append(fallback_bot)

    ok = await download_telegram_file_any(bots, photo_id, tmp)
    if not ok:
        tmp.unlink(missing_ok=True)
        return None

    framed = apply_rarity_frame(tmp, dest, rarity)
    tmp.unlink(missing_ok=True)
    if not framed:
        return None
    return photo_uuid, str(dest)


async def migrate_all_card_photos(
    bot: Bot, db, fallback_bot: Optional[Bot] = None
) -> tuple[int, int]:
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
            bot,
            row["id"],
            photo_id,
            rarity=rarity,
            existing_uuid=row["photo_uuid"],
            fallback_bot=fallback_bot,
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


async def reframe_all_photos(
    bot: Bot, db, fallback_bot: Optional[Bot] = None
) -> tuple[int, int]:
    """
    Перерисовка: скачать сырое фото (admin → user token) и наложить рамку.
    """
    ensure_photo_dir()
    cur = await db.execute(
        "SELECT id, photo_id, photo_path, photo_uuid, rarity FROM cards"
    )
    rows = await cur.fetchall()
    ok = fail = 0
    for row in rows:
        rarity = row["rarity"] or "common"
        photo_uuid = row["photo_uuid"] or new_photo_uuid()
        dest = photo_path_for_uuid(photo_uuid)

        if row["photo_id"]:
            result = await sync_card_photo(
                bot,
                row["id"],
                row["photo_id"],
                rarity=rarity,
                existing_uuid=photo_uuid,
                fallback_bot=fallback_bot,
            )
            if result:
                puuid, ppath = result
                await db.execute(
                    "UPDATE cards SET photo_path = ?, photo_uuid = ? WHERE id = ?",
                    (ppath, puuid, row["id"]),
                )
                ok += 1
            else:
                # fallback: локальный файл без перекачки
                src = None
                if row["photo_path"] and Path(row["photo_path"]).is_file():
                    src = Path(row["photo_path"])
                if src is not None:
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
                else:
                    fail += 1
            continue

        src = None
        if row["photo_path"] and Path(row["photo_path"]).is_file():
            src = Path(row["photo_path"])
        if src is None:
            fail += 1
            continue
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
    ensure_photo_dir()
    cur = await db.execute("SELECT id, photo_path, photo_uuid FROM cards")
    rows = await cur.fetchall()
    ok = fail = 0
    for row in rows:
        old_path = None
        if row["photo_path"] and Path(row["photo_path"]).is_file():
            old_path = Path(row["photo_path"])
        else:
            legacy = _PHOTO_DIR / f"{row['id']}.jpg"
            if legacy.is_file():
                old_path = legacy
        if old_path is None:
            fail += 1
            continue
        if row["photo_uuid"] and old_path.name == f"{row['photo_uuid']}.jpg":
            ok += 1
            continue
        photo_uuid = row["photo_uuid"] or new_photo_uuid()
        new_path = photo_path_for_uuid(photo_uuid)
        try:
            if old_path.resolve() != new_path.resolve():
                new_path.write_bytes(old_path.read_bytes())
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
