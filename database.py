"""
Хранилище данных — Google Sheets.
Две вкладки:
  • transactions  — все транзакции
  • settings      — настройки (дата начала учёта)

Переменные окружения:
  GOOGLE_SHEET_ID      — ID таблицы из URL
  GOOGLE_CREDENTIALS   — содержимое JSON-ключа сервисного аккаунта (одной строкой)
"""

import os
import json
import logging
from datetime import datetime
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_ID = os.environ["GOOGLE_SHEET_ID"]

# Заголовки листа transactions
TX_HEADERS = ["id", "created_at", "user_id", "username", "amount", "currency", "comment", "raw_text", "msg_id"]

# ─────────────────────────────────────────────
# ПОДКЛЮЧЕНИЕ
# ─────────────────────────────────────────────
def _get_client() -> gspread.Client:
    creds_raw = os.environ["GOOGLE_CREDENTIALS"]
    creds_dict = json.loads(creds_raw)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def _get_sheets():
    client = _get_client()
    spreadsheet = client.open_by_key(SHEET_ID)
    return spreadsheet


def _get_tx_sheet():
    return _get_sheets().worksheet("transactions")


def _get_settings_sheet():
    return _get_sheets().worksheet("settings")


# ─────────────────────────────────────────────
# ИНИЦИАЛИЗАЦИЯ
# ─────────────────────────────────────────────
def init_db():
    """Создаёт нужные вкладки и заголовки если их нет."""
    try:
        spreadsheet = _get_sheets()
        existing = [ws.title for ws in spreadsheet.worksheets()]

        # Вкладка transactions
        if "transactions" not in existing:
            ws = spreadsheet.add_worksheet(title="transactions", rows=10000, cols=len(TX_HEADERS))
            ws.append_row(TX_HEADERS)
            # Форматирование заголовка
            ws.format("A1:I1", {
                "backgroundColor": {"red": 0.12, "green": 0.30, "blue": 0.48},
                "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            })
            # Колонка amount (E) — числовой формат без разделителей
            ws.format("E:E", {"numberFormat": {"type": "NUMBER", "pattern": "0"}})
            ws.freeze(rows=1)
            logger.info("Created 'transactions' sheet")
        else:
            ws = spreadsheet.worksheet("transactions")
            # Проверяем заголовки
            if ws.row_count == 0 or ws.row_values(1) != TX_HEADERS:
                ws.insert_row(TX_HEADERS, 1)
                ws.format("A1:I1", {
                    "backgroundColor": {"red": 0.12, "green": 0.30, "blue": 0.48},
                    "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                })
                ws.freeze(rows=1)
            # Всегда применяем числовой формат без разделителей к колонке amount
            ws.format("E:E", {"numberFormat": {"type": "NUMBER", "pattern": "0"}})

        # Вкладка settings
        if "settings" not in existing:
            ws2 = spreadsheet.add_worksheet(title="settings", rows=50, cols=2)
            ws2.append_row(["key", "value"])
            logger.info("Created 'settings' sheet")

        logger.info("Google Sheets DB initialized OK")
    except Exception as e:
        logger.error(f"init_db error: {e}")
        raise


# ─────────────────────────────────────────────
# НАСТРОЙКИ (с кэшем в памяти — снижает кол-во запросов к API)
# ─────────────────────────────────────────────
_settings_cache: dict = {}  # key → value, живёт пока бот запущен


def get_setting(key: str, default=None):
    # Сначала смотрим в кэш
    if key in _settings_cache:
        return _settings_cache[key] or default
    try:
        ws = _get_settings_sheet()
        records = ws.get_all_records()
        # Загружаем всё в кэш сразу
        for row in records:
            _settings_cache[str(row.get("key"))] = str(row.get("value"))
        return _settings_cache.get(key) or default
    except Exception as e:
        logger.error(f"get_setting error: {e}")
        return default


def set_setting(key: str, value: str):
    # Обновляем кэш сразу
    _settings_cache[key] = value
    try:
        ws = _get_settings_sheet()
        records = ws.get_all_records()
        for i, row in enumerate(records, start=2):
            if str(row.get("key")) == key:
                ws.update_cell(i, 2, value)
                return
        ws.append_row([key, value])
    except Exception as e:
        logger.error(f"set_setting error: {e}")


def get_start_date() -> Optional[str]:
    val = get_setting("start_date")
    return val if val else None


def set_start_date(dt: str):
    set_setting("start_date", dt)


# ─────────────────────────────────────────────
# ВСПОМОГАТЕЛЬНЫЕ
# ─────────────────────────────────────────────
def _next_id(ws) -> int:
    """Возвращает следующий auto-increment ID."""
    all_vals = ws.col_values(1)  # колонка id
    nums = [int(v) for v in all_vals[1:] if v.isdigit()]
    return max(nums) + 1 if nums else 1


def _rows_to_dicts(rows: list, headers: list) -> list:
    result = []
    for row in rows:
        row = list(row) + [""] * (len(headers) - len(row))
        d = dict(zip(headers, row))
        # Конвертируем amount — Google Sheets может вернуть:
        # "3000000", "3.000.000", "3,000,000", "3 000 000", "-150000", "3000.5"
        try:
            raw = str(d["amount"]).strip().replace("\xa0", "").replace(" ", "")
            # Определяем формат: если точек больше одной — они разделители тысяч
            # Если точка одна и после неё не более 2 цифр — это десятичная
            dot_count   = raw.count(".")
            comma_count = raw.count(",")
            if dot_count > 1:
                # 3.000.000 → убираем все точки
                raw = raw.replace(".", "")
            elif dot_count == 1 and comma_count == 0:
                # Может быть 3000.50 (десятичная) или 3.000 (тысячи)
                parts = raw.split(".")
                if len(parts[1]) == 3:
                    # После точки ровно 3 цифры → разделитель тысяч: 3.000 → 3000
                    raw = raw.replace(".", "")
                else:
                    # Десятичная дробь: 3000.50 → берём целую часть
                    raw = raw.split(".")[0]
            # Убираем запятые (американский формат 3,000,000)
            raw = raw.replace(",", "")
            d["amount"] = int(raw) if raw.lstrip("-") else 0
        except (ValueError, TypeError):
            d["amount"] = 0
        result.append(d)
    return result


# ─────────────────────────────────────────────
# ТРАНЗАКЦИИ
# ─────────────────────────────────────────────
def add_transaction(user_id, username, amount, currency, comment, raw_text, msg_id=None) -> int:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    start = get_start_date()
    if start and now[:10] < start:
        return -1
    try:
        ws    = _get_tx_sheet()
        tx_id = _next_id(ws)
        ws.append_row([
            tx_id, now, user_id, username,
            amount, currency,
            comment or "", raw_text or "",
            msg_id or "",
        ])
        return tx_id
    except Exception as e:
        logger.error(f"add_transaction error: {e}")
        return -1


def _find_row_by_id(ws, tx_id: int) -> Optional[int]:
    """Возвращает номер строки (1-based) для данного tx_id."""
    ids = ws.col_values(1)
    for i, val in enumerate(ids, start=1):
        if str(val) == str(tx_id):
            return i
    return None


def update_transaction(tx_id, amount, currency, comment, raw_text):
    try:
        ws  = _get_tx_sheet()
        row = _find_row_by_id(ws, tx_id)
        if not row:
            return
        ws.update_cell(row, 5, amount)    # amount
        ws.update_cell(row, 6, currency)  # currency
        ws.update_cell(row, 7, comment or "")
        ws.update_cell(row, 8, raw_text or "")
    except Exception as e:
        logger.error(f"update_transaction error: {e}")


def delete_transaction(tx_id) -> bool:
    try:
        ws  = _get_tx_sheet()
        row = _find_row_by_id(ws, tx_id)
        if not row:
            return False
        ws.delete_rows(row)
        return True
    except Exception as e:
        logger.error(f"delete_transaction error: {e}")
        return False


def edit_transaction_comment(tx_id, new_comment):
    try:
        ws  = _get_tx_sheet()
        row = _find_row_by_id(ws, tx_id)
        if row:
            ws.update_cell(row, 7, new_comment)
    except Exception as e:
        logger.error(f"edit_transaction_comment error: {e}")


def get_transaction_by_id(tx_id) -> Optional[dict]:
    try:
        ws  = _get_tx_sheet()
        row = _find_row_by_id(ws, tx_id)
        if not row:
            return None
        vals = ws.row_values(row)
        return _rows_to_dicts([vals], TX_HEADERS)[0]
    except Exception as e:
        logger.error(f"get_transaction_by_id error: {e}")
        return None


def get_transaction_by_msg_id(msg_id) -> Optional[dict]:
    try:
        ws   = _get_tx_sheet()
        data = ws.get_all_values()
        # Последний совпадающий msg_id
        for row in reversed(data[1:]):
            row = list(row) + [""] * (len(TX_HEADERS) - len(row))
            if str(row[8]) == str(msg_id):
                return _rows_to_dicts([row], TX_HEADERS)[0]
        return None
    except Exception as e:
        logger.error(f"get_transaction_by_msg_id error: {e}")
        return None


def _get_all_tx_filtered(from_date=None, to_date=None) -> list:
    """Возвращает все транзакции с фильтрацией по датам."""
    try:
        ws   = _get_tx_sheet()
        data = ws.get_all_values()
        rows = data[1:]  # пропускаем заголовок

        start = from_date or get_start_date()
        result = []
        for row in rows:
            row = list(row) + [""] * (len(TX_HEADERS) - len(row))
            created_at = row[1][:10] if row[1] else ""
            if start and created_at < start:
                continue
            if to_date and created_at > to_date:
                continue
            result.append(row)
        return _rows_to_dicts(result, TX_HEADERS)
    except Exception as e:
        logger.error(f"_get_all_tx_filtered error: {e}")
        return []


def get_balance(from_date=None, to_date=None) -> dict:
    txs = _get_all_tx_filtered(from_date, to_date)
    result = {"UZS": 0, "USD": 0}
    for t in txs:
        cur = t.get("currency", "")
        if cur in result:
            result[cur] += t["amount"]
    return result


def get_recent_transactions(limit=5, from_date=None) -> list:
    txs = _get_all_tx_filtered(from_date=from_date)
    return list(reversed(txs[-limit:])) if txs else []


def get_all_transactions(from_date=None, to_date=None) -> list:
    return _get_all_tx_filtered(from_date, to_date)


def get_first_transaction_date() -> Optional[str]:
    try:
        ws   = _get_tx_sheet()
        data = ws.get_all_values()
        if len(data) < 2:
            return None
        return data[1][1][:10] if data[1][1] else None
    except Exception as e:
        logger.error(f"get_first_transaction_date error: {e}")
        return None


def clear_all_transactions() -> int:
    """Удаляет все транзакции. Возвращает количество удалённых строк."""
    try:
        ws    = _get_tx_sheet()
        rows  = ws.get_all_values()
        count = len(rows) - 1  # без заголовка
        if count <= 0:
            return 0
        ws.delete_rows(2, count + 1)
        return count
    except Exception as e:
        logger.error(f"clear_all_transactions error: {e}")
        return -1


def get_report(from_date, to_date) -> dict:
    txs = _get_all_tx_filtered(from_date, to_date)

    def calc(currency, positive):
        vals = [t["amount"] for t in txs if t["currency"] == currency]
        if positive:
            return sum(v for v in vals if v > 0)
        return abs(sum(v for v in vals if v < 0))

    return {
        "income_uzs":  calc("UZS", True),
        "expense_uzs": calc("UZS", False),
        "balance_uzs": calc("UZS", True) - calc("UZS", False),
        "income_usd":  calc("USD", True),
        "expense_usd": calc("USD", False),
        "balance_usd": calc("USD", True) - calc("USD", False),
        "count":       len(txs),
        "transactions": txs,
    }
