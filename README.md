# Мряу v2.3

## Запуск
```bash
pip install -r requirements.txt
export BOT_TOKEN=... ADMIN_BOT_TOKEN=... SUPERADMIN_IDS=...
python main.py
```

## Редкости (дефолт)
| key | имя | вес | награда |
|-----|-----|-----|---------|
| common | Обычная | 38 | +10 |
| uncommon | Необычная | 20 | +40 |
| valuable | Ценная | 12 | +70 |
| super_valuable | Сверхценная | 8 | +120 |
| legendary | Легендарная | 4 | +200 |
| negative | Негативная | 18 | **−20** |

`rare` удалена. Миграция: `/remove_rare` (rare→uncommon + redraw + дефолт конфиг).

## Файлы
- `main.py` — точка входа
- `user_bot.py` — user-бот (бывший bot.py)
- `admin_bot.py` — админ
- `api.py` — Mini App API
- `card_photos.py` — UUID + рамки
- `game_config.py` — константы / rarity_config

## Админ
```
/remove_rare      — убрать rare, баланс, redraw
/checkbalance · /setrarity · /resetrarities
/redraw_cards · /recalc_balances
/clear_logs · /backup · /userbot on|off
```
