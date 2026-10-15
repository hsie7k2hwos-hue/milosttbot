"""
API мини-приложения «Мряу».

Запуск: uvicorn api:app --host 0.0.0.0 --port 8080
Env: BOT_TOKEN, DB_NAME, CARD_PHOTOS_DIR, CORS_ORIGINS, WEBAPP_URL
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote

import aiosqlite
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api")

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
DB_NAME = os.getenv("DB_NAME", "/app/data/cards_game.db")
CARD_PHOTOS_DIR = Path(os.getenv("CARD_PHOTOS_DIR", "/app/data/card_photos"))
STREAK_EXPIRE_SECONDS = 24 * 3600
INIT_DATA_MAX_AGE = int(os.getenv("INIT_DATA_MAX_AGE", "86400"))  # 24h

# CORS: Vercel + локалка
_cors = os.getenv("CORS_ORIGINS", "*").strip()
CORS_ORIGINS = [o.strip() for o in _cors.split(",") if o.strip()] if _cors != "*" else ["*"]

# Дефолтные редкости (совпадают с game_config / ботом)
DEFAULT_RARITIES = {
    "common": {"name": "Обычная", "icon": "⚪", "reward": 5, "weight": 50, "order": 1},
    "uncommon": {"name": "Необычная", "icon": "🟢", "reward": 12, "weight": 25, "order": 2},
    "valuable": {"name": "Ценная", "icon": "🟣", "reward": 25, "weight": 12, "order": 3},
    "super_valuable": {"name": "Очень ценная", "icon": "🟠", "reward": 50, "weight": 7, "order": 4},
    "legendary": {"name": "Легендарная", "icon": "🟡", "reward": 100, "weight": 3, "order": 5},
    "negative": {"name": "Негативная", "icon": "💀", "reward": -10, "weight": 3, "order": 6},
}


# ── DB ─────────────────────────────────────────────────────────────────────
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


async def load_rarities(db: aiosqlite.Connection) -> dict:
    try:
        cur = await db.execute(
            "SELECT key, name, icon, reward, weight, sort_order FROM rarities ORDER BY sort_order"
        )
        rows = await cur.fetchall()
        if rows:
            return {
                r["key"]: {
                    "name": r["name"],
                    "icon": r["icon"] or "•",
                    "reward": int(r["reward"] or 0),
                    "weight": int(r["weight"] or 1),
                    "order": int(r["sort_order"] or 0),
                }
                for r in rows
            }
    except Exception:
        pass
    return {k: dict(v) for k, v in DEFAULT_RARITIES.items()}


# ── Auth (Telegram initData) ────────────────────────────────────────────────
def _parse_init_pairs(init_data: str) -> tuple[dict[str, str], str]:
    """
    Разбор initData вручную (без parse_qsl), чтобы значения совпадали
    с тем, что подписывает Telegram.
    Возвращает (pairs_without_hash, received_hash).
    """
    pairs: dict[str, str] = {}
    received_hash = ""
    for chunk in init_data.split("&"):
        if not chunk or "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        if key == "hash":
            received_hash = value
            continue
        if key == "signature":
            # Bot API 8.0 — в HMAC-hash не участвует
            continue
        # Telegram кладёт URL-encoded значения; для data_check_string нужны decoded
        pairs[key] = unquote(value)
    return pairs, received_hash


def validate_init_data(init_data: str, bot_token: str, max_age: int = INIT_DATA_MAX_AGE) -> dict:
    """
    Проверка initData по https://core.telegram.org/bots/webapps#validating-data-received-via-the-web-app

    secret_key = HMAC_SHA256(<bot_token>, "WebAppData")
    calculated_hash = HMAC_SHA256(data_check_string, secret_key)
    """
    if not init_data or not str(init_data).strip():
        raise HTTPException(status_code=401, detail="missing_init_data")
    if not bot_token:
        raise HTTPException(status_code=500, detail="bot_token_not_configured")

    init_data = str(init_data).strip()
    # иногда клиент присылает уже-decoded строку или с префиксом
    if init_data.lower().startswith("tma "):
        init_data = init_data[4:].strip()

    pairs, received_hash = _parse_init_pairs(init_data)
    if not received_hash:
        raise HTTPException(status_code=401, detail="missing_hash")

    data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))

    # key = "WebAppData", message = bot_token  (как в официальной доке)
    secret = hmac.new(
        key=b"WebAppData",
        msg=bot_token.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    calculated = hmac.new(
        key=secret,
        msg=data_check.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated, received_hash):
        # fallback: попробовать без unquote (на случай уже-decoded initData)
        pairs_raw: dict[str, str] = {}
        rh2 = ""
        for chunk in init_data.split("&"):
            if not chunk or "=" not in chunk:
                continue
            key, value = chunk.split("=", 1)
            if key == "hash":
                rh2 = value
                continue
            if key == "signature":
                continue
            pairs_raw[key] = value
        data_check2 = "\n".join(f"{k}={v}" for k, v in sorted(pairs_raw.items()))
        calculated2 = hmac.new(secret, data_check2.encode("utf-8"), hashlib.sha256).hexdigest()
        if hmac.compare_digest(calculated2, rh2 or received_hash):
            pairs = pairs_raw
            received_hash = rh2 or received_hash
        else:
            logger.warning(
                "invalid_signature: init_len=%s auth_date=%s keys=%s token_prefix=%s…",
                len(init_data),
                pairs.get("auth_date"),
                sorted(pairs.keys()),
                bot_token[:8] if bot_token else "",
            )
            raise HTTPException(status_code=401, detail="invalid_signature")

    auth_date = int(pairs.get("auth_date") or 0)
    if max_age > 0 and auth_date > 0:
        age = time.time() - auth_date
        if age > max_age:
            raise HTTPException(status_code=401, detail="init_data_expired")

    user_raw = pairs.get("user")
    if not user_raw:
        raise HTTPException(status_code=401, detail="no_user")
    try:
        user = json.loads(user_raw)
    except json.JSONDecodeError:
        # user мог остаться percent-encoded
        try:
            user = json.loads(unquote(user_raw))
        except Exception:
            raise HTTPException(status_code=401, detail="bad_user_json")
    if not isinstance(user, dict) or "id" not in user:
        raise HTTPException(status_code=401, detail="no_user")

    return {
        "user": user,
        "auth_date": auth_date,
        "query_id": pairs.get("query_id"),
        "start_param": pairs.get("start_param"),
    }


# ── Session tokens (обход обрезки длинных заголовков на прокси) ─────────────
# Формат: base64url(payload_json).base64url(hmac)
# payload: {"uid": int, "exp": unix_ts}
SESSION_TTL = int(os.getenv("SESSION_TTL", "86400"))  # 24h


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def make_session_token(user_id: int, ttl: int = SESSION_TTL) -> str:
    payload = json.dumps(
        {"uid": int(user_id), "exp": int(time.time()) + ttl},
        separators=(",", ":"),
    ).encode()
    sig = hmac.new(
        (BOT_TOKEN or "x").encode(),
        payload,
        hashlib.sha256,
    ).digest()
    return f"{_b64url_encode(payload)}.{_b64url_encode(sig)}"


def verify_session_token(token: str) -> Optional[int]:
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return None
        payload = _b64url_decode(parts[0])
        sig = _b64url_decode(parts[1])
        expected = hmac.new(
            (BOT_TOKEN or "x").encode(),
            payload,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(payload)
        if int(data.get("exp", 0)) < time.time():
            return None
        return int(data["uid"])
    except Exception:
        return None


async def _extract_init_data(request: Request) -> str:
    """Достаём initData из body / заголовков / query."""
    # 1) JSON body — самый надёжный путь через прокси
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            body = await request.json()
            if isinstance(body, dict) and body.get("initData"):
                return str(body["initData"]).strip()
        except Exception:
            pass

    # 2) X-Telegram-Init-Data
    h = request.headers.get("x-telegram-init-data") or request.headers.get(
        "X-Telegram-Init-Data"
    )
    if h and h.strip():
        return h.strip()

    # 3) Authorization: tma <data>
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("tma "):
        return auth[4:].strip()

    # 4) query
    q = request.query_params.get("initData") or request.query_params.get("init_data")
    if q:
        return q.strip()

    return ""


async def get_current_user(request: Request) -> dict:
    """
    Порядок:
    1) Bearer <session_token>  — после POST /api/auth
    2) initData в body/headers — прямая проверка
    """
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is empty — cannot validate")
        raise HTTPException(status_code=500, detail="bot_token_not_configured")

    # Session token
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        uid = verify_session_token(auth[7:].strip())
        if uid:
            return {"id": uid}
        raise HTTPException(status_code=401, detail="session_expired")

    # Прямой initData
    raw = await _extract_init_data(request)
    if not raw:
        logger.warning(
            "auth: no credentials (no Bearer, no initData). headers=%s",
            [k for k in request.headers.keys() if k.lower() in (
                "authorization", "x-telegram-init-data", "content-type"
            )],
        )
        raise HTTPException(status_code=401, detail="unauthorized")

    parsed = validate_init_data(raw, BOT_TOKEN)
    return parsed["user"]


class AuthBody(BaseModel):
    initData: str = Field(..., min_length=10)


# ── Helpers ─────────────────────────────────────────────────────────────────
def display_name(nickname: Optional[str], user_id: int, fallback: Optional[str] = None) -> str:
    if nickname and str(nickname).strip():
        return str(nickname).strip()
    if fallback and str(fallback).strip():
        return str(fallback).strip()[:32]
    return str(user_id)


def effective_streak(streak: int, last_claim: int, now: Optional[int] = None) -> int:
    now = now or int(time.time())
    if streak > 0 and last_claim > 0 and (now - last_claim) >= STREAK_EXPIRE_SECONDS:
        return 0
    return streak or 0


def row_to_user_public(row: aiosqlite.Row, cards_count: int = 0, total_cards: int = 0) -> dict:
    uid = row["user_id"]
    streak = effective_streak(row["streak"] or 0, row["last_claim"] or 0)
    return {
        "user_id": uid,
        "nickname": display_name(row["nickname"], uid),
        "coins": int(row["coins"] or 0),
        "streak": streak,
        "cards_count": cards_count,
        "total_cards": total_cards,
        "registration": int(row["registration"] or 0),
        "role": row["role"] or "user",
    }


# ── App ─────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    Path(DB_NAME).parent.mkdir(parents=True, exist_ok=True)
    CARD_PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("API ready · DB=%s · photos=%s", DB_NAME, CARD_PHOTOS_DIR)
    yield


app = FastAPI(title="Мряу Mini App API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Health ──────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {
        "ok": True,
        "ts": int(time.time()),
        "bot_token_set": bool(BOT_TOKEN),
        "bot_token_prefix": (BOT_TOKEN[:6] + "…") if BOT_TOKEN else None,
    }


@app.post("/api/auth")
async def api_auth(body: AuthBody):
    """
    Обмен initData → короткий session token.
    initData идёт в JSON body — прокси его не обрезает (в отличие от заголовков).
    """
    if not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="bot_token_not_configured")

    raw = (body.initData or "").strip()
    if not raw:
        raise HTTPException(status_code=401, detail="missing_init_data")

    try:
        parsed = validate_init_data(raw, BOT_TOKEN)
    except HTTPException as e:
        logger.warning(
            "POST /api/auth failed: %s init_len=%s has_hash=%s token_set=%s",
            e.detail,
            len(raw),
            "hash=" in raw,
            bool(BOT_TOKEN),
        )
        raise

    user = parsed["user"]
    uid = int(user["id"])
    token = make_session_token(uid)
    logger.info("auth OK user_id=%s", uid)
    return {
        "ok": True,
        "token": token,
        "user_id": uid,
        "username": user.get("username"),
        "first_name": user.get("first_name"),
        "expires_in": SESSION_TTL,
    }


@app.post("/api/auth-check")
async def auth_check(body: AuthBody):
    """Диагностика подписи initData (POST body)."""
    raw = (body.initData or "").strip()
    if not raw:
        return JSONResponse(
            status_code=401,
            content={"ok": False, "error": "no_init_data", "init_data_len": 0},
        )
    if not BOT_TOKEN:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": "bot_token_not_configured"},
        )
    try:
        parsed = validate_init_data(raw, BOT_TOKEN)
        u = parsed["user"]
        return {
            "ok": True,
            "user_id": u.get("id"),
            "username": u.get("username"),
            "auth_date": parsed.get("auth_date"),
            "init_data_len": len(raw),
        }
    except HTTPException as e:
        return JSONResponse(
            status_code=e.status_code,
            content={
                "ok": False,
                "error": e.detail,
                "init_data_len": len(raw),
                "has_hash": "hash=" in raw,
                "bot_token_prefix": (BOT_TOKEN[:6] + "…") if BOT_TOKEN else None,
                "hint": (
                    "BOT_TOKEN на API = токен user-бота, к которому привязан WebApp в BotFather"
                    if e.detail == "invalid_signature"
                    else None
                ),
            },
        )


@app.get("/api/rarities")
async def api_rarities():
    async with get_db() as db:
        rarities = await load_rarities(db)
    items = [
        {"key": k, **v}
        for k, v in sorted(rarities.items(), key=lambda x: x[1].get("order", 99))
    ]
    return {"rarities": items}


# ── Me / Profile ────────────────────────────────────────────────────────────
@app.get("/api/me")
async def api_me(user: dict = Depends(get_current_user)):
    user_id = int(user["id"])
    async with get_db() as db:
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        if not row:
            # создаём минимальную запись (как get_or_create_user в боте)
            now = int(time.time())
            await db.execute(
                "INSERT OR IGNORE INTO users (user_id, registration, role) VALUES (?, ?, 'user')",
                (user_id, now),
            )
            await db.commit()
            cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()

        if (row["role"] or "") == "banned":
            raise HTTPException(status_code=403, detail="banned")

        cur = await db.execute(
            "SELECT COUNT(*) FROM inventory WHERE user_id = ?", (user_id,)
        )
        cards_count = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM cards")
        total_cards = (await cur.fetchone())[0]

        fallback = user.get("first_name") or user.get("username")
        data = row_to_user_public(row, cards_count, total_cards)
        data["nickname"] = display_name(row["nickname"], user_id, fallback)
        data["tg_username"] = user.get("username")
        data["tg_first_name"] = user.get("first_name")
        data["is_own"] = True
        return data


@app.get("/api/profile/{target_id}")
async def api_profile(target_id: int, user: dict = Depends(get_current_user)):
    """Публичный профиль. Карточки чужих пользователей не отдаём."""
    async with get_db() as db:
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (target_id,))
        row = await cur.fetchone()
        if not row or (row["role"] or "") == "banned":
            raise HTTPException(status_code=404, detail="user_not_found")

        cur = await db.execute(
            "SELECT COUNT(*) FROM inventory WHERE user_id = ?", (target_id,)
        )
        cards_count = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM cards")
        total_cards = (await cur.fetchone())[0]

        data = row_to_user_public(row, cards_count, total_cards)
        data["is_own"] = int(user["id"]) == target_id
        # чужие карточки не отдаём
        return data


# ── Collection (только свои) ────────────────────────────────────────────────
@app.get("/api/collection")
async def api_collection(
    rarity: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    user_id = int(user["id"])
    async with get_db() as db:
        rarities = await load_rarities(db)

        # stats по редкостям
        cur = await db.execute(
            """
            SELECT c.rarity, COUNT(*) AS cnt
            FROM inventory i
            JOIN cards c ON i.card_id = c.id
            WHERE i.user_id = ?
            GROUP BY c.rarity
            """,
            (user_id,),
        )
        stats_rows = await cur.fetchall()
        stats = {r["rarity"]: r["cnt"] for r in stats_rows}

        rarity_stats = []
        for key, info in sorted(rarities.items(), key=lambda x: x[1].get("order", 99)):
            cur = await db.execute(
                "SELECT COUNT(*) FROM cards WHERE rarity = ?", (key,)
            )
            total_of = (await cur.fetchone())[0]
            owned = stats.get(key, 0)
            if owned == 0 and total_of == 0:
                continue
            rarity_stats.append(
                {
                    "key": key,
                    "name": info["name"],
                    "icon": info["icon"],
                    "owned": owned,
                    "total": total_of,
                }
            )

        # список карточек
        params: list[Any] = [user_id]
        where = "i.user_id = ?"
        if rarity and rarity in rarities:
            where += " AND c.rarity = ?"
            params.append(rarity)

        cur = await db.execute(
            f"""
            SELECT c.id, c.name, c.rarity, c.photo_path, c.photo_uuid, c.photo_id,
                   i.claim_time
            FROM inventory i
            JOIN cards c ON i.card_id = c.id
            WHERE {where}
            ORDER BY i.claim_time DESC
            """,
            params,
        )
        cards = []
        for r in await cur.fetchall():
            photo_url = None
            if r["photo_uuid"] or r["photo_path"] or r["id"]:
                photo_url = f"/api/photo/{r['id']}"
            cards.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "rarity": r["rarity"],
                    "rarity_name": rarities.get(r["rarity"], {}).get("name", r["rarity"]),
                    "rarity_icon": rarities.get(r["rarity"], {}).get("icon", "•"),
                    "reward": rarities.get(r["rarity"], {}).get("reward", 0),
                    "claim_time": int(r["claim_time"] or 0),
                    "photo_url": photo_url,
                }
            )

        cur = await db.execute("SELECT COUNT(*) FROM cards")
        total_in_game = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT COUNT(*) FROM inventory WHERE user_id = ?", (user_id,)
        )
        total_owned = (await cur.fetchone())[0]

    return {
        "total_owned": total_owned,
        "total_in_game": total_in_game,
        "rarity_stats": rarity_stats,
        "cards": cards,
        "filter": rarity,
    }


# ── Top ─────────────────────────────────────────────────────────────────────
@app.get("/api/top")
async def api_top(
    kind: str = Query("coins", pattern="^(coins|cards|streak)$"),
    limit: int = Query(20, ge=1, le=50),
    user: dict = Depends(get_current_user),
):
    user_id = int(user["id"])
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
            top = await cur.fetchall()
            cur = await db.execute(
                "SELECT COUNT(*) AS value FROM inventory WHERE user_id = ?",
                (user_id,),
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
                f"""
                SELECT user_id, nickname, ({effective}) AS value
                FROM users
                WHERE role != 'banned'
                ORDER BY value DESC, user_id ASC
                LIMIT ?
                """,
                (now_ts, STREAK_EXPIRE_SECONDS, limit),
            )
            top = await cur.fetchall()
            cur = await db.execute(
                f"SELECT ({effective}) AS value FROM users WHERE user_id = ?",
                (now_ts, STREAK_EXPIRE_SECONDS, user_id),
            )
            my_row = await cur.fetchone()
            my_value = (my_row["value"] if my_row else 0) or 0
            cur = await db.execute(
                f"SELECT COUNT(*) + 1 FROM users WHERE ({effective}) > ? AND role != 'banned'",
                (now_ts, STREAK_EXPIRE_SECONDS, my_value),
            )
            my_rank = (await cur.fetchone())[0]

        else:  # coins
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
            top = await cur.fetchall()
            cur = await db.execute(
                "SELECT coins AS value FROM users WHERE user_id = ?", (user_id,)
            )
            my_row = await cur.fetchone()
            my_value = (my_row["value"] if my_row else 0) or 0
            cur = await db.execute(
                "SELECT COUNT(*) + 1 FROM users WHERE coins > ? AND role != 'banned'",
                (my_value,),
            )
            my_rank = (await cur.fetchone())[0]

        items = [
            {
                "rank": i,
                "user_id": r["user_id"],
                "nickname": display_name(r["nickname"], r["user_id"]),
                "value": int(r["value"] or 0),
                "is_me": r["user_id"] == user_id,
            }
            for i, r in enumerate(top, 1)
        ]

        cur = await db.execute(
            "SELECT nickname FROM users WHERE user_id = ?", (user_id,)
        )
        my_nick_row = await cur.fetchone()
        my_nick = display_name(
            my_nick_row["nickname"] if my_nick_row else None, user_id
        )

    return {
        "kind": kind,
        "items": items,
        "me": {
            "rank": my_rank,
            "value": int(my_value),
            "nickname": my_nick,
            "user_id": user_id,
        },
    }


# ── Card photo ──────────────────────────────────────────────────────────────
def _find_photo(card_id: int, photo_path: Optional[str], photo_uuid: Optional[str]) -> Optional[Path]:
    if photo_path:
        p = Path(photo_path)
        if p.is_file() and p.stat().st_size > 0:
            return p
        cand = CARD_PHOTOS_DIR / p.name
        if cand.is_file() and cand.stat().st_size > 0:
            return cand
    if photo_uuid:
        for e in (".webp", ".jpg", ".png", ".jpeg"):
            cand = CARD_PHOTOS_DIR / f"{photo_uuid}{e}"
            if cand.is_file() and cand.stat().st_size > 0:
                return cand
    for e in (".webp", ".jpg"):
        cand = CARD_PHOTOS_DIR / f"{card_id}{e}"
        if cand.is_file() and cand.stat().st_size > 0:
            return cand
    return None


@app.get("/api/photo/{card_id}")
async def api_photo(card_id: int):
    """Публичная раздача локальных фото карточек (без auth — только id)."""
    async with get_db() as db:
        cur = await db.execute(
            "SELECT photo_path, photo_uuid FROM cards WHERE id = ?", (card_id,)
        )
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="not_found")

    path = _find_photo(card_id, row["photo_path"], row["photo_uuid"])
    if not path:
        raise HTTPException(status_code=404, detail="photo_missing")

    media = "image/webp" if path.suffix.lower() == ".webp" else "image/jpeg"
    return FileResponse(path, media_type=media, headers={"Cache-Control": "public, max-age=86400"})


# ── Static fallback (если API и статика на одном хосте) ─────────────────────
# На BotHost можно положить dist/ рядом, на Vercel фронт отдельно.
_STATIC = Path(__file__).parent / "static"
if _STATIC.is_dir():
    app.mount("/", StaticFiles(directory=str(_STATIC), html=True), name="static")
