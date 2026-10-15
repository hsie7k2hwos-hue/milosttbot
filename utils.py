"""
Общие утилиты: форматирование, экранирование, валидация.
"""
from __future__ import annotations

import html
import re
import time
from typing import Any, Optional

from config import ROLES

NICKNAME_RE = re.compile(r"^[\w\-. ]{2,32}$", re.UNICODE)
URL_RE = re.compile(r"(https?://|t\.me/|@\w+)", re.IGNORECASE)


def esc(text: Any) -> str:
    return html.escape(str(text), quote=False)


def fmt_num(n: Any) -> str:
    try:
        return f"{int(n):,}".replace(",", "\u00a0")
    except (TypeError, ValueError):
        return str(n)


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 100 in (11, 12, 13, 14):
        return many
    last = n % 10
    if last == 1:
        return one
    if last in (2, 3, 4):
        return few
    return many


def fmt_coins(n: int) -> str:
    return f"{fmt_num(n)} {plural(n, 'монета', 'монеты', 'монет')}"


def fmt_days(n: int) -> str:
    return f"{fmt_num(n)} {plural(n, 'день', 'дня', 'дней')}"


def fmt_remaining(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h} ч {m} мин"
    if m > 0:
        return f"{m} мин" if s == 0 else f"{m} мин {s} сек"
    return f"{s} сек"


def line(emoji: str, label: str, value: Any) -> str:
    return f"{emoji} <b>{label}</b> · {value}"


def bq(text: str) -> str:
    return f"<blockquote>{text}</blockquote>"


def sep(width: int = 18) -> str:
    return "─" * width


def role_display(role: str) -> str:
    info = ROLES.get(role, ROLES["user"])
    return f"{info['icon']} {info['name']}"


def display_name(
    nickname: Optional[str],
    user_id: int,
    fallback: Optional[str] = None,
) -> str:
    if nickname and str(nickname).strip():
        return str(nickname).strip()
    if fallback and str(fallback).strip():
        return str(fallback).strip()[:32]
    return str(user_id)


def validate_nickname(raw: str) -> Optional[str]:
    raw = raw.strip()
    if not (2 <= len(raw) <= 32):
        return None
    if URL_RE.search(raw):
        return None
    if not NICKNAME_RE.match(raw):
        return None
    return raw


def now_ts() -> int:
    return int(time.time())
