import os
import json
import requests
import gspread
from flask import Flask, request
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
SHEET_ID = os.environ.get("SHEET_ID")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

USER_STATES = {}


# =========================
# SIMPLE CACHE FOR SPEED
# =========================

CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "300"))
PRODUCTS_PAGE_SIZE = int(os.environ.get("PRODUCTS_PAGE_SIZE", "3"))

CACHE = {
    "records": {},
    "values": {}
}


def cache_get(bucket, key):
    item = CACHE.get(bucket, {}).get(key)
    if not item:
        return None

    created_at = item.get("created_at")
    if not created_at:
        return None

    age = (datetime.now() - created_at).total_seconds()
    if age > CACHE_TTL_SECONDS:
        try:
            del CACHE[bucket][key]
        except Exception:
            pass
        return None

    return item.get("data")


def cache_set(bucket, key, data):
    CACHE.setdefault(bucket, {})[key] = {
        "created_at": datetime.now(),
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
    creds_dict = json.loads(GOOGLE_CREDS_JSON)

    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]

    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID)


def get_records(sheet_name):
    sh = get_sheet()
    worksheet = sh.worksheet(sheet_name)
    return worksheet.get_all_records()


def get_values(sheet_name):
    sh = get_sheet()
    worksheet = sh.worksheet(sheet_name)
    return worksheet.get_all_values()


def get_or_create_worksheet(sheet_name, headers):
    sh = get_sheet()

    try:
        ws = sh.worksheet(sheet_name)
    except Exception:
        ws = sh.add_worksheet(title=sheet_name, rows=1000, cols=len(headers))
        ws.append_row(headers, value_input_option="USER_ENTERED")

    values = ws.get_all_values()
    if not values:
        ws.append_row(headers, value_input_option="USER_ENTERED")

    return ws


def append_contact_request(row):
    headers = ["Дата", "Telegram ID", "ПІБ", "Телефон", "Статус"]
    ws = get_or_create_worksheet("Заявки", headers)
    ws.append_row(row, value_input_option="USER_ENTERED")


def get_contact_requests_with_rows():
    headers = ["Дата", "Telegram ID", "ПІБ", "Телефон", "Статус"]
    ws = get_or_create_worksheet("Заявки", headers)
    rows = ws.get_all_values()
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
    sh = get_sheet()
    worksheet = sh.worksheet(sheet_name)
    worksheet.append_row(row, value_input_option="USER_ENTERED")
    clear_cache(sheet_name)


def update_cell(sheet_name, row, col, value):
    sh = get_sheet()
    worksheet = sh.worksheet(sheet_name)
    worksheet.update_cell(row, col, value)
    clear_cache(sheet_name)


def delete_row(sheet_name, row_index):
    sh = get_sheet()
    worksheet = sh.worksheet(sheet_name)
    worksheet.delete_rows(row_index)
    clear_cache(sheet_name)


def clear_user_cart(telegram_id):
    sh = get_sheet()
    ws = sh.worksheet("Кошик")
    rows = ws.get_all_values()
    rows_to_delete = []

    for i, row in enumerate(rows[1:], start=2):
        if len(row) > 0 and str(row[0]) == str(telegram_id):
            rows_to_delete.append(i)

    for row_index in reversed(rows_to_delete):
        ws.delete_rows(row_index)


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


def get_cart_worksheet():
    """
    Лист "Кошик" тепер має додаткові колонки для нагадувань.
    Якщо старі колонки вже були — код акуратно додасть відсутні в кінець.
    """
    sh = get_sheet()

    try:
        ws = sh.worksheet("Кошик")
    except Exception:
        ws = sh.add_worksheet(title="Кошик", rows=1000, cols=len(CART_BASE_HEADERS))
        ws.append_row(CART_BASE_HEADERS, value_input_option="USER_ENTERED")
        return ws

    values = ws.get_all_values()
    if not values:
        ws.append_row(CART_BASE_HEADERS, value_input_option="USER_ENTERED")
        return ws

    headers = values[0]
    changed = False

    for idx, header in enumerate(CART_BASE_HEADERS, start=1):
        if len(headers) < idx or not str(headers[idx - 1]).strip():
            ws.update_cell(1, idx, header)
            changed = True

    if changed:
        print("Кошик headers updated for reminders")

    return ws


def now_str():
    return datetime.now().strftime("%d.%m.%Y %H:%M")


def update_cart_reminder_columns(row_index, updated_at=None, reminder1=None, reminder2=None, reminder3=None):
    try:
        ws = get_cart_worksheet()

        if updated_at is not None:
            ws.update_cell(row_index, 7, updated_at)
        if reminder1 is not None:
            ws.update_cell(row_index, 8, reminder1)
        if reminder2 is not None:
            ws.update_cell(row_index, 9, reminder2)
        if reminder3 is not None:
            ws.update_cell(row_index, 10, reminder3)

    except Exception as e:
        print("update_cart_reminder_columns error:", e)


def cart_reminder_keyboard():
    return {
        "inline_keyboard": [
            [inline_button("🛒 Перейти до кошика", "open_cart")]
        ]
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
            "⏰ <b>Нагадуємо про Ваш кошик</b>\n\n"
            "Обрані товари все ще очікують на Вас 🛍\n\n"
            "Якщо бажаєте оформити замовлення — поверніться до кошика та завершіть покупку 💛"
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
    rows = ws.get_all_values()
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
            grouped[telegram_id]["updated_dates"].append(datetime.now())

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
    """
    grouped = get_cart_rows_grouped_by_user()
    now = datetime.now()
    sent_count = 0

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

        try:
            discount_percent = get_client_discount_percent(telegram_id)
        except:
            discount_percent = 0

        text = cart_reminder_text(
            reminder_number=reminder_number,
            total=data.get("total", 0),
            discount_percent=discount_percent
        )

        send_message(telegram_id, text, cart_reminder_keyboard())

        sent_at = now_str()
        for row_index in data.get("rows", []):
            try:
                get_cart_worksheet().update_cell(row_index, reminder_col, sent_at)
            except Exception as e:
                print("cart reminder mark error:", e)

        sent_count += 1

    return sent_count

def get_orders_with_rows():
    rows = get_values("Замовлення")
    result = []

    for i, row in enumerate(rows[1:], start=2):
        if len(row) >= 12:
            item = {
                "row_index": i,
                "Дата": row[0] if len(row) > 0 else "",
                "Telegram ID": row[1] if len(row) > 1 else "",
                "ПІБ": row[2] if len(row) > 2 else "",
                "Телефон": row[3] if len(row) > 3 else "",
                "Адреса доставки": row[4] if len(row) > 4 else "",
                "Спосіб доставки": row[5] if len(row) > 5 else "",
                "Спосіб оплати": row[6] if len(row) > 6 else "",
                "Товари": row[7] if len(row) > 7 else "",
                "Сума": row[8] if len(row) > 8 else "",
                "Потрібно зв’язатись": row[9] if len(row) > 9 else "",
                "Коментар": row[10] if len(row) > 10 else "",
                "Статус": row[11] if len(row) > 11 else ""
            }
        else:
            item = {
                "row_index": i,
                "Дата": row[0] if len(row) > 0 else "",
                "Telegram ID": row[1] if len(row) > 1 else "",
                "ПІБ": row[2] if len(row) > 2 else "",
                "Телефон": row[3] if len(row) > 3 else "",
                "Адреса доставки": row[4] if len(row) > 4 else "",
                "Спосіб доставки": "",
                "Спосіб оплати": "",
                "Товари": row[5] if len(row) > 5 else "",
                "Сума": row[6] if len(row) > 6 else "",
                "Потрібно зв’язатись": row[7] if len(row) > 7 else "",
                "Коментар": row[8] if len(row) > 8 else "",
                "Статус": row[9] if len(row) > 9 else ""
            }
        result.append(item)

    return result


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
        datetime.now().strftime("%d.%m.%Y %H:%M"),
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

    send_message(
        chat_id,
        "✅ Дякуємо! Квитанцію отримано та передано менеджеру на перевірку 💛\n\n"
        "Після перевірки ми оновимо статус Вашого замовлення."
    )
    return True


# =========================
# TELEGRAM HELPERS
# =========================

def send_message(chat_id, text, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }

    if keyboard:
        payload["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)

    try:
        response = requests.post(f"{BASE_URL}/sendMessage", json=payload, timeout=15)

        if response.ok:
            data = response.json()
            return data.get("result", {}).get("message_id")

        print("send_message telegram error:", response.text)
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
        payload["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)

    try:
        response = requests.post(f"{BASE_URL}/sendPhoto", json=payload, timeout=15)

        if not response.ok:
            print("send_photo telegram error:", response.text)
            return False

        return True

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
        payload["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)

    try:
        response = requests.post(f"{BASE_URL}/sendDocument", json=payload, timeout=20)

        if not response.ok:
            print("send_document telegram error:", response.text)
            return False

        return True

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
        payload["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)

    try:
        requests.post(f"{BASE_URL}/editMessageText", json=payload, timeout=15)
    except Exception as e:
        print("edit_message error:", e)


def edit_caption(chat_id, message_id, caption, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "caption": caption,
        "parse_mode": "HTML"
    }

    if keyboard:
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
        payload["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)

    try:
        r = requests.post(f"{BASE_URL}/editMessageMedia", data=payload, timeout=15)
        if not r.ok:
            print("edit_media_photo telegram error:", r.text)
    except Exception as e:
        print("edit_media_photo error:", e)



def answer_callback(callback_id):
    try:
        requests.post(
            f"{BASE_URL}/answerCallbackQuery",
            json={"callback_query_id": callback_id},
            timeout=15
        )
    except Exception as e:
        print("answer_callback error:", e)


def main_menu(is_admin=False):
    keyboard = [
        [{"text": "📦 Каталог"}, {"text": "🔥 Акції"}],
        [{"text": "🛒 Кошик"}, {"text": "📦 Мої замовлення"}],
        [{"text": "🎁 Мої бонуси"}, {"text": "👥 Реферальна програма"}],
        [{"text": "📞 Зв’язатися з менеджером"}, {"text": "🚚 Доставка і оплата"}]
    ]

    if is_admin:
        keyboard.append([{"text": "👑 Кабінет"}])

    return {
        "keyboard": keyboard,
        "resize_keyboard": True
    }


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


def back_to_main_inline():
    return {
        "inline_keyboard": [
            [inline_button("⬅️ Назад у меню", "back_main")]
        ]
    }


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
        rows = ws.get_all_records()

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
        ws = get_users_worksheet()
        rows = ws.get_all_values()

        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        telegram_id = str(chat_id).strip()
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

                ws.update_cell(i, 6, now)
                ws.update_cell(i, 7, visits + 1)
                return

        ws.append_row([
            telegram_id,
            username,
            first_name,
            last_name,
            now,
            now,
            1
        ], value_input_option="USER_ENTERED")

    except Exception as e:
        print("register_user_activity error:", e)


def parse_bot_datetime(value):
    value = str(value or "").strip()

    for fmt in ["%d.%m.%Y %H:%M", "%d.%m.%Y"]:
        try:
            return datetime.strptime(value, fmt)
        except:
            pass

    return None


def get_clients_monitoring_stats():
    try:
        users_rows = get_users_worksheet().get_all_values()[1:]
    except Exception as e:
        print("get users stats error:", e)
        users_rows = []

    orders = get_orders_with_rows()
    now = datetime.now()
    today_prefix = now.strftime("%d.%m.%Y")
    month_part = now.strftime(".%m.%Y")

    total_users = 0
    new_today = 0
    new_month = 0

    for row in users_rows:
        if not row or not str(row[0]).strip():
            continue

        total_users += 1
        first_seen = row[4] if len(row) > 4 else ""

        if str(first_seen).startswith(today_prefix):
            new_today += 1

        if month_part in str(first_seen):
            new_month += 1

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
        "repeat_clients": repeat_clients
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
REFERRAL_BONUS_AMOUNT = int(os.environ.get("REFERRAL_BONUS_AMOUNT", "50"))
REFERRAL_MIN_ORDER_SUM = float(os.environ.get("REFERRAL_MIN_ORDER_SUM", "500"))
BONUS_MAX_USE_PERCENT = float(os.environ.get("BONUS_MAX_USE_PERCENT", "20"))
BONUS_VALID_DAYS = int(os.environ.get("BONUS_VALID_DAYS", "60"))


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
    return (datetime.now() + timedelta(days=BONUS_VALID_DAYS)).strftime("%d.%m.%Y")


def get_bonus_rows():
    try:
        return get_bonus_worksheet().get_all_values()
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
    today = datetime.now().date()
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


def calculate_bonus_to_use(chat_id, subtotal_after_discount):
    balance = get_available_bonus_balance(chat_id)
    max_allowed = round(float(subtotal_after_discount or 0) * BONUS_MAX_USE_PERCENT / 100, 2)
    return max(0, min(balance, max_allowed))


def add_bonus_transaction(chat_id, amount, transaction_type, comment="", order_row_index="", status="Активний", expires_at=None):
    try:
        ws = get_bonus_worksheet()
        ws.append_row([
            now_str(),
            chat_id,
            transaction_type,
            amount,
            amount,
            expires_at or bonus_expiry_date(),
            status,
            comment,
            order_row_index
        ], value_input_option="USER_ENTERED")
        clear_cache("Бонуси")
    except Exception as e:
        print("add_bonus_transaction error:", e)


def spend_bonuses(chat_id, amount, order_row_index="", comment="Списання бонусів за замовлення"):
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
        rows = ws.get_all_values()

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
        f"Бонусами можна оплатити до <b>{int(BONUS_MAX_USE_PERCENT)}%</b> суми замовлення.\n"
        f"Термін дії бонусів: <b>{BONUS_VALID_DAYS} днів</b>."
    )

    keyboard = {"inline_keyboard": [[inline_button("🛒 Перейти до кошика", "open_cart")]]}

    if callback_message:
        edit_message(chat_id, callback_message["message_id"], text, keyboard)
    else:
        send_message(chat_id, text, keyboard)


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
        f"• бонусами можна оплатити до <b>{int(BONUS_MAX_USE_PERCENT)}%</b> суми замовлення\n"
        f"• бонуси діють <b>{BONUS_VALID_DAYS} днів</b> з моменту нарахування\n\n"
        "⚠️ <b>Умови програми</b>\n"
        "• бонуси нараховуються тільки після статусу <b>Завершено</b>\n"
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

    if callback_message:
        edit_message(chat_id, callback_message["message_id"], text, keyboard)
    else:
        send_message(chat_id, text, keyboard)




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
        rows = get_referrals_worksheet().get_all_values()[1:]
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

        bonus_rows = get_bonus_worksheet().get_all_values()[1:]
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
        rows = get_referrals_worksheet().get_all_values()[1:]

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

        bonus_rows = get_bonus_worksheet().get_all_values()[1:]
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
        rows = ws.get_all_values()

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
        rows = get_referrals_worksheet().get_all_values()

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



def bonus_already_added_for_order(order_row_index, transaction_type="Бонус за покупку"):
    try:
        rows = get_bonus_worksheet().get_all_values()
        for row in rows[1:]:
            row_type = str(row[2] if len(row) > 2 else "").strip()
            row_order = str(row[8] if len(row) > 8 else "").strip()
            row_status = str(row[6] if len(row) > 6 else "").strip().lower()
            if row_type == transaction_type and row_order == str(order_row_index).strip() and row_status == "активний":
                return True
    except Exception as e:
        print("bonus_already_added_for_order error:", e)
    return False


def process_purchase_bonus_for_order(order):
    """
    Нараховує клієнту 5% бонусами після статусу "Завершено".
    Повторно за те саме замовлення бонус не нараховується.
    """
    try:
        if not order:
            return False

        chat_id = str(order.get("Telegram ID", "")).strip()
        order_row_index = str(order.get("row_index", "")).strip()
        total = safe_float(order.get("Сума"))

        if not chat_id or not order_row_index or total <= 0:
            return False

        if bonus_already_added_for_order(order_row_index, "Бонус за покупку"):
            return False

        bonus_amount = round(total * PURCHASE_BONUS_PERCENT / 100, 2)
        if bonus_amount <= 0:
            return False

        add_bonus_transaction(
            chat_id=chat_id,
            amount=bonus_amount,
            transaction_type="Бонус за покупку",
            comment=f"{int(PURCHASE_BONUS_PERCENT)}% від завершеного замовлення",
            order_row_index=order_row_index,
            status="Активний",
            expires_at=bonus_expiry_date()
        )

        send_message(
            chat_id,
            "🎉 <b>Дякуємо за покупку!</b>\n\n"
            "Ваше замовлення успішно завершене 💛\n\n"
            f"🎁 На Ваш бонусний рахунок нараховано <b>{bonus_amount} бонусів</b>.\n"
            f"Бонуси діють протягом <b>{BONUS_VALID_DAYS} днів</b>."
        )
        return True

    except Exception as e:
        print("process_purchase_bonus_for_order error:", e)
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

        rows = get_bonus_worksheet().get_all_values()
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
        rows = ws.get_all_values()

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
    Щоб не створювати негатив — не пишемо, що бонуси згорять,
    а просто нагадуємо про наявний бонусний рахунок.
    """
    try:
        rows = get_bonus_worksheet().get_all_values()
        notified = set()
        sent = 0

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

            send_message(telegram_id, bonus_expiry_reminder_text(balance))
            notified.add(telegram_id)
            sent += 1

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
        rows = ws.get_all_values()

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
        date_now = datetime.now().strftime("%d.%m.%Y %H:%M")

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

    for item in cart:
        try:
            subtotal += float(item.get("Сума") or 0)
        except:
            pass

    discount_percent = get_client_discount_percent(chat_id)
    discount_amount = round(subtotal * discount_percent / 100, 2) if discount_percent else 0
    after_discount = round(subtotal - discount_amount, 2)

    if use_bonuses is None:
        state = USER_STATES.get(str(chat_id), {})
        use_bonuses = bool(state.get("use_bonuses"))

    available_bonuses = get_available_bonus_balance(chat_id)
    max_bonus_to_use = calculate_bonus_to_use(chat_id, after_discount)
    bonus_used = max_bonus_to_use if use_bonuses else 0

    total = round(after_discount - bonus_used, 2)

    return {
        "subtotal": subtotal,
        "discount_percent": discount_percent,
        "discount_amount": discount_amount,
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

    send_message(chat_id, text, keyboard)

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


def ensure_headers(ws, headers):
    """
    Акуратно додає відсутні заголовки у перший рядок,
    не ламаючи вже існуючі колонки.
    """
    try:
        values = ws.get_all_values()
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
    value = str(value or "").strip()
    if not value:
        return None

    for fmt in ["%d.%m.%Y", "%d.%m.%Y %H:%M", "%Y-%m-%d", "%Y-%m-%d %H:%M"]:
        try:
            return datetime.strptime(value, fmt).date()
        except:
            pass

    return None


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
        "Статус"
    ]
    return get_or_create_worksheet("Надіслані акції", headers)


def get_broadcast_client_ids():
    """
    Беремо всіх користувачів, які хоча б раз взаємодіяли з ботом.
    Адмінів не виключаємо, щоб власник теж бачив тестові розсилки.
    """
    ids = []
    try:
        rows = get_users_worksheet().get_all_values()[1:]
        for row in rows:
            telegram_id = str(row[0] if len(row) > 0 else "").strip()
            if telegram_id and telegram_id not in ids:
                ids.append(telegram_id)
    except Exception as e:
        print("get_broadcast_client_ids error:", e)

    return ids


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
        sale_price = str(product.get("Акційна ціна", "") or "").strip()
        old_price = str(product.get("Стара ціна", "") or "").strip()
        sale = str(product.get("Акція", "") or "").strip()

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

    text += "\nЗаходьте переглянути актуальні пропозиції 💛"
    return text


def send_marketing_to_all(text, keyboard=None, photo_url=None):
    sent = 0
    failed = 0

    for client_id in get_broadcast_client_ids():
        try:
            ok = False
            if photo_url:
                ok = send_photo(client_id, photo_url, text, keyboard)
            if not ok:
                ok = bool(send_message(client_id, text, keyboard))

            if ok:
                sent += 1
            else:
                failed += 1
        except Exception as e:
            print("send_marketing_to_all user error:", client_id, e)
            failed += 1

    return sent, failed


def process_marketing_broadcasts():
    """
    Запускається через /marketing-broadcasts.
    Надсилає заплановані рядки з листа "Розсилки".
    За один запуск бере обмежену кількість розсилок, щоб не було спаму.
    """
    ws = get_marketing_worksheet()
    rows = ws.get_all_values()
    if not rows:
        return 0

    headers = rows[0]
    today = datetime.now().date()
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

        product = get_product_by_id(product_id) if product_id else None
        photos = get_product_photos(product) if product else []
        photo_url = photos[0] if photos else None

        text = marketing_message_text(row_type, title, body, product)
        keyboard = product_marketing_keyboard(product_id if product else None, button_text)
        sent, failed = send_marketing_to_all(text, keyboard, photo_url)

        update_cell_by_header(ws, row_index, headers, "Надіслано", "Так")
        update_cell_by_header(ws, row_index, headers, "Дата надсилання", now_str())
        sent_campaigns += 1

        print(f"marketing campaign sent row={row_index}, sent={sent}, failed={failed}")

    if sent_campaigns == 0:
        # Якщо ручних розсилок на сьогодні немає — автоматично надсилаємо "товар дня".
        sent_campaigns += process_auto_product_day_broadcast()

    return sent_campaigns


def sale_product_already_broadcasted(product_id):
    try:
        rows = get_sale_broadcasts_worksheet().get_all_values()[1:]
        for row in rows:
            if str(row[1] if len(row) > 1 else "").strip() == str(product_id).strip():
                return True
    except Exception as e:
        print("sale_product_already_broadcasted error:", e)
    return False


def mark_sale_product_broadcasted(product):
    try:
        product_id = str(product.get("ID товару", "")).strip()
        name = str(product.get("Назва товару", "")).strip()
        ws = get_sale_broadcasts_worksheet()
        ws.append_row([now_str(), product_id, name, "Надіслано"], value_input_option="USER_ENTERED")
    except Exception as e:
        print("mark_sale_product_broadcasted error:", e)


def process_sale_broadcasts():
    """
    Запускається через /sale-broadcasts.
    Якщо в таблиці з'явився новий активний акційний товар,
    бот один раз повідомить про нього клієнтам.
    """
    sale_products = get_sale_products()
    sent_count = 0

    for product in sale_products:
        if sent_count >= SALE_BROADCAST_LIMIT_PER_RUN:
            break

        product_id = str(product.get("ID товару", "")).strip()
        if not product_id or sale_product_already_broadcasted(product_id):
            continue

        photos = get_product_photos(product)
        photo_url = photos[0] if photos else None
        text = marketing_message_text(
            "Акція",
            "🔥 Нова акційна пропозиція",
            "Ми додали вигідну пропозицію для Вас.",
            product
        )
        keyboard = product_marketing_keyboard(product_id, "🔥 Переглянути товар")
        send_marketing_to_all(text, keyboard, photo_url)
        mark_sale_product_broadcasted(product)
        sent_count += 1

    return sent_count


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
    rows = ws.get_all_values()
    if not rows:
        return 0

    headers = rows[0]
    now_dt = datetime.now()
    sent = 0

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

        keyboard = {
            "inline_keyboard": [
                [inline_button("📦 Переглянути каталог", "open_catalog")],
                [inline_button("🔥 Переглянути акції", "open_sales")]
            ]
        }

        ok = send_message(telegram_id, inactive_client_text(), keyboard)
        if ok:
            update_cell_by_header(ws, row_index, headers, "Останнє нагадування неактивним", now_str())
            sent += 1

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
    today = datetime.now().strftime("%d.%m.%Y")
    try:
        rows = get_auto_product_broadcasts_worksheet().get_all_values()[1:]
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
        rows = get_auto_product_broadcasts_worksheet().get_all_values()[1:]
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
    sent, failed = send_marketing_to_all(text, keyboard, photo_url)

    if sent > 0:
        mark_auto_product_broadcasted(product)
        print(f"auto product broadcast sent product={product_id}, sent={sent}, failed={failed}")
        return 1

    return 0

def show_product_by_id(chat_id, product_id, callback_message=None):
    product = get_product_by_id(product_id)
    if not product:
        send_message(chat_id, "На жаль, товар уже не знайдено або він недоступний 😔", main_menu(is_admin(chat_id)))
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


def get_sale_products():
    products = get_cached_records("Товари")
    return [
        p for p in products
        if str(p.get("Акція")).strip() != ""
        and str(p.get("Активний")).strip().lower() in ["так", "yes", "true", "1"]
    ]


def product_text(product, index=None, total=None):
    name = safe_text(product.get("Назва товару"), "Товар без назви")
    description = safe_text(product.get("Опис"), "")
    price = safe_text(product.get("Ціна"), "0")
    old_price = str(product.get("Стара ціна", "") or "").strip()
    sale_price = str(product.get("Акційна ціна", "") or "").strip()
    sale = str(product.get("Акція") or "").strip()

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
            [inline_button("🛒 Додати в кошик", f"add_one_{product_id}")]
        ]

    if extra_photos:
        buttons.append([inline_button("📸 Більше фото", f"more_photos_{index}")])

    buttons.append([inline_button("🛒 Перейти в кошик", "open_cart")])

    return {"inline_keyboard": buttons}

def start(chat_id):
    USER_STATES.pop(str(chat_id), None)

    text = (
        "Привіт 👋\n\n"
        "Вітаємо у нашій крамничці 🛍💛\n\n"
        "Ми постійно оновлюємо асортимент, додаємо новинки та найкращі пропозиції для Вас ✨\n\n"
        "Обов'язково заглядайте до каталогу та розділу акцій — там регулярно з'являються нові товари та вигідні знижки 🔥\n\n"
        "Бажаємо приємних покупок та гарного настрою 🌸\n\n"
        "Оберіть, будь ласка, що хочете переглянути:"
    )
    send_message(chat_id, text, main_menu(is_admin(chat_id)))


def show_main_menu(chat_id):
    USER_STATES.pop(str(chat_id), None)
    send_message(
        chat_id,
        "🏠 <b>Головне меню</b>\n\nОберіть, будь ласка, що хочете переглянути:",
        main_menu(is_admin(chat_id))
    )


def show_my_id(chat_id):
    send_message(chat_id, f"Ваш Telegram ID:\n<code>{chat_id}</code>", main_menu(is_admin(chat_id)))


def show_catalog_menu(chat_id):
    active_categories = get_active_categories()

    if not active_categories:
        send_message(chat_id, "Поки немає активних категорій 😔", main_menu(is_admin(chat_id)))
        return

    send_message(
        chat_id,
        "📦 <b>Каталог</b>\n\nОберіть категорію нижче 👇",
        categories_menu()
    )



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

    send_message(chat_id, "📸 Додаткові фото товару:")

    for photo_url in extra_photos:
        ok = send_photo(chat_id, photo_url, "")

        if not ok:
            send_document(chat_id, photo_url, "")



def product_short_caption(product, index=None, total=None):
    name = safe_text(product.get("Назва товару"), "Товар без назви")
    caption = ""

    if index is not None and total is not None:
        caption += f"📦 Товар {index + 1} з {total}\n"

    caption += f"<b>{name}</b>"
    return caption[:1000]


def send_product_text(chat_id, text, keyboard=None):
    """
    Telegram дозволяє довгий текст окремим повідомленням, але не дозволяє
    дуже довгий підпис під фото. Тому опис товару відправляємо окремо.
    """
    max_len = 3900

    if len(text) <= max_len:
        send_message(chat_id, text, keyboard)
        return

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

    for idx, part in enumerate(parts):
        part_keyboard = keyboard if idx == len(parts) - 1 else None
        send_message(chat_id, part, part_keyboard)


def show_product_card(chat_id, products, index=0, mode="category", category_id="", photo_index=0):
    if not products:
        send_message(chat_id, "Товарів поки немає 😔", main_menu(is_admin(chat_id)))
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
        # ВАЖЛИВО: не ставимо весь опис у caption, бо Telegram має ліміт ~1024 символи.
        # Фото надсилаємо з коротким підписом, а повний опис + кнопки окремим повідомленням.
        short_caption = product_short_caption(product, index, total)
        ok = send_photo(chat_id, photos[0], short_caption)

        if not ok:
            doc_ok = send_document(chat_id, photos[0], short_caption)
            if not doc_ok:
                print("product photo failed, sending text only")

        send_product_text(chat_id, text, keyboard)
    else:
        send_product_text(chat_id, text, keyboard)



def build_products_page_keyboard(page, total_pages):
    buttons = []

    nav_row = []
    if page > 0:
        nav_row.append(inline_button("⬅️ Назад", f"products_page_{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(inline_button("Далі ➡️", f"products_page_{page + 1}"))

    if nav_row:
        buttons.append(nav_row)

    buttons.append([inline_button("🛒 Перейти в кошик", "open_cart")])
    return {"inline_keyboard": buttons}


def show_products_page(chat_id, products, page=0, mode="category", category_id="", callback_message=None):
    if not products:
        text = "Товарів поки немає 😔"
        keyboard = back_to_main_inline()
        if callback_message:
            edit_message(chat_id, callback_message["message_id"], text, keyboard)
        else:
            send_message(chat_id, text, main_menu(is_admin(chat_id)))
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

    if callback_message:
        edit_message(chat_id, callback_message["message_id"], header, build_products_page_keyboard(page, total_pages))
    else:
        send_message(chat_id, header, build_products_page_keyboard(page, total_pages))

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
    send_message(
        chat_id,
        f"📄 Сторінка <b>{page + 1}</b> з <b>{total_pages}</b>",
        build_products_page_keyboard(page, total_pages)
    )


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
        # Для редагування теж не використовуємо довгий caption.
        short_caption = product_short_caption(product, index, total)
        edit_media_photo(chat_id, message_id, photos[photo_index], short_caption)
        send_product_text(chat_id, text, keyboard)
    else:
        send_product_text(chat_id, text, keyboard)

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
    subcategories = get_active_subcategories(category_id)

    if not subcategories:
        text = "У цій категорії поки немає підкатегорій 😔"
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

    buttons = []

    for subcategory in subcategories:
        subcategory_id = subcategory.get("ID підкатегорії")
        name = subcategory.get("Назва підкатегорії")
        buttons.append([inline_button(name, f"subcategory_{subcategory_id}")])

    buttons.append([inline_button("⬅️ Назад до категорій", "back_categories")])

    keyboard = {"inline_keyboard": buttons}
    text = "Оберіть, будь ласка, розділ 👇"

    if callback_message:
        edit_message(chat_id, callback_message["message_id"], text, keyboard)
    else:
        send_message(chat_id, text, keyboard)



def show_products_by_subcategory(chat_id, subcategory_id, callback_message=None):
    products = get_products_by_subcategory(subcategory_id)

    if not products:
        text = "У цьому розділі поки немає товарів 😔"
        keyboard = {
            "inline_keyboard": [
                [inline_button("⬅️ Назад до каталогу", "back_categories")]
            ]
        }

        if callback_message:
            edit_message(chat_id, callback_message["message_id"], text, keyboard)
        else:
            send_message(chat_id, text, keyboard)
        return

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

    if not products:
        text = "У цьому підрозділі поки немає товарів 😔"
        keyboard = back_to_main_inline()

        if callback_message:
            edit_message(chat_id, callback_message["message_id"], text, keyboard)
        else:
            send_message(chat_id, text, keyboard)
        return

    state = USER_STATES.get(str(chat_id), {})

    show_products_page(
        chat_id=chat_id,
        products=products,
        page=0,
        mode="subsection",
        category_id=str(subsection_id),
        callback_message=callback_message
    )

    state["step"] = "viewing_products"
    state["subsection_id"] = subsection_id
    USER_STATES[str(chat_id)] = {**USER_STATES.get(str(chat_id), {}), **state}

def show_products_by_category(chat_id, category_id):
    products = get_active_products_by_category(category_id)

    if not products:
        send_message(chat_id, "У цій категорії поки немає товарів 😔", categories_menu())
        return

    show_products_page(chat_id, products, 0, "category", str(category_id))


def show_sales(chat_id):
    sale_products = get_sale_products()

    if not sale_products:
        send_message(chat_id, "Поки немає активних акцій 😔", main_menu(is_admin(chat_id)))
        return

    show_products_page(
        chat_id=chat_id,
        products=sale_products,
        page=0,
        mode="sale",
        category_id=""
    )

def add_to_cart(chat_id, product_id, callback_message=None):
    products = get_cached_records("Товари")
    product = None

    for p in products:
        if str(p.get("ID товару")) == str(product_id):
            product = p
            break

    if not product:
        send_message(chat_id, "Товар не знайдено 😔", main_menu(is_admin(chat_id)))
        return

    availability = str(product.get("Наявність", "") or "").strip().lower()

    if availability == "немає":
        send_message(chat_id, "❌ Цього товару зараз немає в наявності.", main_menu(is_admin(chat_id)))
        return

    name = safe_text(product.get("Назва товару"), "Товар")
    price = safe_float(product.get("Акційна ціна") or product.get("Ціна") or 0)

    existing = find_cart_row_by_product(chat_id, product_id)

    if existing:
        row_index = existing["row_index"]
        old_qty = int(float(existing["qty"] or 1))
        new_qty = old_qty + 1
        new_sum = price * new_qty

        update_cell("Кошик", row_index, 5, new_qty)
        update_cell("Кошик", row_index, 6, new_sum)
        update_cart_reminder_columns(row_index, updated_at=now_str(), reminder1="", reminder2="", reminder3="")
    else:
        new_qty = 1
        new_sum = price
        get_cart_worksheet()
        append_row("Кошик", [chat_id, product_id, name, price, new_qty, new_sum, now_str(), "", "", ""])

    text = (
        f"✅ Товар <b>{name}</b> додано в кошик.\n\n"
        f"Кількість: <b>{new_qty} шт.</b>\n"
        f"Сума: <b>{new_sum} грн</b>"
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
    items = find_user_cart_rows(chat_id)

    if not items:
        text = "Ваш кошик поки порожній 🛒"
        keyboard = {"inline_keyboard": [[inline_button("🔄 Оновити кошик", "open_cart")]]}

        if callback_message:
            edit_message(chat_id, callback_message["message_id"], text, keyboard)
        else:
            send_message(chat_id, text, keyboard)
        return

    subtotal = 0
    text = "🛒 <b>Ваш кошик:</b>\n\n"
    buttons = []

    for item in items:
        name = item["name"]
        price = float(item["price"] or 0)
        qty = int(float(item["qty"] or 1))
        summa = float(item["sum"] or price * qty)
        row_index = item["row_index"]

        subtotal += summa
        text += f"• {name} — {qty} шт. × {price} грн = <b>{summa} грн</b>\n"

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
        text += (
            f"\n\n🎁 Ваші бонуси: <b>{available_bonuses}</b>"
            f"\nМожна списати в цьому замовленні: <b>{max_bonus_to_use} грн</b>"
        )
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

    if callback_message:
        edit_message(chat_id, callback_message["message_id"], text, keyboard)
    else:
        send_message(chat_id, text, keyboard)

def change_cart_qty(chat_id, row_index, delta, callback_message=None):
    rows = get_values("Кошик")

    try:
        row_index = int(row_index)
        row = rows[row_index - 1]
    except:
        send_message(chat_id, "Не вдалося змінити кількість. Спробуйте ще раз.", main_menu(is_admin(chat_id)))
        return

    if len(row) < 6 or str(row[0]) != str(chat_id):
        send_message(chat_id, "Цей товар не знайдено у Вашому кошику.", main_menu(is_admin(chat_id)))
        return

    try:
        price = safe_float(row[3] or 0)
        qty = int(safe_float(row[4] or 1))
    except:
        price = 0
        qty = 1

    new_qty = qty + int(delta)

    if new_qty <= 0:
        delete_row("Кошик", row_index)
        show_cart(chat_id, callback_message)
        return

    new_sum = price * new_qty

    update_cell("Кошик", row_index, 5, new_qty)
    update_cell("Кошик", row_index, 6, new_sum)
    update_cart_reminder_columns(row_index, updated_at=now_str(), reminder1="", reminder2="", reminder3="")

    show_cart(chat_id, callback_message)


def delete_cart_item(chat_id, row_index, callback_message=None):
    try:
        delete_row("Кошик", int(row_index))
        show_cart(chat_id, callback_message)
    except Exception:
        send_message(chat_id, "Не вдалося видалити товар. Спробуйте ще раз.", main_menu(is_admin(chat_id)))


def start_order(chat_id):
    cart = get_user_cart(chat_id)

    if not cart:
        send_message(chat_id, "Ваш кошик порожній, немає що замовляти 😔", main_menu(is_admin(chat_id)))
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

    send_message(chat_id, "Введіть, будь ласка, Ваше ПІБ:")

def handle_contact_state(chat_id, text, user):
    state = USER_STATES.get(str(chat_id))

    if not state:
        return False

    if state.get("step") == "contact_waiting_full_name":
        state["contact_full_name"] = text.strip()
        state["step"] = "contact_waiting_phone"
        send_message(chat_id, "Введіть, будь ласка, Ваш номер телефону:")
        return True

    if state.get("step") == "contact_waiting_phone":
        state["contact_phone"] = text.strip()
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
        send_message(chat_id, "Введіть, будь ласка, Ваш номер телефону:")
        return True

    if step == "waiting_phone":
        state["phone"] = text.strip()
        state["step"] = "waiting_delivery"
        USER_STATES[str(chat_id)] = state

        keyboard = {
            "inline_keyboard": [
                [inline_button("🚚 Нова пошта", "delivery_Нова пошта")],
                [inline_button("📦 Укрпошта", "delivery_Укрпошта")]
            ]
        }
        send_message(chat_id, "Оберіть, будь ласка, спосіб доставки:", keyboard)
        return True

    if step == "waiting_city":
        state["city"] = text.strip()
        delivery_method = state.get("delivery_method", "")

        if delivery_method == "Нова пошта":
            state["step"] = "waiting_np_branch"
            send_message(chat_id, "Введіть, будь ласка, номер або адресу відділення Нової пошти:")
        elif delivery_method == "Укрпошта":
            state["step"] = "waiting_ukrposhta_index"
            send_message(chat_id, "Введіть, будь ласка, індекс Укрпошти:")
        else:
            state["step"] = "waiting_delivery"
            keyboard = {
                "inline_keyboard": [
                    [inline_button("🚚 Нова пошта", "delivery_Нова пошта")],
                    [inline_button("📦 Укрпошта", "delivery_Укрпошта")]
                ]
            }
            send_message(chat_id, "Оберіть, будь ласка, спосіб доставки:", keyboard)

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
            [inline_button("💳 Оплата за реквізитами IBAN", "payment_Оплата за реквізитами IBAN")],
            [inline_button("📦 Накладений платіж", "payment_Накладений платіж")]
        ]
    }

    text = "Оберіть, будь ласка, спосіб оплати:"

    if callback_message:
        edit_message(chat_id, callback_message["message_id"], text, keyboard)
    else:
        send_message(chat_id, text, keyboard)


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
        edit_message(chat_id, callback_message["message_id"], text, keyboard)
    else:
        send_message(chat_id, text, keyboard)


def finish_order(chat_id, user, need_contact, callback_message=None):
    cart = get_user_cart(chat_id)

    if not cart:
        USER_STATES.pop(str(chat_id), None)
        send_message(chat_id, "Кошик порожній, немає що замовляти 😔", main_menu(is_admin(chat_id)))
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
        name = item.get("Назва товару")
        qty = int(item.get("Кількість") or 1)
        products_text.append(f"{name} x{qty}")

    order_date = datetime.now().strftime("%d.%m.%Y %H:%M")
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
        extra_notes.append(f"Бонуси списано: {bonus_used} грн")

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
        final_text += f"🎁 Списано бонусів: <b>{bonus_used} грн</b>\n"

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

    keyboard = {
        "inline_keyboard": [
            [inline_button("🔥 Переглянути акції", "open_sales")]
        ]
    }

    if callback_message:
        edit_message(chat_id, callback_message["message_id"], final_text, keyboard)
    else:
        send_message(chat_id, final_text, keyboard)

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



def show_my_orders(chat_id):
    orders = get_orders_with_rows()

    my_orders = [
        order for order in orders
        if str(order.get("Telegram ID")) == str(chat_id)
    ]

    if not my_orders:
        send_message(
            chat_id,
            "📦 У Вас поки немає замовлень.",
            main_menu(is_admin(chat_id))
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

    send_message(chat_id, text, main_menu(is_admin(chat_id)))


def contact_manager(chat_id, user):
    USER_STATES[str(chat_id)] = {
        "step": "contact_waiting_full_name",
        "contact_full_name": "",
        "contact_phone": ""
    }

    send_message(chat_id, "Введіть, будь ласка, Ваше ПІБ:")


def finish_contact_request(chat_id, user, state):
    request_date = datetime.now().strftime("%d.%m.%Y %H:%M")
    full_name = state.get("contact_full_name", "")
    phone = state.get("contact_phone", "")

    append_contact_request([
        request_date,
        chat_id,
        full_name,
        phone,
        "Нова"
    ])

    send_message(
        chat_id,
        "✅ Дякуємо! Заявку передано менеджеру. Ми скоро зв’яжемося з Вами 💛",
        main_menu(is_admin(chat_id))
    )

    admin_text = (
        "📞 <b>Нова заявка на зв’язок</b>\n\n"
        f"<b>ПІБ:</b> {full_name}\n"
        f"<b>Телефон:</b> {phone}\n"
        f"<b>Telegram ID:</b> {chat_id}"
    )

    for admin_id in get_admin_ids():
        send_message(admin_id, admin_text)

def show_delivery_payment(chat_id):
    settings = get_cached_records("Налаштування")

    if not settings:
        send_message(chat_id, "Інформацію про доставку й оплату ще не додано.", main_menu(is_admin(chat_id)))
        return

    text = "🚚 <b>Доставка і оплата</b>\n\n"

    for row in settings:
        param = row.get("Параметр")
        value = row.get("Значення")
        text += f"<b>{param}:</b>\n{value}\n\n"

    send_message(chat_id, text, back_to_main_inline())


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
            [inline_button("🆕 Нові", "admin_status_Нове")],
            [inline_button("💳 Очікується оплата", "admin_status_Очікується оплата")],
            [inline_button("🟡 В обробці", "admin_status_В обробці")],
            [inline_button("🚚 Відправлено", "admin_status_Відправлено")],
            [inline_button("✅ Завершено", "admin_status_Завершено")],
            [inline_button("❌ Скасовано", "admin_status_Скасовано")],
            [inline_button("📞 Заявки на зв’язок", "contact_requests")],
            [inline_button("👥 Клієнти", "clients_stats")],
            [inline_button("👥 Рефералка", "admin_referrals")],
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
            [inline_button("💳 Очікується оплата", f"set_status_{row_index}_Очікується оплата")],
            [inline_button("🟡 В обробці", f"set_status_{row_index}_В обробці")],
            [inline_button("🚚 Відправлено", f"set_status_{row_index}_Відправлено")],
            [inline_button("✅ Завершено", f"set_status_{row_index}_Завершено")],
            [inline_button("❌ Скасовано", f"set_status_{row_index}_Скасовано")],
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
        keyboard = order_status_keyboard(order.get("row_index"), f"admin_status_{status}")
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
        target_row = rows[int(row_index) - 1] if len(rows) >= int(row_index) else []
        status_col = 12 if len(target_row) >= 12 else 10
        update_cell("Замовлення", int(row_index), status_col, status)

        if status == "Відправлено" and target_order:
            target_order["Статус"] = status
            notify_client_order_sent(target_order)
        elif status == "Завершено" and target_order:
            target_order["Статус"] = status
            process_purchase_bonus_for_order(target_order)
            process_referral_bonus_for_order(target_order)
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

        keyboard = {
            "inline_keyboard": [
                [inline_button("🔄 Оновити цей статус", f"admin_status_{status}")],
                [inline_button("⬅️ Назад у кабінет", "admin_back")]
            ]
        }

        if callback_message:
            edit_message(chat_id, callback_message["message_id"], text, keyboard)
        else:
            send_message(chat_id, text, keyboard)

    except Exception:
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

    now = datetime.now()
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
        register_user_activity(chat_id, user)

        if handle_payment_receipt(chat_id, message):
            return "ok"

        category = get_category_by_button_text(text)

        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            if len(parts) > 1 and parts[1].startswith("ref_"):
                register_referral_from_start(chat_id, parts[1].replace("ref_", "").strip())
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
            with_loading(chat_id, "🔥 Шукаємо найвигідніші пропозиції для Вас...\n\nЗачекайте декілька секунд ⏳", show_sales, chat_id)
        elif text == "🛒 Кошик":
            with_loading(chat_id, "🛒 Формуємо Ваш кошик...\n\nЗачекайте, будь ласка ⏳", show_cart, chat_id)
        elif text == "📦 Мої замовлення":
            with_loading(chat_id, "📦 Завантажуємо інформацію про Ваші замовлення...\n\nЗачекайте, будь ласка ⏳", show_my_orders, chat_id)
        elif text == "🎁 Мої бонуси":
            with_loading(chat_id, "🎁 Завантажуємо Ваші бонуси...", show_bonus_cabinet, chat_id)
        elif text == "👥 Реферальна програма":
            with_loading(chat_id, "👥 Завантажуємо умови реферальної програми...", show_referral_program, chat_id)
        elif text == "📞 Зв’язатися з менеджером":
            contact_manager(chat_id, user)
        elif text == "🚚 Доставка і оплата":
            with_loading(chat_id, "🚚 Завантажуємо інформацію про доставку та оплату...", show_delivery_payment, chat_id)
        elif text == "👑 Кабінет":
            with_loading(chat_id, "👑 Завантажуємо кабінет...\n\nОтримуємо актуальні дані ⏳", show_admin_cabinet, chat_id)
        else:
            send_message(chat_id, "Оберіть, будь ласка, дію з меню 👇", main_menu(is_admin(chat_id)))

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
            answer_callback(callback_id)

        if data_value.startswith("photo_"):
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
            with_loading(chat_id, "📦 Відкриваємо каталог...", show_catalog_menu, chat_id)

        elif data_value == "open_cart":
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
            with_loading(chat_id, "🔥 Завантажуємо акційні пропозиції...", show_sales, chat_id)

        elif data_value == "back_main":
            edit_message(
                chat_id,
                message_id,
                "🏠 <b>Головне меню</b>\n\nОберіть дію нижче 👇",
                back_to_main_inline()
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
            edit_message(chat_id, message_id, "🗑 Кошик очищено.")

        elif data_value.startswith("delete_cart_row_"):
            row_index = data_value.replace("delete_cart_row_", "")
            with_loading(chat_id, "🛒 Оновлюємо кошик...", delete_cart_item, chat_id, row_index, callback_message)

        elif data_value.startswith("delivery_"):
            state = USER_STATES.get(str(chat_id), {})
            state["delivery_method"] = data_value.replace("delivery_", "")
            state["step"] = "waiting_city"
            USER_STATES[str(chat_id)] = state

            edit_message(chat_id, message_id, "Введіть, будь ласка, місто доставки:")

        elif data_value.startswith("payment_"):
            state = USER_STATES.get(str(chat_id), {})
            state["payment_method"] = data_value.replace("payment_", "")
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

            edit_message(chat_id, message_id, "Коментар пропущено ✅")
            ask_free_delivery_offer(chat_id)

        elif data_value == "need_contact_yes":
            with_loading(chat_id, "📦 Оформлюємо Ваше замовлення...", finish_order, chat_id, user, "Так", callback_message)

        elif data_value == "need_contact_no":
            with_loading(chat_id, "📦 Оформлюємо Ваше замовлення...", finish_order, chat_id, user, "Ні", callback_message)

        elif data_value.startswith("admin_status_"):
            status = data_value.replace("admin_status_", "")
            with_loading(chat_id, "👑 Завантажуємо замовлення за статусом...", show_orders_by_status, chat_id, status, callback_message)

        elif data_value.startswith("set_status_"):
            parts = data_value.split("_", 3)
            row_index = parts[2]
            status = parts[3]
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



@app.route("/bonus-reminders", methods=["GET", "POST"])
def bonus_reminders_endpoint():
    token = request.args.get("token", "")

    if CRON_SECRET and token != CRON_SECRET:
        return "Forbidden", 403

    try:
        sent_count = process_bonus_reminders()
        return f"Bonus reminders sent: {sent_count}"
    except Exception as e:
        print("bonus_reminders_endpoint error:", e)
        return "Bonus reminders error", 500


@app.route("/cart-reminders", methods=["GET", "POST"])
def cart_reminders_endpoint():
    token = request.args.get("token", "")

    if CRON_SECRET and token != CRON_SECRET:
        return "Forbidden", 403

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

    try:
        sent_count = process_auto_product_day_broadcast()
        return f"Auto product broadcasts sent: {sent_count}"
    except Exception as e:
        print("auto_product_broadcasts_endpoint error:", e)
        return "Auto product broadcasts error", 500



if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
