"""
Хендлеры админ-бота.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, Union

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import DEFAULT_RARITIES, ROLES, PER_PAGE, BOT_TOKEN
from db import (
    get_db, is_admin, is_superadmin, get_user_role, set_user_role,
    add_card, get_card, update_card, delete_card, count_cards, list_cards,
    get_rarity_counts, load_rarities, get_user, set_coins, set_nickname,
    count_user_cards, get_or_create_user,
)
from photos import sync_card_photo, get_card_photo_input
from utils import esc, fmt_num, line, bq, sep, role_display, display_name, now_ts
from admin_bot.keyboards import (
    main_reply_kb, rarity_kb,
    CardFilterCB, CardPageCB, CardManageCB, CardRarityCB,
    UserFilterCB, UserPageCB, UserViewCB, UserActionCB, StatsCB,
)

logger = logging.getLogger("admin_bot")
router = Router()


# ── Filters ─────────────────────────────────────────────────────────────────

async def _require_admin(event: Message | CallbackQuery) -> bool:
    uid = event.from_user.id
    if await is_admin(uid):
        return True
    text = "⚠️ Нет доступа. Требуется роль admin / superadmin."
    if isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=True)
    else:
        await event.answer(text)
    return False


# ── FSM ─────────────────────────────────────────────────────────────────────

class AddCardSG(StatesGroup):
    photo = State()
    name = State()
    rarity = State()


class EditNameSG(StatesGroup):
    card_id = State()
    new_name = State()


class EditPhotoSG(StatesGroup):
    card_id = State()
    photo = State()


# ── Safe edit ───────────────────────────────────────────────────────────────

async def safe_edit(
    message: Message,
    *,
    text: Optional[str] = None,
    caption: Optional[str] = None,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    media=None,
) -> Message:
    try:
        if media is not None:
            await message.edit_media(
                media=InputMediaPhoto(media=media, caption=caption or ""),
                reply_markup=reply_markup,
            )
            return message
        if caption is not None and message.photo:
            await message.edit_caption(caption=caption, reply_markup=reply_markup)
            return message
        if text is not None:
            await message.edit_text(text, reply_markup=reply_markup)
            return message
    except TelegramBadRequest:
        try:
            await message.delete()
        except Exception:
            pass
        if media is not None:
            return await message.answer_photo(photo=media, caption=caption or "", reply_markup=reply_markup)
        return await message.answer(text or caption or "—", reply_markup=reply_markup)
    return message


# ── START / MENU ────────────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not await _require_admin(message):
        return
    role = await get_user_role(message.from_user.id)
    text = (
        f"⚙️ <b>Админ-панель</b>\n{sep()}\n\n"
        f"{line('🎭', 'Ваша роль', role_display(role))}\n\n"
        "Используйте меню ниже или команды."
    )
    await message.answer(text, reply_markup=main_reply_kb())


@router.message(Command("cancel"))
@router.message(F.text == "❌ Отмена")
async def cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    await state.clear()
    if current:
        await message.answer("✅ Операция отменена", reply_markup=main_reply_kb())
    else:
        await message.answer("Нечего отменять.", reply_markup=main_reply_kb())


@router.callback_query(F.data == "cancel_add")
async def cancel_add(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await call.message.answer("✅ Отменено", reply_markup=main_reply_kb())


@router.callback_query(F.data == "admin_main")
async def admin_main(call: CallbackQuery) -> None:
    if not await _require_admin(call):
        return
    role = await get_user_role(call.from_user.id)
    text = (
        f"⚙️ <b>Админ-панель</b>\n{sep()}\n\n"
        f"{line('🎭', 'Ваша роль', role_display(role))}"
    )
    await safe_edit(call.message, text=text)
    await call.answer()


# ── ADD CARD ────────────────────────────────────────────────────────────────

@router.message(F.text == "➕ Добавить")
async def add_card_start(message: Message, state: FSMContext) -> None:
    if not await _require_admin(message):
        return
    await state.set_state(AddCardSG.photo)
    counts = await get_rarity_counts()
    total = sum(counts.values()) or 1
    lines = []
    for k, v in DEFAULT_RARITIES.items():
        actual = counts.get(k, 0)
        share = actual / total * 100
        lines.append(f"{v['icon']} {v['name']}: {actual} (цель ~{v['weight']}%, сейчас {share:.0f}%)")
    hint = "\n".join(lines)
    await message.answer(
        "📷 <b>Отправьте фото новой карточки</b>\n\n" + bq(hint) + "\n\nИли «❌ Отмена».",
        reply_markup=main_reply_kb(fsm=True),
    )


@router.message(AddCardSG.photo, F.photo)
async def add_card_photo(message: Message, state: FSMContext) -> None:
    if not await _require_admin(message):
        await state.clear()
        return
    await state.update_data(photo_id=message.photo[-1].file_id)
    await state.set_state(AddCardSG.name)
    await message.answer(
        "✍️ <b>Введите название</b> (до 64 символов)",
        reply_markup=main_reply_kb(fsm=True),
    )


@router.message(AddCardSG.name, F.text)
async def add_card_name(message: Message, state: FSMContext) -> None:
    if not await _require_admin(message):
        await state.clear()
        return
    if message.text.strip() in ("❌ Отмена", "/cancel"):
        return
    await state.update_data(name=message.text[:64])
    await state.set_state(AddCardSG.rarity)
    await message.answer("🎲 <b>Выберите редкость</b>", reply_markup=rarity_kb())


@router.callback_query(AddCardSG.rarity, F.data.startswith("set_rarity:"))
async def add_card_rarity(call: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(call):
        await state.clear()
        return
    rarity = call.data.split(":")[1]
    data = await state.get_data()
    photo_path = photo_uuid = None

    card_id = await add_card(data["name"], rarity, photo_id=data["photo_id"])

    try:
        result = await sync_card_photo(call.bot, card_id, data["photo_id"], rarity=rarity)
        if result:
            photo_uuid, photo_path = result
            await update_card(card_id, photo_path=photo_path, photo_uuid=photo_uuid)
    except Exception as e:
        logger.error("sync photo: %s", e)

    r = DEFAULT_RARITIES.get(rarity, {})
    body = "\n".join([
        line("🃏", "Название", esc(data["name"])),
        line(r.get("icon", "•"), "Редкость", r.get("name", rarity)),
        line("🆔", "ID", card_id),
        line("💾", "Файл", photo_path or "только file_id"),
    ])
    await call.message.answer(
        f"✅ <b>Карточка добавлена</b>\n\n{bq(body)}",
        reply_markup=main_reply_kb(),
    )
    await state.clear()
    await call.answer()


# ── CARDS LIST ──────────────────────────────────────────────────────────────

async def build_filter_menu() -> tuple[str, InlineKeyboardMarkup]:
    counts = await get_rarity_counts()
    total = sum(counts.values())
    text = f"📜 <b>Карточки</b>\n{sep()}\n\nВсего: <b>{fmt_num(total)}</b>\n\nВыберите редкость:"
    b = InlineKeyboardBuilder()
    b.button(text=f"📦 Все ({fmt_num(total)})", callback_data=CardFilterCB(rarity="all").pack())
    for key, info in DEFAULT_RARITIES.items():
        cnt = counts.get(key, 0)
        b.button(
            text=f"{info['icon']} {info['name']} ({fmt_num(cnt)})",
            callback_data=CardFilterCB(rarity=key).pack(),
        )
    b.button(text="‹ В меню", callback_data="admin_main")
    b.adjust(1)
    return text, b.as_markup()


async def build_cards_page(page: int = 0, rarity: str = "all") -> tuple[str, InlineKeyboardMarkup]:
    total = await count_cards(None if rarity == "all" else rarity)
    if total == 0:
        label = DEFAULT_RARITIES.get(rarity, {}).get("name", rarity) if rarity != "all" else "все"
        return (
            f"📜 <b>Карточки</b> · {label}\n\nПусто.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="‹ К редкостям", callback_data="cards_filter")],
                [InlineKeyboardButton(text="‹ В меню", callback_data="admin_main")],
            ]),
        )

    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    cards = await list_cards(None if rarity == "all" else rarity, PER_PAGE, page * PER_PAGE)

    b = InlineKeyboardBuilder()
    lines = []
    for c in cards:
        r = DEFAULT_RARITIES.get(c["rarity"], {})
        from db import get_db
        async with get_db() as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM inventory WHERE card_id = ?", (c["id"],)
            )
            owners = (await cur.fetchone())[0]
        has = "💾" if c["photo_path"] else "☁️"
        lines.append(
            f"{r.get('icon', '•')} <b>{esc(c['name'])}</b>\n"
            f"    #{c['id']} · 👥 {fmt_num(owners)} · {has}"
        )
        b.button(
            text=f"{r.get('icon', '')} {c['name'][:28]}",
            callback_data=CardManageCB(card_id=c["id"], rarity=rarity).pack(),
        )

    filter_label = DEFAULT_RARITIES[rarity]["name"] if rarity != "all" else "Все"
    text = (
        f"📜 <b>Карточки</b> · {filter_label}\n{sep()}\n"
        f"{line('📄', 'Страница', f'<b>{page + 1}</b> / {total_pages}')}\n"
        f"{line('📦', 'Найдено', f'<b>{fmt_num(total)}</b>')}\n\n"
        f"{bq(chr(10).join(lines))}"
    )

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="‹", callback_data=CardPageCB(page=page - 1, rarity=rarity).pack()))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="ignore"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="›", callback_data=CardPageCB(page=page + 1, rarity=rarity).pack()))

    b.adjust(1)
    b.row(*nav)
    b.row(InlineKeyboardButton(text="‹ К редкостям", callback_data="cards_filter"))
    b.row(InlineKeyboardButton(text="‹ В меню", callback_data="admin_main"))
    return text, b.as_markup()


@router.message(F.text == "📜 Карточки")
@router.callback_query(F.data == "cards_filter")
async def cards_filter(event: Message | CallbackQuery) -> None:
    if not await _require_admin(event):
        return
    text, kb = await build_filter_menu()
    if isinstance(event, CallbackQuery):
        await safe_edit(event.message, text=text, reply_markup=kb)
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb)


@router.callback_query(CardFilterCB.filter())
async def cards_chosen(call: CallbackQuery, callback_data: CardFilterCB) -> None:
    if not await _require_admin(call):
        return
    text, kb = await build_cards_page(0, callback_data.rarity)
    await safe_edit(call.message, text=text, reply_markup=kb)
    await call.answer()


@router.callback_query(CardPageCB.filter())
async def cards_page(call: CallbackQuery, callback_data: CardPageCB) -> None:
    if not await _require_admin(call):
        return
    text, kb = await build_cards_page(callback_data.page, callback_data.rarity)
    await safe_edit(call.message, text=text, reply_markup=kb)
    await call.answer()


@router.callback_query(CardManageCB.filter())
async def card_manage(call: CallbackQuery, callback_data: CardManageCB) -> None:
    if not await _require_admin(call):
        return
    card = await get_card(callback_data.card_id)
    if not card:
        await call.answer("❌ Не найдена", show_alert=True)
        return

    from db import get_db
    async with get_db() as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM inventory WHERE card_id = ?", (card["id"],)
        )
        owners = (await cur.fetchone())[0]

    r = DEFAULT_RARITIES.get(card["rarity"], {})
    body = "\n".join([
        line("🆔", "ID", f"<code>{card['id']}</code>"),
        line(r.get("icon", "•"), "Редкость", r.get("name", card["rarity"])),
        line("👥", "Владельцев", f"<b>{fmt_num(owners)}</b>"),
        line("💾", "Файл", card["photo_path"] or "нет"),
    ])
    caption = f"🃏 <b>{esc(card['name'])}</b>\n{sep(20)}\n\n{bq(body)}"

    b = InlineKeyboardBuilder()
    b.button(text="✏️ Название", callback_data=f"edit_name:{card['id']}")
    b.button(text="🎲 Редкость", callback_data=f"edit_rarity:{card['id']}")
    b.button(text="🖼 Фото", callback_data=f"edit_photo:{card['id']}")
    b.button(text="🗑 Удалить", callback_data=f"delete_card:{card['id']}")
    b.button(text="‹ К списку", callback_data=CardPageCB(page=0, rarity=callback_data.rarity).pack())
    b.adjust(1)

    photo = get_card_photo_input(card["id"], card["photo_path"])
    if not photo and card["photo_id"]:
        photo = card["photo_id"]

    await safe_edit(
        call.message,
        text=caption if not photo else None,
        caption=caption if photo else None,
        media=photo,
        reply_markup=b.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("delete_card:"))
async def delete_card_cb(call: CallbackQuery) -> None:
    if not await _require_admin(call):
        return
    card_id = int(call.data.split(":")[1])
    await delete_card(card_id)
    await call.message.answer("🗑 Карточка удалена", reply_markup=main_reply_kb())
    await call.answer()


@router.callback_query(F.data.startswith("edit_name:"))
async def edit_name_start(call: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(call):
        return
    await state.update_data(card_id=int(call.data.split(":")[1]))
    await state.set_state(EditNameSG.new_name)
    await call.message.answer("✍️ Введите новое название:", reply_markup=main_reply_kb(fsm=True))
    await call.answer()


@router.message(EditNameSG.new_name, F.text)
async def edit_name_save(message: Message, state: FSMContext) -> None:
    if not await _require_admin(message):
        await state.clear()
        return
    data = await state.get_data()
    new_name = message.text[:64]
    await update_card(data["card_id"], name=new_name)
    await message.answer(f"✅ Название: {esc(new_name)}", reply_markup=main_reply_kb())
    await state.clear()


@router.callback_query(F.data.startswith("edit_rarity:"))
async def edit_rarity_start(call: CallbackQuery) -> None:
    if not await _require_admin(call):
        return
    card_id = int(call.data.split(":")[1])
    b = InlineKeyboardBuilder()
    for key, info in DEFAULT_RARITIES.items():
        b.button(
            text=f"{info['icon']} {info['name']}",
            callback_data=CardRarityCB(card_id=card_id, rarity=key).pack(),
        )
    b.button(text="‹ Назад", callback_data=CardManageCB(card_id=card_id).pack())
    b.adjust(1)
    await call.message.answer("🎲 Выберите редкость:", reply_markup=b.as_markup())
    await call.answer()


@router.callback_query(CardRarityCB.filter())
async def edit_rarity_save(call: CallbackQuery, callback_data: CardRarityCB) -> None:
    if not await _require_admin(call):
        return
    await update_card(callback_data.card_id, rarity=callback_data.rarity)
    name = DEFAULT_RARITIES[callback_data.rarity]["name"]
    await call.message.answer(f"✅ Редкость: {name}", reply_markup=main_reply_kb())
    await call.answer()


@router.callback_query(F.data.startswith("edit_photo:"))
async def edit_photo_start(call: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(call):
        return
    await state.set_state(EditPhotoSG.photo)
    await state.update_data(card_id=int(call.data.split(":")[1]))
    await call.message.answer("📷 Отправьте новое фото\n\nИли «❌ Отмена».", reply_markup=main_reply_kb(fsm=True))
    await call.answer()


@router.message(EditPhotoSG.photo, F.photo)
async def edit_photo_save(message: Message, state: FSMContext) -> None:
    if not await _require_admin(message):
        await state.clear()
        return
    data = await state.get_data()
    card_id = data.get("card_id")
    if not card_id:
        await state.clear()
        return
    card = await get_card(card_id)
    rarity = card["rarity"] if card else "common"
    new_photo = message.photo[-1].file_id
    await update_card(card_id, photo_id=new_photo)
    path_str = None
    try:
        result = await sync_card_photo(
            message.bot, card_id, new_photo, rarity=rarity,
            existing_uuid=card["photo_uuid"] if card else None,
        )
        if result:
            puuid, path_str = result
            await update_card(card_id, photo_path=path_str, photo_uuid=puuid)
    except Exception as e:
        logger.error("edit photo: %s", e)
    await message.answer(
        f"✅ Фото обновлено (ID <code>{card_id}</code>)\n{line('💾', 'Файл', path_str or 'ошибка')}",
        reply_markup=main_reply_kb(),
    )
    await state.clear()


# ── USERS ───────────────────────────────────────────────────────────────────

async def build_user_filter() -> tuple[str, InlineKeyboardMarkup]:
    from db import get_db
    async with get_db() as db:
        cur = await db.execute("SELECT role, COUNT(*) FROM users GROUP BY role")
        counts = dict(await cur.fetchall())
        cur = await db.execute("SELECT COUNT(*) FROM users")
        total = (await cur.fetchone())[0]
    text = f"👥 <b>Пользователи</b>\n{sep()}\n\nВсего: <b>{fmt_num(total)}</b>\n\nВыберите роль:"
    b = InlineKeyboardBuilder()
    b.button(text=f"📦 Все ({fmt_num(total)})", callback_data=UserFilterCB(role="all").pack())
    for key, info in ROLES.items():
        cnt = counts.get(key, 0)
        b.button(text=f"{info['icon']} {info['name']} ({fmt_num(cnt)})", callback_data=UserFilterCB(role=key).pack())
    b.button(text="‹ В меню", callback_data="admin_main")
    b.adjust(1)
    return text, b.as_markup()


async def build_users_page(page: int = 0, role: str = "all") -> tuple[str, InlineKeyboardMarkup]:
    from db import get_db
    async with get_db() as db:
        if role == "all":
            cur = await db.execute("SELECT COUNT(*) FROM users")
        else:
            cur = await db.execute("SELECT COUNT(*) FROM users WHERE role = ?", (role,))
        total = (await cur.fetchone())[0]

        if total == 0:
            return (
                f"👥 Нет записей (фильтр: {role}).",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="‹ К ролям", callback_data="users_filter")],
                    [InlineKeyboardButton(text="‹ В меню", callback_data="admin_main")],
                ]),
            )

        total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
        page = max(0, min(page, total_pages - 1))
        offset = page * PER_PAGE

        if role == "all":
            cur = await db.execute(
                "SELECT user_id, nickname, coins, streak, role, registration FROM users ORDER BY registration DESC LIMIT ? OFFSET ?",
                (PER_PAGE, offset),
            )
        else:
            cur = await db.execute(
                "SELECT user_id, nickname, coins, streak, role, registration FROM users WHERE role = ? ORDER BY registration DESC LIMIT ? OFFSET ?",
                (role, PER_PAGE, offset),
            )
        users = await cur.fetchall()

    b = InlineKeyboardBuilder()
    lines = []
    for u in users:
        ri = ROLES.get(u["role"] or "user", ROLES["user"])
        nick = display_name(u["nickname"], u["user_id"])
        lines.append(
            f"{ri['icon']} <b>{esc(nick)}</b>\n"
            f"    <code>{u['user_id']}</code> · 🪙 {fmt_num(u['coins'] or 0)} · 🔥 {u['streak'] or 0}"
        )
        b.button(
            text=f"{ri['icon']} {nick[:24]}",
            callback_data=UserViewCB(user_id=u["user_id"], role=role).pack(),
        )

    role_label = ROLES[role]["name"] if role != "all" else "Все"
    text = (
        f"👥 <b>Пользователи</b> · {role_label}\n{sep()}\n"
        f"{line('📄', 'Страница', f'<b>{page + 1}</b> / {total_pages}')}\n"
        f"{line('📦', 'Найдено', f'<b>{fmt_num(total)}</b>')}\n\n"
        f"{bq(chr(10).join(lines))}"
    )

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="‹", callback_data=UserPageCB(page=page - 1, role=role).pack()))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="ignore"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="›", callback_data=UserPageCB(page=page + 1, role=role).pack()))

    b.adjust(1)
    b.row(*nav)
    b.row(InlineKeyboardButton(text="‹ К ролям", callback_data="users_filter"))
    b.row(InlineKeyboardButton(text="‹ В меню", callback_data="admin_main"))
    return text, b.as_markup()


@router.message(F.text == "👥 Пользователи")
@router.callback_query(F.data == "users_filter")
async def users_filter(event: Message | CallbackQuery) -> None:
    if not await _require_admin(event):
        return
    text, kb = await build_user_filter()
    if isinstance(event, CallbackQuery):
        await safe_edit(event.message, text=text, reply_markup=kb)
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb)


@router.callback_query(UserFilterCB.filter())
async def users_chosen(call: CallbackQuery, callback_data: UserFilterCB) -> None:
    if not await _require_admin(call):
        return
    text, kb = await build_users_page(0, callback_data.role)
    await safe_edit(call.message, text=text, reply_markup=kb)
    await call.answer()


@router.callback_query(UserPageCB.filter())
async def users_page(call: CallbackQuery, callback_data: UserPageCB) -> None:
    if not await _require_admin(call):
        return
    text, kb = await build_users_page(callback_data.page, callback_data.role)
    await safe_edit(call.message, text=text, reply_markup=kb)
    await call.answer()


@router.callback_query(UserViewCB.filter())
async def user_view(call: CallbackQuery, callback_data: UserViewCB) -> None:
    if not await _require_admin(call):
        return
    user = await get_user(callback_data.user_id)
    if not user:
        await call.answer("❌ Не найден", show_alert=True)
        return

    owned = await count_user_cards(callback_data.user_id)
    nick = display_name(user["nickname"], callback_data.user_id)
    role = user["role"] or "user"
    reg = datetime.fromtimestamp(user["registration"]).strftime("%d.%m.%Y %H:%M") if user["registration"] else "—"
    viewer_super = await is_superadmin(call.from_user.id)

    body = "\n".join([
        line("🆔", "ID", f"<code>{callback_data.user_id}</code>"),
        line("🎭", "Роль", role_display(role)),
        line("📅", "Регистрация", reg),
        line("🪙", "Монеты", f"<b>{fmt_num(user['coins'] or 0)}</b>"),
        line("🃏", "Карточек", f"<b>{fmt_num(owned)}</b>"),
        line("🔥", "Стрик", f"<b>{user['streak'] or 0}</b>"),
    ])
    caption = f"👤 <b>{esc(nick)}</b>\n{sep(20)}\n\n{bq(body)}"

    b = InlineKeyboardBuilder()
    b.button(text="🪙 Монеты", callback_data=UserActionCB(action="coins", user_id=callback_data.user_id).pack())
    b.button(text="⏱ Сброс CD", callback_data=UserActionCB(action="resetcd", user_id=callback_data.user_id).pack())

    if role == "banned":
        b.button(text="✅ Разбан", callback_data=UserActionCB(action="unban", user_id=callback_data.user_id).pack())
    elif role == "user":
        b.button(text="🚫 Бан", callback_data=UserActionCB(action="ban", user_id=callback_data.user_id).pack())
        if viewer_super:
            b.button(text="🛡 Админ", callback_data=UserActionCB(action="make_admin", user_id=callback_data.user_id).pack())
    elif role == "admin" and viewer_super:
        b.button(text="❌ Снять", callback_data=UserActionCB(action="unadmin", user_id=callback_data.user_id).pack())
        b.button(text="👑 Super", callback_data=UserActionCB(action="make_super", user_id=callback_data.user_id).pack())
        b.button(text="🚫 Бан", callback_data=UserActionCB(action="ban", user_id=callback_data.user_id).pack())
    elif role == "superadmin" and viewer_super and callback_data.user_id != call.from_user.id:
        b.button(text="❌ Снять", callback_data=UserActionCB(action="unadmin", user_id=callback_data.user_id).pack())

    b.button(text="‹ К списку", callback_data=UserPageCB(page=0, role=callback_data.role).pack())
    b.adjust(1)

    await safe_edit(call.message, text=caption, reply_markup=b.as_markup())
    await call.answer()


@router.callback_query(UserActionCB.filter())
async def user_action(call: CallbackQuery, callback_data: UserActionCB) -> None:
    if not await _require_admin(call):
        return
    action = callback_data.action
    target = callback_data.user_id
    viewer = call.from_user.id
    viewer_super = await is_superadmin(viewer)
    target_role = await get_user_role(target)

    if action == "ban":
        if target == viewer:
            await call.answer("❌ Нельзя забанить себя", show_alert=True)
            return
        if target_role == "superadmin" and not viewer_super:
            await call.answer("❌ Недостаточно прав", show_alert=True)
            return
        await set_user_role(target, "banned")
        await call.message.answer(f"🚫 <code>{target}</code> заблокирован", reply_markup=main_reply_kb())
        await call.answer("Готово")
    elif action == "unban":
        await set_user_role(target, "user")
        await call.message.answer(f"✅ <code>{target}</code> разблокирован", reply_markup=main_reply_kb())
        await call.answer("Готово")
    elif action == "make_admin":
        if not viewer_super:
            await call.answer("❌ Только superadmin", show_alert=True)
            return
        await set_user_role(target, "admin")
        await call.message.answer(f"🛡 <code>{target}</code> → администратор", reply_markup=main_reply_kb())
        await call.answer("Готово")
    elif action == "make_super":
        if not viewer_super:
            await call.answer("❌ Только superadmin", show_alert=True)
            return
        await set_user_role(target, "superadmin")
        await call.message.answer(f"👑 <code>{target}</code> → главный администратор", reply_markup=main_reply_kb())
        await call.answer("Готово")
    elif action == "unadmin":
        if not viewer_super:
            await call.answer("❌ Только superadmin", show_alert=True)
            return
        if target == viewer:
            await call.answer("❌ Нельзя снять с себя", show_alert=True)
            return
        await set_user_role(target, "user")
        await call.message.answer(f"✅ Права сняты с <code>{target}</code>", reply_markup=main_reply_kb())
        await call.answer("Готово")
    elif action == "coins":
        await call.message.answer(f"🪙 /setcoins {target} 1000")
        await call.answer()
    elif action == "resetcd":
        from db import get_db
        async with get_db() as db:
            await db.execute("UPDATE users SET last_claim = 0 WHERE user_id = ?", (target,))
        await call.message.answer(f"⏱ Кулдаун сброшен для <code>{target}</code>", reply_markup=main_reply_kb())
        await call.answer("Готово")
    else:
        await call.answer()


# ── COMMANDS ────────────────────────────────────────────────────────────────

@router.message(Command("setcoins"))
async def cmd_setcoins(message: Message, command: CommandObject) -> None:
    if not await _require_admin(message):
        return
    args = (command.args or "").strip().split()
    if not args:
        await message.reply("✏️ /setcoins [USERID] N")
        return
    try:
        if len(args) == 1:
            target = message.from_user.id
            coins = int(args[0])
        else:
            target = int(args[0])
            coins = int(args[1])
    except ValueError:
        await message.reply("❌ Нужны числа")
        return
    if coins < 0:
        await message.reply("❌ Не может быть отрицательным")
        return
    user = await get_user(target)
    if not user:
        await message.reply("❌ Пользователь не найден")
        return
    old = user["coins"] or 0
    await set_coins(target, coins)
    await message.reply(
        f"✅ Монеты <code>{target}</code>\n\n"
        + bq(line("🪙", "Было", fmt_num(old)) + chr(10) + line("🪙", "Стало", f"<b>{fmt_num(coins)}</b>"))
    )


@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject) -> None:
    if not await _require_admin(message):
        return
    arg = (command.args or "").strip()
    if not arg:
        await message.reply("✏️ /ban USERID")
        return
    try:
        target = int(arg)
    except ValueError:
        await message.reply("❌ USERID должен быть числом")
        return
    if target == message.from_user.id:
        await message.reply("❌ Нельзя забанить себя")
        return
    tr = await get_user_role(target)
    if tr in ("admin", "superadmin") and not await is_superadmin(message.from_user.id):
        await message.reply("❌ Недостаточно прав")
        return
    ok = await set_user_role(target, "banned")
    if not ok:
        await message.reply("❌ Пользователь не найден")
        return
    await message.reply(f"🚫 Заблокирован: <code>{target}</code>")


@router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject) -> None:
    if not await _require_admin(message):
        return
    arg = (command.args or "").strip()
    if not arg:
        await message.reply("✏️ /unban USERID")
        return
    try:
        target = int(arg)
    except ValueError:
        await message.reply("❌ USERID должен быть числом")
        return
    ok = await set_user_role(target, "user")
    if not ok:
        await message.reply("❌ Пользователь не найден")
        return
    await message.reply(f"✅ Разблокирован: <code>{target}</code>")


@router.message(Command("setadmin"))
async def cmd_setadmin(message: Message, command: CommandObject) -> None:
    if not await is_superadmin(message.from_user.id):
        await message.reply("❌ Только superadmin")
        return
    arg = (command.args or "").strip()
    if not arg:
        await message.reply("✏️ /setadmin USERID")
        return
    try:
        target = int(arg)
    except ValueError:
        await message.reply("❌ USERID должен быть числом")
        return
    ok = await set_user_role(target, "admin")
    if not ok:
        await message.reply("❌ Пользователь не найден")
        return
    await message.reply(f"✅ Администратор: <code>{target}</code>")


@router.message(Command("resetcd"))
async def cmd_resetcd(message: Message, command: CommandObject) -> None:
    if not await _require_admin(message):
        return
    args = (command.args or "").strip().split()
    target = message.from_user.id
    if args:
        try:
            target = int(args[0])
        except ValueError:
            await message.reply("❌ USERID должен быть числом")
            return
    from db import get_db
    async with get_db() as db:
        cur = await db.execute("SELECT 1 FROM users WHERE user_id = ?", (target,))
        if not await cur.fetchone():
            await message.reply("❌ Пользователь не найден")
            return
        await db.execute("UPDATE users SET last_claim = 0 WHERE user_id = ?", (target,))
    await message.reply(f"✅ Кулдаун сброшен: <code>{target}</code>")


@router.message(Command("delcard"))
async def cmd_delcard(message: Message, command: CommandObject) -> None:
    if not await _require_admin(message):
        return
    arg = (command.args or "").strip()
    if not arg:
        await message.reply("✏️ /delcard ID")
        return
    try:
        card_id = int(arg)
    except ValueError:
        await message.reply("❌ ID должен быть числом")
        return
    card = await get_card(card_id)
    if not card:
        await message.reply("❌ Карточка не найдена")
        return
    await delete_card(card_id)
    await message.reply(f"🗑 Удалена: {esc(card['name'])} (#{card_id})")


# ── STATS / TOP ─────────────────────────────────────────────────────────────

@router.message(F.text == "📊 Статистика")
@router.message(Command("stats"))
async def admin_stats(message: Message) -> None:
    if not await _require_admin(message):
        return
    from db import get_db
    async with get_db() as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        total_users = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM users WHERE role = 'banned'")
        banned = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM users WHERE role IN ('admin','superadmin')")
        staff = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM cards")
        total_cards = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COALESCE(SUM(coins),0) FROM users")
        total_coins = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM inventory")
        owned = (await cur.fetchone())[0]
        cur = await db.execute("SELECT rarity, COUNT(*) FROM cards GROUP BY rarity")
        by_r = dict(await cur.fetchall())

    rarity_lines = "\n".join(
        line(v["icon"], v["name"], fmt_num(by_r.get(k, 0)))
        for k, v in DEFAULT_RARITIES.items()
    )
    text = (
        f"📊 <b>Обзор</b>\n{sep(20)}\n\n"
        f"{bq(line('👥', 'Пользователей', f'<b>{fmt_num(total_users)}</b>') + chr(10) + line('🛡', 'Админов', f'<b>{fmt_num(staff)}</b>') + chr(10) + line('🚫', 'Забанено', f'<b>{fmt_num(banned)}</b>'))}\n\n"
        f"{bq(line('🃏', 'Карточек', f'<b>{fmt_num(total_cards)}</b>') + chr(10) + rarity_lines)}\n\n"
        f"{bq(line('🪙', 'Монет в игре', f'<b>{fmt_num(total_coins)}</b>') + chr(10) + line('📦', 'В коллекциях', f'<b>{fmt_num(owned)}</b>'))}"
    )
    await message.reply(text, reply_markup=main_reply_kb())


@router.message(F.text == "🏆 Топ")
@router.message(Command("top"))
async def admin_top(message: Message) -> None:
    if not await _require_admin(message):
        return
    from db import get_db
    async with get_db() as db:
        cur = await db.execute(
            """SELECT user_id, nickname, coins,
                      (SELECT COUNT(*) FROM inventory i WHERE i.user_id = u.user_id) AS cards
               FROM users u WHERE role != 'banned' ORDER BY coins DESC LIMIT 10"""
        )
        by_coins = await cur.fetchall()

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, r in enumerate(by_coins):
        medal = medals[i] if i < 3 else f"{i + 1}."
        nick = display_name(r["nickname"], r["user_id"])
        lines.append(f"{medal} {esc(nick)} — 🪙 {fmt_num(r['coins'] or 0)}")

    text = (
        f"🏆 <b>Топ по монетам</b>\n{sep(20)}\n\n"
        f"{bq(chr(10).join(lines) if lines else 'Пусто')}"
    )
    await message.reply(text, reply_markup=main_reply_kb())


@router.message(F.text == "🛠 Справка")
@router.message(Command("help"))
async def admin_help(message: Message) -> None:
    if not await _require_admin(message):
        return
    text = (
        "🛠 <b>Команды админа</b>\n" + sep() + "\n\n"
        "<b>Карточки</b>\n"
        "• Меню «➕ Добавить» — пошаговое добавление\n"
        "• /delcard ID — удалить\n\n"
        "<b>Пользователи</b>\n"
        "• /ban USERID · /unban USERID\n"
        "• /setadmin USERID (только super)\n"
        "• /setcoins [USERID] N\n"
        "• /resetcd [USERID]\n\n"
        "<b>Прочее</b>\n"
        "• /stats · /top\n"
        "• Меню для навигации"
    )
    await message.reply(text, reply_markup=main_reply_kb())


@router.callback_query(F.data == "ignore")
async def ignore_cb(call: CallbackQuery) -> None:
    await call.answer()
