"""
Игровая логика: выдача карточек, стрики, кубик.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta
from typing import Any, Optional

from config import (
    COOLDOWN_SECONDS,
    STREAK_EXPIRE_SECONDS,
    STREAK_BONUSES,
    DICE_MAP,
    DICE_MIN_BALANCE,
    DICE_COOLDOWN_SECONDS,
)
from db import (
    get_db,
    get_or_create_user,
    load_rarities,
    count_cards,
    count_user_cards,
    add_to_inventory,
    update_coins,
)
from utils import now_ts

logger = logging.getLogger("game")


def streak_bonus_for(streak: int) -> int:
    if streak <= 1:
        return 0
    for days, bonus in STREAK_BONUSES:
        if streak <= days:
            return bonus
    return 60


async def burn_expired_streak(user_id: int) -> bool:
    """Сжигает стрик, если прошло больше STREAK_EXPIRE_SECONDS с последнего клейма."""
    async with get_db() as db:
        cur = await db.execute(
            "SELECT streak, last_claim FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        if not row:
            return False
        streak = row["streak"] or 0
        last_claim = row["last_claim"] or 0
        if streak > 0 and last_claim > 0 and (now_ts() - last_claim) >= STREAK_EXPIRE_SECONDS:
            await db.execute(
                "UPDATE users SET streak = 0, last_streak_date = 0 WHERE user_id = ?",
                (user_id,),
            )
            return True
    return False


async def update_streak(user_id: int) -> tuple[int, int, int, bool]:
    """
    Обновляет стрик. Возвращает (new_streak, bonus, new_balance, was_updated).
    """
    await burn_expired_streak(user_id)
    now = datetime.now()
    today_start = int(datetime(now.year, now.month, now.day).timestamp())
    today_end = today_start + 86400 - 1
    yesterday_start = int(
        (now - timedelta(days=1)).replace(hour=0, minute=0, second=0).timestamp()
    )
    yesterday_end = yesterday_start + 86400 - 1
    ts = now_ts()

    async with get_db() as db:
        cur = await db.execute(
            "SELECT streak, last_streak_date, coins, last_claim FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        if not row:
            return 0, 0, 0, False

        streak = row["streak"] or 0
        last_date = row["last_streak_date"] or 0
        balance = row["coins"] or 0
        last_claim = row["last_claim"] or 0

        if last_claim > 0 and (ts - last_claim) >= STREAK_EXPIRE_SECONDS:
            if streak > 0:
                await db.execute(
                    "UPDATE users SET streak = 0, last_streak_date = 0 WHERE user_id = ?",
                    (user_id,),
                )
            return 0, 0, balance, False

        if today_start <= last_date <= today_end:
            return streak, 0, balance, False

        is_consecutive = streak > 0 and yesterday_start <= last_date <= yesterday_end
        new_streak = (streak + 1) if is_consecutive else 1
        bonus = streak_bonus_for(new_streak)

        await db.execute(
            """UPDATE users SET streak = ?, last_streak_date = ?, coins = coins + ?
               WHERE user_id = ?""",
            (new_streak, ts, bonus, user_id),
        )
        cur = await db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
        new_balance = (await cur.fetchone())[0]
        return new_streak, bonus, new_balance, True


async def issue_card(user_id: int) -> tuple[Optional[dict], str]:
    """
    Выдаёт новую (ещё не имеющуюся) карточку.
    Возвращает (card_dict, status).
    status: success | no_cards | all_collected | cooldown | error
    """
    try:
        await get_or_create_user(user_id)
        rarities = await load_rarities()
        total = await count_cards()
        if total == 0:
            return None, "no_cards"

        owned = await count_user_cards(user_id)
        if owned >= total:
            return None, "all_collected"

        # Проверка кулдауна
        async with get_db() as db:
            cur = await db.execute(
                "SELECT last_claim, coins FROM users WHERE user_id = ?", (user_id,)
            )
            row = await cur.fetchone()
            last_claim = row["last_claim"] or 0 if row else 0
            if now_ts() - last_claim < COOLDOWN_SECONDS:
                return None, "cooldown"

        # Выбор редкости по весам
        keys = list(rarities.keys())
        weights = [rarities[k]["weight"] for k in keys]
        card = None
        selected_rarity = None

        for _ in range(15):
            selected_rarity = random.choices(keys, weights=weights, k=1)[0]
            async with get_db() as db:
                cur = await db.execute(
                    """SELECT id, name, photo_id, photo_path, rarity
                       FROM cards
                       WHERE rarity = ?
                         AND id NOT IN (SELECT card_id FROM inventory WHERE user_id = ?)
                       ORDER BY RANDOM() LIMIT 1""",
                    (selected_rarity, user_id),
                )
                card = await cur.fetchone()
            if card:
                break

        if not card:
            async with get_db() as db:
                cur = await db.execute(
                    """SELECT id, name, photo_id, photo_path, rarity
                       FROM cards
                       WHERE id NOT IN (SELECT card_id FROM inventory WHERE user_id = ?)
                       ORDER BY RANDOM() LIMIT 1""",
                    (user_id,),
                )
                card = await cur.fetchone()
            if card:
                selected_rarity = card["rarity"]

        if not card:
            return None, "all_collected"

        reward = rarities.get(selected_rarity, rarities["common"])["reward"]
        new_balance = await update_coins(user_id, reward)

        async with get_db() as db:
            await db.execute(
                "UPDATE users SET last_claim = ? WHERE user_id = ?",
                (now_ts(), user_id),
            )

        await add_to_inventory(user_id, card["id"])

        # Для негативных — фактическая дельта может быть меньше
        actual_delta = reward
        if reward < 0:
            # update_coins уже ограничил до 0
            actual_delta = reward  # упрощённо

        return {
            "id": card["id"],
            "name": card["name"],
            "photo_id": card["photo_id"],
            "photo_path": card["photo_path"],
            "rarity": selected_rarity,
            "coins_earned": actual_delta,
            "balance": new_balance,
        }, "success"
    except Exception as e:
        logger.error("issue_card: %s", e)
        return None, "error"


async def play_dice(user_id: int) -> tuple[Optional[dict], str]:
    """
    Бросок кубика.
    Возвращает (result_dict, status).
    status: success | low_balance | cooldown | error
    """
    try:
        await get_or_create_user(user_id)
        ts = now_ts()

        async with get_db() as db:
            cur = await db.execute(
                "SELECT coins, last_dice FROM users WHERE user_id = ?", (user_id,)
            )
            row = await cur.fetchone()
            balance = (row["coins"] or 0) if row else 0
            last_dice = (row["last_dice"] or 0) if row else 0

            if balance < DICE_MIN_BALANCE:
                return None, "low_balance"
            if last_dice > 0 and (ts - last_dice) < DICE_COOLDOWN_SECONDS:
                return None, "cooldown"

            await db.execute(
                "UPDATE users SET last_dice = ? WHERE user_id = ?", (ts, user_id)
            )

        # Значение кости будет получено от Telegram (reply_dice)
        # Здесь только заготовка; реальное значение приходит в хендлере
        return {"balance_before": balance}, "ready"
    except Exception as e:
        logger.error("play_dice: %s", e)
        return None, "error"


async def apply_dice_result(user_id: int, dice_value: int) -> dict:
    delta = DICE_MAP.get(dice_value, 0)
    new_balance = await update_coins(user_id, delta)
    return {
        "value": dice_value,
        "delta": delta,
        "balance": new_balance,
    }
