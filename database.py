"""
SQLite база данных для Тихого Финансового Контролёра.
Путь к БД берётся из переменной окружения DB_PATH (по умолчанию /data/finance.db).
"""

import sqlite3
import os
from datetime import datetime, date

DB_PATH = os.environ.get("DB_PATH", "/data/finance.db")


def _get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT    NOT NULL,
                user_id    INTEGER NOT NULL,
                username   TEXT    NOT NULL,
                amount     INTEGER NOT NULL,
                currency   TEXT    NOT NULL,
                comment    TEXT,
                raw_text   TEXT
            )
        """)
        # Таблица настроек (например, дата начала учёта)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.commit()


# ─────────────────────────────────────────────
# НАСТРОЙКИ
# ─────────────────────────────────────────────

def get_setting(key: str, default=None):
    with _get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    with _get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value)
        )
        conn.commit()


def get_start_date() -> str | None:
    """Возвращает дату начала учёта в формате 'YYYY-MM-DD' или None."""
    return get_setting("start_date")


def set_start_date(dt: str):
    """dt — строка 'YYYY-MM-DD'."""
    set_setting("start_date", dt)


# ─────────────────────────────────────────────
# ТРАНЗАКЦИИ
# ─────────────────────────────────────────────

def add_transaction(user_id, username, amount, currency, comment, raw_text) -> int:
    """Возвращает ID новой записи."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Проверяем дату начала учёта
    start = get_start_date()
    if start and now[:10] < start:
        return -1  # Запись до даты начала — игнорируем

    with _get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO transactions (created_at, user_id, username, amount, currency, comment, raw_text)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (now, user_id, username, amount, currency, comment, raw_text),
        )
        conn.commit()
        return cur.lastrowid


def delete_transaction(tx_id: int) -> bool:
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
        conn.commit()
        return cur.rowcount > 0


def edit_transaction_comment(tx_id: int, new_comment: str) -> bool:
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE transactions SET comment = ? WHERE id = ?",
            (new_comment, tx_id)
        )
        conn.commit()
        return cur.rowcount > 0


def get_transaction_by_id(tx_id: int):
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM transactions WHERE id = ?", (tx_id,)
        ).fetchone()
    return dict(row) if row else None


def get_balance(from_date: str = None, to_date: str = None) -> dict:
    """
    Возвращает {'UZS': int, 'USD': int}.
    Учитывает глобальную start_date, если from_date не задан явно.
    """
    start = from_date or get_start_date()

    query = "SELECT currency, SUM(amount) as total FROM transactions"
    params = []
    conditions = []

    if start:
        conditions.append("created_at >= ?")
        params.append(start + " 00:00:00")
    if to_date:
        conditions.append("created_at <= ?")
        params.append(to_date + " 23:59:59")

    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " GROUP BY currency"

    with _get_conn() as conn:
        rows = conn.execute(query, params).fetchall()

    return {row["currency"]: row["total"] or 0 for row in rows}


def get_recent_transactions(limit: int = 5, from_date: str = None) -> list:
    start = from_date or get_start_date()
    query = "SELECT * FROM transactions"
    params = []
    if start:
        query += " WHERE created_at >= ?"
        params.append(start + " 00:00:00")
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    with _get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_report(from_date: str, to_date: str) -> dict:
    """
    Возвращает детальный отчёт за период.
    {
        'income_uzs': int, 'expense_uzs': int, 'balance_uzs': int,
        'income_usd': int, 'expense_usd': int, 'balance_usd': int,
        'count': int,
        'transactions': [...]
    }
    """
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM transactions
            WHERE created_at >= ? AND created_at <= ?
            ORDER BY created_at ASC
            """,
            (from_date + " 00:00:00", to_date + " 23:59:59"),
        ).fetchall()

    txs = [dict(r) for r in rows]

    def calc(currency, positive):
        vals = [r["amount"] for r in txs if r["currency"] == currency]
        if positive:
            return sum(v for v in vals if v > 0)
        else:
            return abs(sum(v for v in vals if v < 0))

    return {
        "income_uzs":  calc("UZS", True),
        "expense_uzs": calc("UZS", False),
        "balance_uzs": calc("UZS", True) - calc("UZS", False),
        "income_usd":  calc("USD", True),
        "expense_usd": calc("USD", False),
        "balance_usd": calc("USD", True) - calc("USD", False),
        "count": len(txs),
        "transactions": txs,
    }
