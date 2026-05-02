# Attention Switch - не пропусти сообщение любимой, чтобы она потом не обижалась.

Шуточное Windows-приложение на `PyQt6`: подключаешь Telegram, выбираешь один чат, и при новом сообщении приложение:

- сворачивает все окна;
- открывает Telegram Desktop через `tg://`;
- показывает большой тревожный баннер.

## Установка

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

## .env

Положи `.env` рядом с `app.py`, а после упаковки в `exe` держи его рядом с `.exe`.

```env
ATTN_API_ID=
ATTN_API_HASH=
ATTN_PHONE=
ATTN_SESSION_NAME=
ATTN_MONITORED_CHAT_ID=
ATTN_MONITORED_CHAT_TITLE=
ATTN_ALERT_TEXT="ВНИМАНИЕ, ПИШЕТ ЛЮБИМАЯ ДЕВУШКА, СРОЧНО СВЕРНИ ВСЕ ДЕЛА И ПОГОВОРИ С НЕЙ"
```

`ATTN_MONITORED_CHAT_ID` можно не заполнять: проще один раз выбрать чат в интерфейсе, и приложение само сохранит выбор локально.

## Что нужно заранее

1. Установленный Telegram Desktop с зарегистрированным `tg://`-обработчиком.
2. `API ID` и `API Hash` из `https://my.telegram.org/apps`.

## Примечания

- Сессия Telegram и конфиг сохраняются в `%USERPROFILE%\.attention_switch`.
- Для публичных чатов и каналов приложение открывает точный deep link на сообщение.
- Для некоторых редких диалогов без username/phone Telegram может открыться просто на основном окне клиента.
