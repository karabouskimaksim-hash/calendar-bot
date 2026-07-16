# -*- coding: utf-8 -*-
# ВЕРСИЯ 4: публичный многопользовательский бот.
# Каждый пользователь подключает СВОЙ Яндекс Календарь командой /connect.
# Пароли шифруются, данные хранятся в файле-базе users.db.

import os
import re
import sqlite3
from datetime import datetime, timedelta

import caldav
from cryptography.fernet import Fernet
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ========================= НАСТРОЙКИ =========================

# Токен читаем из "переменной окружения" BOT_TOKEN (так задают секреты
# на хостинге). Если её нет — например, при запуске дома — берём запасной
# вариант из кавычек ниже.
TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН_ДЛЯ_ЗАПУСКА_ДОМА")

# Ссылка для донатов (Boosty, YooMoney и т.п.). Впиши свою, когда появится.
DONATE_URL = "https://boosty.to/ВПИШИ_СВОЮ_СТРАНИЦУ"

EVENT_DURATION_MINUTES = 60

# =============================================================

# --- Где хранить данные ---
# Хостинг через переменную DATA_DIR указывает "вечную" папку, которая
# переживает обновления кода. Дома переменной нет — храним рядом с кодом.
DATA_DIR = os.environ.get("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
KEY_PATH = os.path.join(DATA_DIR, "secret.key")
DB_PATH = os.path.join(DATA_DIR, "users.db")

# --- Шифрование: при первом запуске создаём секретный ключ ---
if not os.path.exists(KEY_PATH):
    with open(KEY_PATH, "wb") as f:
        f.write(Fernet.generate_key())
    print("Создал новый файл secret.key — храни его и никому не отдавай!")

with open(KEY_PATH, "rb") as f:
    fernet = Fernet(f.read())

# --- База данных: файл users.db создастся сам ---
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute(
    """CREATE TABLE IF NOT EXISTS users (
        tg_id INTEGER PRIMARY KEY,   -- id пользователя в Telegram
        login TEXT,                  -- логин Яндекса
        enc_password BLOB,           -- зашифрованный пароль приложения
        calendar_url TEXT,           -- адрес выбранного календаря
        calendar_name TEXT           -- его название (для красоты)
    )"""
)
db.commit()


def save_user(tg_id, login, password, cal_url, cal_name):
    """Сохранить (или обновить) пользователя. Пароль шифруем!"""
    enc = fernet.encrypt(password.encode())
    db.execute(
        "INSERT OR REPLACE INTO users VALUES (?, ?, ?, ?, ?)",
        (tg_id, login, enc, cal_url, cal_name),
    )
    db.commit()


def load_user(tg_id):
    """Достать пользователя из базы. Вернёт None, если не подключён."""
    row = db.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)).fetchone()
    if row is None:
        return None
    tg_id, login, enc, cal_url, cal_name = row
    password = fernet.decrypt(enc).decode()  # расшифровываем
    return {"login": login, "password": password,
            "cal_url": cal_url, "cal_name": cal_name}


def delete_user(tg_id):
    db.execute("DELETE FROM users WHERE tg_id = ?", (tg_id,))
    db.commit()


# --- Умный разбор даты и времени ---

WEEKDAYS = {  # "стемы" слов, чтобы ловить разные окончания (пятница/пятницу)
    "понедельник": 0, "вторник": 1, "сред": 2, "четверг": 3,
    "пятниц": 4, "суббот": 5, "воскресен": 6,
}


def parse_when(text):
    """Превращает текст вроде 'завтра 18:00' или '20.07 18:30'
    в дату-время. Возвращает datetime или None, если не понял."""
    text = text.lower().strip()
    now = datetime.now()

    # 1) Ищем время в формате ЧЧ:ММ
    m = re.search(r"(\d{1,2}):(\d{2})", text)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        return None

    # 2) Ищем дату: сначала словами...
    if "послезавтра" in text:
        day = now + timedelta(days=2)
    elif "завтра" in text:
        day = now + timedelta(days=1)
    elif "сегодня" in text:
        day = now
    else:
        # ...потом день недели ("в пятницу")
        day = None
        for stem, target in WEEKDAYS.items():
            if stem in text:
                ahead = (target - now.weekday()) % 7
                candidate = now + timedelta(days=ahead)
                # если это сегодня, но время уже прошло — берём через неделю
                if ahead == 0 and (hour, minute) <= (now.hour, now.minute):
                    candidate += timedelta(days=7)
                day = candidate
                break
        # ...и наконец формат 20.07 или 20.07.2026
        if day is None:
            d = re.search(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?", text)
            if d is None:
                return None
            try:
                day = datetime(
                    year=int(d.group(3)) if d.group(3) else now.year,
                    month=int(d.group(2)),
                    day=int(d.group(1)),
                )
            except ValueError:  # например, 32.13
                return None

    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


# --- Подключение к календарю конкретного пользователя ---

def get_user_calendar(user):
    """Создаёт подключение к выбранному календарю пользователя."""
    client = caldav.DAVClient(
        url="https://caldav.yandex.ru",
        username=user["login"],
        password=user["password"],
    )
    return client.calendar(url=user["cal_url"])


# ================= КОМАНДЫ И ДИАЛОГИ =================

# Номера шагов диалогов
WAITING_DATE = 1
C_LOGIN, C_PASSWORD, C_CALENDAR = 10, 11, 12


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! 📅 Я превращаю заметки в события Яндекс Календаря.\n\n"
        "1️⃣ Подключи свой календарь: /connect\n"
        "2️⃣ Пришли или перешли мне заметку\n"
        "3️⃣ Ответь, на когда — «завтра 18:00», «в пятницу 09:30» "
        "или «20.07 18:30»\n\n"
        "Другие команды:\n"
        "/disconnect — отключить календарь и удалить свои данные\n"
        "/donate — поддержать проект ❤️"
    )


async def donate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Спасибо, что хочешь поддержать бота! ❤️\n{DONATE_URL}"
    )


async def disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    delete_user(update.effective_user.id)
    await update.message.reply_text(
        "Готово: календарь отключён, твои данные удалены из базы. 👋"
    )


# ----- Диалог подключения календаря (/connect) -----

async def connect_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Подключаем твой Яндекс Календарь! Это займёт пару минут.\n\n"
        "Мне понадобится ПАРОЛЬ ПРИЛОЖЕНИЯ (не основной пароль!) — "
        "специальный пароль только для календаря, его можно отозвать "
        "в любой момент. Как создать:\n"
        "1. Зайди на id.yandex.ru → «Безопасность» → «Пароли приложений»\n"
        "2. Создай пароль типа «Календарь CalDAV»\n"
        "3. Скопируй его\n\n"
        "🔒 Пароль я храню в зашифрованном виде и использую только "
        "для создания событий. Удалить данные: /disconnect\n\n"
        "Для начала напиши свой логин Яндекса (например, ivanov@yandex.ru).\n"
        "Отмена: /cancel"
    )
    return C_LOGIN


async def connect_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_login"] = update.message.text.strip()
    await update.message.reply_text(
        "Теперь пришли пароль приложения. Я сразу удалю твоё сообщение "
        "из чата для безопасности."
    )
    return C_PASSWORD


async def connect_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    try:
        await update.message.delete()  # стираем пароль из переписки
    except Exception:
        pass

    login = context.user_data["new_login"]
    msg = await update.effective_chat.send_message("Проверяю подключение... ⏳")

    # Пробуем войти и получить список календарей
    try:
        client = caldav.DAVClient(
            url="https://caldav.yandex.ru", username=login, password=password
        )
        calendars = client.principal().calendars()
        if not calendars:
            raise Exception("в аккаунте нет ни одного календаря")
    except Exception:
        await msg.edit_text(
            "Не получилось войти 😕 Проверь логин и пароль приложения "
            "(тип — «Календарь CalDAV») и попробуй ещё раз: /connect"
        )
        return ConversationHandler.END

    # Успех! Запоминаем пароль ВРЕМЕННО (до выбора календаря)
    context.user_data["new_password"] = password
    context.user_data["found_calendars"] = [
        (str(c.url), c.name or "Без названия") for c in calendars
    ]

    # Показываем кнопки с календарями
    buttons = [
        [InlineKeyboardButton(name, callback_data=str(i))]
        for i, (_, name) in enumerate(context.user_data["found_calendars"])
    ]
    await msg.edit_text(
        "Подключение работает! ✅ В какой календарь добавлять события?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return C_CALENDAR


async def connect_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query  # нажатие кнопки приходит сюда
    await query.answer()

    idx = int(query.data)
    cal_url, cal_name = context.user_data["found_calendars"][idx]

    save_user(
        tg_id=update.effective_user.id,
        login=context.user_data["new_login"],
        password=context.user_data["new_password"],
        cal_url=cal_url,
        cal_name=cal_name,
    )
    # Подчищаем временные данные из "кармана"
    for key in ("new_login", "new_password", "found_calendars"):
        context.user_data.pop(key, None)

    await query.edit_message_text(
        f"Готово! 🎉 Буду добавлять события в календарь «{cal_name}».\n"
        f"Просто пришли мне любую заметку."
    )
    return ConversationHandler.END


# ----- Основной диалог: заметка → дата → событие -----

async def got_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if load_user(update.effective_user.id) is None:
        await update.message.reply_text(
            "Сначала подключи свой календарь — это просто: /connect"
        )
        return ConversationHandler.END

    context.user_data["note"] = update.message.text
    await update.message.reply_text(
        "Записал 📝 На когда поставить?\n"
        "Например: «завтра 18:00», «в пятницу 09:30», «20.07 18:30»\n"
        "Отменить: /cancel"
    )
    return WAITING_DATE


async def got_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    event_time = parse_when(update.message.text)
    if event_time is None:
        await update.message.reply_text(
            "Не понял 😅 Напиши, например: «завтра 18:00» или «20.07 18:30»"
        )
        return WAITING_DATE

    user = load_user(update.effective_user.id)
    note = context.user_data["note"]
    event_time = event_time.astimezone()

    try:
        cal = get_user_calendar(user)
        cal.save_event(
            dtstart=event_time,
            dtend=event_time + timedelta(minutes=EVENT_DURATION_MINUTES),
            summary=note,
        )
    except Exception:
        await update.message.reply_text(
            "Не получилось создать событие 😔 Возможно, пароль приложения "
            "был отозван — попробуй переподключиться: /connect"
        )
        return ConversationHandler.END

    await update.message.reply_text(
        f"✅ Добавил в «{user['cal_name']}»:\n"
        f"«{note}»\n"
        f"🗓 {event_time.strftime('%d.%m.%Y в %H:%M')}"
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ок, отменил ❌")
    return ConversationHandler.END


# ================= СБОРКА И ЗАПУСК =================

app = ApplicationBuilder().token(TOKEN).build()

connect_conv = ConversationHandler(
    entry_points=[CommandHandler("connect", connect_start)],
    states={
        C_LOGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, connect_login)],
        C_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, connect_password)],
        C_CALENDAR: [CallbackQueryHandler(connect_calendar)],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)

note_conv = ConversationHandler(
    entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, got_note)],
    states={
        WAITING_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_date)],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("donate", donate))
app.add_handler(CommandHandler("disconnect", disconnect))
app.add_handler(connect_conv)
app.add_handler(note_conv)

print("Бот v4 (многопользовательский) запущен! Остановить: Ctrl+C")
app.run_polling()
