# Фото карточек: хранение на диске + показ в мини-аппке

## Зачем

- `file_id` Telegram **нельзя** показать в браузере
- `file_id` может протухнуть — картинки пропадут
- Решение: при добавлении карточки **скачивать** файл на диск (`/app/data/card_photos/`) и отдавать через API

Vercel для картинок **не обязателен**: на Bothost папка рядом с БД (`/app/data`) обычно сохраняется. Vercel Blob — запасной вариант, если диск эфемерный.

---

## 1. Файлы в репозиторий бота

Скопируй в корень репо бота (рядом с `api.py`):

- `card_photos.py`
- обновлённый `api.py`

---

## 2. Колонка в БД

При первом запуске API/миграции добавится:

```sql
ALTER TABLE cards ADD COLUMN photo_path TEXT;
```

`photo_id` **оставь** — бот по-прежнему шлёт фото в чат через Telegram.

---

## 3. При добавлении карточки (aiogram)

После `INSERT INTO cards ...` получи `lastrowid` и сохрани файл:

```python
from card_photos import sync_card_photo

# ... после INSERT
card_id = cur.lastrowid  # или SELECT last_insert_rowid()
path = await sync_card_photo(message.bot, card_id, photo_id)
await db.execute(
    "UPDATE cards SET photo_path = ? WHERE id = ?",
    (path, card_id),
)
```

То же при смене фото (`EditCardPhotoSG`).

---

## 4. Одноразовая миграция старых карточек

Добавь админ-команду:

```python
from card_photos import migrate_all_card_photos

@router.message(Command("migrate_photos"), admin_filter)
async def cmd_migrate_photos(message: Message):
    async with get_db() as db:
        ok, fail = await migrate_all_card_photos(message.bot, db)
    await message.reply(f"✅ Скачано/уже было: {ok}\n❌ Ошибок: {fail}")
```

В Telegram напиши `/migrate_photos` (от админа).  
Подожди — для каждой карточки один запрос к Telegram.

---

## 5. Мини-аппка

Фронт уже использует `photo_url` → `https://твой-api.bothost.ru/api/card/12/photo`.

После миграции и редеплоя картинки появятся в коллекции.

---

## Проверка

```bash
# на диске бота
ls /app/data/card_photos/

# в браузере
curl -I https://ТВОЙ-ДОМЕН.bothost.ru/api/card/1/photo
# 200 OK, content-type image/jpeg
```

---

## Vercel (если очень нужно)

Можно грузить в [Vercel Blob](https://vercel.com/docs/storage/vercel-blob) при добавлении карточки и писать публичный URL в `photo_path`.  
Сложнее (токен Vercel, лимиты). Для Bothost достаточно локальной папки `card_photos`.
