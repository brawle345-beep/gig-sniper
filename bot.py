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
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

ADMIN_ID = "7183342708"
CHANNEL_ID = "@gigsnipervakansii"
CHANNEL_URL = "https://t.me/gigsnipervakansii"

IS_MAINTENANCE = False
FREE_DELAY = 900  # 15 минут

class AdminStates(StatesGroup):
    give_id = State()
    give_days = State()
    add_channel = State()
    del_channel = State()

class UserStates(StatesGroup):
    waiting_for_filter = State()
    waiting_for_vacancy = State()

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
                filter_word VARCHAR(255) DEFAULT NULL,
                is_active BOOLEAN DEFAULT TRUE
            );
            ALTER TABLE users ADD COLUMN IF NOT EXISTS is_paid BOOLEAN DEFAULT FALSE;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS filter_word VARCHAR(255) DEFAULT NULL;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;
            
            CREATE TABLE IF NOT EXISTS referrals (
                referrer BIGINT NOT NULL,
                referred BIGINT NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                PRIMARY KEY (referrer, referred),
                UNIQUE (referred)
            );
            
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

            -- ТАБЛИЦА ДЛЯ ОТЛОЖЕННЫХ ПУШЕЙ
            CREATE TABLE IF NOT EXISTS delayed_pushes (
                id SERIAL PRIMARY KEY,
                text TEXT NOT NULL,
                send_at TIMESTAMP WITH TIME ZONE NOT NULL
            );
        """)
        
        count = await c.fetchval("SELECT COUNT(*) FROM channels")
        if count == 0:
            await c.execute("""
                INSERT INTO channels (name) VALUES 
                ('it_vakansii_jobs'), ('remote_it_jobs'), ('python_jobs'), ('freelance_jobs')
            """)
            
        maint_val = await c.fetchval("SELECT value FROM settings WHERE key = 'maintenance'")
        IS_MAINTENANCE = (maint_val == '1') if maint_val else False
        if maint_val is None:
            await c.execute("INSERT INTO settings (key, value) VALUES ('maintenance', '0')")

async def get_channels() -> list:
    try:
        p = await get_pool()
        async with p.acquire() as c:
            rows = await c.fetch("SELECT name FROM channels")
            return [r['name'] for r in rows]
    except: return []

async def get_premium_until(user_id: int):
    try:
        p = await get_pool()
        async with p.acquire() as c:
            row = await c.fetchrow("SELECT premium_until FROM users WHERE user_id = $1", user_id)
        return row["premium_until"] if row else None
    except: return None

async def get_user_filter(user_id: int):
    try:
        p = await get_pool()
        async with p.acquire() as c:
            row = await c.fetchrow("SELECT filter_word FROM users WHERE user_id = $1", user_id)
        return row["filter_word"] if row else None
    except: return None

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
    except: return False

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
                
                if today_count >= 5: return False

                claimed = await c.fetchval("""
                    INSERT INTO referrals (referrer, referred) VALUES ($1, $2)
                    ON CONFLICT DO NOTHING RETURNING referrer
                """, ref_id, new_uid)
                if claimed is None: return False
                
                await c.execute("""
                    INSERT INTO users (user_id, premium_until)
                    VALUES ($1, NOW() + interval '10 days')
                    ON CONFLICT (user_id) DO UPDATE
                    SET premium_until = GREATEST(COALESCE(users.premium_until, NOW()), NOW())
                                        + interval '10 days'
                """, ref_id)
                return True
    except: return False

REQUIRED_WORDS = ["python", "react", "frontend", "backend", "разработчик", "верстальщик", "удаленка", "вакансия", "ищу", "требуется", "looking for", "hiring", "middle", "senior", "junior", "golang", "java", "c++", "devops"]
STOP_WORDS = ["курс", "промокод", "скидка", "подпишись", "реклама", "инфобизнес", "наставничество", "база", "слив", "обучение", "заработок", "доход", "крипт", "бесплатно", "лайфхак", "марафон", "вебинар"]

_feed = {"data": None, "ts": 0.0}
_feed_lock = asyncio.Lock()

def is_valid_job(text: str) -> bool:
    text_lower = text.lower()
    if len(text_lower) < 150 or len(text_lower) > 4000: return False
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
                except: pass
        jobs = jobs[-50:] 
        jobs.reverse()
        _feed["data"] = jobs
        _feed["ts"] = now
        return jobs

# ---------- БЕЗОПАСНАЯ РАССЫЛКА (FloodWait + Мертвые души) ----------
async def safe_broadcast(user_ids: list, text: str, markup=None):
    success = 0
    p = await get_pool()
    for uid in user_ids:
        if str(uid) == ADMIN_ID: continue
        try:
            await bot.send_message(uid, text, reply_markup=markup, disable_web_page_preview=True)
            success += 1
            await asyncio.sleep(0.05)
        except TelegramRetryAfter as e:
            log.warning(f"FloodWait! Sleep {e.retry_after}s")
            await asyncio.sleep(e.retry_after)
            try: 
                await bot.send_message(uid, text, reply_markup=markup, disable_web_page_preview=True)
                success += 1
            except: pass
        except TelegramForbiddenError:
            # Юзер заблокировал бота — помечаем мертвым
            try:
                async with p.acquire() as c:
                    await c.execute("UPDATE users SET is_active = FALSE WHERE user_id = $1", uid)
            except: pass
        except Exception as e:
            log.error(f"Broadcast error to {uid}: {e}")
    return success

async def delayed_push_worker():
    """Воркер для выдачи FOMO-пушей (защита от дублей)"""
    while True:
        try:
            p = await get_pool()
            async with p.acquire() as c:
                # Забираем и СРАЗУ удаляем, чтобы не задублировать при рестарте
                rows = await c.fetch("DELETE FROM delayed_pushes WHERE send_at <= NOW() RETURNING text")
            
            if rows:
                async with p.acquire() as c:
                    free_users = await c.fetch("SELECT user_id FROM users WHERE (premium_until IS NULL OR premium_until <= NOW()) AND is_active = TRUE")
                
                for row in rows:
                    raw_text = row['text']
                    safe_text = escape(raw_text[:300] + "...")
                    text = (
                        f"⏳ <b>Остывающая вакансия</b>\n\n{safe_text}\n\n"
                        "<i>Она уже 15 минут как ушла Премиум-юзерам. Скорее всего, отклики уже летят. Купи Премиум, чтобы получать вакансии первым.</i>"
                    )
                    me = await bot.get_me()
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="💎 Купить Премиум и видеть первым", callback_data="buy_premium")],
                        [InlineKeyboardButton(text="🔥 Открыть в боте", url=f"https://t.me/{me.username}?start=push")]
                    ])
                    
                    await safe_broadcast([u['user_id'] for u in free_users], text, kb)
        except Exception as e:
            log.error(f"Delayed push error: {e}")
        await asyncio.sleep(60)

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
                            
                            prem_users = await c.fetch("SELECT user_id FROM users WHERE premium_until > NOW() AND is_active = TRUE")
                        
                        raw_text = re.sub(r'\n{3,}', '\n\n', latest_job['text'].strip())
                        if len(raw_text) > 3500: raw_text = raw_text[:3500] + "..."
                        safe_text = escape(raw_text)
                        
                        prem_text = f"🚨 <b>СВЕЖАЯ ВАКАНСИЯ (Мгновенно)!</b>\n\n{safe_text}"
                        kb_prem = InlineKeyboardMarkup(inline_keyboard=[[
                            InlineKeyboardButton(text="🔥 Открыть в боте", url=f"https://t.me/{bot_me.username}?start=push")
                        ]])
                        
                        await safe_broadcast([u['user_id'] for u in prem_users], prem_text, kb_prem)
                        
                        # Кладем в БД отложенный пуш для бесплатных
                        async with p.acquire() as c:
                            await c.execute(
                                "INSERT INTO delayed_pushes (text, send_at) VALUES ($1, NOW() + interval '15 minutes')",
                                raw_text
                            )
                            
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

@dp.errors()
async def error_handler(event: ErrorEvent):
    log.error(f"Unhandled error: {event.exception}", exc_info=event.exception)
    try:
        if event.update.message:
            await event.update.message.answer("⚠️ Ошибка. Админ уведомлен.")
    except: pass

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
                await event.answer("🛠 <b>Бот на обслуживании.</b>\nЗайди позже!")
            elif isinstance(event, CallbackQuery):
                await event.answer("🛠 Тех. работы.", show_alert=True)
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
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): pass

async def build_main_menu(user_id: int, is_admin=False):
    kb = [
        [InlineKeyboardButton(text="🔥 Смотреть вакансии", callback_data="show_jobs:0")],
        [InlineKeyboardButton(text="💎 Премиум-доступ", callback_data="buy_premium"), InlineKeyboardButton(text="👤 Профиль", callback_data="my_status")],
        [InlineKeyboardButton(text="📝 Разместить вакансию (B2B)", callback_data="post_vacancy")],
        [InlineKeyboardButton(text="🎁 Пригласить друга (+10 дней)", callback_data="invite")],
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
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад к вакансиям", callback_data="show_jobs:0")]])

async def render_admin_panel(cb: CallbackQuery, state: FSMContext | None = None):
    if state: await state.clear()
    if str(cb.from_user.id) != ADMIN_ID: return
    try:
        p = await get_pool()
        async with p.acquire() as c:
            total = await c.fetchval("SELECT COUNT(*) FROM users")
            premium_users = await c.fetchval("SELECT COUNT(*) FROM users WHERE premium_until > NOW() AND is_paid = TRUE")
            trial_users = await c.fetchval("SELECT COUNT(*) FROM users WHERE premium_until > NOW() AND is_paid = FALSE")
            channels = await c.fetch("SELECT name FROM channels")
            
        ch_list = ", ".join([f"@{r['name']}" for r in channels])
        maint_status = "🔴 ВКЛ" if IS_MAINTENANCE else "🟢 ВЫКЛ"
        
        text = (
            f"🛠 <b>Пульт управления</b>\n━━━━━━━━━━━━━━━━━━\n\n"
            f"👥 Юзеров: <b>{total}</b>\n"
            f"💎 Купили: <b>{premium_users}</b>\n"
            f"🎁 Триал: <b>{trial_users}</b>\n"
            f"💰 Доход: <b>{premium_users * 250} ⭐</b>\n"
            f"🛠 Тех. работы: <b>{maint_status}</b>\n\n"
            f"📡 <b>Каналы в парсере:</b>\n<code>{ch_list}</code>"
        )
        await safe_edit(cb.message, text, get_admin_menu())
    except: await safe_edit(cb.message, "⚠️ Ошибка БД.", get_admin_menu())

async def render_job(cb: CallbackQuery, index: int):
    if not await check_sub(cb.from_user.id):
        await safe_edit(cb.message, "⚠️ <b>Для доступа подпишись на канал!</b>", get_sub_menu())
        return

    jobs = await fetch_feed()
    is_prem = await has_premium(cb.from_user.id)
    user_filter = await get_user_filter(cb.from_user.id) if is_prem else None
    if user_filter:
        jobs = [j for j in jobs if user_filter.lower() in j['text'].lower()]

    limit = 30 if is_prem else 3
    jobs = jobs[:limit]
    
    if not jobs:
        await safe_edit(cb.message, "😴 Пока нет вакансий.", await build_main_menu(cb.from_user.id, str(cb.from_user.id) == ADMIN_ID))
        return

    index = max(0, min(index, len(jobs) - 1))
    job = jobs[index]
    
    raw_text = re.sub(r'\n{3,}', '\n\n', job['text'].strip())
    if len(raw_text) > 3500: raw_text = raw_text[:3500].rsplit(' ', 1)[0] + "..."
    
    text = f"<b>💼 Вакансия {index+1} из {len(jobs)}</b>\n<i>Источник: @{job['channel']}</i>\n━━━━━━━━━━━━━━━━━━\n\n{escape(raw_text)}"
    kb = []

    nav_row = []
    if index > 0: nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"show_jobs:{index-1}"))
    else: nav_row.append(InlineKeyboardButton(text="⬅️", callback_data="ignore"))
    nav_row.append(InlineKeyboardButton(text=f"{index+1}/{len(jobs)}", callback_data="ignore"))
    if index < len(jobs) - 1: nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"show_jobs:{index+1}"))
    else: nav_row.append(InlineKeyboardButton(text="➡️", callback_data="ignore"))
    kb.append(nav_row)

    contact_row = []
    if is_prem:
        if job.get('contact_url'): contact_row.append(InlineKeyboardButton(text="💬 Написать", url=job['contact_url']))
        if job.get('post_url'): contact_row.append(InlineKeyboardButton(text="🔗 Оригинал", url=job['post_url']))
        if contact_row: kb.append(contact_row)
        
        short_f = user_filter[:15] + "..." if user_filter and len(user_filter) > 15 else user_filter
        f_text = f"⚙️ Фильтр: {escape(short_f)}" if short_f else "⚙️ Фильтры"
        kb.append([InlineKeyboardButton(text=f_text, callback_data="user_filters")])
    else:
        kb.append([InlineKeyboardButton(text="🔒 Контакты скрыты", callback_data="buy_premium")])
        kb.append([InlineKeyboardButton(text="💎 Купить Премиум (250⭐)", callback_data="buy_premium")])

    me = await bot.get_me()
    share_text = f"Смотри какая вакансия: {job['text'][:50]}..."
    share_url = f"https://t.me/share/url?url=https://t.me/{me.username}?start=ref_{cb.from_user.id}&text={share_text}"
    kb.append([InlineKeyboardButton(text="📤 Поделиться с другом (бонус)", url=share_url)])
    kb.append([InlineKeyboardButton(text="🏠 В меню", callback_data="main_menu")])
    
    await safe_edit(cb.message, text, InlineKeyboardMarkup(inline_keyboard=kb))

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
            raw_text = re.sub(r'\n{3,}', '\n\n', j['text'].strip())[:100] + "..."
            results.append(
                InlineQueryResultArticle(
                    id=str(i), title=f"Вакансия из @{j['channel']}", description=escape(raw_text),
                    input_message_content=InputTextMessageContent(message_text=f"💼 <b>Вакансия:</b>\n\n{escape(j['text'][:3500])}", parse_mode=ParseMode.HTML)
                )
            )
        await query.answer(results, cache_time=60, is_personal=True)
    except TelegramBadRequest: pass

@dp.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None: return
    
    if current_state == UserStates.waiting_for_vacancy.state:
        await message.answer("⚠️ <b>Ты уже оплатил размещение!</b>\n\nОтмена невозможна, так как средства списаны. Пожалуйста, отправь текст вакансии.")
        return
        
    await state.clear()
    
    if current_state in [AdminStates.give_id.state, AdminStates.give_days.state, AdminStates.add_channel.state, AdminStates.del_channel.state]:
        await message.answer("❌ Отменено.", reply_markup=get_admin_menu())
    else:
        await message.answer("❌ Отменено.", reply_markup=await build_main_menu(message.from_user.id, str(message.from_user.id) == ADMIN_ID))

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    is_admin = str(uid) == ADMIN_ID
    user_name = message.from_user.first_name or "User"
    
    if not await check_sub(uid):
        await message.answer("👋 <b>Привет!</b>\n\nПодпишись на канал с вакансиями.", reply_markup=get_sub_menu())
        return

    until = await get_premium_until(uid)
    if until is None:
        await add_premium_days(uid, 3)
        if not is_admin:
            try:
                await bot.send_message(ADMIN_ID, f"👤 <b>Новый юзер</b>\n{escape(user_name)}\nID: <code>{uid}</code>\n🎁 Триал 3 дня.")
            except: pass
            
    parts = message.text.split()
    if len(parts) > 1 and parts[1].startswith("ref_"):
        raw = parts[1][4:]
        if raw.isdigit() and await apply_referral(int(raw), uid):
            try: await bot.send_message(int(raw), f"🎁 По твоей ссылке пришел {escape(user_name)}! +10 дней.")
            except: pass

    try:
        p = await get_pool()
        async with p.acquire() as c:
            has_pending = await c.fetchval("SELECT user_id FROM pending_vacancies WHERE user_id = $1 AND text IS NULL", uid)
        if has_pending:
            await state.set_state(UserStates.waiting_for_vacancy)
            await message.answer("⚠️ <b>У тебя есть оплаченная вакансия!</b>\n\nОтправь текст вакансии сообщением.")
            return
    except: pass
            
    await message.answer(
        f"👋 <b>Привет, {escape(user_name)}!</b>\n\n"
        "🚀 <b>Gig Sniper</b> — радар IT-заказов.\n\n"
        "🎁 Активирован <b>3-дневный Премиум</b>.\n\n"
        "<i>Совет: напиши @этот_бот в любом чате, чтобы искать вакансии.</i>",
        reply_markup=await build_main_menu(uid, is_admin),
    )

@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(cb: CallbackQuery):
    if await check_sub(cb.from_user.id):
        await cb.answer()
        try: await cb.message.delete()
        except: pass
        await cb.message.answer("✅ Подписка подтверждена! Жми /start", reply_markup=await build_main_menu(cb.from_user.id, str(cb.from_user.id) == ADMIN_ID))
    else: await cb.answer("❌ Ты не подписан!", show_alert=True)

@dp.callback_query(F.data == "main_menu")
async def back_to_menu(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await safe_edit(cb.message, "⚡️ <b>Главное меню:</b>", await build_main_menu(cb.from_user.id, str(cb.from_user.id) == ADMIN_ID))

@dp.callback_query(F.data == "reset_filter_menu")
async def reset_filter_from_menu(cb: CallbackQuery, state: FSMContext):
    await cb.answer("✅ Фильтр сброшен.", show_alert=True)
    await state.clear()
    try:
        p = await get_pool()
        async with p.acquire() as c:
            await c.execute("UPDATE users SET filter_word = NULL WHERE user_id = $1", cb.from_user.id)
    except: pass
    await safe_edit(cb.message, "⚡️ <b>Главное меню:</b>\n\n<i>Фильтр сброшен.</i>", await build_main_menu(cb.from_user.id, str(cb.from_user.id) == ADMIN_ID))

@dp.callback_query(F.data.startswith("show_jobs:"))
async def show_jobs(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    try: index = int(cb.data.split(":")[1])
    except: index = 0
        
    if index == 0 and not cb.message.text.startswith("💼"):
        await safe_edit(cb.message, "🔄 <i>Сканирую...</i>")
    await render_job(cb, index)

@dp.callback_query(F.data == "ignore")
async def ignore_btn(cb: CallbackQuery): await cb.answer()

@dp.callback_query(F.data == "user_filters")
async def show_filters(cb: CallbackQuery, state: FSMContext):
    if not await has_premium(cb.from_user.id):
        await cb.answer("Фильтры доступны только с Премиумом!", show_alert=True)
        return
    await cb.answer()
    current_filter = await get_user_filter(cb.from_user.id)
    f_text = f"<b>{escape(current_filter)}</b>" if current_filter else "<i>Не задан</i>"
    await state.set_state(UserStates.waiting_for_filter)
    await safe_edit(cb.message,
        f"⚙️ <b>Настройка фильтра</b>\n\nТекущий: {f_text}\n\nОтправь слово (например, <code>python</code>).\nДля удаления отправь <code>-</code>.",
        get_user_cancel_menu()
    )

@dp.message(UserStates.waiting_for_filter)
async def set_filter(message: Message, state: FSMContext):
    if not await has_premium(message.from_user.id):
        await state.clear()
        await message.answer("Премиум закончился.", reply_markup=await build_main_menu(message.from_user.id, str(message.from_user.id) == ADMIN_ID))
        return
    if not message.text or message.text.startswith('/'):
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=get_user_cancel_menu())
        return
    word = message.text.strip()[:50]
    if word == "-" or word == "": word = None
    try:
        p = await get_pool()
        async with p.acquire() as c:
            await c.execute("UPDATE users SET filter_word = $1 WHERE user_id = $2", word, message.from_user.id)
    except: pass
    await state.clear()
    if word: await message.answer(f"✅ Фильтр установлен: <b>{escape(word)}</b>", reply_markup=get_user_cancel_menu())
    else: await message.answer("✅ Фильтр сброшен.", reply_markup=get_user_cancel_menu())

# ---------- B2B: Публикация вакансии ----------
@dp.callback_query(F.data == "post_vacancy")
async def post_vacancy_start(cb: CallbackQuery):
    await cb.answer()
    try:
        p = await get_pool()
        async with p.acquire() as c:
            has_pending = await c.fetchval("SELECT user_id FROM pending_vacancies WHERE user_id = $1", cb.from_user.id)
        if has_pending:
            await cb.answer("⚠️ У тебя уже есть оплаченная вакансия! Отправь её текст.", show_alert=True)
            return
    except: pass
    
    text = (
        "📝 <b>Размещение вакансии</b>\n\n"
        "Твоя вакансия уйдет в канал и разослана всем Премиум-юзерам (живая база IT-спецов).\n\n"
        "<b>Правила:</b>\n"
        "1. Стек, задачи, условия, оплата.\n"
        "2. Обязателен контакт (@username).\n"
        "3. Модерация до 10 минут.\n\n"
        "Стоимость: 1000 ⭐"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Оплатить 1000 ⭐", callback_data="pay_for_vacancy")],
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="main_menu")]
    ])
    await safe_edit(cb.message, text, kb)

@dp.callback_query(F.data == "pay_for_vacancy")
async def pay_for_vacancy(cb: CallbackQuery):
    await cb.answer()
    # Проверка на дубль перед выставлением счета
    try:
        p = await get_pool()
        async with p.acquire() as c:
            has_pending = await c.fetchval("SELECT user_id FROM pending_vacancies WHERE user_id = $1", cb.from_user.id)
        if has_pending:
            await cb.answer("⚠️ У тебя уже есть оплаченная заявка! Отправь текст вакансии в чат.", show_alert=True)
            return
    except: pass

    await bot.send_invoice(
        chat_id=cb.from_user.id, 
        title="Размещение вакансии", 
        description="Публикация в канале и рассылка Премиум-юзерам.",
        payload="vacancy_post", 
        provider_token="", 
        currency="XTR", 
        prices=[LabeledPrice(label="Размещение", amount=1000)],
    )

@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery): await q.answer(ok=True)

@dp.message(F.successful_payment)
async def on_paid(message: Message, state: FSMContext):
    payload = message.successful_payment.invoice_payload
    amount = message.successful_payment.total_amount
    
    if payload == "premium_30" and amount != 250: return
    if payload == "vacancy_post" and amount != 1000: return
    
    if payload == "premium_30":
        try:
            await add_premium_days(message.from_user.id, 30)
            p = await get_pool()
            async with p.acquire() as c: 
                await c.execute("UPDATE users SET is_paid = TRUE WHERE user_id = $1", message.from_user.id)
            name = escape(message.from_user.first_name or "User")
            await bot.send_message(ADMIN_ID, f"💰 <b>Премиум!</b>\n👤 {name}\n🆔 <code>{message.from_user.id}</code>\n💎 30 дней.")
            await message.answer("✅ <b>Оплата прошла!</b> Доступ на 30 дней открыт.", reply_markup=await build_main_menu(message.from_user.id, str(message.from_user.id) == ADMIN_ID))
        except Exception as e:
            await bot.send_message(ADMIN_ID, f"⚠️ <b>ОШИБКА!</b>\nID: <code>{message.from_user.id}</code>\nВыдай вручную: <code>/give {message.from_user.id} 30</code>")

    elif payload == "vacancy_post":
        try:
            p = await get_pool()
            async with p.acquire() as c:
                await c.execute("INSERT INTO pending_vacancies (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING", message.from_user.id)
            await state.set_state(UserStates.waiting_for_vacancy)
            await message.answer("✅ <b>Оплата получена!</b>\n\nОтправь текст вакансии (до 4000 символов). Не забудь @username.", reply_markup=get_user_cancel_menu())
            name = escape(message.from_user.first_name or "User")
            await bot.send_message(ADMIN_ID, f"💼 <b>Оплата за вакансию!</b>\n👤 {name}\nID: <code>{message.from_user.id}</code>\nЖдем текст...")
        except Exception as e:
            await bot.send_message(ADMIN_ID, f"⚠️ Ошибка БД (вакансия): {e}")

@dp.message(UserStates.waiting_for_vacancy)
async def receive_vacancy(message: Message, state: FSMContext):
    if not message.text or message.text.startswith('/'):
        await message.answer("❌ Отмена невозможна, ты оплатил размещение. Отправь текст вакансии.")
        return

    raw_text = message.text.strip()
    if len(raw_text) < 50:
        await message.answer("❌ Слишком коротко. Вакансия должна содержать детали и контакт. Отправь еще раз:")
        return

    try:
        p = await get_pool()
        async with p.acquire() as c:
            await c.execute("UPDATE pending_vacancies SET text = $1 WHERE user_id = $2", raw_text, message.from_user.id)
    except Exception as e:
        await message.answer("⚠️ Ошибка БД. Попробуй позже.")
        return

    mod_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"approve_vac_{message.from_user.id}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_vac_{message.from_user.id}")]
    ])
    
    await bot.send_message(
        ADMIN_ID,
        f"📝 <b>Вакансия на модерацию</b>\n👤 От: {escape(message.from_user.first_name or 'User')} (ID: <code>{message.from_user.id}</code>)\n━━━━━━━━━━━━━━━━━━\n\n{escape(raw_text[:3500])}\n\n━━━━━━━━━━━━━━━━━━",
        reply_markup=mod_kb
    )
    await state.clear()
    await message.answer("⏳ <b>Отправлено на модерацию.</b>\n\nПоявится в канале и у Премиумов после проверки.", reply_markup=await build_main_menu(message.from_user.id, str(message.from_user.id) == ADMIN_ID))

# ---------- Модерация ----------
@dp.callback_query(F.data.startswith("approve_vac_"))
async def approve_vacancy(cb: CallbackQuery):
    if str(cb.from_user.id) != ADMIN_ID: return
    try: uid = int(cb.data.split("_")[2])
    except: return
    await cb.answer("Публикую...")
    
    try:
        p = await get_pool()
        async with p.acquire() as c:
            row = await c.fetchrow("SELECT text FROM pending_vacancies WHERE user_id = $1", uid)
        if not row or not row['text']:
            await safe_edit(cb.message, "❌ Ошибка: текст не найден.")
            return
        text_to_post = escape(row['text'][:3500])
    except Exception as e:
        await safe_edit(cb.message, f"❌ Ошибка БД: {e}")
        return
    
    try:
        post = await bot.send_message(CHANNEL_ID, f"📢 <b>Новая вакансия!</b>\n\n{text_to_post}", disable_web_page_preview=True)
        post_url = post.link
    except Exception as e:
        await safe_edit(cb.message, f"❌ Ошибка постинга: {e}")
        return
        
    try:
        async with p.acquire() as c:
            await c.execute("DELETE FROM pending_vacancies WHERE user_id = $1", uid)
    except: pass

    try:
        async with p.acquire() as c:
            users = await c.fetch("SELECT user_id FROM users WHERE premium_until > NOW() AND is_active = TRUE")
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 Оригинал в канале", url=post_url)]])
        
        await safe_broadcast([u['user_id'] for u in users], f"🚨 <b>СВЕЖАЯ ВАКАНСИЯ!</b>\n\n{text_to_post}", kb)
        
        await safe_edit(cb.message, "✅ <b>Опубликовано и разослано!</b>")
        await bot.send_message(uid, "✅ <b>Твоя вакансия опубликована!</b> Спасибо.")
    except Exception as e:
        log.error(f"Ошибка рассылки: {e}")
        await safe_edit(cb.message, f"⚠️ Пост есть, рассылка упала: {e}")

@dp.callback_query(F.data.startswith("reject_vac_"))
async def reject_vacancy(cb: CallbackQuery):
    if str(cb.from_user.id) != ADMIN_ID: return
    try: uid = int(cb.data.split("_")[2])
    except: return
    await cb.answer("Отклонено.")
    try:
        p = await get_pool()
        async with p.acquire() as c:
            await c.execute("DELETE FROM pending_vacancies WHERE user_id = $1", uid)
    except: pass
    await safe_edit(cb.message, "❌ Вакансия отклонена.")
    try: await bot.send_message(uid, "❌ <b>Твоя вакансия отклонена модератором.</b>\n\nПричина: нарушение правил (нет контакта, короткий текст или реклама).")
    except: pass

# ---------- Остальное ----------
@dp.callback_query(F.data == "invite")
async def invite(cb: CallbackQuery):
    await cb.answer()
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{cb.from_user.id}"
    await safe_edit(cb.message,
        f"🎁 <b>Партнёрская программа</b>\n\nТвоя ссылка:\n<code>{link}</code>\n\n<blockquote>За каждого друга — <b>+10 дней Премиума</b>.</blockquote>\n\n<i>Отправь ссылку тем, кто ищет работу.</i>",
        await build_main_menu(cb.from_user.id, str(cb.from_user.id) == ADMIN_ID))

@dp.callback_query(F.data == "my_status")
async def my_status(cb: CallbackQuery):
    await cb.answer()
    uid = str(cb.from_user.id)
    if uid == ADMIN_ID: text = "👑 <b>Статус:</b> Админ"
    else:
        until = await get_premium_until(cb.from_user.id)
        if until and until.timestamp() > time.time(): text = f"💎 <b>Статус:</b> Премиум (Осталось дней: {int((until.timestamp() - time.time()) / 86400)})"
        else: text = "👤 <b>Статус:</b> Бесплатный\n\n<i>Купи Премиум, чтобы видеть вакансии первым.</i>"
    await safe_edit(cb.message, text, await build_main_menu(cb.from_user.id, uid == ADMIN_ID))

@dp.callback_query(F.data == "buy_premium")
async def buy_premium(cb: CallbackQuery):
    await cb.answer()
    await safe_edit(cb.message, "💎 <b>Счёт отправлен ниже.</b>", await build_main_menu(cb.from_user.id, str(cb.from_user.id) == ADMIN_ID))
    await bot.send_invoice(
        chat_id=cb.from_user.id, title="Премиум (30 дней)", description="Полный доступ к вакансиям, фильтрам и мгновенным пушам.",
        payload="premium_30", provider_token="", currency="XTR", prices=[LabeledPrice(label="Доступ", amount=250)],
    )

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
    val = '1' if IS_MAINTENANCE else '0'
    try:
        p = await get_pool()
        async with p.acquire() as c:
            await c.execute("UPDATE settings SET value = $1 WHERE key = 'maintenance'", val)
    except: pass
    await cb.answer(f"Тех. работы: {'ВКЛ' if IS_MAINTENANCE else 'ВЫКЛ'}", show_alert=True)
    await render_admin_panel(cb, state)

@dp.callback_query(F.data == "admin_give")
async def admin_give_start(cb: CallbackQuery, state: FSMContext):
    if str(cb.from_user.id) != ADMIN_ID: return
    await cb.answer()
    await state.set_state(AdminStates.give_id)
    await safe_edit(cb.message, "💎 <b>Выдача Премиума</b>\n\nОтправь <b>ID пользователя</b>.\n/cancel для отмены.", get_cancel_menu())

@dp.message(AdminStates.give_id)
async def admin_give_days_input(message: Message, state: FSMContext):
    if str(message.from_user.id) != ADMIN_ID: return
    if not message.text or not message.text.isdigit():
        await message.answer("❌ ID должен быть числом. Попробуй еще раз:", reply_markup=get_cancel_menu())
        return
    await state.update_data(target_uid=int(message.text))
    await state.set_state(AdminStates.give_days)
    await message.answer(f"👤 Юзер: <code>{message.text}</code>\n\nСколько дней выдать?", reply_markup=get_cancel_menu())

@dp.message(AdminStates.give_days)
async def admin_give_days_finish(message: Message, state: FSMContext):
    if str(message.from_user.id) != ADMIN_ID: return
    if not message.text or not message.text.isdigit():
        await message.answer("❌ Дни должны быть числом:", reply_markup=get_cancel_menu())
        return
    days = int(message.text)
    if days <= 0:
        await message.answer("❌ Больше нуля:", reply_markup=get_cancel_menu())
        return
    data = await state.get_data()
    target_uid = data.get('target_uid')
    if not target_uid: return
    try:
        await add_premium_days(target_uid, days)
        await message.answer(f"✅ Выдано {days} дней юзеру <code>{target_uid}</code>.", reply_markup=get_admin_menu())
    except Exception as e:
        await message.answer(f"❌ Ошибка БД: {e}", reply_markup=get_admin_menu())
    await state.clear()

@dp.callback_query(F.data == "admin_clear")
async def admin_clear_fakes(cb: CallbackQuery, state: FSMContext):
    if str(cb.from_user.id) != ADMIN_ID: return
    await cb.answer("🧹 Неоплаченные сброшены!", show_alert=True)
    try:
        p = await get_pool()
        async with p.acquire() as c: 
            await c.execute("UPDATE users SET premium_until = NOW(), filter_word = NULL WHERE is_paid = FALSE")
    except: pass
    await render_admin_panel(cb, state)

@dp.callback_query(F.data == "admin_add_ch")
async def admin_add_ch_start(cb: CallbackQuery, state: FSMContext):
    if str(cb.from_user.id) != ADMIN_ID: return
    await cb.answer()
    await state.set_state(AdminStates.add_channel)
    await safe_edit(cb.message, "➕ <b>Добавление канала</b>\n\nОтправь юзернейм канала (@name или ссылку).", get_cancel_menu())

@dp.message(AdminStates.add_channel)
async def admin_add_ch_finish(message: Message, state: FSMContext):
    if str(message.from_user.id) != ADMIN_ID: return
    if not message.text or message.text.startswith('/'):
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=get_admin_menu())
        return
    clean_name = message.text.replace("https://t.me/", "").replace("@", "").strip()[:100]
    if not clean_name: return
    try:
        p = await get_pool()
        async with p.acquire() as c:
            await c.execute("INSERT INTO channels (name) VALUES ($1)", clean_name)
            _feed["data"] = None
            await state.clear()
            await message.answer(f"✅ Канал <b>@{clean_name}</b> добавлен.", reply_markup=get_admin_menu())
    except asyncpg.exceptions.UniqueViolationError:
        await message.answer(f"⚠️ Канал <b>@{clean_name}</b> уже есть.", reply_markup=get_admin_menu())
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}", reply_markup=get_admin_menu())

@dp.callback_query(F.data == "admin_del_ch")
async def admin_del_ch_start(cb: CallbackQuery, state: FSMContext):
    if str(cb.from_user.id) != ADMIN_ID: return
    await cb.answer()
    await state.set_state(AdminStates.del_channel)
    await safe_edit(cb.message, "🗑 <b>Удаление канала</b>\n\nОтправь юзернейм канала, который убрать.", get_cancel_menu())

@dp.message(AdminStates.del_channel)
async def admin_del_ch_finish(message: Message, state: FSMContext):
    if str(message.from_user.id) != ADMIN_ID: return
    if not message.text or message.text.startswith('/'):
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=get_admin_menu())
        return
    clean_name = message.text.replace("https://t.me/", "").replace("@", "").strip()
    try:
        p = await get_pool()
        async with p.acquire() as c:
            result = await c.execute("DELETE FROM channels WHERE name = $1", clean_name)
            if result == "DELETE 1":
                _feed["data"] = None
                await state.clear()
                await message.answer(f"🗑 Канал <b>@{clean_name}</b> удален.", reply_markup=get_admin_menu())
            else:
                await message.answer(f"❌ Не найден.", reply_markup=get_admin_menu())
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}", reply_markup=get_admin_menu())

async def main():
    log.info("init db...")
    await init_db()
    asyncio.create_task(autopush_worker())
    asyncio.create_task(delayed_push_worker())
    log.info("bot started")
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot)

if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    asyncio.run(main())
