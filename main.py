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
        result.append({
            "row_index": i,
            "Дата": row[0] if len(row) > 0 else "",
            "Telegram ID": row[1] if len(row) > 1 else "",
            "ПІБ": row[2] if len(row) > 2 else "",
            "Телефон": row[3] if len(row) > 3 else "",
            "Адреса доставки": row[4] if len(row) > 4 else "",
            "Товари": row[5] if len(row) > 5 else "",
            "Сума": row[6] if len(row) > 6 else "",
            "Потрібно зв’язатись": row[7] if len(row) > 7 else "",
            "Коментар": row[8] if len(row) > 8 else "",
            "Статус": row[9] if len(row) > 9 else ""
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


def edit_callback_message(callback_message, text, keyboard=None):
    chat_id = callback_message["chat"]["id"]
    message_id = callback_message["message_id"]

    if "photo" in callback_message:
        edit_caption(chat_id, message_id, text, keyboard)
    else:
        edit_message(chat_id, message_id, text, keyboard)


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
        [{"text": "🛒 Кошик"}, {"text": "✅ Замовити"}],
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


def back_main_keyboard():
    return {
        "inline_keyboard": [
            [inline_button("📦 Каталог", "open_catalog"), inline_button("🔥 Акції", "open_sales")],
            [inline_button("🛒 Кошик", "open_cart")],
            [inline_button("🚚 Доставка і оплата", "open_delivery")]
        ]
    }


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


def show_main_inline(callback_message):
    edit_callback_message(
        callback_message,
        "🏠 <b>Головне меню</b>\n\nОберіть розділ 👇",
        back_main_keyboard()
    )


def show_categories(chat_id, callback_message=None):
    categories = get_records("Категорії")
    active_categories = [
        c for c in categories
        if str(c.get("Активна")).strip().lower() in ["так", "yes", "true", "1"]
    ]

    if not active_categories:
        text = "Поки немає активних категорій 😔"
        keyboard = {"inline_keyboard": [[inline_button("⬅️ Назад", "back_main")]]}

        if callback_message:
            edit_callback_message(callback_message, text, keyboard)
        else:
            send_message(chat_id, text, keyboard)
        return

    buttons = []
    for cat in active_categories:
        buttons.append([
            inline_button(
                f"📁 {cat.get('Назва категорії')}",
                f"cat_{cat.get('ID категорії')}"
            )
        ])

    buttons.append([inline_button("⬅️ Назад", "back_main")])

    text = "📦 <b>Каталог</b>\n\nОберіть категорію 👇"
    keyboard = {"inline_keyboard": buttons}

    if callback_message:
        edit_callback_message(callback_message, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)


def show_products_by_category(chat_id, category_id, callback_message=None):
    products = get_records("Товари")
    filtered = [
        p for p in products
        if str(p.get("ID категорії")) == str(category_id)
        and str(p.get("Активний")).strip().lower() in ["так", "yes", "true", "1"]
    ]

    if not filtered:
        text = "У цій категорії поки немає товарів 😔"
        keyboard = {"inline_keyboard": [[inline_button("⬅️ Назад до каталогу", "open_catalog")]]}

        if callback_message:
            edit_callback_message(callback_message, text, keyboard)
        else:
            send_message(chat_id, text, keyboard)
        return

    header = "📦 <b>Товари в категорії</b>\n\nОсь що є в наявності 👇"
    keyboard = {"inline_keyboard": [[inline_button("⬅️ Назад до каталогу", "open_catalog")]]}

    if callback_message:
        edit_callback_message(callback_message, header, keyboard)
    else:
        send_message(chat_id, header, keyboard)

    for product in filtered:
        send_product(chat_id, product)


def show_sales(chat_id, callback_message=None):
    products = get_records("Товари")
    sale_products = [
        p for p in products
        if str(p.get("Акція")).strip() != ""
        and str(p.get("Активний")).strip().lower() in ["так", "yes", "true", "1"]
    ]

    keyboard = {"inline_keyboard": [[inline_button("⬅️ Назад", "back_main")]]}

    if not sale_products:
        text = "Поки немає активних акцій 😔"

        if callback_message:
            edit_callback_message(callback_message, text, keyboard)
        else:
            send_message(chat_id, text, keyboard)
        return

    text = "🔥 <b>Акції</b>\n\nПоказую активні акційні товари 👇"

    if callback_message:
        edit_callback_message(callback_message, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)

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
            [inline_button("🛒 Додати в кошик", f"add_one_{product_id}")],
            [inline_button("⬅️ До каталогу", "open_catalog")]
        ]
    }

    if photo:
        send_photo(chat_id, photo, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)


def add_to_cart(chat_id, product_id, callback_message=None):
    products = get_records("Товари")
    product = None

    for p in products:
        if str(p.get("ID товару")) == str(product_id):
            product = p
            break

    if not product:
        text = "Товар не знайдено 😔"
        keyboard = {"inline_keyboard": [[inline_button("⬅️ Назад", "back_main")]]}

        if callback_message:
            edit_callback_message(callback_message, text, keyboard)
        else:
            send_message(chat_id, text, keyboard)
        return

    name = product.get("Назва товару")
    price = float(product.get("Ціна") or 0)

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
            [inline_button("🛒 Перейти в кошик", "open_cart")],
            [inline_button("⬅️ До каталогу", "open_catalog")]
        ]
    }

    if callback_message:
        edit_callback_message(callback_message, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)


def show_cart(chat_id, callback_message=None):
    items = find_user_cart_rows(chat_id)

    if not items:
        text = "Твій кошик поки порожній 🛒"
        keyboard = {"inline_keyboard": [[inline_button("⬅️ Назад", "back_main")]]}

        if callback_message:
            edit_callback_message(callback_message, text, keyboard)
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
    buttons.append([inline_button("⬅️ Назад", "back_main")])

    keyboard = {"inline_keyboard": buttons}

    if callback_message:
        edit_callback_message(callback_message, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)


def change_cart_qty(chat_id, row_index, delta, callback_message=None):
    rows = get_values("Кошик")

    try:
        row_index = int(row_index)
        row = rows[row_index - 1]
    except:
        text = "Не вдалося змінити кількість. Спробуйте ще раз."
        if callback_message:
            edit_callback_message(callback_message, text, {"inline_keyboard": [[inline_button("⬅️ Назад", "back_main")]]})
        else:
            send_message(chat_id, text, main_menu(is_admin(chat_id)))
        return

    if len(row) < 6 or str(row[0]) != str(chat_id):
        text = "Цей товар не знайдено у Вашому кошику."
        if callback_message:
            edit_callback_message(callback_message, text, {"inline_keyboard": [[inline_button("⬅️ Назад", "back_main")]]})
        else:
            send_message(chat_id, text, main_menu(is_admin(chat_id)))
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
        text = "Не вдалося видалити товар. Спробуйте ще раз."
        if callback_message:
            edit_callback_message(callback_message, text, {"inline_keyboard": [[inline_button("⬅️ Назад", "back_main")]]})
        else:
            send_message(chat_id, text, main_menu(is_admin(chat_id)))


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


def finish_order(chat_id, user, need_contact, callback_message=None):
    cart = get_user_cart(chat_id)

    if not cart:
        USER_STATES.pop(str(chat_id), None)
        text = "Кошик порожній, немає що замовляти 😔"
        if callback_message:
            edit_callback_message(callback_message, text, {"inline_keyboard": [[inline_button("⬅️ Назад", "back_main")]]})
        else:
            send_message(chat_id, text, main_menu(is_admin(chat_id)))
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

    keyboard = {"inline_keyboard": [[inline_button("⬅️ Назад у меню", "back_main")]]}

    if callback_message:
        edit_callback_message(callback_message, final_text, keyboard)
    else:
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


def show_delivery_payment(chat_id, callback_message=None):
    settings = get_records("Налаштування")

    if not settings:
        text = "Інформацію про доставку й оплату ще не додано."
        keyboard = {"inline_keyboard": [[inline_button("⬅️ Назад", "back_main")]]}

        if callback_message:
            edit_callback_message(callback_message, text, keyboard)
        else:
            send_message(chat_id, text, keyboard)
        return

    text = "🚚 <b>Доставка і оплата</b>\n\n"

    for row in settings:
        param = row.get("Параметр")
        value = row.get("Значення")
        text += f"<b>{param}:</b>\n{value}\n\n"

    keyboard = {"inline_keyboard": [[inline_button("⬅️ Назад", "back_main")]]}

    if callback_message:
        edit_callback_message(callback_message, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)


# =========================
# ADMIN CABINET
# =========================

def show_admin_cabinet(chat_id, callback_message=None):
    if not is_admin(chat_id):
        send_message(chat_id, "Цей розділ доступний тільки адміністратору.", main_menu(False))
        return

    orders = get_orders_with_rows()

    new_orders = []
    processed_orders = []
    total_new = 0
    total_processed = 0

    for order in orders:
        status = str(order.get("Статус")).strip().lower()

        try:
            value = float(order.get("Сума") or 0)
        except:
            value = 0

        if status == "нове":
            new_orders.append(order)
            total_new += value
        elif status == "опрацьовано":
            processed_orders.append(order)
            total_processed += value

    text = (
        "👑 <b>Кабінет</b>\n\n"
        f"🆕 Нові замовлення: <b>{len(new_orders)}</b>\n"
        f"💰 Сума нових: <b>{total_new} грн</b>\n\n"
        f"✅ Опрацьовані: <b>{len(processed_orders)}</b>\n"
        f"💰 Сума опрацьованих: <b>{total_processed} грн</b>"
    )

    keyboard = {
        "inline_keyboard": [
            [inline_button("🆕 Нові замовлення", "admin_new_orders")],
            [inline_button("✅ Опрацьовані", "admin_processed_orders")],
            [inline_button("💰 Сума замовлень", "admin_orders_sum")]
        ]
    }

    if callback_message:
        edit_callback_message(callback_message, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)


def show_admin_new_orders(chat_id, callback_message=None):
    if not is_admin(chat_id):
        return

    orders = get_orders_with_rows()
    new_orders = [o for o in orders if str(o.get("Статус")).strip().lower() == "нове"]

    if not new_orders:
        text = "Нових замовлень немає ✅"
        keyboard = {"inline_keyboard": [[inline_button("⬅️ Назад у кабінет", "admin_back")]]}

        if callback_message:
            edit_callback_message(callback_message, text, keyboard)
        else:
            send_message(chat_id, text, keyboard)
        return

    # Щоб чат не засмічувався, показуємо тільки останнє нове замовлення в тому самому повідомленні.
    order = new_orders[-1]

    text = (
        "🆕 <b>Останнє нове замовлення</b>\n\n"
        f"<b>Дата:</b> {order.get('Дата')}\n"
        f"<b>ПІБ:</b> {order.get('ПІБ')}\n"
        f"<b>Телефон:</b> {order.get('Телефон')}\n"
        f"<b>Адреса:</b> {order.get('Адреса доставки')}\n"
        f"<b>Товари:</b> {order.get('Товари')}\n"
        f"<b>Сума:</b> {order.get('Сума')} грн\n"
        f"<b>Потрібно зв’язатись:</b> {order.get('Потрібно зв’язатись')}\n"
        f"<b>Статус:</b> {order.get('Статус')}\n\n"
        f"Усього нових: <b>{len(new_orders)}</b>"
    )

    keyboard = {
        "inline_keyboard": [
            [inline_button("✅ Опрацьовано", f"mark_processed_{order.get('row_index')}")],
            [inline_button("🔄 Оновити", "admin_new_orders")],
            [inline_button("⬅️ Назад у кабінет", "admin_back")]
        ]
    }

    if callback_message:
        edit_callback_message(callback_message, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)


def show_admin_processed_orders(chat_id, callback_message=None):
    if not is_admin(chat_id):
        return

    orders = get_orders_with_rows()
    processed_orders = [o for o in orders if str(o.get("Статус")).strip().lower() == "опрацьовано"]

    if not processed_orders:
        text = "Опрацьованих замовлень поки немає."
        keyboard = {"inline_keyboard": [[inline_button("⬅️ Назад у кабінет", "admin_back")]]}

        if callback_message:
            edit_callback_message(callback_message, text, keyboard)
        else:
            send_message(chat_id, text, keyboard)
        return

    order = processed_orders[-1]

    text = (
        "✅ <b>Останнє опрацьоване замовлення</b>\n\n"
        f"<b>Дата:</b> {order.get('Дата')}\n"
        f"<b>ПІБ:</b> {order.get('ПІБ')}\n"
        f"<b>Телефон:</b> {order.get('Телефон')}\n"
        f"<b>Адреса:</b> {order.get('Адреса доставки')}\n"
        f"<b>Товари:</b> {order.get('Товари')}\n"
        f"<b>Сума:</b> {order.get('Сума')} грн\n"
        f"<b>Потрібно зв’язатись:</b> {order.get('Потрібно зв’язатись')}\n"
        f"<b>Статус:</b> {order.get('Статус')}\n\n"
        f"Усього опрацьованих: <b>{len(processed_orders)}</b>"
    )

    keyboard = {
        "inline_keyboard": [
            [inline_button("🔄 Оновити", "admin_processed_orders")],
            [inline_button("⬅️ Назад у кабінет", "admin_back")]
        ]
    }

    if callback_message:
        edit_callback_message(callback_message, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)


def mark_order_processed(chat_id, row_index, callback_message=None):
    if not is_admin(chat_id):
        return

    try:
        update_cell("Замовлення", int(row_index), 10, "Опрацьовано")

        text = (
            "✅ <b>Замовлення опрацьовано</b>\n\n"
            "Статус у таблиці змінено на: <b>Опрацьовано</b>"
        )

        keyboard = {
            "inline_keyboard": [
                [inline_button("🆕 Оновити нові замовлення", "admin_new_orders")],
                [inline_button("✅ Перейти в опрацьовані", "admin_processed_orders")],
                [inline_button("⬅️ Назад у кабінет", "admin_back")]
            ]
        }

        if callback_message:
            edit_callback_message(callback_message, text, keyboard)
        else:
            send_message(chat_id, text, keyboard)

    except Exception:
        send_message(chat_id, "Не вдалося змінити статус. Спробуйте ще раз.", main_menu(True))


def show_admin_orders_sum(chat_id, callback_message=None):
    if not is_admin(chat_id):
        return

    orders = get_orders_with_rows()
    total_all = 0
    total_new = 0
    total_processed = 0

    for order in orders:
        try:
            value = float(order.get("Сума") or 0)
        except:
            value = 0

        total_all += value

        status = str(order.get("Статус")).strip().lower()

        if status == "нове":
            total_new += value
        elif status == "опрацьовано":
            total_processed += value

    text = (
        "💰 <b>Сума замовлень</b>\n\n"
        f"🆕 Нові: <b>{total_new} грн</b>\n"
        f"✅ Опрацьовані: <b>{total_processed} грн</b>\n"
        f"📦 Усі разом: <b>{total_all} грн</b>"
    )

    keyboard = {"inline_keyboard": [[inline_button("⬅️ Назад у кабінет", "admin_back")]]}

    if callback_message:
        edit_callback_message(callback_message, text, keyboard)
    else:
        send_message(chat_id, text, keyboard)


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
        callback_id = callback.get("id")
        callback_message = callback["message"]
        chat_id = callback_message["chat"]["id"]
        data_value = callback["data"]
        user = callback.get("from", {})

        if callback_id:
            answer_callback(callback_id)

        if data_value.startswith("cat_"):
            category_id = data_value.replace("cat_", "")
            show_products_by_category(chat_id, category_id, callback_message)

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

        elif data_value == "order_now":
            start_order(chat_id)

        elif data_value == "clear_cart":
            clear_user_cart(chat_id)
            USER_STATES.pop(str(chat_id), None)
            edit_callback_message(
                callback_message,
                "🗑 Кошик очищено.",
                {"inline_keyboard": [[inline_button("⬅️ Назад", "back_main")]]}
            )

        elif data_value.startswith("delete_cart_row_"):
            row_index = data_value.replace("delete_cart_row_", "")
            delete_cart_item(chat_id, row_index, callback_message)

        elif data_value == "need_contact_yes":
            finish_order(chat_id, user, "Так", callback_message)

        elif data_value == "need_contact_no":
            finish_order(chat_id, user, "Ні", callback_message)

        elif data_value == "open_catalog":
            show_categories(chat_id, callback_message)

        elif data_value == "open_sales":
            show_sales(chat_id, callback_message)

        elif data_value == "open_cart":
            show_cart(chat_id, callback_message)

        elif data_value == "open_delivery":
            show_delivery_payment(chat_id, callback_message)

        elif data_value == "back_main":
            show_main_inline(callback_message)

        elif data_value == "admin_new_orders":
            show_admin_new_orders(chat_id, callback_message)

        elif data_value == "admin_processed_orders":
            show_admin_processed_orders(chat_id, callback_message)

        elif data_value.startswith("mark_processed_"):
            row_index = data_value.replace("mark_processed_", "")
            mark_order_processed(chat_id, row_index, callback_message)

        elif data_value == "admin_orders_sum":
            show_admin_orders_sum(chat_id, callback_message)

        elif data_value == "admin_back":
            show_admin_cabinet(chat_id, callback_message)

    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
