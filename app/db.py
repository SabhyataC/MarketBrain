import sqlite3
from pathlib import Path

DB_PATH = Path("data/app.db")


def get_conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _check_old_format(conn):
    """The products table changed shape. Fail loudly instead of crashing later."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
    if cols and "attrs" not in cols:
        raise RuntimeError(
            "data/app.db is in the old format (products had no variants). "
            "It only holds test data: delete data/app.db and start the server again."
        )
MODEL_CALLS_SQL = """
CREATE TABLE IF NOT EXISTS model_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT DEFAULT CURRENT_TIMESTAMP,
    kind TEXT NOT NULL,          -- text, vision, image_generate, image_edit
    purpose TEXT NOT NULL DEFAULT '',
    model TEXT,
    seconds REAL,
    ok INTEGER NOT NULL,
    error TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER
);
"""

def init_db():
    with get_conn() as conn:
        _check_old_format(conn)
        conn.executescript("""
        -- A product holds everything shared: details, price, custom fields.
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            brand TEXT,
            category TEXT,
            description TEXT,
            price REAL,
            cost REAL,
            photo TEXT,
            attrs TEXT NOT NULL DEFAULT '{}',   -- JSON: any extra facts the owner wants
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        -- A variant inherits from its product. It stores only what makes it
        -- different (options) and any field that differs (overrides).
        -- Every product has at least one variant: options '{}' means "no options".
        CREATE TABLE IF NOT EXISTS variants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
            options TEXT NOT NULL DEFAULT '{}',    -- JSON like {"colour":"red","size":"M"}
            stock INTEGER NOT NULL DEFAULT 0,
            overrides TEXT NOT NULL DEFAULT '{}',  -- JSON like {"price": 899}
            UNIQUE(product_id, options)
        );

        CREATE TABLE IF NOT EXISTS proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            decided_at TEXT
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT DEFAULT CURRENT_TIMESTAMP,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT
        );
        """)
        conn.executescript(MODEL_CALLS_SQL)