
"""
SAGBAN CRM — FastAPI Backend v2
================================
Admin panel, mijozlar, mahsulotlar, ombor,
buyurtmalar, real KG, qarzdorlik, to'lovlar,
Telegram xabarnomalari, yangiliklar va nakladnoy.

Python 3.11+
FastAPI + SQLite + Telegram Bot API

Muhim:
- Pul integer UZS shaklida saqlanadi.
- Og'irlik integer gramm shaklida saqlanadi.
- Telegram xabarlari SQLite navbatiga yoziladi.
- Yetkazilgan buyurtma ledger'ga bir marta yoziladi.
- Mavjud database.py bilan ishlaydi.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import sqlite3
import urllib.error
import urllib.request

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from database import get_db, init_db


# ==========================================
# CONFIGURATION
# ==========================================

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0") == "1"

SESSION_HOURS = 12

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("sagban")

STATUSES = (
    "NEW",
    "ACCEPTED",
    "PREPARING",
    "READY",
    "ON_THE_WAY",
    "DELIVERED",
    "CANCELLED",
)

STATUS_NAMES = {
    "NEW": "Yangi",
    "ACCEPTED": "Qabul qilindi",
    "PREPARING": "Tayyorlanmoqda",
    "READY": "Tayyor",
    "ON_THE_WAY": "Yo'lda",
    "DELIVERED": "Yetkazildi",
    "CANCELLED": "Bekor qilindi",
}

ALLOWED_NEXT = {
    "NEW": {"ACCEPTED", "CANCELLED"},
    "ACCEPTED": {"PREPARING", "CANCELLED"},
    "PREPARING": {"READY", "CANCELLED"},
    "READY": {"ON_THE_WAY", "CANCELLED"},
    "ON_THE_WAY": {"DELIVERED", "CANCELLED"},
    "DELIVERED": set(),
    "CANCELLED": set(),
}


# ==========================================
# HELPERS
# ==========================================

def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def rowdict(row):
    return dict(row) if row else None


def format_money(value):
    return f"{int(value or 0):,}".replace(",", " ")


def format_kg(value):
    result = Decimal(int(value or 0)) / 1000
    return format(result.normalize(), "f")


def grams(value: Any) -> int:
    try:
        number = Decimal(str(value))

        if (
            not number.is_finite()
            or number <= 0
            or number > 100000
        ):
            raise ValueError

        result = int(
            (number * 1000).quantize(
                Decimal("1"),
                rounding=ROUND_HALF_UP,
            )
        )

        if result <= 0:
            raise ValueError

        return result

    except (ValueError, InvalidOperation, TypeError):
        raise HTTPException(
            422,
            "KG musbat son bo'lishi kerak",
        )


def item_total(quantity_grams, price_per_kg):
    return (
        int(quantity_grams) * int(price_per_kg) + 500
    ) // 1000


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)

    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        250000,
    )

    return f"{salt}${digest.hex()}"


def verify_password(password, stored):
    try:
        salt, _ = stored.split("$", 1)

        return hmac.compare_digest(
            password_hash(password, salt),
            stored,
        )

    except (ValueError, TypeError):
        return False


def audit(db, actor, action, target, target_id):
    db.execute(
        """
        INSERT INTO audit_log (
            actor_id, action, target,
            target_id, created_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (actor, action, target, target_id, now()),
    )


def ensure_customer(db, customer_id):
    customer = db.execute(
        """
        SELECT *
        FROM customers
        WHERE id = ? AND is_active = 1
        """,
        (customer_id,),
    ).fetchone()

    if not customer:
        raise HTTPException(404, "Mijoz topilmadi")

    return customer


# ==========================================
# PYDANTIC MODELS
# ==========================================

class LoginIn(BaseModel):
    username: str
    password: str


class CustomerIn(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    phone: str = Field(default="", max_length=40)
    address: str = Field(default="", max_length=300)
    credit_limit: int = Field(default=0, ge=0)


class ProductIn(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    price_per_kg: int = Field(gt=0)
    min_stock_grams: int = Field(default=0, ge=0)


class StockIn(BaseModel):
    product_id: int
    quantity_kg: str
    note: str = Field(default="", max_length=500)


class OrderLineIn(BaseModel):
    product_id: int
    ordered_kg: str


class OrderIn(BaseModel):
    customer_id: int
    items: list[OrderLineIn] = Field(min_length=1)
    note: str = Field(default="", max_length=500)


class ActualLineIn(BaseModel):
    item_id: int
    actual_kg: str


class ActualIn(BaseModel):
    items: list[ActualLineIn] = Field(min_length=1)


class StatusIn(BaseModel):
    status: str


class PaymentIn(BaseModel):
    customer_id: int
    amount: int = Field(gt=0)
    note: str = Field(default="", max_length=500)


class NewsIn(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    message: str = Field(min_length=2, max_length=3000)


# ==========================================
# TELEGRAM DATABASE QUEUE
# ==========================================

def setup_notifications():
    """
    Mavjud bazaga yangi jadval qo'shadi.
    Eski ma'lumotlarni o'chirmaydi.
    """

    with get_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                message TEXT NOT NULL,
                event_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK(status IN (
                        'PENDING', 'SENDING',
                        'SENT', 'FAILED'
                    )),
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                sent_at TEXT,
                next_attempt_at TEXT
            )
            """
        )

        db.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_telegram_outbox_pending
            ON telegram_outbox(status, next_attempt_at)
            """
        )

        db.commit()


def queue_telegram(
    db,
    telegram_id,
    message,
    event_key,
):
    if not telegram_id:
        return False

    cursor = db.execute(
        """
        INSERT OR IGNORE INTO telegram_outbox (
            telegram_id,
            message,
            event_key,
            status,
            created_at,
            next_attempt_at
        )
        VALUES (?, ?, ?, 'PENDING', ?, ?)
        """,
        (
            int(telegram_id),
            message,
            event_key,
            now(),
            now(),
        ),
    )

    return cursor.rowcount > 0


def telegram_send_sync(telegram_id, message):
    if not BOT_TOKEN or BOT_TOKEN.startswith(
        "TOKENNI_SHU_YERGA"
    ):
        raise RuntimeError(
            "BOT_TOKEN .env faylida sozlanmagan"
        )

    url = (
        "https://api.telegram.org/bot"
        + BOT_TOKEN
        + "/sendMessage"
    )

    payload = json.dumps(
        {
            "chat_id": telegram_id,
            "text": message,
            "disable_web_page_preview": True,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json"
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=15,
        ) as response:
            result = json.loads(
                response.read().decode("utf-8")
            )

            if not result.get("ok"):
                raise RuntimeError(
                    "Telegram API xabarni qabul qilmadi"
                )

            return result

    except urllib.error.HTTPError as error:
        body = error.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            f"Telegram HTTP {error.code}: {body[:300]}"
        ) from error


def process_one_notification():
    """
    Bir xabarni navbatdan oladi.
    Bir vaqtda ikki worker bir xil xabarni
    olishining oldini olish uchun transaction.
    """

    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")

        item = db.execute(
            """
            SELECT *
            FROM telegram_outbox
            WHERE status IN ('PENDING', 'FAILED')
              AND attempts < 5
              AND (
                  next_attempt_at IS NULL
                  OR next_attempt_at <= ?
              )
            ORDER BY id
            LIMIT 1
            """,
            (now(),),
        ).fetchone()

        if not item:
            db.commit()
            return False

        notification = dict(item)

        db.execute(
            """
            UPDATE telegram_outbox
            SET status = 'SENDING',
                attempts = attempts + 1
            WHERE id = ?
            """,
            (notification["id"],),
        )

        db.commit()

    try:
        telegram_send_sync(
            notification["telegram_id"],
            notification["message"],
        )

        with get_db() as db:
            db.execute(
                """
                UPDATE telegram_outbox
                SET status = 'SENT',
                    sent_at = ?,
                    last_error = NULL
                WHERE id = ?
                """,
                (now(), notification["id"]),
            )

            db.commit()

    except Exception as error:
        attempts = notification["attempts"] + 1

        retry_seconds = min(
            30 * (2 ** (attempts - 1)),
            1800,
        )

        retry_at = (
            datetime.now(timezone.utc)
            + timedelta(seconds=retry_seconds)
        ).isoformat()

        with get_db() as db:
            db.execute(
                """
                UPDATE telegram_outbox
                SET status = 'FAILED',
                    last_error = ?,
                    next_attempt_at = ?
                WHERE id = ?
                """,
                (
                    str(error)[:500],
                    retry_at,
                    notification["id"],
                ),
            )

            db.commit()

        logger.warning(
            "Telegram xabar #%s yuborilmadi: %s",
            notification["id"],
            error,
        )

    return True


async def notification_worker():
    """
    Server ishlayotgan paytda navbatni
    muntazam tekshiradi.
    """

    while True:
        try:
            processed = await asyncio.to_thread(
                process_one_notification
            )

            await asyncio.sleep(
                0.15 if processed else 2
            )

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception(
                "Telegram notification worker xatosi"
            )

            await asyncio.sleep(5)


def recover_notifications():
    """
    Server to'xtagan paytda SENDING bo'lib
    qolgan xabarlarni qayta navbatga qo'yadi.
    """

    with get_db() as db:
        db.execute(
            """
            UPDATE telegram_outbox
            SET status = 'FAILED',
                next_attempt_at = ?
            WHERE status = 'SENDING'
            """,
            (now(),),
        )

        db.commit()


def queue_order_status(db, order_id, status):
    row = db.execute(
        """
        SELECT
            o.id,
            o.final_total,
            c.telegram_id,
            c.name
        FROM orders o
        JOIN customers c ON c.id = o.customer_id
        WHERE o.id = ?
        """,
        (order_id,),
    ).fetchone()

    if not row or not row["telegram_id"]:
        return False

    message = (
        "SAGBAN — BUYURTMA XABARNOMASI\n\n"
        f"Buyurtma: #{order_id}\n"
        f"Mijoz: {row['name']}\n"
        f"Yangi holat: {STATUS_NAMES[status]}"
    )

    if status == "ACCEPTED":
        message += (
            "\n\nBuyurtmangiz qabul qilindi."
        )

    elif status == "PREPARING":
        message += (
            "\n\nMahsulotlaringiz tayyorlanmoqda."
        )

    elif status == "READY":
        message += (
            "\n\nBuyurtmangiz tayyor!"
        )

    elif status == "ON_THE_WAY":
        message += (
            "\n\nBuyurtmangiz yo'lga chiqdi."
        )

    elif status == "DELIVERED":
        message += (
            "\n\nBuyurtmangiz yetkazib berildi."
        )

    elif status == "CANCELLED":
        message += (
            "\n\nBuyurtmangiz bekor qilindi."
        )

    if row["final_total"] is not None and status in (
        "READY",
        "ON_THE_WAY",
        "DELIVERED",
    ):
        message += (
            "\nYakuniy summa: "
            f"{format_money(row['final_total'])} so'm"
        )

    return queue_telegram(
        db,
        row["telegram_id"],
        message,
        f"order:{order_id}:status:{status}",
    )


# ==========================================
# WEBSOCKET
# ==========================================

class WSManager:
    def __init__(self):
        self.clients = set()
        self.sessions = {}

    def session_valid(self, token_hash):
        with get_db() as db:
            row = db.execute(
                """
                SELECT 1
                FROM admin_sessions s
                JOIN admins a ON a.id = s.admin_id
                WHERE s.token_hash = ?
                  AND s.expires_at > ?
                  AND a.is_active = 1
                """,
                (token_hash, now()),
            ).fetchone()

        return row is not None

    async def notify(self, event, entity_id=None):
        for websocket in list(self.clients):
            token_hash = self.sessions.get(websocket)

            if (
                not token_hash
                or not self.session_valid(token_hash)
            ):
                await self.disconnect(websocket)
                continue

            try:
                await websocket.send_json({
                    "event": event,
                    "id": entity_id,
                    "time": now(),
                })
            except Exception:
                await self.disconnect(websocket)

    async def disconnect(self, websocket):
        self.clients.discard(websocket)
        self.sessions.pop(websocket, None)

        try:
            await websocket.close(code=1008)
        except Exception:
            pass

    async def disconnect_session(self, token_hash):
        for websocket, session_hash in list(
            self.sessions.items()
        ):
            if session_hash == token_hash:
                await self.disconnect(websocket)


ws_manager = WSManager()


# ==========================================
# FASTAPI LIFESPAN
# ==========================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    setup_notifications()
    recover_notifications()

    worker = asyncio.create_task(
        notification_worker()
    )

    logger.info("SAGBAN CRM ishga tushdi")

    try:
        yield

    finally:
        worker.cancel()

        try:
            await worker
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="SAGBAN CRM API",
    version="2.0.0",
    lifespan=lifespan,
)

if (BASE_DIR / "static").is_dir():
    app.mount(
        "/static",
        StaticFiles(
            directory=BASE_DIR / "static"
        ),
        name="static",
    )


# ==========================================
# AUTHENTICATION
# ==========================================

def current_admin(request: Request):
    token = request.cookies.get(
        "sagban_session"
    )

    if not token:
        raise HTTPException(
            401,
            "Admin sifatida tizimga kiring",
        )

    token_hash = hashlib.sha256(
        token.encode()
    ).hexdigest()

    with get_db() as db:
        row = db.execute(
            """
            SELECT a.id, a.username, a.role
            FROM admin_sessions s
            JOIN admins a ON a.id = s.admin_id
            WHERE s.token_hash = ?
              AND s.expires_at > ?
              AND a.is_active = 1
            """,
            (token_hash, now()),
        ).fetchone()

    if not row:
        raise HTTPException(
            401,
            "Sessiya tugagan"
        )

    return dict(row)


def require_manager(
    admin=Depends(current_admin)
):
    if admin["role"] not in (
        "ADMIN",
        "SUPER_ADMIN",
    ):
        raise HTTPException(
            403,
            "Ruxsat yo'q"
        )

    return admin


@app.get("/", include_in_schema=False)
def home():
    index = (
        BASE_DIR
        / "templates"
        / "index.html"
    )

    if index.exists():
        return FileResponse(index)

    return {
        "message": "SAGBAN CRM API",
        "docs": "/docs",
    }


@app.get("/api/health")
def health():
    with get_db() as db:
        db.execute("SELECT 1")

    return {
        "ok": True,
        "service": "SAGBAN CRM",
    }


@app.post("/api/auth/login")
def login(payload: LoginIn):
    with get_db() as db:
        row = db.execute(
            """
            SELECT *
            FROM admins
            WHERE username = ?
              AND is_active = 1
            """,
            (payload.username.strip(),),
        ).fetchone()

        if not row or not verify_password(
            payload.password,
            row["password_hash"],
        ):
            raise HTTPException(
                401,
                "Login yoki parol noto'g'ri",
            )

        token = secrets.token_urlsafe(48)

        expires = (
            datetime.now(timezone.utc)
            + timedelta(hours=SESSION_HOURS)
        ).isoformat()

        db.execute(
            """
            INSERT INTO admin_sessions (
                admin_id,
                token_hash,
                expires_at
            )
            VALUES (?, ?, ?)
            """,
            (
                row["id"],
                hashlib.sha256(
                    token.encode()
                ).hexdigest(),
                expires,
            ),
        )

        db.commit()

    response = JSONResponse(
        {
            "ok": True,
            "username": row["username"],
            "role": row["role"],
        }
    )

    response.set_cookie(
        "sagban_session",
        token,
        max_age=SESSION_HOURS * 3600,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
    )

    return response


@app.post("/api/auth/logout")
async def logout(request: Request):
    token = request.cookies.get(
        "sagban_session"
    )

    if token:
        with get_db() as db:
            db.execute(
                """
                DELETE FROM admin_sessions
                WHERE token_hash = ?
                """,
                (
                    hashlib.sha256(
                        token.encode()
                    ).hexdigest(),
                ),
            )

            db.commit()

    if token:
        await ws_manager.disconnect_session(
            hashlib.sha256(
                token.encode()
            ).hexdigest()
        )

    response = JSONResponse({"ok": True})

    response.delete_cookie(
        "sagban_session",
        samesite="strict",
    )

    return response


@app.get("/api/auth/me")
def me(admin=Depends(current_admin)):
    return admin


# ==========================================
# DASHBOARD
# ==========================================

@app.get("/api/dashboard")
def dashboard(admin=Depends(current_admin)):
    with get_db() as db:
        counts = {
            status: db.execute(
                """
                SELECT COUNT(*)
                FROM orders
                WHERE status = ?
                """,
                (status,),
            ).fetchone()[0]
            for status in STATUSES
        }

        customers = db.execute(
            """
            SELECT COUNT(*)
            FROM customers
            WHERE is_active = 1
            """
        ).fetchone()[0]

        products = db.execute(
            """
            SELECT COUNT(*)
            FROM products
            WHERE is_active = 1
            """
        ).fetchone()[0]

        debt = db.execute(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM ledger
            """
        ).fetchone()[0]

        sales = db.execute(
            """
            SELECT COALESCE(SUM(final_total), 0)
            FROM orders
            WHERE status = 'DELIVERED'
            """
        ).fetchone()[0]

        recent = [
            dict(row)
            for row in db.execute(
                """
                SELECT
                    o.id,
                    o.status,
                    o.final_total,
                    o.created_at,
                    c.name AS customer
                FROM orders o
                JOIN customers c
                    ON c.id = o.customer_id
                ORDER BY o.id DESC
                LIMIT 12
                """
            ).fetchall()
        ]

    return {
        "statuses": counts,
        "customers": customers,
        "products": products,
        "total_debt": debt,
        "delivered_sales": sales,
        "recent_orders": recent,
    }


# ==========================================
# CUSTOMERS
# ==========================================

@app.get("/api/customers")
def customers(admin=Depends(current_admin)):
    with get_db() as db:
        return [
            dict(row)
            for row in db.execute(
                """
                SELECT
                    c.*,
                    COALESCE(
                        (
                            SELECT SUM(l.amount)
                            FROM ledger l
                            WHERE l.customer_id = c.id
                        ),
                        0
                    ) AS debt
                FROM customers c
                ORDER BY c.id DESC
                """
            ).fetchall()
        ]


@app.post(
    "/api/customers",
    status_code=201,
)
async def add_customer(
    payload: CustomerIn,
    admin=Depends(require_manager),
):
    with get_db() as db:
        code = (
            "CL-"
            + secrets.token_hex(5).upper()
        )

        cursor = db.execute(
            """
            INSERT INTO customers (
                name,
                phone,
                address,
                login_code,
                credit_limit,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                payload.name.strip(),
                payload.phone.strip(),
                payload.address.strip(),
                code,
                payload.credit_limit,
                now(),
            ),
        )

        customer_id = cursor.lastrowid

        audit(
            db,
            admin["id"],
            "CREATE",
            "customer",
            customer_id,
        )

        db.commit()

    await ws_manager.notify(
        "customer.created",
        customer_id,
    )

    return {
        "id": customer_id,
        "login_code": code,
    }


# ==========================================
# PRODUCTS
# ==========================================

@app.get("/api/products")
def products(admin=Depends(current_admin)):
    with get_db() as db:
        return [
            dict(row)
            for row in db.execute(
                """
                SELECT *
                FROM products
                ORDER BY id DESC
                """
            ).fetchall()
        ]


@app.post(
    "/api/products",
    status_code=201,
)
async def add_product(
    payload: ProductIn,
    admin=Depends(require_manager),
):
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO products (
                name,
                price_per_kg,
                min_stock_grams,
                created_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                payload.name.strip(),
                payload.price_per_kg,
                payload.min_stock_grams,
                now(),
            ),
        )

        product_id = cursor.lastrowid

        audit(
            db,
            admin["id"],
            "CREATE",
            "product",
            product_id,
        )

        db.commit()

    await ws_manager.notify(
        "product.created",
        product_id,
    )

    return {"id": product_id}


# ==========================================
# WAREHOUSE
# ==========================================

@app.get("/api/warehouse")
def warehouse(admin=Depends(current_admin)):
    with get_db() as db:
        return [
            dict(row)
            for row in db.execute(
                """
                SELECT
                    id,
                    name,
                    stock_grams,
                    min_stock_grams
                FROM products
                WHERE is_active = 1
                ORDER BY name
                """
            ).fetchall()
        ]


@app.post("/api/warehouse/in")
async def stock_in(
    payload: StockIn,
    admin=Depends(require_manager),
):
    quantity = grams(
        payload.quantity_kg
    )

    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")

        cursor = db.execute(
            """
            UPDATE products
            SET stock_grams = stock_grams + ?
            WHERE id = ?
              AND is_active = 1
            """,
            (
                quantity,
                payload.product_id,
            ),
        )

        if not cursor.rowcount:
            raise HTTPException(
                404,
                "Mahsulot topilmadi",
            )

        db.execute(
            """
            INSERT INTO stock_movements (
                product_id,
                quantity_grams,
                kind,
                note,
                created_at
            )
            VALUES (?, ?, 'IN', ?, ?)
            """,
            (
                payload.product_id,
                quantity,
                payload.note,
                now(),
            ),
        )

        audit(
            db,
            admin["id"],
            "STOCK_IN",
            "product",
            payload.product_id,
        )

        db.commit()

    await ws_manager.notify(
        "stock.updated",
        payload.product_id,
    )

    return {"ok": True}


# ==========================================
# ORDERS
# ==========================================

@app.get("/api/orders")
def orders(
    status: str | None = None,
    admin=Depends(current_admin),
):
    if status and status not in STATUSES:
        raise HTTPException(
            422,
            "Status noto'g'ri",
        )

    with get_db() as db:
        sql = """
            SELECT
                o.*,
                c.name AS customer_name
            FROM orders o
            JOIN customers c
                ON c.id = o.customer_id
        """

        params = ()

        if status:
            sql += " WHERE o.status = ?"
            params = (status,)

        sql += """
            ORDER BY o.id DESC
            LIMIT 200
        """

        return [
            dict(row)
            for row in db.execute(
                sql,
                params,
            ).fetchall()
        ]


@app.get("/api/orders/{order_id}")
def order_detail(
    order_id: int,
    admin=Depends(current_admin),
):
    with get_db() as db:
        order = db.execute(
            """
            SELECT *
            FROM orders
            WHERE id = ?
            """,
            (order_id,),
        ).fetchone()

        if not order:
            raise HTTPException(
                404,
                "Buyurtma topilmadi",
            )

        items = [
            dict(row)
            for row in db.execute(
                """
                SELECT *
                FROM order_items
                WHERE order_id = ?
                ORDER BY id
                """,
                (order_id,),
            ).fetchall()
        ]

    return {
        "order": dict(order),
        "items": items,
    }


@app.post(
    "/api/orders",
    status_code=201,
)
async def create_order(
    payload: OrderIn,
    admin=Depends(current_admin),
):
    if len(payload.items) > 100:
        raise HTTPException(
            422,
            "Mahsulotlar juda ko'p",
        )

    ids = [
        item.product_id
        for item in payload.items
    ]

    if len(ids) != len(set(ids)):
        raise HTTPException(
            422,
            "Mahsulot takrorlangan",
        )

    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")

        ensure_customer(
            db,
            payload.customer_id,
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
                payload.customer_id,
                payload.note,
                now(),
            ),
        )

        order_id = cursor.lastrowid

        for item in payload.items:
            product = db.execute(
                """
                SELECT *
                FROM products
                WHERE id = ?
                  AND is_active = 1
                """,
                (item.product_id,),
            ).fetchone()

            if not product:
                raise HTTPException(
                    404,
                    f"Mahsulot topilmadi: {item.product_id}",
                )

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
                    product["id"],
                    product["name"],
                    grams(item.ordered_kg),
                    product["price_per_kg"],
                ),
            )

        audit(
            db,
            admin["id"],
            "CREATE",
            "order",
            order_id,
        )

        db.commit()

    await ws_manager.notify(
        "order.created",
        order_id,
    )

    return {
        "id": order_id,
        "status": "NEW",
    }


# ==========================================
# ACTUAL KG
# ==========================================

@app.put("/api/orders/{order_id}/actual")
async def set_actual(
    order_id: int,
    payload: ActualIn,
    admin=Depends(current_admin),
):
    ids = [
        item.item_id
        for item in payload.items
    ]

    if len(ids) != len(set(ids)):
        raise HTTPException(
            422,
            "Mahsulot qatori takrorlangan",
        )

    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")

        order = db.execute(
            """
            SELECT status
            FROM orders
            WHERE id = ?
            """,
            (order_id,),
        ).fetchone()

        if not order:
            raise HTTPException(
                404,
                "Buyurtma topilmadi",
            )

        if order["status"] not in (
            "ACCEPTED",
            "PREPARING",
        ):
            raise HTTPException(
                409,
                "Bu statusda real KG o'zgartirilmaydi",
            )

        existing = {
            row["id"]
            for row in db.execute(
                """
                SELECT id
                FROM order_items
                WHERE order_id = ?
                """,
                (order_id,),
            ).fetchall()
        }

        if set(ids) != existing:
            raise HTTPException(
                422,
                "Barcha mahsulotlarning real KG sini kiriting",
            )

        for item in payload.items:
            db.execute(
                """
                UPDATE order_items
                SET actual_grams = ?
                WHERE id = ?
                  AND order_id = ?
                """,
                (
                    grams(item.actual_kg),
                    item.item_id,
                    order_id,
                ),
            )

        audit(
            db,
            admin["id"],
            "ACTUAL_WEIGHT",
            "order",
            order_id,
        )

        db.commit()

    await ws_manager.notify(
        "order.updated",
        order_id,
    )

    return {"ok": True}


# ==========================================
# ORDER STATUS + TELEGRAM
# ==========================================

@app.post("/api/orders/{order_id}/status")
async def change_status(
    order_id: int,
    payload: StatusIn,
    admin=Depends(current_admin),
):
    new_status = payload.status.upper()

    if new_status not in STATUSES:
        raise HTTPException(
            422,
            "Status noto'g'ri",
        )

    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")

        order = db.execute(
            """
            SELECT *
            FROM orders
            WHERE id = ?
            """,
            (order_id,),
        ).fetchone()

        if not order:
            raise HTTPException(
                404,
                "Buyurtma topilmadi",
            )

        old_status = order["status"]

        if new_status not in ALLOWED_NEXT[old_status]:
            raise HTTPException(
                409,
                f"{old_status} -> {new_status} ruxsat etilmagan",
            )

        lines = db.execute(
            """
            SELECT *
            FROM order_items
            WHERE order_id = ?
            """,
            (order_id,),
        ).fetchall()

        # READY: ombordan mahsulot yechish

        if new_status == "READY":
            if not lines or any(
                line["actual_grams"] is None
                for line in lines
            ):
                raise HTTPException(
                    409,
                    "Avval barcha real KG larni kiriting",
                )

            for line in lines:
                updated = db.execute(
                    """
                    UPDATE products
                    SET stock_grams =
                        stock_grams - ?
                    WHERE id = ?
                      AND stock_grams >= ?
                    """,
                    (
                        line["actual_grams"],
                        line["product_id"],
                        line["actual_grams"],
                    ),
                )

                
                if not updated.rowcount:
                        product_stock = db.execute(
                            """
                            SELECT stock_grams
                            FROM products
                            WHERE id = ?
                            """,
                            (line["product_id"],),
                        ).fetchone()
                    
                        available_grams = (
                            product_stock["stock_grams"]
                            if product_stock else 0
                        )
                    
                        required_kg = line["actual_grams"] / 1000
                        available_kg = available_grams / 1000
                    
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                f"Omborda mahsulot yetarli emas!\n"
                                f"Mahsulot: {line['product_name']}\n"
                                f"Kerak: {required_kg:g} kg\n"
                                f"Mavjud: {available_kg:g} kg"
                            ),
                        )
                    

                db.execute(
                    """
                    INSERT INTO stock_movements (
                        product_id,
                        order_id,
                        quantity_grams,
                        kind,
                        note,
                        created_at
                    )
                    VALUES (?, ?, ?, 'OUT', ?, ?)
                    """,
                    (
                        line["product_id"],
                        order_id,
                        -line["actual_grams"],
                        "Buyurtma tayyor",
                        now(),
                    ),
                )

            total = sum(
                item_total(
                    line["actual_grams"],
                    line["price_per_kg"],
                )
                for line in lines
            )

            db.execute(
                """
                UPDATE orders
                SET final_total = ?
                WHERE id = ?
                """,
                (total, order_id),
            )

        # CANCELLED: omborga qaytarish

        if (
            new_status == "CANCELLED"
            and old_status in (
                "READY",
                "ON_THE_WAY",
            )
        ):
            for line in lines:
                db.execute(
                    """
                    UPDATE products
                    SET stock_grams =
                        stock_grams + ?
                    WHERE id = ?
                    """,
                    (
                        line["actual_grams"],
                        line["product_id"],
                    ),
                )

                db.execute(
                    """
                    INSERT INTO stock_movements (
                        product_id,
                        order_id,
                        quantity_grams,
                        kind,
                        note,
                        created_at
                    )
                    VALUES (?, ?, ?, 'RETURN', ?, ?)
                    """,
                    (
                        line["product_id"],
                        order_id,
                        line["actual_grams"],
                        "Bekor qilingan buyurtma",
                        now(),
                    ),
                )

        # DELIVERED: qarzdorlikni hisoblash

        if new_status == "DELIVERED":
            if order["final_total"] is None:
                raise HTTPException(
                    409,
                    "Yakuniy summa hisoblanmagan",
                )

            db.execute(
                """
                INSERT INTO ledger (
                    customer_id,
                    order_id,
                    amount,
                    kind,
                    created_at
                )
                VALUES (?, ?, ?, 'SALE', ?)
                """,
                (
                    order["customer_id"],
                    order_id,
                    order["final_total"],
                    now(),
                ),
            )

        db.execute(
            """
            UPDATE orders
            SET status = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                new_status,
                now(),
                order_id,
            ),
        )

        db.execute(
            """
            INSERT INTO order_events (
                order_id,
                old_status,
                new_status,
                actor_id,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                order_id,
                old_status,
                new_status,
                admin["id"],
                now(),
            ),
        )

        audit(
            db,
            admin["id"],
            "STATUS_" + new_status,
            "order",
            order_id,
        )

        queued = queue_order_status(
            db,
            order_id,
            new_status,
        )

        db.commit()

    await ws_manager.notify(
        "order.status",
        order_id,
    )

    return {
        "ok": True,
        "status": new_status,
        "telegram_queued": queued,
    }


# ==========================================
# PAYMENTS
# ==========================================

@app.get("/api/payments")
def payments(admin=Depends(current_admin)):
    with get_db() as db:
        return [
            dict(row)
            for row in db.execute(
                """
                SELECT *
                FROM payments
                ORDER BY id DESC
                LIMIT 200
                """
            ).fetchall()
        ]


@app.post(
    "/api/payments",
    status_code=201,
)
async def add_payment(
    payload: PaymentIn,
    admin=Depends(require_manager),
):
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")

        ensure_customer(
            db,
            payload.customer_id,
        )

        cursor = db.execute(
            """
            INSERT INTO payments (
                customer_id,
                amount,
                note,
                created_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                payload.customer_id,
                payload.amount,
                payload.note,
                now(),
            ),
        )

        payment_id = cursor.lastrowid

        db.execute(
            """
            INSERT INTO ledger (
                customer_id,
                payment_id,
                amount,
                kind,
                created_at
            )
            VALUES (?, ?, ?, 'PAYMENT', ?)
            """,
            (
                payload.customer_id,
                payment_id,
                -payload.amount,
                now(),
            ),
        )

        audit(
            db,
            admin["id"],
            "PAYMENT",
            "payment",
            payment_id,
        )

        db.commit()

    await ws_manager.notify(
        "payment.created",
        payment_id,
    )

    return {"id": payment_id}


# ==========================================
# NEWS + TELEGRAM BROADCAST
# ==========================================

@app.get("/api/news")
def news(admin=Depends(current_admin)):
    with get_db() as db:
        return [
            dict(row)
            for row in db.execute(
                """
                SELECT *
                FROM news
                ORDER BY id DESC
                LIMIT 100
                """
            ).fetchall()
        ]


@app.post(
    "/api/news",
    status_code=201,
)
async def add_news(
    payload: NewsIn,
    admin=Depends(require_manager),
):
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")

        cursor = db.execute(
            """
            INSERT INTO news (
                title,
                message,
                created_at
            )
            VALUES (?, ?, ?)
            """,
            (
                payload.title.strip(),
                payload.message.strip(),
                now(),
            ),
        )

        news_id = cursor.lastrowid

        customers = db.execute(
            """
            SELECT id, telegram_id
            FROM customers
            WHERE is_active = 1
              AND telegram_id IS NOT NULL
            """
        ).fetchall()

        telegram_message = (
            "SAGBAN — YANGILIK\n\n"
            f"{payload.title.strip()}\n\n"
            f"{payload.message.strip()}"
        )

        queued = 0

        for customer in customers:
            if queue_telegram(
                db,
                customer["telegram_id"],
                telegram_message,
                f"news:{news_id}:customer:{customer['id']}",
            ):
                queued += 1

        audit(
            db,
            admin["id"],
            "CREATE",
            "news",
            news_id,
        )

        db.commit()

    await ws_manager.notify(
        "news.created",
        news_id,
    )

    return {
        "id": news_id,
        "telegram_queued": queued,
        "message": (
            f"Yangilik saqlandi. "
            f"{queued} ta Telegram xabari navbatga qo'yildi."
        ),
    }


# ==========================================
# NOTIFICATION REPORTS
# ==========================================

@app.get("/api/notifications/stats")
def notification_stats(
    admin=Depends(current_admin)
):
    with get_db() as db:
        rows = db.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM telegram_outbox
            GROUP BY status
            """
        ).fetchall()

    result = {
        "PENDING": 0,
        "SENDING": 0,
        "SENT": 0,
        "FAILED": 0,
    }

    for row in rows:
        result[row["status"]] = row["count"]

    return result


@app.get("/api/news/{news_id}/delivery")
def news_delivery(
    news_id: int,
    admin=Depends(current_admin),
):
    with get_db() as db:
        news_item = db.execute(
            """
            SELECT id
            FROM news
            WHERE id = ?
            """,
            (news_id,),
        ).fetchone()

        if not news_item:
            raise HTTPException(
                404,
                "Yangilik topilmadi",
            )

        rows = db.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM telegram_outbox
            WHERE event_key LIKE ?
            GROUP BY status
            """,
            (f"news:{news_id}:customer:%",),
        ).fetchall()

    result = {
        "PENDING": 0,
        "SENDING": 0,
        "SENT": 0,
        "FAILED": 0,
    }

    for row in rows:
        result[row["status"]] = row["count"]

    return {
        "news_id": news_id,
        "delivery": result,
    }


# ==========================================
# INVOICE / NAKLADNOY DATA
# ==========================================

def build_invoice(order_id):
    with get_db() as db:
        order = db.execute(
            """
            SELECT
                o.*,
                c.name AS customer_name,
                c.phone AS customer_phone,
                c.address AS customer_address
            FROM orders o
            JOIN customers c
                ON c.id = o.customer_id
            WHERE o.id = ?
            """,
            (order_id,),
        ).fetchone()

        if not order:
            raise HTTPException(
                404,
                "Buyurtma topilmadi",
            )

        order = dict(order)

        rows = db.execute(
            """
            SELECT *
            FROM order_items
            WHERE order_id = ?
            ORDER BY id
            """,
            (order_id,),
        ).fetchall()

        items = []

        estimated_total = 0
        actual_total = 0
        all_actual = bool(rows)

        for row in rows:
            item = dict(row)

            estimated = item_total(
                item["ordered_grams"],
                item["price_per_kg"],
            )

            actual = None

            if item["actual_grams"] is not None:
                actual = item_total(
                    item["actual_grams"],
                    item["price_per_kg"],
                )
                actual_total += actual
            else:
                all_actual = False

            estimated_total += estimated

            item["estimated_total"] = estimated
            item["actual_total"] = actual

            items.append(item)

        # Ledger'ning joriy balansi.
        current_balance = db.execute(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM ledger
            WHERE customer_id = ?
            """,
            (order["customer_id"],),
        ).fetchone()[0]

        # Shu buyurtmaning SALE yozuvi.
        posted_sale = db.execute(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM ledger
            WHERE order_id = ?
              AND kind = 'SALE'
            """,
            (order_id,),
        ).fetchone()[0]

    # Joriy balansdan ushbu buyurtma savdosini
    # ajratib ko'rsatamiz.
    other_balance = (
        current_balance - posted_sale
    )

    final_total = order["final_total"]

    if final_total is None and all_actual:
        final_total = actual_total

    # Faqat DELIVERED bo'lsa, savdo qarzga
    # rasmiy yozilgan hisoblanadi.
    invoice_total = final_total or 0

    balance_after = (
        other_balance + invoice_total
    )

    return {
        "order": order,
        "items": items,
        "estimated_total": estimated_total,
        "final_total": final_total,
        "other_balance": other_balance,
        "posted_sale": posted_sale,
        "current_balance": current_balance,
        "balance_after_invoice": balance_after,
        "balance_is_posted": bool(posted_sale),
        "seller": {
            "name": os.getenv(
                "COMPANY_NAME",
                "SAGBAN JIZZAX",
            ),
            "agent": os.getenv(
                "COMPANY_AGENT",
                "SAGBAN",
            ),
            "phone": os.getenv(
                "COMPANY_PHONE",
                "",
            ),
        },
    }


@app.get("/api/orders/{order_id}/invoice")
def invoice_data(
    order_id: int,
    admin=Depends(current_admin),
):
    return build_invoice(order_id)


# ==========================================
# PRINTABLE INVOICE
# ==========================================

@app.get(
    "/api/orders/{order_id}/invoice/print",
    response_class=HTMLResponse,
)
def invoice_print(
    order_id: int,
    format: str = "a4",
    copies: int = 1,
    admin=Depends(current_admin),
):
    allowed_formats = {
        "a4",
        "80mm",
        "58mm",
    }

    if format not in allowed_formats:
        raise HTTPException(
            422,
            "Format noto'g'ri",
        )

    if copies not in (1, 2):
        raise HTTPException(
            422,
            "Nusxalar soni 1 yoki 2 bo'lishi kerak",
        )

    data = build_invoice(order_id)

    order = data["order"]
    seller = data["seller"]

    def e(value):
        return html.escape(
            str(value if value is not None else "")
        )

    def line_items():
        result = []

        for index, item in enumerate(
            data["items"],
            start=1,
        ):
            quantity = (
                item["actual_grams"]
                if item["actual_grams"] is not None
                else item["ordered_grams"]
            )

            total = (
                item["actual_total"]
                if item["actual_total"] is not None
                else item["estimated_total"]
            )

            result.append(
                "<tr>"
                f"<td>{index}</td>"
                f"<td>{e(item['product_name'])}</td>"
                f"<td>{format_kg(quantity)} kg</td>"
                f"<td>{format_money(item['price_per_kg'])}</td>"
                f"<td>{format_money(total)}</td>"
                "</tr>"
            )

        return "".join(result)

    def invoice_page():
        total = (
            data["final_total"]
            if data["final_total"] is not None
            else data["estimated_total"]
        )

        status = STATUS_NAMES.get(
            order["status"],
            order["status"],
        )

        return f"""
        <section class="invoice">
          <header class="invoice-header">
            <div class="seller">
              <strong>{e(seller['name'])}</strong>
              <div>Agent: {e(seller['agent'])}</div>
              <div>Tel: {e(seller['phone'])}</div>
            </div>

            <div class="logo">Sagban</div>

            <div class="buyer">
              <strong>Mijoz:</strong>
              {e(order['customer_name'])}
              <div>
                Tel: {e(order['customer_phone'])}
              </div>
              <div>
                {e(order['customer_address'])}
              </div>
            </div>
          </header>

          <h1>NAKLADNOY</h1>

          <div class="invoice-meta">
            <span>
              Buyurtma № {order['id']}
            </span>
            <span>
              Holati: {e(status)}
            </span>
          </div>

          <table class="products">
            <thead>
              <tr>
                <th>№</th>
                <th>Mahsulot nomi</th>
                <th>Miqdor</th>
                <th>Narxi</th>
                <th>Umumiy narxi</th>
              </tr>
            </thead>
            <tbody>
              {line_items()}
            </tbody>
            <tfoot>
              <tr>
                <td colspan="4">
                  <strong>Jami</strong>
                </td>
                <td>
                  <strong>
                    {format_money(total)} UZS
                  </strong>
                </td>
              </tr>
            </tfoot>
          </table>

          <div class="balance">
            <div>
              <span>Boshqa operatsiyalar balansi:</span>
              <strong>
                {format_money(data['other_balance'])}
                UZS
              </strong>
            </div>

            <div>
              <span>Ushbu buyurtma:</span>
              <strong>
                +{format_money(total)} UZS
              </strong>
            </div>

            <div class="balance-total">
              <span>
                {(
                    'Joriy qarzdorlik'
                    if data['balance_is_posted']
                    else 'Buyurtma qo‘shilgandagi balans'
                )}:
              </span>
              <strong>
                {format_money(
                    data['balance_after_invoice']
                )} UZS
              </strong>
            </div>
          </div>

          <div class="invoice-bottom">
            <p>
              Sana:
              {e(order['created_at'][:10])}
            </p>

            <p>
              Buyurtma №:
              <strong>ORD-{order['id']}</strong>
            </p>
          </div>

          <div class="signature">
            Qabul qiluvchi imzosi:
            __________________
          </div>

          <div class="notice">
            {(
                'Yakuniy real KG bo‘yicha hisoblangan.'
                if data['final_total'] is not None
                else 'Dastlabki hisob. Real KG kiritilmagan.'
            )}
          </div>
        </section>
        """

    page_size = {
        "a4": "A4",
        "80mm": "80mm auto",
        "58mm": "58mm auto",
    }[format]

    receipt = format != "a4"

    width = (
        "100%"
        if not receipt
        else ("80mm" if format == "80mm" else "58mm")
    )

    pages = "".join(
        invoice_page()
        for _ in range(copies)
    )

    document = f"""
    <!DOCTYPE html>
    <html lang="uz">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport"
            content="width=device-width,initial-scale=1">
      <title>
        SAGBAN Nakladnoy #{order_id}
      </title>

      <style>
        * {{
          box-sizing: border-box;
        }}

        body {{
          margin: 0;
          background: #edf0f4;
          color: #152033;
          font-family: Arial, sans-serif;
        }}

        .toolbar {{
          position: sticky;
          top: 0;
          display: flex;
          justify-content: center;
          gap: 12px;
          padding: 15px;
          background: white;
          border-bottom: 1px solid #ddd;
        }}

        button {{
          border: 0;
          border-radius: 8px;
          padding: 11px 18px;
          background: #c81020;
          color: white;
          font-weight: bold;
          cursor: pointer;
        }}

        .invoice {{
          width: {width};
          max-width: 210mm;
          min-height: 0;
          margin: 20px auto;
          padding: {'8mm' if receipt else '13mm'};
          background: white;
          box-shadow: 0 5px 25px #0001;
        }}

        .invoice-header {{
          display: grid;
          grid-template-columns: 1fr 1fr 1fr;
          align-items: center;
          gap: 12px;
          font-size: 12px;
        }}

        .seller,
        .buyer {{
          line-height: 1.7;
          overflow-wrap: anywhere;
        }}

        .buyer {{
          text-align: right;
        }}

        .logo {{
          padding: 8px;
          border-radius: 20px;
          background: #e60012;
          color: white;
          text-align: center;
          font-size: 35px;
          font-weight: 900;
          font-family: Georgia, serif;
          font-style: italic;
        }}

        h1 {{
          margin: 24px 0 12px;
          font-size: 18px;
          text-align: center;
          letter-spacing: 2px;
        }}

        .invoice-meta {{
          display: flex;
          justify-content: space-between;
          gap: 12px;
          margin-bottom: 12px;
          font-size: 12px;
        }}

        table {{
          width: 100%;
          border-collapse: collapse;
          font-size: 12px;
        }}

        th, td {{
          border: 1px solid #333;
          padding: 7px 5px;
          text-align: left;
        }}

        th {{
          background: #f3f5f8;
        }}

        td:first-child {{
          width: 28px;
        }}

        .balance {{
          margin-top: 10px;
          padding: 12px;
          background: #f5f7fa;
        }}

        .balance > div {{
          display: flex;
          justify-content: space-between;
          gap: 10px;
          margin: 6px 0;
          font-size: 12px;
        }}

        .balance-total {{
          padding-top: 9px;
          border-top: 1px solid #dce0e7;
          font-size: 14px !important;
          font-weight: bold;
        }}

        .invoice-bottom {{
          display: flex;
          justify-content: space-between;
          gap: 10px;
          margin-top: 20px;
          font-size: 12px;
        }}

        .signature {{
          margin-top: 22px;
          font-size: 12px;
        }}

        .notice {{
          margin-top: 18px;
          color: #777;
          font-size: 10px;
        }}

        @media (max-width: 600px) {{
          .invoice {{
            margin: 0 auto;
            padding: 12px;
          }}

          .invoice-header {{
            gap: 5px;
          }}

          .logo {{
            font-size: 22px;
          }}

          th, td {{
            font-size: 10px;
            padding: 5px 3px;
          }}
        }}

        @media print {{
          @page {{
            size: {page_size};
            margin: {'0' if receipt else '8mm'};
          }}

          body {{
            background: white;
          }}

          .toolbar {{
            display: none;
          }}

          .invoice {{
            margin: 0 auto;
            padding: {'3mm' if receipt else '5mm'};
            box-shadow: none;
            max-width: none;
            break-after: page;
          }}

          .invoice:last-child {{
            break-after: auto;
          }}

          .balance,
          th {{
            print-color-adjust: exact;
            -webkit-print-color-adjust: exact;
          }}
        }}

        {' '.join([
          '.invoice-header { display:block; text-align:center; }',
          '.seller, .buyer { text-align:center; margin:8px 0; }',
          '.logo { margin:8px auto; font-size:24px; }',
          'h1 { font-size:14px; }',
          '.invoice-meta { font-size:10px; }',
          'table { font-size:9px; }',
          'th,td { padding:3px 2px; font-size:9px; }',
          '.balance { padding:5px; }',
          '.balance > div { font-size:10px; }',
          '.invoice-bottom { font-size:10px; }',
        ]) if receipt else ''}
      </style>
    </head>

    <body>
      <div class="toolbar">
        <button onclick="window.print()">
          Chop etish / PDF
        </button>
      </div>

      {pages}
    </body>
    </html>
    """

    return HTMLResponse(
        content=document,
        headers={
            "Cache-Control": "no-store",
        },
    )


# ==========================================
# WEBSOCKET
# ==========================================

@app.websocket("/ws")
async def websocket_updates(
    websocket: WebSocket
):

    origin = websocket.headers.get("origin")

    allowed_origins = {
        "http://127.0.0.1:8000",
        "http://localhost:8000",
    }

    if origin not in allowed_origins:
        await websocket.close(code=1008)
        return

    token = websocket.cookies.get(
        "sagban_session"
    )

    if not token:
        await websocket.close(code=1008)
        return

    token_hash = hashlib.sha256(
        token.encode()
    ).hexdigest()

    with get_db() as db:
        row = db.execute(
            """
            SELECT 1
            FROM admin_sessions s
            JOIN admins a
                ON a.id = s.admin_id
            WHERE s.token_hash = ?
              AND s.expires_at > ?
              AND a.is_active = 1
            """,
            (token_hash, now()),
        ).fetchone()

    if not row:
        await websocket.close(code=1008)
        return

    await websocket.accept()

    ws_manager.clients.add(websocket)
    ws_manager.sessions[websocket] = token_hash

    try:
        while True:
            await websocket.receive_text()

    except WebSocketDisconnect:
        pass

    finally:
        ws_manager.clients.discard(websocket)
        ws_manager.sessions.pop(websocket, None)
