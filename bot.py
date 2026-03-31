import logging
import re
import os
from datetime import datetime, date, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from database import (
    init_db,
    add_transaction,
    delete_transaction,
    edit_transaction_comment,
    get_transaction_by_id,
    get_balance,
    get_recent_transactions,
    get_report,
    get_start_date,
    set_start_date,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
BOT_TOKEN     = os.environ["BOT_TOKEN"]
ADMIN_ID      = int(os.environ["ADMIN_ID"])
ALLOWED_GROUP = int(os.environ["ALLOWED_GROUP_ID"])

ALLOWED_USERS_RAW = os.environ.get("ALLOWED_USER_IDS", "")
ALLOWED_USERS = set()
if ALLOWED_USERS_RAW.strip():
    ALLOWED_USERS = {int(uid.strip()) for uid in ALLOWED_USERS_RAW.split(",") if uid.strip()}
ALLOWED_USERS.add(ADMIN_ID)


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def fmt(amount: int, currency: str) -> str:
    if currency == "UZS":
        return f"{abs(amount):,} UZS".replace(",", " ")
    return f"{abs(amount):,} $".replace(",", " ")


def parse_transaction(text: str):
    text = text.strip()
    if text.startswith("+"):
        t_type = "income"
        text = text[1:].strip()
    elif text.startswith("-"):
        t_type = "expense"
        text = text[1:].strip()
    else:
        t_type = "expense"

    match = re.search(r"\d[\d.]*", text)
    if not match:
        return None

    raw_number = match.group().replace(".", "")
    try:
        amount = int(raw_number)
    except ValueError:
        return None

    if amount <= 0:
        return None

    currency = "USD" if "$" in text else "UZS"
    rest = text[match.end():].replace("$", "").strip()

    return {"type": t_type, "amount": amount, "currency": currency, "comment": rest}


def parse_date(s: str) -> str | None:
    """Принимает DD.MM.YYYY или DD.MM.YY, возвращает YYYY-MM-DD или None."""
    for fmt_str in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(s.strip(), fmt_str).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def is_allowed(user_id: int) -> bool:
    return user_id in ALLOWED_USERS


# ─────────────────────────────────────────────
# ГРУППА: парсинг транзакций
# ─────────────────────────────────────────────
async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    msg  = update.message
    user = msg.from_user
    text = msg.text.strip()

    if msg.chat.id != ALLOWED_GROUP:
        return

    tx = parse_transaction(text)
    user_display = f"@{user.username}" if user.username else user.full_name

    if tx is None:
        if not re.search(r"\d", text):
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"⚠️ <b>Некорректная запись</b>\n"
                    f"👤 {user_display}\n"
                    f"📝 <code>{text}</code>"
                ),
                parse_mode="HTML",
            )
        return

    sign   = 1 if tx["type"] == "income" else -1
    tx_id  = add_transaction(
        user_id=user.id,
        username=user_display,
        amount=sign * tx["amount"],
        currency=tx["currency"],
        comment=tx["comment"],
        raw_text=text,
    )

    if tx_id == -1:
        start = get_start_date()
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"⏭ <b>Запись проигнорирована</b> (до даты начала учёта {start})\n"
                f"👤 {user_display}: <code>{text}</code>"
            ),
            parse_mode="HTML",
        )
        return

    icon     = "📥" if tx["type"] == "income" else "📤"
    sign_str = "+" if tx["type"] == "income" else "-"

    # Кнопка удаления для админа
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🗑 Удалить запись #{tx_id}", callback_data=f"del:{tx_id}")]
    ])

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"{icon} <b>Запись #{tx_id} принята</b>\n"
            f"👤 {user_display}\n"
            f"💰 {sign_str}{fmt(tx['amount'], tx['currency'])}\n"
            f"📝 {tx['comment'] or '—'}"
        ),
        parse_mode="HTML",
        reply_markup=keyboard,
    )


# ─────────────────────────────────────────────
# CALLBACK: удаление через кнопку
# ─────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.answer("⛔ Только для администратора.", show_alert=True)
        return

    data = query.data
    if data.startswith("del:"):
        tx_id = int(data.split(":")[1])
        tx    = get_transaction_by_id(tx_id)
        if not tx:
            await query.edit_message_text("❌ Запись не найдена (уже удалена?).")
            return
        delete_transaction(tx_id)
        sign = "+" if tx["amount"] > 0 else ""
        await query.edit_message_text(
            f"🗑 <b>Запись #{tx_id} удалена</b>\n"
            f"{sign}{fmt(tx['amount'], tx['currency'])} | {tx['comment'] or '—'}",
            parse_mode="HTML",
        )


# ─────────────────────────────────────────────
# /balance — текущий баланс
# ─────────────────────────────────────────────
async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ У вас нет доступа.")
        return

    bal    = get_balance()
    recent = get_recent_transactions(5)
    uzs    = bal.get("UZS", 0)
    usd    = bal.get("USD", 0)

    start = get_start_date()
    start_line = f"\n📅 Учёт с: <b>{start}</b>" if start else ""

    lines = [
        f"💰 <b>Текущий баланс</b>{start_line}",
        "",
        f"💵 <b>USD:</b>  {'📈' if usd >= 0 else '📉'} {fmt(usd, 'USD')}",
        f"💳 <b>UZS:</b>  {'📈' if uzs >= 0 else '📉'} {fmt(uzs, 'UZS')}",
    ]

    if recent:
        lines += ["", "─────────────────", "🕐 <b>Последние 5 операций:</b>"]
        for r in recent:
            sign = "+" if r["amount"] > 0 else ""
            lines.append(
                f"  <b>#{r['id']}</b> {sign}{fmt(r['amount'], r['currency'])}"
                f" | {r['username']} | {r['comment'] or '—'}"
            )

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await balance_command(update, context)


# ─────────────────────────────────────────────
# /report — отчёт за период
# ─────────────────────────────────────────────
async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ У вас нет доступа.")
        return

    today = date.today()
    args  = context.args  # список слов после команды

    # Определяем период
    if not args:
        # По умолчанию — текущий месяц
        from_date = today.strftime("%Y-%m-01")
        to_date   = today.strftime("%Y-%m-%d")
        label     = f"Месяц ({today.strftime('%m.%Y')})"

    elif args[0].lower() == "сегодня":
        from_date = to_date = today.strftime("%Y-%m-%d")
        label = f"Сегодня ({today.strftime('%d.%m.%Y')})"

    elif args[0].lower() == "неделя":
        from_date = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")
        to_date   = today.strftime("%Y-%m-%d")
        label     = "Текущая неделя"

    elif args[0].lower() == "месяц":
        from_date = today.strftime("%Y-%m-01")
        to_date   = today.strftime("%Y-%m-%d")
        label     = f"Текущий месяц ({today.strftime('%m.%Y')})"

    elif "-" in args[0]:
        # Формат: 01.06-30.06 или 01.06.2025-30.06.2025
        parts = args[0].split("-")
        if len(parts) == 2:
            # Добавляем год если не указан
            def normalize(d):
                if d.count(".") == 1:
                    d += f".{today.year}"
                return d
            fd = parse_date(normalize(parts[0]))
            td = parse_date(normalize(parts[1]))
            if fd and td:
                from_date = fd
                to_date   = td
                label     = f"{parts[0]} — {parts[1]}"
            else:
                await update.message.reply_text(
                    "❌ Неверный формат даты.\nПример: /report 01.06-30.06"
                )
                return
        else:
            await update.message.reply_text("❌ Неверный формат. Пример: /report 01.06-30.06")
            return
    else:
        await update.message.reply_text(
            "📊 Форматы команды /report:\n"
            "  /report — текущий месяц\n"
            "  /report сегодня\n"
            "  /report неделя\n"
            "  /report месяц\n"
            "  /report 01.06-30.06"
        )
        return

    r = get_report(from_date, to_date)

    def sign_icon(val):
        return "📈" if val >= 0 else "📉"

    text = (
        f"📊 <b>Отчёт: {label}</b>\n"
        f"📅 {from_date} → {to_date}\n"
        f"📝 Записей: {r['count']}\n"
        f"\n"
        f"━━━━━━  💵 USD  ━━━━━━\n"
        f"📥 Доход:   +{fmt(r['income_usd'],  'USD')}\n"
        f"📤 Расход:  -{fmt(r['expense_usd'], 'USD')}\n"
        f"{sign_icon(r['balance_usd'])} Итого:   {'+' if r['balance_usd'] >= 0 else ''}{fmt(r['balance_usd'], 'USD')}\n"
        f"\n"
        f"━━━━━━  💳 UZS  ━━━━━━\n"
        f"📥 Доход:   +{fmt(r['income_uzs'],  'UZS')}\n"
        f"📤 Расход:  -{fmt(r['expense_uzs'], 'UZS')}\n"
        f"{sign_icon(r['balance_uzs'])} Итого:   {'+' if r['balance_uzs'] >= 0 else ''}{fmt(r['balance_uzs'], 'UZS')}\n"
    )

    # Детали транзакций (максимум 15)
    txs = r["transactions"][-15:]
    if txs:
        text += "\n─────────────────────\n<b>Последние записи:</b>\n"
        for t in reversed(txs):
            sign = "+" if t["amount"] > 0 else ""
            dt   = t["created_at"][5:10]  # MM-DD
            text += f"  <b>#{t['id']}</b> {dt} {sign}{fmt(t['amount'], t['currency'])} | {t['comment'] or '—'}\n"

    await update.message.reply_text(text, parse_mode="HTML")


# ─────────────────────────────────────────────
# /delete <id> — удаление записи (только админ)
# ─────────────────────────────────────────────
async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Только для администратора.")
        return

    if not context.args:
        await update.message.reply_text("❌ Укажите ID: /delete 42")
        return

    try:
        tx_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом.")
        return

    tx = get_transaction_by_id(tx_id)
    if not tx:
        await update.message.reply_text(f"❌ Запись #{tx_id} не найдена.")
        return

    delete_transaction(tx_id)
    sign = "+" if tx["amount"] > 0 else ""
    await update.message.reply_text(
        f"🗑 <b>Запись #{tx_id} удалена</b>\n"
        f"{sign}{fmt(tx['amount'], tx['currency'])} | {tx['comment'] or '—'}",
        parse_mode="HTML",
    )


# ─────────────────────────────────────────────
# /edit <id> <новый комментарий> — правка (только админ)
# ─────────────────────────────────────────────
async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Только для администратора.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("❌ Формат: /edit 42 новый комментарий")
        return

    try:
        tx_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом.")
        return

    new_comment = " ".join(context.args[1:])
    tx = get_transaction_by_id(tx_id)
    if not tx:
        await update.message.reply_text(f"❌ Запись #{tx_id} не найдена.")
        return

    edit_transaction_comment(tx_id, new_comment)
    await update.message.reply_text(
        f"✏️ <b>Запись #{tx_id} обновлена</b>\n"
        f"Старый комментарий: {tx['comment'] or '—'}\n"
        f"Новый комментарий: {new_comment}",
        parse_mode="HTML",
    )


# ─────────────────────────────────────────────
# /setstart <дата> — установить дату начала учёта (только админ)
# ─────────────────────────────────────────────
async def setstart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Только для администратора.")
        return

    if not context.args:
        current = get_start_date()
        await update.message.reply_text(
            f"📅 Текущая дата начала учёта: <b>{current or 'не задана (учёт с начала)'}</b>\n\n"
            f"Чтобы изменить: /setstart 01.07.2025\n"
            f"Чтобы убрать ограничение: /setstart сброс",
            parse_mode="HTML",
        )
        return

    arg = context.args[0]

    if arg.lower() in ("сброс", "reset", "off"):
        set_start_date("")
        await update.message.reply_text("✅ Дата начала учёта сброшена. Теперь учитываются все записи.")
        return

    parsed = parse_date(arg)
    if not parsed:
        await update.message.reply_text("❌ Неверный формат. Пример: /setstart 01.07.2025")
        return

    set_start_date(parsed)
    await update.message.reply_text(
        f"✅ Дата начала учёта установлена: <b>{parsed}</b>\n"
        f"Записи до этой даты будут игнорироваться.",
        parse_mode="HTML",
    )


# ─────────────────────────────────────────────
# /help
# ─────────────────────────────────────────────
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    admin_section = ""
    if is_admin(update.effective_user.id):
        admin_section = (
            "\n\n🔧 <b>Команды администратора:</b>\n"
            "/delete 42 — удалить запись #42\n"
            "/edit 42 новый текст — изменить комментарий\n"
            "/setstart 01.07.2025 — учитывать записи с этой даты\n"
            "/setstart сброс — убрать ограничение по дате"
        )

    await update.message.reply_text(
        "📋 <b>Команды бота:</b>\n\n"
        "/start или /balance — текущий баланс\n"
        "/report — отчёт за текущий месяц\n"
        "/report сегодня — за сегодня\n"
        "/report неделя — за текущую неделю\n"
        "/report месяц — за текущий месяц\n"
        "/report 01.06-30.06 — за произвольный период"
        + admin_section,
        parse_mode="HTML",
    )


# ─────────────────────────────────────────────
# ЗАПУСК
# ─────────────────────────────────────────────
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    private = filters.ChatType.PRIVATE
    groups  = filters.TEXT & filters.ChatType.GROUPS

    app.add_handler(CommandHandler("start",    start_command,   filters=private))
    app.add_handler(CommandHandler("balance",  balance_command, filters=private))
    app.add_handler(CommandHandler("report",   report_command,  filters=private))
    app.add_handler(CommandHandler("delete",   delete_command,  filters=private))
    app.add_handler(CommandHandler("edit",     edit_command,    filters=private))
    app.add_handler(CommandHandler("setstart", setstart_command, filters=private))
    app.add_handler(CommandHandler("help",     help_command,    filters=private))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(groups, handle_group_message))

    logger.info("Bot started (v2).")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
