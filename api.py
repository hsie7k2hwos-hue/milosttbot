"""
API для Telegram Mini App «Мряу».

Запуск (на том же сервере, где бот и БД):
    pip install fastapi uvicorn python-multipart
    uvicorn api:app --host 0.0.0.0 --port 8080

Или рядом с ботом через systemd / docker.

Переменные окружения:
    BOT_TOKEN   — токен бота (обязательно, для проверки initData)
    DB_NAME     — путь к SQLite (по умолчанию /app/data/cards_game.db)
    CORS_ORIGINS — через запятую, например https://meow.vercel.app
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime
from typing import Any, Optional
from urllib.parse import parse_qsl

import aiosqlite
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── Конфиг ──
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

RARITIES = {
    "common": {"icon": "⚪", "name": "Обычная", "reward": 10},
    "rare": {"icon": "🔵", "name": "Редкая", "reward": 25},
    "epic": {"icon": "🟣", "name": "Эпическая", "reward": 50},
    "mythical": {"icon": "🔴", "name": "Мифическая", "reward": 75},
    "legendary": {"icon": "🟡", "name": "Легендарная", "reward": 100},
}

MARKET_PRICES = {
    "common": 1,
    "rare": 3,
    "epic": 8,
    "mythical": 20,
    "legendary": 50,
}

GENDERS = {
    "male": {"icon": "♂", "name": "Мужской"},
    "female": {"icon": "♀", "name": "Женский"},
    "other": {"icon": "⚧", "name": "Другой"},
    "none": {"icon": "—", "name": "Не задан"},
}

ROLES = {
    "user": {"icon": "👤", "name": "Пользователь"},
    "admin": {"icon": "🛡", "name": "Администратор"},
    "superadmin": {"icon": "👑", "name": "Главный администратор"},
    "banned": {"icon": "🚫", "name": "Заблокирован"},
}

COOLDOWN_SECONDS = 4 * 3600
GEM_TO_COINS = 100

app = FastAPI(title="Мряу Mini App API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS + ["*"],  # Telegram WebView может слать разные origin
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Проверка initData ──
def validate_init_data(init_data: str) -> dict[str, Any]:
    """Проверяет подпись Telegram WebApp initData. Возвращает распарсенные поля."""
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

    # auth_date не старше 24 часов
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


# ── БД ──
async def get_db():
    db = await aiosqlite.connect(DB_NAME)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON;")
    return db


async def ensure_user(db: aiosqlite.Connection, tg_user: dict) -> aiosqlite.Row:
    """Создаёт пользователя, если его ещё нет (как get_or_create_user в боте)."""
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

    cur = await db.execute("SELECT COUNT(*) FROM users")
    total = (await cur.fetchone())[0]
    role = "superadmin" if total == 0 else "user"

    await db.execute(
        "INSERT INTO users (user_id, nickname, registration, streak, last_streak_date, role) "
        "VALUES (?, ?, ?, 0, 0, ?)",
        (user_id, nickname, now, role),
    )
    await db.commit()
    cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    return await cur.fetchone()


# ── Эндпоинты ──

@app.get("/health")
async def health():
    return {"ok": True, "ts": int(time.time())}


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
            "SELECT COALESCE(SUM(amount), 0) FROM inventory WHERE user_id = ?",
            (user_id,),
        )
        cards_count = (await cur.fetchone())[0]

        cur = await db.execute("SELECT COUNT(*) FROM cards")
        total_cards = (await cur.fetchone())[0]

        role = user["role"] or "user"
        gender = user["gender"] or "none"
        g = GENDERS.get(gender, GENDERS["none"])
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
            "gender": gender,
            "gender_display": f"{g['icon']} {g['name']}",
            "coins": user["coins"] or 0,
            "gems": user["gems"] or 0,
            "streak": user["streak"] or 0,
            "cards_count": cards_count,
            "total_cards": total_cards,
            "registration": reg,
            "registration_str": datetime.fromtimestamp(reg).strftime("%d.%m.%Y") if reg else "—",
            "days_with_us": max(0, (now - reg) // 86400) if reg else 0,
            "last_claim": last_claim,
            "cooldown_remaining": remaining_cd,
            "can_claim_free": remaining_cd <= 0,
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

        # Статистика по редкостям
        cur = await db.execute(
            """SELECT c.rarity, COALESCE(SUM(i.amount), 0) AS cnt
               FROM inventory i
               JOIN cards c ON i.card_id = c.id
               WHERE i.user_id = ?
               GROUP BY c.rarity""",
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

        # Список карточек
        sql = """
            SELECT c.id, c.name, c.rarity, c.photo_id, i.amount, i.claim_time
            FROM inventory i
            JOIN cards c ON i.card_id = c.id
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
            cards.append({
                "id": row["id"],
                "name": row["name"],
                "rarity": row["rarity"],
                "rarity_icon": r.get("icon", ""),
                "rarity_name": r.get("name", row["rarity"]),
                "reward": r.get("reward", 0),
                "photo_id": row["photo_id"],
                "amount": row["amount"],
                "claim_time": row["claim_time"],
            })

        # Кол-во карточек каждой редкости в игре
        rarity_totals = {}
        for rk in RARITIES:
            cur = await db.execute(
                "SELECT COUNT(*) FROM cards WHERE rarity = ?", (rk,)
            )
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
                """SELECT u.user_id, u.nickname, COALESCE(SUM(i.amount), 0) AS value
                   FROM users u
                   LEFT JOIN inventory i ON u.user_id = i.user_id
                   WHERE u.role != 'banned'
                   GROUP BY u.user_id
                   ORDER BY value DESC, u.user_id ASC
                   LIMIT ?""",
                (limit,),
            )
            top = await cur.fetchall()
            cur = await db.execute(
                """SELECT COALESCE(SUM(amount), 0) FROM inventory WHERE user_id = ?""",
                (user_id,),
            )
            my_value = (await cur.fetchone())[0]
            cur = await db.execute(
                """SELECT COUNT(*) + 1 FROM (
                     SELECT u.user_id, COALESCE(SUM(i.amount), 0) AS value
                     FROM users u
                     LEFT JOIN inventory i ON u.user_id = i.user_id
                     WHERE u.role != 'banned'
                     GROUP BY u.user_id
                   ) WHERE value > ?""",
                (my_value,),
            )
            my_rank = (await cur.fetchone())[0]
            unit = "🃏"
            title = "🃏 Топ по карточкам"
        elif kind == "streak":
            cur = await db.execute(
                """SELECT user_id, nickname, streak AS value FROM users
                   WHERE role != 'banned'
                   ORDER BY value DESC, user_id ASC LIMIT ?""",
                (limit,),
            )
            top = await cur.fetchall()
            cur = await db.execute(
                "SELECT streak FROM users WHERE user_id = ?", (user_id,)
            )
            row = await cur.fetchone()
            my_value = (row["streak"] or 0) if row else 0
            cur = await db.execute(
                "SELECT COUNT(*) + 1 FROM users WHERE streak > ? AND role != 'banned'",
                (my_value,),
            )
            my_rank = (await cur.fetchone())[0]
            unit = "🔥"
            title = "🔥 Топ по стрику"
        else:
            cur = await db.execute(
                """SELECT user_id, nickname, coins AS value FROM users
                   WHERE role != 'banned'
                   ORDER BY value DESC, user_id ASC LIMIT ?""",
                (limit,),
            )
            top = await cur.fetchall()
            cur = await db.execute(
                "SELECT coins FROM users WHERE user_id = ?", (user_id,)
            )
            row = await cur.fetchone()
            my_value = (row["coins"] or 0) if row else 0
            cur = await db.execute(
                "SELECT COUNT(*) + 1 FROM users WHERE coins > ? AND role != 'banned'",
                (my_value,),
            )
            my_rank = (await cur.fetchone())[0]
            unit = "🪙"
            title = "🪙 Топ по монетам"

        cur = await db.execute(
            "SELECT nickname FROM users WHERE user_id = ?", (user_id,)
        )
        nick_row = await cur.fetchone()
        my_nick = (nick_row["nickname"] if nick_row else None) or f"User{user_id}"

        return {
            "kind": kind,
            "title": title,
            "unit": unit,
            "top": [
                {
                    "user_id": r["user_id"],
                    "nickname": r["nickname"] or f"User{r['user_id']}",
                    "value": r["value"] or 0,
                }
                for r in top
            ],
            "me": {
                "rank": my_rank,
                "nickname": my_nick,
                "value": my_value,
            },
        }
    finally:
        await db.close()


@app.get("/api/market")
async def api_market(x_telegram_init_data: Optional[str] = Header(None)):
    tg_user = get_user_from_header(x_telegram_init_data)
    user_id = tg_user["id"]

    db = await get_db()
    try:
        user = await ensure_user(db, tg_user)
        gems = user["gems"] or 0
        coins = user["coins"] or 0

        missing_by_rarity = {}
        for r_key in RARITIES:
            cur = await db.execute(
                """SELECT COUNT(*) FROM cards c
                   WHERE c.rarity = ?
                     AND c.id NOT IN (
                         SELECT card_id FROM inventory WHERE user_id = ?
                     )""",
                (r_key, user_id),
            )
            missing_by_rarity[r_key] = (await cur.fetchone())[0]

        rarities = []
        for key, info in RARITIES.items():
            missing = missing_by_rarity.get(key, 0)
            rarities.append({
                "key": key,
                "icon": info["icon"],
                "name": info["name"],
                "price": MARKET_PRICES.get(key, 0),
                "missing": missing,
                "collected": missing == 0,
            })

        return {
            "gems": gems,
            "coins": coins,
            "gem_to_coins": GEM_TO_COINS,
            "max_buy": coins // GEM_TO_COINS,
            "rarities": rarities,
        }
    finally:
        await db.close()


@app.get("/api/market/cards")
async def api_market_cards(
    rarity: str = Query(...),
    page: int = Query(0, ge=0),
    x_telegram_init_data: Optional[str] = Header(None),
):
    tg_user = get_user_from_header(x_telegram_init_data)
    user_id = tg_user["id"]

    if rarity not in RARITIES:
        raise HTTPException(400, "Неизвестная редкость")

    db = await get_db()
    try:
        user = await ensure_user(db, tg_user)
        gems = user["gems"] or 0
        price = MARKET_PRICES.get(rarity, 0)
        r_info = RARITIES[rarity]

        cur = await db.execute(
            """SELECT c.id, c.name, c.photo_id, c.rarity
               FROM cards c
               WHERE c.rarity = ?
                 AND c.id NOT IN (
                     SELECT card_id FROM inventory WHERE user_id = ?
                 )
               ORDER BY c.id ASC""",
            (rarity, user_id),
        )
        cards = await cur.fetchall()
        total = len(cards)

        if total == 0:
            return {
                "rarity": rarity,
                "rarity_name": r_info["name"],
                "rarity_icon": r_info["icon"],
                "price": price,
                "gems": gems,
                "total": 0,
                "page": 0,
                "card": None,
            }

        page = max(0, min(page, total - 1))
        card = cards[page]

        return {
            "rarity": rarity,
            "rarity_name": r_info["name"],
            "rarity_icon": r_info["icon"],
            "price": price,
            "gems": gems,
            "total": total,
            "page": page,
            "card": {
                "id": card["id"],
                "name": card["name"],
                "photo_id": card["photo_id"],
                "rarity": card["rarity"],
            },
        }
    finally:
        await db.close()


class BuyBody(BaseModel):
    card_id: int


@app.post("/api/market/buy")
async def api_market_buy(
    body: BuyBody,
    x_telegram_init_data: Optional[str] = Header(None),
):
    tg_user = get_user_from_header(x_telegram_init_data)
    user_id = tg_user["id"]
    card_id = body.card_id

    db = await get_db()
    try:
        await ensure_user(db, tg_user)

        cur = await db.execute(
            "SELECT id, name, rarity, photo_id FROM cards WHERE id = ?",
            (card_id,),
        )
        card = await cur.fetchone()
        if not card:
            raise HTTPException(404, "Карточка не найдена")

        rarity = card["rarity"]
        price = MARKET_PRICES.get(rarity, 0)
        if price <= 0:
            raise HTTPException(400, "Эту карточку нельзя купить")

        cur = await db.execute(
            "SELECT amount FROM inventory WHERE user_id = ? AND card_id = ?",
            (user_id, card_id),
        )
        if await cur.fetchone():
            raise HTTPException(400, "У вас уже есть эта карточка")

        cur = await db.execute(
            "SELECT gems FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        gems = (row["gems"] or 0) if row else 0
        if gems < price:
            raise HTTPException(400, f"Недостаточно кристаллов. Нужно {price} 💎")

        now = int(time.time())
        await db.execute(
            "UPDATE users SET gems = gems - ? WHERE user_id = ?",
            (price, user_id),
        )
        await db.execute(
            """INSERT INTO inventory (user_id, card_id, claim_time, amount)
               VALUES (?, ?, ?, 1)
               ON CONFLICT(user_id, card_id) DO UPDATE SET amount = amount + 1,
                                                           claim_time = ?""",
            (user_id, card_id, now, now),
        )
        await db.commit()

        cur = await db.execute(
            "SELECT gems FROM users WHERE user_id = ?", (user_id,)
        )
        new_gems = (await cur.fetchone())[0] or 0
        r_info = RARITIES.get(rarity, {})

        return {
            "ok": True,
            "card": {
                "id": card["id"],
                "name": card["name"],
                "rarity": rarity,
                "rarity_icon": r_info.get("icon", ""),
                "rarity_name": r_info.get("name", rarity),
                "photo_id": card["photo_id"],
            },
            "price": price,
            "gems": new_gems,
        }
    finally:
        await db.close()


class ExchangeBody(BaseModel):
    amount: int  # сколько кристаллов купить


@app.post("/api/market/exchange")
async def api_market_exchange(
    body: ExchangeBody,
    x_telegram_init_data: Optional[str] = Header(None),
):
    tg_user = get_user_from_header(x_telegram_init_data)
    user_id = tg_user["id"]
    amount = body.amount

    if amount <= 0 or amount > 10000:
        raise HTTPException(400, "Некорректное количество")

    cost = amount * GEM_TO_COINS
    db = await get_db()
    try:
        await ensure_user(db, tg_user)
        cur = await db.execute(
            "SELECT coins, gems FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        coins = (row["coins"] or 0) if row else 0
        if coins < cost:
            raise HTTPException(400, f"Недостаточно монет. Нужно {cost} 🪙")

        await db.execute(
            "UPDATE users SET coins = coins - ?, gems = gems + ? WHERE user_id = ?",
            (cost, amount, user_id),
        )
        await db.commit()

        cur = await db.execute(
            "SELECT coins, gems FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        return {
            "ok": True,
            "amount": amount,
            "cost": cost,
            "coins": row["coins"] or 0,
            "gems": row["gems"] or 0,
        }
    finally:
        await db.close()


@app.get("/api/card/{card_id}")
async def api_card(
    card_id: int,
    x_telegram_init_data: Optional[str] = Header(None),
):
    tg_user = get_user_from_header(x_telegram_init_data)
    user_id = tg_user["id"]

    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT id, name, rarity, photo_id FROM cards WHERE id = ?",
            (card_id,),
        )
        card = await cur.fetchone()
        if not card:
            raise HTTPException(404, "Карточка не найдена")

        cur = await db.execute(
            "SELECT amount, claim_time FROM inventory WHERE user_id = ? AND card_id = ?",
            (user_id, card_id),
        )
        inv = await cur.fetchone()
        r = RARITIES.get(card["rarity"], {})

        return {
            "id": card["id"],
            "name": card["name"],
            "rarity": card["rarity"],
            "rarity_icon": r.get("icon", ""),
            "rarity_name": r.get("name", card["rarity"]),
            "reward": r.get("reward", 0),
            "photo_id": card["photo_id"],
            "owned": inv is not None,
            "amount": inv["amount"] if inv else 0,
            "claim_time": inv["claim_time"] if inv else None,
        }
    finally:
        await db.close()


# Для локального запуска:
# uvicorn api:app --host 0.0.0.0 --port 8080 --reload
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api:app", host="0.0.0.0", port=int(os.getenv("API_PORT", "8080")), reload=False)
