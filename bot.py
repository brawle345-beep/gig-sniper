# language: Python 3.11, file: bot.py
# pip install aiogram==3.20.0 asyncpg==0.30.0 aiohttp==3.11.0 flask==3.1.2 beautifulsoup4==4.12.3 lxml

import os
import time
import re
import asyncio
import logging
from threading import Thread
from html import escape

import aiohttp
import asyncpg
from bs4 import BeautifulSoup
from flask import Flask
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery, TelegramObject, InlineQuery, InlineQueryResultArticle, InputTextMessageContent
)
from aiogram.filters import CommandStart, Command
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

ADMIN_ID = "7183342708"
MAINTENANCE_MODE = False

CHANNEL_ID = "@gigsnipervakansii"
CHANNEL_URL = "https://t.me/gigsnipervakansii"

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
                premium_until TIMESTAMP WITH TIME ZONE,
                is_paid BOOLEAN DEFAULT FALSE
            );
            ALTER TABLE users ADD COLUMN IF NOT EXISTS is_paid BOOLEAN DEFAULT FALSE;
            
            CREATE TABLE IF NOT EXISTS referrals (
                referrer BIGINT NOT NULL,
                referred BIGINT NOT NULL,
                PRIMARY KEY (referrer, referred),
                UNIQUE (referred)
            );
            
            CREATE TABLE IF NOT EXISTS channels (
                name VARCHAR(255) PRIMARY KEY
            );
        """)
        # Добавляем дефолтные каналы, если таблица пустая
        count = await c.fetchval("SELECT COUNT(*) FROM channels")
        if count == 0:
            await c.execute("""
                INSERT INTO channels (name) VALUES 
                ('it_vakansii_jobs'), ('remote_it_jobs'), ('python_jobs')
            """)

async def get_channels() -> list:
    p = await get_pool()
    async with p.acquire() as c:
        rows = await c.fetch("SELECT name FROM channels")
        return [r['name'] for r in rows]

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

async def check_sub(user_id: int) -> bool:
    if str(user_id) == ADMIN_ID: return True
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception:
        return False

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

# ---------- Парсер ----------
KEYWORDS = ["python", "react", "frontend", "backend", "разработчик", "верстальщик", "удаленка", "вакансия", "ищу"]

_feed = {"data": None, "ts": 0.0, "last_sent_id": None}

async def fetch_feed() -> list:
    now = time.time()
    if _feed["data"] is not None and now - _feed["ts"] < 300:
        return _feed["data"]
        
    jobs = []
    headers = {"User-Agent": "Mozilla/5.0"}
    channels = await get_channels()
    
    async with aiohttp.ClientSession(headers=headers) as session:
        for channel in channels:
            url = f"https://t.me/s/{channel}"
            try:
                async with session.get(url, timeout=10) as r:
                    if r.status != 200: continue
                    html = await r.text()
                    soup = BeautifulSoup(html, 'html.parser')
                    messages = soup.find_all('div', class_='tgme_widget_message_wrap')
                    for msg in messages:
                        text_div = msg.find('div', class_='tgme_widget_message_text')
                        if not text_div: continue
                        text = text_div.get_text(separator='\n').strip()
                        if not any(kw.lower() in text.lower() for kw in KEYWORDS): continue
                        match = re.search(r'(?:@|t\.me/)([a-zA-Z0-9_]{5,32})', text)
                        contact_url = f"https://t.me/{match.group(1)}" if match else None
                        link_div = msg.find('a', class_='tgme_widget_message_date')
                        post_url = link_div['href'] if link_div else url
                        jobs.append({"text": text, "contact_url": contact_url, "post_url": post_url, "channel": channel})
            except Exception as e:
                log.warning(f"Ошибка парсинга {channel}: {e}")
                
    jobs = jobs[-20:]
    jobs.reverse()
    _feed["data"] = jobs
    _feed["ts"] = now
    return jobs

# ---------- Авторассылка ----------
async def autopush_worker():
    await asyncio.sleep(10)
    while True:
        try:
            jobs = await fetch_feed()
            if jobs:
                latest_job = jobs[0]
                if _feed["last_sent_id"] != latest_job['post_url']:
                    _feed["last_sent_id"] = latest_job['post_url']
                    
                    p = await get_pool()
                    async with p.acquire() as c:
                        users = await c.fetch("SELECT user_id FROM users WHERE premium_until > NOW()")
                    
                    raw_text = re.sub(r'\n{3,}', '\n\n', latest_job['text'].strip())
                    if len(raw_text) > 400: raw_text = raw_text[:400] + "..."
                    push_text = f"🚨 <b>СВЕЖАЯ ВАКАНСИЯ!</b>\n\n{raw_text}"
                    
                    kb = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="🔥 Открыть в боте", url=f"https://t.me/{(await bot.get_me()).username}?start=push")
                    ]])
                    
                    for u in users:
                        if str(u['user_id']) == ADMIN_ID: continue
                        try:
                            await bot.send_message(u['user_id'], push_text, reply_markup=kb, disable_web_page_preview=True)
                            await asyncio.sleep(0.05)
                        except Exception: pass
        except Exception as e:
            log.error(f"Autopush error: {e}")
        await asyncio.sleep(300)

# ---------- Flask ----------
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
            return
        return await handler(event, data)

dp.message.middleware(Maintenance())
dp.callback_query.middleware(Maintenance())

# ---------- Меню ----------
def get_main_menu(is_admin=False):
    kb = [
        [InlineKeyboardButton(text="🔥 Смотреть вакансии", callback_data="show_jobs:0")],
        [InlineKeyboardButton(text="💎 Премиум-доступ", callback_data="buy_premium"), InlineKeyboardButton(text="👤 Профиль", callback_data="my_status")],
        [InlineKeyboardButton(text="🎁 Пригласить друга", callback_data="invite")],
        [InlineKeyboardButton(text="📢 Наш канал", url=CHANNEL_URL)],
    ]
    if is_admin:
        kb.insert(0, [InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_sub_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")]
    ])

# ---------- Карточки ----------
async def render_job(cb: CallbackQuery, index: int):
    if not await check_sub(cb.from_user.id):
        await cb.message.edit_text("⚠️ <b>Для доступа к вакансиям подпишись на наш канал!</b>", reply_markup=get_sub_menu())
        return

    jobs = await fetch_feed()
    is_prem = await has_premium(cb.from_user.id)
    limit = 10 if is_prem else 3
    jobs = jobs[:limit]
    
    if not jobs:
        await cb.message.edit_text("😴 Пока новых вакансий нет.", reply_markup=get_main_menu(str(cb.from_user.id) == ADMIN_ID))
        return

    index = max(0, min(index, len(jobs) - 1))
    job = jobs[index]
    
    raw_text = re.sub(r'\n{3,}', '\n\n', job['text'].strip())
    if len(raw_text) > 600: raw_text = raw_text[:600].rsplit(' ', 1)[0] + "..."
    
    text = f"<b>💼 Вакансия {index+1} из {len(jobs)}</b>\n<i>Источник: @{job['channel']}</i>\n━━━━━━━━━━━━━━━━━━\n\n{raw_text}"
    
    nav_row = []
    if index > 0: nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"show_jobs:{index-1}"))
    else: nav_row.append(InlineKeyboardButton(text="⬅️", callback_data="ignore"))
    nav_row.append(InlineKeyboardButton(text=f"{index+1}/{len(jobs)}", callback_data="ignore"))
    if index < len(jobs) - 1: nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"show_jobs:{index+1}"))
    else: nav_row.append(InlineKeyboardButton(text="➡️", callback_data="ignore"))

    kb = [nav_row]
    contact_row = []
    if job.get('contact_url'): contact_row.append(InlineKeyboardButton(text="💬 Написать", url=job['contact_url']))
    if job.get('post_url'): contact_row.append(InlineKeyboardButton(text="🔗 Оригинал", url=job['post_url']))
    if contact_row: kb.append(contact_row)
    if not is_prem: kb.append([InlineKeyboardButton(text="💎 Купить Премиум", callback_data="buy_premium")])

    kb.append([InlineKeyboardButton(text="🏠 В меню", callback_data="main_menu")])
    try:
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), disable_web_page_preview=True)
    except TelegramBadRequest: pass

# ---------- Инлайн ----------
@dp.inline_query()
async def inline_search(query: InlineQuery):
    jobs = await fetch_feed()
    results = []
    search_q = query.query.lower()
    
    for i, j in enumerate(jobs[:15]):
        if search_q and search_q not in j['text'].lower(): continue
        raw_text = re.sub(r'\n{3,}', '\n\n', j['text'].strip())[:100] + "..."
        results.append(
            InlineQueryResultArticle(
                id=str(i), title=f"Вакансия из @{j['channel']}", description=raw_text,
                input_message_content=InputTextMessageContent(message_text=f"💼 <b>Вакансия:</b>\n\n{j['text'][:1000]}", parse_mode=ParseMode.HTML)
            )
        )
    await query.answer(results, cache_time=60, is_personal=True)

# ---------- Хендлеры ----------
@dp.message(CommandStart())
async def start(message: Message):
    uid = message.from_user.id
    is_admin = str(uid) == ADMIN_ID
    
    if not await check_sub(uid):
        await message.answer("👋 <b>Привет!</b>\n\nЧтобы пользоваться ботом, подпишись на наш канал с вакансиями.", reply_markup=get_sub_menu())
        return

    until = await get_premium_until(uid)
    if until is None:
        await add_premium_days(uid, 3)
        if not is_admin:
            try:
                name = escape(message.from_user.first_name)
                await bot.send_message(ADMIN_ID, f"👤 <b>Новый юзер</b>\n{name}\nID: <code>{uid}</code>\n🎁 Триал 3 дня.")
            except Exception: pass
            
    parts = message.text.split()
    if len(parts) > 1 and parts[1].startswith("ref_"):
        raw = parts[1][4:]
        if raw.isdigit() and await apply_referral(int(raw), uid):
            try: await bot.send_message(int(raw), f"🎁 По твоей ссылке перешёл {escape(message.from_user.first_name)}! +7 дней.")
            except Exception: pass
            
    await message.answer(
        f"👋 <b>Привет, {escape(message.from_user.first_name)}!</b>\n\n"
        "🚀 <b>Gig Sniper</b> — твой радар IT-заказов из Telegram.\n\n"
        "🎁 Тебе активирован <b>3-дневный Премиум</b>.\n\n"
        "<i>Совет: напиши @этот_бот в любом чате, чтобы искать вакансии без входа в бота.</i>",
        reply_markup=get_main_menu(is_admin),
    )

@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(cb: CallbackQuery):
    if await check_sub(cb.from_user.id):
        await cb.message.delete()
        await cb.message.answer("✅ Подписка подтверждена! Жми /start", reply_markup=get_main_menu(str(cb.from_user.id) == ADMIN_ID))
    else:
        await cb.answer("❌ Ты всё еще не подписан!", show_alert=True)

@dp.callback_query(F.data == "main_menu")
async def back_to_menu(cb: CallbackQuery):
    await cb.message.edit_text("⚡️ <b>Главное меню:</b>", reply_markup=get_main_menu(str(cb.from_user.id) == ADMIN_ID))
    await cb.answer()

@dp.callback_query(F.data.startswith("show_jobs:"))
async def show_jobs(cb: CallbackQuery):
    index = int(cb.data.split(":")[1])
    if index == 0 and not cb.message.text.startswith("💼"):
        await cb.message.edit_text("🔄 <i>Сканирую каналы...</i>")
    await render_job(cb, index)
    await cb.answer()

@dp.callback_query(F.data == "ignore")
async def ignore_btn(cb: CallbackQuery): await cb.answer()

@dp.callback_query(F.data == "buy_premium")
async def buy_premium(cb: CallbackQuery):
    await cb.message.edit_text("💎 <b>Счёт отправлен ниже.</b>", reply_markup=get_main_menu(str(cb.from_user.id) == ADMIN_ID))
    await bot.send_invoice(
        chat_id=cb.from_user.id, title="Премиум (30 дней)", description="Полный доступ к вакансиям.",
        payload="premium_30", provider_token="", currency="XTR", prices=[LabeledPrice(label="Доступ", amount=250)],
    )
    await cb.answer()

@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery): await q.answer(ok=True)

@dp.message(F.successful_payment)
async def on_paid(message: Message):
    if message.successful_payment.invoice_payload != "premium_30": return
    try:
        await add_premium_days(message.from_user.id, 30)
        p = await get_pool()
        async with p.acquire() as c: await c.execute("UPDATE users SET is_paid = TRUE WHERE user_id = $1", message.from_user.id)
        await bot.send_message(ADMIN_ID, f"💰 <b>Оплата!</b> ID: <code>{message.from_user.id}</code>")
    except Exception: pass
    await message.answer("✅ <b>Оплата прошла!</b> Доступ на 30 дней открыт.", reply_markup=get_main_menu(str(message.from_user.id) == ADMIN_ID))

@dp.callback_query(F.data == "invite")
async def invite(cb: CallbackQuery):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{cb.from_user.id}"
    await cb.message.edit_text(
        f"🎁 <b>Партнёрская программа</b>\n\nТвоя ссылка:\n<code>{link}</code>\n\n<blockquote>За каждого друга — <b>+7 дней Премиума</b>.</blockquote>",
        reply_markup=get_main_menu(str(cb.from_user.id) == ADMIN_ID))
    await cb.answer()

@dp.callback_query(F.data == "my_status")
async def my_status(cb: CallbackQuery):
    uid = str(cb.from_user.id)
    if uid == ADMIN_ID: text = "👑 <b>Статус:</b> Админ"
    else:
        until = await get_premium_until(cb.from_user.id)
        if until and until.timestamp() > time.time(): text = f"💎 <b>Статус:</b> Премиум (Осталось дней: {(int((until.timestamp() - time.time()) / 86400))})"
        else: text = "👤 <b>Статус:</b> Бесплатный"
    await cb.message.edit_text(text, reply_markup=get_main_menu(uid == ADMIN_ID))
    await cb.answer()

# ---------- Админка ----------
@dp.callback_query(F.data == "admin_panel")
async def admin_panel(cb: CallbackQuery):
    if str(cb.from_user.id) != ADMIN_ID: return
    p = await get_pool()
    async with p.acquire() as c:
        total = await c.fetchval("SELECT COUNT(*) FROM users")
        premium_users = await c.fetch("SELECT user_id, premium_until FROM users WHERE premium_until > NOW() AND is_paid = TRUE")
        trial_users = await c.fetchval("SELECT COUNT(*) FROM users WHERE premium_until > NOW() AND is_paid = FALSE")
        channels = await c.fetch("SELECT name FROM channels")
        
    prem_count = len(premium_users)
    ch_list = ", ".join([f"@{r['name']}" for r in channels])
    
    text = (
        f"🛠 <b>Админ-панель</b>\n\n"
        f"👥 Юзеров: <b>{total}</b>\n"
        f"💎 Купили: <b>{prem_count}</b>\n"
        f"🎁 Триал: <b>{trial_users}</b>\n"
        f"💰 Доход: <b>{prem_count * 250} ⭐</b>\n\n"
        f"📡 <b>Каналы для парсинга:</b>\n<code>{ch_list}</code>\n\n"
        f"<i>Чтобы добавить канал, напиши:</i>\n"
        f"<code>/add_channel имя_канала</code>\n"
        f"<i>Чтобы удалить:</i>\n"
        f"<code>/del_channel имя_канала</code>"
    )
    await cb.message.edit_text(text, reply_markup=get_main_menu(True))
    await cb.answer()

@dp.message(F.text.startswith("/add_channel "))
async def add_channel(message: Message):
    if str(message.from_user.id) != ADMIN_ID: return
    raw_name = message.text.replace("/add_channel ", "").strip()
    clean_name = raw_name.replace("https://t.me/", "").replace("@", "").strip()
    
    p = await get_pool()
    async with p.acquire() as c:
        try:
            await c.execute("INSERT INTO channels (name) VALUES ($1)", clean_name)
            _feed["data"] = None # Сбрасываем кеш, чтобы бот сразу прочитал новый канал
            await message.answer(f"✅ Канал <b>@{clean_name}</b> добавлен в парсер.")
        except asyncpg.exceptions.UniqueViolationError:
            await message.answer(f"⚠️ Канал <b>@{clean_name}</b> уже есть в списке.")

@dp.message(F.text.startswith("/del_channel "))
async def del_channel(message: Message):
    if str(message.from_user.id) != ADMIN_ID: return
    raw_name = message.text.replace("/del_channel ", "").strip()
    clean_name = raw_name.replace("https://t.me/", "").replace("@", "").strip()
    
    p = await get_pool()
    async with p.acquire() as c:
        result = await c.execute("DELETE FROM channels WHERE name = $1", clean_name)
        if result == "DELETE 1":
            _feed["data"] = None
            await message.answer(f"🗑 Канал <b>@{clean_name}</b> удален.")
        else:
            await message.answer(f"❌ Канал <b>@{clean_name}</b> не найден.")

@dp.message(F.text == "/clear_fakes")
async def clear_fakes(message: Message):
    if str(message.from_user.id) != ADMIN_ID: return
    p = await get_pool()
    async with p.acquire() as c: await c.execute("UPDATE users SET premium_until = NOW() WHERE is_paid = FALSE")
    await message.answer("🧹 Фейковые випки сброшены.")

@dp.message(F.text.startswith("/give "))
async def give_premium(message: Message):
    if str(message.from_user.id) != ADMIN_ID: return
    try:
        _, uid, days = message.text.split()
        await add_premium_days(int(uid), int(days))
        await message.answer(f"✅ Выдал {days} дней.")
    except Exception as e: await message.answer(f"❌ Ошибка: {e}")

# ---------- Запуск ----------
async def main():
    log.info("init db...")
    await init_db()
    asyncio.create_task(autopush_worker())
    log.info("bot started")
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot)

if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    asyncio.run(main())
