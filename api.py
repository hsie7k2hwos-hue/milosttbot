"""
API для Telegram Mini App «Мряу».

    uvicorn api:app --host 0.0.0.0 --port 8080

Env:
    BOT_TOKEN, DB_NAME, CORS_ORIGINS, CARD_PHOTOS_DIR / CARDS_PHOTO_DIR
    SUPERADMIN_IDS — через запятую
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qsl

import aiosqlite
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_NAME = os.getenv("DB_NAME", "/app/data/cards_game.db")
CORS_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "CORS_ORIGINS",
        "https://web.telegram.org,http://localhost:3000,http://localhost:8080,http://127.0.0.1:8080",
    ).split(",")
    if o.strip()
]

SUPERADMIN_IDS = {
    int(x.strip())
    for x in (os.getenv("SUPERADMIN_IDS") or os.getenv("SUPERADMIN_ID") or "").split(",")
    if x.strip().isdigit()
}

try:
    from game_config import DEFAULT_RARITIES, API_COOLDOWN_SECONDS
    RARITIES = {k: dict(v) for k, v in DEFAULT_RARITIES.items()}
    COOLDOWN_SECONDS = API_COOLDOWN_SECONDS
except ImportError:
    RARITIES = {
        "common":         {"icon": "⚪", "name": "Обычная",     "reward": 10,  "weight": 40},
        "rare":           {"icon": "🔵", "name": "Редкая",      "reward": 25,  "weight": 25},
        "uncommon":       {"icon": "🟢", "name": "Необычная",   "reward": 45,  "weight": 15},
        "valuable":       {"icon": "🟣", "name": "Ценная",      "reward": 75,  "weight": 10},
        "super_valuable": {"icon": "🟠", "name": "Сверхценная", "reward": 120, "weight": 7},
        "legendary":      {"icon": "🟡", "name": "Легендарная", "reward": 200, "weight": 3},
    }
    COOLDOWN_SECONDS = 4 * 3600

ROLES = {
    "user":       {"icon": "👤", "name": "Пользователь"},
    "admin":      {"icon": "🛡", "name": "Администратор"},
    "superadmin": {"icon": "👑", "name": "Главный администратор"},
    "banned":     {"icon": "🚫", "name": "Заблокирован"},
}

app = FastAPI(title="Мряу Mini App API", version="2.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS + ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def validate_init_data(init_data: str) -> dict[str, Any]:
    if not BOT_TOKEN:
        raise HTTPException(500, "BOT_TOKEN не задан на сервере")
    if not init_data or not init_data.strip():
        raise HTTPException(401, "Нет initData")

    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "Нет hash в initData")

    data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calculated, received_hash):
        raise HTTPException(401, "Неверная подпись initData")

    auth_date = int(parsed.get("auth_date", 0))
    if auth_date and time.time() - auth_date > 86400:
        raise HTTPException(401, "initData устарел")

    user_raw = parsed.get("user")
    if not user_raw:
        raise HTTPException(401, "Нет user в initData")

    try:
        user = json.loads(user_raw)
    except json.JSONDecodeError:
        raise HTTPException(401, "Некорректный user JSON")

    if not user.get("id"):
        raise HTTPException(401, "Нет user.id")

    return {"user": user, "raw": parsed}


def get_user_from_header(x_telegram_init_data: Optional[str]) -> dict:
    if not x_telegram_init_data:
        raise HTTPException(401, "Заголовок X-Telegram-Init-Data обязателен")
    return validate_init_data(x_telegram_init_data)["user"]


def card_photo_url(card_id: int) -> str:
    return f"/api/card/{int(card_id)}/photo"


def _photo_search_dirs() -> list[Path]:
    dirs: list[Path] = []
    env_dir = os.getenv("CARDS_PHOTO_DIR") or os.getenv("CARD_PHOTOS_DIR")
    if env_dir:
        dirs.append(Path(env_dir))
    try:
        db_parent = Path(DB_NAME).expanduser().resolve().parent
        dirs.append(db_parent / "card_photos")
    except Exception:
        pass
    dirs.extend(
        [
            Path("/app/data/card_photos"),
            Path("/app/card_photos"),
            Path("card_photos"),
            Path("./card_photos"),
        ]
    )
    seen: set[str] = set()
    out: list[Path] = []
    for d in dirs:
        try:
            key = str(d.resolve()) if d.exists() else str(d)
        except Exception:
            key = str(d)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def resolve_card_photo_file(card_id: int, photo_path: str | None = None) -> Path | None:
    """Только по photo_path из БД — не по card_id (защита от перебора)."""
    if photo_path:
        pp = Path(str(photo_path))
        if pp.is_file() and pp.stat().st_size > 0:
            return pp
        name = pp.name
        if name:
            for d in _photo_search_dirs():
                cand = d / name
                if cand.is_file() and cand.stat().st_size > 0:
                    return cand
    return None


async def get_db():
    db = await aiosqlite.connect(DB_NAME)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON;")
    return db


async def ensure_user(db: aiosqlite.Connection, tg_user: dict) -> aiosqlite.Row:
    user_id = tg_user["id"]
    cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = await cur.fetchone()
    if row:
        return row

    nickname = (tg_user.get("first_name") or "") + (
        f" {tg_user['last_name']}" if tg_user.get("last_name") else ""
    )
    nickname = (nickname.strip() or tg_user.get("username") or str(user_id))[:32]
    now = int(time.time())
    role = "superadmin" if user_id in SUPERADMIN_IDS else "user"

    if role != "superadmin" and SUPERADMIN_IDS:
        cur = await db.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'superadmin'"
        )
        if (await cur.fetchone())[0] == 0 and user_id in SUPERADMIN_IDS:
            role = "superadmin"

    await db.execute(
        "INSERT INTO users (user_id, nickname, registration, streak, last_streak_date, role) "
        "VALUES (?, ?, ?, 0, 0, ?)",
        (user_id, nickname, now, role),
    )
    await db.commit()
    cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    return await cur.fetchone()


async def require_admin(db: aiosqlite.Connection, tg_user: dict) -> aiosqlite.Row:
    user = await ensure_user(db, tg_user)
    role = user["role"] or "user"
    if role not in ("admin", "superadmin"):
        raise HTTPException(403, "Недостаточно прав")
    return user


async def ensure_photo_columns(db: aiosqlite.Connection) -> None:
    for col in ("photo_path", "photo_uuid"):
        try:
            await db.execute(f"ALTER TABLE cards ADD COLUMN {col} TEXT")
            await db.commit()
        except Exception:
            pass


def _day_key(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _calc_streak(last_streak_date: int, current_streak: int, now: int) -> tuple[int, int]:
    today = _day_key(now)
    if not last_streak_date:
        return 1, now
    last_day = _day_key(int(last_streak_date))
    if last_day == today:
        return max(current_streak, 1), last_streak_date
    yesterday_ts = now - 86400
    if _day_key(yesterday_ts) == last_day:
        return (current_streak or 0) + 1, now
    return 1, now


@app.get("/api/card/{card_id}/photo")
async def api_card_photo(card_id: int):
    db = await get_db()
    try:
        await ensure_photo_columns(db)
        cur = await db.execute(
            "SELECT photo_path FROM cards WHERE id = ?", (card_id,)
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "Карточка не найдена")
        photo_path = row["photo_path"] if row["photo_path"] else None
        path = resolve_card_photo_file(card_id, photo_path)
        if not path:
            raise HTTPException(404, "photo_not_found")
        media = "image/jpeg"
        suf = path.suffix.lower()
        if suf == ".png":
            media = "image/png"
        elif suf == ".webp":
            media = "image/webp"
        elif suf == ".gif":
            media = "image/gif"
        return FileResponse(
            path,
            media_type=media,
            filename=path.name,
            headers={"Cache-Control": "public, max-age=86400"},
        )
    finally:
        await db.close()


@app.get("/health")
async def health():
    return {"ok": True, "ts": int(time.time()), "version": "2.1"}


@app.get("/api/me")
async def api_me(x_telegram_init_data: Optional[str] = Header(None)):
    tg_user = get_user_from_header(x_telegram_init_data)
    user_id = tg_user["id"]
    db = await get_db()
    try:
        user = await ensure_user(db, tg_user)
        if (user["role"] or "user") == "banned":
            raise HTTPException(403, "Вы заблокированы")
        cur = await db.execute(
            "SELECT COUNT(DISTINCT card_id) FROM inventory WHERE user_id = ?",
            (user_id,),
        )
        cards_count = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM cards")
        total_cards = (await cur.fetchone())[0]
        role = user["role"] or "user"
        r = ROLES.get(role, ROLES["user"])
        reg = user["registration"] or 0
        last_claim = user["last_claim"] or 0
        now = int(time.time())
        remaining_cd = max(0, COOLDOWN_SECONDS - (now - last_claim)) if last_claim else 0
        return {
            "user_id": user_id,
            "nickname": user["nickname"] or f"User{user_id}",
            "username": tg_user.get("username"),
            "photo_url": tg_user.get("photo_url"),
            "role": role,
            "role_display": f"{r['icon']} {r['name']}",
            "coins": user["coins"] or 0,
            "streak": user["streak"] or 0,
            "cards_count": cards_count,
            "total_cards": total_cards,
            "registration": reg,
            "registration_str": datetime.fromtimestamp(reg).strftime("%d.%m.%Y") if reg else "—",
            "days_with_us": max(0, (now - reg) // 86400) if reg else 0,
            "last_claim": last_claim,
            "cooldown_remaining": remaining_cd,
            "cooldown_seconds": COOLDOWN_SECONDS,
            "can_claim_free": remaining_cd <= 0,
            "is_admin": role in ("admin", "superadmin"),
            "is_superadmin": role == "superadmin",
        }
    finally:
        await db.close()


@app.get("/api/collection")
async def api_collection(
    rarity: Optional[str] = Query(None),
    x_telegram_init_data: Optional[str] = Header(None),
):
    tg_user = get_user_from_header(x_telegram_init_data)
    user_id = tg_user["id"]
    db = await get_db()
    try:
        await ensure_user(db, tg_user)
        await ensure_photo_columns(db)
        cur = await db.execute(
            """SELECT c.rarity, COUNT(DISTINCT i.card_id) AS cnt
               FROM inventory i JOIN cards c ON i.card_id = c.id
               WHERE i.user_id = ? GROUP BY c.rarity""",
            (user_id,),
        )
        stats = {row["rarity"]: row["cnt"] for row in await cur.fetchall()}
        cur = await db.execute("SELECT COUNT(*) FROM cards")
        total_in_game = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT COUNT(DISTINCT card_id) FROM inventory WHERE user_id = ?",
            (user_id,),
        )
        owned_unique = (await cur.fetchone())[0]
        sql = """
            SELECT c.id, c.name, c.rarity, c.photo_id, c.photo_path, i.claim_time
            FROM inventory i JOIN cards c ON i.card_id = c.id
            WHERE i.user_id = ?
        """
        params: list = [user_id]
        if rarity and rarity in RARITIES:
            sql += " AND c.rarity = ?"
            params.append(rarity)
        sql += " ORDER BY i.claim_time DESC"
        cur = await db.execute(sql, params)
        rows = await cur.fetchall()
        cards = []
        for row in rows:
            r = RARITIES.get(row["rarity"], {})
            cid = row["id"]
            pp = row["photo_path"] if "photo_path" in row.keys() else None
            has_photo = resolve_card_photo_file(cid, pp) is not None
            cards.append({
                "id": cid,
                "name": row["name"],
                "rarity": row["rarity"],
                "rarity_icon": r.get("icon", ""),
                "rarity_name": r.get("name", row["rarity"]),
                "reward": r.get("reward", 0),
                "photo_id": row["photo_id"],
                "photo_url": card_photo_url(cid),
                "has_photo": has_photo,
                "claim_time": row["claim_time"],
            })
        rarity_totals = {}
        for rk in RARITIES:
            cur = await db.execute("SELECT COUNT(*) FROM cards WHERE rarity = ?", (rk,))
            rarity_totals[rk] = (await cur.fetchone())[0]
        return {
            "owned_unique": owned_unique,
            "total_in_game": total_in_game,
            "stats": stats,
            "rarity_totals": rarity_totals,
            "cards": cards,
        }
    finally:
        await db.close()


@app.get("/api/top")
async def api_top(
    kind: str = Query("coins"),
    x_telegram_init_data: Optional[str] = Header(None),
):
    tg_user = get_user_from_header(x_telegram_init_data)
    user_id = tg_user["id"]
    limit = 10
    if kind not in ("coins", "cards", "streak"):
        kind = "coins"
    db = await get_db()
    try:
        await ensure_user(db, tg_user)
        if kind == "cards":
            cur = await db.execute(
                """SELECT u.user_id, u.nickname,
                          COALESCE(COUNT(DISTINCT i.card_id), 0) AS value
                   FROM users u LEFT JOIN inventory i ON u.user_id = i.user_id
                   WHERE u.role != 'banned'
                   GROUP BY u.user_id ORDER BY value DESC, u.user_id ASC LIMIT ?""",
                (limit,),
            )
            top = await cur.fetchall()
            cur = await db.execute(
                "SELECT COALESCE(COUNT(DISTINCT card_id), 0) FROM inventory WHERE user_id = ?",
                (user_id,),
            )
            my_value = (await cur.fetchone())[0]
            cur = await db.execute(
                """SELECT COUNT(*) + 1 FROM (
                     SELECT u.user_id, COALESCE(COUNT(DISTINCT i.card_id), 0) AS value
                     FROM users u LEFT JOIN inventory i ON u.user_id = i.user_id
                     WHERE u.role != 'banned' GROUP BY u.user_id
                   ) WHERE value > ?""",
                (my_value,),
            )
            my_rank = (await cur.fetchone())[0]
            unit, title = "🃏", "🃏 Топ по карточкам"
        elif kind == "streak":
            cur = await db.execute(
                """SELECT user_id, nickname, streak AS value FROM users
                   WHERE role != 'banned' ORDER BY value DESC, user_id ASC LIMIT ?""",
                (limit,),
            )
            top = await cur.fetchall()
            cur = await db.execute("SELECT streak FROM users WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            my_value = (row["streak"] or 0) if row else 0
            cur = await db.execute(
                "SELECT COUNT(*) + 1 FROM users WHERE streak > ? AND role != 'banned'",
                (my_value,),
            )
            my_rank = (await cur.fetchone())[0]
            unit, title = "🔥", "🔥 Топ по стрику"
        else:
            cur = await db.execute(
                """SELECT user_id, nickname, coins AS value FROM users
                   WHERE role != 'banned' ORDER BY value DESC, user_id ASC LIMIT ?""",
                (limit,),
            )
            top = await cur.fetchall()
            cur = await db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            my_value = (row["coins"] or 0) if row else 0
            cur = await db.execute(
                "SELECT COUNT(*) + 1 FROM users WHERE coins > ? AND role != 'banned'",
                (my_value,),
            )
            my_rank = (await cur.fetchone())[0]
            unit, title = "🪙", "🪙 Топ по монетам"
        items = [
            {
                "rank": i,
                "user_id": row["user_id"],
                "nickname": row["nickname"] or f"User{row['user_id']}",
                "value": row["value"] or 0,
            }
            for i, row in enumerate(top, 1)
        ]
        return {"kind": kind, "title": title, "unit": unit, "items": items, "me": {"rank": my_rank, "value": my_value}}
    finally:
        await db.close()


@app.post("/api/claim")
async def api_claim(x_telegram_init_data: Optional[str] = Header(None)):
    tg_user = get_user_from_header(x_telegram_init_data)
    user_id = tg_user["id"]
    now = int(time.time())
    db = await get_db()
    try:
        user = await ensure_user(db, tg_user)
        if (user["role"] or "user") == "banned":
            raise HTTPException(403, "Вы заблокированы")
        last_claim = user["last_claim"] or 0
        if last_claim and (now - last_claim) < COOLDOWN_SECONDS:
            remaining = COOLDOWN_SECONDS - (now - last_claim)
            raise HTTPException(429, detail={"error": "cooldown", "remaining": remaining, "cooldown_seconds": COOLDOWN_SECONDS})
        cur = await db.execute("SELECT COUNT(*) FROM cards")
        total_cards = (await cur.fetchone())[0]
        if total_cards == 0:
            raise HTTPException(400, "no_cards")
        cur = await db.execute("SELECT COUNT(*) FROM inventory WHERE user_id = ?", (user_id,))
        owned = (await cur.fetchone())[0]
        if owned >= total_cards:
            raise HTTPException(400, "all_collected")
        rarities_list = list(RARITIES.keys())
        weights = [RARITIES[r]["weight"] for r in rarities_list]
        card = None
        selected_rarity = None
        for _ in range(12):
            selected_rarity = random.choices(rarities_list, weights=weights, k=1)[0]
            cur = await db.execute(
                """SELECT id, name, photo_id, photo_path, rarity FROM cards
                   WHERE rarity = ? AND id NOT IN (SELECT card_id FROM inventory WHERE user_id = ?)
                   ORDER BY RANDOM() LIMIT 1""",
                (selected_rarity, user_id),
            )
            card = await cur.fetchone()
            if card:
                break
        if not card:
            cur = await db.execute(
                """SELECT id, name, photo_id, photo_path, rarity FROM cards
                   WHERE id NOT IN (SELECT card_id FROM inventory WHERE user_id = ?)
                   ORDER BY RANDOM() LIMIT 1""",
                (user_id,),
            )
            card = await cur.fetchone()
            if card:
                selected_rarity = card["rarity"]
        if not card:
            raise HTTPException(400, "all_collected")
        reward = RARITIES.get(selected_rarity, RARITIES["common"])["reward"]
        new_streak, new_last = _calc_streak(user["last_streak_date"] or 0, user["streak"] or 0, now)
        await db.execute(
            """UPDATE users SET last_claim = ?, coins = coins + ?, streak = ?, last_streak_date = ?
               WHERE user_id = ?""",
            (now, reward, new_streak, new_last, user_id),
        )
        await db.execute(
            """INSERT INTO inventory (user_id, card_id, claim_time) VALUES (?, ?, ?)
               ON CONFLICT(user_id, card_id) DO UPDATE SET claim_time = ?""",
            (user_id, card["id"], now, now),
        )
        await db.commit()
        cur = await db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
        balance = (await cur.fetchone())["coins"]
        r = RARITIES.get(selected_rarity, {})
        return {
            "ok": True,
            "card": {
                "id": card["id"],
                "name": card["name"],
                "rarity": selected_rarity,
                "rarity_icon": r.get("icon", ""),
                "rarity_name": r.get("name", selected_rarity),
                "reward": reward,
                "photo_url": card_photo_url(card["id"]),
            },
            "coins_earned": reward,
            "balance": balance,
            "streak": new_streak,
            "cooldown_seconds": COOLDOWN_SECONDS,
            "next_claim_at": now + COOLDOWN_SECONDS,
        }
    finally:
        await db.close()


@app.get("/api/card/{card_id}")
async def api_card_one(card_id: int, x_telegram_init_data: Optional[str] = Header(None)):
    tg_user = get_user_from_header(x_telegram_init_data)
    db = await get_db()
    try:
        await ensure_user(db, tg_user)
        cur = await db.execute(
            "SELECT id, name, rarity, photo_id, photo_path FROM cards WHERE id = ?",
            (card_id,),
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "Карточка не найдена")
        r = RARITIES.get(row["rarity"], {})
        pp = row["photo_path"] if "photo_path" in row.keys() else None
        return {
            "id": row["id"],
            "name": row["name"],
            "rarity": row["rarity"],
            "rarity_icon": r.get("icon", ""),
            "rarity_name": r.get("name", row["rarity"]),
            "reward": r.get("reward", 0),
            "photo_url": card_photo_url(row["id"]),
            "has_photo": resolve_card_photo_file(row["id"], pp) is not None,
        }
    finally:
        await db.close()


@app.get("/api/admin/overview")
async def api_admin_overview(x_telegram_init_data: Optional[str] = Header(None)):
    tg_user = get_user_from_header(x_telegram_init_data)
    db = await get_db()
    try:
        await require_admin(db, tg_user)
        cur = await db.execute("SELECT COUNT(*) FROM users")
        users = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM cards")
        cards = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM inventory")
        inv = (await cur.fetchone())[0]
        cur = await db.execute("SELECT rarity, COUNT(*) FROM cards GROUP BY rarity")
        by_r = {row[0]: row[1] for row in await cur.fetchall()}
        return {"users": users, "cards": cards, "inventory_rows": inv, "by_rarity": by_r}
    finally:
        await db.close()


class CardCreateBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    rarity: str


@app.get("/api/admin/cards")
async def api_admin_cards(
    page: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    rarity: Optional[str] = Query(None),
    x_telegram_init_data: Optional[str] = Header(None),
):
    tg_user = get_user_from_header(x_telegram_init_data)
    db = await get_db()
    try:
        await require_admin(db, tg_user)
        where, params = "", []
        if rarity and rarity in RARITIES:
            where = "WHERE rarity = ?"
            params.append(rarity)
        cur = await db.execute(f"SELECT COUNT(*) FROM cards {where}", params)
        total = (await cur.fetchone())[0]
        params.extend([limit, page * limit])
        cur = await db.execute(
            f"SELECT id, name, rarity, photo_path FROM cards {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params,
        )
        rows = await cur.fetchall()
        return {
            "total": total,
            "page": page,
            "limit": limit,
            "cards": [
                {"id": r["id"], "name": r["name"], "rarity": r["rarity"], "photo_url": card_photo_url(r["id"])}
                for r in rows
            ],
        }
    finally:
        await db.close()


@app.post("/api/admin/cards")
async def api_admin_create_card(body: CardCreateBody, x_telegram_init_data: Optional[str] = Header(None)):
    tg_user = get_user_from_header(x_telegram_init_data)
    if body.rarity not in RARITIES:
        raise HTTPException(400, "Неизвестная редкость")
    db = await get_db()
    try:
        await require_admin(db, tg_user)
        cur = await db.execute(
            "INSERT INTO cards (name, rarity) VALUES (?, ?)",
            (body.name.strip()[:64], body.rarity),
        )
        await db.commit()
        return {"ok": True, "id": cur.lastrowid}
    finally:
        await db.close()


class CardPatchBody(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=64)
    rarity: Optional[str] = None


@app.patch("/api/admin/cards/{card_id}")
async def api_admin_patch_card(
    card_id: int, body: CardPatchBody, x_telegram_init_data: Optional[str] = Header(None)
):
    tg_user = get_user_from_header(x_telegram_init_data)
    db = await get_db()
    try:
        await require_admin(db, tg_user)
        updates, params = [], []
        if body.name is not None:
            updates.append("name = ?")
            params.append(body.name.strip()[:64])
        if body.rarity is not None:
            if body.rarity not in RARITIES:
                raise HTTPException(400, "Неизвестная редкость")
            updates.append("rarity = ?")
            params.append(body.rarity)
        if not updates:
            raise HTTPException(400, "Нечего обновлять")
        params.append(card_id)
        await db.execute(f"UPDATE cards SET {', '.join(updates)} WHERE id = ?", params)
        await db.commit()
        return {"ok": True}
    finally:
        await db.close()


@app.delete("/api/admin/cards/{card_id}")
async def api_admin_delete_card(card_id: int, x_telegram_init_data: Optional[str] = Header(None)):
    tg_user = get_user_from_header(x_telegram_init_data)
    db = await get_db()
    try:
        await require_admin(db, tg_user)
        await db.execute("DELETE FROM inventory WHERE card_id = ?", (card_id,))
        await db.execute("DELETE FROM cards WHERE id = ?", (card_id,))
        await db.commit()
        return {"ok": True}
    finally:
        await db.close()


@app.get("/api/admin/users")
async def api_admin_users(
    page: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    role: Optional[str] = Query(None),
    x_telegram_init_data: Optional[str] = Header(None),
):
    tg_user = get_user_from_header(x_telegram_init_data)
    db = await get_db()
    try:
        await require_admin(db, tg_user)
        where, params = "", []
        if role and role in ROLES:
            where = "WHERE role = ?"
            params.append(role)
        cur = await db.execute(f"SELECT COUNT(*) FROM users {where}", params)
        total = (await cur.fetchone())[0]
        params.extend([limit, page * limit])
        cur = await db.execute(
            f"""SELECT user_id, nickname, role, coins, streak, registration, last_claim
                FROM users {where} ORDER BY registration DESC LIMIT ? OFFSET ?""",
            params,
        )
        rows = await cur.fetchall()
        users = []
        for row in rows:
            r = ROLES.get(row["role"] or "user", ROLES["user"])
            users.append({
                "user_id": row["user_id"],
                "nickname": row["nickname"] or f"User{row['user_id']}",
                "role": row["role"] or "user",
                "role_display": f"{r['icon']} {r['name']}",
                "coins": row["coins"] or 0,
                "streak": row["streak"] or 0,
                "registration": row["registration"] or 0,
                "last_claim": row["last_claim"] or 0,
            })
        return {"total": total, "page": page, "limit": limit, "users": users}
    finally:
        await db.close()


class UserUpdateBody(BaseModel):
    nickname: Optional[str] = Field(None, min_length=1, max_length=32)
    role: Optional[str] = None
    coins: Optional[int] = None
    streak: Optional[int] = None
    reset_cooldown: Optional[bool] = None


@app.patch("/api/admin/users/{target_id}")
async def api_admin_user_update(
    target_id: int, body: UserUpdateBody, x_telegram_init_data: Optional[str] = Header(None)
):
    tg_user = get_user_from_header(x_telegram_init_data)
    db = await get_db()
    try:
        admin = await require_admin(db, tg_user)
        admin_role = admin["role"] or "user"
        cur = await db.execute("SELECT user_id, role FROM users WHERE user_id = ?", (target_id,))
        target = await cur.fetchone()
        if not target:
            raise HTTPException(404, "Пользователь не найден")
        if body.role is not None:
            if body.role not in ROLES:
                raise HTTPException(400, "Неизвестная роль")
            if admin_role != "superadmin":
                raise HTTPException(403, "Только superadmin может менять роли")
        updates, params = [], []
        if body.nickname is not None:
            updates.append("nickname = ?")
            params.append(body.nickname.strip()[:32])
        if body.role is not None:
            updates.append("role = ?")
            params.append(body.role)
        if body.coins is not None:
            if body.coins < 0:
                raise HTTPException(400, "Монеты не могут быть отрицательными")
            updates.append("coins = ?")
            params.append(body.coins)
        if body.streak is not None:
            if body.streak < 0:
                raise HTTPException(400, "Стрик не может быть отрицательным")
            updates.append("streak = ?")
            params.append(body.streak)
        if body.reset_cooldown:
            updates.append("last_claim = 0")
        if not updates:
            raise HTTPException(400, "Нечего обновлять")
        params.append(target_id)
        await db.execute(f"UPDATE users SET {', '.join(updates)} WHERE user_id = ?", params)
        await db.commit()
        return {"ok": True, "user_id": target_id}
    finally:
        await db.close()


class GiveCardBody(BaseModel):
    card_id: int


@app.post("/api/admin/users/{target_id}/give-card")
async def api_admin_give_card(
    target_id: int, body: GiveCardBody, x_telegram_init_data: Optional[str] = Header(None)
):
    tg_user = get_user_from_header(x_telegram_init_data)
    db = await get_db()
    try:
        await require_admin(db, tg_user)
        cur = await db.execute("SELECT id, name, rarity FROM cards WHERE id = ?", (body.card_id,))
        card = await cur.fetchone()
        if not card:
            raise HTTPException(404, "Карточка не найдена")
        cur = await db.execute("SELECT user_id FROM users WHERE user_id = ?", (target_id,))
        if not await cur.fetchone():
            raise HTTPException(404, "Пользователь не найден")
        now = int(time.time())
        await db.execute(
            """INSERT INTO inventory (user_id, card_id, claim_time) VALUES (?, ?, ?)
               ON CONFLICT(user_id, card_id) DO UPDATE SET claim_time = ?""",
            (target_id, body.card_id, now, now),
        )
        await db.commit()
        r = RARITIES.get(card["rarity"], {})
        return {
            "ok": True,
            "card": {
                "id": card["id"],
                "name": card["name"],
                "rarity": card["rarity"],
                "rarity_icon": r.get("icon", ""),
            },
        }
    finally:
        await db.close()


@app.get("/api/help")
async def api_help():
    return {
        "title": "Помощь — Мряу",
        "sections": [
            {
                "title": "🃏 Как получить карточку",
                "text": "Напиши «мряу» боту или нажми «Получить» в мини-приложении.",
            },
            {
                "title": "🔥 Стрик",
                "text": "Получай карточку каждый день. Пропуск > 24 ч — сброс.",
            },
            {
                "title": "🪙 Монеты",
                "text": "Обычная +8 · Редкая +20 · Необычная +40 · Ценная +70 · Сверхценная +110 · Легендарная +200",
            },
            {
                "title": "📊 Редкости",
                "text": "⚪ Обычная · 🔵 Редкая · 🟢 Необычная · 🟣 Ценная · 🟠 Сверхценная · 🟡 Легендарная",
            },
        ],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=int(os.getenv("API_PORT", "8080")), reload=False)
