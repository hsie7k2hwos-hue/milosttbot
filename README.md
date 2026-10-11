# Мряу v2.2

User-бот + admin-бот + Mini App API, общая SQLite.

## Запуск

```bash
pip install -r requirements.txt
export BOT_TOKEN=... ADMIN_BOT_TOKEN=... SUPERADMIN_IDS=123456789
python main.py          # единая точка входа
```

Или по отдельности: `python bot.py` / `python admin_bot.py` / `uvicorn api:app`.

## Новое в 2.2

- **Рамка карточки:** цветная окантовка + ромбы в углах + звёзды (1–6 по редкости)
- **`rarity_config` в БД** — веса и награды: `/setrarity`, `/resetrarities`, `/checkbalance`
- **`/redraw_cards`** — перерисовка всех фото под текущую редкость
- **`/recalc_balances`** — монеты = сумма наград карточек в коллекции
- **`/userbot on|off`** — выключить user-бот (нужен рестарт main.py)
- **`/backup` · `/export_db` · `/import_db`** — бэкап и обмен БД
- **Стрик:** 2→10, 7→15, 14→20, 30→30, 60→40
- **Экономика:** ник 120, кубик −15…+25, карты 10…200
- **`main.py`** — проверка окружения и запуск всего

## Файлы

| Файл | Роль |
|------|------|
| main.py | Точка входа |
| bot.py | User-бот |
| admin_bot.py | Админ |
| api.py | Mini App API |
| card_photos.py | UUID + рамки |
| game_config.py | Константы и БД-конфиг |

## Env

```
BOT_TOKEN=
ADMIN_BOT_TOKEN=
SUPERADMIN_IDS=123456789
DB_NAME=/app/data/cards_game.db
CARD_PHOTOS_DIR=/app/data/card_photos
START_API=0
API_PORT=8080
```

## Админ-команды баланса

```
/checkbalance
/setrarity common weight 35
/setrarity legendary reward 250
/resetrarities
/balance_rarities
/redraw_cards
/recalc_balances
/userbot off|on
/backup
/export_db
/import_db   (файл .db + superadmin)
/check
```
