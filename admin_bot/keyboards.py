"""
Клавиатуры админ-бота.
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

from config import DEFAULT_RARITIES, ROLES


class CardFilterCB(CallbackData, prefix="acf"):
    rarity: str


class CardPageCB(CallbackData, prefix="acp"):
    page: int
    rarity: str = "all"


class CardManageCB(CallbackData, prefix="acm"):
    card_id: int
    rarity: str = "all"


class CardRarityCB(CallbackData, prefix="acr"):
    card_id: int
    rarity: str


class UserFilterCB(CallbackData, prefix="auf"):
    role: str


class UserPageCB(CallbackData, prefix="aup"):
    page: int
    role: str = "all"


class UserViewCB(CallbackData, prefix="auv"):
    user_id: int
    role: str = "all"


class UserActionCB(CallbackData, prefix="aua"):
    action: str
    user_id: int


class StatsCB(CallbackData, prefix="ast"):
    section: str = "overview"


def main_reply_kb(fsm: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="➕ Добавить"), KeyboardButton(text="📜 Карточки")],
        [KeyboardButton(text="👥 Пользователи"), KeyboardButton(text="📊 Статистика")],
        [KeyboardButton(text="🏆 Топ"), KeyboardButton(text="🛠 Справка")],
    ]
    if fsm:
        rows.append([KeyboardButton(text="❌ Отмена")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите раздел…",
    )


def rarity_kb(prefix: str = "set_rarity") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key, val in DEFAULT_RARITIES.items():
        b.button(text=f"{val['icon']} {val['name']}", callback_data=f"{prefix}:{key}")
    b.button(text="✕ Отмена", callback_data="cancel_add")
    b.adjust(1)
    return b.as_markup()
