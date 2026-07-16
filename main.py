# -*- coding: utf-8 -*-
# ВЕРСИЯ 5: многопользовательский бот с поддержкой часовых поясов.
# Бот спрашивает у пользователя "сколько у тебя сейчас времени?",
# вычисляет сдвиг относительно сервера и применяет его к событиям.

import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

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

# Токен читаем из переменной окружения BOT_TOKEN (задаётся на хостинге).
# Если её нет (запуск дома) — берём запасной вариант из кавычек.
TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН_ДЛЯ_ЗАПУСКА_ДОМА")

# Ссылка для донатов (впиши свою, когда появится)
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

# --- Шифрование ---
if not os.path.exists(KEY_PATH):
    with open(KEY_PATH, "wb") as f:
        f.write(Fernet.generate_key())
    print("Создал новый файл secret.key — храни его и никому не отдавай!")

with open(KEY_PATH, "rb") as f:
    fernet = Fernet(f.read())

# --- База данных ---
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute(
    """CREATE TABLE IF NOT EXISTS users (
        tg_id INTEGER PRIMARY KEY,
        login TEXT,
        enc_password BLOB,
        calendar_url TEXT,
        calendar_name TEXT,
        tz_offset INTEGER DEFAULT 0   -- сдвиг пользователя от сервера, в минутах
    )"""
)
# "Миграция": если база создана старой версией бота (без колонки tz_offset),
# добавляем колонку. Если она уже есть — команда упадёт, и мы это проигнорируем.
try:
    db.execute("ALTER TABLE users ADD COLUMN tz_offset INTEGER DEFAULT 0")
except sqlite3.OperationalError:
    pass
db.commit()


def save_user(tg_id, login, password, cal_url, cal_name, tz_offset):
    enc = fernet.encrypt(password.encode())
    db.execute(
        "INSERT OR REPLACE INTO users VALUES (?, ?, ?, ?, ?, ?)",
        (tg_id, login, enc, cal_url, cal_name, tz_offset),
    )
    db.commit()


def load_user(tg_id):
    row = db.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)).fetchone()
    if row is None:
        return None
    tg_id, login, enc, cal_url, cal_name, tz_offset = row
    return {
        "login": login,
        "password": fernet.decrypt(enc).decode(),
        "cal_url": cal_url,
        "cal_name": cal_name,
        "tz_offset": tz_offset or 0,
    }


def set_user_offset(tg_id, tz_offset):
    db.execute("UPDATE users SET tz_offset = ? WHERE tg_id = ?", (tz_offset, tg_id))
    db.commit()


def delete_user(tg_id):
    db.execute("DELETE FROM users WHERE tg_id = ?", (tg_id,))
    db.commit()


# --- Работа со временем ---

def server_now():
    """Текущее время сервера по UTC (без привязки к поясу)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def user_now(user):
    """Текущее время ПОЛЬЗОВАТЕЛЯ = время сервера + его сдвиг."""
    return server_now() + timedelta(minutes=user["tz_offset"])


def compute_offset(hhmm_text):
    """Пользователь написал, сколько у него сейчас времени (например '14:05').
    Вычисляем его сдвиг от сервера в минутах, округляя до 15 минут.
    Возвращает None, если текст не похож на время."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", hhmm_text.strip())
    if not m:
        return None
    h, mnt = int(m.group(1)), int(m.group(2))
    if h > 23 or mnt > 59:
        return None
    now = server_now()
    diff = (h * 60 + mnt) - (now.hour * 60 + now.minute)
    # Из-за границы суток разница может "перескочить" — нормализуем
    # в диапазон реальных поясов (от -12:00 до +14:00)
    if diff < -720:
        diff += 1440
    elif diff > 840:
        diff -= 1440
    return round(diff / 15) * 15  # округляем до четверти часа


WEEKDAYS = {
    "понедельник": 0, "вторник": 1, "сред": 2, "четверг": 3,
    "пятниц": 4, "суббот": 5, "воскресен": 6,
}


def parse_when(text, now):
    """Превращает 'завтра 18:00' / 'в пятницу 09:30' / '20.07 18:30'
    в дату-время. now — текущее время В ПОЯСЕ ПОЛЬЗОВАТЕЛЯ."""
    text = text.lower().strip()

    m = re.search(r"(\d{1,2}):(\d{2})", text)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        return None

    if "послезавтра" in text:
        day = now + timedelta(days=2)
    elif "завтра" in text:
        day = now + timedelta(days=1)
    elif "сегодня" in text:
        day = now
    else:
        day = None
        for stem, target in WEEKDAYS.items():
            if stem in text:
                ahead = (target - now.weekday()) % 7
                candidate = now + timedelta(days=ahead)
                if ahead == 0 and (hour, minute) <= (now.hour, now.minute):
                    candidate += timedelta(days=7)
                day = candidate
                break
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
            except ValueError:
                return None

    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def parse_duration(text):
    """Ищет в тексте длительность: 'на 2 часа', '30 мин', '1.5 часа',
    'полчаса', 'полтора часа'. Возвращает минуты или None."""
    text = text.lower().replace(",", ".")
    if "полтора" in text:
        return 90
    if "полчаса" in text:
        return 30
    m = re.search(r"(\d+(?:\.\d+)?)\s*час", text)  # '2 часа', '1.5 часа'
    if m is None:
        m = re.search(r"(\d+(?:\.\d+)?)\s*ч\b", text)  # '2ч', '2 ч'
    if m:
        return int(float(m.group(1)) * 60)
    m = re.search(r"(\d+)\s*мин", text)  # '30 мин', '45 минут'
    if m:
        return int(m.group(1))
    return None


def get_user_calendar(user):
    client = caldav.DAVClient(
        url="https://caldav.yandex.ru",
        username=user["login"],
        password=user["password"],
    )
    return client.calendar(url=user["cal_url"])


# ================= КОМАНДЫ И ДИАЛОГИ =================

WAITING_DATE = 1
C_LOGIN, C_PASSWORD, C_CALENDAR, C_TIME = 10, 11, 12, 13
TZ_TIME = 20


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! 📅 Я превращаю заметки в события Яндекс Календаря.\n\n"
        "1️⃣ Подключи свой календарь: /connect\n"
        "2️⃣ Пришли или перешли мне заметку\n"
        "3️⃣ Ответь, на когда — «завтра 18:00», «в пятницу 09:30» "
        "или «20.07 18:30»\n\n"
        "Другие команды:\n"
        "/timezone — поправить часовой пояс\n"
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


# ----- Диалог подключения (/connect) -----

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
        await update.message.delete()
    except Exception:
        pass

    login = context.user_data["new_login"]
    msg = await update.effective_chat.send_message("Проверяю подключение... ⏳")

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

    context.user_data["new_password"] = password
    context.user_data["found_calendars"] = [
        (str(c.url), c.name or "Без названия") for c in calendars
    ]

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
    query = update.callback_query
    await query.answer()

    idx = int(query.data)
    context.user_data["chosen_calendar"] = context.user_data["found_calendars"][idx]

    await query.edit_message_text(
        "Последний штрих — часовой пояс. 🕐\n"
        "Напиши, сколько у тебя СЕЙЧАС времени, в формате ЧЧ:ММ "
        "(например, 14:05) — я сам вычислю твой пояс."
    )
    return C_TIME


async def connect_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    offset = compute_offset(update.message.text)
    if offset is None:
        await update.message.reply_text(
            "Не понял 😅 Напиши текущее время в формате ЧЧ:ММ, например 14:05"
        )
        return C_TIME

    cal_url, cal_name = context.user_data["chosen_calendar"]
    save_user(
        tg_id=update.effective_user.id,
        login=context.user_data["new_login"],
        password=context.user_data["new_password"],
        cal_url=cal_url,
        cal_name=cal_name,
        tz_offset=offset,
    )
    for key in ("new_login", "new_password", "found_calendars", "chosen_calendar"):
        context.user_data.pop(key, None)

    await update.message.reply_text(
        f"Готово! 🎉 Буду добавлять события в календарь «{cal_name}» "
        f"по твоему местному времени.\n"
        f"Просто пришли мне любую заметку.\n\n"
        f"Если время в событиях вдруг «поедет» (например, после перевода "
        f"часов) — поправь пояс командой /timezone."
    )
    return ConversationHandler.END


# ----- Диалог смены пояса (/timezone) -----

async def tz_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if load_user(update.effective_user.id) is None:
        await update.message.reply_text("Сначала подключи календарь: /connect")
        return ConversationHandler.END
    await update.message.reply_text(
        "Напиши, сколько у тебя СЕЙЧАС времени (например, 14:05) — "
        "я обновлю твой часовой пояс. Отмена: /cancel"
    )
    return TZ_TIME


async def tz_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    offset = compute_offset(update.message.text)
    if offset is None:
        await update.message.reply_text(
            "Не понял 😅 Напиши текущее время в формате ЧЧ:ММ, например 14:05"
        )
        return TZ_TIME
    set_user_offset(update.effective_user.id, offset)
    await update.message.reply_text("Обновил часовой пояс! ✅")
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
        "Можно указать длительность: «завтра 18:00 на 2 часа» "
        "или «завтра 18:00-19:30» (без неё поставлю 1 час)\n"
        "Отменить: /cancel"
    )
    return WAITING_DATE


async def got_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = load_user(update.effective_user.id)
    text = update.message.text

    # Разбираем дату, считая "сейчас" по часам ПОЛЬЗОВАТЕЛЯ
    event_local = parse_when(text, now=user_now(user))
    if event_local is None:
        await update.message.reply_text(
            "Не понял 😅 Напиши, например: «завтра 18:00» или «20.07 18:30»"
        )
        return WAITING_DATE

    # Определяем длительность события.
    duration = EVENT_DURATION_MINUTES  # по умолчанию
    # Вариант 1: диапазон '18:00-19:30'
    rng = re.search(r"(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})", text)
    if rng:
        start_m = int(rng.group(1)) * 60 + int(rng.group(2))
        end_m = int(rng.group(3)) * 60 + int(rng.group(4))
        if end_m <= start_m:  # конец 'за полночь'
            end_m += 1440
        duration = end_m - start_m
    else:
        # Вариант 2: словами — 'на 2 часа', '30 мин', 'полтора часа'
        d = parse_duration(text)
        if d:
            duration = d

    # Переводим местное время пользователя в мировое (UTC):
    # вычитаем его сдвиг и помечаем результат как UTC.
    event_utc = (event_local - timedelta(minutes=user["tz_offset"])).replace(
        tzinfo=timezone.utc
    )

    note = context.user_data["note"]
    try:
        cal = get_user_calendar(user)
        cal.save_event(
            dtstart=event_utc,
            dtend=event_utc + timedelta(minutes=duration),
            summary=note,
        )
    except Exception:
        await update.message.reply_text(
            "Не получилось создать событие 😔 Возможно, пароль приложения "
            "был отозван — попробуй переподключиться: /connect"
        )
        return ConversationHandler.END

    end_local = event_local + timedelta(minutes=duration)
    await update.message.reply_text(
        f"✅ Добавил в «{user['cal_name']}»:\n"
        f"«{note}»\n"
        f"🗓 {event_local.strftime('%d.%m.%Y')}, "
        f"{event_local.strftime('%H:%M')}–{end_local.strftime('%H:%M')} "
        f"(твоё время)"
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
        C_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, connect_time)],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)

tz_conv = ConversationHandler(
    entry_points=[CommandHandler("timezone", tz_start)],
    states={
        TZ_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, tz_time)],
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
app.add_handler(tz_conv)
app.add_handler(note_conv)

print("Бот v5 (часовые пояса) запущен! Остановить: Ctrl+C")
app.run_polling()
