# Карточный бот (user + admin)

Одна точка входа (`bot.py`), **одна SQLite-база** (WAL).  
Админ-бот поднимается в том же процессе, если задан `ADMIN_BOT_TOKEN`.

## Файлы

| Файл | Назначение |
|------|------------|
| `bot.py` | Точка входа: user-бот (+ admin параллельно) |
| `admin_bot.py` | Логика админ-бота (импортируется из bot.py) |
| `card_photos.py` | Скачивание фото на диск |

## .env

```env
# Пользовательский бот
BOT_TOKEN=123:AAA
BOT_USERNAME=milosttbot
WEBAPP_URL=https://your-miniapp.example.com

# Админ-бот (другой бот в @BotFather) — тот же процесс
ADMIN_BOT_TOKEN=456:BBB

# Общая БД и фото
DB_NAME=/app/data/cards_game.db
CARD_PHOTOS_DIR=/app/data/card_photos
LOG_PATH=/app/data/bot.log
ADMIN_LOG_PATH=/app/data/admin_bot.log
```

## Запуск

```bash
# единственная команда на хостинге
python bot.py
```

Нужны оба файла рядом: `bot.py` и `admin_bot.py` (+ `card_photos.py`).

## Что изменилось

1. **Нет дубликатов** — только новые карточки; колонка `amount` не используется.
2. **Маркет / кристаллы / гемы** — удалены.
3. **Кулдаун 3 часа**; сброс CD только в админ-боте.
4. **Пол / gender** — удалены.
5. **Ник** по умолчанию `NULL` → показ имени Telegram или ID; смена **300** 🪙.
6. **Мини-приложение** — кнопка только после карточки (+ reply-клавиатура 2×2).
7. **Эффект 🎉** на mythical/legendary (ЛС).
8. **Админка** вынесена в `admin_bot.py`.
9. **Дуэли** `/duel <ставка>` ответом в группе, таблица `duels`, 10 мин, 2×dice, банк 100%.
10. Формат текста: `{emoji} {label}: {value}`.
11. Reply-клавиатура: карточка / профиль / топ / мини-апп.
12–13. Фото карточек и дефолт-аватар → диск (`/migrate_photos`, `/sync_default_avatar` в админ-боте).
14. Стрик — **вторым сообщением**; в группах авто-удаление через 30 с.
15. Упоминания (`tg://user`) убраны.

## Первый супер-админ

Первый, кто напишет `/start` **user-боту**, получает `superadmin` в БД. Дальше права — через админ-бота (`/setadmin`).

## Тестовые команды (админ-бот)

- `/migrate_photos` — все `photo_id` → `card_photos/{id}.jpg` + `photo_path` в БД  
- `/sync_default_avatar` — дефолтный аватар на диск  
- `/getfileid` + фото — показать file_id  
