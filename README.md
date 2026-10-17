# API для Telegram Mini App

Файл `api.py` кладётся **в репозиторий бота** (рядом с `main.py`, `game_config.py`, `user_bot.py`).

## Подключение

1. Скопируйте `api.py` в корень проекта бота.
2. В `.env` / переменных BotHost:

```env
START_API=1
# или
ENABLE_WEBAPP_API=1

API_PORT=8080
# BotHost обычно сам пробрасывает PORT

BOT_TOKEN=...          # обязателен для проверки initData
DB_NAME=/app/data/cards_game.db

# CORS: домен Vercel-приложения
CORS_ORIGINS=https://your-miniapp.vercel.app,https://web.telegram.org

# URL мини-приложения (для кнопок в боте)
WEBAPP_URL=https://your-miniapp.vercel.app

# Публичный URL этого API (для /check в админ-боте)
API_BASE=https://your-bothost-public-url
PUBLIC_API_BASE=https://your-bothost-public-url

# Кэш аватаров (как в admin_bot)
AVATARS_DIR=/app/data/user_avatars
```

3. Убедитесь, что в `requirements.txt` есть:

```
fastapi>=0.110.0
uvicorn[standard]>=0.27.0
```

4. Перезапустите процесс (`python main.py`). В логах должно быть:

```
API → 0.0.0.0:8080
```

Проверка: `GET https://your-api/health` → `{"ok":true,"db":true,...}`

## Эндпоинты

| Метод | Путь | Auth | Описание |
|-------|------|------|----------|
| GET | `/health` | — | Healthcheck |
| GET | `/api/rarities` | — | Редкости + счётчики |
| GET | `/api/cards` | — | Каталог (`rarity`, `page`, `limit`, `q`) |
| GET | `/api/cards/{id}` | — | Одна карточка |
| GET | `/api/cards/{id}/photo` | — | Файл фото (webp/jpg) |
| GET | `/api/top` | — | Топ (`kind=coins\|cards\|streak`) |
| GET | `/api/stats` | — | Общая статистика |
| GET | `/api/me` | Telegram | Профиль текущего пользователя |
| GET | `/api/me/collection` | Telegram | Коллекция текущего пользователя |
| GET | `/api/users/{id}` | — | Публичный профиль |

Auth: заголовок `X-Telegram-Init-Data` (значение `Telegram.WebApp.initData`).

Документация Swagger: `/docs`
