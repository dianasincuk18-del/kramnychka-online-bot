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

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

USER_STATES = {}


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


def append_row(sheet_name, row):
    sh = get_sheet()
    worksheet = sh.worksheet(sheet_name)
    worksheet.append_row(row, value_input_option="USER_ENTERED")


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


def send_message(chat_id, text, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }

    if keyboard:
        payload["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)

    requests.post(f"{BASE_URL}/sendMessage", json=payload)


def send_photo(chat_id, photo_url, caption, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "photo": photo_url,
        "caption": caption,
        "parse_mode": "HTML"
    }

    if keyboard:
        payload["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)

    requests.post(f"{BASE_URL}/sendPhoto", json=payload)


def main_menu():
    return {
        "keyboard": [
            [{"text": "📦 Каталог"}, {"text": "🗂 Категорії"}],
            [{"text": "🔥 Акції"}, {"text": "🛒 Кошик"}],
            [{"text": "✅ Замовити"}],
            [{"text": "🚚 Доставка і оплата"}]
        ],
        "resize_keyboard": True
    }


def inline_button(text, callback_data):
    return {"text": text, "callback_data": callback_data}


def start(chat_id):
    USER_STATES.pop(str(chat_id), None)

    text = (
        "Привіт 👋\n\n"
        "Вітаю у нашій крамничці 🛍\n"
        "Обери, що хочеш переглянути:"
    )
    send_message(chat_id, text, main_menu())


def show_categories(chat_id):
    categories = get_records("Категорії")
    active_categories = [
        c for c in categories
        if str(c.get("Активна")).strip().lower() in ["так", "yes", "true", "1"]
    ]

    if not active_categories:
        send_message(chat_id, "Поки немає активних категорій 😔", main_menu())
        return

    buttons = []
    for cat in active_categories:
        buttons.append([
            inline_button(
                f"📁 {cat.get('Назва категорії')}",
                f"cat_{cat.get('ID категорії')}"
            )
        ])

    send_message(chat_id, "Ось наші категорії 👇", {"inline_keyboard": buttons})


def show_all_products(chat_id):
    products = get_records("Товари")
    active_products = [
        p for p in products
        if str(p.get("Активний")).strip().lower() in ["так", "yes", "true", "1"]
    ]

    if not active_products:
        send_message(chat_id, "Поки немає активних товарів 😔", main_menu())
        return

    for product in active_products:
        send_product(chat_id, product)


def show_products_by_category(chat_id, category_id):
    products = get_records("Товари")
    filtered = [
        p for p in products
        if str(p.get("ID категорії")) == str(category_id)
        and str(p.get("Активний")).strip().lower() in ["так", "yes", "true", "1"]
    ]

    if not filtered:
        send_message(chat_id, "У цій категорії поки немає товарів 😔", main_menu())
        return

    for product in filtered:
        send_product(chat_id, product)


def show_sales(chat_id):
    products = get_records("Товари")
    sale_products = [
        p for p in products
        if str(p.get("Акція")).strip() != ""
        and str(p.get("Активний")).strip().lower() in ["так", "yes", "true", "1"]
    ]

    if not sale_products:
        send_message(chat_id, "Поки немає активних акцій 😔", main_menu())
        return

    for product in sale_products:
        send_product(chat_id, product)


def send_product(chat_id, product):
    product_id = product.get("ID товару")
    name = product.get("Назва товару")
    description = product.get("Опис")
    price = product.get("Ціна")
    photo = str(product.get("Фото")).strip()
    sale = str(product.get("Акція")).strip()

    text = f"<b>{name}</b>\n\n"
    text += f"{description}\n\n"
    text += f"💰 Ціна: <b>{price} грн</b>"

    if sale:
        text += f"\n🔥 Акція: <b>{sale}</b>"

    keyboard = {
        "inline_keyboard": [
            [inline_button("🛒 Додати в кошик", f"add_{product_id}")]
        ]
    }

    if photo:
        send_photo(chat_id, photo, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)


def add_to_cart(chat_id, product_id):
    products = get_records("Товари")
    product = None

    for p in products:
        if str(p.get("ID товару")) == str(product_id):
            product = p
            break

    if not product:
        send_message(chat_id, "Товар не знайдено 😔", main_menu())
        return

    name = product.get("Назва товару")
    price = float(product.get("Ціна") or 0)

    append_row("Кошик", [chat_id, product_id, name, price, 1, price])
    send_message(chat_id, f"✅ Товар <b>{name}</b> додано в кошик.", main_menu())


def show_cart(chat_id):
    cart = get_user_cart(chat_id)

    if not cart:
        send_message(chat_id, "Твій кошик поки порожній 🛒", main_menu())
        return

    total = 0
    text = "🛒 <b>Твій кошик:</b>\n\n"

    for item in cart:
        name = item.get("Назва товару")
        price = float(item.get("Ціна") or 0)
        qty = int(item.get("Кількість") or 1)
        summa = float(item.get("Сума") or price * qty)
        total += summa
        text += f"• {name} — {qty} шт. × {price} грн = <b>{summa} грн</b>\n"

    text += f"\n💰 Разом: <b>{total} грн</b>"

    keyboard = {
        "inline_keyboard": [
            [inline_button("✅ Оформити замовлення", "order_now")],
            [inline_button("🗑 Очистити кошик", "clear_cart")]
        ]
    }

    send_message(chat_id, text, keyboard)


def start_order(chat_id):
    cart = get_user_cart(chat_id)

    if not cart:
        send_message(chat_id, "Кошик порожній, немає що замовляти 😔", main_menu())
        return

    USER_STATES[str(chat_id)] = {
        "step": "waiting_full_name",
        "full_name": "",
        "phone": "",
        "address": "",
        "need_contact": ""
    }

    send_message(chat_id, "Введіть, будь ласка, Ваше ПІБ:")


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


def finish_order(chat_id, user, need_contact):
    cart = get_user_cart(chat_id)

    if not cart:
        USER_STATES.pop(str(chat_id), None)
        send_message(chat_id, "Кошик порожній, немає що замовляти 😔", main_menu())
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

    append_row("Замовлення", [
        order_date,
        chat_id,
        state.get("full_name", ""),
        state.get("phone", ""),
        state.get("address", ""),
        ", ".join(products_text),
        total,
        need_contact,
        "",
        "Нове"
    ])

    clear_user_cart(chat_id)
    USER_STATES.pop(str(chat_id), None)

    send_message(
        chat_id,
        "✅ Замовлення прийнято!\n\n"
        "Дякуємо! Ми передали Ваше замовлення менеджеру 💛",
        main_menu()
    )


def show_delivery_payment(chat_id):
    settings = get_records("Налаштування")

    if not settings:
        send_message(chat_id, "Інформацію про доставку й оплату ще не додано.", main_menu())
        return

    text = "🚚 <b>Доставка і оплата</b>\n\n"

    for row in settings:
        param = row.get("Параметр")
        value = row.get("Значення")
        text += f"<b>{param}:</b>\n{value}\n\n"

    send_message(chat_id, text, main_menu())


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

        if text == "/start":
            start(chat_id)
        elif handle_order_state(chat_id, text, user):
            pass
        elif text == "📦 Каталог":
            show_categories(chat_id)
        elif text == "🗂 Категорії":
            show_categories(chat_id)
        elif text == "🛍 Товари":
            show_all_products(chat_id)
        elif text == "🔥 Акції":
            show_sales(chat_id)
        elif text == "🛒 Кошик":
            show_cart(chat_id)
        elif text == "✅ Замовити":
            start_order(chat_id)
        elif text == "🚚 Доставка і оплата":
            show_delivery_payment(chat_id)
        else:
            send_message(chat_id, "Обери дію з меню 👇", main_menu())

    if "callback_query" in data:
        callback = data["callback_query"]
        chat_id = callback["message"]["chat"]["id"]
        data_value = callback["data"]
        user = callback.get("from", {})

        if data_value.startswith("cat_"):
            category_id = data_value.replace("cat_", "")
            show_products_by_category(chat_id, category_id)
        elif data_value.startswith("add_"):
            product_id = data_value.replace("add_", "")
            add_to_cart(chat_id, product_id)
        elif data_value == "order_now":
            start_order(chat_id)
        elif data_value == "clear_cart":
            clear_user_cart(chat_id)
            USER_STATES.pop(str(chat_id), None)
            send_message(chat_id, "🗑 Кошик очищено.", main_menu())
        elif data_value == "need_contact_yes":
            finish_order(chat_id, user, "Так")
        elif data_value == "need_contact_no":
            finish_order(chat_id, user, "Ні")

    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
