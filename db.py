"""
Слой базы данных. Единый доступ ко всем таблицам.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

import aiosqlite

from config import DB_PATH, SUPERADMIN_IDS, DEFAULT_RARITIES, ROLES
from utils import now_ts

logger = logging.getLogger("db")


@asynccontextmanager
async def get_db() -> AsyncIterator[aiosqlite.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON;")
    await db.execute("PRAGMA journal_mode = WAL;")
    await db.execute("PRAGMA busy_timeout = 5000;")
    try:
        yield db
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.error("DB error: %s", e)
        raise
    finally:
        await db.close()


async def init_db() -> None:
    """Создаёт все таблицы и индексы."""
    async with get_db() as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS cards (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                rarity      TEXT NOT NULL,
                photo_id    TEXT,
                photo_path  TEXT,
                photo_uuid  TEXT,
                created_at  INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id          INTEGER PRIMARY KEY,
                nickname         TEXT,
                role             TEXT DEFAULT 'user',
                coins            INTEGER DEFAULT 0,
                last_claim       INTEGER DEFAULT 0,
                last_dice        INTEGER DEFAULT 0,
                streak           INTEGER DEFAULT 0,
                last_streak_date INTEGER DEFAULT 0,
                registration     INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS inventory (
                user_id    INTEGER NOT NULL,
                card_id    INTEGER NOT NULL,
                claim_time INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, card_id),
                FOREIGN KEY (card_id) REFERENCES cards(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS rarity_config (
                rarity_key TEXT PRIMARY KEY,
                weight     INTEGER NOT NULL,
                reward     INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bot_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_cards_rarity ON cards(rarity);
            CREATE INDEX IF NOT EXISTS idx_inventory_user ON inventory(user_id);
            CREATE INDEX IF NOT EXISTS idx_users_coins ON users(coins);
            CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
        """)

        # Дефолтные редкости
        for key, info in DEFAULT_RARITIES.items():
            await db.execute(
                """INSERT OR IGNORE INTO rarity_config (rarity_key, weight, reward)
                   VALUES (?, ?, ?)""",
                (key, info["weight"], info["reward"]),
            )

        await db.execute(
            "INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('user_bot_enabled', '1')"
        )

        # Суперадмины из env
        if SUPERADMIN_IDS:
            cur = await db.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'superadmin'"
            )
            if (await cur.fetchone())[0] == 0:
                for sid in SUPERADMIN_IDS:
                    await db.execute(
                        """INSERT INTO users (user_id, role, registration)
                           VALUES (?, 'superadmin', ?)
                           ON CONFLICT(user_id) DO UPDATE SET role = 'superadmin'""",
                        (sid, now_ts()),
                    )
                    logger.info("Superadmin from env: %s", sid)

    logger.info("Database initialized: %s", DB_PATH)


# ── Users ───────────────────────────────────────────────────────────────────

async def get_or_create_user(user_id: int) -> aiosqlite.Row:
    async with get_db() as db:
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        if row:
            return row
        role = "superadmin" if user_id in SUPERADMIN_IDS else "user"
        await db.execute(
            """INSERT INTO users (user_id, role, registration)
               VALUES (?, ?, ?)""",
            (user_id, role, now_ts()),
        )
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return await cur.fetchone()


async def get_user(user_id: int) -> Optional[aiosqlite.Row]:
    async with get_db() as db:
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return await cur.fetchone()


async def get_user_role(user_id: int) -> str:
    user = await get_user(user_id)
    if not user:
        return "user"
    role = user["role"] or "user"
    return role if role in ROLES else "user"


async def is_admin(user_id: int) -> bool:
    return (await get_user_role(user_id)) in ("admin", "superadmin")


async def is_superadmin(user_id: int) -> bool:
    return (await get_user_role(user_id)) == "superadmin"


async def is_banned(user_id: int) -> bool:
    return (await get_user_role(user_id)) == "banned"


async def set_user_role(user_id: int, role: str) -> bool:
    if role not in ROLES:
        return False
    async with get_db() as db:
        cur = await db.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
        if not await cur.fetchone():
            return False
        await db.execute("UPDATE users SET role = ? WHERE user_id = ?", (role, user_id))
    return True


async def update_coins(user_id: int, delta: int) -> int:
    """Меняет баланс, не даёт уйти ниже 0. Возвращает новый баланс."""
    async with get_db() as db:
        cur = await db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        old = (row["coins"] or 0) if row else 0
        new = max(0, old + delta)
        await db.execute("UPDATE users SET coins = ? WHERE user_id = ?", (new, user_id))
        return new


async def set_coins(user_id: int, value: int) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE users SET coins = ? WHERE user_id = ?", (max(0, value), user_id)
        )


async def set_nickname(user_id: int, nick: Optional[str]) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE users SET nickname = ? WHERE user_id = ?", (nick, user_id)
        )


# ── Cards ───────────────────────────────────────────────────────────────────

async def add_card(
    name: str,
    rarity: str,
    photo_id: Optional[str] = None,
    photo_path: Optional[str] = None,
    photo_uuid: Optional[str] = None,
) -> int:
    async with get_db() as db:
        cur = await db.execute(
            """INSERT INTO cards (name, rarity, photo_id, photo_path, photo_uuid, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name, rarity, photo_id, photo_path, photo_uuid, now_ts()),
        )
        return cur.lastrowid


async def get_card(card_id: int) -> Optional[aiosqlite.Row]:
    async with get_db() as db:
        cur = await db.execute("SELECT * FROM cards WHERE id = ?", (card_id,))
        return await cur.fetchone()


async def update_card(card_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [card_id]
    async with get_db() as db:
        await db.execute(f"UPDATE cards SET {cols} WHERE id = ?", vals)


async def delete_card(card_id: int) -> None:
    async with get_db() as db:
        await db.execute("DELETE FROM inventory WHERE card_id = ?", (card_id,))
        await db.execute("DELETE FROM cards WHERE id = ?", (card_id,))


async def count_cards(rarity: Optional[str] = None) -> int:
    async with get_db() as db:
        if rarity:
            cur = await db.execute(
                "SELECT COUNT(*) FROM cards WHERE rarity = ?", (rarity,)
            )
        else:
            cur = await db.execute("SELECT COUNT(*) FROM cards")
        return (await cur.fetchone())[0]


async def list_cards(
    rarity: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> list[aiosqlite.Row]:
    async with get_db() as db:
        if rarity:
            cur = await db.execute(
                "SELECT * FROM cards WHERE rarity = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                (rarity, limit, offset),
            )
        else:
            cur = await db.execute(
                "SELECT * FROM cards ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        return await cur.fetchall()


async def get_rarity_counts() -> dict[str, int]:
    async with get_db() as db:
        cur = await db.execute("SELECT rarity, COUNT(*) FROM cards GROUP BY rarity")
        return dict(await cur.fetchall())


# ── Inventory ───────────────────────────────────────────────────────────────

async def user_owns_card(user_id: int, card_id: int) -> bool:
    async with get_db() as db:
        cur = await db.execute(
            "SELECT 1 FROM inventory WHERE user_id = ? AND card_id = ?",
            (user_id, card_id),
        )
        return await cur.fetchone() is not None


async def add_to_inventory(user_id: int, card_id: int) -> None:
    async with get_db() as db:
        await db.execute(
            """INSERT INTO inventory (user_id, card_id, claim_time)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id, card_id) DO UPDATE SET claim_time = excluded.claim_time""",
            (user_id, card_id, now_ts()),
        )


async def count_user_cards(user_id: int) -> int:
    async with get_db() as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM inventory WHERE user_id = ?", (user_id,)
        )
        return (await cur.fetchone())[0]


async def get_user_cards_by_rarity(user_id: int, rarity: str) -> list[aiosqlite.Row]:
    async with get_db() as db:
        cur = await db.execute(
            """SELECT c.*, i.claim_time
               FROM inventory i
               JOIN cards c ON c.id = i.card_id
               WHERE i.user_id = ? AND c.rarity = ?
               ORDER BY i.claim_time DESC""",
            (user_id, rarity),
        )
        return await cur.fetchall()


async def get_user_rarity_stats(user_id: int) -> dict[str, int]:
    async with get_db() as db:
        cur = await db.execute(
            """SELECT c.rarity, COUNT(*)
               FROM inventory i
               JOIN cards c ON c.id = i.card_id
               WHERE i.user_id = ?
               GROUP BY c.rarity""",
            (user_id,),
        )
        return dict(await cur.fetchall())


# ── Rarity config ───────────────────────────────────────────────────────────

async def load_rarities() -> dict[str, dict[str, Any]]:
    async with get_db() as db:
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
            "color": info["color"],
            "stars": info["stars"],
        }
    return out


async def set_rarity_param(key: str, field: str, value: int) -> bool:
    if key not in DEFAULT_RARITIES or field not in ("weight", "reward"):
        return False
    async with get_db() as db:
        if field == "weight":
            await db.execute(
                "UPDATE rarity_config SET weight = ? WHERE rarity_key = ?",
                (value, key),
            )
        else:
            await db.execute(
                "UPDATE rarity_config SET reward = ? WHERE rarity_key = ?",
                (value, key),
            )
    return True


async def reset_rarities() -> None:
    async with get_db() as db:
        for key, info in DEFAULT_RARITIES.items():
            await db.execute(
                """INSERT INTO rarity_config (rarity_key, weight, reward)
                   VALUES (?, ?, ?)
                   ON CONFLICT(rarity_key) DO UPDATE SET
                     weight = excluded.weight, reward = excluded.reward""",
                (key, info["weight"], info["reward"]),
            )


# ── Settings ────────────────────────────────────────────────────────────────

async def get_setting(key: str, default: str = "") -> str:
    async with get_db() as db:
        cur = await db.execute(
            "SELECT value FROM bot_settings WHERE key = ?", (key,)
        )
        row = await cur.fetchone()
        return row[0] if row else default


async def set_setting(key: str, value: str) -> None:
    async with get_db() as db:
        await db.execute(
            """INSERT INTO bot_settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value),
        )


async def is_user_bot_enabled() -> bool:
    return (await get_setting("user_bot_enabled", "1")) in ("1", "true", "yes", "on")
