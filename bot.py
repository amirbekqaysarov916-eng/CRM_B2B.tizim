
"""
SAGBAN CRM — Telegram B2B Bot
Python 3.11+ / aiogram 3 / SQLite

Funksiyalar:
1. Login kodi orqali kirish
2. Mahsulotlar katalogi
3. KG bo'yicha savat
4. Savatni o'zgartirish
5. Buyurtma yuborish
6. Buyurtmalar tarixi
7. Qarzdorlik
8. Yangiliklar
9. Profil

Barcha callback tugmalarda foydalanuvchi
callback.from_user.id orqali tekshiriladi.
"""

import asyncio
import logging
import os
import sqlite3
from datetime import datetime
from aiogram.exceptions import TelegramNetworkError
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

from database import get_db, init_db

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("SAGBAN")
router = Router()

STATUS = {
    "NEW": "Yangi",
    "ACCEPTED": "Qabul qilindi",
    "PREPARING": "Tayyorlanmoqda",
    "READY": "Tayyor",
    "ON_THE_WAY": "Yo'lda",
    "DELIVERED": "Yetkazildi",
    "CANCELLED": "Bekor qilindi",
}


# ==========================================
# FSM
# ==========================================

class Login(StatesGroup):
    code = State()


class OrderFlow(StatesGroup):
    quantity = State()
    note = State()


# ==========================================
# FORMATLASH
# ==========================================

def money(value):
    return f"{int(value or 0):,}".replace(",", " ")


def kg(grams):
    value = Decimal(int(grams)) / Decimal(1000)
    return format(value.normalize(), "f")


def parse_kg(raw):
    try:
        value = Decimal(str(raw).strip().replace(",", "."))

        if not value.is_finite() or not (0 < value <= 100000):
            return None

        grams = int(
            (value * 1000).quantize(
                Decimal("1"),
                rounding=ROUND_HALF_UP
            )
        )

        return grams if grams > 0 else None

    except (InvalidOperation, ValueError):
        return None


def subtotal(grams, price):
    return (int(grams) * int(price) + 500) // 1000


def menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="Mahsulotlar"),
                KeyboardButton(text="Savat"),
            ],
            [
                KeyboardButton(text="Buyurtmalarim"),
                KeyboardButton(text="Qarzdorlik"),
            ],
            [
                KeyboardButton(text="Yangiliklar"),
                KeyboardButton(text="Profil"),
            ],
        ],
        resize_keyboard=True,
    )


def inline(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def btn(label, data):
    return InlineKeyboardButton(
        text=label,
        callback_data=data
    )


# ==========================================
# DATABASE — CART TABLE
# ==========================================

def setup_bot_db():
    init_db()

    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS bot_cart (
                customer_id INTEGER NOT NULL
                    REFERENCES customers(id)
                    ON DELETE CASCADE,
                product_id INTEGER NOT NULL
                    REFERENCES products(id),
                quantity_grams INTEGER NOT NULL
                    CHECK(quantity_grams > 0),
                PRIMARY KEY(customer_id, product_id)
            )
        """)

        db.commit()


# ==========================================
# DATABASE — CUSTOMERS
# ==========================================

def get_customer(telegram_id):
    with get_db() as db:
        row = db.execute(
            """
            SELECT *
            FROM customers
            WHERE telegram_id = ?
            """,
            (telegram_id,)
        ).fetchone()

        return dict(row) if row else None


def login_customer(telegram_id, code):
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")

        row = db.execute(
            """
            SELECT *
            FROM customers
            WHERE UPPER(login_code) = ?
            """,
            (code.upper(),)
        ).fetchone()

        if not row:
            return "INVALID", None

        customer = dict(row)

        if not customer["is_active"]:
            return "INACTIVE", None

        linked_id = customer["telegram_id"]

        if linked_id is not None and linked_id != telegram_id:
            return "LINKED", None

        other = db.execute(
            """
            SELECT id
            FROM customers
            WHERE telegram_id = ?
              AND id != ?
            """,
            (telegram_id, customer["id"])
        ).fetchone()

        if other:
            return "OTHER", None

        db.execute(
            """
            UPDATE customers
            SET telegram_id = ?
            WHERE id = ?
            """,
            (telegram_id, customer["id"])
        )

        db.commit()

        return "OK", customer


# ==========================================
# DATABASE — PRODUCTS
# ==========================================

def get_products():
    with get_db() as db:
        rows = db.execute(
            """
            SELECT *
            FROM products
            WHERE is_active = 1
            ORDER BY name
            """
        ).fetchall()

        return [dict(row) for row in rows]


def get_product(product_id):
    with get_db() as db:
        row = db.execute(
            """
            SELECT *
            FROM products
            WHERE id = ?
              AND is_active = 1
            """,
            (product_id,)
        ).fetchone()

        return dict(row) if row else None


# ==========================================
# DATABASE — CART
# ==========================================

def get_cart(customer_id):
    with get_db() as db:
        rows = db.execute(
            """
            SELECT
                c.product_id,
                c.quantity_grams,
                p.name,
                p.price_per_kg,
                p.is_active
            FROM bot_cart c
            JOIN products p ON p.id = c.product_id
            WHERE c.customer_id = ?
            ORDER BY p.name
            """,
            (customer_id,)
        ).fetchall()

        return [dict(row) for row in rows]


def add_to_cart(customer_id, product_id, grams):
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")

        product = db.execute(
            """
            SELECT id
            FROM products
            WHERE id = ? AND is_active = 1
            """,
            (product_id,)
        ).fetchone()

        if not product:
            raise ValueError("Mahsulot mavjud emas")

        db.execute(
            """
            INSERT INTO bot_cart (
                customer_id,
                product_id,
                quantity_grams
            )
            VALUES (?, ?, ?)
            ON CONFLICT(customer_id, product_id)
            DO UPDATE SET
                quantity_grams =
                    quantity_grams + excluded.quantity_grams
            """,
            (customer_id, product_id, grams)
        )

        db.commit()


def remove_from_cart(customer_id, product_id):
    with get_db() as db:
        db.execute(
            """
            DELETE FROM bot_cart
            WHERE customer_id = ?
              AND product_id = ?
            """,
            (customer_id, product_id)
        )

        db.commit()


def clear_cart(customer_id):
    with get_db() as db:
        db.execute(
            """
            DELETE FROM bot_cart
            WHERE customer_id = ?
            """,
            (customer_id,)
        )

        db.commit()


# ==========================================
# DATABASE — ORDERS
# ==========================================

def create_order(customer_id, note):
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")

        customer = db.execute(
            """
            SELECT id
            FROM customers
            WHERE id = ? AND is_active = 1
            """,
            (customer_id,)
        ).fetchone()

        if not customer:
            raise ValueError("Mijoz faol emas")

        items = db.execute(
            """
            SELECT
                c.product_id,
                c.quantity_grams,
                p.name,
                p.price_per_kg,
                p.is_active
            FROM bot_cart c
            JOIN products p ON p.id = c.product_id
            WHERE c.customer_id = ?
            """,
            (customer_id,)
        ).fetchall()

        if not items:
            raise ValueError("Savat bo'sh")

        if len(items) > 100:
            raise ValueError("Savatda mahsulotlar juda ko'p")

        for item in items:
            if not item["is_active"]:
                raise ValueError(
                    f"{item['name']} hozir mavjud emas"
                )

        cursor = db.execute(
            """
            INSERT INTO orders (
                customer_id,
                status,
                note,
                created_at
            )
            VALUES (?, 'NEW', ?, ?)
            """,
            (
                customer_id,
                note[:500],
                datetime.now().isoformat()
            )
        )

        order_id = cursor.lastrowid

        for item in items:
            db.execute(
                """
                INSERT INTO order_items (
                    order_id,
                    product_id,
                    product_name,
                    ordered_grams,
                    price_per_kg
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    item["product_id"],
                    item["name"],
                    item["quantity_grams"],
                    item["price_per_kg"]
                )
            )

        db.execute(
            """
            DELETE FROM bot_cart
            WHERE customer_id = ?
            """,
            (customer_id,)
        )

        db.commit()

        return order_id


def get_orders(customer_id):
    with get_db() as db:
        rows = db.execute(
            """
            SELECT *
            FROM orders
            WHERE customer_id = ?
            ORDER BY id DESC
            LIMIT 20
            """,
            (customer_id,)
        ).fetchall()

        return [dict(row) for row in rows]


def get_order(order_id, customer_id):
    with get_db() as db:
        order = db.execute(
            """
            SELECT *
            FROM orders
            WHERE id = ?
              AND customer_id = ?
            """,
            (order_id, customer_id)
        ).fetchone()

        if not order:
            return None

        items = db.execute(
            """
            SELECT *
            FROM order_items
            WHERE order_id = ?
            """,
            (order_id,)
        ).fetchall()

        return (
            dict(order),
            [dict(item) for item in items]
        )


def get_debt(customer_id):
    with get_db() as db:
        row = db.execute(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM ledger
            WHERE customer_id = ?
            """,
            (customer_id,)
        ).fetchone()

        return int(row[0])


def get_news():
    with get_db() as db:
        rows = db.execute(
            """
            SELECT *
            FROM news
            ORDER BY id DESC
            LIMIT 10
            """
        ).fetchall()

        return [dict(row) for row in rows]


# ==========================================
# AUTH CHECK
# ==========================================

async def check_user(user_id):
    customer = await asyncio.to_thread(
        get_customer,
        user_id
    )

    if not customer:
        return None, "Avval /start orqali tizimga kiring."

    if not customer["is_active"]:
        return None, "Hisobingiz faol emas."

    return customer, None


async def message_customer(message):
    customer, error = await check_user(
        message.from_user.id
    )

    if error:
        await message.answer(error)

    return customer


async def callback_customer(callback):
    """
    MUHIM:
    callback.message.from_user.id emas!
    callback.from_user.id ishlatiladi.
    """

    customer, error = await check_user(
        callback.from_user.id
    )

    if error:
        await callback.answer(
            error,
            show_alert=True
        )
        return None

    return customer


# ==========================================
# START
# ==========================================

@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()

    customer = await asyncio.to_thread(
        get_customer,
        message.from_user.id
    )

    if customer and customer["is_active"]:
        await message.answer(
            f"Xush kelibsiz, {customer['name']}!\n\n"
            "SAGBAN mahsulotlariga buyurtma "
            "berishingiz mumkin.",
            reply_markup=menu()
        )
        return

    await state.set_state(Login.code)

    await message.answer(
        "SAGBAN B2B tizimiga xush kelibsiz!\n\n"
        "Administrator bergan login kodini kiriting:"
    )


@router.message(Login.code)
async def login_handler(
    message: Message,
    state: FSMContext
):
    code = (message.text or "").strip().upper()

    if not code:
        await message.answer("Login kodini kiriting.")
        return

    result, customer = await asyncio.to_thread(
        login_customer,
        message.from_user.id,
        code
    )

    errors = {
        "INVALID": "Login kodi noto'g'ri.",
        "INACTIVE": "Mijoz hisobi faol emas.",
        "LINKED": (
            "Bu kod boshqa Telegram "
            "akkauntiga ulangan."
        ),
        "OTHER": (
            "Sizning Telegram akkauntingiz "
            "boshqa mijozga ulangan."
        ),
    }

    if result != "OK":
        await message.answer(errors.get(result, "Xatolik"))
        return

    await state.clear()

    await message.answer(
        f"Muvaffaqiyatli kirdingiz!\n\n"
        f"Mijoz: {customer['name']}",
        reply_markup=menu()
    )




# ==========================================
# MENYU BOSILGANDA FSM HOLATINI TOZALASH
# ==========================================

MAIN_MENU_BUTTONS = {
    "Mahsulotlar",
    "Savat",
    "Buyurtmalarim",
    "Qarzdorlik",
    "Yangiliklar",
    "Profil",
}


@router.message(
    OrderFlow.quantity,
    F.text.in_(MAIN_MENU_BUTTONS)
)
@router.message(
    OrderFlow.note,
    F.text.in_(MAIN_MENU_BUTTONS)
)
async def switch_menu(
    message: Message,
    state: FSMContext
):
    await state.clear()

    customer = await message_customer(message)

    if not customer:
        return

    choice = message.text

    if choice == "Mahsulotlar":
        await products_menu(message)

    elif choice == "Savat":
        await cart_menu(message)

    elif choice == "Buyurtmalarim":
        await orders_menu(message)

    elif choice == "Qarzdorlik":
        await debt_menu(message)

    elif choice == "Yangiliklar":
        await news_menu(message)

    elif choice == "Profil":
        await profile_menu(message)





# ==========================================
# PRODUCT CATALOG
# ==========================================


@router.message(F.text == "Mahsulotlar")
async def products_menu(message: Message):
    customer = await message_customer(message)

    if not customer:
        return

    products = await asyncio.to_thread(get_products)

    if not products:
        await message.answer(
            "Hozircha mahsulotlar yo'q."
        )
        return

    rows = []

    for product in products:
        rows.append([
            btn(
                f"{product['name']} — "
                f"{money(product['price_per_kg'])} so'm/kg",
                f"p:{product['id']}"
            )
        ])

    await message.answer(
        "SAGBAN mahsulotlar katalogi\n\n"
        "Mahsulotni tanlang:",
        reply_markup=inline(rows)
    )


@router.callback_query(F.data.startswith("p:"))
async def select_product(
    callback: CallbackQuery,
    state: FSMContext
):
    customer = await callback_customer(callback)

    if not customer:
        return

    try:
        product_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Noto'g'ri mahsulot")
        return

    product = await asyncio.to_thread(
        get_product,
        product_id
    )

    if not product:
        await callback.answer(
            "Mahsulot topilmadi",
            show_alert=True
        )
        return

    await state.set_state(OrderFlow.quantity)

    await state.update_data(
        selected_product=product_id
    )

    await callback.answer()

    await callback.message.answer(
        f"Mahsulot: {product['name']}\n"
        f"Narxi: {money(product['price_per_kg'])} so'm/kg\n\n"
        "Necha kilogramm kerak?\n"
        "Masalan: 2.5\n\n"
        "Bekor qilish: /cancel"
    )


# ==========================================
# QUANTITY
# ==========================================

@router.message(OrderFlow.quantity)
async def quantity_handler(
    message: Message,
    state: FSMContext
):
    customer = await message_customer(message)

    if not customer:
        return

    grams = parse_kg(message.text or "")

    if grams is None:
        await message.answer(
            "KG noto'g'ri.\n"
            "Masalan: 1.5 yoki 3"
        )
        return

    data = await state.get_data()
    product_id = data.get("selected_product")

    if not product_id:
        await state.clear()
        await message.answer(
            "Mahsulotni qayta tanlang."
        )
        return

    product = await asyncio.to_thread(
        get_product,
        product_id
    )

    if not product:
        await state.clear()
        await message.answer(
            "Mahsulot mavjud emas."
        )
        return

    try:
        await asyncio.to_thread(
            add_to_cart,
            customer["id"],
            product_id,
            grams
        )
    except (ValueError, sqlite3.Error) as error:
        log.error("Savat xatosi: %s", error)
        await message.answer(
            "Savatga qo'shib bo'lmadi."
        )
        return

    await state.clear()

    await message.answer(
        "Savatga qo'shildi!\n\n"
        f"Mahsulot: {product['name']}\n"
        f"Miqdor: {kg(grams)} kg\n"
        f"Summa: "
        f"{money(subtotal(grams, product['price_per_kg']))} so'm",
        reply_markup=inline([
            [btn("Savatni ochish", "cart")],
            [btn("Yana mahsulot tanlash", "catalog")]
        ])
    )


# ==========================================
# CART
# ==========================================

async def display_cart(message, customer_id):
    items = await asyncio.to_thread(
        get_cart,
        customer_id
    )

    if not items:
        await message.answer(
            "Savatingiz bo'sh.\n\n"
            "Mahsulotlar bo'limidan tanlang."
        )
        return

    lines = ["SAGBAN — SAVAT\n"]

    total = 0
    rows = []

    for index, item in enumerate(items, start=1):
        amount = subtotal(
            item["quantity_grams"],
            item["price_per_kg"]
        )

        total += amount

        lines.append(
            f"{index}. {item['name']}\n"
            f"   {kg(item['quantity_grams'])} kg × "
            f"{money(item['price_per_kg'])} so'm\n"
            f"   = {money(amount)} so'm\n"
        )

        rows.append([
            btn(
                f"O'chirish: {item['name']}",
                f"remove:{item['product_id']}"
            )
        ])

    lines.append(
        f"\nTaxminiy jami: {money(total)} so'm"
    )

    lines.append(
        "\nYakuniy summa real KG bo'yicha hisoblanadi."
    )

    rows.extend([
        [btn("Buyurtma berish", "checkout")],
        [btn("Yana mahsulot qo'shish", "catalog")],
        [btn("Savatni tozalash", "clear")],
    ])

    await message.answer(
        "\n".join(lines),
        reply_markup=inline(rows)
    )


@router.message(F.text == "Savat")
async def cart_menu(message: Message):
    customer = await message_customer(message)

    if customer:
        await display_cart(
            message,
            customer["id"]
        )


@router.callback_query(F.data == "cart")
async def cart_callback(callback: CallbackQuery):
    customer = await callback_customer(callback)

    if not customer:
        return

    await callback.answer()

    await display_cart(
        callback.message,
        customer["id"]
    )


@router.callback_query(F.data == "catalog")
async def catalog_callback(callback: CallbackQuery):
    customer = await callback_customer(callback)

    if not customer:
        return

    await callback.answer()

    products = await asyncio.to_thread(get_products)

    if not products:
        await callback.message.answer(
            "Mahsulotlar mavjud emas."
        )
        return

    rows = [
        [
            btn(
                f"{p['name']} — "
                f"{money(p['price_per_kg'])} so'm/kg",
                f"p:{p['id']}"
            )
        ]
        for p in products
    ]

    await callback.message.answer(
        "Mahsulotni tanlang:",
        reply_markup=inline(rows)
    )


@router.callback_query(F.data.startswith("remove:"))
async def remove_item(callback: CallbackQuery):
    customer = await callback_customer(callback)

    if not customer:
        return

    try:
        product_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Noto'g'ri mahsulot")
        return

    await asyncio.to_thread(
        remove_from_cart,
        customer["id"],
        product_id
    )

    await callback.answer("Mahsulot o'chirildi")

    await display_cart(
        callback.message,
        customer["id"]
    )


@router.callback_query(F.data == "clear")
async def clear_callback(callback: CallbackQuery):
    customer = await callback_customer(callback)

    if not customer:
        return

    await asyncio.to_thread(
        clear_cart,
        customer["id"]
    )

    await callback.answer("Savat tozalandi")

    await callback.message.answer(
        "Savat tozalandi.",
        reply_markup=menu()
    )


# ==========================================
# CHECKOUT
# ==========================================

@router.callback_query(F.data == "checkout")
async def checkout_callback(
    callback: CallbackQuery,
    state: FSMContext
):
    customer = await callback_customer(callback)

    if not customer:
        return

    items = await asyncio.to_thread(
        get_cart,
        customer["id"]
    )

    if not items:
        await callback.answer(
            "Savat bo'sh",
            show_alert=True
        )
        return

    await state.set_state(OrderFlow.note)

    await callback.answer()

    await callback.message.answer(
        "Buyurtma uchun izoh yozing.\n\n"
        "Masalan: Ertalab yetkazib bering.\n\n"
        "Izoh bo'lmasa, - yuboring.\n"
        "Bekor qilish: /cancel"
    )


@router.message(OrderFlow.note)
async def note_handler(
    message: Message,
    state: FSMContext
):
    customer = await message_customer(message)

    if not customer:
        return

    note = (message.text or "").strip()

    if note == "-":
        note = ""

    if len(note) > 500:
        await message.answer(
            "Izoh 500 belgidan oshmasin."
        )
        return

    try:
        order_id = await asyncio.to_thread(
            create_order,
            customer["id"],
            note
        )

    except (ValueError, sqlite3.Error) as error:
        log.error("Buyurtma xatosi: %s", error)

        await message.answer(
            "Buyurtma saqlanmadi.\n"
            "Savatni tekshiring yoki "
            "administratorga murojaat qiling."
        )
        return

    await state.clear()

    await message.answer(
        "BUYURTMA QABUL QILINDI!\n\n"
        f"Buyurtma: #{order_id}\n"
        f"Mijoz: {customer['name']}\n"
        "Holati: Yangi\n\n"
        "Buyurtmangiz SAGBAN admin paneliga "
        "yuborildi.\n\n"
        "Yakuniy KG va summa mahsulot "
        "tortilgandan keyin belgilanadi.",
        reply_markup=menu()
    )


# ==========================================
# ORDERS HISTORY
# ==========================================

@router.message(F.text == "Buyurtmalarim")
async def orders_menu(message: Message):
    customer = await message_customer(message)

    if not customer:
        return

    orders = await asyncio.to_thread(
        get_orders,
        customer["id"]
    )

    if not orders:
        await message.answer(
            "Hali buyurtmalar yo'q."
        )
        return

    rows = []

    for order in orders:
        rows.append([
            btn(
                f"#{order['id']} — "
                f"{STATUS.get(order['status'], order['status'])}",
                f"order:{order['id']}"
            )
        ])

    await message.answer(
        "Buyurtmalaringiz:",
        reply_markup=inline(rows)
    )


@router.callback_query(F.data.startswith("order:"))
async def order_callback(callback: CallbackQuery):
    customer = await callback_customer(callback)

    if not customer:
        return

    try:
        order_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Noto'g'ri buyurtma")
        return

    result = await asyncio.to_thread(
        get_order,
        order_id,
        customer["id"]
    )

    if not result:
        await callback.answer(
            "Buyurtma topilmadi",
            show_alert=True
        )
        return

    order, items = result

    lines = [
        f"BUYURTMA #{order['id']}",
        f"Holati: {STATUS.get(order['status'], order['status'])}",
        ""
    ]

    total = 0

    for item in items:
        total += subtotal(
            item["ordered_grams"],
            item["price_per_kg"]
        )

        actual = (
            kg(item["actual_grams"]) + " kg"
            if item["actual_grams"] is not None
            else "Kiritilmagan"
        )

        lines.extend([
            item["product_name"],
            f"Buyurtma: {kg(item['ordered_grams'])} kg",
            f"Real KG: {actual}",
            ""
        ])

    lines.append(
        f"Taxminiy summa: {money(total)} so'm"
    )

    if order["final_total"] is not None:
        lines.append(
            f"Yakuniy summa: "
            f"{money(order['final_total'])} so'm"
        )

    if order["note"]:
        lines.append(
            f"Izoh: {order['note']}"
        )

    await callback.answer()

    await callback.message.answer(
        "\n".join(lines)
    )


# ==========================================
# DEBT
# ==========================================

@router.message(F.text == "Qarzdorlik")
async def debt_menu(message: Message):
    customer = await message_customer(message)

    if not customer:
        return

    debt = await asyncio.to_thread(
        get_debt,
        customer["id"]
    )

    await message.answer(
        "SAGBAN — HISOB-KITOB\n\n"
        f"Mijoz: {customer['name']}\n"
        f"Balans: {money(debt)} so'm\n"
        f"Kredit limiti: "
        f"{money(customer['credit_limit'])} so'm\n\n"
        "Musbat balans — qarzdorlik.\n"
        "Manfiy balans — oldindan to'lov."
    )


# ==========================================
# NEWS
# ==========================================

@router.message(F.text == "Yangiliklar")
async def news_menu(message: Message):
    customer = await message_customer(message)

    if not customer:
        return

    news = await asyncio.to_thread(get_news)

    if not news:
        await message.answer(
            "Hozircha yangiliklar yo'q."
        )
        return

    for item in news:
        await message.answer(
            f"{item['title']}\n\n"
            f"{item['message']}"
        )


# ==========================================
# PROFILE
# ==========================================

@router.message(F.text == "Profil")
async def profile_menu(message: Message):
    customer = await message_customer(message)

    if not customer:
        return

    await message.answer(
        "SAGBAN — MIJOZ PROFILI\n\n"
        f"Nomi: {customer['name']}\n"
        f"Telefon: {customer['phone'] or '—'}\n"
        f"Manzil: {customer['address'] or '—'}\n"
        f"Mijoz ID: {customer['id']}\n\n"
        "Ma'lumotlarni o'zgartirish uchun "
        "administratorga murojaat qiling."
    )


# ==========================================
# CANCEL
# ==========================================

@router.message(Command("cancel"))
async def cancel(
    message: Message,
    state: FSMContext
):
    await state.clear()

    await message.answer(
        "Amal bekor qilindi.\n"
        "Savatingizdagi mahsulotlar saqlangan.",
        reply_markup=menu()
    )


# ==========================================
# FALLBACK
# ==========================================

@router.message()
async def fallback(message: Message):
    customer = await message_customer(message)

    if not customer:
        return

    await message.answer(
        "Quyidagi menyudan bo'lim tanlang.",
        reply_markup=menu()
    )


# ==========================================
# MAIN
# ==========================================

async def main():
    if not TOKEN or TOKEN == "TOKENNI_SHU_YERGA_QOYING":
        raise RuntimeError(
            "BOT_TOKEN .env faylida to'g'ri kiritilmagan."
        )

    setup_bot_db()

    bot = Bot(token=TOKEN)

    dp = Dispatcher()
    dp.include_router(router)

    
    try:
            await bot.delete_webhook(
                drop_pending_updates=False
            )

            log.info("SAGBAN Telegram bot ishga tushmoqda...")

            await dp.start_polling(bot)

    finally:
            await bot.session.close()


# ==========================================
# TELEGRAM BOT AUTO RECONNECT
# ==========================================

if __name__ == "__main__":
    import time
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    while True:
        try:
            logging.info("SAGBAN Telegram bot ishga tushmoqda...")

            asyncio.run(main())

            logging.warning(
                "Bot odatiy tarzda to'xtadi."
            )
            break

        except (
            TelegramNetworkError,
            ConnectionError,
            OSError,
            TimeoutError,
        ) as error:
            logging.error(
                "Telegramga ulanish uzildi: %s",
                error,
            )

            logging.info(
                "10 soniyadan keyin qayta ulanadi..."
            )

            time.sleep(10)

        except KeyboardInterrupt:
            logging.info("Bot to'xtatildi.")
            break

        except Exception:
            logging.exception(
                "Botda kutilmagan xatolik yuz berdi"
            )
            break

