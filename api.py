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

# Публичный URL API (если за reverse-proxy request.base_url неверный)
PUBLIC_API_BASE = (os.getenv("PUBLIC_API_BASE") or os.getenv("API_BASE") or "").rstrip("/")

AVATARS_DIR = Path(os.getenv("AVATARS_DIR", "/app/data/user_avatars"))
DEFAULT_AVATAR_PATH = Path(
    os.getenv("DEFAULT_AVATAR_PATH", "/app/data/card_photos/default_avatar.jpg")
)

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


def public_base(request: Request) -> str:
    if PUBLIC_API_BASE:
        return PUBLIC_API_BASE
    # X-Forwarded-* если есть
    proto = request.headers.get("x-forwarded-proto")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if proto and host:
        return f"{proto}://{host}".rstrip("/")
    return str(request.base_url).rstrip("/")


def card_photo_url(card_id: int, request: Request) -> str:
    return f"{public_base(request)}/api/cards/{card_id}/photo"


def avatar_url(user_id: int, request: Request) -> str:
    return f"{public_base(request)}/api/users/{user_id}/avatar"


def photo_file_exists(photo_path: Optional[str]) -> bool:
    if not photo_path:
        return False
    try:
        p = Path(photo_path)
        return p.is_file() and p.stat().st_size > 200
    except Exception:
        return False


def resolve_card_photo_path(photo_path: Optional[str], photo_uuid: Optional[str] = None) -> Optional[Path]:
    """Найти файл фото на диске (path из БД или по uuid в стандартной папке)."""
    if photo_path:
        p = Path(photo_path)
        if p.is_file() and p.stat().st_size > 200:
            return p
        # иногда path устарел, а файл переименован в .webp
        for ext in (".webp", ".jpg", ".jpeg", ".png"):
            alt = p.with_suffix(ext)
            if alt.is_file() and alt.stat().st_size > 200:
                return alt
    if photo_uuid:
        photo_dir = Path(os.getenv("CARD_PHOTOS_DIR", "/app/data/card_photos"))
        for ext in (".webp", ".jpg", ".jpeg", ".png"):
            cand = photo_dir / f"{photo_uuid}{ext}"
            if cand.is_file() and cand.stat().st_size > 200:
                return cand
    return None


def resolve_avatar_path(user_id: int) -> Optional[Path]:
    p = AVATARS_DIR / f"{user_id}.jpg"
    if p.is_file() and p.stat().st_size > 0:
        return p
    # из таблицы user_avatars
    return None


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
            f"""SELECT id, name, rarity, photo_path, photo_id, photo_uuid
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
        has = resolve_card_photo_path(r["photo_path"], r["photo_uuid"] if "photo_uuid" in r.keys() else None) is not None
        items.append(
            {
                "id": r["id"],
                "name": r["name"],
                "rarity": r["rarity"],
                "rarity_icon": ri["icon"],
                "rarity_name": ri["name"],
                "reward": ri["reward"],
                "owners": owners.get(r["id"], 0),
                "has_photo": has,
                "photo_url": card_photo_url(r["id"], request) if has else None,
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
            "SELECT id, name, rarity, photo_path, photo_id, photo_uuid FROM cards WHERE id = ?",
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
    has = resolve_card_photo_path(
        row["photo_path"], row["photo_uuid"] if "photo_uuid" in row.keys() else None
    ) is not None
    return {
        "id": row["id"],
        "name": row["name"],
        "rarity": row["rarity"],
        "rarity_icon": ri["icon"],
        "rarity_name": ri["name"],
        "reward": ri["reward"],
        "owners": owners,
        "has_photo": has,
        "photo_url": card_photo_url(row["id"], request) if has else None,
    }


@app.get("/api/cards/{card_id}/photo")
async def api_card_photo(card_id: int):
    async with get_db() as db:
        cur = await db.execute(
            "SELECT photo_path, photo_id, photo_uuid FROM cards WHERE id = ?",
            (card_id,),
        )
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "Нет карточки")

    path = resolve_card_photo_path(
        row["photo_path"],
        row["photo_uuid"] if "photo_uuid" in row.keys() else None,
    )
    if path is None:
        raise HTTPException(404, "Фото отсутствует")

    suffix = path.suffix.lower()
    media = {
        ".webp": "image/webp",
        ".png": "image/png",
        ".gif": "image/gif",
    }.get(suffix, "image/jpeg")
    return FileResponse(
        path,
        media_type=media,
        headers={
            "Cache-Control": "public, max-age=604800, immutable",
            "Cross-Origin-Resource-Policy": "cross-origin",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.get("/api/top")
async def api_top(
    request: Request,
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
        uid = r["user_id"]
        has_av = (AVATARS_DIR / f"{uid}.jpg").is_file()
        items.append(
            {
                "rank": i,
                "user_id": uid,
                "name": display_name(r["nickname"], uid),
                "value": int(r["value"] or 0),
                "avatar_url": avatar_url(uid, request) if has_av else None,
                "has_avatar": has_av,
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
        "has_avatar": (AVATARS_DIR / f"{user_id}.jpg").is_file(),
        "avatar_url": avatar_url(user_id, request),
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
            SELECT c.id, c.name, c.rarity, c.photo_path, c.photo_id, c.photo_uuid, i.claim_time
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
        has = resolve_card_photo_path(
            r["photo_path"], r["photo_uuid"] if "photo_uuid" in r.keys() else None
        ) is not None
        items.append(
            {
                "id": r["id"],
                "name": r["name"],
                "rarity": r["rarity"],
                "rarity_icon": ri["icon"],
                "rarity_name": ri["name"],
                "reward": ri["reward"],
                "claim_time": r["claim_time"] or 0,
                "has_photo": has,
                "photo_url": card_photo_url(r["id"], request) if has else None,
            }
        )
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "pages": max(1, (total + limit - 1) // limit) if total else 0,
        "items": items,
    }


@app.get("/api/me/activity")
async def api_me_activity(
    tg: dict = Depends(get_telegram_user),
    range: str = Query("week", pattern="^(week|month|all)$"),
):
    """Получения карточек по дням: week (7), month (30), all (до 90 точек / по дням)."""
    user_id = tg["user_id"]
    now = int(time.time())
    if range == "week":
        days = 7
        since = now - days * 86400
    elif range == "month":
        days = 30
        since = now - days * 86400
    else:
        days = 90
        since = 0

    async with get_db() as db:
        if since:
            cur = await db.execute(
                """
                SELECT date(claim_time, 'unixepoch', 'localtime') AS d, COUNT(*) AS cnt
                FROM inventory
                WHERE user_id = ? AND claim_time >= ?
                GROUP BY d
                ORDER BY d
                """,
                (user_id, since),
            )
        else:
            cur = await db.execute(
                """
                SELECT date(claim_time, 'unixepoch', 'localtime') AS d, COUNT(*) AS cnt
                FROM inventory
                WHERE user_id = ? AND claim_time > 0
                GROUP BY d
                ORDER BY d DESC
                LIMIT 90
                """,
                (user_id,),
            )
        rows = await cur.fetchall()

    by_day = {r["d"]: int(r["cnt"]) for r in rows if r["d"]}

    # заполнить непрерывный ряд для week/month
    from datetime import datetime, timedelta, timezone

    points = []
    if range in ("week", "month"):
        today = datetime.now().date()
        for i in range(days - 1, -1, -1):
            d = today - timedelta(days=i)
            key = d.isoformat()
            points.append({"date": key, "count": by_day.get(key, 0)})
    else:
        # all: последние 90 дней с данными, от старых к новым
        keys = sorted(by_day.keys())[-90:]
        for key in keys:
            points.append({"date": key, "count": by_day[key]})

    total = sum(p["count"] for p in points)
    return {"range": range, "total": total, "points": points}


@app.get("/api/users/{user_id}/avatar")
async def api_user_avatar(user_id: int):
    path = AVATARS_DIR / f"{user_id}.jpg"
    if path.is_file() and path.stat().st_size > 0:
        return FileResponse(
            path,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "public, max-age=3600",
                "Cross-Origin-Resource-Policy": "cross-origin",
                "Access-Control-Allow-Origin": "*",
            },
        )
    # fallback default
    if DEFAULT_AVATAR_PATH.is_file():
        return FileResponse(
            DEFAULT_AVATAR_PATH,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "public, max-age=86400",
                "Cross-Origin-Resource-Policy": "cross-origin",
                "Access-Control-Allow-Origin": "*",
            },
        )
    # 1x1 transparent PNG
    import base64

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    return Response(
        content=png,
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=3600",
            "Cross-Origin-Resource-Policy": "cross-origin",
            "Access-Control-Allow-Origin": "*",
        },
    )


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

    has_av = (AVATARS_DIR / f"{user_id}.jpg").is_file()
    return {
        "user_id": user_id,
        "name": display_name(user["nickname"], user_id),
        "coins": coins,
        "streak": streak,
        "cards_count": cards_count,
        "total_cards": total_cards,
        "rank_coins": rank_coins,
        "rarities": rarity_breakdown,
        "has_avatar": has_av,
        "avatar_url": avatar_url(user_id, request),
    }


@app.get("/api/users/{user_id}/collection")
async def api_user_collection(
    user_id: int,
    request: Request,
    rarity: Optional[str] = Query(None),
    page: int = Query(0, ge=0),
    limit: int = Query(24, ge=1, le=100),
):
    async with get_db() as db:
        cur = await db.execute(
            "SELECT role FROM users WHERE user_id = ?", (user_id,)
        )
        u = await cur.fetchone()
        if not u or (u["role"] or "") == "banned":
            raise HTTPException(404, "Пользователь не найден")
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
            SELECT c.id, c.name, c.rarity, c.photo_path, c.photo_id, c.photo_uuid, i.claim_time
            FROM inventory i JOIN cards c ON c.id = i.card_id
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
        has = resolve_card_photo_path(
            r["photo_path"], r["photo_uuid"] if "photo_uuid" in r.keys() else None
        ) is not None
        items.append(
            {
                "id": r["id"],
                "name": r["name"],
                "rarity": r["rarity"],
                "rarity_icon": ri["icon"],
                "rarity_name": ri["name"],
                "reward": ri["reward"],
                "claim_time": r["claim_time"] or 0,
                "has_photo": has,
                "photo_url": card_photo_url(r["id"], request) if has else None,
            }
        )
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "pages": max(1, (total + limit - 1) // limit) if total else 0,
        "items": items,
    }


@app.get("/")
async def root():
    return {
        "service": "cards-game-api",
        "docs": "/docs",
        "health": "/health",
    }
