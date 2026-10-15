"""
Клавиатуры пользовательского бота.
"""
from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.filters.callback_data import CallbackData

from config import BOT_USERNAME


# ── CallbackData ────────────────────────────────────────────────────────────

class CollectionCB(CallbackData, prefix="coll"):
    rarity: str
    page: int = 0
    user_id: int = 0


class MainMenuCB(CallbackData, prefix="main"):
    user_id: int = 0


class BackProfileCB(CallbackData, prefix="bprof"):
    user_id: int = 0


class TopCB(CallbackData, prefix="top"):
    kind: str  # coins | cards | streak


class TopRefreshCB(CallbackData, prefix="topr"):
    kind: str


class NickConfirmCB(CallbackData, prefix="nick"):
    action: str  # apply | reset | cancel


class OkDelCB(CallbackData, prefix="ok"):
    pass


class HelpCB(CallbackData, prefix="help"):
    tab: str = "cards"


class CardActionCB(CallbackData, prefix="cact"):
    action: str
    user_id: int = 0


# ── Reply keyboards ─────────────────────────────────────────────────────────

def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🃏 Получить карточку"), KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="🏆 Топ"), KeyboardButton(text="🎲 Кубик")],
            [KeyboardButton(text="❓ Помощь")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите действие…",
    )


# ── Inline keyboards ────────────────────────────────────────────────────────

def profile_kb(owner_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🃏 Мои карточки", callback_data=MainMenuCB(user_id=owner_id).pack())
    b.adjust(1)
    return b.as_markup()


def after_card_kb(user_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(
        text="🃏 Мои карточки",
        callback_data=CardActionCB(action="collection", user_id=user_id).pack(),
    )
    b.adjust(1)
    return b.as_markup()


def top_kb(kind: str = "coins") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for k, label in (("coins", "🪙"), ("cards", "🃏"), ("streak", "🔥")):
        mark = "● " if kind == k else "○ "
        b.button(text=f"{mark}{label}", callback_data=TopCB(kind=k).pack())
    b.button(text="↻ Обновить", callback_data=TopRefreshCB(kind=kind).pack())
    b.adjust(3, 1)
    return b.as_markup()


def ok_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Понятно", callback_data=OkDelCB().pack())
    return b.as_markup()


def help_kb(active: str) -> InlineKeyboardMarkup:
    tabs = [
        ("cards", "🃏 Карточки"),
        ("coins", "🪙 Монеты"),
        ("streak", "🔥 Стрик"),
        ("games", "🎲 Кубик"),
        ("profile", "👤 Профиль"),
    ]
    b = InlineKeyboardBuilder()
    for key, label in tabs:
        mark = "✓ " if key == active else ""
        b.button(text=f"{mark}{label}", callback_data=HelpCB(tab=key).pack())
    b.adjust(2, 2, 1)
    return b.as_markup()


def nick_confirm_kb(action: str = "apply") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✓ Подтвердить", callback_data=NickConfirmCB(action=action).pack())
    b.button(text="✕ Отмена", callback_data=NickConfirmCB(action="cancel").pack())
    b.adjust(2)
    return b.as_markup()
