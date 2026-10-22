"""SAGBAN CRM SQLite storage, compatible with app.py.

Amounts: integer Uzbek so'm. Weights: integer grams.
This module creates missing tables; it does NOT migrate incompatible old schemas.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env')
DB_FILE = Path(os.getenv('DATABASE', 'data/sagban_v2.db'))
if not DB_FILE.is_absolute():
    DB_FILE = BASE_DIR / DB_FILE


def get_db() -> sqlite3.Connection:
    """Return a connection. Use `with get_db() as db` for transactions."""
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_FILE, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=30000')
    return db


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), 250_000)
    return f'{salt}${digest.hex()}'


SCHEMA = """
CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'ADMIN' CHECK (role IN ('SUPER_ADMIN','ADMIN','OPERATOR')),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS admin_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sessions_admin ON admin_sessions(admin_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON admin_sessions(expires_at);

CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    login_code TEXT NOT NULL UNIQUE,
    telegram_id INTEGER UNIQUE,
    credit_limit INTEGER NOT NULL DEFAULT 0 CHECK (credit_limit >= 0),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_customers_name ON customers(name);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    price_per_kg INTEGER NOT NULL CHECK (price_per_kg > 0),
    stock_grams INTEGER NOT NULL DEFAULT 0 CHECK (stock_grams >= 0),
    min_stock_grams INTEGER NOT NULL DEFAULT 0 CHECK (min_stock_grams >= 0),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    status TEXT NOT NULL DEFAULT 'NEW' CHECK (status IN
        ('NEW','ACCEPTED','PREPARING','READY','ON_THE_WAY','DELIVERED','CANCELLED')),
    note TEXT NOT NULL DEFAULT '',
    final_total INTEGER CHECK (final_total IS NULL OR final_total >= 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_status_created ON orders(status,created_at);

CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    product_name TEXT NOT NULL,
    ordered_grams INTEGER NOT NULL CHECK (ordered_grams > 0),
    actual_grams INTEGER CHECK (actual_grams IS NULL OR actual_grams > 0),
    price_per_kg INTEGER NOT NULL CHECK (price_per_kg > 0),
    UNIQUE (order_id,product_id)
);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);

CREATE TABLE IF NOT EXISTS stock_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    order_id INTEGER REFERENCES orders(id),
    quantity_grams INTEGER NOT NULL CHECK (quantity_grams != 0),
    kind TEXT NOT NULL CHECK (kind IN ('IN','OUT','RETURN','ADJUSTMENT')),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_stock_movements_product ON stock_movements(product_id);
CREATE INDEX IF NOT EXISTS idx_stock_movements_order ON stock_movements(order_id);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    amount INTEGER NOT NULL CHECK (amount > 0),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_payments_customer ON payments(customer_id);

CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    order_id INTEGER UNIQUE REFERENCES orders(id),
    payment_id INTEGER UNIQUE REFERENCES payments(id),
    amount INTEGER NOT NULL CHECK (amount != 0),
    kind TEXT NOT NULL CHECK (kind IN ('SALE','PAYMENT','ADJUSTMENT')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK ((kind='SALE' AND order_id IS NOT NULL AND payment_id IS NULL AND amount > 0)
        OR (kind='PAYMENT' AND payment_id IS NOT NULL AND order_id IS NULL AND amount < 0)
        OR (kind='ADJUSTMENT' AND order_id IS NULL AND payment_id IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_ledger_customer ON ledger(customer_id);

CREATE TABLE IF NOT EXISTS order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    old_status TEXT NOT NULL,
    new_status TEXT NOT NULL,
    actor_id INTEGER REFERENCES admins(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_order_events_order ON order_events(order_id);

CREATE TABLE IF NOT EXISTS news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id INTEGER REFERENCES admins(id),
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    target_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
"""


def init_db() -> None:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript(SCHEMA)
        # Never create a predictable default admin password.
        existing = db.execute('SELECT COUNT(*) FROM admins').fetchone()[0]
        if existing == 0:
            username = os.getenv('ADMIN_USERNAME', '').strip()
            password = os.getenv('ADMIN_PASSWORD', '')
            if username and len(password) >= 12:
                db.execute(
                    'INSERT INTO admins(username,password_hash,role,created_at) VALUES(?,?,?,?)',
                    (username, hash_password(password), 'SUPER_ADMIN', datetime.now(timezone.utc).isoformat()),
                )
                print(f'[SAGBAN] Admin yaratildi: {username}')
            else:
                print('[SAGBAN] DIQQAT: ADMIN_USERNAME va kamida 12 belgili ADMIN_PASSWORD ni .env ga kiriting.')
        db.commit()


if __name__ == '__main__':
    init_db()
    with get_db() as db:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    print(f'[SAGBAN] Baza: {DB_FILE}')
    print(f'[SAGBAN] Jadvallar: {len(tables)} ta — {", ".join(tables)}')
