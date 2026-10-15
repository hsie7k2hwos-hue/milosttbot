"""
Обработка фотографий карточек: нормализация, рамки по редкости, звёзды.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Optional, Tuple, Union

from aiogram import Bot
from aiogram.types import FSInputFile

from config import (
    PHOTOS_DIR,
    CARD_W,
    CARD_H,
    BORDER,
    STAR_AREA,
    CORNER_SIZE,
    BG_COLOR,
    DEFAULT_RARITIES,
)

logger = logging.getLogger("photos")


def ensure_photo_dir() -> Path:
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    return PHOTOS_DIR


def new_photo_uuid() -> str:
    return uuid.uuid4().hex


def photo_path_for_uuid(photo_uuid: str) -> Path:
    return PHOTOS_DIR / f"{photo_uuid}.webp"


def find_existing_photo(photo_uuid: str, fallback: Optional[str] = None) -> Optional[Path]:
    for ext in (".webp", ".jpg", ".png"):
        p = PHOTOS_DIR / f"{photo_uuid}{ext}"
        if p.is_file() and p.stat().st_size > 0:
            return p
    if fallback and Path(fallback).is_file():
        return Path(fallback)
    return None


def get_card_photo_input(
    card_id: int,
    photo_path: Optional[str] = None,
) -> Optional[Union[FSInputFile, str]]:
    if photo_path:
        p = Path(photo_path)
        if p.is_file() and p.stat().st_size > 0:
            return FSInputFile(p)
    return None


def _lighten(color: tuple, amount: int = 50) -> tuple:
    return tuple(min(255, c + amount) for c in color)


def _darken(color: tuple, amount: int = 40) -> tuple:
    return tuple(max(0, c - amount) for c in color)


def _draw_diamond(draw, cx: int, cy: int, size: int, fill, outline) -> None:
    half = size // 2
    points = [
        (cx, cy - half),
        (cx + half, cy),
        (cx, cy + half),
        (cx - half, cy),
    ]
    draw.polygon(points, fill=fill, outline=outline)


def _draw_star(draw, cx: int, cy: int, r: int, color, fill) -> None:
    import math
    points = []
    for i in range(10):
        angle = math.pi / 2 + i * math.pi / 5
        radius = r if i % 2 == 0 else r * 0.4
        points.append((cx + radius * math.cos(angle), cy - radius * math.sin(angle)))
    draw.polygon(points, fill=fill, outline=color)


def _rgba_to_rgb(img):
    from PIL import Image
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (20, 20, 28))
        bg.paste(img, mask=img.split()[3])
        return bg
    return img.convert("RGB")


def _blur_fill_canvas(resized, canvas_size=(CARD_W, CARD_H)):
    from PIL import Image, ImageFilter, ImageEnhance, ImageDraw

    cw, ch = canvas_size
    nw, nh = resized.size

    cover = max(cw / max(1, nw), ch / max(1, nh)) * 1.15
    bw, bh = max(1, int(nw * cover)), max(1, int(nh * cover))
    bg = resized.resize((bw, bh), Image.Resampling.BILINEAR)
    left, top = (bw - cw) // 2, (bh - ch) // 2
    bg = bg.crop((left, top, left + cw, top + ch))
    bg = bg.filter(ImageFilter.GaussianBlur(radius=28))
    bg = ImageEnhance.Brightness(bg).enhance(0.55)
    bg = ImageEnhance.Color(bg).enhance(1.15)

    vignette = Image.new("L", (cw, ch), 0)
    vd = ImageDraw.Draw(vignette)
    for y in range(ch):
        t = abs(y - ch / 2) / (ch / 2)
        alpha = int(40 + 80 * (t ** 1.5))
        vd.line([(0, y), (cw, y)], fill=min(180, alpha))
    dark = Image.new("RGB", (cw, ch), (12, 12, 18))
    bg = Image.composite(dark, bg, vignette)

    ox, oy = (cw - nw) // 2, (ch - nh) // 2
    bg.paste(resized, (ox, oy))
    return bg


def normalize_to_portrait(img):
    from PIL import Image

    img = _rgba_to_rgb(img)
    w, h = img.size
    target_ratio = CARD_W / CARD_H
    src_ratio = w / h if h else 1

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

    scale = min(CARD_W / w, CARD_H / h)
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    resized = img.resize((nw, nh), Image.Resampling.LANCZOS)
    return _blur_fill_canvas(resized, (CARD_W, CARD_H))


def apply_rarity_frame(src: Path, dest: Path, rarity: str) -> bool:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        logger.warning("Pillow not installed")
        if src != dest:
            dest.write_bytes(src.read_bytes())
        return dest.is_file()

    try:
        img = Image.open(src)
        img = normalize_to_portrait(img)

        info = DEFAULT_RARITIES.get(rarity, DEFAULT_RARITIES["common"])
        color = tuple(info["color"])
        light = _lighten(color, 55)
        dark = _darken(color, 40)
        n_stars = info.get("stars", 1)

        w, h = img.size
        pad = BORDER + 8
        star_h = STAR_AREA
        new_w = w + 2 * pad
        new_h = h + 2 * pad + star_h

        canvas = Image.new("RGB", (new_w, new_h), BG_COLOR)
        draw = ImageDraw.Draw(canvas)

        # outer border
        draw.rectangle([0, 0, new_w - 1, new_h - 1], outline=(12, 12, 16), width=3)
        # colored frame
        draw.rectangle([4, 4, new_w - 5, 4 + BORDER], fill=color)
        draw.rectangle([4, 4, 4 + BORDER, new_h - star_h - 5], fill=color)
        draw.rectangle([new_w - 5 - BORDER, 4, new_w - 5, new_h - star_h - 5], fill=color)
        draw.rectangle(
            [4, new_h - star_h - 5 - BORDER, new_w - 5, new_h - star_h - 5], fill=color
        )

        inner = 4 + BORDER
        draw.rectangle(
            [inner, inner, new_w - inner - 1, new_h - star_h - inner - 1],
            outline=light, width=2,
        )
        draw.rectangle(
            [inner + 2, inner + 2, new_w - inner - 3, new_h - star_h - inner - 3],
            outline=dark, width=1,
        )

        # corner diamonds
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

        # stars area
        star_top = new_h - star_h
        draw.rectangle([0, star_top, new_w, new_h], fill=(18, 18, 24))
        draw.line([(pad, star_top + 2), (new_w - pad, star_top + 2)], fill=color, width=2)

        if n_stars > 0:
            star_r = 11
            gap = 8
            total_w = n_stars * (star_r * 2) + max(0, n_stars - 1) * gap
            start_x = (new_w - total_w) // 2 + star_r
            cy = star_top + star_h // 2 + 2
            for i in range(n_stars):
                cx = start_x + i * (star_r * 2 + gap)
                _draw_star(draw, cx, cy, star_r, color=light, fill=color)

        ensure_photo_dir()
        dest = Path(dest)
        if dest.suffix.lower() != ".webp":
            dest = dest.with_suffix(".webp")
        canvas.save(dest, "WEBP", quality=88)
        return dest.is_file() and dest.stat().st_size > 0
    except Exception as e:
        logger.error("apply_rarity_frame: %s", e)
        return False


async def download_telegram_file(bot: Bot, file_id: str, dest: Path) -> bool:
    try:
        tg_file = await bot.get_file(file_id)
        data = await bot.download_file(tg_file.file_path)
        dest.write_bytes(data.read())
        return dest.is_file() and dest.stat().st_size > 0
    except Exception as e:
        logger.error("download_telegram_file: %s", e)
        return False


async def sync_card_photo(
    bot: Bot,
    card_id: int,
    photo_id: str,
    rarity: str = "common",
    existing_uuid: Optional[str] = None,
    fallback_bot: Optional[Bot] = None,
) -> Optional[Tuple[str, str]]:
    """Скачать → нормализовать → рамка → сохранить. Возвращает (uuid, path)."""
    ensure_photo_dir()
    photo_uuid = existing_uuid or new_photo_uuid()
    dest = photo_path_for_uuid(photo_uuid)
    tmp = PHOTOS_DIR / f"_tmp_{photo_uuid}.jpg"

    bots = [bot]
    if fallback_bot is not None and fallback_bot is not bot:
        bots.append(fallback_bot)

    ok = False
    for b in bots:
        ok = await download_telegram_file(b, photo_id, tmp)
        if ok:
            break

    if not ok:
        tmp.unlink(missing_ok=True)
        return None

    framed = apply_rarity_frame(tmp, dest, rarity)
    tmp.unlink(missing_ok=True)
    if not framed:
        return None

    actual = find_existing_photo(photo_uuid, str(dest))
    if actual is None:
        return None
    return photo_uuid, str(actual)
