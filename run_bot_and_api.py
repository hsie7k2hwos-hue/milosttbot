"""
Запуск бота + API мини-аппки в одном процессе (для Bothost).

1. Положи этот файл и api.py в КОРЕНЬ репозитория бота (рядом с основным .py).
2. В requirements.txt добавь:
      fastapi
      uvicorn
      aiosqlite
3. В Bothost: включи «Использовать домен», порт = 8080 (или как в PORT).
4. Точка входа (main file) укажи: run_bot_and_api.py
   ИЛИ в конце своего main.py вызови то же, что в main() ниже.

Переменные (Bothost сам даёт BOT_TOKEN):
  BOT_TOKEN  — уже есть
  DB_NAME    — путь к базе, например /app/data/cards_game.db
  PORT       — Bothost подставляет сам
"""

from __future__ import annotations

import asyncio
import os
import sys

# ── путь к БД: подстрой под свой бот ──
# В исходном боте было: DB_NAME = os.getenv("DB_NAME", "/app/data/cards_game.db")
os.environ.setdefault("DB_NAME", os.getenv("DB_NAME", "/app/data/cards_game.db"))


async def run_api():
    """Поднимает FastAPI (api.py) на 0.0.0.0:PORT."""
    import uvicorn

    port = int(os.getenv("PORT", "8080"))
    config = uvicorn.Config(
        "api:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        # Bothost проксирует HTTPS снаружи
    )
    server = uvicorn.Server(config)
    await server.serve()


async def run_bot():
    """
    Запуск твоего aiogram-бота.
    ВАЖНО: замени импорт на свой главный модуль.
    Если у тебя файл bot.py / main.py с функцией main() — подставь его.
    """
    # --- ВАРИАНТ A: весь бот в одном файле, например bot.py с async def main() ---
    # from bot import main as bot_main
    # await bot_main()

    # --- ВАРИАНТ B: как в твоём pasted коде — asyncio.run(main()) в if __name__ ---
    # Скопируй тело main() из своего файла бота сюда ИЛИ импортируй:

    try:
        # Популярные имена главного файла:
        for mod_name in ("bot", "main", "app", "__main__"):
            try:
                mod = __import__(mod_name)
                if hasattr(mod, "main") and asyncio.iscoroutinefunction(mod.main):
                    await mod.main()
                    return
            except ImportError:
                continue
    except Exception as e:
        print(f"[run_bot] не удалось импортировать main бота: {e}", file=sys.stderr)
        raise

    print(
        "[run_bot] Не найден async def main() в bot.py / main.py.\n"
        "Открой run_bot_and_api.py и в run_bot() явно импортируй свой бот.",
        file=sys.stderr,
    )
    # Не падаем — API всё равно может работать
    await asyncio.Event().wait()


async def combined():
    # API и бот параллельно
    await asyncio.gather(
        run_api(),
        run_bot(),
    )


if __name__ == "__main__":
    asyncio.run(combined())
