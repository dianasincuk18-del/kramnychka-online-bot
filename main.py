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

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

USER_STATES = {}


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


def update_cell(sheet_name, row, col, value):
    sh = get_sheet()
    worksheet = sh.worksheet(sheet_name)
    worksheet.update_cell(row, col, value)


def delete_row(sheet_name, row_index):
    sh = get_sheet()
    worksheet = sh.worksheet(sheet_name)
    worksheet.delete_rows(row_index)


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
        requests.post(f"{BASE_URL}/sendMessage", json=payload, timeout=15)
    except Exception as e:
        print("send_message error:", e)



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


def get_subcategory_by_button_text(text, category_id=None):
    clean_text = str(text).replace("📂", "").strip()
    subcategories = get_records("Підкатегорії")

    for subcategory in subcategories:
        active = str(subcategory.get("Активна", "")).strip().lower()
        name = str(subcategory.get("Назва підкатегорії", "")).strip()
        item_category_id = str(subcategory.get("ID категорії", "")).strip()

        if active in ["так", "yes", "1", "true", "активна"] and name == clean_text:
            if category_id is None or str(category_id) == item_category_id:
                return subcategory

    return None

def inline_button(text, callback_data):
    return {"text": text, "callback_data": callback_data}


def safe_text(value, default="—"):
    value = str(value or "").strip()
    return value if value else default


def safe_float(value, default=0):
    try:
        return float(value or default)
    except:
        return default


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
# DATA HELPERS
# =========================

def get_active_categories():
    categories = get_records("Категорії")
    return [
        c for c in categories
        if str(c.get("Активна")).strip().lower() in ["так", "yes", "true", "1"]
    ]



def get_active_subcategories(category_id):
    subcategories = get_records("Підкатегорії")
    result = []

    for item in subcategories:
        active = str(item.get("Активна", "")).strip().lower()
        item_category_id = str(item.get("ID категорії", "")).strip()

        if item_category_id == str(category_id) and active in ["так", "yes", "1", "true", "активна"]:
            result.append(item)

    return result


def get_products_by_subcategory(subcategory_id):
    products = get_records("Товари")
    result = []

    for product in products:
        active = str(product.get("Активний", "")).strip().lower()
        product_subcategory_id = str(product.get("ID підкатегорії", "")).strip()

        if product_subcategory_id == str(subcategory_id) and active in ["так", "yes", "1", "true", "активний"]:
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
    products = get_records("Товари")
    return [
        p for p in products
        if str(p.get("ID категорії")) == str(category_id)
        and str(p.get("Активний")).strip().lower() in ["так", "yes", "true", "1"]
    ]


def get_sale_products():
    products = get_records("Товари")
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
        "Вітаю у нашій крамничці 🛍\n"
        "Обери, що хочеш переглянути:"
    )
    send_message(chat_id, text, main_menu(is_admin(chat_id)))


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
            "У цій категорії поки немає підкатегорій 😔",
            categories_menu()
        )
        return

    USER_STATES[str(chat_id)] = {
        "step": "choosing_subcategory",
        "category_id": category_id
    }

    send_message(
        chat_id,
        "📂 <b>Підкатегорії</b>\n\nОберіть підкатегорію нижче 👇",
        subcategories_menu(category_id)
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
    text = "Оберіть підкатегорію 👇"

    if callback_message:
        edit_message(chat_id, callback_message["message_id"], text, keyboard)
    else:
        send_message(chat_id, text, keyboard)



def show_products_by_subcategory(chat_id, subcategory_id, callback_message=None):
    products = get_products_by_subcategory(subcategory_id)

    if not products:
        text = "У цій підкатегорії поки немає товарів 😔"
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

    USER_STATES[str(chat_id)] = {
        "step": "viewing_products",
        "products": products,
        "index": 0,
        "mode": "subcategory",
        "subcategory_id": subcategory_id
    }

    send_message(chat_id, f"📦 Знайдено товарів: <b>{len(products)}</b>")

    for idx, product in enumerate(products):
        show_product_card(
            chat_id=chat_id,
            products=products,
            index=idx,
            mode="subcategory",
            category_id=str(subcategory_id),
            photo_index=0
        )

def show_products_by_category(chat_id, category_id):
    products = get_active_products_by_category(category_id)

    if not products:
        send_message(chat_id, "У цій категорії поки немає товарів 😔", categories_menu())
        return

    show_product_card(chat_id, products, 0, "category", category_id)


def show_sales(chat_id):
    sale_products = get_sale_products()

    if not sale_products:
        send_message(chat_id, "Поки немає активних акцій 😔", main_menu(is_admin(chat_id)))
        return

    show_product_card(chat_id, sale_products, 0, "sale")


def add_to_cart(chat_id, product_id, callback_message=None):
    products = get_records("Товари")
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
    else:
        new_qty = 1
        new_sum = price
        append_row("Кошик", [chat_id, product_id, name, price, new_qty, new_sum])

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
        text = "Твій кошик поки порожній 🛒"
        keyboard = {"inline_keyboard": [[inline_button("🔄 Оновити кошик", "open_cart")]]}

        if callback_message:
            edit_message(chat_id, callback_message["message_id"], text, keyboard)
        else:
            send_message(chat_id, text, keyboard)
        return

    total = 0
    text = "🛒 <b>Твій кошик:</b>\n\n"
    buttons = []

    for item in items:
        name = item["name"]
        price = float(item["price"] or 0)
        qty = int(float(item["qty"] or 1))
        summa = float(item["sum"] or price * qty)
        row_index = item["row_index"]

        total += summa
        text += f"• {name} — {qty} шт. × {price} грн = <b>{summa} грн</b>\n"

        buttons.append([
            inline_button("➖", f"cart_minus_{row_index}"),
            inline_button(f"{qty} шт", f"cart_qty_{row_index}"),
            inline_button("➕", f"cart_plus_{row_index}"),
            inline_button("❌", f"delete_cart_row_{row_index}")
        ])

    text += f"\n💰 Разом: <b>{total} грн</b>"

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
        price = float(row[3] or 0)
        qty = int(float(row[4] or 1))
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
        send_message(chat_id, "Кошик порожній, немає що замовляти 😔", main_menu(is_admin(chat_id)))
        return

    USER_STATES[str(chat_id)] = {
        "step": "waiting_full_name",
        "full_name": "",
        "phone": "",
        "address": "",
        "need_contact": "",
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

    if state["step"] == "waiting_full_name":
        state["full_name"] = text.strip()
        state["step"] = "waiting_phone"
        send_message(chat_id, "Введіть, будь ласка, Ваш номер телефону:")
        return True

    if state["step"] == "waiting_phone":
        state["phone"] = text.strip()
        state["step"] = "waiting_address"
        send_message(chat_id, "Введіть, будь ласка, адресу доставки:")
        return True

    if state["step"] == "waiting_address":
        state["address"] = text.strip()
        state["step"] = "waiting_delivery"

        keyboard = {
            "inline_keyboard": [
                [inline_button("🚚 Нова пошта", "delivery_Нова пошта")],
                [inline_button("📦 Укрпошта", "delivery_Укрпошта")],
            ]
        }

        send_message(chat_id, "Оберіть спосіб доставки:", keyboard)
        return True

    if state["step"] == "waiting_comment":
        state["comment"] = text.strip()
        state["step"] = "waiting_need_contact"

        keyboard = {
            "inline_keyboard": [
                [inline_button("Так, зв’яжіться зі мною", "need_contact_yes")],
                [inline_button("Ні, не потрібно", "need_contact_no")]
            ]
        }

        send_message(chat_id, "Чи потрібно зв’язатись з Вами для уточнення деталей?", keyboard)
        return True

    return False

def finish_order(chat_id, user, need_contact, callback_message=None):
    cart = get_user_cart(chat_id)

    if not cart:
        USER_STATES.pop(str(chat_id), None)
        send_message(chat_id, "Кошик порожній, немає що замовляти 😔", main_menu(is_admin(chat_id)))
        return

    state = USER_STATES.get(str(chat_id), {})
    total = 0
    products_text = []

    for item in cart:
        name = item.get("Назва товару")
        qty = int(item.get("Кількість") or 1)
        summa = float(item.get("Сума") or 0)
        total += summa
        products_text.append(f"{name} x{qty}")

    order_date = datetime.now().strftime("%d.%m.%Y %H:%M")
    full_name = state.get("full_name", "")
    phone = state.get("phone", "")
    address = state.get("address", "")
    delivery_method = state.get("delivery_method", "")
    payment_method = state.get("payment_method", "")
    comment = state.get("comment", "")
    products_joined = ", ".join(products_text)

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
        comment,
        "Нове"
    ])

    clear_user_cart(chat_id)
    USER_STATES.pop(str(chat_id), None)

    notify_admin(
        full_name=full_name,
        phone=phone,
        address=address,
        delivery_method=delivery_method,
        payment_method=payment_method,
        comment=comment,
        products=products_joined,
        total=total,
        need_contact=need_contact,
        telegram_id=chat_id
    )

    final_text = (
        "✅ Дякуємо! Замовлення прийнято, ми передали його в обробку 💛\n\n"
    )

    if payment_method == "Оплата на карту":
        payment_details = get_setting_value("Реквізити для оплати")

        if payment_details:
            final_text += (
                "💳 <b>Реквізити для оплати:</b>\n"
                f"{payment_details}\n\n"
                f"Сума до оплати: <b>{total} грн</b>\n\n"
            )
        else:
            final_text += (
                "💳 Ви обрали оплату на карту.\n"
                "Менеджер надішле реквізити для оплати 💛\n\n"
            )

    if need_contact == "Так":
        final_text += "Менеджер скоро зв’яжеться з Вами для уточнення деталей.\n\n"

    final_text += "Поки ми його обробляємо, можете переглянути акційні товари 🔥"

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
        f"<b>Адреса:</b> {address}\n"
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
        settings = get_records("Налаштування")

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
            "📦 У вас поки немає замовлень.",
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
    settings = get_records("Налаштування")

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
        "В обробці": {"count": 0, "sum": 0},
        "Відправлено": {"count": 0, "sum": 0},
        "Скасовано": {"count": 0, "sum": 0},
        "Опрацьовано": {"count": 0, "sum": 0},
    }

    for order in orders:
        status = str(order.get("Статус")).strip()

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

    text = (
        "👑 <b>Кабінет</b>\n\n"
        f"🆕 Нові: <b>{stats['Нове']['count']}</b> / {stats['Нове']['sum']} грн\n"
        f"🟡 В обробці: <b>{stats['В обробці']['count']}</b> / {stats['В обробці']['sum']} грн\n"
        f"🚚 Відправлено: <b>{stats['Відправлено']['count']}</b> / {stats['Відправлено']['sum']} грн\n"
        f"❌ Скасовано: <b>{stats['Скасовано']['count']}</b> / {stats['Скасовано']['sum']} грн\n"
        f"✅ Опрацьовано: <b>{stats['Опрацьовано']['count']}</b> / {stats['Опрацьовано']['sum']} грн"
    )

    keyboard = {
        "inline_keyboard": [
            [inline_button("🆕 Нові", "admin_status_Нове")],
            [inline_button("🟡 В обробці", "admin_status_В обробці")],
            [inline_button("🚚 Відправлено", "admin_status_Відправлено")],
            [inline_button("❌ Скасовано", "admin_status_Скасовано")],
            [inline_button("✅ Опрацьовано", "admin_status_Опрацьовано")],
            [inline_button("📞 Заявки на зв’язок", "contact_requests")],
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
    if status == "В обробці":
        return "🟡"
    if status == "Відправлено":
        return "🚚"
    if status == "Скасовано":
        return "❌"
    if status == "Опрацьовано":
        return "✅"
    return "📦"


def order_details_text(order, title="Замовлення"):
    return (
        f"📦 <b>{title}</b>\n\n"
        f"<b>Дата:</b> {order.get('Дата')}\n"
        f"<b>ПІБ:</b> {order.get('ПІБ')}\n"
        f"<b>Телефон:</b> {order.get('Телефон')}\n"
        f"<b>Адреса:</b> {order.get('Адреса доставки')}\n"
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
            [inline_button("🟡 В обробці", f"set_status_{row_index}_В обробці")],
            [inline_button("🚚 Відправлено", f"set_status_{row_index}_Відправлено")],
            [inline_button("❌ Скасовано", f"set_status_{row_index}_Скасовано")],
            [inline_button("✅ Опрацьовано", f"set_status_{row_index}_Опрацьовано")],
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

    order = filtered[-1]
    text = order_details_text(order, f"{status_emoji(status)} Замовлення: {status}")
    text += f"\n\nУсього у цьому статусі: <b>{len(filtered)}</b>"

    keyboard = {
        "inline_keyboard": [
            [inline_button("🟡 В обробці", f"set_status_{order.get('row_index')}_В обробці")],
            [inline_button("🚚 Відправлено", f"set_status_{order.get('row_index')}_Відправлено")],
            [inline_button("❌ Скасовано", f"set_status_{order.get('row_index')}_Скасовано")],
            [inline_button("✅ Опрацьовано", f"set_status_{order.get('row_index')}_Опрацьовано")],
            [inline_button("🔄 Оновити", f"admin_status_{status}")],
            [inline_button("⬅️ Назад у кабінет", "admin_back")]
        ]
    }

    if callback_message:
        edit_message(chat_id, callback_message["message_id"], text, keyboard)
    else:
        send_message(chat_id, text, keyboard)


def notify_client_status_change(client_chat_id, status):
    if not client_chat_id:
        return

    messages = {
        "В обробці": "🟡 Ваше замовлення вже в обробці. Дякуємо за очікування 💛",
        "Відправлено": "🚚 Ваше замовлення відправлено. Очікуйте доставку 💛",
        "Скасовано": "❌ Ваше замовлення скасовано. Якщо це помилка — напишіть нам 💛",
        "Опрацьовано": "✅ Ваше замовлення опрацьовано. Дякуємо, що обрали нас 💛",
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
        f"🟡 В обробці: <b>{stats['В обробці']['sum']} грн</b>\n"
        f"🚚 Відправлено: <b>{stats['Відправлено']['sum']} грн</b>\n"
        f"❌ Скасовано: <b>{stats['Скасовано']['sum']} грн</b>\n"
        f"✅ Опрацьовано: <b>{stats['Опрацьовано']['sum']} грн</b>\n\n"
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
        title = "🆕 Нова заявка на зв’язок"
    else:
        filtered = [r for r in requests_list if str(r.get("Статус")).strip().lower() == "опрацьовано"]
        title = "✅ Опрацьована заявка"

    if not filtered:
        text = "Заявок у цьому розділі немає."
        keyboard = {
            "inline_keyboard": [
                [inline_button("⬅️ Назад до заявок", "contact_requests")]
            ]
        }

        if callback_message:
            edit_message(chat_id, callback_message["message_id"], text, keyboard)
        else:
            send_message(chat_id, text, keyboard)
        return

    item = filtered[-1]

    text = (
        f"{title}\n\n"
        f"<b>Дата:</b> {item.get('Дата')}\n"
        f"<b>ПІБ:</b> {item.get('ПІБ')}\n"
        f"<b>Телефон:</b> {item.get('Телефон')}\n"
        f"<b>Статус:</b> {item.get('Статус')}\n\n"
        f"Усього в розділі: <b>{len(filtered)}</b>"
    )

    buttons = []

    if status == "Нова":
        buttons.append([inline_button("✅ Опрацьовано", f"contact_done_{item.get('row_index')}")])

    buttons.append([inline_button("🔄 Оновити", "contact_requests_new" if status == "Нова" else "contact_requests_processed")])
    buttons.append([inline_button("⬅️ Назад до заявок", "contact_requests")])

    keyboard = {"inline_keyboard": buttons}

    if callback_message:
        edit_message(chat_id, callback_message["message_id"], text, keyboard)
    else:
        send_message(chat_id, text, keyboard)


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
    show_orders_by_status(chat_id, "Опрацьовано", callback_message)


def mark_order_processed(chat_id, row_index, callback_message=None):
    set_order_status(chat_id, row_index, "Опрацьовано", callback_message)


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

        category = get_category_by_button_text(text)

        if text == "/start":
            start(chat_id)
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
            if state.get("step") == "choosing_subcategory":
                USER_STATES.pop(str(chat_id), None)
                show_catalog_menu(chat_id)
            else:
                USER_STATES.pop(str(chat_id), None)
                start(chat_id)
        elif text == "📦 Каталог":
            show_catalog_menu(chat_id)
        elif category:
            show_subcategories_reply(chat_id, category.get("ID категорії"))
        elif get_subcategory_by_button_text(text, USER_STATES.get(str(chat_id), {}).get("category_id")):
            subcategory = get_subcategory_by_button_text(text, USER_STATES.get(str(chat_id), {}).get("category_id"))
            show_products_by_subcategory(chat_id, subcategory.get("ID підкатегорії"))
        elif text == "🔥 Акції":
            show_sales(chat_id)
        elif text == "🛒 Кошик":
            show_cart(chat_id)
        elif text == "📦 Мої замовлення":
            show_my_orders(chat_id)
        elif text == "📞 Зв’язатися з менеджером":
            contact_manager(chat_id, user)
        elif text == "🚚 Доставка і оплата":
            show_delivery_payment(chat_id)
        elif text == "👑 Кабінет":
            show_admin_cabinet(chat_id)
        else:
            send_message(chat_id, "Обери дію з меню 👇", main_menu(is_admin(chat_id)))

    if "callback_query" in data:
        callback = data["callback_query"]
        callback_id = callback.get("id")
        callback_message = callback["message"]
        chat_id = callback_message["chat"]["id"]
        message_id = callback_message["message_id"]
        data_value = callback["data"]
        user = callback.get("from", {})

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

            update_product_card(chat_id, message_id, products, product_index, mode, category_id, photo_index, callback_message)

        elif data_value == "photo_counter":
            answer_callback(callback_id)


        elif data_value.startswith("catpage_"):
            parts = data_value.split("_")
            page = int(parts[-1])
            state = USER_STATES.get(str(chat_id), {})
            products = state.get("products", [])
            mode = state.get("mode", "category")
            category_id = state.get("category_id", "")
            update_product_card(chat_id, message_id, products, page, mode, category_id, 0, callback_message)

        elif data_value.startswith("sale_page_"):
            page = int(data_value.replace("sale_page_", ""))
            state = USER_STATES.get(str(chat_id), {})
            products = state.get("products", get_sale_products())
            update_product_card(chat_id, message_id, products, page, "sale", "", 0, callback_message)

        elif data_value.startswith("more_photos_"):
            product_index = data_value.replace("more_photos_", "")
            show_more_product_photos(chat_id, product_index)

        elif data_value == "product_unavailable":
            answer_callback(callback_id)

        elif data_value.startswith("add_one_"):
            product_id = data_value.replace("add_one_", "")
            add_to_cart(chat_id, product_id, callback_message)

        elif data_value.startswith("cart_plus_"):
            row_index = data_value.replace("cart_plus_", "")
            change_cart_qty(chat_id, row_index, 1, callback_message)

        elif data_value.startswith("cart_minus_"):
            row_index = data_value.replace("cart_minus_", "")
            change_cart_qty(chat_id, row_index, -1, callback_message)

        elif data_value.startswith("cart_qty_"):
            show_cart(chat_id, callback_message)

        elif data_value == "open_cart":
            show_cart(chat_id, callback_message)

        elif data_value == "open_sales":
            show_sales(chat_id)

        elif data_value == "back_main":
            edit_message(
                chat_id,
                message_id,
                "🏠 <b>Головне меню</b>\n\nОберіть дію нижче 👇",
                back_to_main_inline()
            )

        elif data_value == "order_now":
            start_order(chat_id)

        elif data_value == "clear_cart":
            clear_user_cart(chat_id)
            USER_STATES.pop(str(chat_id), None)
            edit_message(chat_id, message_id, "🗑 Кошик очищено.")

        elif data_value.startswith("delete_cart_row_"):
            row_index = data_value.replace("delete_cart_row_", "")
            delete_cart_item(chat_id, row_index, callback_message)

        elif data_value.startswith("delivery_"):
            state = USER_STATES.get(str(chat_id), {})
            state["delivery_method"] = data_value.replace("delivery_", "")
            state["step"] = "waiting_payment"
            USER_STATES[str(chat_id)] = state

            keyboard = {
                "inline_keyboard": [
                    [inline_button("💳 Оплата на карту", "payment_Оплата на карту")],
                    [inline_button("📦 Накладений платіж", "payment_Накладений платіж")],
                    [inline_button("💵 Оплата при отриманні", "payment_Оплата при отриманні")]
                ]
            }

            edit_message(chat_id, message_id, "Оберіть спосіб оплати:", keyboard)

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
                "Наприклад: зручний час доставки, побажання, уточнення.",
                keyboard
            )

        elif data_value == "comment_skip":
            state = USER_STATES.get(str(chat_id), {})
            state["comment"] = ""
            state["step"] = "waiting_need_contact"
            USER_STATES[str(chat_id)] = state

            keyboard = {
                "inline_keyboard": [
                    [inline_button("Так, зв’яжіться зі мною", "need_contact_yes")],
                    [inline_button("Ні, не потрібно", "need_contact_no")]
                ]
            }

            edit_message(chat_id, message_id, "Чи потрібно зв’язатись з Вами для уточнення деталей?", keyboard)

        elif data_value == "need_contact_yes":
            finish_order(chat_id, user, "Так", callback_message)

        elif data_value == "need_contact_no":
            finish_order(chat_id, user, "Ні", callback_message)

        elif data_value.startswith("admin_status_"):
            status = data_value.replace("admin_status_", "")
            show_orders_by_status(chat_id, status, callback_message)

        elif data_value.startswith("set_status_"):
            parts = data_value.split("_", 3)
            row_index = parts[2]
            status = parts[3]
            set_order_status(chat_id, row_index, status, callback_message)

        elif data_value.startswith("admin_status_"):
            status = data_value.replace("admin_status_", "")
            show_orders_by_status(chat_id, status, callback_message)

        elif data_value.startswith("set_status_"):
            parts = data_value.split("_", 3)
            row_index = parts[2]
            status = parts[3]
            set_order_status(chat_id, row_index, status, callback_message)

        elif data_value == "admin_search":
            start_admin_search(chat_id, callback_message)

        elif data_value == "admin_date_filter":
            start_admin_date_filter(chat_id, callback_message)

        elif data_value == "admin_new_orders":
            show_admin_new_orders(chat_id, callback_message)

        elif data_value == "admin_processed_orders":
            show_admin_processed_orders(chat_id, callback_message)

        elif data_value.startswith("mark_processed_"):
            row_index = data_value.replace("mark_processed_", "")
            mark_order_processed(chat_id, row_index, callback_message)

        elif data_value == "summary_today":
            show_summary(chat_id, "today", callback_message)

        elif data_value == "summary_month":
            show_summary(chat_id, "month", callback_message)

        elif data_value == "contact_requests":
            show_contact_requests(chat_id, callback_message)

        elif data_value == "contact_requests_new":
            show_contact_requests_by_status(chat_id, "Нова", callback_message)

        elif data_value == "contact_requests_processed":
            show_contact_requests_by_status(chat_id, "Опрацьовано", callback_message)

        elif data_value.startswith("contact_done_"):
            row_index = data_value.replace("contact_done_", "")
            mark_contact_request_done(chat_id, row_index, callback_message)

        elif data_value == "admin_orders_sum":
            show_admin_orders_sum(chat_id, callback_message)

        elif data_value == "admin_back":
            show_admin_cabinet(chat_id, callback_message)

    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
