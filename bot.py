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
    LabeledPrice, PreCheckoutQuery, TelegramObject, InlineQuery, 
    InlineQueryResultArticle, InputTextMessageContent, ErrorEvent
)
from aiogram.filters import CommandStart, Command
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

ADMIN_ID = "7183342708"
CHANNEL_ID = "@gigsniper_channel"
CHANNEL_URL = "https://t.me/gigsniper_channel"

IS_MAINTENANCE = False

class AdminStates(StatesGroup):
    give_id = State()
    give_days = State()
    add_channel = State()
    del_channel = State()

class UserStates(StatesGroup):
    waiting_for_filter = State()
    waiting_for_vacancy = State()

class CVStates(StatesGroup):
    waiting_for_cv = State()
    waiting_for_vacancy = State()

# ---------- База ----------
_pool: asyncpg.Pool | None = None

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=20, command_timeout=10)
    return _pool

async def init_db():
    global IS_MAINTENANCE
    p = await get_pool()
    async with p.acquire() as c:
        await c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                premium_until TIMESTAMP WITH TIME ZONE,
                is_paid BOOLEAN DEFAULT FALSE,
                filter_word VARCHAR(255) DEFAULT NULL
            );
            ALTER TABLE users ADD COLUMN IF NOT EXISTS is_paid BOOLEAN DEFAULT FALSE;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS filter_word VARCHAR(255) DEFAULT NULL;
            
            CREATE TABLE IF NOT EXISTS referrals (
                referrer BIGINT NOT NULL,
                referred BIGINT NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                PRIMARY KEY (referrer, referred),
                UNIQUE (referred)
            );
            ALTER TABLE referrals ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW();
            
            CREATE TABLE IF NOT EXISTS channels (
                name VARCHAR(255) PRIMARY KEY
            );
            
            CREATE TABLE IF NOT EXISTS settings (
                key VARCHAR(50) PRIMARY KEY,
                value VARCHAR(50)
            );
            
            CREATE TABLE IF NOT EXISTS pending_vacancies (
                user_id BIGINT PRIMARY KEY,
                paid_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                text TEXT DEFAULT NULL
            );
            ALTER TABLE pending_vacancies ADD COLUMN IF NOT EXISTS text TEXT DEFAULT NULL;
        """)
        
        count = await c.fetchval("SELECT COUNT(*) FROM channels")
        if count == 0:
            await c.execute("""
                INSERT INTO channels (name) VALUES 
                ('it_vakansii_jobs'), ('remote_it_jobs'), ('python_jobs')
            """)
            
        maint_val = await c.fetchval("SELECT value FROM settings WHERE key = 'maintenance'")
        if maint_val is None:
            await c.execute("INSERT INTO settings (key, value) VALUES ('maintenance', '0')")
            IS_MAINTENANCE = False
        else:
            IS_MAINTENANCE = (maint_val == '1')

async def get_channels() -> list:
    try:
        p = await get_pool()
        async with p.acquire() as c:
            rows = await c.fetch("SELECT name FROM channels")
            return [r['name'] for r in rows]
    except Exception as e:
        log.error(f"DB error get_channels: {e}")
        return []

async def get_premium_until(user_id: int):
    try:
        p = await get_pool()
        async with p.acquire() as c:
            row = await c.fetchrow("SELECT premium_until FROM users WHERE user_id = $1", user_id)
        return row["premium_until"] if row else None
    except Exception:
        return None

async def get_user_filter(user_id: int):
    try:
        p = await get_pool()
        async with p.acquire() as c:
            row = await c.fetchrow("SELECT filter_word FROM users WHERE user_id = $1", user_id)
        return row["filter_word"] if row else None
    except Exception:
        return None

async def add_premium_days(user_id: int, days: int):
    try:
        p = await get_pool()
        async with p.acquire() as c:
            await c.execute("""
                INSERT INTO users (user_id, premium_until)
                VALUES ($1, NOW() + ($2 || ' days')::interval)
                ON CONFLICT (user_id) DO UPDATE
                SET premium_until = GREATEST(COALESCE(users.premium_until, NOW()), NOW())
                                    + ($2 || ' days')::interval
            """, user_id, str(days))
    except Exception as e:
        log.error(f"Error add_premium: {e}")

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
        return True 

async def apply_referral(ref_id: int, new_uid: int) -> bool:
    if ref_id == new_uid: return False
    try:
        p = await get_pool()
        async with p.acquire() as c:
            async with c.transaction():
                today_count = await c.fetchval("""
                    SELECT COUNT(*) FROM referrals 
                    WHERE referrer = $1 AND created_at > NOW() - interval '1 day'
                """, ref_id)
                if today_count >= 3: return False
                
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
    except Exception:
        return False

# ---------- ИИ Адаптер ----------
async def generate_adapted_cv(cv_text: str, vacancy_text: str) -> str:
    key = GROQ_API_KEY.strip() if GROQ_API_KEY else ""
    if not key:
        return "⚠️ Ошибка: API ключ нейросети не настроен в Render."

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json"
    }
    prompt = f"""Ты HR. Перепиши резюме под вакансию. Выдели ключевые навыки, добавь нужные слова. Ответь ТОЛЬКО текстом резюме без лишних вступлений.
ВАКАНСИЯ:
{vacancy_text}

РЕЗЮМЕ:
{cv_text}
"""
    data = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.5
    }
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, headers=headers, json=data, timeout=30) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    return result['choices'][0]['message']['content']
                else:
                    error_text = await resp.text()
                    log.error(f"Groq API Error {resp.status}: {error_text}")
                    return f"⚠️ Ошибка API (Код {resp.status}). Проверь логи."
        except Exception as e:
            log.error(f"Groq connection error: {e}")
            return "⚠️ Таймаут соединения с нейросетью."

# ---------- Парсер ----------
REQUIRED_WORDS = ["python", "react", "frontend", "backend", "разработчик", "верстальщик", "удаленка", "вакансия", "ищу", "требуется", "looking for", "hiring"]
STOP_WORDS = ["курс", "промокод", "скидка", "подпишись", "реклама", "инфобизнес", "наставничество", "база", "слив", "обучение", "заработок", "доход", "крипт", "бесплатно", "лайфхак", "марафон", "вебинар"]

_feed = {"data": None, "ts": 0.0}
_feed_lock = asyncio.Lock()

def is_valid_job(text: str) -> bool:
    text_lower = text.lower()
    if len(text_lower) < 50 or len(text_lower) > 4000: return False
    if any(sw in text_lower for sw in STOP_WORDS): return False
    if not any(rw in text_lower for rw in REQUIRED_WORDS): return False
    if not re.search(r'(?:@|t\.me/)([a-zA-Z0-9_]{5,32})', text): return False
    return True

async def fetch_feed() -> list:
    async with _feed_lock:
        now = time.time()
        if _feed["data"] is not None and now - _feed["ts"] < 300:
            return _feed["data"]
            
        jobs = []
        headers = {"User-Agent": "Mozilla/5.0"}
        channels = await get_channels()
        if not channels: return []

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
                            if not is_valid_job(text): continue
                            match = re.search(r'(?:@|t\.me/)([a-zA-Z0-9_]{5,32})', text)
                            contact_url = f"https://t.me/{match.group(1)}" if match else None
                            link_div = msg.find('a', class_='tgme_widget_message_date')
                            post_url = link_div['href'] if link_div else url
                            jobs.append({"text": text, "contact_url": contact_url, "post_url": post_url, "channel": channel})
                except Exception:
                    pass
                    
        jobs = jobs[-50:] 
        jobs.reverse()
        _feed["data"] = jobs
        _feed["ts"] = now
        return jobs

# ---------- Авторассылка ----------
async def autopush_worker():
    await asyncio.sleep(10)
    while True:
        try:
            bot_me = await bot.get_me()
            if not IS_MAINTENANCE:
                jobs = await fetch_feed()
                if jobs:
                    latest_job = jobs[0]
                    p = await get_pool()
                    async with p.acquire() as c:
                        last_sent_id = await c.fetchval("SELECT value FROM settings WHERE key = 'last_pushed'")
                    
                    if last_sent_id != latest_job['post_url']:
                        async with p.acquire() as c:
                            await c.execute("""
                                INSERT INTO settings (key, value) VALUES ('last_pushed', $1)
                                ON CONFLICT (key) DO UPDATE SET value = $1
                            """, latest_job['post_url'])
                            users = await c.fetch("SELECT user_id FROM users WHERE premium_until > NOW()")
                        
                        raw_text = re.sub(r'\n{3,}', '\n\n', latest_job['text'].strip())
                        if len(raw_text) > 400: raw_text = raw_text[:400] + "..."
                        push_text = f"🚨 <b>СВЕЖАЯ ВАКАНСИЯ!</b>\n\n{escape(raw_text)}"
                        kb = InlineKeyboardMarkup(inline_keyboard=[[
                            InlineKeyboardButton(text="🔥 Открыть в боте", url=f"https://t.me/{bot_me.username}?start=push")
                        ]])
                        
                        for u in users:
                            if str(u['user_id']) == ADMIN_ID: continue
                            try:
                                await bot.send_message(u['user_id'], push_text, reply_markup=kb, disable_web_page_preview=True)
                                await asyncio.sleep(0.05)
                            except Exception: pass
        except Exception:
            pass
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

@dp.errors()
async def error_handler(event: ErrorEvent):
    log.error(f"Unhandled error: {event.exception}", exc_info=event.exception)

class MaintenanceMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        if isinstance(event, (PreCheckoutQuery, Message)) and (
            isinstance(event, PreCheckoutQuery) or getattr(event, 'successful_payment', None)
        ):
            return await handler(event, data)

        if IS_MAINTENANCE:
            if hasattr(event, 'from_user') and event.from_user and str(event.from_user.id) == ADMIN_ID:
                return await handler(event, data)
            if isinstance(event, Message):
                await event.answer("🛠 <b>Бот на техническом обслуживании.</b>")
            elif isinstance(event, CallbackQuery):
                await event.answer("🛠 Бот на тех. обслуживании.", show_alert=True)
            elif isinstance(event, InlineQuery):
                await event.answer([], cache_time=0, is_personal=True)
            return
        return await handler(event, data)

dp.message.middleware(MaintenanceMiddleware())
dp.callback_query.middleware(MaintenanceMiddleware())
dp.inline_query.middleware(MaintenanceMiddleware())
dp.pre_checkout_query.middleware(MaintenanceMiddleware())

# ---------- Хелперы и Меню ----------
async def safe_edit(message: Message, text: str, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup, disable_web_page_preview=True)
    except TelegramBadRequest:
        pass

async def build_main_menu(user_id: int, is_admin=False):
    kb = [
        [InlineKeyboardButton(text="🔥 Смотреть вакансии", callback_data="show_jobs:0")],
        [InlineKeyboardButton(text="🤖 AI Адаптер резюме", callback_data="cv_adapter")],
        [InlineKeyboardButton(text="💎 Премиум-доступ", callback_data="buy_premium"), InlineKeyboardButton(text="👤 Профиль", callback_data="my_status")],
        [InlineKeyboardButton(text="📝 Разместить вакансию", callback_data="post_vacancy")],
        [InlineKeyboardButton(text="🎁 Пригласить друга", callback_data="invite")],
    ]
    
    user_filter = await get_user_filter(user_id)
    if user_filter:
        short_filter = user_filter[:15] + "..." if len(user_filter) > 15 else user_filter
        kb.insert(2, [InlineKeyboardButton(text=f"❌ Сбросить фильтр ({escape(short_filter)})", callback_data="reset_filter_menu")])
        
    kb.append([InlineKeyboardButton(text="📢 Наш канал", url=CHANNEL_URL)])
    if is_admin:
        kb.insert(0, [InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_sub_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")]
    ])

def get_admin_menu():
    maint_btn = "🟢 Тех. работы: ВЫКЛ" if IS_MAINTENANCE else "🔴 Тех. работы: ВКЛ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Выдать Премиум", callback_data="admin_give"), InlineKeyboardButton(text="🧹 Очистить фейки", callback_data="admin_clear")],
        [InlineKeyboardButton(text="➕ Добавить канал", callback_data="admin_add_ch"), InlineKeyboardButton(text="🗑 Удалить канал", callback_data="admin_del_ch")],
        [InlineKeyboardButton(text=maint_btn, callback_data="admin_toggle_maint")],
        [InlineKeyboardButton(text="🏠 В меню", callback_data="main_menu")]
    ])

def get_cancel_menu():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")]])

def get_user_cancel_menu():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")]])

# ---------- Отрисовки ----------
async def render_admin_panel(cb: CallbackQuery, state: FSMContext | None = None):
    if state: await state.clear()
    if str(cb.from_user.id) != ADMIN_ID: return
    try:
        p = await get_pool()
        async with p.acquire() as c:
            total = await c.fetchval("SELECT COUNT(*) FROM users")
            premium_users = await c.fetch("SELECT user_id, premium_until FROM users WHERE premium_until > NOW() AND is_paid = TRUE")
            trial_users = await c.fetchval("SELECT COUNT(*) FROM users WHERE premium_until > NOW() AND is_paid = FALSE")
            channels = await c.fetch("SELECT name FROM channels")
        ch_list = ", ".join([f"@{r['name']}" for r in channels])
        text = (
            f"🛠 <b>Пульт управления</b>\n━━━━━━━━━━━━━━━━━━\n\n"
            f"👥 Юзеров: <b>{total}</b>\n💎 Купили: <b>{len(premium_users)}</b>\n"
            f"🎁 Триал: <b>{trial_users}</b>\n💰 Доход: <b>{len(premium_users) * 250} ⭐</b>\n"
            f"🛠 Тех. работы: <b>{'🔴 ВКЛ' if IS_MAINTENANCE else '🟢 ВЫКЛ'}</b>\n\n"
            f"📡 <b>Каналы:</b>\n<code>{ch_list}</code>"
        )
        await safe_edit(cb.message, text, get_admin_menu())
    except Exception:
        await safe_edit(cb.message, "⚠️ Ошибка БД.", get_admin_menu())

async def render_job(cb: CallbackQuery, index: int):
    if not await check_sub(cb.from_user.id):
        await safe_edit(cb.message, "⚠️ <b>Подпишись на канал!</b>", get_sub_menu())
        return
    jobs = await fetch_feed()
    is_prem = await has_premium(cb.from_user.id)
    user_filter = None
    if is_prem:
        user_filter = await get_user_filter(cb.from_user.id)
        if user_filter:
            jobs = [j for j in jobs if user_filter.lower() in j['text'].lower()]

    limit = 30 if is_prem else 3
    jobs = jobs[:limit]
    
    if not jobs:
        text = "😴 Пока новых нет." if not user_filter else f"😴 По фильтру '{escape(user_filter)}' пусто."
        await safe_edit(cb.message, text, await build_main_menu(cb.from_user.id, str(cb.from_user.id) == ADMIN_ID))
        return

    index = max(0, min(index, len(jobs) - 1))
    job = jobs[index]
    raw_text = re.sub(r'\n{3,}', '\n\n', job['text'].strip())
    if len(raw_text) > 600: raw_text = raw_text[:600] + "..."
    text = f"<b>💼 Вакансия {index+1}/{len(jobs)}</b>\n<i>Источник: @{job['channel']}</i>\n━━━━━━━━━━━━━━━━━━\n\n{escape(raw_text)}"
    
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
    
    if is_prem:
        f_text = f"⚙️ Фильтр: {escape(user_filter[:15])}" if user_filter else "⚙️ Фильтры"
        kb.append([InlineKeyboardButton(text=f_text, callback_data="user_filters")])
    else:
        kb.append([InlineKeyboardButton(text="💎 Купить Премиум", callback_data="buy_premium")])
    kb.append([InlineKeyboardButton(text="🏠 В меню", callback_data="main_menu")])
    await safe_edit(cb.message, text, InlineKeyboardMarkup(inline_keyboard=kb))

# ---------- Инлайн ----------
@dp.inline_query()
async def inline_search(query: InlineQuery):
    try:
        jobs = await fetch_feed()
        results = []
        search_q = query.query.lower()
        is_prem = await has_premium(query.from_user.id)
        limit = 15 if is_prem else 3
        
        for i, j in enumerate(jobs[:limit]):
            if search_q and search_q not in j['text'].lower(): continue
            results.append(InlineQueryResultArticle(
                id=str(i), title=f"Вакансия из @{j['channel']}", description=escape(j['text'][:100]),
                input_message_content=InputTextMessageContent(message_text=f"💼 <b>Вакансия:</b>\n\n{escape(j['text'][:1000])}", parse_mode=ParseMode.HTML)
            ))
        await query.answer(results, cache_time=60, is_personal=True)
    except TelegramBadRequest: pass

# ---------- Хендлеры ----------
@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    is_admin = str(uid) == ADMIN_ID
    user_name = message.from_user.first_name or "User"
    
    if not await check_sub(uid):
        await message.answer("👋 <b>Привет!</b>\n\nПодпишись на канал.", reply_markup=get_sub_menu())
        return

    until = await get_premium_until(uid)
    if until is None:
        await add_premium_days(uid, 3)
        if not is_admin:
            try: await bot.send_message(ADMIN_ID, f"👤 <b>Новый юзер</b>\n{escape(user_name)}\nID: <code>{uid}</code>\n🎁 Триал 3 дня.")
            except: pass
            
    parts = message.text.split()
    if len(parts) > 1 and parts[1].startswith("ref_"):
        raw = parts[1][4:]
        if raw.isdigit() and await apply_referral(int(raw), uid):
            try: await bot.send_message(int(raw), f"🎁 По твоей ссылке перешёл {escape(user_name)}! +7 дней.")
            except: pass
            
    await message.answer(f"👋 <b>Привет, {escape(user_name)}!</b>\n\n🚀 <b>Gig Sniper</b> — радар IT-заказов.\n\n🎁 Тебе активирован <b>3-дневный Премиум</b>.", reply_markup=await build_main_menu(uid, is_admin))

@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(cb: CallbackQuery):
    if await check_sub(cb.from_user.id):
        await cb.answer()
        await safe_edit(cb.message, "✅ Подписка подтверждена! Жми /start", await build_main_menu(cb.from_user.id, str(cb.from_user.id) == ADMIN_ID))
    else: await cb.answer("❌ Ты не подписан!", show_alert=True)

@dp.callback_query(F.data == "main_menu")
async def back_to_menu(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await safe_edit(cb.message, "⚡️ <b>Главное меню:</b>", await build_main_menu(cb.from_user.id, str(cb.from_user.id) == ADMIN_ID))

@dp.callback_query(F.data.startswith("show_jobs:"))
async def show_jobs(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    try: index = int(cb.data.split(":")[1])
    except: index = 0
    if index == 0 and not cb.message.text.startswith("💼"): await safe_edit(cb.message, "🔄 <i>Сканирую...</i>")
    await render_job(cb, index)

@dp.callback_query(F.data == "ignore")
async def ignore_btn(cb: CallbackQuery): await cb.answer()

# ---------- Фильтры ----------
@dp.callback_query(F.data == "user_filters")
async def show_filters(cb: CallbackQuery, state: FSMContext):
    if not await has_premium(cb.from_user.id): return await cb.answer("Только с Премиумом!", show_alert=True)
    await cb.answer()
    current_filter = await get_user_filter(cb.from_user.id)
    await state.set_state(UserStates.waiting_for_filter)
    await safe_edit(cb.message, f"⚙️ <b>Фильтр</b>\nТекущий: <b>{escape(current_filter) if current_filter else 'Нет'}</b>\n\nОтправь слово (например <code>python</code>) или <code>-</code> для удаления.", get_user_cancel_menu())

@dp.message(UserStates.waiting_for_filter)
async def set_filter(message: Message, state: FSMContext):
    if not await has_premium(message.from_user.id):
        await state.clear()
        return await message.answer("Премиум закончился.", reply_markup=await build_main_menu(message.from_user.id, str(message.from_user.id) == ADMIN_ID))
    if not message.text or message.text.startswith('/'):
        await state.clear()
        return await message.answer("❌ Отменено.", reply_markup=get_user_cancel_menu())
    
    word = message.text.strip()[:50]
    if word == "-" or word == "": word = None
    try:
        p = await get_pool()
        async with p.acquire() as c: await c.execute("UPDATE users SET filter_word = $1 WHERE user_id = $2", word, message.from_user.id)
    except: pass
    await state.clear()
    if word: await message.answer(f"✅ Фильтр: <b>{escape(word)}</b>", reply_markup=get_user_cancel_menu())
    else: await message.answer("✅ Фильтр сброшен.", reply_markup=get_user_cancel_menu())

@dp.callback_query(F.data == "reset_filter_menu")
async def reset_filter_from_menu(cb: CallbackQuery, state: FSMContext):
    await cb.answer("✅ Сброшено.", show_alert=True)
    await state.clear()
    try:
        p = await get_pool()
        async with p.acquire() as c: await c.execute("UPDATE users SET filter_word = NULL WHERE user_id = $1", cb.from_user.id)
    except: pass
    await safe_edit(cb.message, "⚡️ <b>Меню:</b>", await build_main_menu(cb.from_user.id, str(cb.from_user.id) == ADMIN_ID))

# ---------- AI Адаптер ----------
@dp.callback_query(F.data == "cv_adapter")
async def start_cv_adapter(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if not await has_premium(cb.from_user.id):
        return await cb.answer("CV Адаптер доступен с Премиумом!", show_alert=True)
    await state.set_state(CVStates.waiting_for_cv)
    await safe_edit(cb.message, "🤖 <b>CV Адаптер</b>\n\nШаг 1. Отправь текст своего резюме (навыки, опыт).", get_user_cancel_menu())

@dp.message(CVStates.waiting_for_cv)
async def get_cv_text(message: Message, state: FSMContext):
    if not message.text or message.text.startswith('/'):
        await state.clear()
        return await message.answer("❌ Отменено.", reply_markup=await build_main_menu(message.from_user.id, str(message.from_user.id) == ADMIN_ID))
    await state.update_data(cv_text=message.text)
    await state.set_state(CVStates.waiting_for_vacancy)
    await message.answer("Шаг 2. Теперь отправь текст вакансии, под которую нужно адаптировать резюме.")

@dp.message(CVStates.waiting_for_vacancy)
async def get_vacancy_text(message: Message, state: FSMContext):
    if not message.text or message.text.startswith('/'):
        await state.clear()
        return await message.answer("❌ Отменено.", reply_markup=await build_main_menu(message.from_user.id, str(message.from_user.id) == ADMIN_ID))
    
    data = await state.get_data()
    status_msg = await message.answer("⏳ <i>Нейросеть адаптирует...</i>")
    adapted = await generate_adapted_cv(data.get('cv_text'), message.text)
    await state.clear()
    await status_msg.delete()
    
    safe_adapted = escape(adapted)
    
    await message.answer(
        f"✅ <b>Готовое резюме:</b>\n\n<code>{safe_adapted}</code>", 
        reply_markup=await build_main_menu(message.from_user.id, str(message.from_user.id) == ADMIN_ID)
    )

# ---------- B2B и Оплата ----------
@dp.callback_query(F.data == "post_vacancy")
async def post_vacancy_start(cb: CallbackQuery):
    await cb.answer()
    try:
        p = await get_pool()
        async with p.acquire() as c: has = await c.fetchval("SELECT user_id FROM pending_vacancies WHERE user_id = $1", cb.from_user.id)
        if has: return await cb.answer("У тебя уже есть оплаченная вакансия!", show_alert=True)
    except: pass
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Оплатить 1500 ⭐", callback_data="pay_for_vacancy")], [InlineKeyboardButton(text="🔙 Отмена", callback_data="main_menu")]])
    await safe_edit(cb.message, "📝 <b>Размещение вакансии</b>\n\nОпубликуем в канале и разошлем Премиумам. Стоимость: 1500 ⭐", kb)

@dp.callback_query(F.data == "pay_for_vacancy")
async def pay_for_vacancy(cb: CallbackQuery):
    await cb.answer()
    await bot.send_invoice(chat_id=cb.from_user.id, title="Размещение вакансии", description="Публикация и рассылка", payload="vacancy_post", provider_token="", currency="XTR", prices=[LabeledPrice(label="Размещение", amount=1500)])

@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery): await q.answer(ok=True)

@dp.message(F.successful_payment)
async def on_paid(message: Message, state: FSMContext):
    payload = message.successful_payment.invoice_payload
    amount = message.successful_payment.total_amount
    
    if payload == "premium_30" and amount == 250:
        try:
            await add_premium_days(message.from_user.id, 30)
            p = await get_pool()
            async with p.acquire() as c: await c.execute("UPDATE users SET is_paid = TRUE WHERE user_id = $1", message.from_user.id)
            name = escape(message.from_user.first_name or "User")
            await bot.send_message(ADMIN_ID, f"💰 <b>Премиум!</b>\n{name}\nID: <code>{message.from_user.id}</code>")
            await message.answer("✅ <b>Оплата прошла!</b>", reply_markup=await build_main_menu(message.from_user.id, str(message.from_user.id) == ADMIN_ID))
        except: pass

    elif payload == "vacancy_post" and amount == 1500:
        try:
            p = await get_pool()
            async with p.acquire() as c: await c.execute("INSERT INTO pending_vacancies (user_id) VALUES ($1) ON CONFLICT DO NOTHING", message.from_user.id)
            await state.set_state(UserStates.waiting_for_vacancy)
            await message.answer("✅ <b>Оплата получена!</b>\n\nОтправь текст вакансии (до 4000 символов).", reply_markup=get_user_cancel_menu())
        except: pass

@dp.message(UserStates.waiting_for_vacancy)
async def receive_vacancy(message: Message, state: FSMContext):
    if not message.text or message.text.startswith('/') or len(message.text) < 50:
        return await message.answer("❌ Отправь текст (минимум 50 символов) или /cancel.")
    try:
        p = await get_pool()
        async with p.acquire() as c: await c.execute("UPDATE pending_vacancies SET text = $1 WHERE user_id = $2", message.text.strip(), message.from_user.id)
    except: return await message.answer("⚠️ Ошибка БД.")
    
    mod_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"approve_vac_{message.from_user.id}"), InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_vac_{message.from_user.id}")]])
    await bot.send_message(ADMIN_ID, f"📝 <b>Вакансия на модерацию</b>\nОт: <code>{message.from_user.id}</code>\n\n{escape(message.text)}", reply_markup=mod_kb)
    await state.clear()
    await message.answer("⏳ Отправлено на модерацию.", reply_markup=await build_main_menu(message.from_user.id, str(message.from_user.id) == ADMIN_ID))

@dp.callback_query(F.data.startswith("approve_vac_"))
async def approve_vacancy(cb: CallbackQuery):
    if str(cb.from_user.id) != ADMIN_ID: return
    try: uid = int(cb.data.split("_")[2])
    except: return
    await cb.answer("Публикую...")
    try:
        p = await get_pool()
        async with p.acquire() as c: row = await c.fetchrow("SELECT text FROM pending_vacancies WHERE user_id = $1", uid)
        if not row or not row['text']: return await safe_edit(cb.message, "❌ Текст не найден.")
        post = await bot.send_message(CHANNEL_ID, f"📢 <b>Новая вакансия!</b>\n\n{escape(row['text'])}")
        async with p.acquire() as c:
            users = await c.fetch("SELECT user_id FROM users WHERE premium_until > NOW()")
            await c.execute("DELETE FROM pending_vacancies WHERE user_id = $1", uid)
        for u in users:
            if str(u['user_id']) == ADMIN_ID: continue
            try: await bot.send_message(u['user_id'], f"🚨 <b>СВЕЖАЯ ВАКАНСИЯ!</b>\n\n{escape(row['text'])}", disable_web_page_preview=True)
            except: pass
        await safe_edit(cb.message, "✅ Опубликовано!")
        try: await bot.send_message(uid, "✅ Твоя вакансия опубликована!")
        except: pass
    except Exception as e: await safe_edit(cb.message, f"❌ Ошибка: {e}")

@dp.callback_query(F.data.startswith("reject_vac_"))
async def reject_vacancy(cb: CallbackQuery):
    if str(cb.from_user.id) != ADMIN_ID: return
    try: uid = int(cb.data.split("_")[2])
    except: return
    await cb.answer("Отклонено.")
    try:
        p = await get_pool()
        async with p.acquire() as c: await c.execute("DELETE FROM pending_vacancies WHERE user_id = $1", uid)
    except: pass
    await safe_edit(cb.message, "❌ Отклонено.")
    try: await bot.send_message(uid, "❌ Вакансия отклонена.")
    except: pass

# ---------- Остальное меню ----------
@dp.callback_query(F.data == "invite")
async def invite(cb: CallbackQuery):
    await cb.answer()
    me = await bot.get_me()
    await safe_edit(cb.message, f"🎁 <b>Рефералка</b>\nТвоя ссылка:\n<code>https://t.me/{me.username}?start=ref_{cb.from_user.id}</code>\n\n+7 дней за друга.", await build_main_menu(cb.from_user.id, str(cb.from_user.id) == ADMIN_ID))

@dp.callback_query(F.data == "my_status")
async def my_status(cb: CallbackQuery):
    await cb.answer()
    uid = str(cb.from_user.id)
    if uid == ADMIN_ID: text = "👑 <b>Статус:</b> Админ"
    else:
        until = await get_premium_until(cb.from_user.id)
        text = f"💎 <b>Статус:</b> Премиум ({(int((until.timestamp() - time.time()) / 86400))} дн.)" if (until and until.timestamp() > time.time()) else "👤 <b>Статус:</b> Бесплатный"
    await safe_edit(cb.message, text, await build_main_menu(cb.from_user.id, uid == ADMIN_ID))

@dp.callback_query(F.data == "buy_premium")
async def buy_premium(cb: CallbackQuery):
    await cb.answer()
    await bot.send_invoice(chat_id=cb.from_user.id, title="Премиум (30 дней)", description="Доступ к вакансиям и фильтрам", payload="premium_30", provider_token="", currency="XTR", prices=[LabeledPrice(label="Доступ", amount=250)])

# ---------- Админка ----------
@dp.callback_query(F.data == "admin_panel")
async def admin_panel(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await render_admin_panel(cb, state)

@dp.callback_query(F.data == "admin_toggle_maint")
async def toggle_maint(cb: CallbackQuery, state: FSMContext):
    global IS_MAINTENANCE
    if str(cb.from_user.id) != ADMIN_ID: return
    IS_MAINTENANCE = not IS_MAINTENANCE
    try:
        p = await get_pool()
        async with p.acquire() as c: await c.execute("UPDATE settings SET value = $1 WHERE key = 'maintenance'", '1' if IS_MAINTENANCE else '0')
    except: pass
    await cb.answer(f"Тех. работы {'ВКЛ' if IS_MAINTENANCE else 'ВЫКЛ'}", show_alert=True)
    await render_admin_panel(cb, state)

@dp.callback_query(F.data == "admin_give")
async def admin_give_start(cb: CallbackQuery, state: FSMContext):
    if str(cb.from_user.id) != ADMIN_ID: return
    await cb.answer()
    await state.set_state(AdminStates.give_id)
    await safe_edit(cb.message, "💎 <b>Выдача</b>\nОтправь ID:", get_cancel_menu())

@dp.message(AdminStates.give_id)
async def admin_give_days_input(message: Message, state: FSMContext):
    if str(message.from_user.id) != ADMIN_ID or not message.text or not message.text.isdigit(): return
    await state.update_data(target_uid=int(message.text))
    await state.set_state(AdminStates.give_days)
    await message.answer("Сколько дней?", reply_markup=get_cancel_menu())

@dp.message(AdminStates.give_days)
async def admin_give_days_finish(message: Message, state: FSMContext):
    if str(message.from_user.id) != ADMIN_ID or not message.text or not message.text.isdigit(): return
    days = int(message.text)
    if days <= 0: return await message.answer("Больше 0!")
    data = await state.get_data()
    try:
        await add_premium_days(data.get('target_uid'), days)
        await message.answer(f"✅ Выдано {days} дней.", reply_markup=get_admin_menu())
    except: await message.answer("❌ Ошибка.")
    await state.clear()

@dp.callback_query(F.data == "admin_clear")
async def admin_clear_fakes(cb: CallbackQuery, state: FSMContext):
    if str(cb.from_user.id) != ADMIN_ID: return
    await cb.answer("🧹 Сброшено!", show_alert=True)
    try:
        p = await get_pool()
        async with p.acquire() as c: await c.execute("UPDATE users SET premium_until = NOW(), filter_word = NULL WHERE is_paid = FALSE")
    except: pass
    await render_admin_panel(cb, state)

@dp.callback_query(F.data == "admin_add_ch")
async def admin_add_ch_start(cb: CallbackQuery, state: FSMContext):
    if str(cb.from_user.id) != ADMIN_ID: return
    await cb.answer()
    await state.set_state(AdminStates.add_channel)
    await safe_edit(cb.message, "➕ Отправь юзернейм канала:", get_cancel_menu())

@dp.message(AdminStates.add_channel)
async def admin_add_ch_finish(message: Message, state: FSMContext):
    if str(message.from_user.id) != ADMIN_ID or not message.text or message.text.startswith('/'): return
    name = message.text.replace("https://t.me/", "").replace("@", "").strip()[:100]
    if not name: return
    try:
        p = await get_pool()
        async with p.acquire() as c: await c.execute("INSERT INTO channels (name) VALUES ($1)", name)
        _feed["data"] = None
        await state.clear()
        await message.answer(f"✅ Добавлен @{name}.", reply_markup=get_admin_menu())
    except:
        await message.answer("⚠️ Уже есть или ошибка.", reply_markup=get_admin_menu())

@dp.callback_query(F.data == "admin_del_ch")
async def admin_del_ch_start(cb: CallbackQuery, state: FSMContext):
    if str(cb.from_user.id) != ADMIN_ID: return
    await cb.answer()
    await state.set_state(AdminStates.del_channel)
    await safe_edit(cb.message, "🗑 Отправь юзернейм канала:", get_cancel_menu())

@dp.message(AdminStates.del_channel)
async def admin_del_ch_finish(message: Message, state: FSMContext):
    if str(message.from_user.id) != ADMIN_ID or not message.text or message.text.startswith('/'): return
    name = message.text.replace("https://t.me/", "").replace("@", "").strip()
    try:
        p = await get_pool()
        async with p.acquire() as c: res = await c.execute("DELETE FROM channels WHERE name = $1", name)
        if res == "DELETE 1":
            _feed["data"] = None
            await state.clear()
            await message.answer(f"🗑 Удален @{name}.", reply_markup=get_admin_menu())
        else: await message.answer("❌ Не найден.", reply_markup=get_admin_menu())
    except: pass

@dp.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext):
    curr = await state.get_state()
    if not curr: return
    await state.clear()
    if curr == UserStates.waiting_for_vacancy.state:
        try:
            p = await get_pool()
            async with p.acquire() as c: await c.execute("DELETE FROM pending_vacancies WHERE user_id = $1", message.from_user.id)
        except: pass
        return await message.answer("❌ Отменено.", reply_markup=await build_main_menu(message.from_user.id, str(message.from_user.id) == ADMIN_ID))
    await message.answer("❌ Отменено.", reply_markup=get_admin_menu() if "Admin" in curr else await build_main_menu(message.from_user.id, str(message.from_user.id) == ADMIN_ID))

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
