# Мряу Mini App — реальные данные из БД бота

Мини-аппка читает профиль, коллекцию, топ и маркет из той же SQLite, что и бот.

```
Telegram → Mini App (HTML на Vercel / GitHub Pages)
                ↓ HTTPS + initData
         api.py (FastAPI на VPS рядом с ботом)
                ↓
         cards_game.db
```

---

## 1. Запуск API (на сервере бота)

```bash
pip install fastapi uvicorn aiosqlite

export BOT_TOKEN="123456:ABC..."              # токен бота (проверка подписи)
export DB_NAME="/app/data/cards_game.db"      # путь к БД бота
export CORS_ORIGINS="https://твой-фронт.vercel.app,https://web.telegram.org"
export API_PORT=8080

cd meow-miniapp
uvicorn api:app --host 0.0.0.0 --port 8080
```

Проверка: `curl http://127.0.0.1:8080/health`

API снаружи должен быть по **HTTPS** (Nginx + Let's Encrypt или Cloudflare Tunnel).

### Nginx (фрагмент)

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

---

## 2. Фронтенд

В `index.html` укажи URL API:

```html
<script>
  window.MEOW_API_BASE = "https://api.example.com";
</script>
```

Залей папку на Vercel или GitHub Pages (только статика).

**Vercel:** New Project → загрузить папку → Framework Other → Deploy.

**GitHub Pages:** push в репозиторий → Settings → Pages → branch main / root.

---

## 3. Кнопка в боте (aiogram 3)

```python
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

WEBAPP_URL = "https://meow-xxx.vercel.app"

def webapp_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🃏 Открыть мини-аппку",
            web_app=WebAppInfo(url=WEBAPP_URL),
        )
    ]])

# в /start:
await message.answer(
    "Открой мини-аппку — коллекция и топ:",
    reply_markup=webapp_kb(),
)
```

Или Menu Button в BotFather → Bot Settings → Menu Button → URL фронта.

---

## API

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/health` | Проверка |
| GET | `/api/me` | Профиль |
| GET | `/api/collection` | Коллекция |
| GET | `/api/top?kind=coins\|cards\|streak` | Топ-10 |
| GET | `/api/market` | Маркет |
| POST | `/api/market/exchange` | `{"amount":5}` — обмен 🪙→💎 |
| POST | `/api/market/buy` | `{"card_id":12}` |
| GET | `/api/card/{id}` | Одна карточка |

Заголовок для всех `/api/*`:
```
X-Telegram-Init-Data: <значение tg.initData>
```

---

## Что видно в аппке

- Профиль: ник, роль, пол, монеты, кристаллы, стрик, кулдаун
- Коллекция: карточки из inventory, фильтры по редкости
- Маркет: недостающие по редкостям, обмен монет на кристаллы
- Топ: из users / inventory
- Получение карточки и кубик — в чате бота («мряу»)

Фото карточек в БД — Telegram file_id; в UI показываются эмодзи по редкости (без отдельного прокси файлов).

---

## Файлы

```
meow-miniapp/
├── index.html   # UI + MEOW_API_BASE
├── styles.css
├── app.js       # запросы с initData
├── api.py       # FastAPI → SQLite
├── vercel.json
└── README.md
```

## Проблемы

| Симптом | Что сделать |
|---------|-------------|
| API не настроен | Задать `window.MEOW_API_BASE` |
| Открой из Telegram | Жмать кнопку Web App в боте |
| 401 подпись | BOT_TOKEN API = токен того же бота |
| CORS | Добавить домен фронта в CORS_ORIGINS |
| Нет HTTPS у API | Telegram не пустит запросы |
