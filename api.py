"""
FastAPI backend для Telegram Mini App.

Запуск вместе с ботом (main.py / user_bot.py):
  START_API=1  или  ENABLE_WEBAPP_API=1
  API_PORT=8080  (BotHost пробрасывает публичный URL)

Эндпоинты:
  GET  /health
  GET  /api/rarities
  GET  /api/cards?rarity=&page=&limit=&q=
  GET  /api/cards/{id}
  GET  /api/cards/{id}/photo
  GET  /api/top?kind=coins|cards|streak&limit=
  GET  /api/stats
  GET  /api/me                 (Telegram WebApp initData)
  GET  /api/me/collection?rarity=&page=&limit=
  GET  /api/users/{user_id}   (публичный профиль)
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qsl

import aiosqlite
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("api")

DB_NAME = os.getenv("DB_NAME", "/app/data/cards_game.db")
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
ADMIN_BOT_TOKEN = (
    os.getenv("ADMIN_BOT_TOKEN") or os.getenv("BOT_TOKEN_ADMIN") or ""
).strip()
# Токен, которым валидируем initData (user-бот)
AUTH_BOT_TOKEN = BOT_TOKEN or ADMIN_BOT_TOKEN

CORS_ORIGINS = [
    o.strip()
    for o in (os.getenv("CORS_ORIGINS") or "*").split(",")
    if o.strip()
]
# Vercel preview + production часто через *
if "*" not in CORS_ORIGINS:
    CORS_ORIGINS.extend(
        [
            "https://web.telegram.org",
            "https://telegram.org",
        ]
    )

try:
    from game_config import (
        DEFAULT_RARITIES,
        RARITY_ORDER,
        load_rarities,
        ensure_config_tables,
        COOLDOWN_SECONDS,
        NICKNAME_COST,
        DICE_MIN_BALANCE,
        STREAK_BONUSES,
    )
except ImportError:
    DEFAULT_RARITIES = {
        "common": {"icon": "⚪️", "name": "Обычная", "weight": 45, "reward": 15},
        "uncommon": {"icon": "🟢", "name": "Необычная", "weight": 25, "reward": 40},
        "valuable": {"icon": "🟣", "name": "Ценная", "weight": 14, "reward": 80},
        "super_valuable": {"icon": "🟠", "name": "Сверхценная", "weight": 7, "reward": 150},
        "legendary": {"icon": "🟡", "name": "Легендарная", "weight": 2, "reward": 300},
        "negative": {"icon": "⚫️", "name": "Негативная", "weight": 7, "reward": -15},
    }
    RARITY_ORDER = list(DEFAULT_RARITIES.keys())
    COOLDOWN_SECONDS = 3 * 3600
    NICKNAME_COST = 150
    DICE_MIN_BALANCE = 30
    STREAK_BONUSES = [(2, 10), (7, 20), (14, 30), (30, 45), (60, 60)]

    async def ensure_config_tables(db):
        pass

    async def load_rarities(db):
        return {k: dict(v) for k, v in DEFAULT_RARITIES.items()}


STREAK_EXPIRE_SECONDS = 24 * 3600


# ── DB ──────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def get_db():
    db = await aiosqlite.connect(DB_NAME)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON;")
    await db.execute("PRAGMA journal_mode = WAL;")
    await db.execute("PRAGMA busy_timeout = 5000;")
    try:
        yield db
    finally:
        await db.close()


async def row_to_dict(row: aiosqlite.Row | None) -> Optional[dict]:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


# ── Telegram WebApp auth ────────────────────────────────────────────────────
def _validate_init_data(init_data: str, bot_token: str, max_age: int = 86400) -> dict:
    """
    Проверка подписи Telegram.WebApp.initData.
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data or not bot_token:
        raise HTTPException(status_code=401, detail="Нет initData или токена бота")

    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Нет hash в initData")

    # data_check_string
    check_list = [f"{k}={v}" for k, v in sorted(parsed.items())]
    data_check_string = "\n".join(check_list)

    secret_key = hmac.new(
        b"WebAppData", bot_token.encode(), hashlib.sha256
    ).digest()
    calculated = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(calculated, received_hash):
        raise HTTPException(status_code=401, detail="Неверная подпись initData")

    auth_date = int(parsed.get("auth_date") or 0)
    if auth_date and (time.time() - auth_date) > max_age:
        raise HTTPException(status_code=401, detail="initData устарел")

    user_raw = parsed.get("user")
    user: dict[str, Any] = {}
    if user_raw:
        import json

        try:
            user = json.loads(user_raw)
        except Exception:
            raise HTTPException(status_code=401, detail="Битый user в initData")

    return {
        "user": user,
        "user_id": int(user.get("id") or 0),
        "auth_date": auth_date,
        "query_id": parsed.get("query_id"),
        "start_param": parsed.get("start_param"),
    }


async def get_telegram_user(
    x_telegram_init_data: Optional[str] = Header(None, alias="X-Telegram-Init-Data"),
    authorization: Optional[str] = Header(None),
) -> dict:
    """Зависимость: пользователь из заголовка X-Telegram-Init-Data."""
    init_data = x_telegram_init_data
    if not init_data and authorization and authorization.lower().startswith("tma "):
        init_data = authorization[4:].strip()
    if not init_data:
        raise HTTPException(
            status_code=401,
            detail="Требуется заголовок X-Telegram-Init-Data",
        )
    data = _validate_init_data(init_data, AUTH_BOT_TOKEN)
    if not data["user_id"]:
        raise HTTPException(status_code=401, detail="Нет user id")
    return data


# ── App ─────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("API start · DB=%s", DB_NAME)
    try:
        async with get_db() as db:
            await ensure_config_tables(db)
    except Exception as e:
        logger.warning("ensure_config_tables: %s", e)
    yield
    logger.info("API stop")


app = FastAPI(
    title="Cards Game Mini App API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS if "*" not in CORS_ORIGINS else ["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
)


# ── Helpers ─────────────────────────────────────────────────────────────────
def display_name(nickname, user_id, fallback: Optional[str] = None) -> str:
    if nickname and str(nickname).strip():
        return str(nickname).strip()
    if fallback and str(fallback).strip():
        return str(fallback).strip()[:32]
    return str(user_id)


def rarity_info(key: str, rarities: dict) -> dict:
    base = rarities.get(key) or DEFAULT_RARITIES.get(key) or {
        "icon": "•",
        "name": key,
        "weight": 0,
        "reward": 0,
    }
    return {
        "key": key,
        "icon": base.get("icon", "•"),
        "name": base.get("name", key),
        "weight": int(base.get("weight", 0)),
        "reward": int(base.get("reward", 0)),
    }


def card_photo_url(card_id: int, request: Request) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/api/cards/{card_id}/photo"


async def effective_streak(db, user_id: int) -> int:
    cur = await db.execute(
        "SELECT streak, last_claim FROM users WHERE user_id = ?", (user_id,)
    )
    row = await cur.fetchone()
    if not row:
        return 0
    streak = row["streak"] or 0
    last_claim = row["last_claim"] or 0
    if streak > 0 and last_claim > 0 and (time.time() - last_claim) >= STREAK_EXPIRE_SECONDS:
        return 0
    return streak


# ── Routes ──────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    try:
        async with get_db() as db:
            await db.execute("SELECT 1")
        return {"ok": True, "db": True, "ts": int(time.time())}
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={"ok": False, "db": False, "error": str(e)},
        )


@app.get("/api/rarities")
async def api_rarities():
    async with get_db() as db:
        rarities = await load_rarities(db)
        cur = await db.execute("SELECT rarity, COUNT(*) AS cnt FROM cards GROUP BY rarity")
        counts = {r["rarity"]: r["cnt"] for r in await cur.fetchall()}
        cur = await db.execute("SELECT COUNT(*) FROM cards")
        total = (await cur.fetchone())[0]

    items = []
    for key in RARITY_ORDER:
        if key not in rarities:
            continue
        info = rarity_info(key, rarities)
        info["count"] = int(counts.get(key, 0))
        items.append(info)
    return {"total": total, "items": items}


@app.get("/api/cards")
async def api_cards(
    request: Request,
    rarity: Optional[str] = Query(None),
    page: int = Query(0, ge=0),
    limit: int = Query(24, ge=1, le=100),
    q: Optional[str] = Query(None, max_length=64),
):
    async with get_db() as db:
        rarities = await load_rarities(db)
        where = []
        params: list[Any] = []
        if rarity and rarity != "all":
            where.append("rarity = ?")
            params.append(rarity)
        if q:
            where.append("name LIKE ?")
            params.append(f"%{q.strip()}%")
        wh = (" WHERE " + " AND ".join(where)) if where else ""

        cur = await db.execute(f"SELECT COUNT(*) FROM cards{wh}", params)
        total = (await cur.fetchone())[0]
        offset = page * limit
        cur = await db.execute(
            f"""SELECT id, name, rarity, photo_path, photo_id
                FROM cards{wh}
                ORDER BY id DESC LIMIT ? OFFSET ?""",
            params + [limit, offset],
        )
        rows = await cur.fetchall()

        # owners count batch
        ids = [r["id"] for r in rows]
        owners: dict[int, int] = {}
        if ids:
            placeholders = ",".join("?" * len(ids))
            cur = await db.execute(
                f"""SELECT card_id, COUNT(*) AS cnt FROM inventory
                    WHERE card_id IN ({placeholders}) GROUP BY card_id""",
                ids,
            )
            for r in await cur.fetchall():
                owners[r["card_id"]] = r["cnt"]

    items = []
    for r in rows:
        ri = rarity_info(r["rarity"], rarities)
        items.append(
            {
                "id": r["id"],
                "name": r["name"],
                "rarity": r["rarity"],
                "rarity_icon": ri["icon"],
                "rarity_name": ri["name"],
                "reward": ri["reward"],
                "owners": owners.get(r["id"], 0),
                "has_photo": bool(r["photo_path"] or r["photo_id"]),
                "photo_url": card_photo_url(r["id"], request),
            }
        )
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "pages": max(1, (total + limit - 1) // limit) if total else 0,
        "items": items,
    }


@app.get("/api/cards/{card_id}")
async def api_card_one(card_id: int, request: Request):
    async with get_db() as db:
        rarities = await load_rarities(db)
        cur = await db.execute(
            "SELECT id, name, rarity, photo_path, photo_id FROM cards WHERE id = ?",
            (card_id,),
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "Карточка не найдена")
        cur = await db.execute(
            "SELECT COUNT(*) FROM inventory WHERE card_id = ?", (card_id,)
        )
        owners = (await cur.fetchone())[0]
    ri = rarity_info(row["rarity"], rarities)
    return {
        "id": row["id"],
        "name": row["name"],
        "rarity": row["rarity"],
        "rarity_icon": ri["icon"],
        "rarity_name": ri["name"],
        "reward": ri["reward"],
        "owners": owners,
        "has_photo": bool(row["photo_path"] or row["photo_id"]),
        "photo_url": card_photo_url(row["id"], request),
    }


@app.get("/api/cards/{card_id}/photo")
async def api_card_photo(card_id: int):
    async with get_db() as db:
        cur = await db.execute(
            "SELECT photo_path, photo_id FROM cards WHERE id = ?", (card_id,)
        )
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "Нет карточки")

    path = row["photo_path"]
    if path and Path(path).is_file() and Path(path).stat().st_size > 0:
        media = "image/webp" if path.lower().endswith(".webp") else "image/jpeg"
        return FileResponse(
            path,
            media_type=media,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    # placeholder 1×1 dark pixel if no file (frontend shows rarity frame)
    raise HTTPException(404, "Фото отсутствует")


@app.get("/api/top")
async def api_top(
    kind: str = Query("coins", pattern="^(coins|cards|streak)$"),
    limit: int = Query(20, ge=1, le=50),
):
    now_ts = int(time.time())
    async with get_db() as db:
        if kind == "cards":
            cur = await db.execute(
                """
                SELECT u.user_id, u.nickname,
                       (SELECT COUNT(*) FROM inventory i WHERE i.user_id = u.user_id) AS value
                FROM users u
                WHERE u.role != 'banned'
                ORDER BY value DESC, u.user_id ASC
                LIMIT ?
                """,
                (limit,),
            )
        elif kind == "streak":
            cur = await db.execute(
                """
                SELECT user_id, nickname,
                       CASE
                         WHEN last_claim > 0 AND (? - last_claim) >= ?
                         THEN 0 ELSE COALESCE(streak, 0)
                       END AS value
                FROM users
                WHERE role != 'banned'
                ORDER BY value DESC, user_id ASC
                LIMIT ?
                """,
                (now_ts, STREAK_EXPIRE_SECONDS, limit),
            )
        else:
            cur = await db.execute(
                """
                SELECT user_id, nickname, coins AS value
                FROM users
                WHERE role != 'banned'
                ORDER BY value DESC, user_id ASC
                LIMIT ?
                """,
                (limit,),
            )
        rows = await cur.fetchall()

    items = []
    for i, r in enumerate(rows, 1):
        items.append(
            {
                "rank": i,
                "user_id": r["user_id"],
                "name": display_name(r["nickname"], r["user_id"]),
                "value": int(r["value"] or 0),
            }
        )
    return {"kind": kind, "items": items}


@app.get("/api/stats")
async def api_stats():
    async with get_db() as db:
        rarities = await load_rarities(db)
        cur = await db.execute("SELECT COUNT(*) FROM users")
        total_users = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM users WHERE role = 'banned'")
        banned = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM cards")
        total_cards = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COALESCE(SUM(coins),0) FROM users")
        total_coins = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM inventory")
        owned = (await cur.fetchone())[0]
        cur = await db.execute("SELECT rarity, COUNT(*) FROM cards GROUP BY rarity")
        by_r = {r[0]: r[1] for r in await cur.fetchall()}

    rarity_stats = []
    for key in RARITY_ORDER:
        if key not in rarities:
            continue
        ri = rarity_info(key, rarities)
        rarity_stats.append({**ri, "count": int(by_r.get(key, 0))})

    return {
        "users": total_users,
        "banned": banned,
        "cards": total_cards,
        "coins_in_game": int(total_coins),
        "cards_in_collections": owned,
        "rarities": rarity_stats,
        "cooldown_seconds": COOLDOWN_SECONDS,
        "nickname_cost": NICKNAME_COST,
        "dice_min_balance": DICE_MIN_BALANCE,
    }


@app.get("/api/me")
async def api_me(request: Request, tg: dict = Depends(get_telegram_user)):
    user_id = tg["user_id"]
    tg_user = tg.get("user") or {}
    async with get_db() as db:
        rarities = await load_rarities(db)
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = await cur.fetchone()
        if not user:
            # авто-регистрация лёгкая (как в боте)
            now = int(time.time())
            await db.execute(
                "INSERT OR IGNORE INTO users (user_id, registration, role) VALUES (?, ?, 'user')",
                (user_id, now),
            )
            await db.commit()
            cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            user = await cur.fetchone()

        cur = await db.execute(
            "SELECT COUNT(*) FROM inventory WHERE user_id = ?", (user_id,)
        )
        cards_count = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM cards")
        total_cards = (await cur.fetchone())[0]
        cur = await db.execute(
            """
            SELECT c.rarity, COUNT(*) AS cnt
            FROM inventory i JOIN cards c ON c.id = i.card_id
            WHERE i.user_id = ?
            GROUP BY c.rarity
            """,
            (user_id,),
        )
        by_rarity = {r["rarity"]: r["cnt"] for r in await cur.fetchall()}

        streak = await effective_streak(db, user_id)
        last_claim = user["last_claim"] or 0
        now = int(time.time())
        cd_left = max(0, COOLDOWN_SECONDS - (now - last_claim)) if last_claim else 0

        # rank by coins
        coins = user["coins"] or 0
        cur = await db.execute(
            "SELECT COUNT(*) + 1 FROM users WHERE coins > ? AND role != 'banned'",
            (coins,),
        )
        rank_coins = (await cur.fetchone())[0]

    rarity_breakdown = []
    for key in RARITY_ORDER:
        if key not in rarities:
            continue
        ri = rarity_info(key, rarities)
        rarity_breakdown.append({**ri, "owned": int(by_rarity.get(key, 0))})

    name = display_name(
        user["nickname"],
        user_id,
        tg_user.get("first_name") or tg_user.get("username"),
    )
    return {
        "user_id": user_id,
        "name": name,
        "nickname": user["nickname"],
        "role": user["role"] or "user",
        "coins": coins,
        "streak": streak,
        "cards_count": cards_count,
        "total_cards": total_cards,
        "rank_coins": rank_coins,
        "cooldown_left": cd_left,
        "registration": user["registration"] or 0,
        "rarities": rarity_breakdown,
        "photo_url": tg_user.get("photo_url"),
        "username": tg_user.get("username"),
    }


@app.get("/api/me/collection")
async def api_me_collection(
    request: Request,
    tg: dict = Depends(get_telegram_user),
    rarity: Optional[str] = Query(None),
    page: int = Query(0, ge=0),
    limit: int = Query(24, ge=1, le=100),
):
    user_id = tg["user_id"]
    async with get_db() as db:
        rarities = await load_rarities(db)
        where = ["i.user_id = ?"]
        params: list[Any] = [user_id]
        if rarity and rarity != "all":
            where.append("c.rarity = ?")
            params.append(rarity)
        wh = " AND ".join(where)

        cur = await db.execute(
            f"""SELECT COUNT(*) FROM inventory i
                JOIN cards c ON c.id = i.card_id WHERE {wh}""",
            params,
        )
        total = (await cur.fetchone())[0]
        offset = page * limit
        cur = await db.execute(
            f"""
            SELECT c.id, c.name, c.rarity, c.photo_path, c.photo_id, i.claim_time
            FROM inventory i
            JOIN cards c ON c.id = i.card_id
            WHERE {wh}
            ORDER BY i.claim_time DESC
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        )
        rows = await cur.fetchall()

    items = []
    for r in rows:
        ri = rarity_info(r["rarity"], rarities)
        items.append(
            {
                "id": r["id"],
                "name": r["name"],
                "rarity": r["rarity"],
                "rarity_icon": ri["icon"],
                "rarity_name": ri["name"],
                "reward": ri["reward"],
                "claim_time": r["claim_time"] or 0,
                "has_photo": bool(r["photo_path"] or r["photo_id"]),
                "photo_url": card_photo_url(r["id"], request),
            }
        )
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "pages": max(1, (total + limit - 1) // limit) if total else 0,
        "items": items,
    }


@app.get("/api/users/{user_id}")
async def api_user_public(user_id: int, request: Request):
    async with get_db() as db:
        rarities = await load_rarities(db)
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = await cur.fetchone()
        if not user or (user["role"] or "") == "banned":
            raise HTTPException(404, "Пользователь не найден")
        cur = await db.execute(
            "SELECT COUNT(*) FROM inventory WHERE user_id = ?", (user_id,)
        )
        cards_count = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM cards")
        total_cards = (await cur.fetchone())[0]
        streak = await effective_streak(db, user_id)
        coins = user["coins"] or 0
        cur = await db.execute(
            "SELECT COUNT(*) + 1 FROM users WHERE coins > ? AND role != 'banned'",
            (coins,),
        )
        rank_coins = (await cur.fetchone())[0]
        cur = await db.execute(
            """
            SELECT c.rarity, COUNT(*) AS cnt
            FROM inventory i JOIN cards c ON c.id = i.card_id
            WHERE i.user_id = ?
            GROUP BY c.rarity
            """,
            (user_id,),
        )
        by_rarity = {r["rarity"]: r["cnt"] for r in await cur.fetchall()}

    rarity_breakdown = []
    for key in RARITY_ORDER:
        if key not in rarities:
            continue
        ri = rarity_info(key, rarities)
        rarity_breakdown.append({**ri, "owned": int(by_rarity.get(key, 0))})

    return {
        "user_id": user_id,
        "name": display_name(user["nickname"], user_id),
        "coins": coins,
        "streak": streak,
        "cards_count": cards_count,
        "total_cards": total_cards,
        "rank_coins": rank_coins,
        "rarities": rarity_breakdown,
    }


@app.get("/")
async def root():
    return {
        "service": "cards-game-api",
        "docs": "/docs",
        "health": "/health",
    }
