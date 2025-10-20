# main.py — Lottery+ (monolith)
# Требования: python-telegram-bot>=20, Flask
# Как работает:
#  - Telegram бот через long-polling
#  - Встроенный Flask-сервер для webhook от CryptoBot: /crypto_webhook
#  - SQLite база (lottery.db) хранится в той же папке
#  - Для теста /confirm_tx <amount> <tx_hash> — ручное подтверждение

import os
import logging
import sqlite3
import random
import threading
from typing import List, Dict, Optional
from flask import Flask, request, jsonify, abort

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, ContextTypes,
    CommandHandler, CallbackQueryHandler, MessageHandler, filters
)
from aiocryptopay import AioCryptoPay, Networks

# -------------------- КОНФИГ --------------------
# Эти значения ставим через Secrets (Replit) или переменные окружения (Termux)
BOT_TOKEN = os.environ.get("BOT_TOKEN")  # token от @BotFather
OWNER_ID = int(os.environ.get("OWNER_ID", "0")) if os.environ.get("OWNER_ID") else None  # твой telegram id
CRYPTO_WEBHOOK_SECRET = os.environ.get("CRYPTO_WEBHOOK_SECRET", "change_this_secret")  # shared secret для верификации webhook
CRYPTOBOT_API_TOKEN = os.environ.get("CRYPTOBOT_API_TOKEN")  # CryptoBot API token
PORT = int(os.environ.get("PORT", 5000))  # порт для Flask (Replit требует 5000)

# Бизнес-параметры (из ТЗ)
TICKET_PRICE = 0.5
MAX_TICKETS = 10000
PRIZES = (2500.0, 1500.0, 500.0)
BOT_FEE = 500.0

DB_PATH = "lottery.db"

# Проверки
if not BOT_TOKEN:
    raise SystemExit("ERROR: переменная окружения BOT_TOKEN не задана. Добавь в Secrets и перезапусти.")

# -------------------- ЛОГИРОВАНИЕ --------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("lotteryplus")

# -------------------- БАЗА ДАННЫХ (SQLite) --------------------
class DB:
    def __init__(self, path=DB_PATH):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self._init()

    def _init(self):
        cur = self.conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id TEXT PRIMARY KEY,
                created_at INTEGER DEFAULT (strftime('%s','now'))
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                ticket_number INTEGER PRIMARY KEY,
                telegram_id TEXT,
                active INTEGER DEFAULT 0,
                created_at INTEGER DEFAULT (strftime('%s','now'))
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id TEXT,
                invoice_id TEXT,
                amount REAL,
                tx_hash TEXT,
                confirmed INTEGER DEFAULT 0,
                created_at INTEGER DEFAULT (strftime('%s','now'))
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS draws (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER DEFAULT (strftime('%s','now')),
                winners TEXT,
                prizes TEXT
            )
        """)
        self.conn.commit()

    # users
    def ensure_user(self, telegram_id: str):
        cur = self.conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE telegram_id = ?", (telegram_id,))
        if not cur.fetchone():
            cur.execute("INSERT INTO users (telegram_id) VALUES (?)", (telegram_id,))
            self.conn.commit()

    # tickets insert reserved (active=0)
    def add_tickets(self, telegram_id: str, ticket_numbers: List[int]):
        cur = self.conn.cursor()
        for t in ticket_numbers:
            cur.execute("INSERT OR REPLACE INTO tickets (ticket_number, telegram_id, active) VALUES (?, ?, 0)", (t, telegram_id))
        self.conn.commit()

    def activate_tickets(self, ticket_numbers: List[int]):
        cur = self.conn.cursor()
        for t in ticket_numbers:
            cur.execute("UPDATE tickets SET active = 1 WHERE ticket_number = ?", (t,))
        self.conn.commit()

    def get_user_tickets(self, telegram_id: str) -> List[int]:
        cur = self.conn.cursor()
        cur.execute("SELECT ticket_number FROM tickets WHERE telegram_id = ? ORDER BY ticket_number", (telegram_id,))
        return [r[0] for r in cur.fetchall()]

    def count_sold_tickets(self) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM tickets WHERE active = 1")
        return cur.fetchone()[0]

    def get_all_active_tickets(self) -> List[int]:
        cur = self.conn.cursor()
        cur.execute("SELECT ticket_number FROM tickets WHERE active = 1 ORDER BY ticket_number")
        return [r[0] for r in cur.fetchall()]

    def get_ticket_owner(self, ticket_number: int) -> Optional[str]:
        cur = self.conn.cursor()
        cur.execute("SELECT telegram_id FROM tickets WHERE ticket_number = ?", (ticket_number,))
        r = cur.fetchone()
        return r[0] if r else None

    # payments
    def record_payment(self, telegram_id: str, invoice_id: str, amount: float, tx_hash: Optional[str]=None, confirmed: bool=False):
        cur = self.conn.cursor()
        cur.execute("INSERT INTO payments (telegram_id, invoice_id, amount, tx_hash, confirmed) VALUES (?, ?, ?, ?, ?)",
                    (telegram_id, invoice_id, amount, tx_hash or "", int(bool(confirmed))))
        self.conn.commit()

    def confirm_payment_by_invoice(self, invoice_id: str, tx_hash: str) -> Optional[Dict]:
        cur = self.conn.cursor()
        cur.execute("SELECT id, telegram_id, amount FROM payments WHERE invoice_id = ? AND confirmed = 0 ORDER BY id DESC LIMIT 1", (invoice_id,))
        row = cur.fetchone()
        if not row:
            return None
        payment_id, telegram_id, amount = row
        cur.execute("UPDATE payments SET confirmed = 1, tx_hash = ? WHERE id = ?", (tx_hash, payment_id))
        self.conn.commit()
        return {"telegram_id": telegram_id, "amount": amount, "invoice_id": invoice_id, "payment_id": payment_id}

    def save_draw(self, winners: List[int], prizes: List[float]):
        cur = self.conn.cursor()
        winners_str = ",".join(map(str, winners))
        prizes_str = ",".join(map(str, prizes))
        cur.execute("INSERT INTO draws (winners, prizes) VALUES (?, ?)", (winners_str, prizes_str))
        self.conn.commit()

# -------------------- CORE (логика) --------------------
class LotteryCore:
    def __init__(self, db: DB):
        self.db = db
        cur = self.db.conn.cursor()
        cur.execute("SELECT MAX(ticket_number) FROM tickets")
        r = cur.fetchone()
        self.next_ticket = (r[0] or 0) + 1

    def available(self) -> int:
        return max(0, MAX_TICKETS - self.db.count_sold_tickets())

    def reserve_tickets(self, telegram_id: str, qty: int) -> Dict:
        if qty < 1 or qty > 100:
            raise ValueError("Можно покупать от 1 до 100 билетов")
        if qty > self.available():
            raise ValueError("Недостаточно билетов в наличии")

        tickets = [self.next_ticket + i for i in range(qty)]
        self.db.ensure_user(telegram_id)
        self.db.add_tickets(telegram_id, tickets)
        self.next_ticket += qty

        total_price = round(qty * TICKET_PRICE, 6)
        # invoice id — уникальный ключ для отслеживания оплаты (string)
        invoice_id = f"{telegram_id}-{tickets[0]}-{tickets[-1]}"
        # сохраняем платёж как ожидание
        self.db.record_payment(telegram_id, invoice_id, total_price, tx_hash=None, confirmed=False)

        return {"reserved": tickets, "total_price": total_price, "invoice_id": invoice_id}

    def activate_payment(self, invoice_id: str, tx_hash: str) -> Dict:
        """Вызывается при webhook от CryptoBot — активирует билеты, возвращает список активированных"""
        info = self.db.confirm_payment_by_invoice(invoice_id, tx_hash)
        if not info:
            return {"ok": False, "reason": "invoice_not_found_or_already_confirmed"}
        telegram_id = info["telegram_id"]
        amount = info["amount"]
        expected_qty = int(round(amount / TICKET_PRICE))
        # В базе найдём последние ожидающие активации билеты этого пользователя
        cur = self.db.conn.cursor()
        cur.execute("SELECT ticket_number FROM tickets WHERE telegram_id = ? AND active = 0 ORDER BY ticket_number DESC LIMIT ?",
                    (telegram_id, expected_qty))
        rows = cur.fetchall()
        ticket_numbers = [r[0] for r in rows]
        if ticket_numbers:
            self.db.activate_tickets(ticket_numbers)
        return {"ok": True, "activated": ticket_numbers, "telegram_id": telegram_id, "amount": amount}

    def draw_winners(self) -> Dict:
        sold = self.db.count_sold_tickets()
        if sold < MAX_TICKETS:
            raise RuntimeError("Лотерея ещё не завершена — продано меньше необходимых билетов")
        all_tickets = self.db.get_all_active_tickets()
        if len(all_tickets) < 3:
            raise RuntimeError("Недостаточно активных билетов")
        winners = random.sample(all_tickets, 3)
        payouts = {}
        for i, t in enumerate(winners):
            owner = self.db.get_ticket_owner(t)
            payouts[owner] = payouts.get(owner, 0.0) + PRIZES[i]
        self.db.save_draw(winners, list(PRIZES))
        return {"winners": winners, "payouts": payouts, "bot_fee": BOT_FEE}

# -------------------- Инициализация --------------------
db = DB(DB_PATH)
core = LotteryCore(db)

# CryptoBot API клиент
crypto = None
if CRYPTOBOT_API_TOKEN:
    crypto = AioCryptoPay(token=CRYPTOBOT_API_TOKEN, network=Networks.MAIN_NET)
    logger.info("CryptoBot API инициализирован")
else:
    logger.warning("CRYPTOBOT_API_TOKEN не задан — автоматические инвойсы отключены")

# -------------------- Flask для webhook --------------------
flask_app = Flask("crypto_webhook")

@flask_app.route("/crypto_webhook", methods=["POST"])
def crypto_webhook():
    """
    Ожидаемый формат JSON от CryptoBot (договариваемся сами):
    {
      "invoice_id": "...",
      "amount": 1.0,
      "tx_hash": "0x...",
      "status": "confirmed"
    }
    Header: X-WEBHOOK-SECRET: <CRYPTO_WEBHOOK_SECRET>
    """
    secret = request.headers.get("X-WEBHOOK-SECRET", "")
    if secret != CRYPTO_WEBHOOK_SECRET:
        logger.warning("Webhook secret mismatch")
        abort(403)

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"ok": False, "reason": "no_json"}), 400

    invoice_id = data.get("invoice_id")
    tx_hash = data.get("tx_hash")
    status = data.get("status", "").lower()
    amount = data.get("amount")

    if not invoice_id or not tx_hash or not amount:
        return jsonify({"ok": False, "reason": "missing_fields"}), 400

    if status != "confirmed":
        return jsonify({"ok": False, "reason": "not_confirmed"}), 400

    res = core.activate_payment(invoice_id, tx_hash)
    if not res.get("ok"):
        return jsonify({"ok": False, "reason": res.get("reason")}), 400

    # notify user via Telegram (асинхронно — запустим в фоне)
    telegram_id = res["telegram_id"]
    activated = res["activated"]
    # отправка уведомления делаем через отдельный поток, т.к. Flask и бот работают в одном процессе
    def notify():
        try:
            app = ApplicationBuilder().token(BOT_TOKEN).build()
            # использую short-lived запуск: создадим приложение, отправим, закрыли.
            # NB: это простая реализация уведомления. В проде лучше иметь глобальный объект приложения.
            async def _send():
                await app.initialize()
                await app.start()
                try:
                    await app.bot.send_message(chat_id=int(telegram_id),
                                               text=f"Оплата подтверждена. Ваши билеты активированы: {activated}")
                except Exception as e:
                    logger.exception("Notify failed: %s", e)
                await app.stop()
                await app.shutdown()

            import asyncio
            asyncio.run(_send())
        except Exception as e:
            logger.exception("Background notify failed: %s", e)

    threading.Thread(target=notify, daemon=True).start()

    return jsonify({"ok": True, "activated": activated})

# -------------------- Telegram handlers --------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🎟️ Купить билеты", callback_data="buy")],
        [InlineKeyboardButton("📊 Прогресс продаж", callback_data="progress")],
        [InlineKeyboardButton("📜 Мои билеты", callback_data="mytickets")],
        [InlineKeyboardButton("🏆 Топ-покупатели", callback_data="top")],
        [InlineKeyboardButton("ℹ️ Правила", callback_data="rules")]
    ]
    await update.message.reply_text("Добро пожаловать в Lottery+!\nВыбирай:", reply_markup=InlineKeyboardMarkup(keyboard))

async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    user = str(q.from_user.id)
    if data == "buy":
        await q.message.reply_text("Сколько билетов хотите купить? Введите число от 1 до 100 (например: 5)")
        return
    if data == "progress":
        sold = db.count_sold_tickets()
        await q.message.reply_text(f"Продано {sold} из {MAX_TICKETS} билетов.")
        return
    if data == "mytickets":
        tickets = db.get_user_tickets(user)
        await q.message.reply_text(f"Ваши билеты: {tickets if tickets else 'пока нет'}")
        return
    if data == "top":
        cur = db.conn.cursor()
        cur.execute("SELECT telegram_id, COUNT(ticket_number) as cnt FROM tickets WHERE active = 1 GROUP BY telegram_id ORDER BY cnt DESC LIMIT 10")
        rows = cur.fetchall()
        if not rows:
            await q.message.reply_text("Пока нет покупателей.")
            return
        text = "\n".join([f"{r[0]} — {r[1]} билетов" for r in rows])
        await q.message.reply_text(text)
        return
    if data == "rules":
        await q.message.reply_text(f"Правила:\n— Цена: {TICKET_PRICE} TON\n— Всего: {MAX_TICKETS} билетов\n— Призы: {PRIZES}\n— Комиссия: {BOT_FEE} TON")
        return

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    user = str(update.message.from_user.id)
    if txt.isdigit():
        qty = int(txt)
        try:
            res = core.reserve_tickets(user, qty)
            
            # Формируем сообщение
            instruction = (f"✅ Зарезервировано билетов: {qty}\n"
                           f"🎫 Номера: {res['reserved'][0]}-{res['reserved'][-1]}\n"
                           f"💰 Сумма: {res['total_price']} TON\n"
                           f"📝 Invoice ID: {res['invoice_id']}\n\n")
            
            # Если есть CryptoBot API — создаём автоматический инвойс
            if crypto:
                try:
                    invoice = await crypto.create_invoice(
                        asset='TON',
                        amount=str(res['total_price']),
                        description=f"Lottery+ билеты #{res['reserved'][0]}-{res['reserved'][-1]}",
                        payload=res['invoice_id'],  # Используем invoice_id как payload
                        expires_in=3600  # 1 час
                    )
                    # Кнопка с прямой ссылкой на оплату
                    keyboard = [[InlineKeyboardButton("💳 Оплатить", url=invoice.bot_invoice_url)]]
                    instruction += "Нажмите кнопку ниже, чтобы оплатить через CryptoBot.\nПосле оплаты ваши билеты автоматически активируются! 🎉"
                    await update.message.reply_text(instruction, reply_markup=InlineKeyboardMarkup(keyboard))
                    logger.info(f"Создан инвойс {invoice.invoice_id} для пользователя {user}")
                except Exception as e:
                    logger.error(f"Ошибка создания инвойса: {e}")
                    # Fallback на ручную оплату
                    instruction += f"Оплатите через @CryptoBot и укажите в комментарии: {res['invoice_id']}"
                    keyboard = [[InlineKeyboardButton("💳 Открыть CryptoBot", url="https://t.me/CryptoBot")]]
                    await update.message.reply_text(instruction, reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                # Если нет API токена — даём инструкцию вручную
                instruction += f"Оплатите через @CryptoBot и укажите в комментарии: {res['invoice_id']}"
                keyboard = [[InlineKeyboardButton("💳 Открыть CryptoBot", url="https://t.me/CryptoBot")]]
                await update.message.reply_text(instruction, reply_markup=InlineKeyboardMarkup(keyboard))
                
        except ValueError as e:
            await update.message.reply_text(str(e))
        return

    await update.message.reply_text("Не понял. Нажми /start и выбери действие.")

# Ручное подтверждение (только для теста/админа)
async def confirm_tx_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /confirm_tx <invoice_id> <tx_hash>
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Использование: /confirm_tx <invoice_id> <tx_hash>")
        return
    invoice_id = context.args[0]
    tx_hash = context.args[1]
    res = core.activate_payment(invoice_id, tx_hash)
    if not res.get("ok"):
        await update.message.reply_text(f"Ошибка активации: {res.get('reason')}")
        return
    await update.message.reply_text(f"Активировано билетов: {res.get('activated')} для пользователя {res.get('telegram_id')}")

# Админ: запуск розыгрыша (OWNER_ID)
async def draw_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not OWNER_ID or update.message.from_user.id != OWNER_ID:
        await update.message.reply_text("Только владелец может это сделать.")
        return
    try:
        res = core.draw_winners()
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")
        return
    # Здесь нужно отправлять выплаты через CryptoBot API — пока просто показываем резултат
    payouts_lines = [f"{owner}: {amount} TON" for owner, amount in res["payouts"].items()]
    await update.message.reply_text("Розыгрыш завершён!\n"
                                    f"Победители (ticket numbers): {res['winners']}\n"
                                    f"Выплаты:\n" + "\n".join(payouts_lines) + f"\nКомиссия бота: {res['bot_fee']} TON")

# Админ: статус
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not OWNER_ID or update.message.from_user.id != OWNER_ID:
        await update.message.reply_text("Только владелец может это сделать.")
        return
    sold = db.count_sold_tickets()
    await update.message.reply_text(f"Продано {sold}/{MAX_TICKETS} билетов. Следующий номер: {core.next_ticket}")

# -------------------- Запуск Flask в отдельном потоке --------------------
def run_flask():
    # Во время разработки на Replit обязательно выставить порт (PORT), иначе Replit не примет
    logger.info("Starting Flask webhook on port %s", PORT)
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# -------------------- MAIN: запуск Telegram bot и Flask --------------------
def main():
    # Запускаем Flask в фоне
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Telegram
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(CommandHandler("confirm_tx", confirm_tx_cmd))
    app.add_handler(CommandHandler("draw", draw_cmd))
    app.add_handler(CommandHandler("status", status_cmd))

    logger.info("Запускаю Telegram polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
