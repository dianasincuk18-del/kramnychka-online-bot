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
        requests.post(f"{BASE_URL}/sendPhoto", json=payload, timeout=15)
    except Exception as e:
        print("send_photo error:", e)


def main_menu(is_admin=False):
    keyboard = [
        [{"text": "📦 Каталог"}, {"text": "🗂 Категорії"}],
        [{"text": "🔥 Акції"}, {"text": "🛒 Кошик"}],
        [{"text": "✅ Замовити"}],
        [{"text": "🚚 Доставка і оплата"}]
    ]

    if is_admin:
        keyboard.append([{"text": "👑 Кабінет"}])

    return {
        "keyboard": keyboard,
        "resize_keyboard": True
    }


def inline_button(text, callback_data):
    return {"text": text, "callback_data": callback_data}


def is_admin(chat_id):
    return str(chat_id) == str(ADMIN_CHAT_ID) and str(ADMIN_CHAT_ID).strip() != ""


# =========================
# BOT LOGIC
# =========================

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


def show_categories(chat_id):
    categories = get_records("Категорії")
    active_categories = [
        c for c in categories
        if str(c.get("Активна")).strip().lower() in ["так", "yes", "true", "1"]
    ]

    if not active_categories:
        send_message(chat_id, "Поки немає активних категорій 😔", main_menu(is_admin(chat_id)))
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


def show_products_by_category(chat_id, category_id):
    products = get_records("Товари")
    filtered = [
        p for p in products
        if str(p.get("ID категорії")) == str(category_id)
        and str(p.get("Активний")).strip().lower() in ["так", "yes", "true", "1"]
    ]

    if not filtered:
        send_message(chat_id, "У цій категорії поки немає товарів 😔", main_menu(is_admin(chat_id)))
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
        send_message(chat_id, "Поки немає активних акцій 😔", main_menu(is_admin(chat_id)))
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
            [inline_button("🛒 Додати в кошик", f"choose_qty_{product_id}")]
        ]
    }

    if photo:
        send_photo(chat_id, photo, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)


def show_qty_choice(chat_id, product_id):
    products = get_records("Товари")
    product = None

    for p in products:
        if str(p.get("ID товару")) == str(product_id):
            product = p
            break

    if not product:
        send_message(chat_id, "Товар не знайдено 😔", main_menu(is_admin(chat_id)))
        return

    name = product.get("Назва товару")

    keyboard = {
        "inline_keyboard": [
            [
                inline_button("1 шт", f"add_{product_id}_1"),
                inline_button("2 шт", f"add_{product_id}_2"),
                inline_button("3 шт", f"add_{product_id}_3")
            ],
            [
                inline_button("4 шт", f"add_{product_id}_4"),
                inline_button("5 шт", f"add_{product_id}_5")
            ]
        ]
    }

    send_message(chat_id, f"Оберіть кількість товару:\n<b>{name}</b>", keyboard)


def add_to_cart(chat_id, product_id, qty=1):
    products = get_records("Товари")
    product = None

    for p in products:
        if str(p.get("ID товару")) == str(product_id):
            product = p
            break

    if not product:
        send_message(chat_id, "Товар не знайдено 😔", main_menu(is_admin(chat_id)))
        return

    name = product.get("Назва товару")
    price = float(product.get("Ціна") or 0)
    qty = int(qty)
    summa = price * qty

    append_row("Кошик", [chat_id, product_id, name, price, qty, summa])
    send_message(
        chat_id,
        f"✅ Товар <b>{name}</b> додано в кошик.\nКількість: <b>{qty} шт.</b>\nСума: <b>{summa} грн</b>",
        main_menu(is_admin(chat_id))
    )


def show_cart(chat_id):
    cart = get_user_cart(chat_id)

    if not cart:
        send_message(chat_id, "Твій кошик поки порожній 🛒", main_menu(is_admin(chat_id)))
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
            [inline_button("❌ Видалити товар", "delete_item_menu")],
            [inline_button("🗑 Очистити кошик", "clear_cart")]
        ]
    }

    send_message(chat_id, text, keyboard)


def show_delete_item_menu(chat_id):
    items = find_user_cart_rows(chat_id)

    if not items:
        send_message(chat_id, "Кошик порожній 🛒", main_menu(is_admin(chat_id)))
        return

    buttons = []

    for item in items:
        title = f"❌ {item['name']} — {item['qty']} шт."
        buttons.append([inline_button(title, f"delete_cart_row_{item['row_index']}")])

    buttons.append([inline_button("⬅️ Назад до кошика", "back_to_cart")])

    send_message(chat_id, "Оберіть товар, який потрібно видалити:", {"inline_keyboard": buttons})


def delete_cart_item(chat_id, row_index):
    try:
        delete_row("Кошик", int(row_index))
        send_message(chat_id, "✅ Товар видалено з кошика.", main_menu(is_admin(chat_id)))
        show_cart(chat_id)
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
    products_joined = ", ".join(products_text)

    append_row("Замовлення", [
        order_date,
        chat_id,
        full_name,
        phone,
        address,
        products_joined,
        total,
        need_contact,
        "",
        "Нове"
    ])

    clear_user_cart(chat_id)
    USER_STATES.pop(str(chat_id), None)

    notify_admin(
        full_name=full_name,
        phone=phone,
        address=address,
        products=products_joined,
        total=total,
        need_contact=need_contact,
        telegram_id=chat_id
    )

    if need_contact == "Так":
        final_text = "✅ Дякуємо! Менеджер скоро зв’яжеться з Вами 💛"
    else:
        final_text = "✅ Дякуємо! Замовлення прийнято, ми передали його в обробку 💛"

    send_message(chat_id, final_text, main_menu(is_admin(chat_id)))


def notify_admin(full_name, phone, address, products, total, need_contact, telegram_id):
    if not ADMIN_CHAT_ID:
        return

    text = (
        "🔔 <b>Нове замовлення!</b>\n\n"
        f"<b>ПІБ:</b> {full_name}\n"
        f"<b>Телефон:</b> {phone}\n"
        f"<b>Адреса:</b> {address}\n"
        f"<b>Товари:</b> {products}\n"
        f"<b>Сума:</b> {total} грн\n"
        f"<b>Потрібно зв’язатись:</b> {need_contact}\n"
        f"<b>Telegram ID:</b> {telegram_id}"
    )

    send_message(ADMIN_CHAT_ID, text)


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

    send_message(chat_id, text, main_menu(is_admin(chat_id)))


# =========================
# ADMIN CABINET
# =========================

def show_admin_cabinet(chat_id):
    if not is_admin(chat_id):
        send_message(chat_id, "Цей розділ доступний тільки адміністратору.", main_menu(False))
        return

    orders = get_records("Замовлення")

    new_orders = []
    total_sum = 0

    for order in orders:
        status = str(order.get("Статус")).strip().lower()
        if status == "нове":
            new_orders.append(order)
            try:
                total_sum += float(order.get("Сума") or 0)
            except:
                pass

    text = (
        "👑 <b>Кабінет</b>\n\n"
        f"🆕 Нові замовлення: <b>{len(new_orders)}</b>\n"
        f"💰 Сума нових замовлень: <b>{total_sum} грн</b>\n\n"
    )

    keyboard = {
        "inline_keyboard": [
            [inline_button("📋 Показати нові замовлення", "admin_new_orders")],
            [inline_button("💰 Сума замовлень", "admin_orders_sum")]
        ]
    }

    send_message(chat_id, text, keyboard)


def show_admin_new_orders(chat_id):
    if not is_admin(chat_id):
        return

    orders = get_records("Замовлення")
    new_orders = [o for o in orders if str(o.get("Статус")).strip().lower() == "нове"]

    if not new_orders:
        send_message(chat_id, "Нових замовлень немає ✅", main_menu(True))
        return

    for idx, order in enumerate(new_orders[-10:], start=1):
        text = (
            f"📦 <b>Нове замовлення #{idx}</b>\n\n"
            f"<b>Дата:</b> {order.get('Дата')}\n"
            f"<b>ПІБ:</b> {order.get('ПІБ')}\n"
            f"<b>Телефон:</b> {order.get('Телефон')}\n"
            f"<b>Адреса:</b> {order.get('Адреса доставки')}\n"
            f"<b>Товари:</b> {order.get('Товари')}\n"
            f"<b>Сума:</b> {order.get('Сума')} грн\n"
            f"<b>Потрібно зв’язатись:</b> {order.get('Потрібно зв’язатись')}\n"
            f"<b>Статус:</b> {order.get('Статус')}"
        )
        send_message(chat_id, text)


def show_admin_orders_sum(chat_id):
    if not is_admin(chat_id):
        return

    orders = get_records("Замовлення")
    total_all = 0
    total_new = 0

    for order in orders:
        try:
            value = float(order.get("Сума") or 0)
        except:
            value = 0

        total_all += value

        if str(order.get("Статус")).strip().lower() == "нове":
            total_new += value

    text = (
        "💰 <b>Сума замовлень</b>\n\n"
        f"Нові замовлення: <b>{total_new} грн</b>\n"
        f"Усі замовлення: <b>{total_all} грн</b>"
    )

    send_message(chat_id, text, main_menu(True))


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

        if text == "/start":
            start(chat_id)
        elif text == "/myid":
            show_my_id(chat_id)
        elif handle_order_state(chat_id, text, user):
            pass
        elif text == "📦 Каталог":
            show_categories(chat_id)
        elif text == "🗂 Категорії":
            show_categories(chat_id)
        elif text == "🔥 Акції":
            show_sales(chat_id)
        elif text == "🛒 Кошик":
            show_cart(chat_id)
        elif text == "✅ Замовити":
            start_order(chat_id)
        elif text == "🚚 Доставка і оплата":
            show_delivery_payment(chat_id)
        elif text == "👑 Кабінет":
            show_admin_cabinet(chat_id)
        else:
            send_message(chat_id, "Обери дію з меню 👇", main_menu(is_admin(chat_id)))

    if "callback_query" in data:
        callback = data["callback_query"]
        chat_id = callback["message"]["chat"]["id"]
        data_value = callback["data"]
        user = callback.get("from", {})

        if data_value.startswith("cat_"):
            category_id = data_value.replace("cat_", "")
            show_products_by_category(chat_id, category_id)

        elif data_value.startswith("choose_qty_"):
            product_id = data_value.replace("choose_qty_", "")
            show_qty_choice(chat_id, product_id)

        elif data_value.startswith("add_"):
            parts = data_value.split("_")
            if len(parts) >= 3:
                product_id = parts[1]
                qty = parts[2]
                add_to_cart(chat_id, product_id, qty)

        elif data_value == "order_now":
            start_order(chat_id)

        elif data_value == "clear_cart":
            clear_user_cart(chat_id)
            USER_STATES.pop(str(chat_id), None)
            send_message(chat_id, "🗑 Кошик очищено.", main_menu(is_admin(chat_id)))

        elif data_value == "delete_item_menu":
            show_delete_item_menu(chat_id)

        elif data_value.startswith("delete_cart_row_"):
            row_index = data_value.replace("delete_cart_row_", "")
            delete_cart_item(chat_id, row_index)

        elif data_value == "back_to_cart":
            show_cart(chat_id)

        elif data_value == "need_contact_yes":
            finish_order(chat_id, user, "Так")

        elif data_value == "need_contact_no":
            finish_order(chat_id, user, "Ні")

        elif data_value == "admin_new_orders":
            show_admin_new_orders(chat_id)

        elif data_value == "admin_orders_sum":
            show_admin_orders_sum(chat_id)

    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
