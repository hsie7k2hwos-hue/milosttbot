# API + фото для бота «Мряу» (v2)

FastAPI-бэкенд для Telegram Mini App. Работает с той же SQLite, что и бот.

## Что изменилось в v2

- Удалены маркет, кристаллы, обмен и покупка
- Добавлен `POST /api/claim` — получение карточки из мини-приложения
- Топ и коллекция считают **уникальные** карточки (не сумму `amount`)
- Расширенная админка: CRUD карточек, просмотр/правка игроков, выдача карточек
- `GET /api/help` — справка

## Файлы

| Файл | Куда |
|------|------|
| `api.py` | **Корень репозитория бота** (заменить старый) |
| `card_photos.py` | Корень репо бота (если ещё нет) |
| `run_bot_and_api.py` | Корень репо бота (опционально, Bothost) |
| `BOT_PHOTOS_INTEGRATION.md` | Документация по фото |
| `requirements.txt` | Объединить с requirements бота |

## Запуск

```bash
pip install fastapi uvicorn[standard] aiosqlite pydantic

export BOT_TOKEN="..."           # тот же, что у бота
export DB_NAME="/app/data/cards_game.db"
export CORS_ORIGINS="https://твой-фронт.vercel.app,https://web.telegram.org"
export CARD_PHOTOS_DIR="/app/data/card_photos"   # опционально
export API_PORT=8080             # или PORT на Bothost

uvicorn api:app --host 0.0.0.0 --port 8080
```

На Bothost: точка входа `run_bot_and_api.py`, включи домен, порт 8080.

## Эндпоинты

| Метод | Путь | Auth | Описание |
|-------|------|------|----------|
| GET | `/health` | — | Проверка |
| GET | `/api/me` | initData | Профиль |
| GET | `/api/collection` | initData | Карточки (уникальные) |
| GET | `/api/top?kind=` | initData | Топ coins/cards/streak |
| POST | `/api/claim` | initData | Бесплатная карточка |
| GET | `/api/card/{id}` | initData | Одна карточка |
| GET | `/api/card/{id}/photo` | — | Фото |
| GET | `/api/help` | — | Справка |
| GET | `/api/admin/overview` | admin | Сводка |
| GET/POST | `/api/admin/cards` | admin | Список / создать |
| PATCH/DELETE | `/api/admin/cards/{id}` | admin | Изменить / удалить |
| GET | `/api/admin/users` | admin | Список игроков |
| GET/PATCH | `/api/admin/users/{id}` | admin | Детали / правка |
| POST | `/api/admin/users/{id}/give-card` | admin | Выдать карточку |

## Логика claim

1. Проверка кулдауна `last_claim` (4 часа)
2. Взвешенный random редкости (common 50 … legendary 3)
3. Случайная карточка этой редкости
4. INSERT/UPDATE inventory, +монеты по редкости, обновление стрика
5. Ответ с карточкой, стриком, новым кулдауном

Стрик: +1 если claim был «вчера» (UTC-день), иначе сброс в 1.

**Важно:** логика claim в API должна совпадать с ботом. Если в боте другие веса/награды — поправь константы `RARITIES` / `COOLDOWN_SECONDS` в `api.py` под бота.

## Фото

См. `BOT_PHOTOS_INTEGRATION.md` и `card_photos.py`:
- при добавлении карточки в боте — `sync_card_photo`
- одноразовая миграция — `/migrate_photos`
- API отдаёт `/api/card/{id}/photo` с диска

## Nginx (фрагмент)

```nginx
server {
    listen 443 ssl;
    server_name api.example.com;
    ssl_certificate     /etc/letsencrypt/live/api.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.example.com/privkey.pem;
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```
