# language: Python 3.11, file: bot.py
import os
import asyncio
import asyncpg
import time
import sys
import requests
from threading import Thread
from flask import Flask
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice, PreCheckoutQuery, TelegramObject
from aiogram.filters import CommandStart, Command
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest

TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL") # Строка подключения из Neon
FEED_URL = "https://raw.githubusercontent.com/brawle345-beep/gig-sniper/main/feed.json"

ADMIN_ID = "7183342708"
MAINTENANCE_MODE = False

# --- База данных ---
async def init_db():
    """Создаёт таблицу пользователей при первом запуске"""
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            premium_until TIMESTAMP WITH TIME ZONE
        );
    """)
    await conn.close()

async def get_premium_until(user_id: int):
    """Возвращает дату окончания подписки или None"""
    conn = await asyncpg.connect(DATABASE_URL)
    row = await conn.fetchrow("SELECT premium_until FROM users WHERE user_id = $1", user_id)
    await conn.close()
    if row:
        return row['premium_until']
    return None

async def set_premium_until(user_id: int, until_timestamp: float):
    """Устанавливает дату окончания подписки"""
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
        INSERT INTO users (user_id, premium_until) 
        VALUES ($1, to_timestamp($2)) 
        ON CONFLICT (user_id) DO UPDATE SET premium_until = to_timestamp($2)
    """, user_id, until_timestamp)
    await conn.close()

async def has_premium(user_id: int):
    """Проверяет, активна ли подписка"""
    if str(user_id) == ADMIN_ID:
        return True
    until = await get_premium_until(user_id)
    if until and until.timestamp() > time.time():
        return True
    return False

# --- Flask для Render ---
app = Flask(__name__)
@app.route('/')
def home(): return "Bot is running!"
def run_flask(): 
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

# --- Aiogram ---
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

class MaintenanceMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        if MAINTENANCE_MODE:
            if event.from_user and str(event.from_user.id) == ADMIN_ID:
                return await handler(event, data)
            if isinstance(event, Message):
                await event.answer("🛠 <b>Бот на техническом обслуживании.</b>\n\nМы обновляем базу вакансий. Зайдите чуть позже!")
            elif isinstance(event, CallbackQuery):
                await event.answer("🛠 Бот на тех. обслуживании.", show_alert=True)
            return 
        return await handler(event, data)

dp.message.middleware(MaintenanceMiddleware())
dp.callback_query.middleware(MaintenanceMiddleware())

def get_main_menu(is_admin=False):
    kb = [
        [InlineKeyboardButton(text="🔥 Свежие вакансии", callback_data="show_jobs")],
        [InlineKeyboardButton(text="💎 Купить Премиум", callback_data="buy_premium")],
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="my_status")],
        [InlineKeyboardButton(text="🎁 Пригласить друга", callback_data="invite")],
        [InlineKeyboardButton(text="📢 Наш канал", url="https://t.me/gigsnipervakansii")]
    ]
    if is_admin:
        kb.insert(0, [InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

async def safe_edit(message: Message, text: str, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup, disable_web_page_preview=True)
    except TelegramBadRequest:
        pass 

@dp.message(CommandStart())
async def start(message: Message):
    uid = message.from_user.id
    is_admin = (str(uid) == ADMIN_ID)
    
    # Проверяем, новый ли пользователь
    until = await get_premium_until(uid)
    is_new = (until is None)
    
    if is_new:
        await set_premium_until(uid, 0)
        if not is_admin:
            try:
                user_name = message.from_user.first_name
                if message.from_user.username: user_name += f" (@{message.from_user.username})"
                await bot.send_message(ADMIN_ID, f"👤 <b>Новый пользователь!</b>\nИмя: {user_name}\nID: <code>{uid}</code>")
            except: pass
            
    # Реферальная система
    args = message.text.split()
    if len(args) > 1 and args[1].startswith("ref_"):
        ref_id = args[1].replace("ref_", "")
        if ref_id.isdigit() and int(ref_id) != uid:
            ref_until = await get_premium_until(int(ref_id))
            base_time = time.time()
            if ref_until and ref_until.timestamp() > time.time():
                base_time = ref_until.timestamp()
            await set_premium_until(int(ref_id), base_time + (7 * 86400))
            try:
                await bot.send_message(int(ref_id), f"🎁 По твоей ссылке перешел {message.from_user.first_name}! Тебе начислено +7 дней Премиума.")
            except: pass

    text = (
        f"👋 <b>Привет, {message.from_user.first_name}!</b>\n\n"
        "🤖 Это <b>Gig Sniper</b> — твой радар свежих IT-заказов и вакансий в удаленке.\n\n"
        "<blockquote>Мы сканируем мировые биржи (RemoteOK, Freelancer) каждые 10 минут, отсеиваем шлак и присылаем только актуальные цели в долларах и рублях.</blockquote>\n\n"
        "⚡️ <b>Выбери действие на панели ниже:</b>"
    )
    await message.answer(text, reply_markup=get_main_menu(is_admin))

@dp.callback_query(F.data == "show_jobs")
async def show_jobs(callback: CallbackQuery):
    await safe_edit(callback.message, "🔄 Загружаю свежие вакансии...")
    try:
        r = requests.get(FEED_URL, timeout=10)
        jobs = r.json() if r.status_code == 200 else []
    except Exception:
        jobs = []

    if not jobs:
        await safe_edit(callback.message, "Пока новых вакансий нет. Загляни позже!", reply_markup=get_main_menu(is_admin=(str(callback.from_user.id) == ADMIN_ID)))
        await callback.answer()
        return

    is_prem = await has_premium(callback.from_user.id)
    limit = 10 if is_prem else 3
    text = "🔥 <b>Свежие вакансии:</b>\n\n"
    for job in jobs[:limit]:
        title = job.get("title", "Без названия")
        company = job.get("company", "Не указана")
        url = job.get("url", "")
        text += f"▪️ <a href='{url}'>{title}</a>\n🏢 {company}\n\n"
    
    if not is_prem:
        text += f"🔒 <i>Показано {limit} из {len(jobs)} вакансий.</i>\n💎 Купи Премиум, чтобы видеть их все."
    
    await safe_edit(callback.message, text, reply_markup=get_main_menu(is_admin=(str(callback.from_user.id) == ADMIN_ID)))
    await callback.answer()

@dp.callback_query(F.data == "buy_premium")
async def buy_premium(callback: CallbackQuery):
    await safe_edit(callback.message, "💎 <b>Счет на оплату отправлен ниже.</b>\n\nПосле оплаты доступ откроется автоматически. (В меню — /start)", reply_markup=get_main_menu(is_admin=(str(callback.from_user.id) == ADMIN_ID)))
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Премиум доступ",
        description="Полный доступ к базе вакансий на 30 дней",
        payload="premium_30",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Доступ", amount=250)],
    )
    await callback.answer()

@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)

@dp.message(F.successful_payment)
async def on_paid(message: Message):
    uid = message.from_user.id
    current_until = await get_premium_until(uid)
    base_time = time.time()
    if current_until and current_until.timestamp() > time.time():
        base_time = current_until.timestamp()
    await set_premium_until(uid, base_time + (30 * 86400))
    await message.answer("✅ <b>Оплата прошла!</b> Доступ открыт на 30 дней. Жми «Свежие вакансии»!", reply_markup=get_main_menu(is_admin=(str(uid) == ADMIN_ID)))

@dp.callback_query(F.data == "invite")
async def invite(callback: CallbackQuery):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{callback.from_user.id}"
    text = f"🎁 <b>Партнерская программа</b>\n\nТвоя ссылка:\n<code>{link}</code>\n\n<blockquote>За каждого друга — <b>+7 дней Премиума</b>.</blockquote>"
    await safe_edit(callback.message, text, reply_markup=get_main_menu(is_admin=(str(callback.from_user.id) == ADMIN_ID)))
    await callback.answer()

@dp.callback_query(F.data == "my_status")
async def my_status(callback: CallbackQuery):
    uid = str(callback.from_user.id)
    if uid == ADMIN_ID:
        text = "👑 <b>Твой статус:</b> Админ\n<blockquote>Безлимитный доступ навсегда.</blockquote>"
    else:
        until = await get_premium_until(callback.from_user.id)
        if until and until.timestamp() > time.time():
            days_left = int((until.timestamp() - time.time()) / 86400)
            text = f"💎 <b>Твой статус:</b> Премиум\n<blockquote>Осталось дней: <b>{days_left}</b></blockquote>"
        else:
            text = "👤 <b>Твой статус:</b> Бесплатный\n<blockquote>Доступно только 3 вакансии.</blockquote>"
    await safe_edit(callback.message, text, reply_markup=get_main_menu(is_admin=(uid == ADMIN_ID)))
    await callback.answer()

@dp.callback_query(F.data == "admin_panel")
async def admin_panel(callback: CallbackQuery):
    if str(callback.from_user.id) != ADMIN_ID: return
    conn = await asyncpg.connect(DATABASE_URL)
    total = await conn.fetchval("SELECT COUNT(*) FROM users")
    premium = await conn.fetchval("SELECT COUNT(*) FROM users WHERE premium_until > NOW()")
    await conn.close()
    text = f"🛠 <b>Админ-панель</b>\n\n👥 Всего пользователей: <b>{total}</b>\n💎 Активных подписок: <b>{premium}</b>\n💰 Примерный доход: <b>{premium * 250} ⭐</b>\n\n<i>Данные хранятся в облаке и не сбрасываются.</i>"
    await safe_edit(callback.message, text, reply_markup=get_main_menu(is_admin=True))
    await callback.answer()

async def main():
    print("Инициализация базы данных...")
    await init_db()  # Создаем таблицу при старте
    print("Бот запущен...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    Thread(target=run_flask).start()
    asyncio.run(main())
