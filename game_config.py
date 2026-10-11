"""
Общие игровые константы и загрузка rarity_config / settings из БД.

DEFAULT_RARITIES — сбалансированный дефолт (вес / награда).
STREAK, dice, nickname — сбалансированы относительно наград за карты.
"""
from __future__ import annotations

import os
from typing import Any, Optional

# ── Дефолтные редкости (сумма weight = 100) ──
DEFAULT_RARITIES: dict[str, dict[str, Any]] = {
    "common":         {"icon": "⚪", "name": "Обычная",     "weight": 40, "reward": 10},
    "rare":           {"icon": "🔵", "name": "Редкая",      "weight": 25, "reward": 25},
    "uncommon":       {"icon": "🟢", "name": "Необычная",   "weight": 15, "reward": 45},
    "valuable":       {"icon": "🟣", "name": "Ценная",      "weight": 10, "reward": 75},
    "super_valuable": {"icon": "🟠", "name": "Сверхценная", "weight": 7,  "reward": 120},
    "legendary":      {"icon": "🟡", "name": "Легендарная", "weight": 3,  "reward": 200},
}

RARITY_ORDER = list(DEFAULT_RARITIES.keys())

OLD_RARITY_MAP = {
    "common": "common",
    "rare": "rare",
    "epic": "uncommon",
    "mythical": "valuable",
    "legendary": "legendary",
}

# Стрик: бонус растёт плавно, после 30 дней — 30, дальше без скачка «+35»
# (streak, bonus) — бонус при достижении этого порога
STREAK_BONUSES = [
    (2, 10),
    (7, 15),
    (14, 20),
    (30, 30),
    (60, 40),
    (float("inf"), 40),
]

COOLDOWN_SECONDS = 3 * 3600
API_COOLDOWN_SECONDS = 4 * 3600

# Смена ника ≈ 12 обычных карточек
NICKNAME_COST = 120

# Кубик: разброс соразмерен ordinary reward
DICE_COOLDOWN_SECONDS = 10 * 60
DICE_MIN_BALANCE = 20
DICE_SPIN_DELAY = 3
DICE_VALUE_MAP = {1: -15, 2: -8, 3: 0, 4: 8, 5: 15, 6: 25}

ROLES = {
    "user":       {"icon": "👤", "name": "Игрок"},
    "admin":      {"icon": "🛡", "name": "Администратор"},
    "superadmin": {"icon": "👑", "name": "Владелец"},
    "banned":     {"icon": "🚫", "name": "Заблокирован"},
}

SUPERADMIN_IDS = {
    int(x.strip())
    for x in (os.getenv("SUPERADMIN_IDS") or os.getenv("SUPERADMIN_ID") or "").split(",")
    if x.strip().isdigit()
}

DB_NAME = os.getenv("DB_NAME", "/app/data/cards_game.db")


def streak_bonus_for(streak: int) -> int:
    if streak <= 1:
        return 0
    for days, bonus in STREAK_BONUSES:
        if streak <= days:
            return bonus
    return 40


async def ensure_config_tables(db) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS rarity_config (
            rarity_key TEXT PRIMARY KEY,
            weight INTEGER NOT NULL,
            reward INTEGER NOT NULL
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    # seed rarities
    for key, info in DEFAULT_RARITIES.items():
        await db.execute(
            """INSERT OR IGNORE INTO rarity_config (rarity_key, weight, reward)
               VALUES (?, ?, ?)""",
            (key, info["weight"], info["reward"]),
        )
    # seed settings
    await db.execute(
        """INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('user_bot_enabled', '1')"""
    )
    await db.commit()


async def load_rarities(db) -> dict[str, dict[str, Any]]:
    """RARITIES с weight/reward из БД, icon/name из DEFAULT."""
    await ensure_config_tables(db)
    cur = await db.execute("SELECT rarity_key, weight, reward FROM rarity_config")
    rows = {r[0]: r for r in await cur.fetchall()}
    out = {}
    for key, info in DEFAULT_RARITIES.items():
        row = rows.get(key)
        out[key] = {
            "icon": info["icon"],
            "name": info["name"],
            "weight": int(row[1]) if row else info["weight"],
            "reward": int(row[2]) if row else info["reward"],
        }
    return out


async def reset_rarities_to_default(db) -> None:
    await ensure_config_tables(db)
    for key, info in DEFAULT_RARITIES.items():
        await db.execute(
            """INSERT INTO rarity_config (rarity_key, weight, reward) VALUES (?, ?, ?)
               ON CONFLICT(rarity_key) DO UPDATE SET weight=excluded.weight, reward=excluded.reward""",
            (key, info["weight"], info["reward"]),
        )
    await db.commit()


async def set_rarity_params(db, key: str, weight: Optional[int] = None, reward: Optional[int] = None) -> bool:
    if key not in DEFAULT_RARITIES:
        return False
    await ensure_config_tables(db)
    cur = await db.execute(
        "SELECT weight, reward FROM rarity_config WHERE rarity_key = ?", (key,)
    )
    row = await cur.fetchone()
    w = weight if weight is not None else (row[0] if row else DEFAULT_RARITIES[key]["weight"])
    r = reward if reward is not None else (row[1] if row else DEFAULT_RARITIES[key]["reward"])
    if w < 0 or r < 0:
        return False
    await db.execute(
        """INSERT INTO rarity_config (rarity_key, weight, reward) VALUES (?, ?, ?)
           ON CONFLICT(rarity_key) DO UPDATE SET weight=excluded.weight, reward=excluded.reward""",
        (key, w, r),
    )
    await db.commit()
    return True


async def get_setting(db, key: str, default: str = "") -> str:
    await ensure_config_tables(db)
    cur = await db.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))
    row = await cur.fetchone()
    return row[0] if row else default


async def set_setting(db, key: str, value: str) -> None:
    await ensure_config_tables(db)
    await db.execute(
        """INSERT INTO bot_settings (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        (key, value),
    )
    await db.commit()


async def is_user_bot_enabled(db) -> bool:
    return (await get_setting(db, "user_bot_enabled", "1")) in ("1", "true", "yes", "on")
