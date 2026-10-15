"""
Хендлеры пользовательского бота.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

from config import (
    COOLDOWN_SECONDS,
    DICE_MIN_BALANCE,
    DICE_COOLDOWN_SECONDS,
    DICE_SPIN_DELAY,
    NICKNAME_COST,
    PARTY_EFFECT,
    GROUP_AUTODELETE,
    DEFAULT_RARITIES,
)
from db import (
    get_or_create_user,
    get_user,
    is_banned,
    count_cards,
    count_user_cards,
    get_user_rarity_stats,
    get_user_cards_by_rarity,
    set_nickname,
    update_coins,
    load_rarities,
)
from game import issue_card, update_streak, apply_dice_result, burn_expired_streak
from photos import get_card_photo_input
from utils import (
    esc, fmt_num, fmt_coins, fmt_days, fmt_remaining, line, bq, sep,
    display_name, validate_nickname, now_ts, plural,
)
from user_bot.keyboards import (
    main_kb, profile_kb, after_card_kb, top_kb, ok_kb, help_kb,
    nick_confirm_kb, CollectionCB, MainMenuCB, BackProfileCB,
    TopCB, TopRefreshCB, NickConfirmCB, OkDelCB, HelpCB, CardActionCB,
)

logger = logging.getLogger("user_bot")
router = Router()

# ── Rate limit ──────────────────────────────────────────────────────────────
_rate: dict[str, list[float]] = {}


def rate_limited(key: str, limit: int, window: float) -> bool:
    import time
    now = time.monotonic()
    bucket = _rate.setdefault(key, [])
    bucket[:] = [t for t in bucket if now - t < window]
    if len(bucket) >= limit:
        return True
    bucket.append(now)
    return False


# ── Helpers ─────────────────────────────────────────────────────────────────

async def check_not_banned(message: Message) -> bool:
    if await is_banned(message.from_user.id):
        await message.reply(
            "🚫 <b>Доступ ограничен</b>\n\n"
            f"{bq('Вы заблокированы. Обратитесь к администрации.')}",
            reply_markup=ok_kb(),
        )
        return False
    return True


async def check_not_banned_cb(callback: CallbackQuery) -> bool:
    if await is_banned(callback.from_user.id):
        await callback.answer("🚫 Вы заблокированы", show_alert=True)
        return False
    return True


async def _try_delete(msg: Optional[Message]) -> None:
    if msg is None:
        return
    try:
        await msg.delete()
    except Exception:
        pass


async def _auto_delete(bot_msg: Message, user_msg: Optional[Message], delay: int) -> None:
    await asyncio.sleep(delay)
    await _try_delete(bot_msg)
    await _try_delete(user_msg)


async def reply_ephemeral(message: Message, text: str, **kwargs):
    if "reply_markup" not in kwargs:
        kwargs["reply_markup"] = ok_kb()
    sent = await message.reply(text, **kwargs)
    if message.chat.type != "private":
        asyncio.create_task(_auto_delete(sent, message, GROUP_AUTODELETE))
    return sent


def _resolve_photo(card: dict) -> Any:
    local = get_card_photo_input(card["id"], card.get("photo_path"))
    if local:
        return local
    return card.get("photo_id")


def _card_caption(card: dict, rarities: dict) -> str:
    r = rarities.get(card["rarity"], rarities.get("common", {}))
    earned = int(card["coins_earned"])
    if earned < 0:
        title = "💀 <b>Негативная карточка</b>"
        reward_str = f"<b>−{fmt_num(abs(earned))}</b> → {fmt_num(card['balance'])}"
        coin_label = "Штраф"
    else:
        title = "✨ <b>Новая карточка</b>"
        reward_str = f"+<b>{fmt_num(earned)}</b> → {fmt_num(card['balance'])}"
        coin_label = "Награда"
    name_str = f"<b>{esc(card['name'])}</b>"
    rarity_line = line(r.get("icon", "•"), "Редкость", r.get("name", card["rarity"]))
    return (
        f"{title}\n\n"
        f"{bq(line('🃏', 'Название', name_str) + chr(10) + rarity_line)}\n\n"
        f"{line('🪙', coin_label, reward_str)}"
    )


# ── START ───────────────────────────────────────────────────────────────────

@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message) -> None:
    await get_or_create_user(message.from_user.id)
    if not await check_not_banned(message):
        return
    text = (
        "👋 <b>Добро пожаловать</b>\n\n"
        f"{bq('Собирайте коллекцию карточек, копите монеты и соревнуйтесь в топе.')}\n\n"
        f"{line('🃏', 'Карточка', 'кнопка ниже или «мряу»')}\n"
        f"{line('⏱', 'Кулдаун', 'раз в 3 часа')}"
    )
    await message.reply(text, reply_markup=main_kb())


# ── GET CARD ────────────────────────────────────────────────────────────────

@router.message(F.text == "🃏 Получить карточку")
@router.message(F.text.lower().strip() == "мряу")
@router.message(F.text.lower().strip() == "милость")
@router.message(Command("meow"))
async def get_card(message: Message) -> None:
    if not await check_not_banned(message):
        return

    user_id = message.from_user.id
    if rate_limited(f"card:{user_id}", 8, 10):
        return

    await get_or_create_user(user_id)
    card, status = await issue_card(user_id)
    streak, bonus, new_bal, streak_upd = await update_streak(user_id)

    if status == "cooldown":
        user = await get_user(user_id)
        remaining = COOLDOWN_SECONDS - (now_ts() - (user["last_claim"] or 0))
        await reply_ephemeral(
            message,
            "⏳ <b>Подождите немного</b>\n\n"
            f"{bq(f'Следующую карточку можно получить через <b>{fmt_remaining(max(0, remaining))}</b>.')}",
        )
        if streak_upd and streak > 0:
            await _send_streak(message, streak, bonus, new_bal)
        return

    if status == "no_cards":
        await reply_ephemeral(message, "❌ <b>Карточек пока нет</b>\n\n" + bq("База ещё пуста."))
        return

    if status == "all_collected":
        await reply_ephemeral(
            message,
            "🎉 <b>Коллекция полная</b>\n\n" + bq("Вы собрали все карточки."),
        )
        if streak_upd and streak > 0:
            await _send_streak(message, streak, bonus, new_bal)
        return

    if status != "success" or not card:
        await reply_ephemeral(message, "❌ Что-то пошло не так. Попробуйте позже.")
        return

    rarities = await load_rarities()
    caption = _card_caption(card, rarities)
    photo = _resolve_photo(card)
    kb = after_card_kb(user_id)
    effect = None
    if card["rarity"] in ("super_valuable", "legendary") and message.chat.type == "private":
        effect = PARTY_EFFECT

    try:
        kwargs: dict[str, Any] = {"caption": caption, "reply_markup": kb}
        if effect:
            kwargs["message_effect_id"] = effect
        if photo:
            await message.reply_photo(photo=photo, **kwargs)
        else:
            await message.reply(caption, reply_markup=kb)
    except TypeError:
        if photo:
            await message.reply_photo(photo=photo, caption=caption, reply_markup=kb)
        else:
            await message.reply(caption, reply_markup=kb)
    except TelegramBadRequest:
        await message.reply(caption, reply_markup=kb)

    if streak_upd and streak > 0:
        await _send_streak(message, streak, bonus, new_bal)


async def _send_streak(message: Message, streak: int, bonus: int, balance: int) -> None:
    if streak <= 0:
        return
    if streak == 1:
        text = (
            "🔥 <b>Стрик начат</b>\n\n"
            f"{bq('Получайте карточку каждый день — серия будет расти.')}"
        )
    else:
        text = (
            f"🔥 <b>Стрик</b>\n\n"
            f"{bq(line('📅', 'Дней подряд', f'<b>{fmt_days(streak)}</b>'))}"
        )
        if bonus > 0:
            text += f"\n\n{line('🪙', 'Бонус', f'<b>+{fmt_num(bonus)}</b> → {fmt_num(balance)}')}"
    try:
        sent = await message.reply(text)
        if message.chat.type != "private":
            asyncio.create_task(_auto_delete(sent, None, GROUP_AUTODELETE))
    except Exception:
        pass


# ── PROFILE ─────────────────────────────────────────────────────────────────

@router.message(F.text == "👤 Профиль")
@router.message(Command("profile"))
async def show_profile(message: Message) -> None:
    if not await check_not_banned(message):
        return
    user_id = message.from_user.id
    await get_or_create_user(user_id)
    await burn_expired_streak(user_id)

    user = await get_user(user_id)
    if not user:
        await message.reply("❌ Ошибка профиля")
        return

    total = await count_cards()
    owned = await count_user_cards(user_id)
    nick = display_name(user["nickname"], user_id, message.from_user.full_name)

    caption = (
        f"👤 <b>{esc(nick)}</b>\n\n"
        f"🃏 Вы собрали <b>{fmt_num(owned)}</b> из {fmt_num(total)} карточек\n"
        f"🪙 У вас <b>{fmt_num(user['coins'] or 0)}</b> {plural(user['coins'] or 0, 'монета', 'монеты', 'монет')}\n"
        f"🔥 Стрик · <b>{fmt_days(user['streak'] or 0)}</b>"
    )
    await message.reply(caption, reply_markup=profile_kb(user_id))


@router.callback_query(BackProfileCB.filter())
async def back_to_profile(callback: CallbackQuery, callback_data: BackProfileCB) -> None:
    if not await check_not_banned_cb(callback):
        return
    if callback_data.user_id and callback.from_user.id != callback_data.user_id:
        await callback.answer("⚠️ Не ваша кнопка")
        return
    user_id = callback_data.user_id or callback.from_user.id
    await burn_expired_streak(user_id)
    user = await get_user(user_id)
    total = await count_cards()
    owned = await count_user_cards(user_id)
    nick = display_name(user["nickname"] if user else None, user_id, callback.from_user.full_name)
    caption = (
        f"👤 <b>{esc(nick)}</b>\n\n"
        f"🃏 Вы собрали <b>{fmt_num(owned)}</b> из {fmt_num(total)} карточек\n"
        f"🪙 У вас <b>{fmt_num(user['coins'] or 0 if user else 0)}</b> монет\n"
        f"🔥 Стрик · <b>{fmt_days(user['streak'] or 0 if user else 0)}</b>"
    )
    try:
        await callback.message.edit_text(caption, reply_markup=profile_kb(user_id))
    except TelegramBadRequest:
        await callback.message.answer(caption, reply_markup=profile_kb(user_id))
    await callback.answer()


# ── COLLECTION ──────────────────────────────────────────────────────────────

@router.callback_query(MainMenuCB.filter())
@router.callback_query(CardActionCB.filter(F.action == "collection"))
async def show_collection(callback: CallbackQuery, callback_data: Any) -> None:
    if not await check_not_banned_cb(callback):
        return
    user_id = getattr(callback_data, "user_id", 0) or callback.from_user.id
    if user_id != callback.from_user.id:
        await callback.answer("⚠️ Не ваша кнопка")
        return

    stats = await get_user_rarity_stats(user_id)
    total = await count_user_cards(user_id)
    total_in_game = await count_cards()
    rarities = await load_rarities()

    buttons = []
    for key, info in rarities.items():
        cnt = stats.get(key, 0)
        if cnt == 0:
            continue
        from db import count_cards as cc
        total_r = await cc(key)
        buttons.append(
            InlineKeyboardButton(
                text=f"{info['icon']} {info['name'][:8]} {fmt_num(cnt)}/{fmt_num(total_r)}",
                callback_data=CollectionCB(rarity=key, page=0, user_id=user_id).pack(),
            )
        )

    grid = []
    for i in range(0, len(buttons), 2):
        grid.append(buttons[i : i + 2])
    grid.append([
        InlineKeyboardButton(
            text="‹ В профиль",
            callback_data=BackProfileCB(user_id=user_id).pack(),
        )
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=grid)
    nick = display_name(None, user_id, callback.from_user.full_name)
    text = (
        f"🃏 <b>Карточки {esc(nick)}</b>\n\n"
        f"{bq(f'Собрано <b>{fmt_num(total)}</b> из {fmt_num(total_in_game)}')}"
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(CollectionCB.filter())
async def view_rarity(callback: CallbackQuery, callback_data: CollectionCB) -> None:
    if not await check_not_banned_cb(callback):
        return
    if callback_data.user_id and callback.from_user.id != callback_data.user_id:
        await callback.answer("⚠️ Не ваша кнопка")
        return
    if rate_limited(f"coll:{callback.from_user.id}", 1, 1.0):
        await callback.answer("⏳ Подождите…")
        return

    user_id = callback_data.user_id or callback.from_user.id
    cards = await get_user_cards_by_rarity(user_id, callback_data.rarity)
    if not cards:
        await callback.answer("Нет карточек")
        return

    page = max(0, min(callback_data.page, len(cards) - 1))
    card = cards[page]
    rarities = await load_rarities()
    info = rarities.get(callback_data.rarity, {})
    rw = int(info.get("reward", 0))
    reward_str = f"−{fmt_num(abs(rw))}" if rw < 0 else f"+{fmt_num(rw)}"

    name_str = f"<b>{esc(card['name'])}</b>"
    rarity_line = line(info.get("icon", "•"), "Редкость", info.get("name", callback_data.rarity))
    caption = (
        f"{bq(line('🃏', 'Название', name_str) + chr(10) + rarity_line)}\n\n"
        f"{line('🪙', 'Награда' if rw >= 0 else 'Штраф', reward_str)}"
    )

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            text="‹",
            callback_data=CollectionCB(rarity=callback_data.rarity, page=page - 1, user_id=user_id).pack(),
        ))
    nav.append(InlineKeyboardButton(text=f"{page + 1} / {len(cards)}", callback_data="ignore"))
    if page < len(cards) - 1:
        nav.append(InlineKeyboardButton(
            text="›",
            callback_data=CollectionCB(rarity=callback_data.rarity, page=page + 1, user_id=user_id).pack(),
        ))

    kb = InlineKeyboardMarkup(inline_keyboard=[
        nav,
        [InlineKeyboardButton(text="‹ К редкостям", callback_data=MainMenuCB(user_id=user_id).pack())],
    ])

    photo = get_card_photo_input(card["id"], card["photo_path"] if "photo_path" in card.keys() else None)
    if not photo and card["photo_id"]:
        photo = card["photo_id"]

    try:
        if photo and callback.message.photo:
            await callback.message.edit_media(
                media=InputMediaPhoto(media=photo, caption=caption),
                reply_markup=kb,
            )
        elif photo:
            await callback.message.answer_photo(photo=photo, caption=caption, reply_markup=kb)
            try:
                await callback.message.delete()
            except Exception:
                pass
        else:
            await callback.message.edit_text(caption, reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(caption, reply_markup=kb)
    await callback.answer()


# ── TOP ─────────────────────────────────────────────────────────────────────

async def build_top(kind: str, current_id: int) -> str:
    await burn_expired_streak(current_id)
    from db import get_db
    limit = 10
    ts = now_ts()

    async with get_db() as db:
        if kind == "cards":
            cur = await db.execute(
                """SELECT u.user_id, u.nickname,
                          (SELECT COUNT(*) FROM inventory i WHERE i.user_id = u.user_id) AS value
                   FROM users u WHERE u.role != 'banned'
                   ORDER BY value DESC, u.user_id ASC LIMIT ?""",
                (limit,),
            )
            top = await cur.fetchall()
            cur = await db.execute(
                "SELECT COUNT(*) FROM inventory WHERE user_id = ?", (current_id,)
            )
            my_val = (await cur.fetchone())[0]
            unit, title = "🃏", "🃏 Топ по карточкам"
        elif kind == "streak":
            cur = await db.execute(
                """SELECT user_id, nickname,
                          CASE WHEN last_claim > 0 AND (? - last_claim) >= ?
                               THEN 0 ELSE COALESCE(streak, 0) END AS value
                   FROM users WHERE role != 'banned'
                   ORDER BY value DESC, user_id ASC LIMIT ?""",
                (ts, STREAK_EXPIRE_SECONDS := 86400, limit),
            )
            top = await cur.fetchall()
            cur = await db.execute(
                """SELECT CASE WHEN last_claim > 0 AND (? - last_claim) >= ?
                               THEN 0 ELSE COALESCE(streak, 0) END
                   FROM users WHERE user_id = ?""",
                (ts, 86400, current_id),
            )
            my_val = (await cur.fetchone())[0]
            unit, title = "🔥", "🔥 Топ по стрику"
        else:
            cur = await db.execute(
                """SELECT user_id, nickname, coins AS value
                   FROM users WHERE role != 'banned'
                   ORDER BY value DESC, user_id ASC LIMIT ?""",
                (limit,),
            )
            top = await cur.fetchall()
            cur = await db.execute(
                "SELECT coins FROM users WHERE user_id = ?", (current_id,)
            )
            row = await cur.fetchone()
            my_val = row[0] if row else 0
            unit, title = "🪙", "🪙 Топ по монетам"

        cur = await db.execute(
            "SELECT nickname FROM users WHERE user_id = ?", (current_id,)
        )
        my_row = await cur.fetchone()
        my_nick = display_name(my_row["nickname"] if my_row else None, current_id)

        # rank
        if kind == "cards":
            cur = await db.execute(
                """SELECT COUNT(*) + 1 FROM (
                     SELECT (SELECT COUNT(*) FROM inventory i WHERE i.user_id = u.user_id) AS v
                     FROM users u WHERE u.role != 'banned'
                   ) WHERE v > ?""",
                (my_val,),
            )
        elif kind == "streak":
            cur = await db.execute(
                """SELECT COUNT(*) + 1 FROM users
                   WHERE role != 'banned'
                     AND (CASE WHEN last_claim > 0 AND (? - last_claim) >= ?
                               THEN 0 ELSE COALESCE(streak, 0) END) > ?""",
                (ts, 86400, my_val),
            )
        else:
            cur = await db.execute(
                "SELECT COUNT(*) + 1 FROM users WHERE coins > ? AND role != 'banned'",
                (my_val,),
            )
        my_rank = (await cur.fetchone())[0]

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, row in enumerate(top, 1):
        medal = medals[i - 1] if i <= 3 else f"{i}."
        nick = display_name(row["nickname"], row["user_id"])
        lines.append(f"{medal} {esc(nick)} · <b>{fmt_num(row['value'])}</b> {unit}")

    return (
        f"<b>{title}</b>\n{sep()}\n\n"
        f"{bq(chr(10).join(lines) if lines else 'Пусто')}\n\n"
        f"{line('📌', 'Ваше место', f'<b>#{fmt_num(my_rank)}</b>')}\n"
        f"{line('👤', esc(my_nick), f'<b>{fmt_num(my_val)}</b> {unit}')}"
    )


@router.message(F.text == "🏆 Топ")
@router.message(Command("top"))
async def show_top(message: Message) -> None:
    if not await check_not_banned(message):
        return
    if rate_limited(f"top:{message.from_user.id}", 5, 5):
        return
    text = await build_top("coins", message.from_user.id)
    await message.reply(text, reply_markup=top_kb("coins"))


@router.callback_query(TopCB.filter())
@router.callback_query(TopRefreshCB.filter())
async def switch_top(callback: CallbackQuery, callback_data: Any) -> None:
    if not await check_not_banned_cb(callback):
        return
    if rate_limited(f"top:{callback.from_user.id}", 5, 5):
        await callback.answer("Слишком часто")
        return
    kind = callback_data.kind
    text = await build_top(kind, callback.from_user.id)
    try:
        await callback.message.edit_text(text, reply_markup=top_kb(kind))
        await callback.answer("Обновлено" if isinstance(callback_data, TopRefreshCB) else None)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            await callback.answer("Данные не изменились")
        else:
            await callback.message.answer(text, reply_markup=top_kb(kind))
            await callback.answer()


# ── DICE ────────────────────────────────────────────────────────────────────

@router.message(F.text == "🎲 Кубик")
@router.message(Command("dice"))
async def dice_handler(message: Message) -> None:
    if not await check_not_banned(message):
        return
    user_id = message.from_user.id
    if rate_limited(f"dice:{user_id}", 5, 15):
        await reply_ephemeral(message, "⏳ Слишком часто. Подождите.")
        return

    await get_or_create_user(user_id)
    user = await get_user(user_id)
    balance = user["coins"] or 0 if user else 0
    last_dice = user["last_dice"] or 0 if user else 0

    if balance < DICE_MIN_BALANCE:
        await reply_ephemeral(
            message,
            "⚠️ <b>Недостаточно монет</b>\n\n"
            f"{bq(line('🪙', 'Нужно', f'<b>{fmt_num(DICE_MIN_BALANCE)}</b>') + chr(10) + line('🪙', 'У вас', f'<b>{fmt_num(balance)}</b>'))}",
        )
        return

    if last_dice > 0 and (now_ts() - last_dice) < DICE_COOLDOWN_SECONDS:
        rem = DICE_COOLDOWN_SECONDS - (now_ts() - last_dice)
        await reply_ephemeral(
            message,
            "⏳ <b>Кубик пока недоступен</b>\n\n"
            f"{bq(f'Следующий бросок через <b>{fmt_remaining(rem)}</b>.')}",
        )
        return

    from db import get_db
    async with get_db() as db:
        await db.execute(
            "UPDATE users SET last_dice = ? WHERE user_id = ?", (now_ts(), user_id)
        )

    spin = await message.reply_dice(emoji="🎲")
    await asyncio.sleep(DICE_SPIN_DELAY)

    value = spin.dice.value if spin.dice and spin.dice.value else 1
    result = await apply_dice_result(user_id, value)

    delta = result["delta"]
    if delta > 0:
        title, delta_str = "🎉 Выигрыш", f"+{fmt_num(delta)}"
    elif delta < 0:
        title, delta_str = "😔 Проигрыш", f"−{fmt_num(abs(delta))}"
    else:
        title, delta_str = "· Ничья", "±0"

    bal = result["balance"]
    bal_str = f"<b>{fmt_num(bal)}</b> {plural(bal, 'монета', 'монеты', 'монет')}"
    text = (
        f"🎲 <b>{title}</b>\n\n"
        f"{bq(line('🎲', 'Выпало', f'<b>{value}</b>') + chr(10) + line('🪙', 'Итог', f'<b>{delta_str}</b>'))}\n\n"
        f"{line('🪙', 'У вас', bal_str)}"
    )
    try:
        await spin.reply(text)
    except TelegramBadRequest:
        await message.reply(text)


# ── NICKNAME ────────────────────────────────────────────────────────────────

class NickSG(StatesGroup):
    pending = State()


@router.message(Command("nick"))
async def nick_cmd(message: Message, command: CommandObject, state: FSMContext) -> None:
    if not await check_not_banned(message):
        return
    user_id = message.from_user.id
    await get_or_create_user(user_id)
    arg = (command.args or "").strip()

    if arg.lower() == "reset":
        await state.set_state(NickSG.pending)
        await state.update_data(action="reset", nick=None)
        await message.reply(
            "♻️ <b>Сбросить никнейм?</b>\n\n"
            f"{bq(line('ℹ️', 'Результат', 'имя из Telegram') + chr(10) + line('🪙', 'Стоимость', 'бесплатно'))}",
            reply_markup=nick_confirm_kb("reset"),
        )
        return

    if not arg:
        await message.reply(
            "✏️ <b>Смена никнейма</b>\n\n"
            f"{bq(f'<code>/nick НовыйНик</code> · {NICKNAME_COST} 🪙')}\n\n"
            f"{line('♻️', 'Сброс', '/nick reset · бесплатно')}"
        )
        return

    new_nick = validate_nickname(arg)
    if not new_nick:
        await message.reply("❌ Неверный никнейм (2–32 символа, без ссылок).")
        return

    user = await get_user(user_id)
    balance = user["coins"] or 0 if user else 0
    if balance < NICKNAME_COST:
        await message.reply(
            "⚠️ <b>Недостаточно монет</b>\n\n"
            f"{bq(line('🪙', 'Нужно', f'<b>{fmt_num(NICKNAME_COST)}</b>') + chr(10) + line('🪙', 'У вас', f'<b>{fmt_num(balance)}</b>'))}"
        )
        return

    await state.set_state(NickSG.pending)
    await state.update_data(action="apply", nick=new_nick)
    await message.reply(
        "✏️ <b>Сменить никнейм?</b>\n\n"
        f"{bq(line('👤', 'Новый ник', f'<b>{esc(new_nick)}</b>') + chr(10) + line('🪙', 'Стоимость', f'<b>{NICKNAME_COST}</b>'))}\n\n"
        f"{line('🪙', 'У вас', fmt_num(balance))}",
        reply_markup=nick_confirm_kb("apply"),
    )


@router.callback_query(NickConfirmCB.filter())
async def nick_confirm(callback: CallbackQuery, callback_data: NickConfirmCB, state: FSMContext) -> None:
    if not await check_not_banned_cb(callback):
        return
    user_id = callback.from_user.id
    data = await state.get_data()

    if callback_data.action == "cancel":
        await state.clear()
        await _try_delete(callback.message)
        await callback.answer()
        return

    if callback_data.action == "reset":
        await set_nickname(user_id, None)
        await state.clear()
        shown = display_name(None, user_id, callback.from_user.full_name)
        await callback.message.edit_text(
            "✓ <b>Никнейм сброшен</b>\n\n" + bq(line("👤", "Отображение", esc(shown))),
            reply_markup=ok_kb(),
        )
        await callback.answer()
        return

    if callback_data.action == "apply":
        nick = data.get("nick")
        if not nick or not validate_nickname(nick):
            await state.clear()
            await callback.message.edit_text("❌ Неверный ник")
            await callback.answer()
            return
        user = await get_user(user_id)
        if not user or (user["coins"] or 0) < NICKNAME_COST:
            await state.clear()
            await callback.message.edit_text("⚠️ Недостаточно монет")
            await callback.answer()
            return
        await update_coins(user_id, -NICKNAME_COST)
        await set_nickname(user_id, nick)
        await state.clear()
        await callback.message.edit_text(
            "✓ <b>Никнейм изменён</b>\n\n"
            + bq(line("👤", "Ник", esc(nick)) + chr(10) + line("🪙", "Списано", str(NICKNAME_COST))),
            reply_markup=ok_kb(),
        )
        await callback.answer()
        return
    await callback.answer()


# ── HELP ────────────────────────────────────────────────────────────────────

HELP = {
    "cards": (
        "🃏 <b>Карточки</b>\n" + sep() + "\n\n"
        "Карточки — основа игры. Каждая имеет название, редкость и награду.\n\n"
        "<b>Как получить</b>\n"
        "Напишите «мряу» или нажмите «Получить карточку». Раз в 3 часа.\n\n"
        "<b>Редкости</b>\n"
        + "\n".join(f"{v['icon']} <b>{v['name']}</b> — {v['reward']} 🪙" for v in DEFAULT_RARITIES.values())
        + "\n\nДубликатов нет."
    ),
    "coins": (
        "🪙 <b>Монеты</b>\n" + sep() + "\n\n"
        "• Награда за карточку\n"
        "• Бонус за стрик\n"
        "• Выигрыш в кубике\n\n"
        f"Смена ника — {NICKNAME_COST} 🪙"
    ),
    "streak": (
        "🔥 <b>Стрик</b>\n" + sep() + "\n\n"
        "Серия дней подряд с карточкой.\n"
        "Пропуск > 24 часов — серия сгорает.\n\n"
        "Бонусы растут с длиной серии."
    ),
    "games": (
        "🎲 <b>Кубик</b>\n" + sep() + "\n\n"
        f"Минимум {DICE_MIN_BALANCE} 🪙. Кулдаун 10 минут.\n\n"
        "1 → −20 · 2 → −10 · 3 → 0 · 4 → +10 · 5 → +20 · 6 → +35"
    ),
    "profile": (
        "👤 <b>Профиль</b>\n" + sep() + "\n\n"
        "Карточки, монеты, стрик.\n"
        f"/nick НовыйНик — смена ({NICKNAME_COST} 🪙)\n"
        "/nick reset — сброс"
    ),
}


@router.message(F.text == "❓ Помощь")
@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if not await check_not_banned(message):
        return
    await message.reply(HELP["cards"], reply_markup=help_kb("cards"))


@router.callback_query(HelpCB.filter())
async def help_tab(callback: CallbackQuery, callback_data: HelpCB) -> None:
    if not await check_not_banned_cb(callback):
        return
    tab = callback_data.tab if callback_data.tab in HELP else "cards"
    try:
        await callback.message.edit_text(HELP[tab], reply_markup=help_kb(tab))
    except TelegramBadRequest:
        pass
    await callback.answer()


# ── UTILS ───────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "ignore")
async def ignore_cb(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(OkDelCB.filter())
async def ok_delete(callback: CallbackQuery) -> None:
    await callback.answer()
    await _try_delete(callback.message)
    if callback.message and callback.message.reply_to_message:
        await _try_delete(callback.message.reply_to_message)
