# Sora Bot — Telegram-бот для генерации видео

Telegram-бот, который генерирует видео через веб‑интерфейс Sora (https://sora.chatgpt.com) с использованием ваших авторизационных cookie.

## Требования
- Python 3.10+ (рекомендовано)
- Аккаунт с доступом к Sora 2
- Telegram‑бот и токен от @BotFather

## Установка

1) Клонируйте репозиторий и перейдите в папку проекта
```bash
git clone https://github.com/ushan0v/sora-bot
cd sora-bot
```

2) Создайте и активируйте виртуальное окружение
- Windows (PowerShell):
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```
- macOS/Linux (bash/zsh):
```bash
python3 -m venv .venv
source .venv/bin/activate
```

3) Обновите pip и установите зависимости
```bash
pip install -U pip
pip install -r requirements.txt
```

4) Установите браузеры для Playwright (нужно один раз)
```bash
python -m playwright install chromium
```

5) Подготовьте файл окружения
- Создай файл `.env` и заполните по шаблону.
- Обязателен только `BOT_TOKEN`. Прокси (`PROXY_URL`) — опционально.

Содержимое `.env`:
```env
# Токен вашего Telegram-бота от @BotFather (обязательно)
BOT_TOKEN=PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE

# Необязательный прокси для запросов
# Примеры:
#   http://user:pass@host:8080
#   socks5://user:pass@host:1080
PROXY_URL=
```

6) Экспортируйте cookie из sora.chatgpt.com
- Авторизуйтесь в браузере на https://sora.chatgpt.com
- Экспортируйте cookie в JSON (удобно через расширения типа “Cookie Editor”)
- Сохраните экспорт в файл `cookies.json` в корне проекта

## Запуск
```bash
python main.py
```