"""
Общие игровые константы и загрузка rarity_config / settings из БД.
"""
from __future__ import annotations

import os
from typing import Any, Optional

DEFAULT_RARITIES: dict[str, dict[str, Any]] = {
    "common":         {"icon": "⚪️", "name": "Обычная",     "weight": 45, "reward": 15},
    "uncommon":       {"icon": "🟢", "name": "Необычная",   "weight": 25, "reward": 40},
    "valuable":       {"icon": "🟣", "name": "Ценная",      "weight": 14, "reward": 80},
    "super_valuable": {"icon": "🟠", "name": "Сверхценная", "weight": 7,  "reward": 150},
    "legendary":      {"icon": "🟡", "name": "Легендарная", "weight": 2,  "reward": 300},
    "negative":       {"icon": "⚫️", "name": "Негативная",  "weight": 7,  "reward": -15},
}

RARITY_ORDER = list(DEFAULT_RARITIES.keys())

OLD_RARITY_MAP = {
    "common": "common",
    "rare": "uncommon",
    "epic": "uncommon",
    "mythical": "valuable",
    "legendary": "legendary",
    "negative": "negative",
}

STREAK_BONUSES = [
    (2, 10),
    (7, 20),
    (14, 30),
    (30, 45),
    (60, 60),
    (float("inf"), 60),
]

COOLDOWN_SECONDS = 3 * 3600
API_COOLDOWN_SECONDS = 4 * 3600
NICKNAME_COST = 150

DICE_COOLDOWN_SECONDS = 10 * 60
DICE_MIN_BALANCE = 30
DICE_SPIN_DELAY = 3
DICE_VALUE_MAP = {1: -20, 2: -10, 3: 0, 4: 10, 5: 20, 6: 35}

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

APP_REVISION = "2.4.0"
APP_REVISION_NOTES = (
    "blur-fill карточек, /revision, rarity 45/25/14/7/2/7, "
    "negative -15, economy rescale, Gemini, drop_legacy"
)


def streak_bonus_for(streak: int) -> int:
    if streak <= 1:
        return 0
    for days, bonus in STREAK_BONUSES:
        if streak <= days:
            return bonus
    return 60


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
    for key, info in DEFAULT_RARITIES.items():
        await db.execute(
            """INSERT OR IGNORE INTO rarity_config (rarity_key, weight, reward)
               VALUES (?, ?, ?)""",
            (key, info["weight"], info["reward"]),
        )
    await db.execute("DELETE FROM rarity_config WHERE rarity_key = 'rare'")
    await db.execute(
        """INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('user_bot_enabled', '1')"""
    )
    await db.commit()


async def load_rarities(db) -> dict[str, dict[str, Any]]:
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
    await db.execute(
        "DELETE FROM rarity_config WHERE rarity_key NOT IN ({})".format(
            ",".join(f"'{k}'" for k in DEFAULT_RARITIES)
        )
    )
    for key, info in DEFAULT_RARITIES.items():
        await db.execute(
            """INSERT INTO rarity_config (rarity_key, weight, reward) VALUES (?, ?, ?)
               ON CONFLICT(rarity_key) DO UPDATE SET
                 weight=excluded.weight, reward=excluded.reward""",
            (key, info["weight"], info["reward"]),
        )
    await db.commit()


async def set_rarity_params(
    db, key: str, weight: Optional[int] = None, reward: Optional[int] = None
) -> bool:
    if key not in DEFAULT_RARITIES:
        return False
    await ensure_config_tables(db)
    cur = await db.execute(
        "SELECT weight, reward FROM rarity_config WHERE rarity_key = ?", (key,)
    )
    row = await cur.fetchone()
    w = weight if weight is not None else (row[0] if row else DEFAULT_RARITIES[key]["weight"])
    r = reward if reward is not None else (row[1] if row else DEFAULT_RARITIES[key]["reward"])
    if w < 0:
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
