"""
API для Telegram Mini App «Мряу».

Запуск (на том же сервере, где бот и БД):
    pip install fastapi uvicorn python-multipart aiosqlite
    uvicorn api:app --host 0.0.0.0 --port 8080

Переменные окружения:
    BOT_TOKEN   — токен бота (обязательно, для проверки initData)
    DB_NAME     — путь к SQLite (по умолчанию /app/data/cards_game.db)
    CORS_ORIGINS — через запятую, например https://meow.vercel.app
    CARDS_PHOTO_DIR — папка с фото карточек (опционально)
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
    "common": {"icon": "⚪", "name": "Обычная", "reward": 10, "weight": 50},
    "rare": {"icon": "🔵", "name": "Редкая", "reward": 25, "weight": 25},
    "epic": {"icon": "🟣", "name": "Эпическая", "reward": 50, "weight": 15},
    "mythical": {"icon": "🔴", "name": "Мифическая", "reward": 75, "weight": 7},
    "legendary": {"icon": "🟡", "name": "Легендарная", "reward": 100, "weight": 3},
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

app = FastAPI(title="Мряу Mini App API", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS + ["*"],
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


# ── Фото карточек ──
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

    cid = int(card_id)
    for d in _photo_search_dirs():
        if not d.exists():
            continue
        for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            candidate = d / f"{cid}{ext}"
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        try:
            for f in d.glob(f"{cid}.*"):
                if f.is_file() and f.stat().st_size > 0:
                    return f
        except Exception:
            pass
    return None


# ── БД ──
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


async def require_admin(db: aiosqlite.Connection, tg_user: dict) -> aiosqlite.Row:
    user = await ensure_user(db, tg_user)
    role = user["role"] or "user"
    if role not in ("admin", "superadmin"):
        raise HTTPException(403, "Недостаточно прав")
    return user


async def require_superadmin(db: aiosqlite.Connection, tg_user: dict) -> aiosqlite.Row:
    user = await ensure_user(db, tg_user)
    role = user["role"] or "user"
    if role != "superadmin":
        raise HTTPException(403, "Только главный администратор")
    return user


async def ensure_photo_path_column(db: aiosqlite.Connection) -> None:
    try:
        await db.execute("ALTER TABLE cards ADD COLUMN photo_path TEXT")
        await db.commit()
    except Exception:
        pass


def _day_key(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _calc_streak(last_streak_date: int, current_streak: int, now: int) -> tuple[int, int]:
    """Возвращает (new_streak, new_last_streak_date). last_streak_date — unix ts последнего claim-дня."""
    today = _day_key(now)
    if not last_streak_date:
        return 1, now
    last_day = _day_key(int(last_streak_date))
    if last_day == today:
        return max(current_streak, 1), last_streak_date
    # вчера?
    yesterday_ts = now - 86400
    if _day_key(yesterday_ts) == last_day or last_day == _day_key(now - 86400):
        return (current_streak or 0) + 1, now
    return 1, now


# ── Эндпоинты ──

@app.get("/api/card/{card_id}/photo")
async def api_card_photo(card_id: int):
    """Публичная раздача фото карточки (без initData)."""
    db = await get_db()
    try:
        await ensure_photo_path_column(db)
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


@app.get("/api/debug/photos")
async def api_debug_photos():
    dirs_info = []
    for d in _photo_search_dirs():
        files = []
        exists = d.exists()
        if exists:
            try:
                files = sorted([f.name for f in d.iterdir() if f.is_file()])[:50]
            except Exception as e:
                files = [f"error: {e}"]
        dirs_info.append({"dir": str(d), "exists": exists, "files": files})
    return {"db_name": DB_NAME, "dirs": dirs_info}


@app.get("/health")
async def health():
    return {"ok": True, "ts": int(time.time()), "version": "2.0"}


@app.get("/api/me")
async def api_me(x_telegram_init_data: Optional[str] = Header(None)):
    tg_user = get_user_from_header(x_telegram_init_data)
    user_id = tg_user["id"]

    db = await get_db()
    try:
        user = await ensure_user(db, tg_user)

        if (user["role"] or "user") == "banned":
            raise HTTPException(403, "Вы заблокированы")

        # уникальные карточки (без amount)
        cur = await db.execute(
            "SELECT COUNT(DISTINCT card_id) FROM inventory WHERE user_id = ?",
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
        await ensure_photo_path_column(db)

        # статистика по уникальным карточкам (без amount)
        cur = await db.execute(
            """SELECT c.rarity, COUNT(DISTINCT i.card_id) AS cnt
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

        sql = """
            SELECT c.id, c.name, c.rarity, c.photo_id, c.photo_path,
                   i.amount, i.claim_time
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
            # уникальные карточки, не сумма amount
            cur = await db.execute(
                """SELECT u.user_id, u.nickname,
                          COALESCE(COUNT(DISTINCT i.card_id), 0) AS value
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
                """SELECT COALESCE(COUNT(DISTINCT card_id), 0)
                   FROM inventory WHERE user_id = ?""",
                (user_id,),
            )
            my_value = (await cur.fetchone())[0]
            cur = await db.execute(
                """SELECT COUNT(*) + 1 FROM (
                     SELECT u.user_id, COALESCE(COUNT(DISTINCT i.card_id), 0) AS value
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


@app.get("/api/card/{card_id}")
async def api_card(
    card_id: int,
    x_telegram_init_data: Optional[str] = Header(None),
):
    tg_user = get_user_from_header(x_telegram_init_data)
    user_id = tg_user["id"]

    db = await get_db()
    try:
        await ensure_photo_path_column(db)
        cur = await db.execute(
            "SELECT id, name, rarity, photo_id, photo_path FROM cards WHERE id = ?",
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

        try:
            _pp = card["photo_path"]
        except (KeyError, IndexError, TypeError):
            _pp = None
        has_photo = resolve_card_photo_file(card["id"], _pp) is not None
        return {
            "id": card["id"],
            "name": card["name"],
            "rarity": card["rarity"],
            "rarity_icon": r.get("icon", ""),
            "rarity_name": r.get("name", card["rarity"]),
            "reward": r.get("reward", 0),
            "photo_id": card["photo_id"],
            "photo_url": card_photo_url(card["id"]),
            "has_photo": has_photo,
            "owned": inv is not None,
            "claim_time": inv["claim_time"] if inv else None,
        }
    finally:
        await db.close()


# ── Уведомление в чат бота (опционально) ──
async def notify_user_claim(user_id: int, text: str) -> None:
    """Шлёт текст через Bot API. Ошибки (блок и т.п.) игнорируются."""
    if not BOT_TOKEN:
        return
    try:
        import urllib.request

        payload = json.dumps(
            {
                "chat_id": user_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
        ).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        pass


# ── Получение карточки ──

@app.post("/api/claim")
async def api_claim(x_telegram_init_data: Optional[str] = Header(None)):
    """Бесплатное получение карточки (кулдаун 4 часа)."""
    tg_user = get_user_from_header(x_telegram_init_data)
    user_id = tg_user["id"]
    now = int(time.time())

    db = await get_db()
    try:
        user = await ensure_user(db, tg_user)
        if (user["role"] or "user") == "banned":
            raise HTTPException(403, "Вы заблокированы")

        last_claim = user["last_claim"] or 0
        remaining = max(0, COOLDOWN_SECONDS - (now - last_claim)) if last_claim else 0
        if remaining > 0:
            raise HTTPException(
                400,
                f"Кулдаун ещё не прошёл. Осталось {remaining // 3600} ч {(remaining % 3600) // 60} мин",
            )

        # список карточек по редкостям
        rarity_cards: dict[str, list] = {k: [] for k in RARITIES}
        cur = await db.execute("SELECT id, name, rarity, photo_id, photo_path FROM cards")
        all_cards = await cur.fetchall()
        if not all_cards:
            raise HTTPException(400, "В игре пока нет карточек")

        for c in all_cards:
            if c["rarity"] in rarity_cards:
                rarity_cards[c["rarity"]].append(c)

        # взвешенный выбор редкости среди тех, где есть карточки
        pool = []
        weights = []
        for key, info in RARITIES.items():
            if rarity_cards[key]:
                pool.append(key)
                weights.append(info["weight"])

        if not pool:
            raise HTTPException(400, "Нет доступных карточек")

        chosen_rarity = random.choices(pool, weights=weights, k=1)[0]
        card = random.choice(rarity_cards[chosen_rarity])
        r_info = RARITIES[chosen_rarity]
        reward = r_info["reward"]

        # стрик
        last_streak_date = user["last_streak_date"] or 0
        current_streak = user["streak"] or 0
        new_streak, new_streak_date = _calc_streak(last_streak_date, current_streak, now)

        # inventory
        await db.execute(
            """INSERT INTO inventory (user_id, card_id, claim_time, amount)
               VALUES (?, ?, ?, 1)
               ON CONFLICT(user_id, card_id) DO UPDATE SET
                 amount = amount + 1,
                 claim_time = ?""",
            (user_id, card["id"], now, now),
        )
        await db.execute(
            """UPDATE users SET
                 last_claim = ?,
                 coins = COALESCE(coins, 0) + ?,
                 streak = ?,
                 last_streak_date = ?
               WHERE user_id = ?""",
            (now, reward, new_streak, new_streak_date, user_id),
        )
        await db.commit()

        cur = await db.execute(
            "SELECT coins, streak FROM users WHERE user_id = ?", (user_id,)
        )
        urow = await cur.fetchone()
        new_coins = (urow["coins"] or 0) if urow else 0
        new_streak_val = (urow["streak"] or 0) if urow else new_streak

        # была ли уже эта карточка
        cur = await db.execute(
            "SELECT amount FROM inventory WHERE user_id = ? AND card_id = ?",
            (user_id, card["id"]),
        )
        inv = await cur.fetchone()
        amount = inv["amount"] if inv else 1
        is_new = amount <= 1

        try:
            _pp = card["photo_path"]
        except (KeyError, IndexError, TypeError):
            _pp = None
        has_photo = resolve_card_photo_file(card["id"], _pp) is not None

        # уведомление в чат бота (если пользователь не блокировал бота)
        new_mark = "✨ <b>Новая!</b>\n" if is_new else ""
        streak_line = f"🔥 Стрик: <b>{new_streak_val}</b>"
        if new_streak_val > current_streak:
            streak_line += " (+1)"
        msg = (
            f"🃏 Карточка из мини-приложения\n"
            f"{new_mark}"
            f"{r_info['icon']} <b>{card['name']}</b> — {r_info['name']}\n"
            f"+{reward} 🪙 · {streak_line}"
        )
        await notify_user_claim(user_id, msg)

        return {
            "ok": True,
            "card": {
                "id": card["id"],
                "name": card["name"],
                "rarity": chosen_rarity,
                "rarity_icon": r_info["icon"],
                "rarity_name": r_info["name"],
                "reward": reward,
                "photo_id": card["photo_id"],
                "photo_url": card_photo_url(card["id"]),
                "has_photo": has_photo,
            },
            "is_new": is_new,
            "reward_coins": reward,
            "coins": new_coins,
            "streak": new_streak_val,
            "streak_increased": new_streak_val > current_streak,
            "cooldown_seconds": COOLDOWN_SECONDS,
            "cooldown_remaining": COOLDOWN_SECONDS,
            "can_claim_free": False,
            "last_claim": now,
        }
    finally:
        await db.close()


# ── Админ: обзор ──

@app.get("/api/admin/overview")
async def api_admin_overview(x_telegram_init_data: Optional[str] = Header(None)):
    tg_user = get_user_from_header(x_telegram_init_data)
    db = await get_db()
    try:
        await require_admin(db, tg_user)

        cur = await db.execute("SELECT COUNT(*) FROM users")
        users_total = (await cur.fetchone())[0]

        cur = await db.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'banned'"
        )
        banned = (await cur.fetchone())[0]

        cur = await db.execute("SELECT COUNT(*) FROM cards")
        cards_total = (await cur.fetchone())[0]

        cur = await db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM inventory"
        )
        inventory_total = (await cur.fetchone())[0]

        cur = await db.execute(
            "SELECT COALESCE(SUM(coins), 0) FROM users WHERE role != 'banned'"
        )
        row = await cur.fetchone()
        coins_sum = row[0] or 0

        cur = await db.execute(
            """SELECT i.user_id, u.nickname, i.card_id, c.name AS card_name,
                      c.rarity, i.amount, i.claim_time
               FROM inventory i
               JOIN users u ON u.user_id = i.user_id
               JOIN cards c ON c.id = i.card_id
               ORDER BY i.claim_time DESC
               LIMIT 40"""
        )
        recent_rows = await cur.fetchall()
        recent = []
        for r in recent_rows:
            ri = RARITIES.get(r["rarity"], {})
            recent.append({
                "user_id": r["user_id"],
                "nickname": r["nickname"] or f"User{r['user_id']}",
                "card_id": r["card_id"],
                "card_name": r["card_name"],
                "rarity": r["rarity"],
                "rarity_icon": ri.get("icon", ""),
                "amount": r["amount"] or 1,
                "claim_time": r["claim_time"],
            })

        return {
            "stats": {
                "users_total": users_total,
                "banned": banned,
                "cards_total": cards_total,
                "inventory_total": inventory_total,
                "coins_sum": coins_sum,
            },
            "recent": recent,
        }
    finally:
        await db.close()


# ── Админ: карточки CRUD ──

class CardCreateBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    rarity: str
    photo_id: Optional[str] = None
    photo_path: Optional[str] = None


class CardUpdateBody(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    rarity: Optional[str] = None
    photo_id: Optional[str] = None
    photo_path: Optional[str] = None


@app.get("/api/admin/cards")
async def api_admin_cards(
    rarity: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    page: int = Query(0, ge=0),
    limit: int = Query(30, ge=1, le=100),
    x_telegram_init_data: Optional[str] = Header(None),
):
    tg_user = get_user_from_header(x_telegram_init_data)
    db = await get_db()
    try:
        await require_admin(db, tg_user)
        await ensure_photo_path_column(db)

        where = []
        params: list = []
        if rarity and rarity in RARITIES:
            where.append("rarity = ?")
            params.append(rarity)
        if q and q.strip():
            where.append("(name LIKE ? OR CAST(id AS TEXT) = ?)")
            params.append(f"%{q.strip()}%")
            params.append(q.strip())

        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        cur = await db.execute(f"SELECT COUNT(*) FROM cards{where_sql}", params)
        total = (await cur.fetchone())[0]

        cur = await db.execute(
            f"""SELECT id, name, rarity, photo_id, photo_path
                FROM cards{where_sql}
                ORDER BY id DESC
                LIMIT ? OFFSET ?""",
            params + [limit, page * limit],
        )
        rows = await cur.fetchall()
        cards = []
        for row in rows:
            r = RARITIES.get(row["rarity"], {})
            pp = row["photo_path"] if "photo_path" in row.keys() else None
            cards.append({
                "id": row["id"],
                "name": row["name"],
                "rarity": row["rarity"],
                "rarity_icon": r.get("icon", ""),
                "rarity_name": r.get("name", row["rarity"]),
                "photo_id": row["photo_id"],
                "photo_path": pp,
                "photo_url": card_photo_url(row["id"]),
                "has_photo": resolve_card_photo_file(row["id"], pp) is not None,
            })

        return {
            "total": total,
            "page": page,
            "limit": limit,
            "cards": cards,
        }
    finally:
        await db.close()


@app.post("/api/admin/cards")
async def api_admin_card_create(
    body: CardCreateBody,
    x_telegram_init_data: Optional[str] = Header(None),
):
    tg_user = get_user_from_header(x_telegram_init_data)
    if body.rarity not in RARITIES:
        raise HTTPException(400, "Неизвестная редкость")

    db = await get_db()
    try:
        await require_admin(db, tg_user)
        await ensure_photo_path_column(db)

        await db.execute(
            "INSERT INTO cards (name, rarity, photo_id, photo_path) VALUES (?, ?, ?, ?)",
            (body.name.strip(), body.rarity, body.photo_id, body.photo_path),
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid()")
        card_id = (await cur.fetchone())[0]

        r = RARITIES[body.rarity]
        return {
            "ok": True,
            "card": {
                "id": card_id,
                "name": body.name.strip(),
                "rarity": body.rarity,
                "rarity_icon": r["icon"],
                "rarity_name": r["name"],
                "photo_id": body.photo_id,
                "photo_path": body.photo_path,
                "photo_url": card_photo_url(card_id),
            },
        }
    finally:
        await db.close()


@app.patch("/api/admin/cards/{card_id}")
async def api_admin_card_update(
    card_id: int,
    body: CardUpdateBody,
    x_telegram_init_data: Optional[str] = Header(None),
):
    tg_user = get_user_from_header(x_telegram_init_data)
    db = await get_db()
    try:
        await require_admin(db, tg_user)
        await ensure_photo_path_column(db)

        cur = await db.execute("SELECT id FROM cards WHERE id = ?", (card_id,))
        if not await cur.fetchone():
            raise HTTPException(404, "Карточка не найдена")

        updates = []
        params: list = []
        if body.name is not None:
            updates.append("name = ?")
            params.append(body.name.strip())
        if body.rarity is not None:
            if body.rarity not in RARITIES:
                raise HTTPException(400, "Неизвестная редкость")
            updates.append("rarity = ?")
            params.append(body.rarity)
        if body.photo_id is not None:
            updates.append("photo_id = ?")
            params.append(body.photo_id or None)
        if body.photo_path is not None:
            updates.append("photo_path = ?")
            params.append(body.photo_path or None)

        if not updates:
            raise HTTPException(400, "Нечего обновлять")

        params.append(card_id)
        await db.execute(
            f"UPDATE cards SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        await db.commit()

        cur = await db.execute(
            "SELECT id, name, rarity, photo_id, photo_path FROM cards WHERE id = ?",
            (card_id,),
        )
        row = await cur.fetchone()
        r = RARITIES.get(row["rarity"], {})
        return {
            "ok": True,
            "card": {
                "id": row["id"],
                "name": row["name"],
                "rarity": row["rarity"],
                "rarity_icon": r.get("icon", ""),
                "rarity_name": r.get("name", row["rarity"]),
                "photo_id": row["photo_id"],
                "photo_path": row["photo_path"],
                "photo_url": card_photo_url(row["id"]),
            },
        }
    finally:
        await db.close()


@app.delete("/api/admin/cards/{card_id}")
async def api_admin_card_delete(
    card_id: int,
    x_telegram_init_data: Optional[str] = Header(None),
):
    tg_user = get_user_from_header(x_telegram_init_data)
    db = await get_db()
    try:
        await require_admin(db, tg_user)
        cur = await db.execute("SELECT id FROM cards WHERE id = ?", (card_id,))
        if not await cur.fetchone():
            raise HTTPException(404, "Карточка не найдена")

        await db.execute("DELETE FROM inventory WHERE card_id = ?", (card_id,))
        await db.execute("DELETE FROM cards WHERE id = ?", (card_id,))
        await db.commit()
        return {"ok": True, "deleted_id": card_id}
    finally:
        await db.close()


# ── Админ: пользователи ──

@app.get("/api/admin/users")
async def api_admin_users(
    q: Optional[str] = Query(None),
    role: Optional[str] = Query(None),
    page: int = Query(0, ge=0),
    limit: int = Query(30, ge=1, le=100),
    x_telegram_init_data: Optional[str] = Header(None),
):
    tg_user = get_user_from_header(x_telegram_init_data)
    db = await get_db()
    try:
        await require_admin(db, tg_user)

        where = []
        params: list = []
        if role and role in ROLES:
            where.append("role = ?")
            params.append(role)
        if q and q.strip():
            where.append(
                "(nickname LIKE ? OR CAST(user_id AS TEXT) = ? OR username LIKE ?)"
            )
            qs = f"%{q.strip()}%"
            params.extend([qs, q.strip(), qs])

        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        # username может отсутствовать в таблице — пробуем без него
        try:
            cur = await db.execute(f"SELECT COUNT(*) FROM users{where_sql}", params)
            total = (await cur.fetchone())[0]
        except Exception:
            # fallback без username
            where2 = [w for w in where if "username" not in w]
            params2 = params[: len(where2) * (2 if q else 1)] if False else []
            where = []
            params = []
            if role and role in ROLES:
                where.append("role = ?")
                params.append(role)
            if q and q.strip():
                where.append("(nickname LIKE ? OR CAST(user_id AS TEXT) = ?)")
                params.append(f"%{q.strip()}%")
                params.append(q.strip())
            where_sql = (" WHERE " + " AND ".join(where)) if where else ""
            cur = await db.execute(f"SELECT COUNT(*) FROM users{where_sql}", params)
            total = (await cur.fetchone())[0]

        cur = await db.execute(
            f"""SELECT user_id, nickname, role, gender, coins, streak,
                       registration, last_claim, last_streak_date
                FROM users{where_sql}
                ORDER BY registration DESC
                LIMIT ? OFFSET ?""",
            params + [limit, page * limit],
        )
        rows = await cur.fetchall()
        users = []
        for row in rows:
            r = ROLES.get(row["role"] or "user", ROLES["user"])
            g = GENDERS.get(row["gender"] or "none", GENDERS["none"])
            users.append({
                "user_id": row["user_id"],
                "nickname": row["nickname"] or f"User{row['user_id']}",
                "role": row["role"] or "user",
                "role_display": f"{r['icon']} {r['name']}",
                "gender": row["gender"] or "none",
                "gender_display": f"{g['icon']} {g['name']}",
                "coins": row["coins"] or 0,
                "streak": row["streak"] or 0,
                "registration": row["registration"] or 0,
                "registration_str": (
                    datetime.fromtimestamp(row["registration"]).strftime("%d.%m.%Y %H:%M")
                    if row["registration"]
                    else "—"
                ),
                "last_claim": row["last_claim"] or 0,
            })

        return {"total": total, "page": page, "limit": limit, "users": users}
    finally:
        await db.close()


@app.get("/api/admin/users/{target_id}")
async def api_admin_user_detail(
    target_id: int,
    x_telegram_init_data: Optional[str] = Header(None),
):
    tg_user = get_user_from_header(x_telegram_init_data)
    db = await get_db()
    try:
        await require_admin(db, tg_user)

        cur = await db.execute(
            "SELECT * FROM users WHERE user_id = ?", (target_id,)
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "Пользователь не найден")

        role = row["role"] or "user"
        gender = row["gender"] or "none"
        r = ROLES.get(role, ROLES["user"])
        g = GENDERS.get(gender, GENDERS["none"])

        cur = await db.execute(
            "SELECT COUNT(DISTINCT card_id) FROM inventory WHERE user_id = ?",
            (target_id,),
        )
        cards_unique = (await cur.fetchone())[0]

        cur = await db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM inventory WHERE user_id = ?",
            (target_id,),
        )
        cards_total_amt = (await cur.fetchone())[0]

        cur = await db.execute(
            """SELECT c.id, c.name, c.rarity, i.amount, i.claim_time
               FROM inventory i
               JOIN cards c ON c.id = i.card_id
               WHERE i.user_id = ?
               ORDER BY i.claim_time DESC
               LIMIT 50""",
            (target_id,),
        )
        inv_rows = await cur.fetchall()
        inventory = []
        for ir in inv_rows:
            ri = RARITIES.get(ir["rarity"], {})
            inventory.append({
                "id": ir["id"],
                "name": ir["name"],
                "rarity": ir["rarity"],
                "rarity_icon": ri.get("icon", ""),
                "amount": ir["amount"] or 1,
                "claim_time": ir["claim_time"],
            })

        reg = row["registration"] or 0
        return {
            "user_id": target_id,
            "nickname": row["nickname"] or f"User{target_id}",
            "role": role,
            "role_display": f"{r['icon']} {r['name']}",
            "gender": gender,
            "gender_display": f"{g['icon']} {g['name']}",
            "coins": row["coins"] or 0,
            "streak": row["streak"] or 0,
            "registration": reg,
            "registration_str": (
                datetime.fromtimestamp(reg).strftime("%d.%m.%Y %H:%M") if reg else "—"
            ),
            "days_with_us": max(0, (int(time.time()) - reg) // 86400) if reg else 0,
            "last_claim": row["last_claim"] or 0,
            "last_streak_date": row["last_streak_date"] or 0,
            "cards_unique": cards_unique,
            "cards_total_amount": cards_total_amt,
            "inventory": inventory,
        }
    finally:
        await db.close()


class UserUpdateBody(BaseModel):
    nickname: Optional[str] = Field(None, min_length=1, max_length=32)
    role: Optional[str] = None
    coins: Optional[int] = None
    streak: Optional[int] = None
    gender: Optional[str] = None
    reset_cooldown: Optional[bool] = None


@app.patch("/api/admin/users/{target_id}")
async def api_admin_user_update(
    target_id: int,
    body: UserUpdateBody,
    x_telegram_init_data: Optional[str] = Header(None),
):
    tg_user = get_user_from_header(x_telegram_init_data)
    db = await get_db()
    try:
        admin = await require_admin(db, tg_user)
        admin_role = admin["role"] or "user"

        cur = await db.execute(
            "SELECT user_id, role FROM users WHERE user_id = ?", (target_id,)
        )
        target = await cur.fetchone()
        if not target:
            raise HTTPException(404, "Пользователь не найден")

        target_role = target["role"] or "user"

        # только superadmin может трогать admin/superadmin и менять роли
        if body.role is not None:
            if body.role not in ROLES:
                raise HTTPException(400, "Неизвестная роль")
            if admin_role != "superadmin":
                raise HTTPException(403, "Только superadmin может менять роли")
            if target_role == "superadmin" and body.role != "superadmin":
                raise HTTPException(403, "Нельзя снять superadmin")

        if target_role in ("admin", "superadmin") and admin_role != "superadmin":
            if body.coins is not None or body.streak is not None or body.reset_cooldown:
                raise HTTPException(403, "Нельзя изменять администраторов")

        updates = []
        params: list = []
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
        if body.gender is not None:
            if body.gender not in GENDERS:
                raise HTTPException(400, "Неизвестный пол")
            updates.append("gender = ?")
            params.append(body.gender)
        if body.reset_cooldown:
            updates.append("last_claim = 0")

        if not updates:
            raise HTTPException(400, "Нечего обновлять")

        params.append(target_id)
        await db.execute(
            f"UPDATE users SET {', '.join(updates)} WHERE user_id = ?",
            params,
        )
        await db.commit()
        return {"ok": True, "user_id": target_id}
    finally:
        await db.close()


class GiveCardBody(BaseModel):
    card_id: int
    amount: int = Field(1, ge=1, le=100)


@app.post("/api/admin/users/{target_id}/give-card")
async def api_admin_give_card(
    target_id: int,
    body: GiveCardBody,
    x_telegram_init_data: Optional[str] = Header(None),
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
            """INSERT INTO inventory (user_id, card_id, claim_time, amount)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id, card_id) DO UPDATE SET
                 amount = amount + ?,
                 claim_time = ?""",
            (target_id, body.card_id, now, body.amount, body.amount, now),
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
            "amount": body.amount,
        }
    finally:
        await db.close()


@app.get("/api/help")
async def api_help():
    """Публичная справка (без auth)."""
    return {
        "title": "Помощь — Мряу",
        "sections": [
            {
                "title": "🃏 Как получить карточку",
                "text": "Напиши «мряу» в чате с ботом или нажми «Получить карточку» в мини-приложении. Кулдаун — 4 часа.",
            },
            {
                "title": "🔥 Стрик",
                "text": "Получай карточку каждый день, чтобы наращивать стрик. Если пропустишь день — стрик сбросится.",
            },
            {
                "title": "🪙 Монеты",
                "text": "За каждую карточку начисляются монеты в зависимости от редкости: обычная +10, редкая +25, эпическая +50, мифическая +75, легендарная +100.",
            },
            {
                "title": "🎲 Кубик",
                "text": "Напиши «мряу кубик» в боте, чтобы бросить кубик и получить бонус.",
            },
            {
                "title": "📊 Редкости",
                "text": "⚪ Обычная · 🔵 Редкая · 🟣 Эпическая · 🔴 Мифическая · 🟡 Легендарная",
            },
            {
                "title": "📱 Мини-приложение",
                "text": "Карточки — твоя коллекция. Получить — бесплатная карточка. Топ — рейтинг игроков. Профиль — статистика.",
            },
        ],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "api:app",
        host="0.0.0.0",
        port=int(os.getenv("API_PORT", "8080")),
        reload=False,
    )
