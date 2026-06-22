import os
import json
import requests
import gspread
import time
import threading
import uuid
from flask import Flask, request
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo

app = Flask(__name__)


def is_uptime_head_check():
    """
    UptimeRobot часто робить HEAD-запити. HEAD має тільки перевіряти, що бот живий,
    а НЕ запускати розсилку. Інакше моніторинг кожні 30 хвилин буде постійно
    смикати /marketing-broadcasts, /daily-reminders, /sale-broadcasts тощо.
    """
    try:
        return request.method == "HEAD"
    except Exception:
        return False


BOT_TOKEN = os.environ.get("BOT_TOKEN")
SHEET_ID = os.environ.get("SHEET_ID")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

USER_STATES = {}

# =========================
# AUTO DELETE / CLEAN CHAT
# =========================
# Автоматично видаляємо товарні картки через 20 хвилин.
# Можна змінити в Render Environment:
# PRODUCT_CARD_AUTO_DELETE_SECONDS = 1200
PRODUCT_CARD_AUTO_DELETE_SECONDS = int(os.environ.get("PRODUCT_CARD_AUTO_DELETE_SECONDS", "1200"))

# Запамʼятовуємо товарні повідомлення, щоб прибирати старі картки при переходах.
USER_PRODUCT_MESSAGES = {}

# Запамʼятовуємо останні сервісні повідомлення меню/кошика/бонусів/акцій.
# Коли клієнт відкриває новий пункт меню, попереднє сервісне повідомлення видаляється.
USER_SERVICE_MESSAGES = {}

# Запамʼятовуємо службові питання під час оформлення/заявок,
# щоб попереднє питання бота прибиралось і чат залишався чистим.
USER_FLOW_MESSAGES = {}

# Кеш для оновлення статусу бота у листі "Користувачі", щоб не писати в Sheets при кожному повідомленні.
USER_BOT_STATUS_CACHE = {}
USER_BOT_STATUS_THROTTLE_SECONDS = int(os.environ.get("USER_BOT_STATUS_THROTTLE_SECONDS", "21600"))
# Для швидкості не оновлюємо "Активний" у Google Sheets після кожного успішного sendMessage/sendPhoto.
# Помилки відправки все одно записуються одразу: заблокував бота / чат не знайдено / недоступний.
TRACK_ACTIVE_STATUS_ON_SUCCESS = os.environ.get("TRACK_ACTIVE_STATUS_ON_SUCCESS", "0").strip().lower() in ["1", "true", "yes", "так"]
# =========================
# BROADCAST ANTI-DUPLICATE PROTECTION
# =========================
# Не даємо одному клієнту отримувати кілька маркетингових/акційних повідомлень за день.
BROADCAST_DAILY_LIMIT_PER_USER = int(os.environ.get("BROADCAST_DAILY_LIMIT_PER_USER", "1"))

# Захист від паралельних запусків одного й того ж endpoint через UptimeRobot/Render.
BROADCAST_LOCK_TTL_SECONDS = int(os.environ.get("BROADCAST_LOCK_TTL_SECONDS", "900"))
BROADCAST_RUN_LOCKS = {}

# Єдиний лист для фіксації ВСІХ розсилок по кожному клієнту.
# Створюється автоматично при першому запуску будь-якої розсилки.
BROADCAST_HISTORY_SHEET_NAME = os.environ.get("BROADCAST_HISTORY_SHEET_NAME", "Історія розсилок")
BROADCAST_RUNS_SHEET_NAME = os.environ.get("BROADCAST_RUNS_SHEET_NAME", "Запуски розсилок")
BROADCAST_LOG_FLUSH_EVERY = int(os.environ.get("BROADCAST_LOG_FLUSH_EVERY", "1"))
# Скільки клієнтів обробляємо за один запуск розсилки.
# Для Render Free краще 20–30, щоб worker не вбивало по памʼяті/timeout.
BROADCAST_MAX_RECIPIENTS_PER_RUN = int(os.environ.get("BROADCAST_MAX_RECIPIENTS_PER_RUN", "5"))

# Внутрішній кеш: не перечитуємо заголовки службових листів при кожному записі логу.
SERVICE_WORKSHEETS_READY = set()

# =========================
# SCHEDULED BROADCASTS
# =========================
# Один безпечний endpoint /scheduled-broadcasts можна викликати планувальником.
# Він сам вирішує, що пора запускати, і НЕ дублює повідомлення клієнтам.
SCHEDULED_PRODUCT_DAY_START_HOUR = int(os.environ.get("SCHEDULED_PRODUCT_DAY_START_HOUR", "10"))
SCHEDULED_SALE_START_HOUR = int(os.environ.get("SCHEDULED_SALE_START_HOUR", "12"))
SCHEDULED_DAILY_MESSAGE_START_HOUR = int(os.environ.get("SCHEDULED_DAILY_MESSAGE_START_HOUR", "15"))
SCHEDULED_CART_REMINDERS_EVERY_RUN = os.environ.get("SCHEDULED_CART_REMINDERS_EVERY_RUN", "1").strip().lower() in ["1", "true", "yes", "так"]


def register_service_message(chat_id, message_id):
    try:
        if not message_id:
            return

        key = str(chat_id)
        USER_SERVICE_MESSAGES.setdefault(key, [])
        if message_id not in USER_SERVICE_MESSAGES[key]:
            USER_SERVICE_MESSAGES[key].append(message_id)
    except Exception as e:
        print("register_service_message error:", e)


def clear_service_messages(chat_id, except_message_id=None):
    try:
        key = str(chat_id)
        message_ids = USER_SERVICE_MESSAGES.get(key, [])
        keep = []

        for message_id in message_ids:
            if except_message_id and str(message_id) == str(except_message_id):
                keep.append(message_id)
                continue
            delete_message(chat_id, message_id)

        USER_SERVICE_MESSAGES[key] = keep
    except Exception as e:
        print("clear_service_messages error:", e)




def register_flow_message(chat_id, message_id):
    try:
        if not message_id:
            return
        key = str(chat_id)
        USER_FLOW_MESSAGES.setdefault(key, [])
        if message_id not in USER_FLOW_MESSAGES[key]:
            USER_FLOW_MESSAGES[key].append(message_id)
    except Exception as e:
        print("register_flow_message error:", e)


def clear_flow_messages(chat_id, except_message_id=None):
    """
    Видаляє попередні службові питання бота під час оформлення замовлення або заявки.
    Відповіді клієнта Telegram видалити не дозволяє, але питання бота можна прибирати.
    """
    try:
        key = str(chat_id)
        message_ids = USER_FLOW_MESSAGES.get(key, [])
        keep = []
        for message_id in message_ids:
            if except_message_id and str(message_id) == str(except_message_id):
                keep.append(message_id)
                continue
            delete_message(chat_id, message_id)
        USER_FLOW_MESSAGES[key] = keep
    except Exception as e:
        print("clear_flow_messages error:", e)


def send_flow_message(chat_id, text, keyboard=None, auto_delete_after=None):
    """
    Надсилає наступне питання в сценарії оформлення і прибирає попереднє питання бота.
    Використовуємо для ПІБ → телефон → місто → відділення → оплата → коментар.
    """
    clear_flow_messages(chat_id)
    message_id = send_message(chat_id, text, keyboard)
    register_flow_message(chat_id, message_id)
    if auto_delete_after:
        schedule_delete_message(chat_id, message_id, auto_delete_after)
    return message_id


def edit_flow_message(chat_id, message_id, text, keyboard=None):
    try:
        clear_flow_messages(chat_id, except_message_id=message_id)
        edit_message(chat_id, message_id, text, keyboard)
        register_flow_message(chat_id, message_id)
        return message_id
    except Exception as e:
        print("edit_flow_message error:", e)
        return None

def send_service_message(chat_id, text, keyboard=None, clear_products=True):
    """
    Надсилає сервісне повідомлення та прибирає попереднє сервісне повідомлення цього клієнта.
    Це тримає чат чистим при переходах: каталог → акції → бонуси → кошик.
    """
    if clear_products:
        clear_product_messages(chat_id)
    clear_service_messages(chat_id)
    message_id = send_message(chat_id, text, keyboard)
    register_service_message(chat_id, message_id)
    return message_id


def can_edit_as_text(callback_message):
    """
    Telegram не дозволяє editMessageText для повідомлень із фото/медіа.
    Якщо кнопка натиснута під фото товару, треба надсилати нове текстове повідомлення,
    а не намагатися перетворити фото на текст.
    """
    try:
        return bool(callback_message and callback_message.get("text") is not None)
    except Exception:
        return False


def update_service_message(chat_id, callback_message, text, keyboard=None, clear_products=True):
    """
    Якщо callback був під текстовим повідомленням — редагуємо його.
    Якщо callback був під фото/медіа — надсилаємо нове сервісне повідомлення.
    Це виправляє кнопки типу «Перейти в кошик» під карткою товару.
    """
    if clear_products:
        clear_product_messages(chat_id)

    if callback_message and can_edit_as_text(callback_message):
        message_id = callback_message.get("message_id")
        clear_service_messages(chat_id, except_message_id=message_id)
        ok = edit_message(chat_id, message_id, text, keyboard)
        if ok:
            register_service_message(chat_id, message_id)
            return message_id

    return send_service_message(chat_id, text, keyboard, clear_products=False)


def schedule_delete_message(chat_id, message_id, delay_seconds=None):
    """
    Планує видалення повідомлення бота через delay_seconds.
    Telegram дозволяє боту видаляти тільки свої повідомлення.
    """
    try:
        if not message_id:
            return

        delay_seconds = int(delay_seconds or PRODUCT_CARD_AUTO_DELETE_SECONDS)
        if delay_seconds <= 0:
            return

        timer = threading.Timer(delay_seconds, delete_message, args=(chat_id, message_id))
        timer.daemon = True
        timer.start()

    except Exception as e:
        print("schedule_delete_message error:", e)


def register_product_message(chat_id, message_id, auto_delete_after=None):
    """
    Зберігаємо message_id товарної картки, щоб:
    1) видалити її при переході на інший розділ;
    2) автоматично прибрати через 10–20 хвилин.
    """
    try:
        if not message_id:
            return

        key = str(chat_id)
        USER_PRODUCT_MESSAGES.setdefault(key, [])
        if message_id not in USER_PRODUCT_MESSAGES[key]:
            USER_PRODUCT_MESSAGES[key].append(message_id)

        schedule_delete_message(chat_id, message_id, auto_delete_after)

    except Exception as e:
        print("register_product_message error:", e)


def clear_product_messages(chat_id):
    """
    При новому кліку/переході прибираємо попередні товарні картки,
    щоб чат не засмічувався старими товарами.
    """
    try:
        key = str(chat_id)
        message_ids = USER_PRODUCT_MESSAGES.get(key, [])

        for message_id in message_ids:
            delete_message(chat_id, message_id)

        USER_PRODUCT_MESSAGES[key] = []

    except Exception as e:
        print("clear_product_messages error:", e)


# =========================
# TIMEZONE
# =========================
# Render/server time can be UTC, тому всі дати та перевірки часу
# рахуємо в київському часовому поясі.
KYIV_TZ = ZoneInfo(os.environ.get("BOT_TIMEZONE", "Europe/Kyiv"))

def current_time():
    return datetime.now(KYIV_TZ)


# =========================
# SIMPLE CACHE FOR SPEED
# =========================

CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "300"))
PRODUCTS_PAGE_SIZE = int(os.environ.get("PRODUCTS_PAGE_SIZE", "3"))

CACHE = {
    "records": {},
    "values": {}
}

# Кешуємо саме підключення до Google Sheets і обʼєкти аркушів,
# щоб не відкривати таблицю заново при кожному кліку.
SHEET_CONNECTION_TTL_SECONDS = int(os.environ.get("SHEET_CONNECTION_TTL_SECONDS", "3600"))
SHEET_CONNECTION_CACHE = {
    "created_at": None,
    "sheet": None,
    "worksheets": {}
}

# Не записуємо активність користувача в Google Sheets при кожному кліку.
# Це сильно зменшує кількість запитів і прибирає помилки 429.
USER_ACTIVITY_THROTTLE_SECONDS = int(os.environ.get("USER_ACTIVITY_THROTTLE_SECONDS", "600"))
USER_ACTIVITY_CACHE = {}


def cache_get(bucket, key):
    item = CACHE.get(bucket, {}).get(key)
    if not item:
        return None

    created_at = item.get("created_at")
    if not created_at:
        return None

    age = (current_time() - created_at).total_seconds()
    if age > CACHE_TTL_SECONDS:
        try:
            del CACHE[bucket][key]
        except Exception:
            pass
        return None

    return item.get("data")


def cache_set(bucket, key, data):
    CACHE.setdefault(bucket, {})[key] = {
        "created_at": current_time(),
        "data": data
    }
    return data


def clear_cache(sheet_name=None):
    """
    Очищаємо кеш після змін у таблиці.
    Якщо sheet_name не передано — чистимо все.
    """
    try:
        if not sheet_name:
            CACHE["records"].clear()
            CACHE["values"].clear()
            return

        CACHE["records"].pop(sheet_name, None)
        CACHE["values"].pop(sheet_name, None)
    except Exception as e:
        print("clear_cache error:", e)



def is_quota_error(error):
    text = str(error).lower()
    return "429" in text or "quota exceeded" in text or "read requests" in text


def google_call_with_retry(func, attempts=4):
    """
    Якщо Google Sheets тимчасово віддає 429, не валимо весь endpoint одразу.
    Для Render Free важливо робити довшу паузу, бо короткі 2-5 секунд не встигають
    звільнити квоту Read requests per minute.
    """
    last_error = None

    for attempt in range(attempts):
        try:
            return func()
        except Exception as e:
            last_error = e
            if not is_quota_error(e) or attempt == attempts - 1:
                raise
            sleep_seconds = 15 + attempt * 15
            print(f"google_call_with_retry quota pause {sleep_seconds}s: {e}")
            time.sleep(sleep_seconds)

    raise last_error


def get_cached_worksheet(sheet_name):
    created_at = SHEET_CONNECTION_CACHE.get("created_at")
    sheet = SHEET_CONNECTION_CACHE.get("sheet")

    if (
        sheet is None
        or created_at is None
        or (current_time() - created_at).total_seconds() > SHEET_CONNECTION_TTL_SECONDS
    ):
        # Оновлюємо підключення до всієї таблиці.
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        sheet = google_call_with_retry(lambda: client.open_by_key(SHEET_ID))

        SHEET_CONNECTION_CACHE["sheet"] = sheet
        SHEET_CONNECTION_CACHE["created_at"] = current_time()
        SHEET_CONNECTION_CACHE["worksheets"] = {}

    worksheets = SHEET_CONNECTION_CACHE.setdefault("worksheets", {})
    if sheet_name not in worksheets:
        worksheets[sheet_name] = google_call_with_retry(lambda: sheet.worksheet(sheet_name))

    return worksheets[sheet_name]


def clear_sheet_connection_cache(sheet_name=None):
    try:
        if not sheet_name:
            SHEET_CONNECTION_CACHE["created_at"] = None
            SHEET_CONNECTION_CACHE["sheet"] = None
            SHEET_CONNECTION_CACHE["worksheets"] = {}
            return
        SHEET_CONNECTION_CACHE.setdefault("worksheets", {}).pop(sheet_name, None)
    except Exception as e:
        print("clear_sheet_connection_cache error:", e)


def get_cached_records(sheet_name):
    cached = cache_get("records", sheet_name)
    if cached is not None:
        return cached

    return cache_set("records", sheet_name, get_records(sheet_name))


def get_cached_values(sheet_name):
    cached = cache_get("values", sheet_name)
    if cached is not None:
        return cached

    return cache_set("values", sheet_name, get_values(sheet_name))



# =========================
# GOOGLE SHEETS
# =========================

def get_sheet():
    created_at = SHEET_CONNECTION_CACHE.get("created_at")
    sheet = SHEET_CONNECTION_CACHE.get("sheet")

    if (
        sheet is not None
        and created_at is not None
        and (current_time() - created_at).total_seconds() <= SHEET_CONNECTION_TTL_SECONDS
    ):
        return sheet

    creds_dict = json.loads(GOOGLE_CREDS_JSON)

    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]

    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    sheet = google_call_with_retry(lambda: client.open_by_key(SHEET_ID))

    SHEET_CONNECTION_CACHE["created_at"] = current_time()
    SHEET_CONNECTION_CACHE["sheet"] = sheet
    SHEET_CONNECTION_CACHE["worksheets"] = {}

    return sheet


def get_records(sheet_name):
    cached = cache_get("records", sheet_name)
    if cached is not None:
        return cached

    worksheet = get_cached_worksheet(sheet_name)
    data = google_call_with_retry(lambda: worksheet.get_all_records())
    return cache_set("records", sheet_name, data)


def get_values(sheet_name):
    cached = cache_get("values", sheet_name)
    if cached is not None:
        return cached

    worksheet = get_cached_worksheet(sheet_name)
    data = google_call_with_retry(lambda: worksheet.get_all_values())
    return cache_set("values", sheet_name, data)

def get_or_create_worksheet(sheet_name, headers):
    sh = get_sheet()

    try:
        ws = get_cached_worksheet(sheet_name)
    except Exception:
        ws = google_call_with_retry(lambda: sh.add_worksheet(title=sheet_name, rows=1000, cols=len(headers)))
        SHEET_CONNECTION_CACHE.setdefault("worksheets", {})[sheet_name] = ws
        google_call_with_retry(lambda: ws.append_row(headers, value_input_option="USER_ENTERED"))

    values = google_call_with_retry(lambda: ws.get_all_values())
    if not values:
        google_call_with_retry(lambda: ws.append_row(headers, value_input_option="USER_ENTERED"))

    return ws


def append_contact_request(row):
    headers = ["Дата", "Telegram ID", "ПІБ", "Телефон", "Статус"]
    ws = get_or_create_worksheet("Заявки", headers)
    ws.append_row(row, value_input_option="USER_ENTERED")


def get_contact_requests_with_rows():
    headers = ["Дата", "Telegram ID", "ПІБ", "Телефон", "Статус"]
    ws = get_or_create_worksheet("Заявки", headers)
    rows = google_call_with_retry(lambda: ws.get_all_values())
    result = []

    for i, row in enumerate(rows[1:], start=2):
        result.append({
            "row_index": i,
            "Дата": row[0] if len(row) > 0 else "",
            "Telegram ID": row[1] if len(row) > 1 else "",
            "ПІБ": row[2] if len(row) > 2 else "",
            "Телефон": row[3] if len(row) > 3 else "",
            "Статус": row[4] if len(row) > 4 else ""
        })

    return result


def append_row(sheet_name, row):
    worksheet = get_cached_worksheet(sheet_name)
    google_call_with_retry(lambda: worksheet.append_row(row, value_input_option="USER_ENTERED"))
    clear_cache(sheet_name)


def update_cell(sheet_name, row, col, value):
    worksheet = get_cached_worksheet(sheet_name)
    google_call_with_retry(lambda: worksheet.update_cell(row, col, value))
    clear_cache(sheet_name)


def delete_row(sheet_name, row_index):
    worksheet = get_cached_worksheet(sheet_name)
    google_call_with_retry(lambda: worksheet.delete_rows(row_index))
    clear_cache(sheet_name)


def clear_user_cart(telegram_id):
    ws = get_cached_worksheet("Кошик")
    rows = google_call_with_retry(lambda: ws.get_all_values())
    rows_to_delete = []

    for i, row in enumerate(rows[1:], start=2):
        if len(row) > 0 and str(row[0]) == str(telegram_id):
            rows_to_delete.append(i)

    for row_index in reversed(rows_to_delete):
        google_call_with_retry(lambda row_index=row_index: ws.delete_rows(row_index))

    if rows_to_delete:
        clear_cache("Кошик")


def get_user_cart(telegram_id):
    rows = get_records("Кошик")
    return [r for r in rows if str(r.get("Telegram ID")) == str(telegram_id)]


def find_user_cart_rows(telegram_id):
    rows = get_values("Кошик")
    result = []

    for i, row in enumerate(rows[1:], start=2):
        if len(row) > 0 and str(row[0]) == str(telegram_id):
            result.append({
                "row_index": i,
                "telegram_id": row[0] if len(row) > 0 else "",
                "product_id": row[1] if len(row) > 1 else "",
                "name": row[2] if len(row) > 2 else "",
                "price": row[3] if len(row) > 3 else "",
                "qty": row[4] if len(row) > 4 else "",
                "sum": row[5] if len(row) > 5 else ""
            })

    return result


def find_cart_row_by_product(telegram_id, product_id):
    rows = get_values("Кошик")

    for i, row in enumerate(rows[1:], start=2):
        if len(row) > 1 and str(row[0]) == str(telegram_id) and str(row[1]) == str(product_id):
            return {
                "row_index": i,
                "telegram_id": row[0] if len(row) > 0 else "",
                "product_id": row[1] if len(row) > 1 else "",
                "name": row[2] if len(row) > 2 else "",
                "price": row[3] if len(row) > 3 else "",
                "qty": row[4] if len(row) > 4 else "",
                "sum": row[5] if len(row) > 5 else ""
            }

    return None



# =========================
# ABANDONED CART REMINDERS
# =========================

CART_BASE_HEADERS = [
    "Telegram ID",
    "ID товару",
    "Назва товару",
    "Ціна",
    "Кількість",
    "Сума",
    "Дата додавання/оновлення",
    "Нагадування 1",
    "Нагадування 2",
    "Нагадування 3"
]

# Щоб клієнтам не прилітали нагадування вночі.
# За замовчуванням надсилаємо тільки з 10:00 до 20:59 за Києвом.
CART_REMINDER_MIN_HOUR = int(os.environ.get("CART_REMINDER_MIN_HOUR", "10"))
CART_REMINDER_MAX_HOUR = int(os.environ.get("CART_REMINDER_MAX_HOUR", "21"))


def get_cart_worksheet():
    """
    Лист "Кошик" тепер має додаткові колонки для нагадувань.
    Якщо старі колонки вже були — код акуратно додасть відсутні в кінець.
    """
    sh = get_sheet()

    try:
        ws = get_cached_worksheet("Кошик")
    except Exception:
        ws = google_call_with_retry(lambda: sh.add_worksheet(title="Кошик", rows=1000, cols=len(CART_BASE_HEADERS)))
        SHEET_CONNECTION_CACHE.setdefault("worksheets", {})["Кошик"] = ws
        google_call_with_retry(lambda: google_call_with_retry(lambda: ws.append_row(CART_BASE_HEADERS, value_input_option="USER_ENTERED")))
        return ws

    values = google_call_with_retry(lambda: ws.get_all_values())
    if not values:
        google_call_with_retry(lambda: ws.append_row(CART_BASE_HEADERS, value_input_option="USER_ENTERED"))
        return ws

    headers = values[0]
    changed = False

    for idx, header in enumerate(CART_BASE_HEADERS, start=1):
        if len(headers) < idx or not str(headers[idx - 1]).strip():
            google_call_with_retry(lambda idx=idx, header=header: ws.update_cell(1, idx, header))
            changed = True

    if changed:
        print("Кошик headers updated for reminders")

    return ws


def now_str():
    return current_time().strftime("%d.%m.%Y %H:%M")


def update_cart_reminder_columns(row_index, updated_at=None, reminder1=None, reminder2=None, reminder3=None):
    try:
        ws = get_cart_worksheet()

        if updated_at is not None:
            google_call_with_retry(lambda: ws.update_cell(row_index, 7, updated_at))
        if reminder1 is not None:
            google_call_with_retry(lambda: ws.update_cell(row_index, 8, reminder1))
        if reminder2 is not None:
            google_call_with_retry(lambda: ws.update_cell(row_index, 9, reminder2))
        if reminder3 is not None:
            google_call_with_retry(lambda: ws.update_cell(row_index, 10, reminder3))

    except Exception as e:
        print("update_cart_reminder_columns error:", e)


def cart_reminder_keyboard(reminder_number=None):
    buttons = []

    if reminder_number == 2:
        buttons.append([inline_button("📞 Залишити заявку на зв’язок", "contact_from_cart")])

    buttons.append([inline_button("🛒 Перейти до кошика", "open_cart")])

    return {
        "inline_keyboard": buttons
    }


def cart_reminder_text(reminder_number, total=0, discount_percent=0):
    if reminder_number == 1:
        return (
            "🛍 <b>Ви додали товари до кошика, але ще не оформили замовлення.</b>\n\n"
            "Можливо, ми можемо Вам допомогти?\n\n"
            "Ваш кошик все ще збережений 💛"
        )

    if reminder_number == 2:
        return (
            "⏰ <b>Бачимо, що у Вашому кошику залишилися товари.</b>\n\n"
            "Можливо, у Вас виникли додаткові питання щодо товару, доставки або оплати? 💛\n\n"
            "Залиште заявку на зв’язок — менеджер допоможе Вам з вибором та оформленням замовлення.\n\n"
            "Або Ви можете одразу повернутися до кошика та завершити покупку 🛍"
        )

    extra = ""
    if discount_percent:
        extra = f"\n\n🎁 Для Вас також активна знижка <b>-{int(discount_percent)}%</b> на замовлення."

    return (
        "🎁 <b>Ми помітили, що у Вас залишилися товари в кошику.</b>\n\n"
        "Можливо, саме час завершити замовлення? ✨"
        f"{extra}\n\n"
        "🛒 Перейдіть до кошика та оформіть покупку у зручний для Вас час."
    )


def get_cart_rows_grouped_by_user():
    ws = get_cart_worksheet()
    rows = google_call_with_retry(lambda: ws.get_all_values())
    grouped = {}

    for row_index, row in enumerate(rows[1:], start=2):
        telegram_id = str(row[0] if len(row) > 0 else "").strip()
        product_id = str(row[1] if len(row) > 1 else "").strip()

        if not telegram_id or not product_id:
            continue

        try:
            item_sum = safe_float(row[5] if len(row) > 5 and row[5] else 0)
        except:
            item_sum = 0

        updated_at = row[6] if len(row) > 6 else ""
        reminder1 = row[7] if len(row) > 7 else ""
        reminder2 = row[8] if len(row) > 8 else ""
        reminder3 = row[9] if len(row) > 9 else ""

        if telegram_id not in grouped:
            grouped[telegram_id] = {
                "rows": [],
                "total": 0,
                "updated_dates": [],
                "reminder1_sent": True,
                "reminder2_sent": True,
                "reminder3_sent": True
            }

        grouped[telegram_id]["rows"].append(row_index)
        grouped[telegram_id]["total"] += item_sum

        parsed = parse_bot_datetime(updated_at)
        if parsed:
            grouped[telegram_id]["updated_dates"].append(parsed)
        else:
            # Старі рядки без дати не спамимо одразу — ставимо поточну дату.
            update_cart_reminder_columns(row_index, updated_at=now_str(), reminder1="", reminder2="", reminder3="")
            grouped[telegram_id]["updated_dates"].append(current_time())

        if not str(reminder1).strip():
            grouped[telegram_id]["reminder1_sent"] = False
        if not str(reminder2).strip():
            grouped[telegram_id]["reminder2_sent"] = False
        if not str(reminder3).strip():
            grouped[telegram_id]["reminder3_sent"] = False

    return grouped


def process_cart_reminders():
    """
    Запускається через окремий URL /cart-reminders.
    Надсилає максимум одне нагадування одному клієнту за один запуск,
    щоб не засипати повідомленнями, якщо бот довго не перевіряв кошики.
    Також не надсилає повідомлення вночі за київським часом.
    """
    now = current_time()

    if now.hour < CART_REMINDER_MIN_HOUR or now.hour >= CART_REMINDER_MAX_HOUR:
        print(f"cart reminders skipped by Kyiv quiet hours: {now_str()}")
        return 0

    grouped = get_cart_rows_grouped_by_user()
    sent_count = 0
    log_rows = []
    sent_keys, today_counts = get_broadcast_recipient_log_snapshot()
    sent_at = now_str()

    for telegram_id, data in grouped.items():
        dates = data.get("updated_dates") or []
        if not dates:
            continue

        # Якщо клієнт додавав товар кілька разів — рахуємо від останнього оновлення кошика.
        last_update = max(dates)
        hours_passed = (now - last_update).total_seconds() / 3600

        reminder_number = None
        reminder_col = None

        if hours_passed >= 1 and not data.get("reminder1_sent"):
            reminder_number = 1
            reminder_col = 8
        elif hours_passed >= 24 and not data.get("reminder2_sent"):
            reminder_number = 2
            reminder_col = 9
        elif hours_passed >= 72 and not data.get("reminder3_sent"):
            reminder_number = 3
            reminder_col = 10

        if not reminder_number:
            continue

        campaign_key = f"cart_reminder|{reminder_number}|{last_update.strftime('%Y%m%d%H%M')}"
        unique_key = f"{telegram_id}|{campaign_key}"
        if unique_key in sent_keys:
            continue

        try:
            discount_percent = get_client_discount_percent(telegram_id)
        except:
            discount_percent = 0

        text = cart_reminder_text(
            reminder_number=reminder_number,
            total=data.get("total", 0),
            discount_percent=discount_percent
        )

        ok = send_message(telegram_id, text, cart_reminder_keyboard(reminder_number))

        if ok:
            sent_keys.add(unique_key)
            log_rows.append([
                sent_at,
                telegram_id,
                f"Нагадування кошика {reminder_number}",
                campaign_key,
                "Надіслано"
            ])

            for row_index in data.get("rows", []):
                try:
                    google_call_with_retry(lambda row_index=row_index: get_cart_worksheet().update_cell(row_index, reminder_col, sent_at))
                except Exception as e:
                    print("cart reminder mark error:", e)

            sent_count += 1

            if len(log_rows) >= BROADCAST_LOG_FLUSH_EVERY:
                append_broadcast_recipient_logs(log_rows)
                log_rows = []

    append_broadcast_recipient_logs(log_rows)
    return sent_count

def get_order_cell(row, headers_map, header_name, fallback_index=None, default=""):
    """
    Дістає значення з рядка замовлення по назві колонки.
    Якщо колонки немає — використовує старий індекс як запасний варіант.
    Це потрібно, бо структура листа "Замовлення" змінювалась.
    """
    try:
        idx = headers_map.get(str(header_name).strip().lower())
        if idx is not None and len(row) > idx:
            return row[idx]
    except Exception:
        pass

    if fallback_index is not None and len(row) > fallback_index:
        return row[fallback_index]

    return default


def get_order_status_col_index():
    """
    Повертає номер колонки статусу в Google Sheets, починаючи з 1.
    У новій структурі це колонка "Статус", але якщо раптом заголовки старі —
    залишаємо запасний варіант.
    """
    try:
        rows = get_values("Замовлення")
        headers = rows[0] if rows else []
        for idx, header in enumerate(headers, start=1):
            if str(header).strip().lower() == "статус":
                return idx
    except Exception as e:
        print("get_order_status_col_index error:", e)

    return 12


def get_orders_with_rows():
    rows = get_values("Замовлення")
    result = []

    headers = rows[0] if rows else []
    headers_map = {
        str(header).strip().lower(): idx
        for idx, header in enumerate(headers)
        if str(header).strip()
    }

    for i, row in enumerate(rows[1:], start=2):
        if not row or not any(str(cell).strip() for cell in row):
            continue

        item = {
            "row_index": i,
            "Дата": get_order_cell(row, headers_map, "Дата", 0),
            "Telegram ID": get_order_cell(row, headers_map, "Telegram ID", 1),
            "ПІБ": get_order_cell(row, headers_map, "ПІБ", 2),
            "Телефон": get_order_cell(row, headers_map, "Телефон", 3),
            "Адреса доставки": get_order_cell(row, headers_map, "Адреса доставки", 4),
            "Спосіб доставки": get_order_cell(row, headers_map, "Спосіб доставки", 5),
            "Спосіб оплати": get_order_cell(row, headers_map, "Спосіб оплати", 6),
            "Товари": get_order_cell(row, headers_map, "Товари", 7),
            "Сума": get_order_cell(row, headers_map, "Сума", 8),
            "Потрібно зв’язатись": get_order_cell(row, headers_map, "Потрібно зв’язатись", 9),
            "Коментар": get_order_cell(row, headers_map, "Коментар", 10),
            "Статус": get_order_cell(row, headers_map, "Статус", 11),
            "Статус оплати": get_order_cell(row, headers_map, "Статус оплати", 12),
            "_raw_row": row
        }

        # Запасний варіант для дуже старої структури:
        # Дата, Telegram ID, ПІБ, Телефон, Адреса, Товари, Сума, ...
        if not str(item.get("Сума", "")).strip() and len(row) > 6:
            possible_old_sum = row[6]
            if safe_float(possible_old_sum) > 0:
                item["Сума"] = possible_old_sum

        result.append(item)

    return result





def get_fresh_order_by_row_index(row_index):
    """
    Завжди перечитує конкретний рядок замовлення напряму з Google Sheets,
    без кешу get_values(). Це потрібно для бонусів після зміни статусу,
    щоб бот точно бачив актуальні Telegram ID, Суму і Статус.
    """
    try:
        ws = get_cached_worksheet("Замовлення")
        rows = google_call_with_retry(lambda: ws.get_all_values())
        if not rows:
            return None

        row_number = int(row_index)
        if row_number <= 1 or len(rows) < row_number:
            print("get_fresh_order_by_row_index: row not found", row_index)
            return None

        headers = rows[0]
        row = rows[row_number - 1]
        headers_map = {
            str(header).strip().lower(): idx
            for idx, header in enumerate(headers)
            if str(header).strip()
        }

        return {
            "row_index": row_number,
            "Дата": get_order_cell(row, headers_map, "Дата", 0),
            "Telegram ID": get_order_cell(row, headers_map, "Telegram ID", 1),
            "ПІБ": get_order_cell(row, headers_map, "ПІБ", 2),
            "Телефон": get_order_cell(row, headers_map, "Телефон", 3),
            "Адреса доставки": get_order_cell(row, headers_map, "Адреса доставки", 4),
            "Спосіб доставки": get_order_cell(row, headers_map, "Спосіб доставки", 5),
            "Спосіб оплати": get_order_cell(row, headers_map, "Спосіб оплати", 6),
            "Товари": get_order_cell(row, headers_map, "Товари", 7),
            "Сума": get_order_cell(row, headers_map, "Сума", 8),
            "Потрібно зв’язатись": get_order_cell(row, headers_map, "Потрібно зв’язатись", 9),
            "Коментар": get_order_cell(row, headers_map, "Коментар", 10),
            "Статус": get_order_cell(row, headers_map, "Статус", 11),
            "Статус оплати": get_order_cell(row, headers_map, "Статус оплати", 12),
            "_raw_row": row
        }
    except Exception as e:
        print("get_fresh_order_by_row_index error:", e, "row_index:", row_index)
        return None

def get_pending_payment_order(chat_id):
    orders = get_orders_with_rows()
    pending_statuses = ["Очікується оплата", "Очікує оплати"]

    for order in reversed(orders):
        if str(order.get("Telegram ID")) == str(chat_id) and str(order.get("Статус", "")).strip() in pending_statuses:
            return order

    return None


def append_payment_receipt(chat_id, full_name, order_row_index, file_id, file_type, caption=""):
    headers = ["Дата", "Telegram ID", "ПІБ", "Рядок замовлення", "Тип файлу", "File ID", "Коментар", "Статус"]
    ws = get_or_create_worksheet("Квитанції", headers)
    ws.append_row([
        current_time().strftime("%d.%m.%Y %H:%M"),
        chat_id,
        full_name,
        order_row_index,
        file_type,
        file_id,
        caption,
        "Нова"
    ], value_input_option="USER_ENTERED")


def notify_admin_payment_receipt(chat_id, order, file_id, file_type, caption=""):
    full_name = order.get("ПІБ", "") if order else ""
    total = order.get("Сума", "") if order else ""
    order_row = order.get("row_index", "") if order else ""

    admin_caption = (
        "🧾 <b>Нова квитанція про оплату</b>\n\n"
        f"<b>ПІБ:</b> {full_name or '—'}\n"
        f"<b>Telegram ID:</b> {chat_id}\n"
        f"<b>Замовлення, рядок:</b> {order_row or '—'}\n"
        f"<b>Сума замовлення:</b> {total or '—'} грн\n"
        f"<b>Коментар клієнта:</b> {caption or '—'}"
    )

    for admin_id in get_admin_ids():
        if file_type == "photo":
            ok = send_photo(admin_id, file_id, admin_caption)
            if not ok:
                send_message(admin_id, admin_caption)
        else:
            ok = send_document(admin_id, file_id, admin_caption)
            if not ok:
                send_message(admin_id, admin_caption)


def handle_payment_receipt(chat_id, message):
    photo = message.get("photo")
    document = message.get("document")

    if not photo and not document:
        return False

    order = get_pending_payment_order(chat_id)
    if not order:
        return False

    caption = message.get("caption", "")

    if photo:
        file_id = photo[-1].get("file_id")
        file_type = "photo"
    else:
        file_id = document.get("file_id")
        file_type = "document"

    if not file_id:
        return False

    append_payment_receipt(
        chat_id=chat_id,
        full_name=order.get("ПІБ", ""),
        order_row_index=order.get("row_index", ""),
        file_id=file_id,
        file_type=file_type,
        caption=caption
    )

    notify_admin_payment_receipt(chat_id, order, file_id, file_type, caption)

    send_service_message(
        chat_id,
        "✅ Дякуємо! Квитанцію отримано та передано менеджеру на перевірку 💛\n\n"
        "Після перевірки ми оновимо статус Вашого замовлення.\n\n"
        "🏠 Оберіть, будь ласка, що хочете зробити далі:",
        main_menu_inline(is_admin(chat_id))
    )
    return True


# =========================
# TELEGRAM HELPERS
# =========================

def normalize_inline_keyboard(keyboard):
    """
    Додає кнопку 🏠 На головну майже до всіх inline-клавіатур.
    Не чіпає Reply Keyboard/remove_keyboard і саме головне меню.
    """
    try:
        if not isinstance(keyboard, dict):
            return keyboard

        if "inline_keyboard" not in keyboard:
            return keyboard

        rows = keyboard.get("inline_keyboard") or []

        # Збираємо всі callback_data, щоб не дублювати кнопку.
        callbacks = []
        for row in rows:
            for button in row:
                if isinstance(button, dict):
                    callbacks.append(str(button.get("callback_data", "")))

        if "back_main" in callbacks:
            return keyboard

        # Якщо це саме головне меню — кнопку На головну не додаємо.
        main_callbacks = {
            "open_catalog",
            "open_sales",
            "open_cart",
            "open_orders",
            "open_bonus_cabinet",
            "open_referral_program",
            "contact_manager_general",
            "manager_order",
            "open_delivery_payment"
        }
        if main_callbacks.issubset(set(callbacks)):
            return keyboard

        rows_copy = []
        for row in rows:
            rows_copy.append([dict(button) for button in row])

        rows_copy.append([{"text": "🏠 На головну", "callback_data": "back_main"}])
        new_keyboard = dict(keyboard)
        new_keyboard["inline_keyboard"] = rows_copy
        return new_keyboard

    except Exception as e:
        print("normalize_inline_keyboard error:", e)
        return keyboard




def ensure_user_columns(column_names):
    """Гарантує, що в листі Користувачі є потрібні колонки, і повертає map назва→номер колонки."""
    result = {}
    try:
        ws = get_users_worksheet()
        rows = google_call_with_retry(lambda: ws.get_all_values())
        headers = rows[0] if rows else []

        normalized = {str(h).strip().lower(): idx for idx, h in enumerate(headers, start=1) if str(h).strip()}
        changed = False

        for column_name in column_names:
            key = str(column_name).strip().lower()
            if key in normalized:
                result[column_name] = normalized[key]
                continue

            new_col = len(headers) + 1
            try:
                current_cols = int(getattr(ws, "col_count", 0) or 0)
                if current_cols < new_col:
                    google_call_with_retry(lambda new_col=new_col, current_cols=current_cols: ws.add_cols(new_col - current_cols))
            except Exception as e:
                print("ensure_user_columns add_cols error:", e)

            google_call_with_retry(lambda new_col=new_col, column_name=column_name: ws.update_cell(1, new_col, column_name))
            headers.append(column_name)
            normalized[key] = new_col
            result[column_name] = new_col
            changed = True

        if changed:
            clear_cache("Користувачі")
            clear_sheet_connection_cache("Користувачі")

        return ws, result
    except Exception as e:
        print("ensure_user_columns error:", e)
        return None, result


def classify_telegram_send_error(response_text):
    text = str(response_text or "").lower()
    if "bot was blocked" in text or "blocked by the user" in text:
        return "Заблокував бота"
    if "user is deactivated" in text:
        return "Неактивний акаунт"
    if "chat not found" in text:
        return "Чат не знайдено"
    if "forbidden" in text:
        return "Недоступний"
    return "Помилка відправки"


def update_user_bot_status(chat_id, status, error_text="", force=False):
    """
    Записує в лист Користувачі, чи бот може писати користувачу.
    Колонки створюються автоматично: Статус бота, Дата перевірки статусу, Остання помилка бота.
    """
    try:
        telegram_id = str(chat_id).strip()
        if not telegram_id:
            return False

        cache_key = f"{telegram_id}:{status}:{str(error_text)[:60]}"
        last = USER_BOT_STATUS_CACHE.get(cache_key)
        if not force and last and (current_time() - last).total_seconds() < USER_BOT_STATUS_THROTTLE_SECONDS:
            return False

        ws, cols = ensure_user_columns(["Статус бота", "Дата перевірки статусу", "Остання помилка бота"])
        if not ws or not cols:
            return False

        rows = google_call_with_retry(lambda: ws.get_all_values())
        row_index = None
        for i, row in enumerate(rows[1:], start=2):
            if len(row) > 0 and str(row[0]).strip() == telegram_id:
                row_index = i
                break

        if not row_index:
            return False

        google_call_with_retry(lambda: ws.update_cell(row_index, cols["Статус бота"], status))
        google_call_with_retry(lambda: ws.update_cell(row_index, cols["Дата перевірки статусу"], now_str()))
        google_call_with_retry(lambda: ws.update_cell(row_index, cols["Остання помилка бота"], str(error_text or "")[:500]))
        USER_BOT_STATUS_CACHE[cache_key] = current_time()
        clear_cache("Користувачі")
        return True
    except Exception as e:
        print("update_user_bot_status error:", e)
        return False


# =========================
# USER BOT STATUS CHECK
# =========================
# Перевірка доступності бота для користувачів без видимих повідомлень.
# Endpoint: /check-users-status
# Можна запускати вручну або через монітор, наприклад раз на тиждень.
CHECK_USERS_STATUS_LIMIT_PER_RUN = int(os.environ.get("CHECK_USERS_STATUS_LIMIT_PER_RUN", "50"))
CHECK_USERS_STATUS_INTERVAL_DAYS = int(os.environ.get("CHECK_USERS_STATUS_INTERVAL_DAYS", "7"))


def col_to_letter(col_number):
    """Перетворює номер колонки Google Sheets у літеру: 1 -> A, 27 -> AA."""
    try:
        col_number = int(col_number)
        result = ""
        while col_number > 0:
            col_number, remainder = divmod(col_number - 1, 26)
            result = chr(65 + remainder) + result
        return result
    except Exception:
        return "A"


def should_check_user_status(row, cols, force=False):
    """
    Вирішує, чи потрібно перевіряти користувача зараз.
    Якщо force=1 у URL — перевіряємо незалежно від дати останньої перевірки.
    """
    if force:
        return True

    try:
        date_col = cols.get("Дата перевірки статусу")
        if not date_col:
            return True

        last_raw = row[date_col - 1] if len(row) >= date_col else ""
        last_dt = parse_bot_datetime(last_raw)
        if not last_dt:
            return True

        days_passed = (current_time() - last_dt).total_seconds() / 86400
        return days_passed >= CHECK_USERS_STATUS_INTERVAL_DAYS
    except Exception:
        return True


def check_telegram_user_status(chat_id):
    """
    Перевіряє, чи бот може звернутися до користувача.
    Використовує sendChatAction — це не створює повідомлення в чаті.
    """
    try:
        response = requests.post(
            f"{BASE_URL}/sendChatAction",
            json={"chat_id": chat_id, "action": "typing"},
            timeout=10
        )

        if response.ok:
            return "Активний", ""

        return classify_telegram_send_error(response.text), str(response.text or "")[:500]
    except Exception as e:
        return "Помилка перевірки", str(e)[:500]


def write_user_status_batch(ws, updates):
    """
    Пише статуси пачкою, щоб не робити 3 окремі update_cell для кожного користувача.
    """
    if not updates:
        return

    try:
        google_call_with_retry(lambda: ws.batch_update(updates, value_input_option="USER_ENTERED"))
    except TypeError:
        # Для старих версій gspread, де batch_update не приймає value_input_option.
        google_call_with_retry(lambda: ws.batch_update(updates))
    except Exception as e:
        print("write_user_status_batch error:", e)
        # Запасний варіант: якщо batch_update не спрацював, пишемо по клітинках.
        for upd in updates:
            try:
                rng = upd.get("range", "")
                values = upd.get("values", [[""]])
                if not rng:
                    continue
                # Очікуємо формат на кшталт J25.
                letters = "".join(ch for ch in rng if ch.isalpha())
                digits = "".join(ch for ch in rng if ch.isdigit())
                if not letters or not digits:
                    continue
                col = 0
                for ch in letters.upper():
                    col = col * 26 + (ord(ch) - 64)
                row = int(digits)
                value = values[0][0] if values and values[0] else ""
                google_call_with_retry(lambda row=row, col=col, value=value: ws.update_cell(row, col, value))
            except Exception as e2:
                print("write_user_status_batch fallback cell error:", e2)


def process_users_status_check(limit=None, force=False):
    """
    Перевіряє статус користувачів пакетами.

    Як працює limit:
    - якщо limit не передано і force=False → перевіряємо CHECK_USERS_STATUS_LIMIT_PER_RUN;
    - якщо limit не передано і force=True → перевіряємо всіх користувачів;
    - якщо limit=all / full / 0 → перевіряємо всіх користувачів;
    - якщо limit=50 → перевіряємо максимум 50 користувачів.

    Це потрібно, щоб /check-users-status?force=1 реально перевіряв усю базу,
    а не тільки перші 50 користувачів.
    """
    result = {
        "checked": 0,
        "active": 0,
        "blocked": 0,
        "unavailable": 0,
        "errors": 0,
        "skipped": 0,
        "total_users": 0,
        "limit": ""
    }

    try:
        raw_limit = "" if limit is None else str(limit).strip().lower()

        if raw_limit in ["all", "full", "всі", "усі", "0", "-1"]:
            max_to_check = None
        elif raw_limit:
            try:
                parsed_limit = int(float(raw_limit))
                max_to_check = parsed_limit if parsed_limit > 0 else None
            except Exception:
                max_to_check = CHECK_USERS_STATUS_LIMIT_PER_RUN
        else:
            # Для ручної повної перевірки /check-users-status?force=1 перевіряємо всіх.
            # Для звичайного планового запуску лишаємо безпечну пачку.
            max_to_check = None if force else CHECK_USERS_STATUS_LIMIT_PER_RUN

        result["limit"] = "all" if max_to_check is None else str(max_to_check)

        ws, cols = ensure_user_columns(["Статус бота", "Дата перевірки статусу", "Остання помилка бота"])
        if not ws or not cols:
            return result

        rows = google_call_with_retry(lambda: ws.get_all_values())
        if len(rows) <= 1:
            return result

        status_col = cols.get("Статус бота")
        date_col = cols.get("Дата перевірки статусу")
        error_col = cols.get("Остання помилка бота")
        now_value = now_str()
        updates = []

        user_rows = []
        for row_index, row in enumerate(rows[1:], start=2):
            telegram_id = str(row[0] if len(row) > 0 else "").strip()
            if telegram_id:
                user_rows.append((row_index, row, telegram_id))

        result["total_users"] = len(user_rows)

        for row_index, row, telegram_id in user_rows:
            if max_to_check is not None and result["checked"] >= max_to_check:
                # Решту не перевіряли саме через ліміт запуску.
                result["skipped"] += 1
                continue

            if not should_check_user_status(row, cols, force=force):
                result["skipped"] += 1
                continue

            status, error_text = check_telegram_user_status(telegram_id)
            result["checked"] += 1

            status_lower = str(status).lower()
            if status == "Активний":
                result["active"] += 1
            elif "заблок" in status_lower:
                result["blocked"] += 1
            elif "помилка" in status_lower:
                result["errors"] += 1
            else:
                result["unavailable"] += 1

            updates.append({
                "range": f"{col_to_letter(status_col)}{row_index}",
                "values": [[status]]
            })
            updates.append({
                "range": f"{col_to_letter(date_col)}{row_index}",
                "values": [[now_value]]
            })
            updates.append({
                "range": f"{col_to_letter(error_col)}{row_index}",
                "values": [[str(error_text or '')[:500]]]
            })

            # Пишемо пачками, щоб не накопичувати великий список і не впертися в timeout.
            if len(updates) >= 150:
                write_user_status_batch(ws, updates)
                updates = []

            # Невелика пауза, щоб не бити Telegram занадто швидко.
            time.sleep(0.05)

        write_user_status_batch(ws, updates)

        if result["checked"]:
            clear_cache("Користувачі")

        print("process_users_status_check:", result)
        return result

    except Exception as e:
        print("process_users_status_check error:", e)
        return result

def send_message(chat_id, text, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }

    if keyboard:
        keyboard = normalize_inline_keyboard(keyboard)
        payload["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)

    try:
        response = requests.post(f"{BASE_URL}/sendMessage", json=payload, timeout=15)

        if response.ok:
            data = response.json()
            # Для швидкості не пишемо в Google Sheets після кожної успішної відправки.
            # Якщо потрібно повернути стару поведінку — поставте TRACK_ACTIVE_STATUS_ON_SUCCESS=1 у Render Environment.
            if TRACK_ACTIVE_STATUS_ON_SUCCESS:
                update_user_bot_status(chat_id, "Активний", "")
            return data.get("result", {}).get("message_id")

        print("send_message telegram error:", response.text)
        update_user_bot_status(chat_id, classify_telegram_send_error(response.text), response.text, force=True)
        return None

    except Exception as e:
        print("send_message error:", e)
        return None


def delete_message(chat_id, message_id):
    if not message_id:
        return

    try:
        requests.post(
            f"{BASE_URL}/deleteMessage",
            json={"chat_id": chat_id, "message_id": message_id},
            timeout=10
        )
    except Exception as e:
        print("delete_message error:", e)


def send_chat_action(chat_id, action="typing"):
    try:
        requests.post(
            f"{BASE_URL}/sendChatAction",
            json={"chat_id": chat_id, "action": action},
            timeout=10
        )
    except Exception as e:
        print("send_chat_action error:", e)


def send_loading(chat_id, text="⏳ Обробляємо Ваш запит..."):
    send_chat_action(chat_id, "typing")
    return send_message(chat_id, text)


def with_loading(chat_id, loading_text, func, *args, **kwargs):
    loading_message_id = send_loading(chat_id, loading_text)
    try:
        return func(*args, **kwargs)
    finally:
        delete_message(chat_id, loading_message_id)


def send_photo(chat_id, photo_url, caption, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "photo": photo_url,
        "caption": caption,
        "parse_mode": "HTML"
    }

    if keyboard:
        keyboard = normalize_inline_keyboard(keyboard)
        payload["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)

    try:
        response = requests.post(f"{BASE_URL}/sendPhoto", json=payload, timeout=15)

        if not response.ok:
            print("send_photo telegram error:", response.text)
            update_user_bot_status(chat_id, classify_telegram_send_error(response.text), response.text, force=True)
            return False

        data = response.json()
        if TRACK_ACTIVE_STATUS_ON_SUCCESS:
            update_user_bot_status(chat_id, "Активний", "")
        return data.get("result", {}).get("message_id") or True

    except Exception as e:
        print("send_photo error:", e)
        return False



def send_document(chat_id, document_url, caption="", keyboard=None):
    payload = {
        "chat_id": chat_id,
        "document": document_url,
        "caption": caption,
        "parse_mode": "HTML"
    }

    if keyboard:
        keyboard = normalize_inline_keyboard(keyboard)
        payload["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)

    try:
        response = requests.post(f"{BASE_URL}/sendDocument", json=payload, timeout=20)

        if not response.ok:
            print("send_document telegram error:", response.text)
            update_user_bot_status(chat_id, classify_telegram_send_error(response.text), response.text, force=True)
            return False

        data = response.json()
        if TRACK_ACTIVE_STATUS_ON_SUCCESS:
            update_user_bot_status(chat_id, "Активний", "")
        return data.get("result", {}).get("message_id") or True

    except Exception as e:
        print("send_document error:", e)
        return False



def edit_message(chat_id, message_id, text, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML"
    }

    if keyboard:
        keyboard = normalize_inline_keyboard(keyboard)
        payload["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)

    try:
        response = requests.post(f"{BASE_URL}/editMessageText", json=payload, timeout=15)
        if not response.ok:
            print("edit_message telegram error:", response.text)
            return False
        return True
    except Exception as e:
        print("edit_message error:", e)
        return False


def edit_caption(chat_id, message_id, caption, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "caption": caption,
        "parse_mode": "HTML"
    }

    if keyboard:
        keyboard = normalize_inline_keyboard(keyboard)
        payload["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)

    try:
        requests.post(f"{BASE_URL}/editMessageCaption", json=payload, timeout=15)
    except Exception as e:
        print("edit_caption error:", e)



def edit_media_photo(chat_id, message_id, photo_url, caption, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "media": json.dumps({
            "type": "photo",
            "media": photo_url,
            "caption": caption,
            "parse_mode": "HTML"
        }, ensure_ascii=False)
    }

    if keyboard:
        keyboard = normalize_inline_keyboard(keyboard)
        payload["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)

    try:
        r = requests.post(f"{BASE_URL}/editMessageMedia", data=payload, timeout=15)
        if not r.ok:
            print("edit_media_photo telegram error:", r.text)
    except Exception as e:
        print("edit_media_photo error:", e)



def answer_callback(callback_id, text=None, show_alert=False):
    try:
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = str(text)[:200]
            payload["show_alert"] = bool(show_alert)

        requests.post(
            f"{BASE_URL}/answerCallbackQuery",
            json=payload,
            timeout=15
        )
    except Exception as e:
        print("answer_callback error:", e)


def callback_loading_text(data_value):
    data_value = str(data_value or "")

    if data_value.startswith("photo_") or data_value.startswith("more_photos_"):
        return "📸 Завантажуємо фото..."
    if data_value.startswith("products_page_") or data_value.startswith("catpage_"):
        return "📦 Завантажуємо товари..."
    if data_value.startswith("sale_page_"):
        return "🔥 Завантажуємо акцію..."
    if data_value.startswith("add_one_"):
        return "🛒 Додаємо товар у кошик..."
    if data_value.startswith("cart_plus_") or data_value.startswith("cart_minus_") or data_value.startswith("cart_qty_"):
        return "🛒 Оновлюємо кошик..."
    if data_value.startswith("promo_product_"):
        return "🛍 Завантажуємо товар..."
    if data_value.startswith("contact_product_"):
        return "📞 Готуємо заявку менеджеру..."
    if data_value in ["open_catalog", "add_more_products"]:
        return "📦 Відкриваємо каталог..."
    if data_value == "open_orders":
        return "📦 Завантажуємо замовлення..."
    if data_value == "open_delivery_payment":
        return "🚚 Завантажуємо доставку та оплату..."
    if data_value in ["contact_manager_general", "manager_order"]:
        return "📞 Готуємо заявку менеджеру..."
    if data_value == "open_admin":
        return "👑 Відкриваємо кабінет..."
    if data_value in ["open_cart", "continue_checkout"]:
        return "🛒 Завантажуємо кошик..."
    if data_value in ["bonus_use", "bonus_disable", "open_bonus_cabinet"]:
        return "🎁 Оновлюємо бонуси..."
    if data_value.startswith("delivery_"):
        return "🚚 Обираємо доставку..."
    if data_value.startswith("payment_"):
        return "💳 Обираємо оплату..."
    if data_value.startswith("admin_") or data_value.startswith("set_status_") or data_value.startswith("mark_"):
        return "👑 Оновлюємо кабінет..."
    if data_value.startswith("contact_"):
        return "📞 Завантажуємо заявки..."

    return "⏳ Обробляємо Ваш запит..."


def main_menu(is_admin=False):
    """
    Старе нижнє меню більше не показуємо.
    Цю функцію залишаємо як безпечний fallback: вона прибирає Reply Keyboard,
    якщо він залишився у клієнта після попередніх версій бота.
    """
    return {"remove_keyboard": True}


def main_menu_inline(is_admin=False):
    buttons = [
        [inline_button("📦 Каталог", "open_catalog"), inline_button("🔥 Акції", "open_sales")],
        [inline_button("🛒 Кошик", "open_cart"), inline_button("📦 Мої замовлення", "open_orders")],
        [inline_button("🎁 Мої бонуси", "open_bonus_cabinet"), inline_button("👥 Реферальна програма", "open_referral_program")],
        [inline_button("📞 Зв’язатися з менеджером", "contact_manager_general")],
        [inline_button("📞 Оформити через менеджера", "manager_order")],
        [inline_button("🚚 Доставка і оплата", "open_delivery_payment")]
    ]

    if is_admin:
        buttons.append([inline_button("👑 Кабінет", "open_admin")])

    return {"inline_keyboard": buttons}


def remove_reply_keyboard(chat_id):
    """
    Акуратно прибирає старі нижні кнопки. Службове повідомлення одразу видаляється,
    щоб не засмічувати чат.
    """
    try:
        message_id = send_message(chat_id, "Оновлюємо меню…", {"remove_keyboard": True})
        if message_id:
            delete_message(chat_id, message_id)
    except Exception as e:
        print("remove_reply_keyboard error:", e)


def categories_menu():
    categories = get_active_categories()
    keyboard = []

    row = []
    for cat in categories:
        row.append({"text": f"📁 {cat.get('Назва категорії')}"})

        if len(row) == 2:
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)

    keyboard.append([{"text": "⬅️ Назад"}])

    return {
        "keyboard": keyboard,
        "resize_keyboard": True
    }



def subcategories_menu(category_id):
    subcategories = get_active_subcategories(category_id)
    keyboard = []

    row = []
    for subcategory in subcategories:
        row.append({"text": f"📂 {subcategory.get('Назва підкатегорії')}"})

        if len(row) == 2:
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)

    keyboard.append([{"text": "⬅️ Назад"}])

    return {
        "keyboard": keyboard,
        "resize_keyboard": True
    }


def subsections_menu(subcategory_id):
    subsections = get_active_subsections(subcategory_id)
    keyboard = []

    row = []
    for subsection in subsections:
        row.append({"text": f"▫️ {subsection.get('Назва підрозділу')}"})

        if len(row) == 2:
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)

    keyboard.append([{"text": "⬅️ Назад"}])

    return {
        "keyboard": keyboard,
        "resize_keyboard": True
    }


def get_subcategory_by_button_text(text, category_id=None):
    clean_text = str(text).replace("📂", "").strip()
    subcategories = get_cached_records("Підкатегорії")

    for subcategory in subcategories:
        active = str(subcategory.get("Активна", "")).strip().lower()
        name = str(subcategory.get("Назва підкатегорії", "")).strip()
        item_category_id = str(subcategory.get("ID категорії", "")).strip()

        if active in ["так", "yes", "1", "true", "активна"] and name == clean_text:
            if category_id is None or str(category_id) == item_category_id:
                return subcategory

    return None


def get_subsection_by_button_text(text, subcategory_id=None):
    clean_text = str(text).replace("▫️", "").strip()
    subsections = get_cached_records("Підрозділи")

    for subsection in subsections:
        active = str(subsection.get("Активна", subsection.get("Активний", ""))).strip().lower()
        name = str(subsection.get("Назва підрозділу", "")).strip()
        item_subcategory_id = str(subsection.get("ID підкатегорії", "")).strip()

        if active in ["так", "yes", "1", "true", "активна", "активний"] and name == clean_text:
            if subcategory_id is None or str(subcategory_id) == item_subcategory_id:
                return subsection

    return None

def inline_button(text, callback_data):
    return {"text": text, "callback_data": callback_data}


def safe_text(value, default="—"):
    value = str(value or "").strip()
    return value if value else default


def safe_float(value, default=0):
    try:
        if value is None or value == "":
            return float(default or 0)

        value = str(value).strip()
        value = value.replace(" ", "").replace("грн", "").replace("UAH", "")
        value = value.replace(",", ".")

        return float(value or default)
    except:
        return float(default or 0)


def safe_int(value, default=0):
    try:
        return int(float(str(value or default).replace(",", ".")))
    except:
        return int(default or 0)


def normalize_sale_text(value):
    return str(value or "").strip()


def get_product_sale_text(product):
    if not product or not is_product_sale_active(product):
        return ""
    return normalize_sale_text(product.get("Акція") or product.get("Акція 1=2") or product.get("Тип акції") or "")


def parse_promo_deal(sale_text):
    """
    Повертає умови акції для кошика.
    1=2      → клієнт отримує 2 шт, платить за 1
    1+1=3    → клієнт отримує 3 шт, платить за 2
    """
    original = normalize_sale_text(sale_text)
    compact = original.lower().replace(" ", "")
    compact = compact.replace("акція", "")

    if "1+1=3" in compact or "1+1+1" in compact:
        return {
            "label": original or "Акція 1+1=3",
            "paid_qty": 2,
            "receive_qty": 3
        }

    if "1=2" in compact or "1+1" in compact:
        return {
            "label": original or "Акція 1=2",
            "paid_qty": 1,
            "receive_qty": 2
        }

    return None


def get_product_promo_deal(product):
    return parse_promo_deal(get_product_sale_text(product))


# =========================
# PROMO GIFT: PRODUCT + GIFT FOR 1 UAH
# =========================

PROMO_GIFT_ROW_PREFIX = "PROMO_GIFT__"


def promo_gift_cart_id(gift_product_id):
    return f"{PROMO_GIFT_ROW_PREFIX}{str(gift_product_id).strip()}"


def is_promo_gift_cart_id(product_id):
    return str(product_id or "").startswith(PROMO_GIFT_ROW_PREFIX)


def promo_gift_original_product_id(product_id):
    product_id = str(product_id or "").strip()
    if is_promo_gift_cart_id(product_id):
        return product_id.replace(PROMO_GIFT_ROW_PREFIX, "", 1)
    return product_id


def get_promo_gift_config(product):
    """
    Універсальна акція: при купівлі товару автоматично додається подарунок/додатковий товар
    за спеціальною ціною.

    У таблиці "Товари" для основного товару заповнюємо:
    - Акція = наприклад "Кушон + SPF за 1 грн"
    - Акція від / Акція до = період дії
    - Подарунок ID = ID товару-подарунка
    - Ціна подарунка = ціна, за яку він додається у кошик
    """
    if not product or not is_product_sale_active(product):
        return None

    gift_id = str(
        product.get("Подарунок ID")
        or product.get("ID подарунка")
        or product.get("ID подарунку")
        or product.get("Подарунок товар ID")
        or ""
    ).strip()

    if not gift_id:
        return None

    gift_price = safe_float(
        product.get("Ціна подарунка")
        or product.get("Ціна подарунку")
        or product.get("Подарунок ціна")
        or product.get("Ціна подарункового товару")
        or 1,
        1
    )

    if gift_price <= 0:
        gift_price = 1

    gift_product = get_product_by_id(gift_id)
    gift_name = safe_text(
        gift_product.get("Назва товару") if gift_product else "",
        "Подарунок за акцією"
    )

    sale_label = get_product_sale_text(product) or "Акція"

    return {
        "gift_id": gift_id,
        "cart_product_id": promo_gift_cart_id(gift_id),
        "gift_name": gift_name,
        "gift_price": round(gift_price, 2),
        "sale_label": sale_label
    }


def promo_gift_text_for_product(product):
    config = get_promo_gift_config(product)
    if not config:
        return ""

    gift_name = config.get("gift_name", "Подарунок за акцією")
    gift_price = config.get("gift_price", 1)

    return (
        f"🎁 До цього товару за умовами акції можна отримати:\n"
        f"<b>{gift_name}</b> всього за <b>{gift_price} грн</b>"
    )


def sync_cart_promo_gifts(chat_id):
    """
    Синхронізує подарунки у кошику:
    - є акційний кушон/товар → додає подарунок за 1 грн;
    - кількість основного товару змінилась → змінює кількість подарунка;
    - основний товар видалили або акція завершилась → прибирає подарунок.
    """
    try:
        items = find_user_cart_rows(chat_id)
        requirements = {}
        existing_gift_rows = {}

        for item in items:
            product_id = str(item.get("product_id") or "").strip()

            if is_promo_gift_cart_id(product_id):
                original_gift_id = promo_gift_original_product_id(product_id)
                existing_gift_rows.setdefault(original_gift_id, []).append(item)
                continue

            product = get_product_by_id(product_id)
            config = get_promo_gift_config(product)
            if not config:
                continue

            qty = safe_int(item.get("qty") or 1, 1)
            if qty <= 0:
                continue

            gift_id = config["gift_id"]
            if gift_id not in requirements:
                requirements[gift_id] = {
                    "cart_product_id": config["cart_product_id"],
                    "gift_name": config["gift_name"],
                    "gift_price": config["gift_price"],
                    "qty": 0,
                    "sum": 0,
                    "labels": []
                }

            requirements[gift_id]["qty"] += qty
            requirements[gift_id]["sum"] = round(
                requirements[gift_id]["sum"] + qty * config["gift_price"],
                2
            )
            if config.get("sale_label") and config.get("sale_label") not in requirements[gift_id]["labels"]:
                requirements[gift_id]["labels"].append(config.get("sale_label"))

        changed = False

        # Оновлюємо або створюємо потрібні подарунки.
        for gift_id, req in requirements.items():
            rows = existing_gift_rows.get(gift_id, [])
            display_name = req["gift_name"]
            if req.get("labels"):
                display_name = f"{display_name} ({req['labels'][0]})"

            if rows:
                main_row = rows[0]
                row_index = main_row["row_index"]
                old_name = str(main_row.get("name") or "")
                old_price = safe_float(main_row.get("price") or 0)
                old_qty = safe_int(main_row.get("qty") or 0)
                old_sum = safe_float(main_row.get("sum") or 0)

                if old_name != display_name:
                    update_cell("Кошик", row_index, 3, display_name)
                    changed = True
                if old_price != req["gift_price"]:
                    update_cell("Кошик", row_index, 4, req["gift_price"])
                    changed = True
                if old_qty != req["qty"]:
                    update_cell("Кошик", row_index, 5, req["qty"])
                    changed = True
                if old_sum != req["sum"]:
                    update_cell("Кошик", row_index, 6, req["sum"])
                    changed = True

                update_cart_reminder_columns(row_index, updated_at=now_str(), reminder1="", reminder2="", reminder3="")

                # Якщо раптом дублікати подарунка — видаляємо зайві.
                for extra in rows[1:]:
                    delete_row("Кошик", extra["row_index"])
                    changed = True
            else:
                get_cart_worksheet()
                append_row("Кошик", [
                    chat_id,
                    req["cart_product_id"],
                    display_name,
                    req["gift_price"],
                    req["qty"],
                    req["sum"],
                    now_str(),
                    "",
                    "",
                    ""
                ])
                changed = True

        # Видаляємо подарунки, якщо акційного товару вже немає або акція завершилась.
        for gift_id, rows in existing_gift_rows.items():
            if gift_id not in requirements:
                for row in reversed(rows):
                    delete_row("Кошик", row["row_index"])
                    changed = True

        if changed:
            clear_cache("Кошик")

        return changed

    except Exception as e:
        print("sync_cart_promo_gifts error:", e)
        return False


def get_cart_item_product(item):
    product_id = item.get("product_id") or item.get("ID товару") or item.get("id") or ""
    if product_id:
        try:
            return get_product_by_id(product_id)
        except Exception:
            pass
    return None


def format_cart_item_line(item):
    name = item.get("name") or item.get("Назва товару") or "Товар"
    product = get_cart_item_product(item)
    promo = get_product_promo_deal(product)

    price = safe_float(item.get("price") or item.get("Ціна") or 0)
    qty = safe_int(item.get("qty") or item.get("Кількість") or 1, 1)
    summa = safe_float(item.get("sum") or item.get("Сума") or price * qty)

    if is_promo_gift_cart_id(item.get("product_id") or item.get("ID товару")):
        return f"🎁 {name} — {qty} шт. × {price} грн = <b>{summa} грн</b>\n"

    if promo:
        paid_qty = promo.get("paid_qty", 1)
        receive_qty = promo.get("receive_qty", 1)
        label = promo.get("label", "Акція")
        packs = qty / receive_qty if receive_qty else qty
        paid_units = packs * paid_qty

        if abs(packs - round(packs)) < 0.001:
            packs = int(round(packs))
        if abs(paid_units - round(paid_units)) < 0.001:
            paid_units = int(round(paid_units))

        if label:
            return f"• {name} ({label}) — {qty} шт. / оплата за {paid_units} шт. = <b>{summa} грн</b>\n"
        return f"• {name} — {qty} шт. / оплата за {paid_units} шт. = <b>{summa} грн</b>\n"

    return f"• {name} — {qty} шт. × {price} грн = <b>{summa} грн</b>\n"


def format_cart_item_for_order(item):
    name = item.get("Назва товару") or item.get("name") or "Товар"
    product_id = item.get("ID товару") or item.get("product_id") or ""

    if is_promo_gift_cart_id(product_id):
        qty = safe_int(item.get("Кількість") or item.get("qty") or 1, 1)
        summa = safe_float(item.get("Сума") or item.get("sum") or 0)
        return f"{name} x{qty} шт. = {summa} грн"

    product = get_product_by_id(product_id) if product_id else None
    promo = get_product_promo_deal(product)
    qty = safe_int(item.get("Кількість") or item.get("qty") or 1, 1)
    summa = safe_float(item.get("Сума") or item.get("sum") or 0)

    if promo:
        label = promo.get("label", "Акція")
        return f"{name} ({label}) x{qty} шт. = {summa} грн"

    return f"{name} x{qty}"


def back_to_main_inline(is_admin_user=False):
    """Повернення на головну через повне inline-меню."""
    return main_menu_inline(is_admin_user)


def get_admins():
    """
    Адміністратори беруться з листа Google Sheets: "Адміністратори".
    Колонки: Telegram ID | ПІБ | Роль
    Ролі, які мають доступ до кабінету: owner, manager, operator.

    ADMIN_CHAT_ID з Render залишений як запасний варіант,
    щоб власник не втратив доступ, якщо лист ще не створений.
    """
    admins = []

    try:
        headers = ["Telegram ID", "ПІБ", "Роль"]
        ws = get_or_create_worksheet("Адміністратори", headers)
        rows = google_call_with_retry(lambda: ws.get_all_records())

        for row in rows:
            telegram_id = str(row.get("Telegram ID", "")).strip()
            full_name = str(row.get("ПІБ", "")).strip()
            role = str(row.get("Роль", "manager")).strip().lower()

            if telegram_id:
                admins.append({
                    "telegram_id": telegram_id,
                    "full_name": full_name,
                    "role": role or "manager"
                })

    except Exception as e:
        print("get_admins error:", e)

    # Запасний адмін із Render Environment Variable
    if str(ADMIN_CHAT_ID).strip():
        fallback_id = str(ADMIN_CHAT_ID).strip()
        if not any(a.get("telegram_id") == fallback_id for a in admins):
            admins.append({
                "telegram_id": fallback_id,
                "full_name": "",
                "role": "owner"
            })

    return admins


def get_user_role(chat_id):
    for admin in get_admins():
        if str(admin.get("telegram_id")).strip() == str(chat_id).strip():
            return str(admin.get("role", "manager")).strip().lower()

    return "user"


def is_admin(chat_id):
    return get_user_role(chat_id) in ["owner", "manager", "operator"]


def get_admin_ids(include_roles=None):
    if include_roles is None:
        include_roles = ["owner", "manager", "operator"]

    ids = []

    for admin in get_admins():
        role = str(admin.get("role", "")).strip().lower()
        telegram_id = str(admin.get("telegram_id", "")).strip()

        if telegram_id and role in include_roles and telegram_id not in ids:
            ids.append(telegram_id)

    return ids


# =========================
# USERS / CLIENT MONITORING
# =========================

def get_users_worksheet():
    headers = [
        "Telegram ID",
        "Username",
        "Імʼя",
        "Прізвище",
        "Дата першого входу",
        "Дата останньої активності",
        "Кількість входів"
    ]
    return get_or_create_worksheet("Користувачі", headers)


def register_user_activity(chat_id, user=None):
    """
    Фіксуємо кожного користувача, який взаємодіє з ботом.
    Це дає можливість бачити у кабінеті кількість користувачів,
    нових за сьогодні / місяць та активність.
    """
    try:
        user = user or {}
        telegram_id = str(chat_id).strip()
        last_activity = USER_ACTIVITY_CACHE.get(telegram_id)
        if last_activity and (current_time() - last_activity).total_seconds() < USER_ACTIVITY_THROTTLE_SECONDS:
            return False

        ws = get_users_worksheet()
        rows = google_call_with_retry(lambda: ws.get_all_values())

        now = current_time().strftime("%d.%m.%Y %H:%M")
        username = str(user.get("username", "") or "").strip()
        first_name = str(user.get("first_name", "") or "").strip()
        last_name = str(user.get("last_name", "") or "").strip()

        for i, row in enumerate(rows[1:], start=2):
            if len(row) > 0 and str(row[0]).strip() == telegram_id:
                try:
                    visits = int(safe_float(row[6])) if len(row) > 6 and str(row[6]).strip() else 0
                except:
                    visits = 0

                if username:
                    ws.update_cell(i, 2, username)
                if first_name:
                    ws.update_cell(i, 3, first_name)
                if last_name:
                    ws.update_cell(i, 4, last_name)

                google_call_with_retry(lambda: ws.update_cell(i, 6, now))
                google_call_with_retry(lambda: ws.update_cell(i, 7, visits + 1))
                clear_cache("Користувачі")
                USER_ACTIVITY_CACHE[telegram_id] = current_time()
                return False

        # ВАЖЛИВО:
        # Не використовуємо append_row для листа "Користувачі",
        # бо Google Sheets іноді бачить окрему "таблицю" праворуч
        # і додає нового користувача не в A:G, а в I:O.
        # Тому знаходимо наступний вільний рядок саме по колонках A:G
        # і записуємо дані чітко в діапазон A:G.
        last_user_row = 1
        for row_index, row in enumerate(rows[1:], start=2):
            left_part = row[:7]
            if any(str(cell).strip() for cell in left_part):
                last_user_row = row_index

        next_row = last_user_row + 1

        google_call_with_retry(lambda: ws.update(
            f"A{next_row}:G{next_row}",
            [[
                telegram_id,
                username,
                first_name,
                last_name,
                now,
                now,
                1
            ]],
            value_input_option="USER_ENTERED"
        ))
        clear_cache("Користувачі")
        USER_ACTIVITY_CACHE[telegram_id] = current_time()
        return True

    except Exception as e:
        print("register_user_activity error:", e)
        return False


def parse_bot_datetime(value):
    value = str(value or "").strip()

    for fmt in ["%d.%m.%Y %H:%M", "%d.%m.%Y"]:
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.replace(tzinfo=KYIV_TZ)
        except:
            pass

    return None


def get_clients_monitoring_stats():
    try:
        users_rows = get_values("Користувачі")[1:]
    except Exception as e:
        print("get users stats error:", e)
        users_rows = []

    orders = get_orders_with_rows()
    now = current_time()
    today_prefix = now.strftime("%d.%m.%Y")
    month_part = now.strftime(".%m.%Y")

    total_users = 0
    new_today = 0
    new_month = 0
    active_users = 0
    blocked_users = 0
    inactive_users = 0

    headers = []
    try:
        all_users_values = get_values("Користувачі")
        headers = all_users_values[0] if all_users_values else []
    except Exception:
        headers = []
    status_col_idx = None
    for idx, header in enumerate(headers):
        if str(header).strip().lower() == "статус бота":
            status_col_idx = idx
            break

    for row in users_rows:
        if not row or not str(row[0]).strip():
            continue

        total_users += 1
        first_seen = row[4] if len(row) > 4 else ""

        if str(first_seen).startswith(today_prefix):
            new_today += 1

        if month_part in str(first_seen):
            new_month += 1

        bot_status = str(row[status_col_idx] if status_col_idx is not None and len(row) > status_col_idx else "").strip().lower()
        if bot_status == "активний":
            active_users += 1
        elif "заблок" in bot_status:
            blocked_users += 1
        elif bot_status:
            inactive_users += 1

    order_count = len(orders)

    orders_by_user = {}
    for order in orders:
        telegram_id = str(order.get("Telegram ID", "")).strip()
        if not telegram_id:
            continue
        orders_by_user[telegram_id] = orders_by_user.get(telegram_id, 0) + 1

    repeat_clients = sum(1 for count in orders_by_user.values() if count >= 2)

    return {
        "total_users": total_users,
        "new_today": new_today,
        "new_month": new_month,
        "order_count": order_count,
        "repeat_clients": repeat_clients,
        "active_users": active_users,
        "blocked_users": blocked_users,
        "inactive_users": inactive_users
    }


def clients_stats_text():
    stats = get_clients_monitoring_stats()

    return (
        "👥 <b>Клієнти</b>\n\n"
        f"Всього користувачів: <b>{stats['total_users']}</b>\n"
        f"Нових сьогодні: <b>{stats['new_today']}</b>\n"
        f"Нових за місяць: <b>{stats['new_month']}</b>\n"
        f"Замовлень: <b>{stats['order_count']}</b>\n"
        f"Повторних клієнтів: <b>{stats['repeat_clients']}</b>"
    )


def show_clients_stats(chat_id, callback_message=None):
    if not is_admin(chat_id):
        send_message(chat_id, "Цей розділ доступний тільки адміністратору.", main_menu(False))
        return

    text = clients_stats_text()
    keyboard = {"inline_keyboard": [[inline_button("⬅️ Назад у кабінет", "admin_back")]]}

    if callback_message:
        edit_message(chat_id, callback_message["message_id"], text, keyboard)
    else:
        send_message(chat_id, text, keyboard)




# =========================
# BONUS / REFERRAL PROGRAM
# =========================

BONUS_RATE_UAH = 1
WELCOME_BONUS_AMOUNT = float(os.environ.get("WELCOME_BONUS_AMOUNT", "100"))
WELCOME_BONUS_BROADCAST_LIMIT_PER_RUN = int(os.environ.get("WELCOME_BONUS_BROADCAST_LIMIT_PER_RUN", "30"))
MENU_UPDATE_LIMIT_PER_RUN = int(os.environ.get("MENU_UPDATE_LIMIT_PER_RUN", "30"))
REFERRAL_BONUS_AMOUNT = int(os.environ.get("REFERRAL_BONUS_AMOUNT", "50"))
REFERRAL_MIN_ORDER_SUM = float(os.environ.get("REFERRAL_MIN_ORDER_SUM", "500"))
BONUS_MAX_USE_PERCENT = float(os.environ.get("BONUS_MAX_USE_PERCENT", "30"))
BONUS_VALID_DAYS = int(os.environ.get("BONUS_VALID_DAYS", "60"))


# Короткі callback-коди для Telegram.
# Telegram дозволяє callback_data максимум 64 байти, тому не можна передавати довгі українські назви в кнопках.
DELIVERY_METHODS = {
    "np": "Нова пошта",
    "ukr": "Укрпошта"
}

PAYMENT_METHODS = {
    "iban": "Оплата за реквізитами IBAN",
    "cod": "Накладений платіж"
}

ORDER_STATUS_CODES = {
    "new": "Нове",
    "pay": "Очікується оплата",
    "work": "В обробці",
    "sent": "Відправлено",
    "done": "Завершено",
    "cancel": "Скасовано"
}

ORDER_STATUS_TO_CODE = {v: k for k, v in ORDER_STATUS_CODES.items()}


def get_bonus_worksheet():
    headers = [
        "Дата",
        "Telegram ID",
        "Тип",
        "Сума",
        "Залишок",
        "Діє до",
        "Статус",
        "Коментар",
        "Рядок замовлення"
    ]
    return get_or_create_worksheet("Бонуси", headers)


def get_referrals_worksheet():
    headers = [
        "Дата",
        "Хто запросив Telegram ID",
        "Запрошений Telegram ID",
        "Телефон запрошеного",
        "Рядок замовлення",
        "Сума замовлення",
        "Статус",
        "Бонус нараховано"
    ]
    return get_or_create_worksheet("Реферали", headers)


def bonus_expiry_date():
    from datetime import timedelta
    return (current_time() + timedelta(days=BONUS_VALID_DAYS)).strftime("%d.%m.%Y")


def get_bonus_rows():
    try:
        return get_values("Бонуси")
    except Exception as e:
        print("get_bonus_rows error:", e)
        return []


def get_available_bonus_balance(chat_id):
    """
    Рахуємо доступні бонуси.
    Плюсові бонуси беруться зі статусом Активний.
    Списання записується окремим рядком з від'ємною сумою.
    Прострочені бонуси не враховуємо.
    """
    rows = get_bonus_rows()
    today = current_time().date()
    balance = 0

    for row in rows[1:]:
        if len(row) < 7:
            continue

        telegram_id = str(row[1]).strip()
        if telegram_id != str(chat_id):
            continue

        status = str(row[6] if len(row) > 6 else "").strip().lower()
        if status not in ["активний", "списано", "використано"]:
            continue

        try:
            amount = safe_float(row[3] if len(row) > 3 else 0)
        except:
            amount = 0

        expires_raw = row[5] if len(row) > 5 else ""
        expires = parse_bot_datetime(expires_raw)
        if amount > 0 and expires and expires.date() < today:
            continue

        balance += amount

    return max(0, round(balance, 2))


def is_bonus_eligible_product(product):
    """
    Бонуси можна списувати тільки на неакційні товари.
    Якщо у товару активна акція, акційна ціна, стара ціна або подарунок за акцією — бонуси на нього не застосовуються.
    """
    if not product:
        return False

    try:
        return not is_product_sale_active(product)
    except Exception:
        return False


def is_bonus_eligible_cart_item(item):
    product_id = item.get("product_id") or item.get("ID товару") or item.get("id") or ""

    if is_promo_gift_cart_id(product_id):
        return False

    product = get_product_by_id(product_id) if product_id else None
    return is_bonus_eligible_product(product)


def calculate_bonus_to_use(chat_id, bonus_eligible_amount):
    balance = get_available_bonus_balance(chat_id)
    max_allowed = round(float(bonus_eligible_amount or 0) * BONUS_MAX_USE_PERCENT / 100, 2)
    return max(0, min(balance, max_allowed))


def add_bonus_transaction(chat_id, amount, transaction_type, comment="", order_row_index="", status="Активний", expires_at=None):
    try:
        ws = get_bonus_worksheet()
        google_call_with_retry(lambda: ws.append_row([
            now_str(),
            chat_id,
            transaction_type,
            amount,
            amount,
            expires_at or bonus_expiry_date(),
            status,
            comment,
            order_row_index
        ], value_input_option="USER_ENTERED"))
        clear_cache("Бонуси")
        return True
    except Exception as e:
        print("add_bonus_transaction error:", e)
        return False


def spend_bonuses(chat_id, amount, order_row_index="", comment="Списання бонусів за замовлення (тільки з неакційних товарів)"):
    amount = float(amount or 0)
    if amount <= 0:
        return

    add_bonus_transaction(
        chat_id=chat_id,
        amount=-amount,
        transaction_type="Списання",
        comment=comment,
        order_row_index=order_row_index,
        status="Списано",
        expires_at=""
    )


def welcome_bonus_message_text():
    return (
        "🎁 <b>Вітаємо!</b>\n\n"
        "Ми оновили бонусну програму нашої крамнички 💛\n\n"
        "На знак подяки кожному клієнту та всім новим користувачам ми нараховуємо "
        f"<b>{int(WELCOME_BONUS_AMOUNT)} вітальних бонусів</b>.\n\n"
        f"🎁 Ваш бонусний рахунок уже поповнено на <b>{int(WELCOME_BONUS_AMOUNT)} бонусів</b>.\n\n"
        "1 бонус = 1 грн.\n\n"
        f"💰 Бонусами можна оплатити до <b>{int(BONUS_MAX_USE_PERCENT)}%</b> суми неакційних товарів.\n"
        "На акційні товари бонуси не списуються.\n\n"
        "🛍 Завітайте до каталогу та оберіть щось для себе — бонуси вже чекають на використання!\n\n"
        "Бажаємо приємних покупок 💛"
    )


def welcome_bonus_already_added(chat_id):
    """
    Перевіряємо, чи вже нараховували клієнту вітальний бонус.
    Враховуємо і бонус за перший вхід, і одноразову акційну розсилку.
    """
    try:
        rows = get_values("Бонуси")
        for row in rows[1:]:
            telegram_id = str(row[1] if len(row) > 1 else "").strip()
            transaction_type = str(row[2] if len(row) > 2 else "").strip().lower()
            if telegram_id == str(chat_id).strip() and transaction_type in [
                "бонус за перший вхід",
                "акційний вітальний бонус"
            ]:
                return True
    except Exception as e:
        print("welcome_bonus_already_added error:", e)

    return False


def grant_welcome_bonus(chat_id, only_if_new=True):
    """
    Нараховує 100 бонусів за перший вхід у бот.
    За замовчуванням працює тільки для нових користувачів.
    """
    try:
        if only_if_new is False:
            return False

        if welcome_bonus_already_added(chat_id):
            return False

        add_bonus_transaction(
            chat_id=chat_id,
            amount=WELCOME_BONUS_AMOUNT,
            transaction_type="Бонус за перший вхід",
            comment="Вітальний бонус за перший вхід у бот",
            order_row_index="",
            status="Активний",
            expires_at=bonus_expiry_date()
        )

        send_message(
            chat_id,
            welcome_bonus_message_text()
        )
        return True

    except Exception as e:
        print("grant_welcome_bonus error:", e)
        return False


def process_welcome_bonus_broadcast():
    """
    Одноразово нараховує 100 вітальних бонусів усім користувачам з листа "Користувачі".
    Повідомлення також фіксується в історії розсилок, щоб при повторному запуску не було дубля.
    """
    try:
        users_rows = get_values("Користувачі")[1:]
    except Exception as e:
        print("process_welcome_bonus_broadcast users error:", e)
        return 0

    sent_count = 0
    log_rows = []
    sent_keys, today_counts = get_broadcast_recipient_log_snapshot()
    sent_at = now_str()

    for row in users_rows:
        if sent_count >= WELCOME_BONUS_BROADCAST_LIMIT_PER_RUN:
            break

        chat_id = str(row[0] if len(row) > 0 else "").strip()
        if not chat_id:
            continue

        campaign_key = f"welcome_bonus|{chat_id}"
        unique_key = f"{chat_id}|{campaign_key}"
        if unique_key in sent_keys:
            continue

        if welcome_bonus_already_added(chat_id):
            continue

        add_bonus_transaction(
            chat_id=chat_id,
            amount=WELCOME_BONUS_AMOUNT,
            transaction_type="Акційний вітальний бонус",
            comment="Бонус за оновлення бонусної програми",
            order_row_index="",
            status="Активний",
            expires_at=bonus_expiry_date()
        )

        ok = send_message(chat_id, welcome_bonus_message_text())
        if ok:
            sent_keys.add(unique_key)
            log_rows.append([sent_at, chat_id, "Вітальний бонус", campaign_key, "Надіслано"])
            sent_count += 1
            if len(log_rows) >= BROADCAST_LOG_FLUSH_EVERY:
                append_broadcast_recipient_logs(log_rows)
                log_rows = []

    append_broadcast_recipient_logs(log_rows)
    return sent_count

def register_referral_from_start(chat_id, referrer_id):
    """
    Фіксуємо, хто кого запросив.
    Самого себе запросити не можна.
    Якщо запрошений уже є у Рефералах — повторно не додаємо.
    """
    try:
        chat_id = str(chat_id).strip()
        referrer_id = str(referrer_id).strip()

        if not chat_id or not referrer_id or chat_id == referrer_id:
            return

        ws = get_referrals_worksheet()
        rows = google_call_with_retry(lambda: ws.get_all_values())

        for row in rows[1:]:
            existing_referral = str(row[2] if len(row) > 2 else "").strip()
            if existing_referral == chat_id:
                return

        ws.append_row([
            now_str(),
            referrer_id,
            chat_id,
            "",
            "",
            "",
            "Очікує першого замовлення",
            "Ні"
        ], value_input_option="USER_ENTERED")
        clear_cache("Реферали")

        send_message(
            referrer_id,
            "👥 За Вашим реферальним посиланням перейшов новий клієнт 💛\n\n"
            "Бонус буде нараховано після його першого успішного замовлення."
        )

    except Exception as e:
        print("register_referral_from_start error:", e)


def get_referral_link(chat_id):
    bot_username = os.environ.get("BOT_USERNAME", "").strip()
    if not bot_username:
        bot_username = "kramnychka_online_ua_bot"
    return f"https://t.me/{bot_username}?start=ref_{chat_id}"


def show_bonus_cabinet(chat_id, callback_message=None):
    balance = get_available_bonus_balance(chat_id)
    referral_link = get_referral_link(chat_id)

    text = (
        "🎁 <b>Ваші бонуси</b>\n\n"
        f"Доступно бонусів: <b>{balance}</b>\n"
        f"1 бонус = 1 грн\n\n"
        f"👥 <b>Ваше реферальне посилання:</b>\n"
        f"{referral_link}\n\n"
        "За перше успішне замовлення друга від "
        f"<b>{int(REFERRAL_MIN_ORDER_SUM)} грн</b> Ви отримаєте "
        f"<b>{REFERRAL_BONUS_AMOUNT} бонусів</b>.\n\n"
        f"Бонусами можна оплатити до <b>{int(BONUS_MAX_USE_PERCENT)}%</b> суми неакційних товарів.\n"
        "На акційні товари, подарунки за 1 грн та товари зі знижкою бонуси не списуються.\n"
        f"Термін дії бонусів: <b>{BONUS_VALID_DAYS} днів</b>."
    )

    keyboard = {"inline_keyboard": [[inline_button("🛒 Перейти до кошика", "open_cart")]]}

    update_service_message(chat_id, callback_message, text, keyboard)


def show_referral_program(chat_id, callback_message=None):
    balance = get_available_bonus_balance(chat_id)
    referral_link = get_referral_link(chat_id)
    referral_stats = get_referral_stats_for_user(chat_id)

    text = (
        "👥 <b>Реферальна програма</b>\n\n"
        "Запрошуйте друзів у нашу крамничку та отримуйте бонуси за їхні покупки 💛\n\n"
        "🎁 <b>Що отримуєте Ви?</b>\n"
        "За кожного нового клієнта, який перейшов за Вашим посиланням, оформив перше замовлення "
        "та отримав його зі статусом <b>Завершено</b>, Вам нараховується "
        f"<b>{REFERRAL_BONUS_AMOUNT} бонусів</b>.\n\n"
        "💎 <b>Бонусна програма</b>\n"
        f"Після кожного завершеного замовлення Вам автоматично нараховується <b>{int(PURCHASE_BONUS_PERCENT)}%</b> "
        "від суми замовлення бонусами.\n"
        "Наприклад: замовлення на 1000 грн → 50 бонусів.\n\n"
        "💰 <b>Як використовувати бонуси?</b>\n"
        "• 1 бонус = 1 грн\n"
        f"• бонусами можна оплатити до <b>{int(BONUS_MAX_USE_PERCENT)}%</b> суми неакційних товарів\n"
        "• бонуси не застосовуються до акційних товарів, подарунків за 1 грн та товарів зі знижкою\n"
        f"• бонуси діють <b>{BONUS_VALID_DAYS} днів</b> з моменту нарахування\n\n"
        "⚠️ <b>Умови програми</b>\n"
        "• бонуси нараховуються тільки після статусу <b>Завершено</b>\n"
        "• списати бонуси можна тільки на товари без активної акції\n"
        f"• мінімальна сума першого замовлення друга для реферального бонусу — <b>{int(REFERRAL_MIN_ORDER_SUM)} грн</b>\n"
        "• бонус за друга нараховується лише за його перше успішне замовлення\n"
        "• один номер телефону може брати участь у програмі лише один раз\n"
        "• якщо замовлення скасоване або повернене, бонуси анулюються\n"
        "• запрошувати самого себе через інший акаунт заборонено\n\n"
        f"🎁 Ваші доступні бонуси: <b>{balance}</b>\n"
        f"👥 Запрошено друзів: <b>{referral_stats['invited_total']}</b>\n"
        f"✅ Успішних рефералів: <b>{referral_stats['successful']}</b>\n"
        f"⏳ Очікують першого замовлення: <b>{referral_stats['waiting']}</b>\n"
        f"💛 Нараховано за рефералку: <b>{referral_stats['bonus_total']}</b> бонусів\n\n"
        "🔗 <b>Ваше реферальне посилання:</b>\n"
        f"{referral_link}"
    )

    keyboard = {
        "inline_keyboard": [
            [inline_button("🎁 Мої бонуси", "open_bonus_cabinet")],
            [inline_button("🛒 Перейти до кошика", "open_cart")]
        ]
    }

    update_service_message(chat_id, callback_message, text, keyboard)




def get_referral_stats_for_user(chat_id):
    """
    Статистика рефералки для конкретного клієнта.
    Рахує, скільки людей перейшло за його посиланням,
    скільки вже дали бонус і скільки бонусів нараховано.
    """
    stats = {
        "invited_total": 0,
        "waiting": 0,
        "successful": 0,
        "cancelled": 0,
        "bonus_total": 0
    }

    try:
        rows = google_call_with_retry(lambda: get_referrals_worksheet().get_all_values())[1:]
        for row in rows:
            referrer_id = str(row[1] if len(row) > 1 else "").strip()
            status = str(row[6] if len(row) > 6 else "").strip().lower()
            bonus_added = str(row[7] if len(row) > 7 else "").strip().lower()

            if referrer_id != str(chat_id).strip():
                continue

            stats["invited_total"] += 1

            if bonus_added in ["так", "yes", "1", "true"] or status in ["бонус нараховано", "успішно"]:
                stats["successful"] += 1
            elif status in ["скасовано", "повернення", "телефон вже використаний", "не перше замовлення"]:
                stats["cancelled"] += 1
            else:
                stats["waiting"] += 1

        bonus_rows = get_values("Бонуси")[1:]
        for row in bonus_rows:
            telegram_id = str(row[1] if len(row) > 1 else "").strip()
            transaction_type = str(row[2] if len(row) > 2 else "").strip().lower()
            if telegram_id != str(chat_id).strip():
                continue
            if "рефераль" not in transaction_type:
                continue
            try:
                stats["bonus_total"] += safe_float(row[3] if len(row) > 3 else 0)
            except:
                pass

        stats["bonus_total"] = round(stats["bonus_total"], 2)

    except Exception as e:
        print("get_referral_stats_for_user error:", e)

    return stats


def get_admin_referral_stats():
    """
    Загальна статистика реферальної програми для кабінету адміністратора.
    """
    stats = {
        "invited_total": 0,
        "waiting": 0,
        "successful": 0,
        "cancelled": 0,
        "referrers_count": 0,
        "bonus_total": 0,
        "top_referrers": []
    }

    referrers = {}
    successful_by_referrer = {}

    try:
        rows = google_call_with_retry(lambda: get_referrals_worksheet().get_all_values())[1:]

        for row in rows:
            referrer_id = str(row[1] if len(row) > 1 else "").strip()
            status = str(row[6] if len(row) > 6 else "").strip().lower()
            bonus_added = str(row[7] if len(row) > 7 else "").strip().lower()

            if not referrer_id:
                continue

            stats["invited_total"] += 1
            referrers[referrer_id] = referrers.get(referrer_id, 0) + 1

            if bonus_added in ["так", "yes", "1", "true"] or status in ["бонус нараховано", "успішно"]:
                stats["successful"] += 1
                successful_by_referrer[referrer_id] = successful_by_referrer.get(referrer_id, 0) + 1
            elif status in ["скасовано", "повернення", "телефон вже використаний", "не перше замовлення"]:
                stats["cancelled"] += 1
            else:
                stats["waiting"] += 1

        stats["referrers_count"] = len(referrers)

        bonus_rows = get_values("Бонуси")[1:]
        for row in bonus_rows:
            transaction_type = str(row[2] if len(row) > 2 else "").strip().lower()
            if "рефераль" not in transaction_type:
                continue
            try:
                stats["bonus_total"] += safe_float(row[3] if len(row) > 3 else 0)
            except:
                pass

        stats["bonus_total"] = round(stats["bonus_total"], 2)

        top = sorted(referrers.items(), key=lambda item: item[1], reverse=True)[:5]
        stats["top_referrers"] = [
            {
                "telegram_id": referrer_id,
                "invited": invited_count,
                "successful": successful_by_referrer.get(referrer_id, 0)
            }
            for referrer_id, invited_count in top
        ]

    except Exception as e:
        print("get_admin_referral_stats error:", e)

    return stats


def admin_referral_stats_text():
    stats = get_admin_referral_stats()

    text = (
        "👥 <b>Реферальна програма</b>\n\n"
        f"Запрошень усього: <b>{stats['invited_total']}</b>\n"
        f"Активних запрошувачів: <b>{stats['referrers_count']}</b>\n"
        f"Очікують першого замовлення: <b>{stats['waiting']}</b>\n"
        f"Успішних рефералів: <b>{stats['successful']}</b>\n"
        f"Скасовано / не зараховано: <b>{stats['cancelled']}</b>\n"
        f"Нараховано реферальних бонусів: <b>{stats['bonus_total']}</b>\n"
    )

    if stats.get("top_referrers"):
        text += "\n🏆 <b>Топ запрошувачів:</b>\n"
        for idx, item in enumerate(stats["top_referrers"], start=1):
            text += (
                f"{idx}. <code>{item['telegram_id']}</code> — "
                f"{item['invited']} запрош., {item['successful']} успішн.\n"
            )

    return text


def show_admin_referral_stats(chat_id, callback_message=None):
    if not is_admin(chat_id):
        return

    text = admin_referral_stats_text()
    keyboard = {"inline_keyboard": [[inline_button("⬅️ Назад у кабінет", "admin_back")]]}

    if callback_message:
        edit_message(chat_id, callback_message["message_id"], text, keyboard)
    else:
        send_message(chat_id, text, keyboard)

def find_referral_for_client(chat_id):
    try:
        ws = get_referrals_worksheet()
        rows = google_call_with_retry(lambda: ws.get_all_values())

        for i, row in enumerate(rows[1:], start=2):
            referral_id = str(row[2] if len(row) > 2 else "").strip()
            bonus_added = str(row[7] if len(row) > 7 else "").strip().lower()
            if referral_id == str(chat_id).strip() and bonus_added not in ["так", "yes", "1", "true"]:
                return ws, i, row
    except Exception as e:
        print("find_referral_for_client error:", e)

    return None, None, None


def has_previous_successful_orders(chat_id, current_order_row_index=None):
    orders = get_orders_with_rows()
    successful_statuses = ["завершено"]

    count = 0
    for order in orders:
        if str(order.get("Telegram ID")).strip() != str(chat_id).strip():
            continue
        if current_order_row_index and str(order.get("row_index")) == str(current_order_row_index):
            continue

        status = str(order.get("Статус", "")).strip().lower()
        if status in successful_statuses:
            count += 1

    return count > 0


def phone_already_used_for_referral(phone, current_referral_row_index=None):
    phone = str(phone or "").strip()
    if not phone:
        return False

    try:
        rows = get_values("Реферали")

        for i, row in enumerate(rows[1:], start=2):
            if current_referral_row_index and i == int(current_referral_row_index):
                continue
            used_phone = str(row[3] if len(row) > 3 else "").strip()
            status = str(row[6] if len(row) > 6 else "").strip().lower()
            if used_phone == phone and status in ["бонус нараховано", "успішно"]:
                return True
    except Exception as e:
        print("phone_already_used_for_referral error:", e)

    return False


def process_referral_bonus_for_order(order):
    """
    Нараховує реферальний бонус тільки після статусу Завершено.
    Умови:
    - це перше замовлення запрошеного клієнта;
    - сума від мінімальної;
    - один телефон бере участь один раз;
    - бонус ще не був нарахований.
    """
    try:
        if not order:
            return False

        referred_id = str(order.get("Telegram ID", "")).strip()
        order_row_index = order.get("row_index", "")
        phone = str(order.get("Телефон", "")).strip()
        total = safe_float(order.get("Сума"))

        if total < REFERRAL_MIN_ORDER_SUM:
            return False

        ws, referral_row_index, referral_row = find_referral_for_client(referred_id)
        if not referral_row:
            return False

        referrer_id = str(referral_row[1] if len(referral_row) > 1 else "").strip()
        if not referrer_id or referrer_id == referred_id:
            return False

        if has_previous_successful_orders(referred_id, current_order_row_index=order_row_index):
            ws.update_cell(referral_row_index, 7, "Не перше замовлення")
            clear_cache("Реферали")
            return False

        if phone_already_used_for_referral(phone, current_referral_row_index=referral_row_index):
            ws.update_cell(referral_row_index, 7, "Телефон вже використаний")
            clear_cache("Реферали")
            return False

        ws.update_cell(referral_row_index, 4, phone)
        ws.update_cell(referral_row_index, 5, order_row_index)
        ws.update_cell(referral_row_index, 6, total)
        ws.update_cell(referral_row_index, 7, "Бонус нараховано")
        ws.update_cell(referral_row_index, 8, "Так")
        clear_cache("Реферали")

        add_bonus_transaction(
            chat_id=referrer_id,
            amount=REFERRAL_BONUS_AMOUNT,
            transaction_type="Реферальний бонус",
            comment=f"За перше замовлення клієнта {referred_id}",
            order_row_index=order_row_index,
            status="Активний",
            expires_at=bonus_expiry_date()
        )

        send_message(
            referrer_id,
            "🎁 <b>Вам нараховано реферальний бонус!</b>\n\n"
            f"На Ваш рахунок додано <b>{REFERRAL_BONUS_AMOUNT} бонусів</b> 💛\n"
            f"Бонуси діють протягом <b>{BONUS_VALID_DAYS} днів</b>."
        )

        return True

    except Exception as e:
        print("process_referral_bonus_for_order error:", e)
        return False



def bonus_already_added_for_order(order_row_index, transaction_type="Бонус за покупку", chat_id=None):
    """
    Перевіряє дубль бонусу саме за конкретне замовлення.

    Важливо:
    - не використовує кеш;
    - не плутає бонус за перший вхід з бонусом за покупку;
    - якщо передано chat_id, перевіряє ще й Telegram ID клієнта.
    """
    try:
        ws = get_bonus_worksheet()
        rows = google_call_with_retry(lambda: ws.get_all_values())

        target_order = str(order_row_index or "").strip()
        target_type = str(transaction_type or "").strip().lower()
        target_chat = str(chat_id or "").strip()

        for row in rows[1:]:
            row_chat = str(row[1] if len(row) > 1 else "").strip()
            row_type = str(row[2] if len(row) > 2 else "").strip().lower()
            row_amount = safe_float(row[3] if len(row) > 3 else 0)
            row_status = str(row[6] if len(row) > 6 else "").strip().lower()
            row_order = str(row[8] if len(row) > 8 else "").strip()

            if row_type != target_type:
                continue
            if row_order != target_order:
                continue
            if row_status != "активний":
                continue
            if row_amount <= 0:
                continue
            if target_chat and row_chat != target_chat:
                continue

            return True

    except Exception as e:
        print("bonus_already_added_for_order error:", e)

    return False


def get_purchase_bonus_amount_for_order(order_row_index):
    """
    Повертає суму бонусу за покупку по конкретному рядку замовлення.
    Потрібно для повідомлення адміну після зміни статусу на "Завершено".
    """
    try:
        rows = get_values("Бонуси")
        for row in reversed(rows[1:]):
            row_type = str(row[2] if len(row) > 2 else "").strip()
            row_order = str(row[8] if len(row) > 8 else "").strip()
            row_status = str(row[6] if len(row) > 6 else "").strip().lower()
            if row_type == "Бонус за покупку" and row_order == str(order_row_index).strip() and row_status == "активний":
                return safe_float(row[3] if len(row) > 3 else 0)
    except Exception as e:
        print("get_purchase_bonus_amount_for_order error:", e)
    return 0


def process_purchase_bonus_for_order(order):
    """
    Нараховує клієнту бонус за покупку після статусу "Завершено".
    Повторно за те саме замовлення бонус не нараховується.

    Важливо: перед нарахуванням перечитуємо конкретний рядок замовлення
    напряму з Google Sheets по row_index, щоб не ловити стару суму з кешу.
    """
    try:
        if not order:
            print("purchase bonus skipped: empty order")
            return False

        order_row_index = str(order.get("row_index", "")).strip()

        # Головна правка: якщо є номер рядка — беремо свіже замовлення напряму з таблиці.
        fresh_order = get_fresh_order_by_row_index(order_row_index) if order_row_index else None
        if fresh_order:
            order = fresh_order

        chat_id = str(order.get("Telegram ID", "")).strip()
        order_row_index = str(order.get("row_index", "")).strip()
        total = safe_float(order.get("Сума"))

        # Додатковий запасний пошук суми по сирому рядку, якщо назва колонки не спрацювала.
        if total <= 0:
            raw_row = order.get("_raw_row") or []
            for idx in [8, 6, 7, 9]:
                if len(raw_row) > idx and safe_float(raw_row[idx]) > 0:
                    total = safe_float(raw_row[idx])
                    order["Сума"] = raw_row[idx]
                    break

        if not chat_id:
            print("purchase bonus skipped: empty Telegram ID", order)
            return False
        if not order_row_index:
            print("purchase bonus skipped: empty order row", order)
            return False
        if total <= 0:
            print("purchase bonus skipped: empty/zero order total", order)
            return False

        clear_cache("Бонуси")

        if bonus_already_added_for_order(order_row_index, "Бонус за покупку", chat_id):
            print(f"purchase bonus skipped: already added for order row {order_row_index}, chat_id={chat_id}")
            return False

        bonus_amount = round(total * PURCHASE_BONUS_PERCENT / 100, 2)
        if bonus_amount <= 0:
            print("purchase bonus skipped: calculated bonus is zero", total, PURCHASE_BONUS_PERCENT)
            return False

        added_ok = add_bonus_transaction(
            chat_id=chat_id,
            amount=bonus_amount,
            transaction_type="Бонус за покупку",
            comment=f"{int(PURCHASE_BONUS_PERCENT)}% від завершеного замовлення на {total} грн",
            order_row_index=order_row_index,
            status="Активний",
            expires_at=bonus_expiry_date()
        )

        if not added_ok:
            print("purchase bonus skipped: bonus row was not written to sheet", chat_id, order_row_index, bonus_amount)
            return False

        clear_cache("Бонуси")

        try:
            new_balance = get_available_bonus_balance(chat_id)
        except Exception:
            new_balance = bonus_amount

        send_message(
            chat_id,
            "🎉 <b>Ваше замовлення успішно завершено!</b>\n\n"
            f"🎁 Вам нараховано <b>{bonus_amount} бонусів</b>.\n"
            f"Зараз доступно: <b>{new_balance} бонусів</b>.\n\n"
            "1 бонус = 1 грн.\n"
            f"Бонусами можна оплатити до <b>{int(BONUS_MAX_USE_PERCENT)}%</b> суми неакційних товарів.\n"
            f"Бонуси діють протягом <b>{BONUS_VALID_DAYS} днів</b> 💛"
        )
        print(f"purchase bonus added: chat_id={chat_id}, order_row={order_row_index}, total={total}, bonus={bonus_amount}")
        return True

    except Exception as e:
        print("process_purchase_bonus_for_order error:", e, "order:", order)
        return False

def cancel_purchase_bonus_for_order(order):
    """
    Якщо замовлення після нарахування бонусів скасовано/повернено — списуємо бонус за покупку назад.
    """
    try:
        if not order:
            return

        chat_id = str(order.get("Telegram ID", "")).strip()
        order_row_index = str(order.get("row_index", "")).strip()
        if not chat_id or not order_row_index:
            return

        rows = get_values("Бонуси")
        for row in rows[1:]:
            row_type = str(row[2] if len(row) > 2 else "").strip()
            row_order = str(row[8] if len(row) > 8 else "").strip()
            row_status = str(row[6] if len(row) > 6 else "").strip().lower()
            try:
                amount = safe_float(row[3] if len(row) > 3 else 0)
            except:
                amount = 0

            if row_type == "Бонус за покупку" and row_order == order_row_index and row_status == "активний" and amount > 0:
                add_bonus_transaction(
                    chat_id=chat_id,
                    amount=-amount,
                    transaction_type="Скасування бонусу за покупку",
                    comment=f"Скасування/повернення замовлення {order_row_index}",
                    order_row_index=order_row_index,
                    status="Списано",
                    expires_at=""
                )
                send_message(
                    chat_id,
                    "ℹ️ Бонуси за це замовлення були скасовані, "
                    "оскільки замовлення скасоване або повернене."
                )
                return

    except Exception as e:
        print("cancel_purchase_bonus_for_order error:", e)

def cancel_referral_bonus_for_order(order):
    """
    Якщо замовлення скасовано/повернено — фіксуємо скасування.
    Якщо бонус уже був нарахований, додаємо зворотне списання.
    """
    try:
        if not order:
            return

        order_row_index = str(order.get("row_index", "")).strip()
        referred_id = str(order.get("Telegram ID", "")).strip()

        ws = get_referrals_worksheet()
        rows = google_call_with_retry(lambda: ws.get_all_values())

        for i, row in enumerate(rows[1:], start=2):
            row_order = str(row[4] if len(row) > 4 else "").strip()
            row_referred = str(row[2] if len(row) > 2 else "").strip()
            bonus_added = str(row[7] if len(row) > 7 else "").strip().lower()
            referrer_id = str(row[1] if len(row) > 1 else "").strip()

            if (order_row_index and row_order == order_row_index) or (row_referred == referred_id and row_order == order_row_index):
                ws.update_cell(i, 7, "Скасовано")
                clear_cache("Реферали")

                if bonus_added in ["так", "yes", "1", "true"] and referrer_id:
                    add_bonus_transaction(
                        chat_id=referrer_id,
                        amount=-REFERRAL_BONUS_AMOUNT,
                        transaction_type="Скасування реферального бонусу",
                        comment=f"Скасування/повернення замовлення {order_row_index}",
                        order_row_index=order_row_index,
                        status="Списано",
                        expires_at=""
                    )
                    send_message(
                        referrer_id,
                        "ℹ️ Реферальний бонус за замовлення було скасовано, "
                        "оскільки замовлення скасоване або повернене."
                    )
                return

    except Exception as e:
        print("cancel_referral_bonus_for_order error:", e)


def bonus_expiry_reminder_text(balance):
    return (
        "🎁 <b>Нагадуємо про Ваші бонуси</b>\n\n"
        f"На Вашому бонусному рахунку доступно: <b>{balance} бонусів</b>.\n\n"
        "Ви можете використати їх для наступного замовлення у нашій крамничці 💛"
    )


def process_bonus_reminders():
    """
    М'яке нагадування про бонуси.
    Фіксується в листі історії, щоб один клієнт не отримав однакове нагадування кілька разів.
    """
    try:
        rows = get_values("Бонуси")
        notified = set()
        sent = 0
        log_rows = []
        sent_keys, today_counts = get_broadcast_recipient_log_snapshot()
        sent_at = now_str()
        today_key = current_time().strftime("%Y%m%d")

        for row in rows[1:]:
            if len(row) < 7:
                continue

            telegram_id = str(row[1]).strip()
            status = str(row[6]).strip().lower()
            if not telegram_id or status != "активний" or telegram_id in notified:
                continue

            balance = get_available_bonus_balance(telegram_id)
            if balance <= 0:
                continue

            campaign_key = f"bonus_reminder|{today_key}"
            unique_key = f"{telegram_id}|{campaign_key}"
            if unique_key in sent_keys:
                continue

            ok = send_message(telegram_id, bonus_expiry_reminder_text(balance))
            if ok:
                sent_keys.add(unique_key)
                log_rows.append([sent_at, telegram_id, "Нагадування про бонуси", campaign_key, "Надіслано"])
                notified.add(telegram_id)
                sent += 1
                if len(log_rows) >= BROADCAST_LOG_FLUSH_EVERY:
                    append_broadcast_recipient_logs(log_rows)
                    log_rows = []

        append_broadcast_recipient_logs(log_rows)
        return sent

    except Exception as e:
        print("process_bonus_reminders error:", e)
        return 0


# =========================
# CLIENTS / DISCOUNTS
# =========================

FREE_DELIVERY_THRESHOLD = 1000
NEXT_ORDER_DISCOUNT_PERCENT = 0  # Вимкнено: замість -10% працює бонусна система
PURCHASE_BONUS_PERCENT = float(os.environ.get("PURCHASE_BONUS_PERCENT", "5"))


def get_clients_worksheet():
    headers = [
        "Telegram ID",
        "ПІБ",
        "Телефон",
        "Знижка %",
        "Знижка активна",
        "Дата останнього замовлення"
    ]
    return get_or_create_worksheet("Клієнти", headers)


def get_client_row(chat_id):
    try:
        ws = get_clients_worksheet()
        rows = google_call_with_retry(lambda: ws.get_all_values())

        for i, row in enumerate(rows[1:], start=2):
            if len(row) > 0 and str(row[0]).strip() == str(chat_id).strip():
                return ws, i, row

        return ws, None, None
    except Exception as e:
        print("get_client_row error:", e)
        return None, None, None


def get_client_discount_percent(chat_id):
    ws, row_index, row = get_client_row(chat_id)

    if not row:
        return 0

    active = str(row[4] if len(row) > 4 else "").strip().lower()
    if active not in ["так", "yes", "true", "1", "активна"]:
        return 0

    try:
        return safe_float(row[3] if len(row) > 3 else 0)
    except:
        return 0


def upsert_client_discount(chat_id, full_name="", phone="", discount_percent=NEXT_ORDER_DISCOUNT_PERCENT, active="Так"):
    try:
        ws, row_index, row = get_client_row(chat_id)
        date_now = current_time().strftime("%d.%m.%Y %H:%M")

        if row_index:
            ws.update_cell(row_index, 2, full_name or (row[1] if len(row) > 1 else ""))
            ws.update_cell(row_index, 3, phone or (row[2] if len(row) > 2 else ""))
            ws.update_cell(row_index, 4, discount_percent)
            ws.update_cell(row_index, 5, active)
            ws.update_cell(row_index, 6, date_now)
        else:
            ws.append_row([
                chat_id,
                full_name,
                phone,
                discount_percent,
                active,
                date_now
            ], value_input_option="USER_ENTERED")
    except Exception as e:
        print("upsert_client_discount error:", e)


def calculate_cart_totals(chat_id, use_bonuses=None):
    cart = get_user_cart(chat_id)
    subtotal = 0
    bonus_eligible_subtotal = 0

    for item in cart:
        try:
            item_sum = safe_float(item.get("Сума") or 0)
            subtotal += item_sum

            if is_bonus_eligible_cart_item(item):
                bonus_eligible_subtotal += item_sum
        except Exception as e:
            print("calculate_cart_totals item error:", e)

    subtotal = round(subtotal, 2)
    bonus_eligible_subtotal = round(bonus_eligible_subtotal, 2)

    discount_percent = get_client_discount_percent(chat_id)
    discount_amount = round(subtotal * discount_percent / 100, 2) if discount_percent else 0
    after_discount = round(subtotal - discount_amount, 2)

    bonus_eligible_discount_amount = round(bonus_eligible_subtotal * discount_percent / 100, 2) if discount_percent else 0
    bonus_eligible_after_discount = round(bonus_eligible_subtotal - bonus_eligible_discount_amount, 2)

    if use_bonuses is None:
        state = USER_STATES.get(str(chat_id), {})
        use_bonuses = bool(state.get("use_bonuses"))

    available_bonuses = get_available_bonus_balance(chat_id)
    max_bonus_to_use = calculate_bonus_to_use(chat_id, bonus_eligible_after_discount)
    bonus_used = max_bonus_to_use if use_bonuses else 0

    total = round(after_discount - bonus_used, 2)

    return {
        "subtotal": subtotal,
        "discount_percent": discount_percent,
        "discount_amount": discount_amount,
        "after_discount": after_discount,
        "bonus_eligible_subtotal": bonus_eligible_subtotal,
        "bonus_eligible_discount_amount": bonus_eligible_discount_amount,
        "bonus_eligible_after_discount": bonus_eligible_after_discount,
        "available_bonuses": available_bonuses,
        "max_bonus_to_use": max_bonus_to_use,
        "bonus_used": bonus_used,
        "total": total
    }
def delivery_note_for_client(delivery_method, total):
    if total >= FREE_DELIVERY_THRESHOLD:
        return "🚚 Доставка для Вас безкоштовна, тому що сума замовлення від 1000 грн."

    if delivery_method == "Нова пошта":
        return "🚚 Доставка оплачується за тарифами Нової пошти."

    if delivery_method == "Укрпошта":
        return "📦 Доставка оплачується за тарифами Укрпошти."

    return "🚚 Доставка оплачується згідно з тарифами служби доставки."


def ask_free_delivery_offer(chat_id):
    totals = calculate_cart_totals(chat_id)
    total = totals["total"]

    if total >= FREE_DELIVERY_THRESHOLD:
        ask_need_contact(chat_id)
        return

    left = round(FREE_DELIVERY_THRESHOLD - total, 2)
    text = (
        "🚚 <b>Безкоштовна доставка діє від 1000 грн.</b>\n\n"
        f"Зараз сума Вашого замовлення: <b>{total} грн</b>.\n"
        f"До безкоштовної доставки залишилось: <b>{left} грн</b>.\n\n"
        "Бажаєте ще додати товари до замовлення?"
    )

    keyboard = {
        "inline_keyboard": [
            [inline_button("🛍 Так, додати товари", "add_more_before_order")],
            [inline_button("✅ Ні, продовжити оформлення", "confirm_order_now")]
        ]
    }

    send_flow_message(chat_id, text, keyboard)

def continue_order_after_adding(chat_id):
    state = USER_STATES.get(str(chat_id), {})

    if state.get("step") != "adding_more_before_order":
        start_order(chat_id)
        return

    ask_free_delivery_offer(chat_id)



# =========================
# MARKETING / BROADCASTS
# =========================

MARKETING_BROADCAST_LIMIT_PER_RUN = int(os.environ.get("MARKETING_BROADCAST_LIMIT_PER_RUN", "1"))
INACTIVE_CLIENT_DAYS = int(os.environ.get("INACTIVE_CLIENT_DAYS", "30"))
SALE_BROADCAST_LIMIT_PER_RUN = int(os.environ.get("SALE_BROADCAST_LIMIT_PER_RUN", "1"))


# Загальне "тихе вікно" для автоматичних розсилок.
# Щоб товар дня, акції та маркетингові повідомлення не йшли клієнтам вночі.
BROADCAST_MIN_HOUR = int(os.environ.get("BROADCAST_MIN_HOUR", "10"))
BROADCAST_MAX_HOUR = int(os.environ.get("BROADCAST_MAX_HOUR", "21"))

AUTO_PRODUCT_BROADCAST_LOCK = {
    "date": "",
    "running": False
}


def broadcasts_allowed_now(name="broadcast"):
    now = current_time()
    if now.hour < BROADCAST_MIN_HOUR or now.hour >= BROADCAST_MAX_HOUR:
        print(f"{name} skipped by Kyiv send window: {now_str()}")
        return False
    return True


# =========================
# DAILY SOFT REMINDERS
# =========================
# Легкі повідомлення "настрій дня", щоб нагадувати про магазин без спаму.
# За замовчуванням: Пн/Ср/Пт/Нд, з 10:00 до 20:59, не більше 1 разу на день.
DAILY_REMINDER_DAYS_OF_WEEK = os.environ.get("DAILY_REMINDER_DAYS_OF_WEEK", "1,3,5,7")
DAILY_REMINDER_MIN_HOUR = int(os.environ.get("DAILY_REMINDER_MIN_HOUR", "10"))
DAILY_REMINDER_MAX_HOUR = int(os.environ.get("DAILY_REMINDER_MAX_HOUR", "21"))
DAILY_REMINDER_LIMIT_PER_RUN = int(os.environ.get("DAILY_REMINDER_LIMIT_PER_RUN", "1"))

DEFAULT_DAILY_MESSAGES = [
    ("1", "Комплімент", "🌸 Нагадуємо: Ви заслуговуєте на маленькі радощі не лише у свята 💛\n\nЗагляньте до каталогу — можливо, там уже чекає щось приємне саме для Вас 🛍"),
    ("2", "Комплімент", "✨ Сьогодні чудовий день, щоб потішити себе чимось приємним.\n\nНавіть маленька покупка може зробити настрій значно кращим 💛"),
    ("3", "Комплімент", "💕 Турбота про себе — це не розкіш, а необхідність.\n\nОберіть щось для себе у нашій крамничці 🌸"),
    ("4", "Комплімент", "🌷 Іноді найкращий подарунок — той, який ми даруємо собі самі.\n\nКаталог уже відкритий для Вашого настрою 🛍"),
    ("5", "Комплімент", "💛 Не забувайте знаходити час для себе навіть у найзавантаженіші дні.\n\nМаленька приємність може змінити весь день ✨"),
    ("6", "Комплімент", "🌺 Кожна жінка заслуговує відчувати себе красивою щодня.\n\nМожливо, саме сьогодні варто обрати щось для себе?"),
    ("7", "Комплімент", "✨ Гарний настрій починається з турботи про себе.\n\nА ми підготували для Вас багато цікавих знахідок 💛"),
    ("8", "Комплімент", "🌸 Сьогодні саме той день, коли варто себе потішити.\n\nПерегляньте каталог — там може бути Ваша нова улюблена річ 🛍"),
    ("9", "Комплімент", "💕 Маленькі радощі створюють велике щастя.\n\nДозвольте собі щось приємне сьогодні 💛"),
    ("10", "Комплімент", "🌷 Ви чудові. Просто нагадуємо 💛\n\nА ще нагадуємо, що у каталозі є багато приємних товарів для Вас."),
    ("11", "Гороскоп", "🔮 Зірки радять сьогодні не відкладати приємні покупки на потім.\n\nМожливо, саме зараз у каталозі чекає Ваша маленька радість ✨"),
    ("12", "Гороскоп", "✨ Всесвіт натякає, що настав час оновлень.\n\nПочати можна з чогось маленького, корисного і приємного 💛"),
    ("13", "Гороскоп", "🌟 Сприятливий день для приємних сюрпризів.\n\nЗагляньте до крамнички — раптом сьогодні саме Ваш день для вдалої покупки 🛍"),
    ("14", "Гороскоп", "🔮 Сьогодні удача на боці тих, хто любить себе балувати.\n\nДозвольте собі маленьку приємність 💛"),
    ("15", "Гороскоп", "✨ Зірки бачать нові покупки у Вашому майбутньому 😄\n\nПеревіримо, що цікавого є в каталозі?"),
    ("16", "Гороскоп", "🌙 Гарний день для невеликих подарунків собі.\n\nНавіть дрібничка може подарувати багато радості."),
    ("17", "Гороскоп", "🌟 Всесвіт шепоче: час потішити себе.\n\nА ми якраз підготували для Вас багато цікавого ✨"),
    ("18", "Гороскоп", "🔮 Можливо, саме сьогодні Ви знайдете свою нову улюблену річ.\n\nКаталог уже чекає 🛍"),
    ("19", "Гороскоп", "✨ День обіцяє багато приємних моментів.\n\nОдин із них може початися з кнопки «Переглянути каталог» 💛"),
    ("20", "Гороскоп", "🌸 Зірки радять приділити трохи часу собі.\n\nПочніть із маленького вибору для гарного настрою."),
    ("21", "Бонуси", "🎁 Ваші бонуси не люблять сумувати без діла.\n\nЗагляньте в каталог — можливо, вони вже знайшли для себе нову покупку 😉"),
    ("22", "Бонуси", "💛 Бонуси створені для того, щоб приносити вигоду.\n\nПеревірте, що можна обрати зі знижкою вже зараз."),
    ("23", "Бонуси", "🎁 Перевірте свій бонусний рахунок — можливо, там уже чекає приємна знижка.\n\n1 бонус = 1 грн 💛"),
    ("24", "Бонуси", "💰 Бонуси — це приємніше, ніж здача в магазині 😄\n\nВикористайте їх для наступної покупки."),
    ("25", "Бонуси", "🎁 Нехай Ваші бонуси працюють на Вас.\n\nБонусами можна оплатити до 30% суми замовлення 💛"),
    ("26", "Бонуси", "💛 Використайте бонуси для наступної покупки.\n\nМожливо, саме час обрати щось приємне?"),
    ("27", "Бонуси", "🎁 Ваш бонусний баланс може зробити покупку ще приємнішою.\n\nЗагляньте до каталогу та перевірте, що Вам сподобається."),
    ("28", "Бонуси", "💰 Накопичувати бонуси добре, використовувати — ще краще.\n\nПотіште себе вигідною покупкою 💛"),
    ("29", "Бонуси", "🎁 Можливо, настав час обміняти бонуси на нову покупку?\n\nКаталог уже чекає на Вас 🛍"),
    ("30", "Бонуси", "💛 Бонуси вже чекають свого часу.\n\nНе забувайте: ними можна оплатити частину замовлення."),
    ("31", "Гумор", "😄 Кажуть, що нова посилка лікує поганий настрій.\n\nМи не лікарі, але звучить дуже правдоподібно 📦"),
    ("32", "Гумор", "📦 Найприємніше повідомлення дня: «Ваше замовлення відправлено».\n\nМожемо наблизити цей момент? 😄"),
    ("33", "Гумор", "😄 У кожної жінки є два настрої: «нічого не хочу» і «додайте в кошик».\n\nЯкий сьогодні у Вас? 🛍"),
    ("34", "Гумор", "🛍 Кошик сам себе не наповнить 😄\n\nАле ми можемо допомогти з вибором."),
    ("35", "Гумор", "📦 Очікування посилки — окремий вид задоволення.\n\nПочати можна з маленького замовлення 💛"),
    ("36", "Гумор", "😄 Якщо день не задався — можливо, бракує нової покупки.\n\nПеревіримо каталог?"),
    ("37", "Гумор", "💛 Гарний настрій іноді приїжджає Новою Поштою.\n\nІ ми знаємо, як його замовити 😉"),
    ("38", "Гумор", "😄 Випадкових покупок не буває — це доля.\n\nМожливо, сьогодні вона приведе Вас у каталог."),
    ("39", "Гумор", "📦 Маленька коробочка може подарувати багато радості.\n\nГоловне — правильно її наповнити 🛍"),
    ("40", "Гумор", "😄 Сьогодні чудовий день для кнопки «Замовити».\n\nПросто залишимо це тут 💛"),
    ("41", "Краса", "💄 Краса починається з догляду.\n\nЗнайдіть кілька хвилин для себе сьогодні 🌸"),
    ("42", "Краса", "🌿 Доглянута шкіра завжди в моді.\n\nА корисні знахідки для догляду вже чекають у каталозі."),
    ("43", "Краса", "✨ 10 хвилин для себе можуть змінити весь день.\n\nПочніть із маленького ритуалу догляду 💛"),
    ("44", "Краса", "🌸 Не забувайте про регулярний догляд за шкірою.\n\nШкіра любить увагу щодня."),
    ("45", "Краса", "💛 Турбота про себе завжди окупається гарним настроєм.\n\nОберіть щось приємне для себе."),
    ("46", "Краса", "🌿 Зволоження — найкращий друг шкіри.\n\nМожливо, у каталозі є саме те, що Вам потрібно."),
    ("47", "Краса", "✨ Ваша шкіра заслуговує на увагу щодня.\n\nПодаруйте їй трохи турботи 💛"),
    ("48", "Краса", "💄 Догляд сьогодні — краса завтра.\n\nА маленькі бʼюті-знахідки завжди під рукою у нашій крамничці."),
    ("49", "Краса", "🌸 Краса починається з маленьких звичок.\n\nОдна з них — час від часу тішити себе."),
    ("50", "Краса", "💛 Знайдіть кілька хвилин для себе просто сьогодні.\n\nКаталог відкритий для натхнення 🛍"),
    ("51", "Продаж", "🛍 Можливо, саме сьогодні в каталозі на Вас чекає щось особливе.\n\nПерегляньте новинки та акції 💛"),
    ("52", "Продаж", "✨ Ми підготували багато цікавих новинок.\n\nЗагляньте до каталогу, щоб нічого не пропустити."),
    ("53", "Продаж", "🎁 Загляньте до каталогу — там завжди є щось цікаве.\n\nА з бонусами покупка може бути ще вигіднішою."),
    ("54", "Продаж", "🛍 Іноді одна покупка може зробити день кращим.\n\nОсобливо якщо вона давно чекала у каталозі."),
    ("55", "Продаж", "💛 Дозвольте собі маленьку приємність.\n\nВи точно цього заслуговуєте."),
    ("56", "Продаж", "🌸 Нові знахідки вже чекають у каталозі.\n\nМожливо, серед них є саме Ваша."),
    ("57", "Продаж", "✨ Ваш майбутній улюблений товар може бути вже там.\n\nЗалишилось тільки переглянути каталог 🛍"),
    ("58", "Продаж", "🎁 Можливо, саме сьогодні Ви знайдете щось корисне для себе.\n\nМи вже підготували пропозиції."),
    ("59", "Продаж", "🛍 Каталог відкритий для гарного настрою.\n\nЗаходьте, переглядайте, обирайте 💛"),
    ("60", "Продаж", "💛 Ми завжди раді допомогти знайти щось особливе.\n\nПочніть із перегляду каталогу."),
    ("61", "Ранок", "☀️ Доброго ранку! Нехай сьогоднішній день принесе Вам багато приводів для усмішок 💛\n\nА один із них може бути у нашому каталозі."),
    ("62", "Ранок", "🌸 Новий день — нові можливості потішити себе.\n\nЗагляньте до крамнички за приємним настроєм."),
    ("63", "Ранок", "☕ Бажаємо ароматної кави, гарного настрою та приємних покупок.\n\nКаталог уже чекає на Вас 🛍"),
    ("64", "Ранок", "✨ Нехай сьогодні Вас оточують лише приємні дрібниці.\n\nІ нехай одна з них буде для себе 💛"),
    ("65", "Ранок", "💛 Памʼятайте: Ви заслуговуєте на щось хороше вже сьогодні.\n\nМаленька покупка теж рахується."),
    ("66", "Вечір", "🌙 Вечір — чудовий час приділити кілька хвилин собі.\n\nПерегляньте каталог у спокійному настрої 💛"),
    ("67", "Вечір", "✨ День добігає кінця, а гарний настрій можна подарувати собі просто зараз.\n\nМожливо, через маленьке замовлення."),
    ("68", "Вечір", "💛 Бажаємо Вам затишного вечора та приємного відпочинку.\n\nА якщо хочеться маленької радості — каталог поруч."),
    ("69", "Вечір", "🌸 Іноді найкраще завершення дня — маленька приємна покупка.\n\nДозвольте собі трохи радості."),
    ("70", "Вечір", "🛍 Можливо, саме сьогодні ввечері Ви знайдете щось особливе для себе.\n\nПеревіримо?"),
    ("71", "Настрій", "💕 Ви сильніші, красивіші та кращі, ніж Вам здається.\n\nІ точно заслуговуєте на турботу про себе."),
    ("72", "Настрій", "🌷 Ніколи не забувайте про себе серед щоденних справ.\n\nМаленька приємність для себе — це теж важливо."),
    ("73", "Настрій", "✨ Щастя часто складається з маленьких радощів.\n\nОдна з них може чекати у нашій крамничці."),
    ("74", "Настрій", "💛 Сьогодні чудовий день для посмішки.\n\nА ще для маленької покупки без приводу."),
    ("75", "Настрій", "🌸 Маленькі подарунки собі теж мають значення.\n\nВони нагадують: Ви важливі."),
    ("76", "Посилки", "📦 Знаєте, що обʼєднує більшість людей? Любов до повідомлення «Посилка вже в дорозі» 😄"),
    ("77", "Посилки", "💛 Очікування посилки — особливий вид радості.\n\nМожливо, час створити собі таке очікування?"),
    ("78", "Посилки", "📦 Нова посилка — це завжди маленьке свято.\n\nНавіть якщо всередині щось дуже практичне."),
    ("79", "Посилки", "✨ Іноді достатньо однієї коробочки, щоб підняти настрій.\n\nПеревіримо, що можна додати до неї?"),
    ("80", "Посилки", "📦 Приємні покупки роблять дні яскравішими.\n\nА ми готові допомогти з вибором.")
]


def get_daily_messages_worksheet():
    headers = [
        "ID",
        "Категорія",
        "Текст",
        "Активне",
        "Останнє надсилання"
    ]
    ws = get_or_create_worksheet("Повідомлення дня", headers)
    ensure_headers(ws, headers)

    try:
        values = google_call_with_retry(lambda: ws.get_all_values())
        if len(values) <= 1:
            rows = [[msg_id, category, text, "Так", ""] for msg_id, category, text in DEFAULT_DAILY_MESSAGES]
            google_call_with_retry(lambda: ws.append_rows(rows, value_input_option="USER_ENTERED"))
            clear_cache("Повідомлення дня")
    except Exception as e:
        print("get_daily_messages_worksheet seed error:", e)

    return ws


def get_daily_log_worksheet():
    headers = [
        "Дата",
        "ID повідомлення",
        "Категорія",
        "Кому надіслано",
        "Статус"
    ]
    ws = get_or_create_worksheet("Надіслані повідомлення дня", headers)
    ensure_headers(ws, headers)
    return ws


def daily_reminders_allowed_today():
    try:
        allowed_days = [
            int(x.strip())
            for x in str(DAILY_REMINDER_DAYS_OF_WEEK).split(",")
            if str(x).strip().isdigit()
        ]
    except Exception:
        allowed_days = [1, 3, 5, 7]

    now = current_time()

    # isoweekday: понеділок=1 ... неділя=7
    today_day = now.isoweekday()

    if allowed_days and today_day not in allowed_days:
        print(f"daily reminders skipped by weekday: {now_str()}")
        return False

    if now.hour < DAILY_REMINDER_MIN_HOUR or now.hour >= DAILY_REMINDER_MAX_HOUR:
        print(f"daily reminders skipped by Kyiv send window: {now_str()}")
        return False

    return True


def daily_reminder_sent_today():
    try:
        rows = get_values("Надіслані повідомлення дня")[1:]
        today_prefix = current_time().strftime("%d.%m.%Y")
        for row in rows:
            sent_date = str(row[0] if len(row) > 0 else "").strip()
            status = str(row[4] if len(row) > 4 else "").strip().lower()
            if sent_date.startswith(today_prefix) and status in ["надіслано", "sent", "так"]:
                return True
    except Exception as e:
        print("daily_reminder_sent_today error:", e)

    return False


def get_next_daily_message():
    ws = get_daily_messages_worksheet()
    rows = google_call_with_retry(lambda: ws.get_all_values())
    if len(rows) <= 1:
        return None, None, None

    headers = rows[0]
    candidates = []

    for row_index, row in enumerate(rows[1:], start=2):
        active = str(get_cell_by_header(row, headers, "Активне", "")).strip().lower()
        msg_id = str(get_cell_by_header(row, headers, "ID", "")).strip()
        category = str(get_cell_by_header(row, headers, "Категорія", "")).strip()
        body = str(get_cell_by_header(row, headers, "Текст", "")).strip()
        last_sent = str(get_cell_by_header(row, headers, "Останнє надсилання", "")).strip()

        if active not in ["так", "yes", "1", "true", "активне", "активна"]:
            continue
        if not msg_id or not body:
            continue

        candidates.append({
            "row_index": row_index,
            "id": msg_id,
            "category": category,
            "text": body,
            "last_sent": last_sent
        })

    if not candidates:
        return None, None, None

    # Беремо те, що давно не надсилалось. Порожні — першими.
    candidates.sort(key=lambda x: x.get("last_sent") or "")
    chosen = candidates[0]
    return ws, headers, chosen


def daily_reminder_keyboard():
    return {
        "inline_keyboard": [
            [inline_button("📦 Переглянути каталог", "open_catalog")],
            [inline_button("🔥 Переглянути акції", "open_sales")],
            [inline_button("🎁 Мої бонуси", "open_bonus_cabinet")]
        ]
    }


def process_daily_soft_reminders():
    """
    Легка автоматична розсилка-нагадування.
    Не частіше 1 разу на день, за замовчуванням тільки Пн/Ср/Пт після 10:00.
    """
    if not daily_reminders_allowed_today():
        return 0

    if not acquire_broadcast_lock("daily-reminders"):
        return 0

    try:
        if daily_reminder_sent_today():
            return 0

        ws, headers, message = get_next_daily_message()
        if not message:
            return 0

        campaign_key = f"daily|{current_time().strftime('%d.%m.%Y')}|{message['id']}"
        sent, failed, completed = send_marketing_to_all(
            message["text"],
            daily_reminder_keyboard(),
            None,
            campaign_key=campaign_key,
            campaign_type="Повідомлення дня"
        )

        if completed:
            update_cell_by_header(ws, message["row_index"], headers, "Останнє надсилання", now_str())

            log_ws = get_daily_log_worksheet()
            google_call_with_retry(lambda: log_ws.append_row([
                now_str(),
                message["id"],
                message["category"],
                sent,
                "Надіслано"
            ], value_input_option="USER_ENTERED"))
            clear_cache("Надіслані повідомлення дня")
            clear_cache("Повідомлення дня")

        print(f"daily soft reminder sent id={message['id']}, sent={sent}, failed={failed}, completed={completed}")
        return 1 if sent > 0 or not completed else 0

    finally:
        release_broadcast_lock("daily-reminders")


def ensure_headers(ws, headers):
    """
    Акуратно додає відсутні заголовки у перший рядок,
    не ламаючи вже існуючі колонки.
    """
    try:
        values = google_call_with_retry(lambda: ws.get_all_values())
        if not values:
            ws.append_row(headers, value_input_option="USER_ENTERED")
            return

        current = values[0]
        changed = False

        for header in headers:
            if header not in current:
                current.append(header)
                changed = True

        if changed:
            ws.update("1:1", [current], value_input_option="USER_ENTERED")
    except Exception as e:
        print("ensure_headers error:", e)


def get_cell_by_header(row, headers, header_name, default=""):
    try:
        idx = headers.index(header_name)
        return row[idx] if len(row) > idx else default
    except ValueError:
        return default


def update_cell_by_header(ws, row_index, headers, header_name, value):
    try:
        col = headers.index(header_name) + 1
        ws.update_cell(row_index, col, value)
    except Exception as e:
        print("update_cell_by_header error:", e)


def parse_sheet_date(value):
    """
    Акуратно читає дати з Google Sheets.
    Підтримує:
    - 18.06.2026
    - 18.06.2026 13:00
    - 18.06 або 18.06. — автоматично підставляє поточний рік
    - 2026-06-18
    - 18/06/2026
    - серійні дати Google Sheets/Excel
    """
    try:
        if value is None:
            return None

        # Якщо gspread/Sheets раптом віддав число-серіал дати.
        if isinstance(value, (int, float)) and float(value) > 0:
            base = datetime(1899, 12, 30)
            return (base + timedelta(days=float(value))).date()

        value = str(value or "").strip()
        if not value:
            return None

        value = value.replace("/", ".").replace("-", ".")
        value = " ".join(value.split())

        # 18.06 або 18.06. → поточний рік
        short = value.split(" ")[0].strip()
        if re.fullmatch(r"\d{1,2}\.\d{1,2}\.?$", short):
            short = short.rstrip(".")
            value = f"{short}.{current_time().year}"

        for fmt in [
            "%d.%m.%Y",
            "%d.%m.%Y %H:%M",
            "%d.%m.%Y %H:%M:%S",
            "%Y.%m.%d",
            "%Y.%m.%d %H:%M",
            "%Y.%m.%d %H:%M:%S",
        ]:
            try:
                return datetime.strptime(value, fmt).date()
            except:
                pass

    except Exception as e:
        print("parse_sheet_date error:", e, "value:", value)

    return None


def get_product_sale_start(product):
    return parse_sheet_date(
        product.get("Акція від")
        or product.get("Акція з")
        or product.get("Дата початку акції")
        or ""
    )


def get_product_sale_end(product):
    return parse_sheet_date(
        product.get("Акція до")
        or product.get("Дата завершення акції")
        or product.get("Дата кінця акції")
        or ""
    )


def product_has_sale_period(product):
    return bool(get_product_sale_start(product) or get_product_sale_end(product))


def is_product_sale_active(product, today=None):
    """
    Акція активна тільки у вказаний період.
    Якщо дати порожні — акція працює як постійна.
    """
    if not product:
        return False

    sale_text = normalize_sale_text(
        product.get("Акція")
        or product.get("Акція 1=2")
        or product.get("Тип акції")
        or ""
    )
    sale_price = str(product.get("Акційна ціна", "") or "").strip()
    old_price = str(product.get("Стара ціна", "") or "").strip()

    if not sale_text and not sale_price and not old_price:
        return False

    today = today or current_time().date()
    start = get_product_sale_start(product)
    end = get_product_sale_end(product)

    if start and today < start:
        return False
    if end and today > end:
        return False

    return True


def get_active_sale_price(product):
    if is_product_sale_active(product):
        return str(product.get("Акційна ціна", "") or "").strip()
    return ""


def sale_days_left(product):
    end = get_product_sale_end(product)
    if not end:
        return None
    return (end - current_time().date()).days


def sale_period_text(product):
    if not is_product_sale_active(product):
        return ""

    start = get_product_sale_start(product)
    end = get_product_sale_end(product)
    parts = []

    if start:
        parts.append(f"з {start.strftime('%d.%m.%Y')}")
    if end:
        parts.append(f"до {end.strftime('%d.%m.%Y')}")

    if not parts:
        return ""

    days_left = sale_days_left(product)
    prefix = "⏳ Термін дії акції: " + " ".join(parts)

    if days_left == 0:
        prefix += "\n🚨 <b>Сьогодні останній день акції!</b>"
    elif days_left == 1:
        prefix += "\n⏰ До завершення акції залишився <b>1 день</b>"
    elif days_left is not None and 1 < days_left <= 3:
        prefix += f"\n⏰ До завершення акції залишилось <b>{days_left} дні</b>"

    return prefix


def sale_broadcast_key(product):
    product_id = str(product.get("ID товару", "") or "").strip()
    sale = get_product_sale_text(product)

    start_dt = get_product_sale_start(product)
    end_dt = get_product_sale_end(product)

    start = start_dt.strftime("%d.%m.%Y") if start_dt else str(product.get("Акція від", "") or product.get("Акція з", "") or "").strip()
    end = end_dt.strftime("%d.%m.%Y") if end_dt else str(product.get("Акція до", "") or product.get("Дата завершення акції", "") or "").strip()

    # Якщо текст акції порожній, але є акційна ціна/стара ціна — ключ все одно має бути унікальним.
    if not sale:
        sale = str(product.get("Акційна ціна", "") or product.get("Стара ціна", "") or "Акція").strip()

    return f"{product_id}|{sale}|{start}|{end}"




def acquire_broadcast_lock(name):
    """
    Простий lock у памʼяті процесу, щоб один і той самий endpoint не запускався паралельно.
    Додатково нижче є sheet-lock, який переживає рестарт Render/декілька worker-ів.
    """
    try:
        key = str(name)
        now_ts = time.time()
        lock = BROADCAST_RUN_LOCKS.get(key)

        if lock and lock.get("running") and now_ts - float(lock.get("started_at", 0)) < BROADCAST_LOCK_TTL_SECONDS:
            print(f"{key} skipped: already running in memory")
            return False

        BROADCAST_RUN_LOCKS[key] = {
            "running": True,
            "started_at": now_ts
        }
        return True
    except Exception as e:
        print("acquire_broadcast_lock error:", e)
        return True


def release_broadcast_lock(name):
    try:
        key = str(name)
        if key in BROADCAST_RUN_LOCKS:
            BROADCAST_RUN_LOCKS[key]["running"] = False
    except Exception as e:
        print("release_broadcast_lock error:", e)


def get_broadcast_runs_worksheet():
    headers = [
        "Дата",
        "Назва",
        "Ключ запуску",
        "Статус",
        "Надіслано",
        "Помилок",
        "Пропущено"
    ]
    # Не робимо get_all_values/ensure_headers на кожному зверненні — це давало 429.
    if BROADCAST_RUNS_SHEET_NAME in SERVICE_WORKSHEETS_READY:
        return get_cached_worksheet(BROADCAST_RUNS_SHEET_NAME)

    ws = get_or_create_worksheet(BROADCAST_RUNS_SHEET_NAME, headers)
    ensure_headers(ws, headers)
    SERVICE_WORKSHEETS_READY.add(BROADCAST_RUNS_SHEET_NAME)
    return ws


def acquire_persistent_broadcast_lock(name, campaign_key):
    """
    Sheet-lock проти дублювання, коли UptimeRobot/Render запускає endpoint кілька разів.

    Додатковий захист:
    - кожен запуск отримує унікальний lock_token;
    - після запису "Розпочато|token" код перечитує лист і перевіряє,
      що саме його запис є останнім для цього campaign_key;
    - якщо паралельно стартували 2 воркери, працювати продовжить тільки останній,
      інший зупиниться ДО відправки клієнтам.
    """
    try:
        name = str(name or "Розсилка").strip()
        campaign_key = str(campaign_key or "").strip()
        if not campaign_key:
            return True

        ws = get_broadcast_runs_worksheet()
        rows = google_call_with_retry(lambda: ws.get_all_values())
        headers = rows[0] if rows else []
        now_dt = current_time()

        latest_row = None
        latest_row_number = None

        for row_number, row in enumerate(rows[1:], start=2):
            row_key = str(get_cell_by_header(row, headers, "Ключ запуску", "")).strip()
            if row_key == campaign_key:
                latest_row = row
                latest_row_number = row_number

        if latest_row:
            latest_status = str(get_cell_by_header(latest_row, headers, "Статус", "")).strip().lower()
            latest_date_raw = str(get_cell_by_header(latest_row, headers, "Дата", "")).strip()
            latest_dt = parse_bot_datetime(latest_date_raw)

            if latest_status in ["завершено", "надіслано", "так", "sent"]:
                print(f"broadcast skipped by sheet lock: already finished {campaign_key}")
                return False

            if latest_status.startswith("розпочато") or latest_status.startswith("в процесі") or latest_status.startswith("running"):
                # На Render Free worker може обірватися і лишити статус "Розпочато".
                # Не тримаємо такий lock 15 хвилин: для наших маленьких пачок достатньо 2 хв.
                effective_lock_ttl = min(int(BROADCAST_LOCK_TTL_SECONDS or 900), 120)
                if latest_dt and (now_dt - latest_dt).total_seconds() < effective_lock_ttl:
                    print(f"broadcast skipped by sheet lock: already running {campaign_key}")
                    return False

                print(f"broadcast stale running lock ignored: {campaign_key}, row={latest_row_number}")

            if latest_status in ["пауза", "помилка", "обірвано", "перервано", "paused", "error"]:
                print(f"broadcast continuation allowed after status={latest_status}: {campaign_key}")

        lock_token = uuid.uuid4().hex[:12]
        lock_status = f"Розпочато|{lock_token}"

        google_call_with_retry(lambda: ws.append_row([
            now_str(),
            name,
            campaign_key,
            lock_status,
            "",
            "",
            ""
        ], value_input_option="USER_ENTERED"))
        clear_cache(BROADCAST_RUNS_SHEET_NAME)

        # Перевірка власності lock. Це прибирає дублювання при одночасному старті 2 worker-ів.
        time.sleep(0.25)
        rows_after = google_call_with_retry(lambda: ws.get_all_values())
        headers_after = rows_after[0] if rows_after else headers
        latest_after = None

        for row in rows_after[1:]:
            row_key = str(get_cell_by_header(row, headers_after, "Ключ запуску", "")).strip()
            if row_key == campaign_key:
                latest_after = row

        latest_after_status = ""
        if latest_after:
            latest_after_status = str(get_cell_by_header(latest_after, headers_after, "Статус", "")).strip()

        if latest_after_status != lock_status:
            print(
                f"broadcast skipped by sheet lock owner check: "
                f"key={campaign_key}, my={lock_status}, latest={latest_after_status}"
            )
            return False

        return True

    except Exception as e:
        print("acquire_persistent_broadcast_lock error:", e)
        # Без sheet-lock краще НЕ запускати розсилку, бо інакше може бути дублювання.
        return False


def finish_persistent_broadcast_lock(name, campaign_key, sent=0, failed=0, skipped=0, status="Завершено"):
    try:
        if not campaign_key:
            return
        ws = get_broadcast_runs_worksheet()
        google_call_with_retry(lambda: ws.append_row([
            now_str(),
            str(name or "Розсилка"),
            str(campaign_key),
            str(status or "Завершено"),
            int(sent or 0),
            int(failed or 0),
            int(skipped or 0)
        ], value_input_option="USER_ENTERED"))
        clear_cache(BROADCAST_RUNS_SHEET_NAME)
    except Exception as e:
        print("finish_persistent_broadcast_lock error:", e)


def get_broadcast_recipient_log_worksheet():
    headers = [
        "Дата",
        "Telegram ID",
        "Тип розсилки",
        "Ключ розсилки",
        "Статус"
    ]
    # Під час розсилки цей лист викликається багато разів.
    # Якщо щоразу перевіряти заголовки через get_all_values(), Google Sheets швидко дає 429.
    if BROADCAST_HISTORY_SHEET_NAME in SERVICE_WORKSHEETS_READY:
        return get_cached_worksheet(BROADCAST_HISTORY_SHEET_NAME)

    ws = get_or_create_worksheet(BROADCAST_HISTORY_SHEET_NAME, headers)
    ensure_headers(ws, headers)
    SERVICE_WORKSHEETS_READY.add(BROADCAST_HISTORY_SHEET_NAME)
    return ws


def _read_broadcast_history_rows():
    """
    Читаємо новий лист Історія розсилок і, якщо є, старий лист Надіслані розсилки клієнтам.
    Це потрібно, щоб після деплою бот не забув уже надіслані сьогодні повідомлення.
    """
    all_rows = []
    for sheet_name in [BROADCAST_HISTORY_SHEET_NAME, "Надіслані розсилки клієнтам"]:
        try:
            rows = get_values(sheet_name)
            if rows:
                all_rows.append(rows)
        except Exception:
            pass
    return all_rows


def normalize_broadcast_daily_bucket(campaign_type="", campaign_key=""):
    """
    Розділяє денний ліміт по типах розсилок.
    Тобто користувач може отримати в один день і Товар дня, і Акцію,
    але не отримає дубль тієї самої кампанії.
    """
    text = f"{campaign_type or ''} {campaign_key or ''}".strip().lower()

    if "auto_product" in text or "товар дня" in text:
        return "product_day"

    if "sale" in text or "акція" in text or "акции" in text:
        return "sale"

    if "daily" in text or "комплімент" in text or "комплимент" in text or "гороскоп" in text or "повідомлення дня" in text:
        return "daily_message"

    if "cart_reminder" in text or "нагадування кошика" in text or "кошик" in text:
        return "cart_reminder"

    return "marketing"


def broadcast_daily_limit_count(today_counts, telegram_id, campaign_type="", campaign_key=""):
    try:
        bucket = normalize_broadcast_daily_bucket(campaign_type, campaign_key)
        return int(today_counts.get(f"{str(telegram_id).strip()}|{bucket}", 0) or 0)
    except Exception:
        return 0


def increment_broadcast_daily_limit_count(today_counts, telegram_id, campaign_type="", campaign_key=""):
    try:
        telegram_id = str(telegram_id).strip()
        bucket = normalize_broadcast_daily_bucket(campaign_type, campaign_key)
        today_counts[f"{telegram_id}|{bucket}"] = today_counts.get(f"{telegram_id}|{bucket}", 0) + 1
        # Загальний лічильник залишаємо для статистики/сумісності,
        # але він більше не блокує різні типи розсилок.
        today_counts[telegram_id] = today_counts.get(telegram_id, 0) + 1
    except Exception as e:
        print("increment_broadcast_daily_limit_count error:", e)


def get_broadcast_recipient_log_snapshot():
    """
    Один раз читаємо історію перед масовою розсилкою.
    Повертає:
    - sent_keys: user|campaign_key, які вже точно були надіслані;
    - today_counts: скільки маркетингових/акційних повідомлень клієнт вже отримав сьогодні.
    """
    sent_keys = set()
    today_counts = {}

    try:
        # Гарантуємо створення нового листа навіть до першого запису.
        get_broadcast_recipient_log_worksheet()

        today_prefix = current_time().strftime("%d.%m.%Y")
        for rows in _read_broadcast_history_rows():
            if not rows:
                continue
            headers = rows[0]
            for row in rows[1:]:
                row_date = str(get_cell_by_header(row, headers, "Дата", row[0] if len(row) > 0 else "")).strip()
                row_user = str(get_cell_by_header(row, headers, "Telegram ID", row[1] if len(row) > 1 else "")).strip()
                row_key = str(get_cell_by_header(row, headers, "Ключ розсилки", "")).strip()
                row_type = str(get_cell_by_header(row, headers, "Тип розсилки", "")).strip()
                row_status = str(get_cell_by_header(row, headers, "Статус", "")).strip().lower()

                if not row_user or row_status not in ["надіслано", "sent", "так"]:
                    continue

                if row_key:
                    sent_keys.add(f"{row_user}|{row_key}")

                if row_date.startswith(today_prefix):
                    bucket = normalize_broadcast_daily_bucket(row_type, row_key)
                    today_counts[f"{row_user}|{bucket}"] = today_counts.get(f"{row_user}|{bucket}", 0) + 1
                    # Загальний лічильник залишаємо для статистики та старих викликів.
                    today_counts[row_user] = today_counts.get(row_user, 0) + 1

    except Exception as e:
        print("get_broadcast_recipient_log_snapshot error:", e)

    return sent_keys, today_counts


def broadcast_recipient_already_sent_today(telegram_id, campaign_type="", campaign_key=""):
    if BROADCAST_DAILY_LIMIT_PER_USER <= 0:
        return False
    try:
        _, today_counts = get_broadcast_recipient_log_snapshot()
        return broadcast_daily_limit_count(today_counts, telegram_id, campaign_type, campaign_key) >= BROADCAST_DAILY_LIMIT_PER_USER
    except Exception as e:
        print("broadcast_recipient_already_sent_today error:", e)
        return False


def broadcast_recipient_key_sent(telegram_id, campaign_key):
    try:
        if not campaign_key:
            return False
        sent_keys, _ = get_broadcast_recipient_log_snapshot()
        return f"{str(telegram_id).strip()}|{str(campaign_key).strip()}" in sent_keys
    except Exception as e:
        print("broadcast_recipient_key_sent error:", e)
        return False


def append_broadcast_recipient_logs(rows_to_append):
    """
    Записуємо логи розсилки пачкою.
    Якщо запис не вдався — кидаємо помилку вгору.
    Це важливо: краще НЕ відправити повідомлення, ніж відправити без запису історії
    і потім продублювати його при наступному запуску.
    """
    if not rows_to_append:
        return

    try:
        ws = get_broadcast_recipient_log_worksheet()
        google_call_with_retry(lambda: ws.append_rows(rows_to_append, value_input_option="USER_ENTERED"))
        clear_cache(BROADCAST_HISTORY_SHEET_NAME)
        clear_cache("Надіслані розсилки клієнтам")
    except Exception as e:
        print("append_broadcast_recipient_logs error:", e)
        try:
            ws = get_broadcast_recipient_log_worksheet()
            for row in rows_to_append:
                google_call_with_retry(lambda row=row: ws.append_row(row, value_input_option="USER_ENTERED"))
            clear_cache(BROADCAST_HISTORY_SHEET_NAME)
            clear_cache("Надіслані розсилки клієнтам")
        except Exception as e2:
            print("append_broadcast_recipient_logs fallback error:", e2)
            raise e2


def mark_broadcast_recipient_sent(telegram_id, campaign_type, campaign_key):
    try:
        append_broadcast_recipient_logs([[
            now_str(),
            str(telegram_id).strip(),
            str(campaign_type or "").strip(),
            str(campaign_key or "").strip(),
            "Надіслано"
        ]])
    except Exception as e:
        print("mark_broadcast_recipient_sent error:", e)


def send_broadcast_telegram_message(chat_id, text, keyboard=None, photo_url=None):
    """
    Легка відправка саме для масових розсилок.

    Повертає:
    {
        "ok": True/False,
        "status": "Активний" / "Заблокував бота" / "Чат не знайдено" / ...,
        "error": текст помилки Telegram
    }

    ВАЖЛИВО: тут НЕ пишемо в Google Sheets після кожної помилки.
    Статуси користувачів накопичуються і пишуться пачкою після завершення маленького запуску.
    """
    reply_markup = None
    if keyboard:
        keyboard = normalize_inline_keyboard(keyboard)
        reply_markup = json.dumps(keyboard, ensure_ascii=False)

    try:
        if photo_url:
            payload = {
                "chat_id": chat_id,
                "photo": photo_url,
                "caption": text,
                "parse_mode": "HTML"
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup

            response = requests.post(f"{BASE_URL}/sendPhoto", json=payload, timeout=15)

            if response.ok:
                return {"ok": True, "status": "Активний", "error": ""}

            error_text = str(response.text or "")[:500]
            status = classify_telegram_send_error(error_text)
            print("send_broadcast photo telegram error:", chat_id, error_text)
            return {"ok": False, "status": status, "error": error_text}

        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML"
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        response = requests.post(f"{BASE_URL}/sendMessage", json=payload, timeout=15)

        if response.ok:
            return {"ok": True, "status": "Активний", "error": ""}

        error_text = str(response.text or "")[:500]
        status = classify_telegram_send_error(error_text)
        print("send_broadcast message telegram error:", chat_id, error_text)
        return {"ok": False, "status": status, "error": error_text}

    except Exception as e:
        error_text = str(e)[:500]
        print("send_broadcast_telegram_message error:", chat_id, error_text)
        return {"ok": False, "status": "Помилка відправки", "error": error_text}


def batch_update_broadcast_user_statuses(status_rows):
    """
    Після маленького запуску розсилки одним batch_update позначаємо заблокованих/недоступних
    у листі "Користувачі", щоб наступні розсилки їх пропускали.
    Не викликається для кожного користувача окремо — це захищає Render від падінь.
    """
    try:
        if not status_rows:
            return 0

        ws, cols = ensure_user_columns(["Статус бота", "Дата перевірки статусу", "Остання помилка бота"])
        if not ws or not cols:
            return 0

        rows = google_call_with_retry(lambda: ws.get_all_values())
        if len(rows) <= 1:
            return 0

        row_by_telegram_id = {}
        for row_index, row in enumerate(rows[1:], start=2):
            telegram_id = str(row[0] if len(row) > 0 else "").strip()
            if telegram_id:
                row_by_telegram_id[telegram_id] = row_index

        now_value = now_str()
        updates = []
        updated = 0

        for item in status_rows:
            telegram_id = str(item.get("telegram_id", "")).strip()
            status = str(item.get("status", "")).strip()
            error_text = str(item.get("error", "") or "")[:500]

            if not telegram_id or not status:
                continue

            row_index = row_by_telegram_id.get(telegram_id)
            if not row_index:
                continue

            updates.append({"range": f"{col_to_letter(cols['Статус бота'])}{row_index}", "values": [[status]]})
            updates.append({"range": f"{col_to_letter(cols['Дата перевірки статусу'])}{row_index}", "values": [[now_value]]})
            updates.append({"range": f"{col_to_letter(cols['Остання помилка бота'])}{row_index}", "values": [[error_text]]})
            updated += 1

        if updates:
            write_user_status_batch(ws, updates)
            clear_cache("Користувачі")

        print(f"batch_update_broadcast_user_statuses updated={updated}")
        return updated

    except Exception as e:
        print("batch_update_broadcast_user_statuses error:", e)
        return 0


def get_marketing_worksheet():
    headers = [
        "ID розсилки",
        "Дата",
        "Тип",
        "ID товару",
        "Заголовок",
        "Текст",
        "Текст кнопки",
        "Активна",
        "Надіслано",
        "Дата надсилання"
    ]
    return get_or_create_worksheet("Розсилки", headers)


def get_sale_broadcasts_worksheet():
    headers = [
        "Дата",
        "ID товару",
        "Назва товару",
        "Статус",
        "Тип",
        "Ключ акції",
        "Акція від",
        "Акція до"
    ]
    ws = get_or_create_worksheet("Надіслані акції", headers)
    ensure_headers(ws, headers)
    return ws


def get_broadcast_client_ids():
    """
    Беремо користувачів, яким бот ще може писати.
    Тих, хто вже має статус "Заблокував бота", "Чат не знайдено", "Неактивний акаунт",
    у масові розсилки більше не включаємо.
    """
    ids = []
    blocked_statuses = {
        "заблокував бота",
        "чат не знайдено",
        "неактивний акаунт",
        "недоступний"
    }

    try:
        rows = get_values("Користувачі")
        if not rows:
            return ids

        headers = rows[0]
        status_col_index = None
        for idx, header in enumerate(headers):
            if str(header).strip().lower() == "статус бота":
                status_col_index = idx
                break

        for row in rows[1:]:
            telegram_id = str(row[0] if len(row) > 0 else "").strip()
            if not telegram_id:
                continue

            status = ""
            if status_col_index is not None and len(row) > status_col_index:
                status = str(row[status_col_index] or "").strip().lower()

            if status in blocked_statuses:
                continue

            if telegram_id not in ids:
                ids.append(telegram_id)

    except Exception as e:
        print("get_broadcast_client_ids error:", e)

    return ids


def has_remaining_broadcast_recipients(campaign_key, sent_keys=None, today_counts=None, campaign_type="Маркетинг"):
    """
    Перевіряє, чи залишились клієнти, яким ще треба спробувати відправити цю розсилку.
    Викликається ПІСЛЯ запису історії та оновлення статусів заблокованих користувачів.
    Якщо нікого не залишилось — у "Запуски розсилок" ставимо "Завершено", а не "Пауза".
    """
    try:
        campaign_key = str(campaign_key or "").strip()
        if not campaign_key:
            return False

        if sent_keys is None or today_counts is None:
            sent_keys, today_counts = get_broadcast_recipient_log_snapshot()

        for client_id in get_broadcast_client_ids():
            client_id = str(client_id or "").strip()
            if not client_id:
                continue

            if f"{client_id}|{campaign_key}" in sent_keys:
                continue

            if BROADCAST_DAILY_LIMIT_PER_USER > 0 and broadcast_daily_limit_count(today_counts, client_id, campaign_type, campaign_key) >= BROADCAST_DAILY_LIMIT_PER_USER:
                continue

            return True

        return False
    except Exception as e:
        print("has_remaining_broadcast_recipients error:", e)
        # Якщо не змогли перевірити — краще залишити Пауза, щоб не позначити розсилку завершеною помилково.
        return True


def get_product_by_id(product_id):
    products = get_cached_records("Товари")
    for product in products:
        if str(product.get("ID товару", "")).strip() == str(product_id).strip():
            return product
    return None


def product_marketing_keyboard(product_id=None, button_text="🛍 Переглянути товар"):
    buttons = []

    if product_id:
        buttons.append([inline_button(button_text or "🛍 Переглянути товар", f"promo_product_{product_id}")])

    buttons.append([inline_button("🔥 Переглянути акції", "open_sales")])
    buttons.append([inline_button("📦 Відкрити каталог", "open_catalog")])

    return {"inline_keyboard": buttons}


def marketing_message_text(row_type, title, body, product=None):
    row_type = str(row_type or "").strip()
    title = str(title or "").strip()
    body = str(body or "").strip()

    if not title:
        if row_type.lower() == "акція":
            title = "🔥 Нова акція у крамничці"
        elif row_type.lower() == "товар дня":
            title = "✨ Товар дня"
        else:
            title = "💛 Новинка у нашій крамничці"

    text = f"<b>{title}</b>\n\n"

    if body:
        text += f"{body}\n\n"

    if product:
        name = safe_text(product.get("Назва товару"), "Товар")
        price = str(product.get("Ціна", "") or "").strip()
        sale_price = get_active_sale_price(product)
        old_price = str(product.get("Стара ціна", "") or "").strip() if is_product_sale_active(product) else ""
        sale = get_product_sale_text(product)

        text += f"🛍 <b>{name}</b>\n"

        if old_price and sale_price:
            text += f"💸 Стара ціна: <s>{old_price} грн</s>\n"
            text += f"🔥 Акційна ціна: <b>{sale_price} грн</b>\n"
        elif sale_price:
            text += f"🔥 Акційна ціна: <b>{sale_price} грн</b>\n"
        elif price:
            text += f"💰 Ціна: <b>{price} грн</b>\n"

        if sale:
            text += f"🎁 Акція: <b>{sale}</b>\n"
            period_info = sale_period_text(product)
            if period_info:
                text += f"{period_info}\n"

        gift_info = promo_gift_text_for_product(product)
        if gift_info:
            text += f"\n{gift_info}\n"

    text += "\nЗаходьте переглянути актуальні пропозиції 💛"
    return text



def send_marketing_to_all(text, keyboard=None, photo_url=None, campaign_key=None, campaign_type="Маркетинг"):
    """
    Масова розсилка з жорстким захистом від дублювання.

    Головне правило: перед тим як відправити повідомлення в Telegram,
    клієнт спочатку фіксується в листі "Історія розсилок".
    Якщо Google Sheets не зміг записати цей рядок — повідомлення НЕ відправляємо.

    Це прибирає ситуацію, коли Render/Google обриває процес після відправки,
    але до запису історії, і той самий клієнт отримує розсилку повторно.

    Повертає: sent, failed, completed
    completed=True тільки коли по цій розсилці більше немає клієнтів для відправки.
    """
    sent = 0
    failed = 0
    skipped = 0
    processed_this_run = 0
    status_rows = []

    if not campaign_key:
        campaign_key = f"{campaign_type}|{current_time().strftime('%d.%m.%Y')}|{str(text)[:80]}"
    campaign_key = str(campaign_key).strip()

    if not acquire_persistent_broadcast_lock(campaign_type, campaign_key):
        return 0, 0, False

    try:
        # Перед запуском беремо свіжу історію, щоб не повторювати тих,
        # кому ця кампанія вже була зарезервована/надіслана.
        get_broadcast_recipient_log_worksheet()
        clear_cache(BROADCAST_HISTORY_SHEET_NAME)
        clear_cache("Надіслані розсилки клієнтам")

        client_ids = get_broadcast_client_ids()
        sent_keys, today_counts = get_broadcast_recipient_log_snapshot()
        max_per_run = int(BROADCAST_MAX_RECIPIENTS_PER_RUN or 10)
        if max_per_run <= 0:
            max_per_run = 10

        for client_id in client_ids:
            try:
                client_id = str(client_id).strip()
                if not client_id:
                    continue

                unique_key = f"{client_id}|{campaign_key}"

                # Якщо вже є в історії — не чіпаємо цього клієнта.
                if unique_key in sent_keys:
                    skipped += 1
                    continue

                # Денний ліміт рахується по bucket: товар дня окремо, акція окремо.
                if BROADCAST_DAILY_LIMIT_PER_USER > 0 and broadcast_daily_limit_count(today_counts, client_id, campaign_type, campaign_key) >= BROADCAST_DAILY_LIMIT_PER_USER:
                    skipped += 1
                    continue

                # Пачка набрана — ставимо паузу і виходимо. Наступний запуск продовжить.
                if processed_this_run >= max_per_run:
                    try:
                        batch_update_broadcast_user_statuses(status_rows)
                    except Exception as status_error:
                        print("send_marketing_to_all pause status update error:", status_error)
                    finish_persistent_broadcast_lock(
                        campaign_type,
                        campaign_key,
                        sent=sent,
                        failed=failed,
                        skipped=skipped,
                        status="Пауза"
                    )
                    print(
                        f"send_marketing_to_all pause type={campaign_type}, key={campaign_key}, "
                        f"sent={sent}, failed={failed}, skipped={skipped}"
                    )
                    return sent, failed, False

                # КРИТИЧНО: резервуємо клієнта ДО Telegram-відправки.
                # Якщо цей запис не вийде зробити — не відправляємо повідомлення,
                # бо інакше при падінні Render буде дубль.
                try:
                    append_broadcast_recipient_logs([[
                        now_str(),
                        client_id,
                        str(campaign_type or "").strip(),
                        campaign_key,
                        "Надіслано"
                    ]])
                except Exception as reserve_error:
                    print("send_marketing_to_all reserve failed, stop before send:", client_id, reserve_error)
                    finish_persistent_broadcast_lock(
                        campaign_type,
                        campaign_key,
                        sent=sent,
                        failed=failed,
                        skipped=skipped,
                        status="Пауза"
                    )
                    return sent, failed, False

                sent_keys.add(unique_key)
                increment_broadcast_daily_limit_count(today_counts, client_id, campaign_type, campaign_key)

                send_result = send_broadcast_telegram_message(
                    chat_id=client_id,
                    text=text,
                    keyboard=keyboard,
                    photo_url=photo_url
                )
                processed_this_run += 1

                ok = bool(send_result.get("ok")) if isinstance(send_result, dict) else bool(send_result)
                send_status = send_result.get("status", "") if isinstance(send_result, dict) else ""
                send_error = send_result.get("error", "") if isinstance(send_result, dict) else ""

                if ok:
                    sent += 1
                else:
                    failed += 1
                    if send_status in ["Заблокував бота", "Чат не знайдено", "Неактивний акаунт", "Недоступний"]:
                        status_rows.append({
                            "telegram_id": client_id,
                            "status": send_status,
                            "error": send_error
                        })

            except Exception as e:
                print("send_marketing_to_all user error:", client_id, e)
                failed += 1

        batch_update_broadcast_user_statuses(status_rows)

        finish_persistent_broadcast_lock(
            campaign_type,
            campaign_key,
            sent=sent,
            failed=failed,
            skipped=skipped,
            status="Завершено"
        )

        print(
            f"send_marketing_to_all type={campaign_type}, key={campaign_key}, "
            f"sent={sent}, failed={failed}, skipped={skipped}, completed=True"
        )
        return sent, failed, True

    except Exception as e:
        print("send_marketing_to_all error:", e)
        try:
            batch_update_broadcast_user_statuses(status_rows)
        except Exception as status_error:
            print("send_marketing_to_all status update error:", status_error)
        finish_persistent_broadcast_lock(
            campaign_type,
            campaign_key,
            sent=sent,
            failed=failed,
            skipped=skipped,
            status="Помилка"
        )
        return sent, failed, False


def process_marketing_broadcasts():
    """
    Запускається через /marketing-broadcasts.
    Надсилає заплановані рядки з листа "Розсилки".
    За один запуск бере обмежену кількість розсилок, щоб не було спаму.
    """
    if not broadcasts_allowed_now("marketing broadcasts"):
        return 0

    if not acquire_broadcast_lock("marketing-broadcasts"):
        return 0

    try:
        ws = get_marketing_worksheet()
        rows = google_call_with_retry(lambda: ws.get_all_values())
        if not rows:
            return 0

        headers = rows[0]
        today = current_time().date()
        sent_campaigns = 0

        for row_index, row in enumerate(rows[1:], start=2):
            if sent_campaigns >= MARKETING_BROADCAST_LIMIT_PER_RUN:
                break

            active = str(get_cell_by_header(row, headers, "Активна", "")).strip().lower()
            sent_flag = str(get_cell_by_header(row, headers, "Надіслано", "")).strip().lower()
            date_raw = get_cell_by_header(row, headers, "Дата", "")
            scheduled_date = parse_sheet_date(date_raw)

            if active not in ["так", "yes", "1", "true", "активна"]:
                continue
            if sent_flag in ["так", "yes", "1", "true", "надіслано"]:
                continue
            if scheduled_date and scheduled_date > today:
                continue

            row_type = get_cell_by_header(row, headers, "Тип", "")
            product_id = get_cell_by_header(row, headers, "ID товару", "")
            title = get_cell_by_header(row, headers, "Заголовок", "")
            body = get_cell_by_header(row, headers, "Текст", "")
            button_text = get_cell_by_header(row, headers, "Текст кнопки", "🛍 Переглянути товар")
            campaign_id = get_cell_by_header(row, headers, "ID розсилки", row_index)

            product = get_product_by_id(product_id) if product_id else None
            photos = get_product_photos(product) if product else []
            photo_url = photos[0] if photos else None

            text = marketing_message_text(row_type, title, body, product)
            keyboard = product_marketing_keyboard(product_id if product else None, button_text)
            campaign_key = f"marketing|{campaign_id}|{product_id}|{date_raw}|{title}"
            sent, failed, completed = send_marketing_to_all(
                text,
                keyboard,
                photo_url,
                campaign_key=campaign_key,
                campaign_type="Маркетинг"
            )

            if completed:
                update_cell_by_header(ws, row_index, headers, "Надіслано", "Так")
                update_cell_by_header(ws, row_index, headers, "Дата надсилання", now_str())
            sent_campaigns += 1

            print(f"marketing campaign sent row={row_index}, sent={sent}, failed={failed}, completed={completed}")

        # ВАЖЛИВО: /marketing-broadcasts більше не запускає автоматично "Товар дня".
        # Товар дня запускається тільки окремо через /auto-product-broadcasts
        # або через єдиний планувальник /scheduled-broadcasts.
        return sent_campaigns

    finally:
        release_broadcast_lock("marketing-broadcasts")

def sale_product_already_broadcasted(product, broadcast_type="Старт"):
    """
    Перевіряє, чи конкретну акцію вже розсилали.
    Ключ включає ID товару + тип акції + період, тому нова акція на той самий товар
    у майбутньому зможе розіслатися ще раз.
    """
    try:
        product_id = str(product.get("ID товару", "") if isinstance(product, dict) else product).strip()
        key = sale_broadcast_key(product) if isinstance(product, dict) else product_id
        rows = get_values("Надіслані акції")
        if not rows:
            return False

        headers = rows[0]
        for row in rows[1:]:
            row_product_id = str(get_cell_by_header(row, headers, "ID товару", row[1] if len(row) > 1 else "")).strip()
            row_type = str(get_cell_by_header(row, headers, "Тип", "")).strip()
            row_key = str(get_cell_by_header(row, headers, "Ключ акції", "")).strip()
            row_status = str(get_cell_by_header(row, headers, "Статус", "")).strip().lower()

            if row_key:
                if row_key == key and row_type == broadcast_type:
                    return True
            else:
                # Старі записи без ключа: вважаємо, що стартову розсилку по цьому товару вже робили.
                if broadcast_type == "Старт" and row_product_id == product_id and row_status in ["надіслано", "так", "sent"]:
                    return True

    except Exception as e:
        print("sale_product_already_broadcasted error:", e)
    return False


def mark_sale_product_broadcasted(product, broadcast_type="Старт"):
    try:
        product_id = str(product.get("ID товару", "")).strip()
        name = str(product.get("Назва товару", "")).strip()
        ws = get_sale_broadcasts_worksheet()
        ws.append_row([
            now_str(),
            product_id,
            name,
            "Надіслано",
            broadcast_type,
            sale_broadcast_key(product),
            str(product.get("Акція від", "") or product.get("Акція з", "") or ""),
            str(product.get("Акція до", "") or "")
        ], value_input_option="USER_ENTERED")
        clear_cache("Надіслані акції")
    except Exception as e:
        print("mark_sale_product_broadcasted error:", e)


def sale_broadcast_text(product, broadcast_type="Старт"):
    gift_config = get_promo_gift_config(product)

    if gift_config:
        gift_name = gift_config.get("gift_name", "подарунок")
        gift_price = gift_config.get("gift_price", 1)

        if broadcast_type == "Останній день":
            title = "🚨 ОСТАННІЙ ДЕНЬ АКЦІЇ НА КУШОНИ!"
            body = (
                f"Сьогодні останній день, коли при виборі будь-якого кушону "
                f"Ви можете отримати <b>{gift_name}</b> всього за <b>{gift_price} грн</b>."
            )
        elif broadcast_type == "3 дні":
            title = "⏳ Акція на кушони скоро завершується"
            body = (
                f"До завершення пропозиції залишилось лише 3 дні. Обирайте кушон, "
                f"а <b>{gift_name}</b> отримуйте всього за <b>{gift_price} грн</b>."
            )
        else:
            title = "☀️ Акція на кушони"
            body = (
                f"При виборі будь-якого кушону в асортименті Ви отримуєте "
                f"<b>{gift_name}</b> всього за <b>{gift_price} грн</b>."
            )

        return marketing_message_text("Акція", title, body, product)

    if broadcast_type == "Останній день":
        title = "🚨 ОСТАННІЙ ДЕНЬ АКЦІЇ!"
        body = "Сьогодні останній день дії цієї пропозиції. Завтра акція вже може бути недоступна."
    elif broadcast_type == "3 дні":
        title = "⏳ Акція скоро завершується"
        body = "До завершення акції залишилось лише 3 дні. Встигніть скористатися вигодою."
    else:
        title = "🔥 Нова акційна пропозиція"
        body = "Ми додали вигідну пропозицію для Вас."

    return marketing_message_text("Акція", title, body, product)

def process_sale_broadcasts():
    """
    Запускається через /sale-broadcasts.
    Працює комплексно:
    1) нова активна акція — розсилка один раз;
    2) за 3 дні до завершення — нагадування один раз;
    3) в останній день — нагадування один раз.
    """
    if not broadcasts_allowed_now("sale broadcasts"):
        return 0

    if not acquire_broadcast_lock("sale-broadcasts"):
        return 0

    try:
        # Важливо: після ручного додавання акції в Google Sheets Render міг ще тримати старий кеш.
        clear_cache("Товари")
        clear_cache("Надіслані акції")
        sale_products = get_sale_products()
        print(f"process_sale_broadcasts sale_products={len(sale_products)}")
        sent_count = 0

        for product in sale_products:
            if sent_count >= SALE_BROADCAST_LIMIT_PER_RUN:
                break

            product_id = str(product.get("ID товару", "")).strip()
            if not product_id:
                continue

            days_left = sale_days_left(product)

            broadcast_type = None
            if days_left == 0 and not sale_product_already_broadcasted(product, "Останній день"):
                broadcast_type = "Останній день"
            elif days_left == 3 and not sale_product_already_broadcasted(product, "3 дні"):
                broadcast_type = "3 дні"
            elif not sale_product_already_broadcasted(product, "Старт"):
                broadcast_type = "Старт"

            if not broadcast_type:
                continue

            photos = get_product_photos(product)
            photo_url = photos[0] if photos else None
            text = sale_broadcast_text(product, broadcast_type)
            keyboard = product_marketing_keyboard(product_id, "🔥 Переглянути товар")
            campaign_key = f"sale|{broadcast_type}|{sale_broadcast_key(product)}"

            sent, failed, completed = send_marketing_to_all(
                text,
                keyboard,
                photo_url,
                campaign_key=campaign_key,
                campaign_type=f"Акція: {broadcast_type}"
            )

            # Позначаємо акцію як оброблену тільки коли всю базу завершено.
            # Якщо Render обірве/зупинить запуск — наступний запуск продовжить розсилку.
            if completed:
                mark_sale_product_broadcasted(product, broadcast_type)
            sent_count += 1

            print(f"sale broadcast type={broadcast_type}, product={product_id}, sent={sent}, failed={failed}, completed={completed}")

        return sent_count

    finally:
        release_broadcast_lock("sale-broadcasts")


def inactive_client_text():
    return (
        "💛 <b>Ми давно Вас не бачили</b>\n\n"
        "У нашій крамничці вже зʼявилися новинки, акції та цікаві пропозиції.\n\n"
        "Завітайте до каталогу — можливо, саме зараз знайдеться щось для Вас ✨"
    )


def process_inactive_clients_reminders():
    """
    Запускається через /inactive-clients.
    Нагадує клієнтам, які не взаємодіяли з ботом INACTIVE_CLIENT_DAYS днів.
    Повторне нагадування — не частіше ніж раз на INACTIVE_CLIENT_DAYS днів.
    """
    headers_needed = [
        "Telegram ID",
        "Username",
        "Імʼя",
        "Прізвище",
        "Дата першого входу",
        "Дата останньої активності",
        "Кількість входів",
        "Останнє нагадування неактивним"
    ]

    ws = get_users_worksheet()
    ensure_headers(ws, headers_needed)
    rows = google_call_with_retry(lambda: ws.get_all_values())
    if not rows:
        return 0

    headers = rows[0]
    now_dt = current_time()
    sent = 0
    log_rows = []
    sent_keys, today_counts = get_broadcast_recipient_log_snapshot()
    sent_at = now_str()

    for row_index, row in enumerate(rows[1:], start=2):
        telegram_id = str(get_cell_by_header(row, headers, "Telegram ID", "")).strip()
        last_active_raw = get_cell_by_header(row, headers, "Дата останньої активності", "")
        last_reminder_raw = get_cell_by_header(row, headers, "Останнє нагадування неактивним", "")

        if not telegram_id:
            continue

        last_active = parse_bot_datetime(last_active_raw)
        if not last_active:
            continue

        days_inactive = (now_dt - last_active).days
        if days_inactive < INACTIVE_CLIENT_DAYS:
            continue

        last_reminder = parse_bot_datetime(last_reminder_raw)
        if last_reminder and (now_dt - last_reminder).days < INACTIVE_CLIENT_DAYS:
            continue

        campaign_key = f"inactive_client|{INACTIVE_CLIENT_DAYS}|{now_dt.strftime('%Y%m%d')}"
        unique_key = f"{telegram_id}|{campaign_key}"
        if unique_key in sent_keys:
            continue

        keyboard = {
            "inline_keyboard": [
                [inline_button("📦 Переглянути каталог", "open_catalog")],
                [inline_button("🔥 Переглянути акції", "open_sales")]
            ]
        }

        ok = send_message(telegram_id, inactive_client_text(), keyboard)
        if ok:
            update_cell_by_header(ws, row_index, headers, "Останнє нагадування неактивним", sent_at)
            sent_keys.add(unique_key)
            log_rows.append([sent_at, telegram_id, "Неактивний клієнт", campaign_key, "Надіслано"])
            sent += 1
            if len(log_rows) >= BROADCAST_LOG_FLUSH_EVERY:
                append_broadcast_recipient_logs(log_rows)
                log_rows = []

    append_broadcast_recipient_logs(log_rows)
    return sent




def get_auto_product_broadcasts_worksheet():
    headers = [
        "Дата",
        "ID товару",
        "Назва товару",
        "Статус"
    ]
    return get_or_create_worksheet("Надіслані товари дня", headers)


def auto_product_broadcast_sent_today():
    today = current_time().strftime("%d.%m.%Y")
    try:
        rows = get_values("Надіслані товари дня")[1:]
        for row in rows:
            sent_date = str(row[0] if len(row) > 0 else "").strip()
            status = str(row[3] if len(row) > 3 else "").strip().lower()
            if sent_date.startswith(today) and status in ["надіслано", "так", "sent"]:
                return True
    except Exception as e:
        print("auto_product_broadcast_sent_today error:", e)
    return False


def get_auto_broadcasted_product_ids():
    ids = set()
    try:
        rows = get_values("Надіслані товари дня")[1:]
        for row in rows:
            product_id = str(row[1] if len(row) > 1 else "").strip()
            status = str(row[3] if len(row) > 3 else "").strip().lower()
            if product_id and status in ["надіслано", "так", "sent"]:
                ids.add(product_id)
    except Exception as e:
        print("get_auto_broadcasted_product_ids error:", e)
    return ids


def mark_auto_product_broadcasted(product):
    try:
        product_id = str(product.get("ID товару", "")).strip()
        name = str(product.get("Назва товару", "")).strip()
        ws = get_auto_product_broadcasts_worksheet()
        ws.append_row([
            now_str(),
            product_id,
            name,
            "Надіслано"
        ], value_input_option="USER_ENTERED")
        clear_cache("Надіслані товари дня")
    except Exception as e:
        print("mark_auto_product_broadcasted error:", e)


def get_next_auto_product_for_broadcast():
    products = get_cached_records("Товари")
    sent_ids = get_auto_broadcasted_product_ids()

    active_products = []
    for product in products:
        product_id = str(product.get("ID товару", "")).strip()
        active = str(product.get("Активний", "")).strip().lower()
        if product_id and active in ["так", "yes", "1", "true", "активний"]:
            active_products.append(product)

    for product in active_products:
        product_id = str(product.get("ID товару", "")).strip()
        if product_id not in sent_ids:
            return product

    # Якщо вже всі товари були у розсилці — починаємо нове коло з першого активного товару.
    if active_products:
        return active_products[0]

    return None


def process_auto_product_day_broadcast():
    """
    Автоматична розсилка "товар дня".
    UptimeRobot може запускати її часто, але код відправить не більше 1 товару на день.
    Товари йдуть по черзі з листа "Товари".
    """
    if not broadcasts_allowed_now("auto product broadcast"):
        return 0

    today_key = current_time().strftime("%d.%m.%Y")
    if AUTO_PRODUCT_BROADCAST_LOCK.get("running") and AUTO_PRODUCT_BROADCAST_LOCK.get("date") == today_key:
        print("auto product broadcast skipped: already running")
        return 0

    if auto_product_broadcast_sent_today():
        return 0

    AUTO_PRODUCT_BROADCAST_LOCK["running"] = True
    AUTO_PRODUCT_BROADCAST_LOCK["date"] = today_key

    try:
        # Повторна перевірка всередині lock, щоб не було дублювання при одночасних запусках UptimeRobot.
        if auto_product_broadcast_sent_today():
            return 0

        product = get_next_auto_product_for_broadcast()

        if not product:
            return 0

        product_id = str(product.get("ID товару", "")).strip()
        photos = get_product_photos(product)
        photo_url = photos[0] if photos else None

        text = marketing_message_text(
            "Товар дня",
            "✨ Товар дня у нашій крамничці",
            "Сьогодні хочемо звернути Вашу увагу на цей товар:",
            product
        )
        keyboard = product_marketing_keyboard(product_id, "🛍 Переглянути товар")
        campaign_key = f"auto_product|{current_time().strftime('%d.%m.%Y')}|{product_id}"
        sent, failed, completed = send_marketing_to_all(
            text,
            keyboard,
            photo_url,
            campaign_key=campaign_key,
            campaign_type="Товар дня"
        )

        if completed:
            mark_auto_product_broadcasted(product)
            print(f"auto product broadcast completed product={product_id}, sent={sent}, failed={failed}")
            return 1

        if sent > 0:
            print(f"auto product broadcast paused product={product_id}, sent={sent}, failed={failed}")
            return 1

        return 0

    finally:
        AUTO_PRODUCT_BROADCAST_LOCK["running"] = False

def show_product_by_id(chat_id, product_id, callback_message=None):
    clear_service_messages(chat_id)
    clear_product_messages(chat_id)
    product = get_product_by_id(product_id)
    if not product:
        show_main_options(chat_id, "На жаль, товар уже не знайдено або він недоступний 😔", callback_message)
        return

    show_product_card(
        chat_id=chat_id,
        products=[product],
        index=0,
        mode="promo",
        category_id="",
        photo_index=0
    )

# =========================
# DATA HELPERS
# =========================

def get_active_categories():
    categories = get_cached_records("Категорії")
    return [
        c for c in categories
        if str(c.get("Активна")).strip().lower() in ["так", "yes", "true", "1"]
    ]


def get_category_by_id(category_id):
    for category in get_active_categories():
        if str(category.get("ID категорії", "")).strip() == str(category_id).strip():
            return category
    return None


def get_subcategory_by_id(subcategory_id):
    try:
        subcategories = get_cached_records("Підкатегорії")
    except Exception:
        return None

    for subcategory in subcategories:
        active = str(subcategory.get("Активна", "")).strip().lower()
        if (
            str(subcategory.get("ID підкатегорії", "")).strip() == str(subcategory_id).strip()
            and active in ["так", "yes", "1", "true", "активна"]
        ):
            return subcategory
    return None


def get_subsection_by_id(subsection_id):
    try:
        subsections = get_cached_records("Підрозділи")
    except Exception:
        return None

    for subsection in subsections:
        active = str(subsection.get("Активна", subsection.get("Активний", ""))).strip().lower()
        if (
            str(subsection.get("ID підрозділу", "")).strip() == str(subsection_id).strip()
            and active in ["так", "yes", "1", "true", "активна", "активний"]
        ):
            return subsection
    return None


def get_active_subcategories(category_id):
    subcategories = get_cached_records("Підкатегорії")
    result = []

    for item in subcategories:
        active = str(item.get("Активна", "")).strip().lower()
        item_category_id = str(item.get("ID категорії", "")).strip()

        if item_category_id == str(category_id) and active in ["так", "yes", "1", "true", "активна"]:
            result.append(item)

    return result


def get_active_subsections(subcategory_id):
    """
    3-й рівень каталогу: Категорія → Підкатегорія → Підрозділ → Товари.
    Дані беруться з листа "Підрозділи".
    """
    try:
        subsections = get_cached_records("Підрозділи")
    except Exception as e:
        print("get_active_subsections error:", e)
        return []

    result = []

    for item in subsections:
        active = str(item.get("Активна", item.get("Активний", ""))).strip().lower()
        item_subcategory_id = str(item.get("ID підкатегорії", "")).strip()

        if item_subcategory_id == str(subcategory_id) and active in ["так", "yes", "1", "true", "активна", "активний"]:
            result.append(item)

    return result


def get_products_by_subcategory(subcategory_id):
    """
    Запасний варіант для старої структури, якщо у підкатегорії немає підрозділів.
    Якщо у товарі вже заповнено "ID підрозділу", він показуватиметься через підрозділ.
    """
    products = get_cached_records("Товари")
    result = []

    for product in products:
        active = str(product.get("Активний", "")).strip().lower()
        product_subcategory_id = str(product.get("ID підкатегорії", "")).strip()
        product_subsection_id = str(product.get("ID підрозділу", "") or "").strip()

        if (
            product_subcategory_id == str(subcategory_id)
            and not product_subsection_id
            and active in ["так", "yes", "1", "true", "активний"]
        ):
            result.append(product)

    return result


def get_products_by_subsection(subsection_id):
    products = get_cached_records("Товари")
    result = []

    for product in products:
        active = str(product.get("Активний", "")).strip().lower()
        product_subsection_id = str(product.get("ID підрозділу", "") or "").strip()

        if product_subsection_id == str(subsection_id) and active in ["так", "yes", "1", "true", "активний"]:
            result.append(product)

    return result

def get_category_by_button_text(text):
    clean_text = str(text).replace("📁", "").strip()
    categories = get_active_categories()

    for cat in categories:
        if str(cat.get("Назва категорії")).strip() == clean_text:
            return cat

    return None


def get_active_products_by_category(category_id):
    products = get_cached_records("Товари")
    return [
        p for p in products
        if str(p.get("ID категорії")) == str(category_id)
        and str(p.get("Активний")).strip().lower() in ["так", "yes", "true", "1"]
    ]


def is_active_product_row(product):
    """
    Перевіряє, чи товар активний.
    Підтримує різні назви колонки: Активний / Активна / Статус.
    Якщо колонки активності немає взагалі — не блокуємо товар, щоб нові структури таблиці не ламали розсилку.
    """
    try:
        active_raw = (
            product.get("Активний")
            or product.get("Активна")
            or product.get("Статус")
            or ""
        )
        active_text = str(active_raw or "").strip().lower()

        if not active_text:
            # Якщо в рядку немає жодної колонки активності — вважаємо товар доступним.
            has_active_column = any(str(k).strip().lower() in ["активний", "активна", "статус"] for k in product.keys())
            return not has_active_column

        return active_text in ["так", "yes", "true", "1", "активний", "активна", "в наявності", "наявний"]
    except Exception:
        return False


def get_sale_products():
    # Примусово беремо свіжі товари, бо акції часто додаються прямо перед запуском.
    clear_cache("Товари")
    products = get_records("Товари")
    result = []

    for p in products:
        try:
            if is_product_sale_active(p) and is_active_product_row(p):
                result.append(p)
        except Exception as e:
            print("get_sale_products row error:", e)

    print(f"get_sale_products found={len(result)}")
    return result


def product_text(product, index=None, total=None):
    name = safe_text(product.get("Назва товару"), "Товар без назви")
    description = safe_text(product.get("Опис"), "")
    price = safe_text(product.get("Ціна"), "0")
    old_price = str(product.get("Стара ціна", "") or "").strip() if is_product_sale_active(product) else ""
    sale_price = get_active_sale_price(product)
    sale = get_product_sale_text(product)

    availability = safe_text(product.get("Наявність"), "")
    brand = safe_text(product.get("Бренд"), "")
    country = safe_text(product.get("Країна виробник"), "")
    volume = safe_text(product.get("Обʼєм / вага") or product.get("Об'єм / вага"), "")
    material = safe_text(product.get("Матеріал"), "")
    package = safe_text(product.get("Комплектація"), "")

    text = ""

    if index is not None and total is not None:
        text += f"📦 Товар {index + 1} з {total}\n\n"

    text += f"<b>{name}</b>\n\n"

    if description:
        text += f"{description}\n\n"

    if availability:
        text += f"📌 Наявність: <b>{availability}</b>\n"
    if brand:
        text += f"🏷 Бренд: {brand}\n"
    if country:
        text += f"🌍 Країна виробник: {country}\n"
    if volume:
        text += f"⚖️ Обʼєм / вага: {volume}\n"
    if material:
        text += f"🧱 Матеріал: {material}\n"
    if package:
        text += f"📦 Комплектація: {package}\n"

    if any([availability, brand, country, volume, material, package]):
        text += "\n"

    if old_price and sale_price:
        text += f"💸 Стара ціна: <s>{old_price} грн</s>\n"
        text += f"🔥 Акційна ціна: <b>{sale_price} грн</b>"
    elif sale_price:
        text += f"🔥 Акційна ціна: <b>{sale_price} грн</b>"
    else:
        text += f"💰 Ціна: <b>{price} грн</b>"

    if sale:
        text += f"\n🎁 Акція: <b>{sale}</b>"
        period_info = sale_period_text(product)
        if period_info:
            text += f"\n{period_info}"

    gift_info = promo_gift_text_for_product(product)
    if gift_info:
        text += f"\n\n{gift_info}"

    return text


def get_product_photos(product):
    photos = []

    for key in ["Фото 1", "Фото 2", "Фото 3", "Фото 4", "Фото 5"]:
        value = str(product.get(key, "") or "").strip()
        if value:
            photos.append(value)

    old_photo = str(product.get("Фото", "") or "").strip()
    if old_photo and old_photo not in photos:
        # Підтримка старої колонки "Фото".
        # Якщо там кілька посилань через кому — теж розділяємо.
        if "," in old_photo:
            for part in old_photo.split(","):
                part = part.strip()
                if part and part not in photos:
                    photos.append(part)
        else:
            photos.append(old_photo)

    return photos




def build_product_keyboard(product_id, products, index, mode="category", category_id="", photo_index=0):
    product = products[index]
    photos = get_product_photos(product)
    extra_photos = photos[1:] if len(photos) > 1 else []

    availability = str(product.get("Наявність", "") or "").strip().lower()

    if availability == "немає":
        buttons = [
            [inline_button("❌ Немає в наявності", "product_unavailable")]
        ]
    else:
        buttons = [
            [inline_button("🛒 Додати в кошик", f"add_one_{product_id}")],
            [inline_button("📞 Замовити через менеджера", f"contact_product_{product_id}")]
        ]

    if extra_photos:
        buttons.append([inline_button("📸 Більше фото", f"more_photos_{index}")])

    buttons.append([inline_button("🛒 Перейти в кошик", "open_cart")])

    return {"inline_keyboard": buttons}

def start(chat_id):
    USER_STATES.pop(str(chat_id), None)
    clear_product_messages(chat_id)
    clear_service_messages(chat_id)
    remove_reply_keyboard(chat_id)

    text = (
        "Привіт 👋\n\n"
        "Вітаємо у нашій крамничці 🛍💛\n\n"
        "Ми постійно оновлюємо асортимент, додаємо новинки та найкращі пропозиції для Вас ✨\n\n"
        "Обов'язково заглядайте до каталогу та розділу акцій — там регулярно з'являються нові товари та вигідні знижки 🔥\n\n"
        "Бажаємо приємних покупок та гарного настрою 🌸\n\n"
        "Оберіть, будь ласка, що хочете переглянути:"
    )
    send_service_message(chat_id, text, main_menu_inline(is_admin(chat_id)), clear_products=False)


def show_main_menu(chat_id, callback_message=None):
    USER_STATES.pop(str(chat_id), None)
    clear_product_messages(chat_id)
    clear_flow_messages(chat_id)
    text = "🏠 <b>Головне меню</b>\n\nОберіть, будь ласка, що хочете переглянути:"
    keyboard = main_menu_inline(is_admin(chat_id))

    if callback_message:
        update_service_message(chat_id, callback_message, text, keyboard, clear_products=False)
    else:
        remove_reply_keyboard(chat_id)
        send_service_message(chat_id, text, keyboard, clear_products=False)




def show_main_options(chat_id, text, callback_message=None, clear_products=True):
    """
    Показує клієнту повідомлення + повне головне меню,
    щоб після помилки/порожнього кошика/очищення не було тупика.
    """
    try:
        clear_flow_messages(chat_id)
        keyboard = main_menu_inline(is_admin(chat_id))
        full_text = f"{text}\n\n🏠 Оберіть, будь ласка, що хочете зробити далі:"

        if callback_message:
            return update_service_message(
                chat_id,
                callback_message,
                full_text,
                keyboard,
                clear_products=clear_products
            )

        return send_service_message(
            chat_id,
            full_text,
            keyboard,
            clear_products=clear_products
        )
    except Exception as e:
        print("show_main_options error:", e)
        return send_message(chat_id, text, main_menu_inline(is_admin(chat_id)))


def show_my_id(chat_id):
    send_service_message(chat_id, f"Ваш Telegram ID:\n<code>{chat_id}</code>", back_to_main_inline())


def show_catalog_menu(chat_id, callback_message=None):
    clear_product_messages(chat_id)
    active_categories = get_active_categories()

    if not active_categories:
        text = "Поки немає активних категорій 😔"
        update_service_message(chat_id, callback_message, text, back_to_main_inline())
        return

    buttons = []
    row = []
    for category in active_categories:
        category_id = str(category.get("ID категорії", "")).strip()
        name = safe_text(category.get("Назва категорії"), "Категорія")
        if not category_id:
            continue

        row.append(inline_button(f"📁 {name}", f"category_{category_id}"))
        if len(row) == 2:
            buttons.append(row)
            row = []

    if row:
        buttons.append(row)

    buttons.append([inline_button("🔥 Переглянути акції", "open_sales")])
    buttons.append([inline_button("⬅️ Назад у меню", "back_main")])

    keyboard = {"inline_keyboard": buttons}
    text = "📦 <b>Каталог</b>\n\nОберіть категорію нижче 👇"

    USER_STATES[str(chat_id)] = {"step": "catalog_inline"}

    update_service_message(chat_id, callback_message, text, keyboard)



def show_more_product_photos(chat_id, product_index):
    state = USER_STATES.get(str(chat_id), {})
    products = state.get("products", [])

    if not products:
        send_message(chat_id, "Не знайшла товар для перегляду фото 😔")
        return

    product_index = int(product_index)

    if product_index < 0 or product_index >= len(products):
        send_message(chat_id, "Не знайшла товар для перегляду фото 😔")
        return

    product = products[product_index]
    photos = get_product_photos(product)
    extra_photos = photos[1:] if len(photos) > 1 else []

    if not extra_photos:
        send_message(chat_id, "Додаткових фото для цього товару немає 😔")
        return

    header_message_id = send_message(chat_id, "📸 Додаткові фото товару:")
    register_product_message(chat_id, header_message_id, PRODUCT_CARD_AUTO_DELETE_SECONDS)

    for photo_url in extra_photos:
        ok = send_photo(chat_id, photo_url, "")

        if ok:
            register_product_message(chat_id, ok, PRODUCT_CARD_AUTO_DELETE_SECONDS)
        else:
            doc_ok = send_document(chat_id, photo_url, "")
            if doc_ok:
                register_product_message(chat_id, doc_ok, PRODUCT_CARD_AUTO_DELETE_SECONDS)



def product_photo_caption(product, index=None, total=None, max_len=1000):
    """
    Caption для фото товару: обовʼязково показуємо опис і ціну в одному повідомленні з фото.
    Якщо повний текст трохи більший за ліміт Telegram, скорочується тільки опис,
    а назва/ціна/акція залишаються.
    """
    full_text = product_text(product, index, total)
    if can_send_as_photo_caption(full_text):
        return full_text

    name = safe_text(product.get("Назва товару"), "Товар без назви")
    description = str(product.get("Опис") or "").strip()
    price = safe_text(product.get("Ціна"), "0")
    old_price = str(product.get("Стара ціна", "") or "").strip() if is_product_sale_active(product) else ""
    sale_price = get_active_sale_price(product)
    sale = get_product_sale_text(product)

    header = ""
    if index is not None and total is not None:
        header += f"📦 Товар {index + 1} з {total}\n\n"

    header += f"<b>{name}</b>\n\n"

    footer = ""
    if old_price and sale_price:
        footer += f"\n\n💸 Стара ціна: <s>{old_price} грн</s>\n🔥 Акційна ціна: <b>{sale_price} грн</b>"
    elif sale_price:
        footer += f"\n\n🔥 Акційна ціна: <b>{sale_price} грн</b>"
    else:
        footer += f"\n\n💰 Ціна: <b>{price} грн</b>"

    if sale:
        footer += f"\n🎁 Акція: <b>{sale}</b>"
        period_info = sale_period_text(product)
        if period_info:
            footer += f"\n{period_info}"

    gift_info = promo_gift_text_for_product(product)
    if gift_info:
        footer += f"\n\n{gift_info}"

    available_for_description = max_len - len(header) - len(footer)
    if available_for_description < 80:
        # Якщо навіть з описом місця мало — лишаємо назву + ціну/акцію.
        return (header.strip() + footer)[:max_len]

    if len(description) > available_for_description:
        description = description[:max(0, available_for_description - 3)].rstrip() + "..."

    return (header + description + footer).strip()[:max_len]


def product_short_caption(product, index=None, total=None):
    # Залишено для сумісності зі старими викликами.
    return product_photo_caption(product, index, total)


def can_send_as_photo_caption(text):
    """
    Telegram дозволяє caption до 1024 символів.
    Даємо запас до 1000 символів, щоб уникнути помилки через HTML/емодзі.
    """
    return len(str(text or "")) <= 1000


def send_product_text(chat_id, text, keyboard=None, auto_delete_after=None, track_product=False):
    """
    Telegram дозволяє довгий текст окремим повідомленням, але не дозволяє
    дуже довгий підпис під фото. Тому опис товару відправляємо окремо.
    Якщо це товарна картка — запамʼятовуємо message_id і видаляємо автоматично.
    """
    max_len = 3900

    if len(text) <= max_len:
        message_id = send_message(chat_id, text, keyboard)
        if track_product:
            register_product_message(chat_id, message_id, auto_delete_after)
        return [message_id] if message_id else []

    parts = []
    current = ""

    for paragraph in text.split("\n"):
        candidate = current + ("\n" if current else "") + paragraph

        if len(candidate) > max_len:
            if current:
                parts.append(current)
            current = paragraph
        else:
            current = candidate

    if current:
        parts.append(current)

    message_ids = []
    for idx, part in enumerate(parts):
        part_keyboard = keyboard if idx == len(parts) - 1 else None
        message_id = send_message(chat_id, part, part_keyboard)
        if message_id:
            message_ids.append(message_id)
            if track_product:
                register_product_message(chat_id, message_id, auto_delete_after)

    return message_ids


def show_product_card(chat_id, products, index=0, mode="category", category_id="", photo_index=0):
    clear_service_messages(chat_id)
    if not products:
        show_main_options(chat_id, "Товарів поки немає 😔")
        return

    total = len(products)
    index = max(0, min(int(index), total - 1))
    product = products[index]
    product_id = product.get("ID товару")

    USER_STATES[str(chat_id)] = {
        "step": "viewing_products",
        "products": products,
        "index": index,
        "mode": mode,
        "category_id": category_id
    }

    photos = get_product_photos(product)
    text = product_text(product, index, total)
    keyboard = build_product_keyboard(product_id, products, index, mode, category_id, 0)

    if photos:
        # Надсилаємо товар ОДНИМ повідомленням: фото + caption + кнопки.
        # Якщо повний опис довший за ліміт Telegram для caption, не дублюємо його
        # другим повідомленням, а показуємо короткий caption з кнопками.
        # Коли опис у таблиці буде скорочений до ~1000 символів — він піде повністю під фото.
        caption = product_photo_caption(product, index, total)
        ok = send_photo(chat_id, photos[0], caption, keyboard)

        if ok:
            register_product_message(chat_id, ok, PRODUCT_CARD_AUTO_DELETE_SECONDS)
        else:
            doc_ok = send_document(chat_id, photos[0], caption, keyboard)
            if doc_ok:
                register_product_message(chat_id, doc_ok, PRODUCT_CARD_AUTO_DELETE_SECONDS)
            else:
                send_product_text(
                    chat_id,
                    text,
                    keyboard,
                    auto_delete_after=PRODUCT_CARD_AUTO_DELETE_SECONDS,
                    track_product=True
                )
    else:
        send_product_text(
            chat_id,
            text,
            keyboard,
            auto_delete_after=PRODUCT_CARD_AUTO_DELETE_SECONDS,
            track_product=True
        )



def build_products_page_keyboard(page, total_pages):
    buttons = []

    nav_row = []
    if page > 0:
        nav_row.append(inline_button("⬅️ Попередня", f"products_page_{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(inline_button("Наступна ➡️", f"products_page_{page + 1}"))

    if nav_row:
        buttons.append(nav_row)

    buttons.append([inline_button("📦 До каталогу", "back_categories")])
    buttons.append([inline_button("🛒 Перейти в кошик", "open_cart")])
    return {"inline_keyboard": buttons}


def show_products_page(chat_id, products, page=0, mode="category", category_id="", callback_message=None):
    # При переході на нову сторінку/розділ прибираємо старі товарні картки.
    clear_product_messages(chat_id)

    if not products:
        text = "Товарів поки немає 😔"
        keyboard = back_to_main_inline()
        update_service_message(chat_id, callback_message, text, keyboard)
        return

    total = len(products)
    page_size = max(1, PRODUCTS_PAGE_SIZE)
    total_pages = (total + page_size - 1) // page_size
    page = max(0, min(int(page), total_pages - 1))

    start_index = page * page_size
    end_index = min(start_index + page_size, total)

    USER_STATES[str(chat_id)] = {
        "step": "viewing_products",
        "products": products,
        "index": start_index,
        "mode": mode,
        "category_id": category_id,
        "page": page
    }

    header = (
        f"📦 Знайдено товарів: <b>{total}</b>\n"
        f"Показуємо: <b>{start_index + 1}–{end_index}</b> з <b>{total}</b>\n"
        f"Сторінка: <b>{page + 1}</b> з <b>{total_pages}</b>"
    )

    update_service_message(chat_id, callback_message, header, build_products_page_keyboard(page, total_pages), clear_products=False)

    for idx in range(start_index, end_index):
        show_product_card(
            chat_id=chat_id,
            products=products,
            index=idx,
            mode=mode,
            category_id=category_id,
            photo_index=0
        )

    # Додаємо кнопки навігації ще раз після товарів, щоб користувачу не треба було скролити вгору.
    nav_message_id = send_message(
        chat_id,
        f"📄 Сторінка <b>{page + 1}</b> з <b>{total_pages}</b>",
        build_products_page_keyboard(page, total_pages)
    )
    register_product_message(chat_id, nav_message_id, PRODUCT_CARD_AUTO_DELETE_SECONDS)


def update_product_card(chat_id, message_id, products, index=0, mode="category", category_id="", photo_index=0, callback_message=None):
    if not products:
        edit_message(
            chat_id,
            message_id,
            "Товарів поки немає 😔",
            {"inline_keyboard": [[inline_button("🛒 Кошик", "open_cart")]]}
        )
        return

    total = len(products)
    index = max(0, min(int(index), total - 1))
    product = products[index]
    product_id = product.get("ID товару")

    USER_STATES[str(chat_id)] = {
        "step": "viewing_products",
        "products": products,
        "index": index,
        "mode": mode,
        "category_id": category_id
    }

    photos = get_product_photos(product)
    photo_index = max(0, min(int(photo_index), len(photos) - 1)) if photos else 0

    text = product_text(product, index, total)
    keyboard = build_product_keyboard(product_id, products, index, mode, category_id, photo_index)

    if photos:
        # Редагуємо одну товарну картку. Якщо caption довгий — показуємо короткий,
        # але не надсилаємо друге повідомлення з описом, щоб не було дублювання.
        caption = product_photo_caption(product, index, total)
        edit_media_photo(chat_id, message_id, photos[photo_index], caption, keyboard)
    else:
        send_product_text(chat_id, text, keyboard, auto_delete_after=PRODUCT_CARD_AUTO_DELETE_SECONDS, track_product=True)

def show_subcategories_reply(chat_id, category_id):
    subcategories = get_active_subcategories(category_id)

    if not subcategories:
        send_message(
            chat_id,
            "У цій категорії поки немає розділів 😔",
            categories_menu()
        )
        return

    USER_STATES[str(chat_id)] = {
        "step": "choosing_subcategory",
        "category_id": category_id
    }

    send_message(
        chat_id,
        "📂 <b>Розділи</b>\n\nОберіть розділ нижче 👇",
        subcategories_menu(category_id)
    )


def show_subsections_reply(chat_id, subcategory_id):
    state = USER_STATES.get(str(chat_id), {})
    category_id = state.get("category_id", "")
    subsections = get_active_subsections(subcategory_id)

    # Якщо підрозділів немає — залишаємо стару логіку і показуємо товари з цього розділу.
    # Так старі розділи без третього рівня не ламаються.
    if not subsections:
        with_loading(chat_id, "📦 Завантажуємо товари...", show_products_by_subcategory, chat_id, subcategory_id)
        return

    USER_STATES[str(chat_id)] = {
        "step": "choosing_subsection",
        "category_id": category_id,
        "subcategory_id": subcategory_id
    }

    send_message(
        chat_id,
        "▫️ <b>Підрозділи</b>\n\nОберіть підрозділ нижче 👇",
        subsections_menu(subcategory_id)
    )


def show_subcategories(chat_id, category_id, callback_message=None):
    clear_product_messages(chat_id)
    category = get_category_by_id(category_id)
    category_name = safe_text(category.get("Назва категорії") if category else "", "Категорія")
    subcategories = get_active_subcategories(category_id)

    USER_STATES[str(chat_id)] = {
        "step": "choosing_subcategory_inline",
        "category_id": str(category_id)
    }

    if not subcategories:
        # Якщо в категорії немає розділів — одразу показуємо товари категорії.
        show_products_by_category(chat_id, category_id, callback_message)
        return

    buttons = []
    for subcategory in subcategories:
        subcategory_id = str(subcategory.get("ID підкатегорії", "")).strip()
        name = safe_text(subcategory.get("Назва підкатегорії"), "Розділ")
        if subcategory_id:
            buttons.append([inline_button(f"📂 {name}", f"subcategory_{subcategory_id}")])

    buttons.append([inline_button("⬅️ Назад до категорій", "back_categories")])

    keyboard = {"inline_keyboard": buttons}
    text = f"📂 <b>{category_name}</b>\n\nОберіть розділ нижче 👇"

    update_service_message(chat_id, callback_message, text, keyboard)


def show_subsections(chat_id, subcategory_id, callback_message=None):
    clear_product_messages(chat_id)
    state = USER_STATES.get(str(chat_id), {})
    category_id = state.get("category_id", "")

    subcategory = get_subcategory_by_id(subcategory_id)
    subcategory_name = safe_text(subcategory.get("Назва підкатегорії") if subcategory else "", "Розділ")
    if not category_id and subcategory:
        category_id = str(subcategory.get("ID категорії", "")).strip()

    subsections = get_active_subsections(subcategory_id)

    USER_STATES[str(chat_id)] = {
        "step": "choosing_subsection_inline",
        "category_id": str(category_id),
        "subcategory_id": str(subcategory_id)
    }

    if not subsections:
        # Якщо підрозділів немає — показуємо товари цього розділу.
        show_products_by_subcategory(chat_id, subcategory_id, callback_message)
        return

    buttons = []
    for subsection in subsections:
        subsection_id = str(subsection.get("ID підрозділу", "")).strip()
        name = safe_text(subsection.get("Назва підрозділу"), "Підрозділ")
        if subsection_id:
            buttons.append([inline_button(f"▫️ {name}", f"subsection_{subsection_id}")])

    if category_id:
        buttons.append([inline_button("⬅️ Назад до розділів", f"back_subcategories_{category_id}")])
    else:
        buttons.append([inline_button("⬅️ Назад до категорій", "back_categories")])

    keyboard = {"inline_keyboard": buttons}
    text = f"▫️ <b>{subcategory_name}</b>\n\nОберіть підрозділ нижче 👇"

    update_service_message(chat_id, callback_message, text, keyboard)



def show_products_by_subcategory(chat_id, subcategory_id, callback_message=None):
    products = get_products_by_subcategory(subcategory_id)
    subcategory = get_subcategory_by_id(subcategory_id)
    category_id = str(subcategory.get("ID категорії", "")).strip() if subcategory else ""

    if not products:
        text = "У цьому розділі поки немає товарів 😔"
        back_callback = f"back_subcategories_{category_id}" if category_id else "back_categories"
        keyboard = {
            "inline_keyboard": [
                [inline_button("⬅️ Назад", back_callback)],
                [inline_button("📦 До каталогу", "back_categories")]
            ]
        }

        if callback_message and can_edit_as_text(callback_message):
            edit_message(chat_id, callback_message["message_id"], text, keyboard)
        else:
            send_service_message(chat_id, text, keyboard, clear_products=False)
        return

    state = USER_STATES.get(str(chat_id), {})
    state.update({
        "category_id": category_id,
        "subcategory_id": str(subcategory_id),
        "back_to": f"back_subcategories_{category_id}" if category_id else "back_categories"
    })
    USER_STATES[str(chat_id)] = state

    show_products_page(
        chat_id=chat_id,
        products=products,
        page=0,
        mode="subcategory",
        category_id=str(subcategory_id),
        callback_message=callback_message
    )



def show_products_by_subsection(chat_id, subsection_id, callback_message=None):
    products = get_products_by_subsection(subsection_id)
    subsection = get_subsection_by_id(subsection_id)
    subcategory_id = str(subsection.get("ID підкатегорії", "")).strip() if subsection else ""
    subcategory = get_subcategory_by_id(subcategory_id) if subcategory_id else None
    category_id = str(subcategory.get("ID категорії", "")).strip() if subcategory else ""

    if not products:
        text = "У цьому підрозділі поки немає товарів 😔"
        back_callback = f"back_subsections_{subcategory_id}" if subcategory_id else "back_categories"
        keyboard = {
            "inline_keyboard": [
                [inline_button("⬅️ Назад", back_callback)],
                [inline_button("📦 До каталогу", "back_categories")]
            ]
        }

        if callback_message:
            edit_message(chat_id, callback_message["message_id"], text, keyboard)
        else:
            send_message(chat_id, text, keyboard)
        return

    state = USER_STATES.get(str(chat_id), {})
    state.update({
        "step": "viewing_products",
        "category_id": category_id,
        "subcategory_id": subcategory_id,
        "subsection_id": str(subsection_id),
        "back_to": f"back_subsections_{subcategory_id}" if subcategory_id else "back_categories"
    })
    USER_STATES[str(chat_id)] = state

    show_products_page(
        chat_id=chat_id,
        products=products,
        page=0,
        mode="subsection",
        category_id=str(subsection_id),
        callback_message=callback_message
    )


def show_products_by_category(chat_id, category_id, callback_message=None):
    products = get_active_products_by_category(category_id)

    if not products:
        text = "У цій категорії поки немає товарів 😔"
        keyboard = {
            "inline_keyboard": [
                [inline_button("⬅️ Назад до категорій", "back_categories")]
            ]
        }
        if callback_message:
            edit_message(chat_id, callback_message["message_id"], text, keyboard)
        else:
            send_message(chat_id, text, keyboard)
        return

    state = USER_STATES.get(str(chat_id), {})
    state.update({
        "category_id": str(category_id),
        "back_to": "back_categories"
    })
    USER_STATES[str(chat_id)] = state

    show_products_page(chat_id, products, 0, "category", str(category_id), callback_message)


def show_sales(chat_id, callback_message=None):
    sale_products = get_sale_products()

    if not sale_products:
        show_main_options(chat_id, "Поки немає активних акцій 😔", callback_message)
        return

    show_products_page(
        chat_id=chat_id,
        products=sale_products,
        page=0,
        mode="sale",
        category_id="",
        callback_message=callback_message
    )

def add_to_cart(chat_id, product_id, callback_message=None):
    products = get_cached_records("Товари")
    product = None

    for p in products:
        if str(p.get("ID товару")) == str(product_id):
            product = p
            break

    if not product:
        show_main_options(chat_id, "Товар не знайдено 😔", callback_message)
        return

    availability = str(product.get("Наявність", "") or "").strip().lower()

    if availability == "немає":
        show_main_options(chat_id, "❌ Цього товару зараз немає в наявності.", callback_message)
        return

    name = safe_text(product.get("Назва товару"), "Товар")
    price = safe_float(get_active_sale_price(product) or product.get("Ціна") or 0)
    promo = get_product_promo_deal(product)
    gift_config = get_promo_gift_config(product)

    # Для акцій типу 1=2 та 1+1=3 у кошику показуємо фактичну кількість,
    # але рахуємо суму тільки за оплачені одиниці.
    receive_qty = int(promo.get("receive_qty", 1)) if promo else 1
    paid_qty = int(promo.get("paid_qty", 1)) if promo else 1
    qty_to_add = receive_qty
    sum_to_add = round(price * paid_qty, 2)

    existing = find_cart_row_by_product(chat_id, product_id)

    if existing:
        row_index = existing["row_index"]
        old_qty = safe_int(existing.get("qty"), 1)
        old_sum = safe_float(existing.get("sum"), 0)
        new_qty = old_qty + qty_to_add
        new_sum = round(old_sum + sum_to_add, 2)

        update_cell("Кошик", row_index, 5, new_qty)
        update_cell("Кошик", row_index, 6, new_sum)
        update_cart_reminder_columns(row_index, updated_at=now_str(), reminder1="", reminder2="", reminder3="")
    else:
        new_qty = qty_to_add
        new_sum = sum_to_add
        get_cart_worksheet()
        append_row("Кошик", [chat_id, product_id, name, price, new_qty, new_sum, now_str(), "", "", ""])

    sync_cart_promo_gifts(chat_id)

    if promo:
        promo_label = promo.get("label", "Акція")
        text = (
            f"✅ Товар <b>{name}</b> додано в кошик.\n\n"
            f"🎁 Акція: <b>{promo_label}</b>\n"
            f"Кількість у кошику: <b>{new_qty} шт.</b>\n"
            f"До оплати за цей товар: <b>{new_sum} грн</b>"
        )
    else:
        text = (
            f"✅ Товар <b>{name}</b> додано в кошик.\n\n"
            f"Кількість: <b>{new_qty} шт.</b>\n"
            f"Сума: <b>{new_sum} грн</b>"
        )

    if gift_config:
        text += (
            f"\n\n🎁 За умовами акції до кошика також додано:\n"
            f"<b>{gift_config['gift_name']}</b> — <b>{gift_config['gift_price']} грн</b>"
        )

    keyboard = {
        "inline_keyboard": [
            [inline_button("🛒 Перейти в кошик", "open_cart")]
        ]
    }

    if callback_message:
        message_id = callback_message["message_id"]
        if "photo" in callback_message:
            edit_caption(chat_id, message_id, text, keyboard)
        else:
            edit_message(chat_id, message_id, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)

def show_cart(chat_id, callback_message=None):
    sync_cart_promo_gifts(chat_id)
    items = find_user_cart_rows(chat_id)

    if not items:
        show_main_options(
            chat_id,
            "Ваш кошик поки порожній 🛒",
            callback_message,
            clear_products=False
        )
        return

    subtotal = 0
    text = "🛒 <b>Ваш кошик:</b>\n\n"
    buttons = []

    for item in items:
        price = safe_float(item.get("price") or 0)
        qty = safe_int(item.get("qty") or 1, 1)
        summa = safe_float(item.get("sum") or price * qty)
        row_index = item["row_index"]

        subtotal += summa
        text += format_cart_item_line(item)

        if is_promo_gift_cart_id(item.get("product_id")):
            buttons.append([
                inline_button(f"🎁 Акційний товар: {qty} шт", f"cart_qty_{row_index}")
            ])
        else:
            buttons.append([
                inline_button("➖", f"cart_minus_{row_index}"),
                inline_button(f"{qty} шт", f"cart_qty_{row_index}"),
                inline_button("➕", f"cart_plus_{row_index}"),
                inline_button("❌", f"delete_cart_row_{row_index}")
            ])

    totals = calculate_cart_totals(chat_id)
    discount_percent = totals["discount_percent"]
    discount_amount = totals["discount_amount"]
    available_bonuses = totals.get("available_bonuses", 0)
    max_bonus_to_use = totals.get("max_bonus_to_use", 0)
    bonus_used = totals.get("bonus_used", 0)
    total = totals["total"]

    text += f"\n💰 Сума товарів: <b>{subtotal} грн</b>"

    if discount_percent:
        text += (
            f"\n🎁 Ваша знижка на це замовлення: <b>-{int(discount_percent)}%</b>"
            f"\n💸 Сума знижки: <b>{discount_amount} грн</b>"
        )

    if available_bonuses:
        bonus_eligible_subtotal = totals.get("bonus_eligible_subtotal", 0)
        text += (
            f"\n\n🎁 Ваші бонуси: <b>{available_bonuses}</b>"
            f"\n💰 Бонусами можна оплатити до <b>{int(BONUS_MAX_USE_PERCENT)}%</b> суми неакційних товарів."
            f"\n🧾 Сума товарів, доступна для списання бонусів: <b>{bonus_eligible_subtotal} грн</b>"
            f"\nМожна списати в цьому замовленні: <b>{max_bonus_to_use} грн</b>"
        )

        if bonus_eligible_subtotal <= 0:
            text += "\n⚠️ У кошику зараз лише акційні товари, тому бонуси до них не застосовуються."

        if bonus_used:
            text += f"\n✅ Бонуси застосовано: <b>-{bonus_used} грн</b>"

    text += f"\n✅ До сплати за товари: <b>{total} грн</b>"

    if total < FREE_DELIVERY_THRESHOLD:
        left = round(FREE_DELIVERY_THRESHOLD - total, 2)
        text += f"\n\n🚚 Безкоштовна доставка діє від <b>1000 грн</b>. Залишилось додати на <b>{left} грн</b>."
    else:
        text += "\n\n🚚 Вам доступна безкоштовна доставка."

    state = USER_STATES.get(str(chat_id), {})
    if available_bonuses and max_bonus_to_use > 0:
        if bonus_used:
            buttons.append([inline_button("🎁 Не використовувати бонуси", "bonus_disable")])
        else:
            buttons.append([inline_button(f"🎁 Використати бонуси (-{max_bonus_to_use} грн)", "bonus_use")])

    if state.get("step") == "adding_more_before_order":
        buttons.append([inline_button("✅ Продовжити оформлення", "continue_checkout")])
    else:
        buttons.append([inline_button("✅ Оформити замовлення", "order_now")])

    buttons.append([inline_button("🗑 Очистити кошик", "clear_cart")])

    keyboard = {"inline_keyboard": buttons}

    if callback_message and can_edit_as_text(callback_message):
        edit_message(chat_id, callback_message["message_id"], text, keyboard)
    else:
        send_service_message(chat_id, text, keyboard, clear_products=False)

def change_cart_qty(chat_id, row_index, delta, callback_message=None):
    rows = get_values("Кошик")

    try:
        row_index = int(row_index)
        row = rows[row_index - 1]
    except:
        show_main_options(chat_id, "Не вдалося змінити кількість. Спробуйте ще раз.", callback_message)
        return

    if len(row) < 6 or str(row[0]) != str(chat_id):
        show_main_options(chat_id, "Цей товар не знайдено у Вашому кошику.", callback_message)
        return

    try:
        product_id = str(row[1] if len(row) > 1 else "").strip()
        price = safe_float(row[3] or 0)
        qty = safe_int(row[4] or 1, 1)
        current_sum = safe_float(row[5] or 0)
    except:
        product_id = ""
        price = 0
        qty = 1
        current_sum = 0

    product = get_product_by_id(product_id) if product_id else None
    promo = get_product_promo_deal(product)

    receive_qty = int(promo.get("receive_qty", 1)) if promo else 1
    paid_qty = int(promo.get("paid_qty", 1)) if promo else 1
    qty_step = receive_qty
    sum_step = round(price * paid_qty, 2)

    new_qty = qty + int(delta) * qty_step
    new_sum = round(current_sum + int(delta) * sum_step, 2)

    if new_qty <= 0 or new_sum <= 0:
        delete_row("Кошик", row_index)
        sync_cart_promo_gifts(chat_id)
        show_cart(chat_id, callback_message)
        return

    update_cell("Кошик", row_index, 5, new_qty)
    update_cell("Кошик", row_index, 6, new_sum)
    update_cart_reminder_columns(row_index, updated_at=now_str(), reminder1="", reminder2="", reminder3="")
    sync_cart_promo_gifts(chat_id)

    show_cart(chat_id, callback_message)


def delete_cart_item(chat_id, row_index, callback_message=None):
    try:
        delete_row("Кошик", int(row_index))
        sync_cart_promo_gifts(chat_id)
        show_cart(chat_id, callback_message)
    except Exception:
        show_main_options(chat_id, "Не вдалося видалити товар. Спробуйте ще раз.", callback_message)


def start_order(chat_id):
    cart = get_user_cart(chat_id)

    if not cart:
        show_main_options(chat_id, "Ваш кошик порожній, немає що замовляти 😔")
        return

    USER_STATES[str(chat_id)] = {
        "step": "waiting_full_name",
        "full_name": "",
        "phone": "",
        "city": "",
        "delivery_point": "",
        "address": "",
        "need_contact": "Ні",
        "delivery_method": "",
        "payment_method": "",
        "comment": ""
    }

    send_flow_message(chat_id, "Введіть, будь ласка, Ваше ПІБ:")

def is_menu_or_catalog_text(text):
    text = str(text or "").strip()
    if not text:
        return False

    main_buttons = [
        "/start",
        "/myid",
        "⬅️ Назад",
        "📦 Каталог",
        "🔥 Акції",
        "🛒 Кошик",
        "📦 Мої замовлення",
        "🎁 Мої бонуси",
        "👥 Реферальна програма",
        "📞 Зв’язатися з менеджером",
        "📞 Оформити через менеджера",
        "🚚 Доставка і оплата",
        "👑 Кабінет",
    ]

    if text in main_buttons:
        return True

    # Кнопки каталогу часто починаються з emoji або службових префіксів.
    # Їх не можна записувати як ПІБ або телефон у заявці.
    blocked_starts = ["📁", "📂", "▫️", "🏠", "💄", "💇", "🦷", "💆", "👙", "🌸", "💎", "👜", "✨"]
    return any(text.startswith(prefix) for prefix in blocked_starts)


def looks_like_phone(text):
    text = str(text or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return len(digits) >= 7


def looks_like_name(text):
    text = str(text or "").strip()
    if len(text) < 2:
        return False
    if is_menu_or_catalog_text(text):
        return False
    digits = sum(1 for ch in text if ch.isdigit())
    letters = sum(1 for ch in text if ch.isalpha())
    return letters >= 2 and digits == 0

def normalize_phone_number(value):
    """
    Нормалізує номер телефону з Telegram contact або з ручного введення.
    Telegram може передати номер без "+", тому для українських номерів додаємо "+".
    """
    phone = str(value or "").strip().replace(" ", "")
    if phone.startswith("00"):
        phone = "+" + phone[2:]
    if phone and not phone.startswith("+") and phone.startswith("380"):
        phone = "+" + phone
    return phone


def phone_request_keyboard():
    """
    Reply-кнопка Telegram, яка дозволяє клієнту поділитися номером одним кліком.
    Telegram не дає номер автоматично без згоди клієнта, тому потрібна саме така кнопка.
    """
    return {
        "keyboard": [
            [{"text": "📱 Поділитися номером", "request_contact": True}],
            [{"text": "⬅️ Назад"}]
        ],
        "resize_keyboard": True,
        "one_time_keyboard": True
    }


def ask_phone_via_contact_button(chat_id, text=None):
    text = text or (
        "📱 Натисніть, будь ласка, кнопку <b>«Поділитися номером»</b> нижче.\n\n"
        "Так менеджер зможе швидше зв’язатися з Вами 💛"
    )
    send_flow_message(chat_id, text, phone_request_keyboard())


def ask_delivery_method_after_phone(chat_id):
    remove_reply_keyboard(chat_id)
    keyboard = {
        "inline_keyboard": [
            [inline_button("🚚 Нова пошта", "delivery_np")],
            [inline_button("📦 Укрпошта", "delivery_ukr")]
        ]
    }
    send_flow_message(chat_id, "Оберіть, будь ласка, спосіб доставки:", keyboard)


def handle_shared_contact(chat_id, message, user):
    """
    Обробляє кнопку Telegram «Поділитися номером».
    Працює для:
    - заявки менеджеру;
    - оформлення замовлення.
    Якщо клієнт натиснув кнопку, номер підтягується автоматично і не треба вводити вручну.
    """
    contact = message.get("contact") if isinstance(message, dict) else None
    if not contact:
        return False

    phone = normalize_phone_number(contact.get("phone_number", ""))
    if not phone:
        send_flow_message(chat_id, "Не вдалося отримати номер телефону. Спробуйте, будь ласка, ще раз.", phone_request_keyboard())
        return True

    state = USER_STATES.get(str(chat_id), {})
    step = state.get("step", "")

    # Якщо це заявка на зв’язок з менеджером.
    if step == "contact_waiting_phone":
        state["contact_phone"] = phone
        finish_contact_request(chat_id, user, state)
        USER_STATES.pop(str(chat_id), None)
        return True

    # Якщо це оформлення замовлення.
    if step == "waiting_phone":
        state["phone"] = phone
        state["step"] = "waiting_delivery"
        USER_STATES[str(chat_id)] = state
        ask_delivery_method_after_phone(chat_id)
        return True

    send_service_message(
        chat_id,
        "Номер отримано ✅\n\nОберіть, будь ласка, що хочете зробити далі:",
        main_menu_inline(is_admin(chat_id))
    )
    return True


def handle_contact_state(chat_id, text, user):
    state = USER_STATES.get(str(chat_id))

    if not state:
        return False

    step = state.get("step")
    if step not in ["contact_waiting_full_name", "contact_waiting_phone"]:
        return False

    # Якщо людина під час заявки натиснула кнопку меню/каталогу —
    # не записуємо це як ПІБ або телефон, а скасовуємо заявку і даємо обробити кнопку далі.
    if is_menu_or_catalog_text(text):
        USER_STATES.pop(str(chat_id), None)
        return False

    if step == "contact_waiting_full_name":
        if not looks_like_name(text):
            send_flow_message(chat_id, "Введіть, будь ласка, Ваше ПІБ текстом. Наприклад: Іваненко Іван")
            return True

        state["contact_full_name"] = text.strip()
        state["step"] = "contact_waiting_phone"
        USER_STATES[str(chat_id)] = state
        ask_phone_via_contact_button(
            chat_id,
            "📱 Натисніть кнопку <b>«Поділитися номером»</b> нижче, щоб менеджер міг з Вами зв’язатися."
        )
        return True

    if step == "contact_waiting_phone":
        if not looks_like_phone(text):
            ask_phone_via_contact_button(
                chat_id,
                "📱 Натисніть кнопку <b>«Поділитися номером»</b> нижче.\n\n"
                "Так номер підтягнеться автоматично, без ручного введення."
            )
            return True

        # Запасний варіант: якщо клієнт все ж написав номер вручну.
        state["contact_phone"] = normalize_phone_number(text)
        finish_contact_request(chat_id, user, state)
        USER_STATES.pop(str(chat_id), None)
        return True

    return False


def handle_order_state(chat_id, text, user):
    state = USER_STATES.get(str(chat_id))

    if not state:
        return False

    step = state.get("step")

    if step == "waiting_full_name":
        state["full_name"] = text.strip()
        state["step"] = "waiting_phone"
        USER_STATES[str(chat_id)] = state
        ask_phone_via_contact_button(
            chat_id,
            "📱 Натисніть кнопку <b>«Поділитися номером»</b> нижче, щоб ми підтягнули Ваш номер для замовлення."
        )
        return True

    if step == "waiting_phone":
        if not looks_like_phone(text):
            ask_phone_via_contact_button(
                chat_id,
                "📱 Натисніть кнопку <b>«Поділитися номером»</b> нижче.\n\n"
                "Так номер підтягнеться автоматично, без ручного введення."
            )
            return True

        # Запасний варіант: якщо клієнт все ж написав номер вручну.
        state["phone"] = normalize_phone_number(text)
        state["step"] = "waiting_delivery"
        USER_STATES[str(chat_id)] = state

        ask_delivery_method_after_phone(chat_id)
        return True

    if step == "waiting_city":
        state["city"] = text.strip()
        delivery_method = state.get("delivery_method", "")

        if delivery_method == "Нова пошта":
            state["step"] = "waiting_np_branch"
            send_flow_message(chat_id, "Введіть, будь ласка, номер або адресу відділення Нової пошти:")
        elif delivery_method == "Укрпошта":
            state["step"] = "waiting_ukrposhta_index"
            send_flow_message(chat_id, "Введіть, будь ласка, індекс Укрпошти:")
        else:
            state["step"] = "waiting_delivery"
            keyboard = {
                "inline_keyboard": [
                    [inline_button("🚚 Нова пошта", "delivery_np")],
                    [inline_button("📦 Укрпошта", "delivery_ukr")]
                ]
            }
            send_flow_message(chat_id, "Оберіть, будь ласка, спосіб доставки:", keyboard)

        USER_STATES[str(chat_id)] = state
        return True

    if step == "waiting_np_branch":
        state["delivery_point"] = text.strip()
        city = state.get("city", "")
        state["address"] = f"Місто: {city}; Відділення Нової пошти: {state['delivery_point']}"
        state["step"] = "waiting_payment"
        USER_STATES[str(chat_id)] = state
        ask_payment_method(chat_id)
        return True

    if step == "waiting_ukrposhta_index":
        state["delivery_point"] = text.strip()
        city = state.get("city", "")
        state["address"] = f"Місто: {city}; Індекс Укрпошти: {state['delivery_point']}"
        state["step"] = "waiting_payment"
        USER_STATES[str(chat_id)] = state
        ask_payment_method(chat_id)
        return True

    if step == "waiting_comment":
        state["comment"] = text.strip()
        state["step"] = "waiting_free_delivery_decision"
        USER_STATES[str(chat_id)] = state
        ask_free_delivery_offer(chat_id)
        return True

    return False

def ask_payment_method(chat_id, callback_message=None):
    keyboard = {
        "inline_keyboard": [
            [inline_button("💳 Оплата за реквізитами IBAN", "payment_iban")],
            [inline_button("📦 Накладений платіж", "payment_cod")]
        ]
    }

    text = "Оберіть, будь ласка, спосіб оплати:"

    if callback_message:
        edit_flow_message(chat_id, callback_message["message_id"], text, keyboard)
    else:
        send_flow_message(chat_id, text, keyboard)


def ask_need_contact(chat_id, callback_message=None):
    state = USER_STATES.get(str(chat_id), {})
    state["step"] = "waiting_need_contact"
    USER_STATES[str(chat_id)] = state

    text = "Чи бажаєте, щоб з Вами зв’язались для уточнення деталей замовлення?"
    keyboard = {
        "inline_keyboard": [
            [inline_button("✅ Так, зв’яжіться зі мною", "need_contact_yes")],
            [inline_button("❌ Ні, не потрібно", "need_contact_no")]
        ]
    }

    if callback_message:
        edit_flow_message(chat_id, callback_message["message_id"], text, keyboard)
    else:
        send_flow_message(chat_id, text, keyboard)


def finish_order(chat_id, user, need_contact, callback_message=None):
    clear_flow_messages(chat_id)
    cart = get_user_cart(chat_id)

    if not cart:
        USER_STATES.pop(str(chat_id), None)
        show_main_options(chat_id, "Кошик порожній, немає що замовляти 😔", callback_message)
        return

    state = USER_STATES.get(str(chat_id), {})
    totals = calculate_cart_totals(chat_id)
    subtotal = totals["subtotal"]
    discount_percent = totals["discount_percent"]
    discount_amount = totals["discount_amount"]
    bonus_used = totals.get("bonus_used", 0)
    total = totals["total"]

    products_text = []

    for item in cart:
        products_text.append(format_cart_item_for_order(item))

    order_date = current_time().strftime("%d.%m.%Y %H:%M")
    full_name = state.get("full_name", "")
    phone = state.get("phone", "")
    address = state.get("address", "")
    delivery_method = state.get("delivery_method", "")
    payment_method = state.get("payment_method", "")
    comment = state.get("comment", "")
    products_joined = ", ".join(products_text)

    extra_notes = []
    if discount_percent:
        extra_notes.append(f"Знижка застосована: -{int(discount_percent)}% ({discount_amount} грн)")
    if bonus_used:
        extra_notes.append(f"Бонуси списано: {bonus_used} грн (тільки з неакційних товарів)")

    if extra_notes:
        comment_for_sheet = (comment + "\n" if comment else "") + "\n".join(extra_notes)
    else:
        comment_for_sheet = comment

    append_row("Замовлення", [
        order_date,
        chat_id,
        full_name,
        phone,
        address,
        delivery_method,
        payment_method,
        products_joined,
        total,
        need_contact,
        comment_for_sheet,
        "Очікується оплата" if payment_method == "Оплата за реквізитами IBAN" else "Нове"
    ])

    order_row_index = ""
    try:
        order_rows = get_values("Замовлення")
        order_row_index = len(order_rows)
    except Exception:
        order_row_index = ""

    if bonus_used:
        spend_bonuses(chat_id, bonus_used, order_row_index)

    clear_user_cart(chat_id)
    USER_STATES.pop(str(chat_id), None)

    notify_admin(
        full_name=full_name,
        phone=phone,
        address=address,
        delivery_method=delivery_method,
        payment_method=payment_method,
        comment=comment_for_sheet,
        products=products_joined,
        total=total,
        need_contact=need_contact,
        telegram_id=chat_id
    )

    order_status = "Очікується оплата" if payment_method == "Оплата за реквізитами IBAN" else "Нове"

    final_text = (
        "✅ <b>Дякуємо за замовлення!</b>\n\n"
        f"Ваше замовлення прийнято. Статус: <b>{order_status}</b>.\n\n"
        f"🛍 Сума товарів: <b>{subtotal} грн</b>\n"
    )

    if discount_percent:
        final_text += (
            f"🎁 Знижка: <b>-{int(discount_percent)}%</b>\n"
            f"💸 Сума знижки: <b>{discount_amount} грн</b>\n"
        )

    if bonus_used:
        final_text += f"🎁 Списано бонусів: <b>{bonus_used} грн</b> <i>(тільки з неакційних товарів)</i>\n"

    final_text += f"💰 Вартість Вашого замовлення за товар: <b>{total} грн</b>\n"
    final_text += f"{delivery_note_for_client(delivery_method, total)}\n\n"

    if payment_method == "Оплата за реквізитами IBAN":
        payment_details = get_setting_value("IBAN") or get_setting_value("Реквізити для оплати")

        if payment_details:
            final_text += (
                "💳 <b>Оплата за реквізитами IBAN:</b>\n"
                f"{payment_details}\n\n"
                "Після оплати надішліть, будь ласка, квитанцію сюди в бот — фото або файл 🧾\n\n"
            )
        else:
            final_text += (
                "💳 Ви обрали оплату за реквізитами IBAN.\n"
                "Менеджер надішле реквізити IBAN для оплати 💛\n"
                "Після оплати надішліть, будь ласка, квитанцію сюди в бот — фото або файл 🧾\n\n"
            )

    if payment_method == "Накладений платіж":
        final_text += "📦 Ви обрали накладений платіж. Оплата буде при отриманні.\n\n"

    final_text += (
        "🎁 Після успішного завершення замовлення Вам буде нараховано "
        f"<b>{int(PURCHASE_BONUS_PERCENT)}%</b> бонусами від суми покупки.\n"
        f"Бонуси діятимуть <b>{BONUS_VALID_DAYS} днів</b> 💛"
    )

    if payment_method == "Оплата за реквізитами IBAN":
        USER_STATES[str(chat_id)] = {"step": "waiting_payment_receipt"}

    keyboard = main_menu_inline(is_admin(chat_id))

    if callback_message:
        update_service_message(chat_id, callback_message, final_text, keyboard, clear_products=True)
    else:
        send_service_message(chat_id, final_text, keyboard, clear_products=True)

def notify_admin(full_name, phone, address, delivery_method, payment_method, comment, products, total, need_contact, telegram_id):
    text = (
        "🔔 <b>Нове замовлення!</b>\n\n"
        f"<b>ПІБ:</b> {full_name}\n"
        f"<b>Телефон:</b> {phone}\n"
        f"<b>Дані доставки:</b> {address}\n"
        f"<b>Доставка:</b> {delivery_method}\n"
        f"<b>Оплата:</b> {payment_method}\n"
        f"<b>Коментар:</b> {comment or '—'}\n"
        f"<b>Товари:</b> {products}\n"
        f"<b>Сума:</b> {total} грн\n"
        f"<b>Потрібно зв’язатись:</b> {need_contact}\n"
        f"<b>Telegram ID клієнта:</b> {telegram_id}"
    )

    for admin_id in get_admin_ids():
        send_message(admin_id, text)


def get_setting_value(param_name):
    try:
        settings = get_cached_records("Налаштування")

        for row in settings:
            param = str(row.get("Параметр", "")).strip().lower()
            value = str(row.get("Значення", "")).strip()

            if param == str(param_name).strip().lower():
                return value

    except Exception as e:
        print("get_setting_value error:", e)

    return ""



def show_my_orders(chat_id, callback_message=None):
    orders = get_orders_with_rows()

    my_orders = [
        order for order in orders
        if str(order.get("Telegram ID")) == str(chat_id)
    ]

    if not my_orders:
        update_service_message(
            chat_id,
            callback_message,
            "📦 У Вас поки немає замовлень.",
            back_to_main_inline()
        )
        return

    text = "📦 <b>Мої замовлення</b>\n\n"

    for idx, order in enumerate(my_orders[-10:], start=1):
        text += (
            f"<b>{idx}. Замовлення від {safe_text(order.get('Дата'))}</b>\n"
            f"🛍 Товари: {safe_text(order.get('Товари'))}\n"
            f"💰 Сума: <b>{safe_text(order.get('Сума'), '0')} грн</b>\n"
            f"🚚 Доставка: {safe_text(order.get('Спосіб доставки'))}\n"
            f"💳 Оплата: {safe_text(order.get('Спосіб оплати'))}\n"
            f"📌 Статус: <b>{safe_text(order.get('Статус'), 'Нове')}</b>\n\n"
        )

    keyboard = {"inline_keyboard": [[inline_button("⬅️ Назад у меню", "back_main")]]}
    update_service_message(chat_id, callback_message, text, keyboard)



def manager_request_cart_summary(chat_id):
    """
    Формує короткий підсумок кошика для адміна, коли клієнт просить оформити через менеджера.
    Показує товари, суму, бонуси та скільки бонусів можна списати.
    """
    try:
        cart = get_user_cart(chat_id)
    except Exception as e:
        print("manager_request_cart_summary cart error:", e)
        cart = []

    if not cart:
        try:
            balance = get_available_bonus_balance(chat_id)
        except Exception:
            balance = 0

        return (
            "\n🛒 <b>Кошик клієнта:</b>\n"
            "Поки порожній або не вдалося підтягнути товари.\n"
            f"🎁 <b>Доступно бонусів:</b> {balance}\n"
        )

    text = "\n🛒 <b>Товари в кошику:</b>\n"

    for item in cart:
        try:
            text += format_cart_item_line(item)
        except Exception:
            name = safe_text(item.get("Назва товару") or item.get("name") or "Товар")
            qty = safe_text(item.get("Кількість") or item.get("qty") or "1")
            summa = safe_text(item.get("Сума") or item.get("sum") or "0")
            text += f"• {name} — {qty} шт. = <b>{summa} грн</b>\n"

    try:
        totals = calculate_cart_totals(chat_id, use_bonuses=False)
    except Exception as e:
        print("manager_request_cart_summary totals error:", e)
        totals = {
            "subtotal": 0,
            "bonus_eligible_after_discount": 0,
            "available_bonuses": 0,
            "max_bonus_to_use": 0,
            "total": 0
        }

    text += (
        f"\n💰 <b>Сума кошика:</b> {totals.get('subtotal', 0)} грн\n"
        f"🎁 <b>Доступно бонусів:</b> {totals.get('available_bonuses', 0)}\n"
        f"✅ <b>Сума неакційних товарів для бонусів:</b> {totals.get('bonus_eligible_after_discount', 0)} грн\n"
        f"💳 <b>Можна списати до:</b> {totals.get('max_bonus_to_use', 0)} бонусів\n"
    )

    return text


def contact_manager(chat_id, user, source="manual", product_id=""):
    product_name = ""
    if product_id:
        try:
            product = get_product_by_id(product_id)
            product_name = safe_text(product.get("Назва товару") if product else "", "")
        except Exception as e:
            print("contact_manager product lookup error:", e)
            product_name = ""

    USER_STATES[str(chat_id)] = {
        "step": "contact_waiting_full_name",
        "contact_full_name": "",
        "contact_phone": "",
        "contact_source": source,
        "contact_product_id": str(product_id or ""),
        "contact_product_name": product_name
    }

    if source == "cart_reminder":
        send_flow_message(
            chat_id,
            "📞 Залиште, будь ласка, Ваше ПІБ — менеджер зв’яжеться з Вами та допоможе з товарами у кошику:"
        )
    elif source == "product_card":
        product_part = f" щодо товару <b>{product_name}</b>" if product_name else " щодо цього товару"
        send_flow_message(
            chat_id,
            f"📞 Залиште, будь ласка, Ваше ПІБ — менеджер зв’яжеться з Вами{product_part} та допоможе оформити замовлення:"
        )
    elif source == "manager_order":
        send_flow_message(
            chat_id,
            "📞 Залиште, будь ласка, Ваше ПІБ — менеджер зв’яжеться з Вами та оформить замовлення:"
        )
    else:
        send_flow_message(chat_id, "Введіть, будь ласка, Ваше ПІБ:")


def finish_contact_request(chat_id, user, state):
    clear_flow_messages(chat_id)
    remove_reply_keyboard(chat_id)
    request_date = current_time().strftime("%d.%m.%Y %H:%M")
    full_name = state.get("contact_full_name", "")
    phone = state.get("contact_phone", "")

    append_contact_request([
        request_date,
        chat_id,
        full_name,
        phone,
        "Нова"
    ])

    send_service_message(
        chat_id,
        "✅ Дякуємо! Заявку передано менеджеру. Ми скоро зв’яжемося з Вами 💛",
        main_menu_inline(is_admin(chat_id))
    )

    source = state.get("contact_source", "manual")
    if source == "cart_reminder":
        source_text = "Кошик / нагадування"
    elif source == "product_card":
        source_text = "Картка товару"
    elif source == "manager_order":
        source_text = "Оформити через менеджера"
    else:
        source_text = "Звичайна заявка"

    product_name = state.get("contact_product_name", "")
    product_id = state.get("contact_product_id", "")
    product_line = ""
    if product_name or product_id:
        product_line = f"<b>Товар:</b> {product_name or '—'}"
        if product_id:
            product_line += f" / ID: {product_id}"
        product_line += "\n"

    cart_line = ""
    if source in ["manager_order", "cart_reminder"]:
        cart_line = manager_request_cart_summary(chat_id)

    admin_text = (
        "📞 <b>Нова заявка на зв’язок</b>\n\n"
        f"<b>Джерело:</b> {source_text}\n"
        f"{product_line}"
        f"<b>ПІБ:</b> {full_name}\n"
        f"<b>Телефон:</b> {phone}\n"
        f"<b>Telegram ID:</b> {chat_id}\n"
        f"{cart_line}"
    )
    for admin_id in get_admin_ids():
        send_message(admin_id, admin_text)

def show_delivery_payment(chat_id, callback_message=None):
    settings = get_cached_records("Налаштування")

    if not settings:
        update_service_message(chat_id, callback_message, "Інформацію про доставку й оплату ще не додано.", back_to_main_inline())
        return

    text = "🚚 <b>Доставка і оплата</b>\n\n"

    for row in settings:
        param = row.get("Параметр")
        value = row.get("Значення")
        text += f"<b>{param}:</b>\n{value}\n\n"

    update_service_message(chat_id, callback_message, text, back_to_main_inline())


# =========================
# ADMIN CABINET
# =========================

def get_status_stats():
    orders = get_orders_with_rows()

    stats = {
        "Нове": {"count": 0, "sum": 0},
        "Очікується оплата": {"count": 0, "sum": 0},
        "В обробці": {"count": 0, "sum": 0},
        "Відправлено": {"count": 0, "sum": 0},
        "Завершено": {"count": 0, "sum": 0},
        "Скасовано": {"count": 0, "sum": 0},
    }

    for order in orders:
        status = str(order.get("Статус")).strip()

        if status == "Опрацьовано":
            status = "В обробці"

        if status not in stats:
            status = "Нове"

        try:
            value = float(order.get("Сума") or 0)
        except:
            value = 0

        stats[status]["count"] += 1
        stats[status]["sum"] += value

    return stats

def show_admin_cabinet(chat_id, callback_message=None):
    if not is_admin(chat_id):
        send_message(chat_id, "Цей розділ доступний тільки адміністратору.", main_menu(False))
        return

    USER_STATES.pop(str(chat_id), None)

    stats = get_status_stats()
    clients_block = clients_stats_text()
    referral_stats = get_admin_referral_stats()

    text = (
        "👑 <b>Кабінет</b>\n\n"
        f"🆕 Нові: <b>{stats['Нове']['count']}</b> / {stats['Нове']['sum']} грн\n"
        f"💳 Очікується оплата: <b>{stats['Очікується оплата']['count']}</b> / {stats['Очікується оплата']['sum']} грн\n"
        f"🟡 В обробці: <b>{stats['В обробці']['count']}</b> / {stats['В обробці']['sum']} грн\n"
        f"🚚 Відправлено: <b>{stats['Відправлено']['count']}</b> / {stats['Відправлено']['sum']} грн\n"
        f"✅ Завершено: <b>{stats['Завершено']['count']}</b> / {stats['Завершено']['sum']} грн\n"
        f"❌ Скасовано: <b>{stats['Скасовано']['count']}</b> / {stats['Скасовано']['sum']} грн\n\n"
        f"{clients_block}\n\n"
        "👥 <b>Реферальна програма</b>\n"
        f"Запрошень: <b>{referral_stats['invited_total']}</b>\n"
        f"Успішних рефералів: <b>{referral_stats['successful']}</b>\n"
        f"Очікують першого замовлення: <b>{referral_stats['waiting']}</b>\n"
        f"Реферальних бонусів нараховано: <b>{referral_stats['bonus_total']}</b>"
    )

    keyboard = {
        "inline_keyboard": [
            [inline_button("🆕 Нові", "admin_status_new")],
            [inline_button("💳 Очікується оплата", "admin_status_pay")],
            [inline_button("🟡 В обробці", "admin_status_work")],
            [inline_button("🚚 Відправлено", "admin_status_sent")],
            [inline_button("✅ Завершено", "admin_status_done")],
            [inline_button("❌ Скасовано", "admin_status_cancel")],
            [inline_button("📞 Заявки на зв’язок", "contact_requests")],
            [inline_button("👥 Клієнти", "clients_stats")],
            [inline_button("👥 Рефералка", "admin_referrals")],
            [inline_button("➕ Створити замовлення клієнту", "admin_create_order")],
            [inline_button("📊 Підсумок за сьогодні", "summary_today")],
            [inline_button("📊 Підсумок за місяць", "summary_month")],
            [inline_button("🔍 Пошук", "admin_search")],
            [inline_button("📅 Фільтр за датою", "admin_date_filter")],
            [inline_button("💰 Сума замовлень", "admin_orders_sum")]
        ]
    }

    if callback_message:
        edit_message(chat_id, callback_message["message_id"], text, keyboard)
    else:
        send_message(chat_id, text, keyboard)

def status_emoji(status):
    if status == "Нове":
        return "🆕"
    if status == "Очікується оплата":
        return "💳"
    if status == "В обробці":
        return "🟡"
    if status == "Відправлено":
        return "🚚"
    if status == "Завершено":
        return "✅"
    if status == "Скасовано":
        return "❌"
    return "📦"

def order_details_text(order, title="Замовлення"):
    return (
        f"📦 <b>{title}</b>\n\n"
        f"<b>Дата:</b> {order.get('Дата')}\n"
        f"<b>ПІБ:</b> {order.get('ПІБ')}\n"
        f"<b>Телефон:</b> {order.get('Телефон')}\n"
        f"<b>Дані доставки:</b> {order.get('Адреса доставки')}\n"
        f"<b>Доставка:</b> {order.get('Спосіб доставки') or '—'}\n"
        f"<b>Оплата:</b> {order.get('Спосіб оплати') or '—'}\n"
        f"<b>Коментар:</b> {order.get('Коментар') or '—'}\n"
        f"<b>Товари:</b> {order.get('Товари')}\n"
        f"<b>Сума:</b> {order.get('Сума')} грн\n"
        f"<b>Потрібно зв’язатись:</b> {order.get('Потрібно зв’язатись')}\n"
        f"<b>Статус:</b> {order.get('Статус')}"
    )

def order_status_keyboard(row_index, extra_back="admin_back"):
    return {
        "inline_keyboard": [
            [inline_button("💳 Очікується оплата", f"set_status_{row_index}_pay")],
            [inline_button("🟡 В обробці", f"set_status_{row_index}_work")],
            [inline_button("🚚 Відправлено", f"set_status_{row_index}_sent")],
            [inline_button("✅ Завершено", f"set_status_{row_index}_done")],
            [inline_button("❌ Скасовано", f"set_status_{row_index}_cancel")],
            [inline_button("⬅️ Назад у кабінет", extra_back)]
        ]
    }

def show_orders_by_status(chat_id, status, callback_message=None):
    if not is_admin(chat_id):
        return

    USER_STATES.pop(str(chat_id), None)

    orders = get_orders_with_rows()
    filtered = [o for o in orders if str(o.get("Статус")).strip() == status]

    if not filtered:
        text = f"{status_emoji(status)} Замовлень зі статусом <b>{status}</b> немає."
        keyboard = {"inline_keyboard": [[inline_button("⬅️ Назад у кабінет", "admin_back")]]}

        if callback_message:
            edit_message(chat_id, callback_message["message_id"], text, keyboard)
        else:
            send_message(chat_id, text, keyboard)
        return

    header = f"{status_emoji(status)} <b>Замовлення: {status}</b>\n\nУсього у цьому статусі: <b>{len(filtered)}</b>"
    header_keyboard = {"inline_keyboard": [[inline_button("⬅️ Назад у кабінет", "admin_back")]]}

    if callback_message:
        edit_message(chat_id, callback_message["message_id"], header, header_keyboard)
    else:
        send_message(chat_id, header, header_keyboard)

    for idx, order in enumerate(filtered, start=1):
        text = order_details_text(order, f"{status_emoji(status)} Замовлення {idx} з {len(filtered)}: {status}")
        keyboard = order_status_keyboard(order.get("row_index"), f"admin_status_{ORDER_STATUS_TO_CODE.get(status, status)}")
        send_message(chat_id, text, keyboard)

def notify_client_order_sent(order):
    client_chat_id = order.get("Telegram ID") if order else ""
    if not client_chat_id:
        return

    try:
        total = float(order.get("Сума") or 0)
    except:
        total = 0

    text = (
        "🚚 <b>Ваше замовлення відправлено!</b>\n\n"
        "Дякуємо за замовлення 💛\n"
        f"💰 Вартість Вашого замовлення за товар: <b>{total} грн</b>\n"
        "🚚 Також враховуйте вартість доставки — вона нараховується за тарифами перевізника.\n\n"
        "Після отримання замовлення менеджер завершить його, і бонуси будуть нараховані автоматично 🎁"
    )

    send_message(client_chat_id, text)

def notify_client_status_change(client_chat_id, status):
    if not client_chat_id:
        return

    messages = {
        "Очікується оплата": "💳 Ваше замовлення очікує оплату. Після оплати надішліть, будь ласка, квитанцію сюди в бот 🧾",
        "В обробці": "🟡 Ваше замовлення вже в обробці. Дякуємо за очікування 💛",
        "Відправлено": "🚚 Ваше замовлення відправлено. Дякуємо за замовлення 💛",
        "Завершено": "✅ Ваше замовлення завершено. Дякуємо за покупку 💛",
        "Скасовано": "❌ Ваше замовлення скасовано. Якщо це помилка — напишіть нам 💛",
        "Нове": "🆕 Ваше замовлення прийнято 💛"
    }

    text = messages.get(status, f"📦 Статус Вашого замовлення змінено на: {status}")

    try:
        send_message(client_chat_id, text)
    except Exception as e:
        print("notify_client_status_change error:", e)

def set_order_status(chat_id, row_index, status, callback_message=None):
    if not is_admin(chat_id):
        return

    try:
        orders = get_orders_with_rows()
        target_order = None

        for order in orders:
            if str(order.get("row_index")) == str(row_index):
                target_order = order
                break

        client_chat_id = target_order.get("Telegram ID") if target_order else ""

        rows = get_values("Замовлення")
        status_col = get_order_status_col_index()
        update_cell("Замовлення", int(row_index), status_col, status)
        clear_cache("Замовлення")

        # Після оновлення статусу перечитуємо саме цей рядок без кешу.
        fresh_target_order = get_fresh_order_by_row_index(row_index)
        if fresh_target_order:
            target_order = fresh_target_order
            client_chat_id = target_order.get("Telegram ID")

        purchase_bonus_added = None
        referral_bonus_added = None

        if status == "Відправлено" and target_order:
            target_order["Статус"] = status
            notify_client_order_sent(target_order)
        elif status == "Завершено" and target_order:
            target_order["Статус"] = status

            # Якщо через кеш/зміну структури сума не підтягнулась,
            # беремо її напряму з рядка таблиці після оновлення статусу.
            if safe_float(target_order.get("Сума")) <= 0:
                try:
                    fresh_rows = get_values("Замовлення")
                    fresh_headers = fresh_rows[0] if fresh_rows else []
                    fresh_map = {
                        str(header).strip().lower(): idx
                        for idx, header in enumerate(fresh_headers)
                        if str(header).strip()
                    }
                    fresh_row = fresh_rows[int(row_index) - 1] if len(fresh_rows) >= int(row_index) else []
                    target_order["Сума"] = get_order_cell(fresh_row, fresh_map, "Сума", 8)
                    target_order["_raw_row"] = fresh_row
                except Exception as e:
                    print("refresh target_order sum error:", e)

            # Важливо: передаємо рядок замовлення та суму саме з вибраного замовлення.
            purchase_bonus_added = process_purchase_bonus_for_order(target_order)
            referral_bonus_added = process_referral_bonus_for_order(target_order)
            notify_client_status_change(client_chat_id, status)
        elif status in ["Скасовано", "Повернення"] and target_order:
            target_order["Статус"] = status
            cancel_purchase_bonus_for_order(target_order)
            cancel_referral_bonus_for_order(target_order)
            notify_client_status_change(client_chat_id, status)
        else:
            notify_client_status_change(client_chat_id, status)

        text = (
            f"{status_emoji(status)} <b>Статус змінено</b>\n\n"
            f"Новий статус замовлення: <b>{status}</b>\n"
            f"Клієнту надіслано сповіщення ✅"
        )

        if status == "Завершено":
            if purchase_bonus_added is True:
                bonus_amount_for_order = get_purchase_bonus_amount_for_order(row_index)
                if bonus_amount_for_order:
                    text += f"\n🎁 Бонус за покупку: <b>нараховано {bonus_amount_for_order} бонусів</b> ✅"
                else:
                    text += "\n🎁 Бонус за покупку: <b>нараховано</b> ✅"
            else:
                text += "\n🎁 Бонус за покупку: <b>не нараховано</b> ⚠️"
                text += (
                    f"\n<i>Перевірка: Telegram ID = {safe_text(target_order.get('Telegram ID') if target_order else '')}, "
                    f"сума = {safe_text(target_order.get('Сума') if target_order else '')} грн.</i>"
                )
                text += "\n<i>Можливі причини: бонус уже був нарахований, сума замовлення 0 грн або немає Telegram ID.</i>"

            if referral_bonus_added is True:
                text += "\n👥 Реферальний бонус: <b>нараховано</b> ✅"

        keyboard = {
            "inline_keyboard": [
                [inline_button("🔄 Оновити цей статус", f"admin_status_{ORDER_STATUS_TO_CODE.get(status, status)}")],
                [inline_button("⬅️ Назад у кабінет", "admin_back")]
            ]
        }

        if callback_message:
            edit_message(chat_id, callback_message["message_id"], text, keyboard)
        else:
            send_message(chat_id, text, keyboard)

    except Exception as e:
        print("set_order_status error:", e, "row_index:", row_index, "status:", status)
        send_message(chat_id, "Не вдалося змінити статус. Спробуйте ще раз.", main_menu(True))


def show_admin_orders_sum(chat_id, callback_message=None):
    if not is_admin(chat_id):
        return

    USER_STATES.pop(str(chat_id), None)

    stats = get_status_stats()
    total_all = sum(v["sum"] for v in stats.values())

    text = (
        "💰 <b>Сума замовлень</b>\n\n"
        f"🆕 Нові: <b>{stats['Нове']['sum']} грн</b>\n"
        f"💳 Очікується оплата: <b>{stats['Очікується оплата']['sum']} грн</b>\n"
        f"🟡 В обробці: <b>{stats['В обробці']['sum']} грн</b>\n"
        f"🚚 Відправлено: <b>{stats['Відправлено']['sum']} грн</b>\n"
        f"✅ Завершено: <b>{stats['Завершено']['sum']} грн</b>\n"
        f"❌ Скасовано: <b>{stats['Скасовано']['sum']} грн</b>\n\n"
        f"📦 Усі разом: <b>{total_all} грн</b>"
    )

    keyboard = {"inline_keyboard": [[inline_button("⬅️ Назад у кабінет", "admin_back")]]}

    if callback_message:
        edit_message(chat_id, callback_message["message_id"], text, keyboard)
    else:
        send_message(chat_id, text, keyboard)


# =========================
# ADMIN MANUAL ORDER CREATION
# =========================

def start_admin_create_order(chat_id, callback_message=None):
    if not is_admin(chat_id):
        return

    USER_STATES[str(chat_id)] = {
        "step": "admin_order_client_id",
        "admin_order": {}
    }

    text = (
        "➕ <b>Створення замовлення клієнту</b>\n\n"
        "Введіть Telegram ID клієнта.\n"
        "Він потрібен, щоб бот правильно підтягнув бонуси, знижки та історію клієнта."
    )

    if callback_message:
        edit_message(chat_id, callback_message["message_id"], text)
    else:
        send_message(chat_id, text)


def parse_admin_product_lines(text):
    """
    Формат для адміна:
    123 x 2
    456*1
    789 3

    Для звичайних товарів кількість = кількість штук.
    Для акцій 1=2 / 1+1=3 кількість = кількість акційних наборів.
    Наприклад: 1 набір 1=2 дасть 2 шт. у замовленні з оплатою за 1 шт.
    """
    raw = str(text or "").replace(",", "\n").replace(";", "\n")
    result = []

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue

        clean = line.lower().replace("×", "x").replace("*", "x")
        parts = clean.split("x")

        if len(parts) >= 2:
            product_id = parts[0].strip()
            qty = safe_int(parts[1].strip(), 1)
        else:
            bits = clean.split()
            product_id = bits[0].strip() if bits else ""
            qty = safe_int(bits[1], 1) if len(bits) > 1 else 1

        if product_id and qty > 0:
            result.append({"product_id": product_id, "qty": qty})

    return result


def build_admin_order_preview(client_id, items, use_bonuses=False):
    products_lines = []
    subtotal = 0
    bonus_eligible_subtotal = 0
    errors = []

    for item in items:
        product_id = str(item.get("product_id", "")).strip()
        packs_qty = safe_int(item.get("qty"), 1)
        product = get_product_by_id(product_id)

        if not product:
            errors.append(f"ID {product_id}: товар не знайдено")
            continue

        name = safe_text(product.get("Назва товару"), "Товар")
        price = safe_float(get_active_sale_price(product) or product.get("Ціна") or 0)
        promo = get_product_promo_deal(product)
        product_is_bonus_eligible = is_bonus_eligible_product(product)

        if promo:
            receive_qty = int(promo.get("receive_qty", 1))
            paid_qty = int(promo.get("paid_qty", 1))
            actual_qty = packs_qty * receive_qty
            paid_units = packs_qty * paid_qty
            line_sum = round(price * paid_units, 2)
            label = promo.get("label", "Акція")
            products_lines.append(f"{name} ({label}) x{actual_qty} шт. / оплата за {paid_units} шт. = {line_sum} грн")
        else:
            actual_qty = packs_qty
            line_sum = round(price * packs_qty, 2)
            products_lines.append(f"{name} x{actual_qty} шт. = {line_sum} грн")

        subtotal = round(subtotal + line_sum, 2)

        if product_is_bonus_eligible:
            bonus_eligible_subtotal = round(bonus_eligible_subtotal + line_sum, 2)

        gift_config = get_promo_gift_config(product)
        if gift_config:
            gift_qty = actual_qty
            gift_sum = round(gift_qty * safe_float(gift_config.get("gift_price"), 1), 2)
            gift_name = safe_text(gift_config.get("gift_name"), "Подарунок за акцією")
            sale_label = safe_text(gift_config.get("sale_label"), "Акція")
            products_lines.append(f"{gift_name} ({sale_label}) x{gift_qty} шт. = {gift_sum} грн")
            subtotal = round(subtotal + gift_sum, 2)
            # Подарунки/акційні товари за 1 грн не входять у суму для списання бонусів.

    discount_percent = get_client_discount_percent(client_id)
    discount_amount = round(subtotal * discount_percent / 100, 2) if discount_percent else 0
    after_discount = round(subtotal - discount_amount, 2)

    bonus_eligible_discount_amount = round(bonus_eligible_subtotal * discount_percent / 100, 2) if discount_percent else 0
    bonus_eligible_after_discount = round(bonus_eligible_subtotal - bonus_eligible_discount_amount, 2)

    available_bonuses = get_available_bonus_balance(client_id)
    max_bonus_to_use = calculate_bonus_to_use(client_id, bonus_eligible_after_discount)
    bonus_used = max_bonus_to_use if use_bonuses else 0
    total = round(after_discount - bonus_used, 2)

    return {
        "products_lines": products_lines,
        "products_text": ", ".join(products_lines),
        "subtotal": subtotal,
        "discount_percent": discount_percent,
        "discount_amount": discount_amount,
        "after_discount": after_discount,
        "bonus_eligible_subtotal": bonus_eligible_subtotal,
        "bonus_eligible_discount_amount": bonus_eligible_discount_amount,
        "bonus_eligible_after_discount": bonus_eligible_after_discount,
        "available_bonuses": available_bonuses,
        "max_bonus_to_use": max_bonus_to_use,
        "bonus_used": bonus_used,
        "total": total,
        "errors": errors
    }

def admin_order_preview_text(state):
    order = state.get("admin_order", {})
    client_id = order.get("client_id", "")
    items = order.get("items", [])
    use_bonuses = bool(order.get("use_bonuses"))
    totals = build_admin_order_preview(client_id, items, use_bonuses)

    text = (
        "🧾 <b>Попередній розрахунок замовлення</b>\n\n"
        f"<b>Telegram ID клієнта:</b> <code>{client_id}</code>\n\n"
    )

    if totals.get("products_lines"):
        text += "<b>Товари:</b>\n"
        for line in totals["products_lines"]:
            text += f"• {line}\n"
    else:
        text += "Товари ще не додано.\n"

    if totals.get("errors"):
        text += "\n⚠️ <b>Помилки:</b>\n"
        for err in totals["errors"]:
            text += f"• {err}\n"

    text += f"\n💰 Сума товарів: <b>{totals['subtotal']} грн</b>"

    if totals["discount_percent"]:
        text += f"\n🎁 Знижка клієнта: <b>-{int(totals['discount_percent'])}%</b> ({totals['discount_amount']} грн)"

    text += (
        f"\n🎁 Доступно бонусів: <b>{totals['available_bonuses']}</b>"
        f"\n🧾 Сума неакційних товарів для бонусів: <b>{totals.get('bonus_eligible_subtotal', 0)} грн</b>"
        f"\n💰 Можна списати до <b>{totals['max_bonus_to_use']} грн</b>"
    )

    if totals.get('available_bonuses', 0) and totals.get('bonus_eligible_subtotal', 0) <= 0:
        text += "\n⚠️ У замовленні тільки акційні товари — бонуси списати не можна."

    if totals["bonus_used"]:
        text += f"\n✅ Буде списано бонусів: <b>{totals['bonus_used']} грн</b>"

    text += f"\n\n✅ До сплати за товари: <b>{totals['total']} грн</b>"
    return text


def admin_order_bonus_keyboard(state):
    order = state.get("admin_order", {})
    totals = build_admin_order_preview(order.get("client_id", ""), order.get("items", []), False)
    buttons = []

    if totals.get("max_bonus_to_use", 0) > 0:
        buttons.append([inline_button(f"🎁 Використати бонуси (-{totals['max_bonus_to_use']} грн)", "admin_order_bonus_yes")])
        buttons.append([inline_button("Без списання бонусів", "admin_order_bonus_no")])
    else:
        buttons.append([inline_button("Продовжити без бонусів", "admin_order_bonus_no")])

    buttons.append([inline_button("❌ Скасувати", "admin_back")])
    return {"inline_keyboard": buttons}


def ask_admin_order_delivery(chat_id, callback_message=None):
    state = USER_STATES.get(str(chat_id), {})
    state["step"] = "admin_order_delivery"
    USER_STATES[str(chat_id)] = state

    text = admin_order_preview_text(state) + "\n\nОберіть спосіб доставки:"
    keyboard = {
        "inline_keyboard": [
            [inline_button("🚚 Нова пошта", "admin_order_delivery_np")],
            [inline_button("📦 Укрпошта", "admin_order_delivery_ukr")],
            [inline_button("❌ Скасувати", "admin_back")]
        ]
    }

    if callback_message:
        edit_message(chat_id, callback_message["message_id"], text, keyboard)
    else:
        send_message(chat_id, text, keyboard)


def ask_admin_order_payment(chat_id, callback_message=None):
    state = USER_STATES.get(str(chat_id), {})
    state["step"] = "admin_order_payment"
    USER_STATES[str(chat_id)] = state

    text = "Оберіть спосіб оплати для замовлення:"
    keyboard = {
        "inline_keyboard": [
            [inline_button("💳 Оплата за реквізитами IBAN", "admin_order_payment_iban")],
            [inline_button("📦 Накладений платіж", "admin_order_payment_cod")],
            [inline_button("❌ Скасувати", "admin_back")]
        ]
    }

    if callback_message:
        edit_message(chat_id, callback_message["message_id"], text, keyboard)
    else:
        send_message(chat_id, text, keyboard)


def finish_admin_created_order(admin_chat_id, callback_message=None):
    if not is_admin(admin_chat_id):
        return

    state = USER_STATES.get(str(admin_chat_id), {})
    order = state.get("admin_order", {})
    client_id = str(order.get("client_id", "")).strip()
    items = order.get("items", [])
    use_bonuses = bool(order.get("use_bonuses"))
    totals = build_admin_order_preview(client_id, items, use_bonuses)

    if not client_id or not items or totals.get("errors"):
        send_message(admin_chat_id, "Не вдалося створити замовлення. Перевірте Telegram ID клієнта та товари.")
        return

    full_name = order.get("full_name", "")
    phone = order.get("phone", "")
    address = order.get("address", "")
    delivery_method = order.get("delivery_method", "")
    payment_method = order.get("payment_method", "")
    comment = order.get("comment", "")
    products_joined = totals.get("products_text", "")
    total = totals.get("total", 0)
    order_date = current_time().strftime("%d.%m.%Y %H:%M")

    extra_notes = [f"Замовлення створив адміністратор: {admin_chat_id}"]
    if totals.get("discount_percent"):
        extra_notes.append(f"Знижка застосована: -{int(totals['discount_percent'])}% ({totals['discount_amount']} грн)")
    if totals.get("bonus_used"):
        extra_notes.append(f"Бонуси списано: {totals['bonus_used']} грн (тільки з неакційних товарів)")

    comment_for_sheet = (comment + "\n" if comment else "") + "\n".join(extra_notes)
    status = "Очікується оплата" if payment_method == "Оплата за реквізитами IBAN" else "Нове"

    append_row("Замовлення", [
        order_date,
        client_id,
        full_name,
        phone,
        address,
        delivery_method,
        payment_method,
        products_joined,
        total,
        "Так",
        comment_for_sheet,
        status
    ])

    order_row_index = ""
    try:
        order_rows = get_values("Замовлення")
        order_row_index = len(order_rows)
    except Exception:
        order_row_index = ""

    if totals.get("bonus_used"):
        spend_bonuses(client_id, totals["bonus_used"], order_row_index, "Списання бонусів за замовлення, створене менеджером (тільки з неакційних товарів)")

    USER_STATES.pop(str(admin_chat_id), None)

    notify_admin(
        full_name=full_name,
        phone=phone,
        address=address,
        delivery_method=delivery_method,
        payment_method=payment_method,
        comment=comment_for_sheet,
        products=products_joined,
        total=total,
        need_contact="Так",
        telegram_id=client_id
    )

    client_text = (
        "✅ <b>Менеджер оформив для Вас замовлення</b>\n\n"
        f"<b>Товари:</b> {products_joined}\n"
        f"💰 До сплати за товари: <b>{total} грн</b>\n"
        f"🚚 Доставка: <b>{delivery_method}</b>\n"
        f"💳 Оплата: <b>{payment_method}</b>\n\n"
    )

    if totals.get("bonus_used"):
        client_text += f"🎁 Списано бонусів: <b>{totals['bonus_used']} грн</b> <i>(тільки з неакційних товарів)</i>\n"

    client_text += "Якщо потрібно щось уточнити — менеджер зв’яжеться з Вами 💛"

    if payment_method == "Оплата за реквізитами IBAN":
        payment_details = get_setting_value("IBAN") or get_setting_value("Реквізити для оплати")
        if payment_details:
            client_text += (
                "\n\n💳 <b>Реквізити для оплати:</b>\n"
                f"{payment_details}\n\n"
                "Після оплати надішліть, будь ласка, квитанцію сюди в бот 🧾"
            )
        USER_STATES[str(client_id)] = {"step": "waiting_payment_receipt"}

    try:
        send_message(client_id, client_text)
    except Exception as e:
        print("send admin-created order to client error:", e)

    text = (
        "✅ <b>Замовлення клієнту створено</b>\n\n"
        f"Клієнт: <code>{client_id}</code>\n"
        f"Сума: <b>{total} грн</b>\n"
        f"Статус: <b>{status}</b>\n"
        "Клієнту надіслано повідомлення в бот."
    )
    keyboard = {"inline_keyboard": [[inline_button("⬅️ Назад у кабінет", "admin_back")]]}

    if callback_message:
        edit_message(admin_chat_id, callback_message["message_id"], text, keyboard)
    else:
        send_message(admin_chat_id, text, keyboard)

def start_admin_search(chat_id, callback_message=None):
    if not is_admin(chat_id):
        return

    USER_STATES[str(chat_id)] = {"step": "admin_search"}

    text = (
        "🔍 <b>Пошук замовлення</b>\n\n"
        "Напишіть ПІБ, телефон або Telegram ID клієнта:"
    )

    if callback_message:
        edit_message(chat_id, callback_message["message_id"], text)
    else:
        send_message(chat_id, text)


def start_admin_date_filter(chat_id, callback_message=None):
    if not is_admin(chat_id):
        return

    USER_STATES[str(chat_id)] = {"step": "admin_date_filter"}

    text = (
        "📅 <b>Фільтр за датою</b>\n\n"
        "Введіть дату у форматі:\n"
        "<code>дд.мм.рррр</code>\n\n"
        "Наприклад: <code>22.05.2026</code>"
    )

    if callback_message:
        edit_message(chat_id, callback_message["message_id"], text)
    else:
        send_message(chat_id, text)


def format_orders_list(orders, title):
    if not orders:
        return f"{title}\n\nНічого не знайдено."

    text = f"{title}\n\n"

    for idx, order in enumerate(orders[-10:], start=1):
        text += (
            f"<b>{idx}. {safe_text(order.get('Дата'))}</b>\n"
            f"{safe_text(order.get('ПІБ'))} | {safe_text(order.get('Телефон'))}\n"
            f"{safe_text(order.get('Товари'))}\n"
            f"Сума: <b>{safe_text(order.get('Сума'), '0')} грн</b>\n"
            f"Статус: <b>{safe_text(order.get('Статус'), 'Нове')}</b>\n\n"
        )

    if len(orders) > 10:
        text += f"Показано останні 10 з {len(orders)} знайдених."

    return text


def handle_admin_state(chat_id, text):
    state = USER_STATES.get(str(chat_id))

    if not state:
        return False

    if not is_admin(chat_id):
        return False

    admin_cancel_texts = [
        "📦 Каталог", "🔥 Акції", "🛒 Кошик", "📦 Мої замовлення",
        "🎁 Мої бонуси", "👥 Реферальна програма", "📞 Зв’язатися з менеджером",
        "📞 Оформити через менеджера", "🚚 Доставка і оплата", "👑 Кабінет", "⬅️ Назад"
    ]
    if state.get("step", "").startswith("admin_order_") and str(text).strip() in admin_cancel_texts:
        USER_STATES.pop(str(chat_id), None)
        # Повертаємо False, щоб основний webhook обробив натиснуту кнопку меню як звичайну дію.
        return False

    if state.get("step") == "admin_order_client_id":
        client_id = "".join(ch for ch in str(text).strip() if ch.isdigit() or ch == "-")
        if not client_id:
            send_message(chat_id, "Введіть, будь ласка, коректний Telegram ID клієнта.")
            return True

        state.setdefault("admin_order", {})["client_id"] = client_id
        state["step"] = "admin_order_products"
        USER_STATES[str(chat_id)] = state

        balance = get_available_bonus_balance(client_id)
        send_message(
            chat_id,
            "✅ Клієнта вибрано.\n\n"
            f"Telegram ID: <code>{client_id}</code>\n"
            f"Доступно бонусів: <b>{balance}</b>\n\n"
            "Тепер введіть товари у форматі:\n"
            "<code>ID товару x кількість</code>\n\n"
            "Наприклад:\n"
            "<code>123 x 1\n456 x 2</code>\n\n"
            "Для акцій 1=2 / 1+1=3 кількість означає кількість акційних наборів."
        )
        return True

    if state.get("step") == "admin_order_products":
        items = parse_admin_product_lines(text)
        if not items:
            send_message(chat_id, "Не бачу товарів. Введіть, будь ласка, у форматі: <code>ID товару x кількість</code>")
            return True

        state.setdefault("admin_order", {})["items"] = items
        preview = build_admin_order_preview(state["admin_order"].get("client_id"), items, False)

        if preview.get("errors"):
            err_text = "⚠️ Є помилки у товарах:\n" + "\n".join([f"• {e}" for e in preview["errors"]])
            err_text += "\n\nВведіть список товарів ще раз."
            send_message(chat_id, err_text)
            return True

        state["step"] = "admin_order_bonus_choice"
        USER_STATES[str(chat_id)] = state
        send_message(chat_id, admin_order_preview_text(state), admin_order_bonus_keyboard(state))
        return True

    if state.get("step") == "admin_order_full_name":
        state.setdefault("admin_order", {})["full_name"] = text.strip()
        state["step"] = "admin_order_phone"
        USER_STATES[str(chat_id)] = state
        send_message(chat_id, "Введіть номер телефону клієнта:")
        return True

    if state.get("step") == "admin_order_phone":
        state.setdefault("admin_order", {})["phone"] = text.strip()
        ask_admin_order_delivery(chat_id)
        return True

    if state.get("step") == "admin_order_city":
        state.setdefault("admin_order", {})["city"] = text.strip()
        delivery_method = state["admin_order"].get("delivery_method", "")
        if delivery_method == "Нова пошта":
            state["step"] = "admin_order_np_branch"
            USER_STATES[str(chat_id)] = state
            send_message(chat_id, "Введіть відділення Нової пошти:")
        else:
            state["step"] = "admin_order_ukrposhta_index"
            USER_STATES[str(chat_id)] = state
            send_message(chat_id, "Введіть індекс Укрпошти:")
        return True

    if state.get("step") == "admin_order_np_branch":
        city = state.setdefault("admin_order", {}).get("city", "")
        state["admin_order"]["delivery_point"] = text.strip()
        state["admin_order"]["address"] = f"Місто: {city}; Відділення Нової пошти: {text.strip()}"
        ask_admin_order_payment(chat_id)
        return True

    if state.get("step") == "admin_order_ukrposhta_index":
        city = state.setdefault("admin_order", {}).get("city", "")
        state["admin_order"]["delivery_point"] = text.strip()
        state["admin_order"]["address"] = f"Місто: {city}; Індекс Укрпошти: {text.strip()}"
        ask_admin_order_payment(chat_id)
        return True

    if state.get("step") == "admin_order_comment":
        state.setdefault("admin_order", {})["comment"] = "" if str(text).strip() == "-" else text.strip()
        USER_STATES[str(chat_id)] = state
        finish_admin_created_order(chat_id)
        return True

    if state.get("step") == "admin_search":
        query = text.strip().lower()
        orders = get_orders_with_rows()

        found = []
        for order in orders:
            fields = [
                str(order.get("ПІБ", "")).lower(),
                str(order.get("Телефон", "")).lower(),
                str(order.get("Telegram ID", "")).lower()
            ]

            if any(query in field for field in fields):
                found.append(order)

        USER_STATES.pop(str(chat_id), None)

        send_message(
            chat_id,
            format_orders_list(found, "🔍 <b>Результати пошуку</b>"),
            main_menu(True)
        )
        return True

    if state.get("step") == "admin_date_filter":
        date_query = text.strip()

        if len(date_query) != 10 or date_query.count(".") != 2:
            send_message(
                chat_id,
                "Дата має бути у форматі <code>дд.мм.рррр</code>.\nНаприклад: <code>22.05.2026</code>"
            )
            return True

        orders = get_orders_with_rows()

        found = [
            order for order in orders
            if str(order.get("Дата", "")).startswith(date_query)
        ]

        USER_STATES.pop(str(chat_id), None)

        send_message(
            chat_id,
            format_orders_list(found, f"📅 <b>Замовлення за {date_query}</b>"),
            main_menu(True)
        )
        return True

    return False


def show_summary(chat_id, period="today", callback_message=None):
    if not is_admin(chat_id):
        return

    now = current_time()
    orders = get_orders_with_rows()
    contact_requests = get_contact_requests_with_rows()

    if period == "today":
        title = "📊 <b>Підсумок за сьогодні</b>"
        prefix = now.strftime("%d.%m.%Y")
        filtered_orders = [o for o in orders if str(o.get("Дата", "")).startswith(prefix)]
        filtered_requests = [r for r in contact_requests if str(r.get("Дата", "")).startswith(prefix)]
    else:
        title = "📊 <b>Підсумок за місяць</b>"
        month_part = now.strftime(".%m.%Y")
        filtered_orders = [o for o in orders if month_part in str(o.get("Дата", ""))]
        filtered_requests = [r for r in contact_requests if month_part in str(r.get("Дата", ""))]

    total_sum = 0
    new_count = 0

    for order in filtered_orders:
        total_sum += safe_float(order.get("Сума"))
        if str(order.get("Статус")).strip().lower() == "нове":
            new_count += 1

    text = (
        f"{title}\n\n"
        f"Замовлень: <b>{len(filtered_orders)}</b>\n"
        f"Сума: <b>{total_sum} грн</b>\n"
        f"Нових замовлень: <b>{new_count}</b>\n"
        f"Заявок на зв’язок: <b>{len(filtered_requests)}</b>"
    )

    keyboard = {"inline_keyboard": [[inline_button("⬅️ Назад у кабінет", "admin_back")]]}

    if callback_message:
        edit_message(chat_id, callback_message["message_id"], text, keyboard)
    else:
        send_message(chat_id, text, keyboard)


def show_contact_requests(chat_id, callback_message=None):
    if not is_admin(chat_id):
        return

    requests_list = get_contact_requests_with_rows()
    new_requests = [r for r in requests_list if str(r.get("Статус")).strip().lower() in ["нова", "нове", ""]]
    processed_requests = [r for r in requests_list if str(r.get("Статус")).strip().lower() == "опрацьовано"]

    text = (
        "📞 <b>Заявки на зв’язок</b>\n\n"
        f"🆕 Нові заявки: <b>{len(new_requests)}</b>\n"
        f"✅ Опрацьовані: <b>{len(processed_requests)}</b>"
    )

    keyboard = {
        "inline_keyboard": [
            [inline_button("🆕 Нові заявки", "contact_requests_new")],
            [inline_button("✅ Опрацьовані заявки", "contact_requests_processed")],
            [inline_button("⬅️ Назад у кабінет", "admin_back")]
        ]
    }

    if callback_message:
        edit_message(chat_id, callback_message["message_id"], text, keyboard)
    else:
        send_message(chat_id, text, keyboard)


def show_contact_requests_by_status(chat_id, status, callback_message=None):
    if not is_admin(chat_id):
        return

    requests_list = get_contact_requests_with_rows()

    if status == "Нова":
        filtered = [r for r in requests_list if str(r.get("Статус")).strip().lower() in ["нова", "нове", ""]]
        title = "🆕 Нові заявки на зв’язок"
    else:
        filtered = [r for r in requests_list if str(r.get("Статус")).strip().lower() == "опрацьовано"]
        title = "✅ Опрацьовані заявки"

    if not filtered:
        text = "Заявок у цьому розділі немає."
        keyboard = {"inline_keyboard": [[inline_button("⬅️ Назад до заявок", "contact_requests")]]}

        if callback_message:
            edit_message(chat_id, callback_message["message_id"], text, keyboard)
        else:
            send_message(chat_id, text, keyboard)
        return

    header = f"{title}\n\nУсього в розділі: <b>{len(filtered)}</b>"
    header_keyboard = {"inline_keyboard": [[inline_button("⬅️ Назад до заявок", "contact_requests")]]}

    if callback_message:
        edit_message(chat_id, callback_message["message_id"], header, header_keyboard)
    else:
        send_message(chat_id, header, header_keyboard)

    for idx, item in enumerate(filtered, start=1):
        text = (
            f"<b>{idx}. Заявка на зв’язок</b>\n\n"
            f"<b>Дата:</b> {item.get('Дата')}\n"
            f"<b>ПІБ:</b> {item.get('ПІБ')}\n"
            f"<b>Телефон:</b> {item.get('Телефон')}\n"
            f"<b>Статус:</b> {item.get('Статус') or 'Нова'}"
        )

        buttons = []
        if status == "Нова":
            buttons.append([inline_button("✅ Опрацьовано", f"contact_done_{item.get('row_index')}")])
        buttons.append([inline_button("⬅️ Назад до заявок", "contact_requests")])
        send_message(chat_id, text, {"inline_keyboard": buttons})

def mark_contact_request_done(chat_id, row_index, callback_message=None):
    if not is_admin(chat_id):
        return

    try:
        update_cell("Заявки", int(row_index), 5, "Опрацьовано")

        text = (
            "✅ <b>Заявку опрацьовано</b>\n\n"
            "Статус заявки змінено на: <b>Опрацьовано</b>"
        )

        keyboard = {
            "inline_keyboard": [
                [inline_button("🆕 Нові заявки", "contact_requests_new")],
                [inline_button("⬅️ Назад до заявок", "contact_requests")]
            ]
        }

        if callback_message:
            edit_message(chat_id, callback_message["message_id"], text, keyboard)
        else:
            send_message(chat_id, text, keyboard)

    except Exception:
        send_message(chat_id, "Не вдалося змінити статус заявки. Спробуйте ще раз.", main_menu(True))



# старі функції залишаємо як аліаси, щоб не ламати старі кнопки
def show_admin_new_orders(chat_id, callback_message=None):
    show_orders_by_status(chat_id, "Нове", callback_message)


def show_admin_processed_orders(chat_id, callback_message=None):
    show_orders_by_status(chat_id, "В обробці", callback_message)


def mark_order_processed(chat_id, row_index, callback_message=None):
    set_order_status(chat_id, row_index, "В обробці", callback_message)


# =========================
# WEBHOOK
# =========================


def process_completed_orders_without_bonus():
    """
    Службова перевірка: проходить по всіх замовленнях зі статусом Завершено
    і нараховує бонус за покупку, якщо його ще не було.
    Корисно, якщо статус випадково змінили вручну або попередній запуск не встиг нарахувати бонус.
    """
    try:
        orders = get_orders_with_rows()
        count = 0
        for order in orders:
            if str(order.get("Статус", "")).strip() == "Завершено":
                if process_purchase_bonus_for_order(order):
                    count += 1
        return count
    except Exception as e:
        print("process_completed_orders_without_bonus error:", e)
        return 0



# =========================
# MENU UPDATE BROADCAST
# =========================

def ensure_users_menu_update_column():
    """
    Додає у лист "Користувачі" службову колонку "Меню оновлено",
    щоб не надсилати оновлення одним і тим самим клієнтам повторно.

    Важливо: якщо в Google Sheets фізично є тільки 8 колонок,
    перед записом у I1 потрібно спочатку додати колонку через add_cols(),
    інакше буде помилка: Range exceeds grid limits.
    """
    try:
        ws = get_users_worksheet()
        rows = google_call_with_retry(lambda: ws.get_all_values())
        headers = rows[0] if rows else []

        for idx, header in enumerate(headers, start=1):
            if str(header).strip().lower() == "меню оновлено":
                return ws, idx

        new_col = len(headers) + 1 if headers else 1

        # Якщо в аркуші фізично не вистачає колонок — додаємо їх перед update_cell.
        try:
            current_cols = int(getattr(ws, "col_count", 0) or 0)
            if current_cols < new_col:
                google_call_with_retry(lambda: ws.add_cols(new_col - current_cols))
        except Exception as e:
            print("ensure_users_menu_update_column add_cols error:", e)

        google_call_with_retry(lambda: ws.update_cell(1, new_col, "Меню оновлено"))
        clear_cache("Користувачі")
        clear_sheet_connection_cache("Користувачі")
        return ws, new_col

    except Exception as e:
        print("ensure_users_menu_update_column error:", e)
        return None, None


def send_updated_inline_menu_to_user(chat_id, is_admin_user=False):
    """
    Прибирає стару нижню клавіатуру і надсилає нове inline-меню.
    Службове повідомлення з remove_keyboard одразу видаляється.
    """
    try:
        remove_reply_keyboard(chat_id)

        text = (
            "🏠 <b>Меню крамнички оновлено</b>\n\n"
            "Тепер усе зручніше: каталог, кошик, бонуси, замовлення та зв’язок з менеджером "
            "доступні через кнопки нижче 👇"
        )

        message_id = send_message(chat_id, text, main_menu_inline(is_admin_user))
        return bool(message_id)

    except Exception as e:
        print("send_updated_inline_menu_to_user error:", e, "chat_id:", chat_id)
        return False


def process_user_menu_updates():
    """
    Оновлює меню старим користувачам з листа "Користувачі".
    За один запуск обробляє MENU_UPDATE_LIMIT_PER_RUN клієнтів.
    Успішно оновлених позначає в колонці "Меню оновлено".
    """
    try:
        ws, menu_col = ensure_users_menu_update_column()
        if not ws or not menu_col:
            return 0

        rows = google_call_with_retry(lambda: ws.get_all_values())
        if len(rows) <= 1:
            return 0

        admin_ids = set(str(x).strip() for x in get_admin_ids())
        processed_count = 0

        for row_index, row in enumerate(rows[1:], start=2):
            if processed_count >= MENU_UPDATE_LIMIT_PER_RUN:
                break

            chat_id = str(row[0] if len(row) > 0 else "").strip()
            if not chat_id:
                continue

            already_updated = str(row[menu_col - 1] if len(row) >= menu_col else "").strip()
            if already_updated:
                continue

            ok = send_updated_inline_menu_to_user(
                chat_id=chat_id,
                is_admin_user=chat_id in admin_ids
            )

            if ok:
                google_call_with_retry(lambda row_index=row_index: ws.update_cell(row_index, menu_col, now_str()))
                processed_count += 1
                time.sleep(0.12)
            else:
                # Не ставимо позначку, щоб можна було повторити пізніше.
                time.sleep(0.12)

        if processed_count:
            clear_cache("Користувачі")

        return processed_count

    except Exception as e:
        print("process_user_menu_updates error:", e)
        return 0



def process_scheduled_broadcasts():
    """
    Єдиний автоматичний планувальник розсилок.

    Як працює:
    - до 10:00 нічого масового не шле;
    - з 10:00 запускає/продовжує Товар дня;
    - з 12:00 запускає/продовжує Акції;
    - з 15:00 запускає/продовжує Повідомлення дня/комплімент;
    - нагадування кошика перевіряє у дозволений час.

    Захист від дублів лишається у send_marketing_to_all():
    один клієнт може отримати за день максимум 1 повідомлення в кожній категорії:
    product_day, sale, daily_message, cart_reminder, marketing.
    """
    now = current_time()
    result = {
        "time": now.strftime("%d.%m.%Y %H:%M"),
        "product_day": 0,
        "sale": 0,
        "daily_message": 0,
        "cart_reminders": 0
    }

    if now.hour < BROADCAST_MIN_HOUR or now.hour >= BROADCAST_MAX_HOUR:
        print("scheduled broadcasts skipped by send window:", now_str())
        return result

    # 1. Товар дня: стартує з 10:00 і продовжується пачками, поки вся база не завершена.
    if now.hour >= SCHEDULED_PRODUCT_DAY_START_HOUR:
        try:
            result["product_day"] = process_auto_product_day_broadcast()
        except Exception as e:
            print("scheduled product day error:", e)

    # 2. Акції: стартують з 12:00 і теж продовжуються пачками.
    if now.hour >= SCHEDULED_SALE_START_HOUR:
        try:
            result["sale"] = process_sale_broadcasts()
        except Exception as e:
            print("scheduled sale error:", e)

    # 3. Комплімент/повідомлення дня: стартує з 15:00 у дозволені дні.
    if now.hour >= SCHEDULED_DAILY_MESSAGE_START_HOUR:
        try:
            result["daily_message"] = process_daily_soft_reminders()
        except Exception as e:
            print("scheduled daily message error:", e)

    # 4. Кошик: окрема логіка з власними 1/24/72 год і нічним обмеженням.
    if SCHEDULED_CART_REMINDERS_EVERY_RUN:
        try:
            result["cart_reminders"] = process_cart_reminders()
        except Exception as e:
            print("scheduled cart reminders error:", e)

    print("scheduled broadcasts result:", result)
    return result

@app.route("/process-completed-orders", methods=["GET", "HEAD"])
def process_completed_orders_route():
    if request.method == "HEAD":
        return "", 200
    count = process_completed_orders_without_bonus()
    return f"Completed orders bonuses processed: {count}", 200


@app.route("/", methods=["GET"])
def home():
    return "Bot is running"


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()

    if "message" in data:
        message = data["message"]
        chat_id = message["chat"]["id"]
        text = message.get("text", "")
        user = message.get("from", {})
        is_new_user = register_user_activity(chat_id, user)

        if handle_shared_contact(chat_id, message, user):
            return "ok"

        if handle_payment_receipt(chat_id, message):
            return "ok"

        category = get_category_by_button_text(text)

        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            if len(parts) > 1 and parts[1].startswith("ref_"):
                register_referral_from_start(chat_id, parts[1].replace("ref_", "").strip())
            grant_welcome_bonus(chat_id, only_if_new=is_new_user)
            with_loading(chat_id, "🌸 Раді бачити Вас у нашій крамничці!\n\n⏳ Завантажуємо меню для Вас...", start, chat_id)
        elif text == "/myid":
            show_my_id(chat_id)
        elif handle_contact_state(chat_id, text, user):
            pass
        elif handle_admin_state(chat_id, text):
            pass
        elif handle_order_state(chat_id, text, user):
            pass
        elif text == "⬅️ Назад":
            state = USER_STATES.get(str(chat_id), {})
            if state.get("step") == "choosing_subsection":
                category_id = state.get("category_id")
                if category_id:
                    with_loading(chat_id, "📂 Повертаємось до розділів...", show_subcategories_reply, chat_id, category_id)
                else:
                    with_loading(chat_id, "🛍️ Завантажуємо каталог для Вас...", show_catalog_menu, chat_id)
            elif state.get("step") == "choosing_subcategory":
                USER_STATES.pop(str(chat_id), None)
                with_loading(chat_id, "🛍️ Завантажуємо каталог для Вас...", show_catalog_menu, chat_id)
            else:
                show_main_menu(chat_id)
        elif text == "📦 Каталог":
            clear_product_messages(chat_id)
            with_loading(chat_id, "🛍️ Зачекайте, будь ласка...\n\nПідбираємо для Вас товари ✨", show_catalog_menu, chat_id)
        elif category:
            with_loading(chat_id, "📂 Завантажуємо розділи...", show_subcategories_reply, chat_id, category.get("ID категорії"))
        elif get_subcategory_by_button_text(text, USER_STATES.get(str(chat_id), {}).get("category_id")):
            subcategory = get_subcategory_by_button_text(text, USER_STATES.get(str(chat_id), {}).get("category_id"))
            with_loading(chat_id, "▫️ Завантажуємо підрозділи...", show_subsections_reply, chat_id, subcategory.get("ID підкатегорії"))
        elif get_subsection_by_button_text(text, USER_STATES.get(str(chat_id), {}).get("subcategory_id")):
            subsection = get_subsection_by_button_text(text, USER_STATES.get(str(chat_id), {}).get("subcategory_id"))
            with_loading(chat_id, "📦 Завантажуємо товари...", show_products_by_subsection, chat_id, subsection.get("ID підрозділу"))
        elif text == "🔥 Акції":
            clear_product_messages(chat_id)
            with_loading(chat_id, "🔥 Шукаємо найвигідніші пропозиції для Вас...\n\nЗачекайте декілька секунд ⏳", show_sales, chat_id)
        elif text == "🛒 Кошик":
            clear_product_messages(chat_id)
            with_loading(chat_id, "🛒 Формуємо Ваш кошик...\n\nЗачекайте, будь ласка ⏳", show_cart, chat_id)
        elif text == "📦 Мої замовлення":
            clear_product_messages(chat_id)
            with_loading(chat_id, "📦 Завантажуємо інформацію про Ваші замовлення...\n\nЗачекайте, будь ласка ⏳", show_my_orders, chat_id)
        elif text == "🎁 Мої бонуси":
            clear_product_messages(chat_id)
            with_loading(chat_id, "🎁 Завантажуємо Ваші бонуси...", show_bonus_cabinet, chat_id)
        elif text == "👥 Реферальна програма":
            clear_product_messages(chat_id)
            with_loading(chat_id, "👥 Завантажуємо умови реферальної програми...", show_referral_program, chat_id)
        elif text == "📞 Зв’язатися з менеджером":
            clear_service_messages(chat_id)
            clear_product_messages(chat_id)
            with_loading(chat_id, "📞 Відкриваємо форму звернення до менеджера...", contact_manager, chat_id, user)
        elif text == "📞 Оформити через менеджера":
            clear_service_messages(chat_id)
            clear_product_messages(chat_id)
            with_loading(chat_id, "📞 Передаємо заявку менеджеру...", contact_manager, chat_id, user, "manager_order")
        elif text == "🚚 Доставка і оплата":
            with_loading(chat_id, "🚚 Завантажуємо інформацію про доставку та оплату...", show_delivery_payment, chat_id)
        elif text == "👑 Кабінет":
            with_loading(chat_id, "👑 Завантажуємо кабінет...\n\nОтримуємо актуальні дані ⏳", show_admin_cabinet, chat_id)
        else:
            send_service_message(chat_id, "Оберіть, будь ласка, дію в меню 👇", main_menu_inline(is_admin(chat_id)))

    if "callback_query" in data:
        callback = data["callback_query"]
        callback_id = callback.get("id")
        callback_message = callback["message"]
        chat_id = callback_message["chat"]["id"]
        message_id = callback_message["message_id"]
        data_value = callback["data"]
        user = callback.get("from", {})
        register_user_activity(chat_id, user)

        if callback_id:
            answer_callback(callback_id, callback_loading_text(data_value))

        if data_value.startswith("category_"):
            category_id = data_value.replace("category_", "")
            with_loading(chat_id, "📂 Завантажуємо розділи...", show_subcategories, chat_id, category_id, callback_message)

        elif data_value.startswith("subcategory_"):
            subcategory_id = data_value.replace("subcategory_", "")
            with_loading(chat_id, "▫️ Завантажуємо підрозділи...", show_subsections, chat_id, subcategory_id, callback_message)

        elif data_value.startswith("subsection_"):
            subsection_id = data_value.replace("subsection_", "")
            with_loading(chat_id, "📦 Завантажуємо товари...", show_products_by_subsection, chat_id, subsection_id, callback_message)

        elif data_value == "back_categories":
            with_loading(chat_id, "📦 Повертаємось до каталогу...", show_catalog_menu, chat_id, callback_message)

        elif data_value.startswith("back_subcategories_"):
            category_id = data_value.replace("back_subcategories_", "")
            with_loading(chat_id, "📂 Повертаємось до розділів...", show_subcategories, chat_id, category_id, callback_message)

        elif data_value.startswith("back_subsections_"):
            subcategory_id = data_value.replace("back_subsections_", "")
            with_loading(chat_id, "▫️ Повертаємось до підрозділів...", show_subsections, chat_id, subcategory_id, callback_message)

        elif data_value.startswith("photo_"):
            # Формат: photo_productindex_photoindex
            parts = data_value.split("_")
            product_index = int(parts[1])
            photo_index = int(parts[2])

            state = USER_STATES.get(str(chat_id), {})
            products = state.get("products", [])
            mode = state.get("mode", "category")
            category_id = state.get("category_id", "") or state.get("subcategory_id", "")

            with_loading(chat_id, "📸 Завантажуємо фото товару...", update_product_card, chat_id, message_id, products, product_index, mode, category_id, photo_index, callback_message)

        elif data_value == "photo_counter":
            answer_callback(callback_id)


        elif data_value.startswith("products_page_"):
            page = int(data_value.replace("products_page_", ""))
            state = USER_STATES.get(str(chat_id), {})
            products = state.get("products", [])
            mode = state.get("mode", "category")
            category_id = state.get("category_id", "") or state.get("subcategory_id", "")
            with_loading(
                chat_id,
                "📦 Завантажуємо наступну сторінку товарів...",
                show_products_page,
                chat_id,
                products,
                page,
                mode,
                category_id,
                callback_message
            )

        elif data_value.startswith("catpage_"):
            parts = data_value.split("_")
            page = int(parts[-1])
            state = USER_STATES.get(str(chat_id), {})
            products = state.get("products", [])
            mode = state.get("mode", "category")
            category_id = state.get("category_id", "")
            with_loading(chat_id, "📦 Завантажуємо товар...", update_product_card, chat_id, message_id, products, page, mode, category_id, 0, callback_message)

        elif data_value.startswith("sale_page_"):
            page = int(data_value.replace("sale_page_", ""))
            state = USER_STATES.get(str(chat_id), {})
            products = state.get("products", get_sale_products())
            with_loading(chat_id, "🔥 Завантажуємо акційну пропозицію...", update_product_card, chat_id, message_id, products, page, "sale", "", 0, callback_message)

        elif data_value.startswith("more_photos_"):
            product_index = data_value.replace("more_photos_", "")
            with_loading(chat_id, "📸 Завантажуємо додаткові фото...", show_more_product_photos, chat_id, product_index)

        elif data_value == "product_unavailable":
            answer_callback(callback_id)

        elif data_value.startswith("add_one_"):
            product_id = data_value.replace("add_one_", "")
            with_loading(chat_id, "🛒 Додаємо товар у кошик...", add_to_cart, chat_id, product_id, callback_message)

        elif data_value.startswith("cart_plus_"):
            row_index = data_value.replace("cart_plus_", "")
            with_loading(chat_id, "🛒 Оновлюємо кошик...", change_cart_qty, chat_id, row_index, 1, callback_message)

        elif data_value.startswith("cart_minus_"):
            row_index = data_value.replace("cart_minus_", "")
            with_loading(chat_id, "🛒 Оновлюємо кошик...", change_cart_qty, chat_id, row_index, -1, callback_message)

        elif data_value.startswith("cart_qty_"):
            with_loading(chat_id, "🛒 Формуємо Ваш кошик...", show_cart, chat_id, callback_message)

        elif data_value.startswith("promo_product_"):
            product_id = data_value.replace("promo_product_", "")
            with_loading(chat_id, "🛍 Завантажуємо товар...", show_product_by_id, chat_id, product_id, callback_message)

        elif data_value == "open_catalog":
            clear_product_messages(chat_id)
            with_loading(chat_id, "📦 Відкриваємо каталог...", show_catalog_menu, chat_id, callback_message)

        elif data_value == "open_cart":
            clear_product_messages(chat_id)
            with_loading(chat_id, "🛒 Формуємо Ваш кошик...", show_cart, chat_id, callback_message)

        elif data_value == "bonus_use":
            state = USER_STATES.get(str(chat_id), {})
            state["use_bonuses"] = True
            USER_STATES[str(chat_id)] = state
            with_loading(chat_id, "🎁 Застосовуємо бонуси...", show_cart, chat_id, callback_message)

        elif data_value == "bonus_disable":
            state = USER_STATES.get(str(chat_id), {})
            state["use_bonuses"] = False
            USER_STATES[str(chat_id)] = state
            with_loading(chat_id, "🎁 Оновлюємо кошик без бонусів...", show_cart, chat_id, callback_message)

        elif data_value == "open_bonus_cabinet":
            with_loading(chat_id, "🎁 Завантажуємо Ваші бонуси...", show_bonus_cabinet, chat_id, callback_message)

        elif data_value == "open_referral_program":
            with_loading(chat_id, "👥 Завантажуємо умови реферальної програми...", show_referral_program, chat_id, callback_message)

        elif data_value == "open_sales":
            clear_product_messages(chat_id)
            with_loading(chat_id, "🔥 Завантажуємо акційні пропозиції...", show_sales, chat_id, callback_message)

        elif data_value == "open_orders":
            clear_product_messages(chat_id)
            with_loading(chat_id, "📦 Завантажуємо інформацію про Ваші замовлення...", show_my_orders, chat_id, callback_message)

        elif data_value == "open_delivery_payment":
            clear_product_messages(chat_id)
            with_loading(chat_id, "🚚 Завантажуємо інформацію про доставку та оплату...", show_delivery_payment, chat_id, callback_message)

        elif data_value == "contact_manager_general":
            clear_service_messages(chat_id)
            clear_product_messages(chat_id)
            with_loading(chat_id, "📞 Відкриваємо форму звернення до менеджера...", contact_manager, chat_id, user)

        elif data_value == "manager_order":
            clear_service_messages(chat_id)
            clear_product_messages(chat_id)
            with_loading(chat_id, "📞 Передаємо заявку менеджеру...", contact_manager, chat_id, user, "manager_order")

        elif data_value == "open_admin":
            with_loading(chat_id, "👑 Завантажуємо кабінет...", show_admin_cabinet, chat_id, callback_message)

        elif data_value == "back_main":
            # Кнопка може бути під фото товару. editMessageText для фото не працює,
            # тому просто прибираємо старі товарні/сервісні повідомлення і надсилаємо нове головне меню.
            USER_STATES.pop(str(chat_id), None)
            clear_flow_messages(chat_id)
            clear_product_messages(chat_id)
            clear_service_messages(chat_id)
            send_service_message(
                chat_id,
                "🏠 <b>Головне меню</b>\n\nОберіть, будь ласка, що хочете переглянути:",
                main_menu_inline(is_admin(chat_id)),
                clear_products=False
            )

        elif data_value == "order_now":
            with_loading(chat_id, "📝 Розпочинаємо оформлення замовлення...", start_order, chat_id)

        elif data_value == "add_more_before_order":
            state = USER_STATES.get(str(chat_id), {})
            state["step"] = "adding_more_before_order"
            USER_STATES[str(chat_id)] = state
            edit_message(
                chat_id,
                message_id,
                "Супер 💛 Можете додати ще товари до замовлення. Коли будете готові — відкрийте кошик і натисніть <b>Продовжити оформлення</b>."
            )
            with_loading(chat_id, "🛍️ Відкриваємо каталог, щоб Ви могли додати ще товари...", show_catalog_menu, chat_id)

        elif data_value == "confirm_order_now":
            with_loading(chat_id, "✅ Продовжуємо оформлення...", ask_need_contact, chat_id, callback_message)

        elif data_value == "continue_checkout":
            with_loading(chat_id, "✅ Продовжуємо оформлення замовлення...", continue_order_after_adding, chat_id)

        elif data_value == "clear_cart":
            clear_user_cart(chat_id)
            USER_STATES.pop(str(chat_id), None)
            show_main_options(
                chat_id,
                "🗑 Кошик очищено.",
                callback_message,
                clear_products=True
            )

        elif data_value.startswith("delete_cart_row_"):
            row_index = data_value.replace("delete_cart_row_", "")
            with_loading(chat_id, "🛒 Оновлюємо кошик...", delete_cart_item, chat_id, row_index, callback_message)

        elif data_value.startswith("delivery_"):
            state = USER_STATES.get(str(chat_id), {})
            delivery_code = data_value.replace("delivery_", "")
            state["delivery_method"] = DELIVERY_METHODS.get(delivery_code, delivery_code)
            state["step"] = "waiting_city"
            USER_STATES[str(chat_id)] = state

            edit_message(chat_id, message_id, "Введіть, будь ласка, місто доставки:")

        elif data_value.startswith("payment_"):
            state = USER_STATES.get(str(chat_id), {})
            payment_code = data_value.replace("payment_", "")
            state["payment_method"] = PAYMENT_METHODS.get(payment_code, payment_code)
            state["step"] = "waiting_comment"
            USER_STATES[str(chat_id)] = state

            keyboard = {
                "inline_keyboard": [
                    [inline_button("Пропустити", "comment_skip")]
                ]
            }

            edit_message(
                chat_id,
                message_id,
                "Додайте коментар до замовлення, якщо потрібно.\n"
                "Наприклад: відтінок, колір, побажання щодо товару.",
                keyboard
            )

        elif data_value == "comment_skip":
            state = USER_STATES.get(str(chat_id), {})
            state["comment"] = ""
            state["step"] = "waiting_free_delivery_decision"
            USER_STATES[str(chat_id)] = state

            edit_flow_message(chat_id, message_id, "Коментар пропущено ✅")
            schedule_delete_message(chat_id, message_id, 4)
            ask_free_delivery_offer(chat_id)

        elif data_value.startswith("contact_product_"):
            product_id = data_value.replace("contact_product_", "")
            with_loading(chat_id, "📞 Відкриваємо заявку менеджеру по товару...", contact_manager, chat_id, user, "product_card", product_id)

        elif data_value == "contact_from_cart":
            with_loading(chat_id, "📞 Відкриваємо заявку на зв’язок...", contact_manager, chat_id, user, "cart_reminder")

        elif data_value == "need_contact_yes":
            with_loading(chat_id, "📦 Оформлюємо Ваше замовлення...", finish_order, chat_id, user, "Так", callback_message)

        elif data_value == "need_contact_no":
            with_loading(chat_id, "📦 Оформлюємо Ваше замовлення...", finish_order, chat_id, user, "Ні", callback_message)

        elif data_value == "admin_create_order":
            with_loading(chat_id, "➕ Відкриваємо створення замовлення...", start_admin_create_order, chat_id, callback_message)

        elif data_value == "admin_order_bonus_yes":
            state = USER_STATES.get(str(chat_id), {})
            state.setdefault("admin_order", {})["use_bonuses"] = True
            state["step"] = "admin_order_full_name"
            USER_STATES[str(chat_id)] = state
            edit_message(chat_id, message_id, admin_order_preview_text(state) + "\n\nВведіть ПІБ клієнта:")

        elif data_value == "admin_order_bonus_no":
            state = USER_STATES.get(str(chat_id), {})
            state.setdefault("admin_order", {})["use_bonuses"] = False
            state["step"] = "admin_order_full_name"
            USER_STATES[str(chat_id)] = state
            edit_message(chat_id, message_id, admin_order_preview_text(state) + "\n\nВведіть ПІБ клієнта:")

        elif data_value.startswith("admin_order_delivery_"):
            state = USER_STATES.get(str(chat_id), {})
            delivery_code = data_value.replace("admin_order_delivery_", "")
            delivery_method = DELIVERY_METHODS.get(delivery_code, delivery_code)
            state.setdefault("admin_order", {})["delivery_method"] = delivery_method
            state["step"] = "admin_order_city"
            USER_STATES[str(chat_id)] = state
            edit_message(chat_id, message_id, "Введіть місто доставки:")

        elif data_value.startswith("admin_order_payment_"):
            state = USER_STATES.get(str(chat_id), {})
            payment_code = data_value.replace("admin_order_payment_", "")
            payment_method = PAYMENT_METHODS.get(payment_code, payment_code)
            state.setdefault("admin_order", {})["payment_method"] = payment_method
            state["step"] = "admin_order_comment"
            USER_STATES[str(chat_id)] = state
            edit_message(chat_id, message_id, "Додайте коментар до замовлення або введіть <code>-</code>, якщо коментар не потрібен:")

        elif data_value.startswith("admin_status_"):
            status_code = data_value.replace("admin_status_", "")
            status = ORDER_STATUS_CODES.get(status_code, status_code)
            with_loading(chat_id, "👑 Завантажуємо замовлення за статусом...", show_orders_by_status, chat_id, status, callback_message)

        elif data_value.startswith("set_status_"):
            parts = data_value.split("_", 3)
            row_index = parts[2]
            status_code = parts[3]
            status = ORDER_STATUS_CODES.get(status_code, status_code)
            with_loading(chat_id, "🔄 Оновлюємо статус замовлення...", set_order_status, chat_id, row_index, status, callback_message)

        elif data_value == "admin_search":
            with_loading(chat_id, "🔍 Відкриваємо пошук...", start_admin_search, chat_id, callback_message)

        elif data_value == "admin_date_filter":
            with_loading(chat_id, "📅 Відкриваємо фільтр за датою...", start_admin_date_filter, chat_id, callback_message)

        elif data_value == "admin_new_orders":
            with_loading(chat_id, "🆕 Завантажуємо нові замовлення...", show_admin_new_orders, chat_id, callback_message)

        elif data_value == "admin_processed_orders":
            with_loading(chat_id, "✅ Завантажуємо замовлення...", show_admin_processed_orders, chat_id, callback_message)

        elif data_value.startswith("mark_processed_"):
            row_index = data_value.replace("mark_processed_", "")
            with_loading(chat_id, "🔄 Оновлюємо статус...", mark_order_processed, chat_id, row_index, callback_message)

        elif data_value == "summary_today":
            with_loading(chat_id, "📊 Рахуємо підсумок за сьогодні...", show_summary, chat_id, "today", callback_message)

        elif data_value == "summary_month":
            with_loading(chat_id, "📊 Рахуємо підсумок за місяць...", show_summary, chat_id, "month", callback_message)

        elif data_value == "contact_requests":
            with_loading(chat_id, "📞 Завантажуємо заявки на зв’язок...", show_contact_requests, chat_id, callback_message)

        elif data_value == "contact_requests_new":
            with_loading(chat_id, "📞 Завантажуємо нові заявки...", show_contact_requests_by_status, chat_id, "Нова", callback_message)

        elif data_value == "contact_requests_processed":
            with_loading(chat_id, "📞 Завантажуємо опрацьовані заявки...", show_contact_requests_by_status, chat_id, "Опрацьовано", callback_message)

        elif data_value.startswith("contact_done_"):
            row_index = data_value.replace("contact_done_", "")
            with_loading(chat_id, "✅ Оновлюємо статус заявки...", mark_contact_request_done, chat_id, row_index, callback_message)

        elif data_value == "admin_orders_sum":
            with_loading(chat_id, "💰 Рахуємо суму замовлень...", show_admin_orders_sum, chat_id, callback_message)

        elif data_value == "clients_stats":
            with_loading(chat_id, "👥 Завантажуємо статистику клієнтів...", show_clients_stats, chat_id, callback_message)

        elif data_value == "admin_referrals":
            with_loading(chat_id, "👥 Рахуємо реферальну статистику...", show_admin_referral_stats, chat_id, callback_message)

        elif data_value == "admin_back":
            with_loading(chat_id, "👑 Оновлюємо кабінет...", show_admin_cabinet, chat_id, callback_message)

    return "ok"





@app.route("/update-user-menus", methods=["GET", "POST", "HEAD"])
def update_user_menus_endpoint():
    if request.method == "HEAD":
        return "", 200

    token = request.args.get("token", "")
    if CRON_SECRET and token != CRON_SECRET:
        return "Forbidden", 403

    try:
        sent_count = process_user_menu_updates()
        return f"User menus updated: {sent_count}", 200
    except Exception as e:
        print("update_user_menus_endpoint error:", e)
        return "User menu update error", 500

@app.route("/check-users-status", methods=["GET", "POST", "HEAD"])
def check_users_status_endpoint():
    if request.method == "HEAD":
        return "", 200

    token = request.args.get("token", "")
    if CRON_SECRET and token != CRON_SECRET:
        return "Forbidden", 403

    try:
        limit = request.args.get("limit", None)
        force = str(request.args.get("force", "")).strip().lower() in ["1", "true", "yes", "так"]
        result = process_users_status_check(limit=limit, force=force)
        return (
            f"Users status checked: {result.get('checked', 0)}; "
            f"active: {result.get('active', 0)}; "
            f"blocked: {result.get('blocked', 0)}; "
            f"unavailable: {result.get('unavailable', 0)}; "
            f"errors: {result.get('errors', 0)}; "
            f"skipped: {result.get('skipped', 0)}; "
            f"total_users: {result.get('total_users', 0)}; "
            f"limit: {result.get('limit', '')}"
        ), 200
    except Exception as e:
        print("check_users_status_endpoint error:", e)
        return "Users status check error", 500


@app.route("/welcome-bonus-broadcast", methods=["GET", "POST"])
def welcome_bonus_broadcast_endpoint():
    token = request.args.get("token", "")

    if CRON_SECRET and token != CRON_SECRET:
        return "Forbidden", 403

    try:
        sent_count = process_welcome_bonus_broadcast()
        return f"Welcome bonuses added: {sent_count}"
    except Exception as e:
        print("welcome_bonus_broadcast_endpoint error:", e)
        return "Welcome bonus broadcast error", 500


@app.route("/bonus-reminders", methods=["GET", "POST"])
def bonus_reminders_endpoint():
    token = request.args.get("token", "")

    if CRON_SECRET and token != CRON_SECRET:
        return "Forbidden", 403


    if is_uptime_head_check():
        return "", 200

    try:
        sent_count = process_bonus_reminders()
        return f"Bonus reminders sent: {sent_count}"
    except Exception as e:
        print("bonus_reminders_endpoint error:", e)
        return "Bonus reminders error", 500


@app.route("/daily-reminders", methods=["GET", "POST"])
def daily_reminders_endpoint():
    token = request.args.get("token", "")

    if CRON_SECRET and token != CRON_SECRET:
        return "Forbidden", 403


    if is_uptime_head_check():
        return "", 200

    try:
        sent_count = process_daily_soft_reminders()
        return f"Daily reminders sent: {sent_count}"
    except Exception as e:
        print("daily_reminders_endpoint error:", e)
        return "Daily reminders error", 500



@app.route("/cart-reminders", methods=["GET", "POST"])
def cart_reminders_endpoint():
    token = request.args.get("token", "")

    if CRON_SECRET and token != CRON_SECRET:
        return "Forbidden", 403


    if is_uptime_head_check():
        return "", 200

    try:
        sent_count = process_cart_reminders()
        return f"Cart reminders sent: {sent_count}"
    except Exception as e:
        print("cart_reminders_endpoint error:", e)
        return "Cart reminders error", 500


@app.route("/marketing-broadcasts", methods=["GET", "POST"])
def marketing_broadcasts_endpoint():
    token = request.args.get("token", "")

    if CRON_SECRET and token != CRON_SECRET:
        return "Forbidden", 403


    if is_uptime_head_check():
        return "", 200

    try:
        sent_count = process_marketing_broadcasts()
        return f"Marketing broadcasts sent: {sent_count}"
    except Exception as e:
        print("marketing_broadcasts_endpoint error:", e)
        return "Marketing broadcasts error", 500


@app.route("/sale-broadcasts", methods=["GET", "POST"])
def sale_broadcasts_endpoint():
    token = request.args.get("token", "")

    if CRON_SECRET and token != CRON_SECRET:
        return "Forbidden", 403


    if is_uptime_head_check():
        return "", 200

    try:
        sent_count = process_sale_broadcasts()
        return f"Sale broadcasts sent: {sent_count}"
    except Exception as e:
        print("sale_broadcasts_endpoint error:", e)
        return "Sale broadcasts error", 500


@app.route("/inactive-clients", methods=["GET", "POST"])
def inactive_clients_endpoint():
    token = request.args.get("token", "")

    if CRON_SECRET and token != CRON_SECRET:
        return "Forbidden", 403


    if is_uptime_head_check():
        return "", 200

    try:
        sent_count = process_inactive_clients_reminders()
        return f"Inactive clients reminders sent: {sent_count}"
    except Exception as e:
        print("inactive_clients_endpoint error:", e)
        return "Inactive clients reminders error", 500


@app.route("/auto-product-broadcasts", methods=["GET", "POST"])
def auto_product_broadcasts_endpoint():
    token = request.args.get("token", "")

    if CRON_SECRET and token != CRON_SECRET:
        return "Forbidden", 403


    if is_uptime_head_check():
        return "", 200

    try:
        sent_count = process_auto_product_day_broadcast()
        return f"Auto product broadcasts sent: {sent_count}"
    except Exception as e:
        print("auto_product_broadcasts_endpoint error:", e)
        return "Auto product broadcasts error", 500






@app.route("/scheduled-broadcasts", methods=["GET", "POST", "HEAD"])
def scheduled_broadcasts_endpoint():
    token = request.args.get("token", "")

    if CRON_SECRET and token != CRON_SECRET:
        return "Forbidden", 403

    # ВАЖЛИВО:
    # Для цього endpoint HEAD дозволений, бо безкоштовний UptimeRobot запускає саме HEAD.
    # Усі старі endpoint-и /marketing-broadcasts, /sale-broadcasts, /daily-reminders
    # при HEAD НЕ запускаються. А тут /scheduled-broadcasts є єдиною безпечною точкою
    # автоматичного запуску по часу.
    try:
        result = process_scheduled_broadcasts()

        # Для HEAD відповідь без тіла, але код 200, щоб UptimeRobot бачив "Up".
        if request.method == "HEAD":
            return "", 200

        return "Scheduled broadcasts: " + json.dumps(result, ensure_ascii=False)
    except Exception as e:
        print("scheduled_broadcasts_endpoint error:", e)
        return "Scheduled broadcasts error", 500

@app.route("/health", methods=["GET", "HEAD"])
def health_endpoint():
    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
