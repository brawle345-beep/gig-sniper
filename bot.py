# language: Python 3.11, file: bot.py
import os
import time
import json
import asyncio
import logging
from threading import Thread
from html import escape

import aiohttp
import asyncpg
from flask import Flask
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery, TelegramObject,
)
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
FEED_URL = "https://raw.githubusercontent.com/brawle345-beep/gig-sniper/main/feed.json"

ADMIN_ID = "7183342708"
MAINTENANCE_MODE = False

# ---------- База ----------
_pool: asyncpg.Pool | None = None

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10, command_timeout=10)
    return _pool

async def init_db():
    p = await get_pool()
    async with p.acquire() as c:
        await c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                premium_until TIMESTAMP WITH TIME ZONE
            );
            CREATE TABLE IF NOT EXISTS referrals (
                referrer BIGINT NOT NULL,
                referred BIGINT NOT NULL,
                PRIMARY KEY (referrer, referred),
                UNIQUE (referred)
            );
        """)

async def get_premium_until(user_id: int):
    p = await get_pool()
    async with p.acquire() as c:
        row = await c.fetchrow("SELECT premium_until FROM users WHERE user_id = $1", user_id)
    return row["premium_until"] if row else None

async def add_premium_days(user_id: int, days: int):
    p = await get_pool()
    async with p.acquire() as c:
        await c.execute("""
            INSERT INTO users (user_id, premium_until)
            VALUES ($1, NOW() + ($2 || ' days')::interval)
            ON CONFLICT (user_id) DO UPDATE
            SET premium_until = GREATEST(COALESCE(users.premium_until, NOW()), NOW())
                                + ($2 || ' days')::interval
        """, user_id, str(days))

async def has_premium(user_id: int) -> bool:
    if str(user_id) == ADMIN_ID: return True
    until = await get_premium_until(user_id)
    return bool(until and until.timestamp() > time.time())

async def apply_referral(ref_id: int, new_uid: int) -> bool:
    if ref_id == new_uid: return False
    p = await get_pool()
    async with p.acquire() as c:
        async with c.transaction():
            claimed = await c.fetchval("""
                INSERT INTO referrals (referrer, referred) VALUES ($1, $2)
                ON CONFLICT DO NOTHING RETURNING referrer
            """, ref_id, new_uid)
            if claimed is None: return False
            await c.execute("""
                INSERT INTO users (user_id, premium_until)
                VALUES ($1, NOW() + interval '7 days')
                ON CONFLICT (user_id) DO UPDATE
                SET premium_until = GREATEST(COALESCE(users.premium_until, NOW()), NOW())
                                    + interval '7 days'
            """, ref_id)
            return True

# ---------- Лента ----------
_feed = {"data": None, "ts": 0.0}

async def fetch_feed() -> list:
    now = time.time()
    if _feed["data"] is not None and now - _feed["ts"] < 30:
        return _feed["data"]
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(FEED_URL) as r:
                if r.status != 200: return _feed["data"] or []
                raw_text = await r.text()
                data = json.loads(raw_text)
                if isinstance(data, dict): data = data.get("jobs", [])
                _feed["data"] = data
                _feed["ts"] = now
                return data
    except Exception as e:
        log.warning("feed: %s", e)
        return _feed["data"] or []

# ---------- Flask (Render keepalive) ----------
app = Flask(__name__)
@app.route("/")
def home(): return "Bot is running!"
def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), threaded=True)

# ---------- Aiogram ----------
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

class Maintenance(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        if MAINTENANCE_MODE and not (event.from_user and str(event.from_user.id) == ADMIN_ID):
            if isinstance(event, Message):
                await event.answer("🛠 <b>Бот на обслуживании.</b> Загляни чуть позже.")
            elif isinstance(event, CallbackQuery):
                await event.answer("🛠 Бот на обслуживании.", show_alert=True)
            return
        return await handler(event, data)

dp.message.middleware(Maintenance())
dp.callback_query.middleware(Maintenance())

# ---------- Меню ----------
def get_main_menu(is_admin=False):
    kb = [
        [InlineKeyboardButton(text="🔥 Свежие вакансии", callback_data="show_jobs")],
        [InlineKeyboardButton(text="💎 Купить Премиум", callback_data="buy_premium")],
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="my_status")],
        [InlineKeyboardButton(text="🎁 Пригласить друга", callback_data="invite")],
        [InlineKeyboardButton(text="📢 Наш канал", url="https://t.me/gigsnipervakansii")],
    ]
    if is_admin:
        kb.insert(0, [InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

async def safe_edit(message: Message, text: str, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup, disable_web_page_preview=True)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            log.warning("edit: %s", e)

# ---------- Хендлеры ----------
@dp.message(CommandStart())
async def start(message: Message):
    uid = message.from_user.id
    is_admin = str(uid) == ADMIN_ID

    until = await get_premium_until(uid)
    if until is None:
        await add_premium_days(uid, 0)
        if not is_admin:
            try:
                name = escape(message.from_user.first_name)
                if message.from_user.username: name += f" (@{message.from_user.username})"
                await bot.send_message(ADMIN_ID, f"👤 <b>Новый юзер</b>\n{name}\nID: <code>{uid}</code>")
            except Exception: pass

    parts = message.text.split()
    if len(parts) > 1 and parts[1].startswith("ref_"):
        raw = parts[1][4:]
        if raw.isdigit() and await apply_referral(int(raw), uid):
            try:
                await bot.send_message(int(raw), f"🎁 По твоей ссылке перешёл {escape(message.from_user.first_name)}! +7 дней.")
            except Exception: pass

    await message.answer(
        f"👋 <b>Привет, {escape(message.from_user.first_name)}!</b>\n\n"
        "🤖 Это <b>Gig Sniper</b> — радар свежих IT-вакансий и заказов.\n\n"
        "<blockquote>Сканируем биржи и Telegram-каналы каждые 10 минут, отсеиваем шлак, "
        "присылаем только актуальное в долларах и рублях.</blockquote>\n\n"
        "⚡️ <b>Выбери действие:</b>",
        reply_markup=get_main_menu(is_admin),
    )

@dp.callback_query(F.data == "show_jobs")
async def show_jobs(cb: CallbackQuery):
    await safe_edit(cb.message, "🔄 Загружаю свежие вакансии...")
    jobs = await fetch_feed()
    is_admin = str(cb.from_user.id) == ADMIN_ID

    if not jobs:
        await safe_edit(cb.message, "Пока новых нет. Загляни позже!", reply_markup=get_main_menu(is_admin))
        await cb.answer()
        return

    is_prem = await has_premium(cb.from_user.id)
    limit = 10 if is_prem else 3
    text = "🔥 <b>Свежие вакансии:</b>\n\n"
    for j in jobs[:limit]:
        text += (f"▪️ <a href='{j.get('url','')}'>{j.get('title','Без названия')}</a>\n"
                 f"🏢 {j.get('company','Не указана')}\n\n")
    if not is_prem:
        text += f"🔒 <i>Показано {limit} из {len(jobs)}.</i>\n💎 Купи Премиум для полного списка."

    await safe_edit(cb.message, text, reply_markup=get_main_menu(is_admin))
    await cb.answer()

@dp.callback_query(F.data == "buy_premium")
async def buy_premium(cb: CallbackQuery):
    await safe_edit(cb.message, "💎 <b>Счёт отправлен ниже.</b>\n\nПосле оплаты доступ откроется сам.",
                    reply_markup=get_main_menu(str(cb.from_user.id) == ADMIN_ID))
    await bot.send_invoice(
        chat_id=cb.from_user.id,
        title="Премиум доступ",
        description="Полный доступ к базе вакансий на 30 дней",
        payload="premium_30",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Доступ", amount=250)],
    )
    await cb.answer()

@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)

@dp.message(F.successful_payment)
async def on_paid(message: Message):
    if message.successful_payment.invoice_payload != "premium_30":
        return
    try:
        await add_premium_days(message.from_user.id, 30)
        name = escape(message.from_user.first_name)
        if message.from_user.username: name += f" (@{message.from_user.username})"
        await bot.send_message(ADMIN_ID, f"💰 <b>Новая оплата!</b>\nЮзер: {name}\nID: <code>{message.from_user.id}</code>")
    except Exception as e:
        log.error("Ошибка при начислении: %s", e)
        
    await message.answer("✅ <b>Оплата прошла!</b> Доступ на 30 дней открыт.",
                         reply_markup=get_main_menu(str(message.from_user.id) == ADMIN_ID))

@dp.callback_query(F.data == "invite")
async def invite(cb: CallbackQuery):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{cb.from_user.id}"
    await safe_edit(cb.message,
        f"🎁 <b>Партнёрская программа</b>\n\nТвоя ссылка:\n<code>{link}</code>\n\n"
        "<blockquote>За каждого друга — <b>+7 дней Премиума</b>.</blockquote>",
        reply_markup=get_main_menu(str(cb.from_user.id) == ADMIN_ID))
    await cb.answer()

@dp.callback_query(F.data == "my_status")
async def my_status(cb: CallbackQuery):
    uid = str(cb.from_user.id)
    if uid == ADMIN_ID:
        text = "👑 <b>Статус:</b> Админ\n<blockquote>Безлимит навсегда.</blockquote>"
    else:
        until = await get_premium_until(cb.from_user.id)
        if until and until.timestamp() > time.time():
            days = int((until.timestamp() - time.time()) / 86400)
            text = f"💎 <b>Статус:</b> Премиум\n<blockquote>Осталось дней: <b>{days}</b></blockquote>"
        else:
            text = "👤 <b>Статус:</b> Бесплатный\n<blockquote>Доступно 3 вакансии.</blockquote>"
    await safe_edit(cb.message, text, reply_markup=get_main_menu(uid == ADMIN_ID))
    await cb.answer()

@dp.callback_query(F.data == "admin_panel")
async def admin_panel(cb: CallbackQuery):
    if str(cb.from_user.id) != ADMIN_ID: return
    p = await get_pool()
    async with p.acquire() as c:
        total = await c.fetchval("SELECT COUNT(*) FROM users")
        premium_users = await c.fetch("SELECT user_id, premium_until FROM users WHERE premium_until > NOW()")
    
    prem_count = len(premium_users)
    prem_list = ""
    for u in premium_users:
        if str(u['user_id']) == ADMIN_ID: continue
        until_str = u['premium_until'].strftime("%d.%m.%Y")
        prem_list += f"🔹 <code>{u['user_id']}</code> — до {until_str}\n"

    text = (
        f"🛠 <b>Админ-панель</b>\n\n"
        f"👥 Юзеров: <b>{total}</b>\n"
        f"💎 Активных подписок: <b>{prem_count}</b>\n"
        f"💰 Доход: <b>{prem_count * 250} ⭐</b>\n\n"
    )
    if prem_list:
        text += f"<b>Список купивших (ID):</b>\n{prem_list}"
    else:
        text += "<i>Пока никто не купил.</i>"

    await safe_edit(cb.message, text, reply_markup=get_main_menu(True))
    await cb.answer()

@dp.message(F.text.startswith("/give "))
async def give_premium(message: Message):
    if str(message.from_user.id) != ADMIN_ID: return
    try:
        _, uid, days = message.text.split()
        await add_premium_days(int(uid), int(days))
        await message.answer(f"✅ Выдал {days} дней юзеру <code>{uid}</code>")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}\nФормат: <code>/give 123456789 30</code>")

# ---------- Запуск ----------
async def main():
    log.info("init db...")
    await init_db()
    log.info("bot started")
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot)

if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    asyncio.run(main())
