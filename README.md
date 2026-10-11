# API + боты «Мряу» (v2.1)

FastAPI-бэкенд и Telegram-боты (user + admin) с общей SQLite.

## Что изменилось

- **Дуэли удалены** из пользовательского бота и БД
- **Новые редкости:** Обычная, Редкая, Необычная, Ценная, Сверхценная, Легендарная  
  (веса 40/25/15/10/7/3, награды 8/20/40/70/110/200)
- **Рамки** на фото карточек (цвет по редкости, Pillow)
- **UUID-имена** файлов фото — нельзя перебрать по `card_id`
- **Superadmin** только из `SUPERADMIN_IDS` в `.env` (не «первый пользователь»)
- Админ: `/check`, `/balance_rarities`, `/rename_rarities`, `/frame_all`, `/migrate_uuid`
- При добавлении карточки — рекомендация редкости по балансу
- Упрощены API и схема (без `amount`, без `duels`, без gender)

## Файлы

| Файл | Назначение |
|------|------------|
| `bot.py` | Пользовательский бот |
| `admin_bot.py` | Админ-бот |
| `api.py` | Mini App API |
| `card_photos.py` | Фото, UUID, рамки |
| `requirements.txt` | Зависимости |

## Env

```bash
BOT_TOKEN=...
ADMIN_BOT_TOKEN=...
SUPERADMIN_IDS=123456789          # через запятую, если нужно несколько
DB_NAME=/app/data/cards_game.db
CARD_PHOTOS_DIR=/app/data/card_photos
CORS_ORIGINS=https://your-front.vercel.app,https://web.telegram.org
WEBAPP_URL=https://your-front.vercel.app
API_BASE=https://api.example.com  # для /check
START_API=1                       # поднять API вместе с ботом
API_PORT=8080
```

## Запуск

```bash
pip install -r requirements.txt
export BOT_TOKEN=... ADMIN_BOT_TOKEN=... SUPERADMIN_IDS=...
python bot.py          # user + admin + optional API
# или отдельно:
uvicorn api:app --host 0.0.0.0 --port 8080
python admin_bot.py
```

## Миграция со старой версии

1. `/rename_rarities` — epic→uncommon, mythical→valuable  
2. `/migrate_photos` — скачать file_id на диск  
3. `/migrate_uuid` — переименовать `{id}.jpg` → `{uuid}.jpg`  
4. `/frame_all` — наложить цветные рамки  
5. `/balance_rarities` — подогнать доли редкостей под веса  
6. `/check` — проверить БД, таблицы, API, Pillow

## Редкости

| Ключ | Название | Вес | Монеты | Цвет рамки |
|------|----------|-----|--------|------------|
| common | Обычная | 40 | 8 | серый |
| rare | Редкая | 25 | 20 | синий |
| uncommon | Необычная | 15 | 40 | зелёный |
| valuable | Ценная | 10 | 70 | фиолетовый |
| super_valuable | Сверхценная | 7 | 110 | оранжевый |
| legendary | Легендарная | 3 | 200 | золотой |
