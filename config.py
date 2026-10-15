"""
Конфигурация карточной игры.
Все настройки в одном месте.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

# ── Токены ──────────────────────────────────────────────────────────────────
BOT_TOKEN: str = (os.getenv("BOT_TOKEN") or "").strip()
ADMIN_BOT_TOKEN: str = (
    os.getenv("ADMIN_BOT_TOKEN") or os.getenv("BOT_TOKEN_ADMIN") or ""
).strip()

# ── Пути ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DB_PATH = Path(os.getenv("DB_NAME", DATA_DIR / "cards_game.db"))
PHOTOS_DIR = Path(os.getenv("PHOTOS_DIR", DATA_DIR / "card_photos"))
AVATARS_DIR = Path(os.getenv("AVATARS_DIR", DATA_DIR / "user_avatars"))
LOG_DIR = Path(os.getenv("LOG_DIR", DATA_DIR / "logs"))

# ── Суперадмины ─────────────────────────────────────────────────────────────
SUPERADMIN_IDS: set[int] = {
    int(x.strip())
    for x in (os.getenv("SUPERADMIN_IDS") or os.getenv("SUPERADMIN_ID") or "").split(",")
    if x.strip().isdigit()
}

# ── Игровые константы ───────────────────────────────────────────────────────
COOLDOWN_SECONDS = 3 * 3600          # 3 часа между карточками
STREAK_EXPIRE_SECONDS = 24 * 3600    # стрик сгорает через 24ч
NICKNAME_COST = 150
DICE_COOLDOWN_SECONDS = 10 * 60
DICE_MIN_BALANCE = 30
DICE_SPIN_DELAY = 3.0

# Награды кубика: значение кости → дельта монет
DICE_MAP: dict[int, int] = {
    1: -20,
    2: -10,
    3: 0,
    4: 10,
    5: 20,
    6: 35,
}

# Бонусы стрика: (дней, бонус_монет)
STREAK_BONUSES: list[tuple[float, int]] = [
    (2, 10),
    (7, 20),
    (14, 30),
    (30, 45),
    (60, 60),
    (float("inf"), 60),
]

# ── Редкости (дефолт) ───────────────────────────────────────────────────────
DEFAULT_RARITIES: dict[str, dict[str, Any]] = {
    "common": {
        "icon": "⚪",
        "name": "Обычная",
        "weight": 45,
        "reward": 15,
        "color": (160, 160, 160),
        "stars": 1,
    },
    "uncommon": {
        "icon": "🟢",
        "name": "Необычная",
        "weight": 25,
        "reward": 40,
        "color": (80, 180, 80),
        "stars": 2,
    },
    "valuable": {
        "icon": "🟣",
        "name": "Ценная",
        "weight": 14,
        "reward": 80,
        "color": (160, 80, 200),
        "stars": 3,
    },
    "super_valuable": {
        "icon": "🟠",
        "name": "Сверхценная",
        "weight": 7,
        "reward": 150,
        "color": (230, 140, 40),
        "stars": 4,
    },
    "legendary": {
        "icon": "🟡",
        "name": "Легендарная",
        "weight": 2,
        "reward": 300,
        "color": (240, 200, 40),
        "stars": 5,
    },
    "negative": {
        "icon": "⬛",
        "name": "Негативная",
        "weight": 7,
        "reward": -15,
        "color": (40, 40, 50),
        "stars": 0,
    },
}

RARITY_ORDER = list(DEFAULT_RARITIES.keys())

# ── Роли ────────────────────────────────────────────────────────────────────
ROLES: dict[str, dict[str, str]] = {
    "user": {"icon": "👤", "name": "Игрок"},
    "admin": {"icon": "🛡", "name": "Администратор"},
    "superadmin": {"icon": "👑", "name": "Владелец"},
    "banned": {"icon": "🚫", "name": "Заблокирован"},
}

# ── Фото ────────────────────────────────────────────────────────────────────
CARD_W = 720
CARD_H = 1080
BORDER = 18
STAR_AREA = 56
CORNER_SIZE = 22
BG_COLOR = (18, 18, 24)

# ── Прочее ──────────────────────────────────────────────────────────────────
PER_PAGE = 6
GROUP_AUTODELETE = 30
BOT_USERNAME = (os.getenv("BOT_USERNAME") or "cardsbot").lstrip("@").strip()

# Эффект при легендарных карточках (Telegram message_effect_id)
PARTY_EFFECT = "5046509860389126442"
