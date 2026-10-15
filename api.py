"""
API мини-приложения «Мряу».

Эндпоинты для Telegram WebApp: профиль, коллекция, топ, публичный профиль.
Авторизация через initData (HMAC-SHA256).
Общая БД с user-ботом.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Optional
from urllib.parse import parse_qsl, unquote

import aiosqlite
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_NAME = os.getenv("DB_NAME", "/app/data/cards_game.db")
CARD_PHOTOS_DIR = os.getenv("CARD_PHOTOS_DIR", "/app/data/card_photos")
INIT_DATA_MAX_AGE = int(os.getenv("INIT_DATA_MAX_AGE", "86400"))  # 24h


def _resolve_static_dir() -> str:
    """Ищем папку webapp: env → рядом с api.py → cwd → /app/webapp."""
    candidates = []
    env = (os.getenv("WEBAPP_STATIC") or "").strip()
    if env:
        candidates.append(env)
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.extend(
        [
            os.path.join(here, "webapp"),
            os.path.join(here, "..", "webapp"),
            os.path.join(os.getcwd(), "webapp"),
            "/app/webapp",
            "/app/data/webapp",
        ]
    )
    for c in candidates:
        path = os.path.abspath(c)
        index = os.path.join(path, "index.html")
        if os.path.isfile(index):
            logger.info("WEBAPP_STATIC resolved: %s", path)
            return path
    # fallback — даже если index нет, вернём первый env/here
    fallback = os.path.abspath(candidates[0] if candidates else os.path.join(here, "webapp"))
    logger.warning("webapp/index.html not found, STATIC_DIR=%s (candidates tried)", fallback)
    return fallback


STATIC_DIR = _resolve_static_dir()

# ── rarities (синхрон с game_config / bot) ──────────────────────────────────
DEFAULT_RARITIES: dict[str, dict] = {
    "common": {"name": "Обычная", "icon": "⬜", "weight": 50, "reward": 5},
    "uncommon": {"name": "Необычная", "icon": "🟩", "weight": 25, "reward": 12},
    "valuable": {"name": "Ценная", "icon": "🟪", "weight": 12, "reward": 25},
    "super_valuable": {"name": "Сверхценная", "icon": "🟧", "weight": 7, "reward": 50},
    "legendary": {"name": "Легендарная", "icon": "🟨", "weight": 4, "reward": 100},
    "negative": {"name": "Негативная", "icon": "💀", "weight": 2, "reward": -20},
}

RARITY_ORDER = list(DEFAULT_RARITIES.keys())
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
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def load_rarities_from_db() -> dict:
    try:
        async with get_db() as db:
            cur = await db.execute(
                "SELECT rarity_key, name, icon, weight, reward FROM rarities"
            )
            rows = await cur.fetchall()
            if not rows:
                return {k: dict(v) for k, v in DEFAULT_RARITIES.items()}
            out = {}
            for r in rows:
                out[r["rarity_key"]] = {
                    "name": r["name"],
                    "icon": r["icon"] or "•",
                    "weight": r["weight"] or 1,
                    "reward": r["reward"] or 0,
                }
            return out if out else {k: dict(v) for k, v in DEFAULT_RARITIES.items()}
    except Exception:
        return {k: dict(v) for k, v in DEFAULT_RARITIES.items()}


# ── Telegram initData validation ────────────────────────────────────────────
def _parse_init_data(init_data: str) -> dict[str, str]:
    return dict(parse_qsl(init_data, keep_blank_values=True))


def validate_init_data(init_data: str, bot_token: str) -> dict[str, Any]:
    """Проверка подписи Telegram WebApp initData. Возвращает user dict."""
    if not init_data or not bot_token:
        raise HTTPException(status_code=401, detail="Missing initData or token")

    parsed = _parse_init_data(init_data)
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="No hash in initData")

    # auth_date freshness
    auth_date = int(parsed.get("auth_date", "0") or 0)
    if auth_date and (time.time() - auth_date) > INIT_DATA_MAX_AGE:
        raise HTTPException(status_code=401, detail="initData expired")

    data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode(), hashlib.sha256
    ).digest()
    calculated = hmac.new(
        secret_key, data_check.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(calculated, received_hash):
        raise HTTPException(status_code=401, detail="Invalid initData signature")

    user_raw = parsed.get("user")
    if not user_raw:
        raise HTTPException(status_code=401, detail="No user in initData")

    try:
        user = json.loads(unquote(user_raw) if "%" in user_raw else user_raw)
    except json.JSONDecodeError:
        try:
            user = json.loads(user_raw)
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=401, detail=f"Bad user JSON: {e}")

    if not user.get("id"):
        raise HTTPException(status_code=401, detail="No user id")
    return user


async def get_auth_user(authorization: Optional[str] = None, x_init_data: Optional[str] = None) -> dict:
    """Из заголовка Authorization: tma <initData> или X-Telegram-Init-Data."""
    init_data = None
    if authorization and authorization.startswith("tma "):
        init_data = authorization[4:].strip()
    elif x_init_data:
        init_data = x_init_data.strip()
    if not init_data:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return validate_init_data(init_data, BOT_TOKEN)


# ── helpers ─────────────────────────────────────────────────────────────────
def display_name(nickname: Optional[str], user_id: int, fallback: Optional[str] = None) -> str:
    if nickname and str(nickname).strip():
        return str(nickname).strip()
    if fallback and str(fallback).strip():
        return str(fallback).strip()[:32]
    return str(user_id)


def fmt_num(n: Any) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    return f"{n:,}".replace(",", "\u00a0")


async def effective_streak(db, user_id: int) -> int:
    now_ts = int(time.time())
    cur = await db.execute(
        "SELECT streak, last_claim FROM users WHERE user_id = ?", (user_id,)
    )
    row = await cur.fetchone()
    if not row:
        return 0
    streak = row["streak"] or 0
    last_claim = row["last_claim"] or 0
    if streak > 0 and last_claim > 0 and (now_ts - last_claim) >= STREAK_EXPIRE_SECONDS:
        return 0
    return streak


async def ensure_user(db, user_id: int, username: Optional[str] = None, full_name: Optional[str] = None):
    cur = await db.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if await cur.fetchone():
        return
    now = int(time.time())
    await db.execute(
        "INSERT OR IGNORE INTO users (user_id, nickname, registration, streak, last_streak_date, role) "
        "VALUES (?, NULL, ?, 0, 0, 'user')",
        (user_id, now),
    )


def photo_url_for_card(card_id: int, photo_path: Optional[str], photo_uuid: Optional[str]) -> Optional[str]:
    """URL локального фото карточки (если есть файл)."""
    base = "/api/card-photo"
    if photo_uuid:
        for ext in (".webp", ".jpg", ".png", ".jpeg"):
            p = os.path.join(CARD_PHOTOS_DIR, f"{photo_uuid}{ext}")
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                return f"{base}/{photo_uuid}{ext}"
    if photo_path:
        name = os.path.basename(photo_path)
        p = os.path.join(CARD_PHOTOS_DIR, name) if not os.path.isabs(photo_path) else photo_path
        if not os.path.isfile(p):
            p = os.path.join(CARD_PHOTOS_DIR, name)
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            return f"{base}/{name}"
    # legacy by id
    for ext in (".webp", ".jpg"):
        p = os.path.join(CARD_PHOTOS_DIR, f"{card_id}{ext}")
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            return f"{base}/{card_id}{ext}"
    return None


# ── FastAPI app ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("API mini-app starting, DB=%s", DB_NAME)
    yield


app = FastAPI(title="Мряу Mini-App API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static mini-app (CSS/JS)
_static_files_dir = os.path.join(STATIC_DIR, "static")
if os.path.isdir(_static_files_dir):
    app.mount("/static", StaticFiles(directory=_static_files_dir), name="static")
    logger.info("Mounted /static from %s", _static_files_dir)
else:
    logger.warning("No static dir at %s — CSS/JS will 404", _static_files_dir)


def _index_response():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.isfile(index_path):
        return FileResponse(index_path, media_type="text/html; charset=utf-8")
    return JSONResponse(
        {
            "ok": False,
            "error": "index.html not found",
            "static_dir": STATIC_DIR,
            "hint": "Положите webapp/ рядом с api.py или задайте WEBAPP_STATIC",
        },
        status_code=503,
    )


@app.get("/")
async def index():
    return _index_response()


@app.get("/index.html")
async def index_html():
    return _index_response()


@app.get("/app")
@app.get("/webapp")
async def index_aliases():
    return _index_response()


@app.get("/api/health")
async def health():
    index_path = os.path.join(STATIC_DIR, "index.html")
    static_ok = os.path.isdir(_static_files_dir)
    return {
        "ok": True,
        "ts": int(time.time()),
        "static_dir": STATIC_DIR,
        "index_exists": os.path.isfile(index_path),
        "static_mounted": static_ok,
        "bot_token_set": bool(BOT_TOKEN),
        "db_name": DB_NAME,
    }


@app.get("/api/card-photo/{filename}")
async def serve_card_photo(filename: str):
    """Отдача локальных фото карточек (только имя файла, без path traversal)."""
    safe = os.path.basename(filename)
    if not safe or ".." in safe or "/" in safe or "\\" in safe:
        raise HTTPException(404)
    path = os.path.join(CARD_PHOTOS_DIR, safe)
    if not os.path.isfile(path):
        raise HTTPException(404)
    media = "image/webp" if safe.endswith(".webp") else "image/jpeg"
    return FileResponse(path, media_type=media)


# ── Me / Profile ────────────────────────────────────────────────────────────
@app.get("/api/me")
async def api_me(
    authorization: Optional[str] = Header(None),
    x_telegram_init_data: Optional[str] = Header(None, alias="X-Telegram-Init-Data"),
):
    tg_user = await get_auth_user(authorization, x_telegram_init_data)
    user_id = int(tg_user["id"])
    full_name = tg_user.get("first_name", "")
    if tg_user.get("last_name"):
        full_name = f"{full_name} {tg_user['last_name']}".strip()
    username = tg_user.get("username")

    async with get_db() as db:
        await ensure_user(db, user_id, username, full_name)
        cur = await db.execute(
            """
            SELECT u.user_id, u.nickname, u.coins, u.streak, u.last_claim, u.registration, u.role,
                   (SELECT COUNT(*) FROM inventory i WHERE i.user_id = u.user_id) AS cards_count
            FROM users u WHERE u.user_id = ?
            """,
            (user_id,),
        )
        row = await cur.fetchone()
        cur = await db.execute("SELECT COUNT(*) FROM cards")
        total_cards = (await cur.fetchone())[0] or 0

    if not row:
        raise HTTPException(404, "User not found")

    streak = await _streak_value(row)
    nick = display_name(row["nickname"], user_id, full_name)

    return {
        "user_id": user_id,
        "nickname": nick,
        "coins": row["coins"] or 0,
        "streak": streak,
        "cards_count": row["cards_count"] or 0,
        "total_cards": total_cards,
        "registration": row["registration"] or 0,
        "role": row["role"] or "user",
        "username": username,
        "is_own": True,
    }


async def _streak_value(row) -> int:
    now_ts = int(time.time())
    streak = row["streak"] or 0
    last_claim = row["last_claim"] or 0
    if streak > 0 and last_claim > 0 and (now_ts - last_claim) >= STREAK_EXPIRE_SECONDS:
        return 0
    return streak


@app.get("/api/profile/{target_id}")
async def api_profile(
    target_id: int,
    authorization: Optional[str] = Header(None),
    x_telegram_init_data: Optional[str] = Header(None, alias="X-Telegram-Init-Data"),
):
    """Публичный профиль другого игрока (без списка карточек)."""
    tg_user = await get_auth_user(authorization, x_telegram_init_data)
    viewer_id = int(tg_user["id"])

    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT u.user_id, u.nickname, u.coins, u.streak, u.last_claim, u.registration, u.role,
                   (SELECT COUNT(*) FROM inventory i WHERE i.user_id = u.user_id) AS cards_count
            FROM users u WHERE u.user_id = ? AND u.role != 'banned'
            """,
            (target_id,),
        )
        row = await cur.fetchone()
        cur = await db.execute("SELECT COUNT(*) FROM cards")
        total_cards = (await cur.fetchone())[0] or 0

    if not row:
        raise HTTPException(404, "Игрок не найден")

    streak = await _streak_value(row)
    nick = display_name(row["nickname"], target_id)

    return {
        "user_id": target_id,
        "nickname": nick,
        "coins": row["coins"] or 0,
        "streak": streak,
        "cards_count": row["cards_count"] or 0,
        "total_cards": total_cards,
        "registration": row["registration"] or 0,
        "role": row["role"] or "user",
        "is_own": target_id == viewer_id,
        # карточки чужого пользователя не отдаём
        "cards_hidden": target_id != viewer_id,
    }


# ── Collection ──────────────────────────────────────────────────────────────
@app.get("/api/collection")
async def api_collection(
    rarity: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
    x_telegram_init_data: Optional[str] = Header(None, alias="X-Telegram-Init-Data"),
):
    """Список карточек текущего пользователя. rarity=all|common|..."""
    tg_user = await get_auth_user(authorization, x_telegram_init_data)
    user_id = int(tg_user["id"])
    rarities = await load_rarities_from_db()

    async with get_db() as db:
        await ensure_user(db, user_id)
        # stats by rarity
        cur = await db.execute(
            """
            SELECT c.rarity, COUNT(*) AS cnt
            FROM inventory i JOIN cards c ON i.card_id = c.id
            WHERE i.user_id = ?
            GROUP BY c.rarity
            """,
            (user_id,),
        )
        stats = {r["rarity"]: r["cnt"] for r in await cur.fetchall()}

        cur = await db.execute("SELECT COUNT(*) FROM cards")
        total_in_game = (await cur.fetchone())[0] or 0

        # totals per rarity in game
        cur = await db.execute(
            "SELECT rarity, COUNT(*) AS cnt FROM cards GROUP BY rarity"
        )
        rarity_totals = {r["rarity"]: r["cnt"] for r in await cur.fetchall()}

        sql = """
            SELECT c.id, c.name, c.rarity, c.photo_id, c.photo_path, c.photo_uuid, i.claim_time
            FROM inventory i JOIN cards c ON i.card_id = c.id
            WHERE i.user_id = ?
        """
        params: list[Any] = [user_id]
        if rarity and rarity != "all" and rarity in rarities:
            sql += " AND c.rarity = ?"
            params.append(rarity)
        sql += " ORDER BY i.claim_time DESC"

        cur = await db.execute(sql, params)
        rows = await cur.fetchall()

    cards = []
    for r in rows:
        rkey = r["rarity"] or "common"
        info = rarities.get(rkey, DEFAULT_RARITIES.get(rkey, {}))
        cards.append(
            {
                "id": r["id"],
                "name": r["name"],
                "rarity": rkey,
                "rarity_name": info.get("name", rkey),
                "rarity_icon": info.get("icon", "•"),
                "reward": info.get("reward", 0),
                "claim_time": r["claim_time"] or 0,
                "photo_url": photo_url_for_card(r["id"], r["photo_path"], r["photo_uuid"]),
            }
        )

    categories = []
    for key in RARITY_ORDER:
        if key not in rarities:
            continue
        info = rarities[key]
        owned = stats.get(key, 0)
        total_r = rarity_totals.get(key, 0)
        if owned > 0 or total_r > 0:
            categories.append(
                {
                    "key": key,
                    "name": info.get("name", key),
                    "icon": info.get("icon", "•"),
                    "owned": owned,
                    "total": total_r,
                }
            )

    return {
        "cards": cards,
        "total_owned": sum(stats.values()),
        "total_in_game": total_in_game,
        "categories": categories,
        "filter": rarity or "all",
    }


# ── Top ─────────────────────────────────────────────────────────────────────
@app.get("/api/top")
async def api_top(
    kind: str = Query("coins", pattern="^(coins|cards|streak)$"),
    limit: int = Query(20, ge=1, le=50),
    authorization: Optional[str] = Header(None),
    x_telegram_init_data: Optional[str] = Header(None, alias="X-Telegram-Init-Data"),
):
    tg_user = await get_auth_user(authorization, x_telegram_init_data)
    current_user_id = int(tg_user["id"])
    now_ts = int(time.time())

    async with get_db() as db:
        if kind == "cards":
            cur = await db.execute(
                """
                SELECT u.user_id, u.nickname,
                       (SELECT COUNT(*) FROM inventory i WHERE i.user_id = u.user_id) AS value
                FROM users u
                WHERE u.role != 'banned'
                ORDER BY value DESC, u.user_id ASC LIMIT ?
                """,
                (limit,),
            )
            top = await cur.fetchall()
            cur = await db.execute(
                "SELECT COUNT(*) AS value FROM inventory WHERE user_id = ?",
                (current_user_id,),
            )
            my_value = (await cur.fetchone())["value"] or 0
            cur = await db.execute(
                """
                SELECT COUNT(*) + 1 FROM (
                    SELECT u.user_id,
                           (SELECT COUNT(*) FROM inventory i WHERE i.user_id = u.user_id) AS value
                    FROM users u WHERE u.role != 'banned'
                ) WHERE value > ?
                """,
                (my_value,),
            )
            my_rank = (await cur.fetchone())[0]

        elif kind == "streak":
            effective = (
                "CASE WHEN last_claim > 0 AND (? - last_claim) >= ? "
                "THEN 0 ELSE COALESCE(streak, 0) END"
            )
            cur = await db.execute(
                f"SELECT user_id, nickname, ({effective}) AS value FROM users "
                f"WHERE role != 'banned' ORDER BY value DESC, user_id ASC LIMIT ?",
                (now_ts, STREAK_EXPIRE_SECONDS, limit),
            )
            top = await cur.fetchall()
            cur = await db.execute(
                f"SELECT ({effective}) AS value FROM users WHERE user_id = ?",
                (now_ts, STREAK_EXPIRE_SECONDS, current_user_id),
            )
            my_row = await cur.fetchone()
            my_value = (my_row["value"] or 0) if my_row else 0
            cur = await db.execute(
                f"SELECT COUNT(*) + 1 FROM users WHERE ({effective}) > ? AND role != 'banned'",
                (now_ts, STREAK_EXPIRE_SECONDS, my_value),
            )
            my_rank = (await cur.fetchone())[0]

        else:  # coins
            cur = await db.execute(
                "SELECT user_id, nickname, coins AS value FROM users "
                "WHERE role != 'banned' ORDER BY value DESC, user_id ASC LIMIT ?",
                (limit,),
            )
            top = await cur.fetchall()
            cur = await db.execute(
                "SELECT coins AS value FROM users WHERE user_id = ?", (current_user_id,)
            )
            my_row = await cur.fetchone()
            my_value = my_row["value"] if my_row else 0
            cur = await db.execute(
                "SELECT COUNT(*) + 1 FROM users WHERE coins > ? AND role != 'banned'",
                (my_value,),
            )
            my_rank = (await cur.fetchone())[0]

        cur = await db.execute(
            "SELECT nickname FROM users WHERE user_id = ?", (current_user_id,)
        )
        my_row2 = await cur.fetchone()
        my_nick = display_name(
            my_row2["nickname"] if my_row2 else None, current_user_id
        )

    unit_map = {"coins": "🪙", "cards": "🃏", "streak": "🔥"}
    title_map = {
        "coins": "Топ по монетам",
        "cards": "Топ по карточкам",
        "streak": "Топ по стрику",
    }

    items = []
    for i, row in enumerate(top, 1):
        items.append(
            {
                "rank": i,
                "user_id": row["user_id"],
                "nickname": display_name(row["nickname"], row["user_id"]),
                "value": row["value"] or 0,
                "is_me": row["user_id"] == current_user_id,
            }
        )

    return {
        "kind": kind,
        "title": title_map.get(kind, "Топ"),
        "unit": unit_map.get(kind, ""),
        "items": items,
        "my_rank": my_rank,
        "my_value": my_value,
        "my_nickname": my_nick,
    }


@app.get("/api/rarities")
async def api_rarities():
    rarities = await load_rarities_from_db()
    return {
        "rarities": [
            {
                "key": k,
                "name": v.get("name", k),
                "icon": v.get("icon", "•"),
                "reward": v.get("reward", 0),
            }
            for k, v in rarities.items()
        ]
    }
