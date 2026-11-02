# language: Python 3.11, file: bot.py
import os
import asyncio
import json
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
FEED_URL = "https://raw.githubusercontent.com/brawle345-beep/gig-sniper/main/feed.json"
DB_FILE = "users.json"

ADMIN_ID = "7183342708"
MAINTENANCE_MODE = False
JOBS_PER_PAGE = 5

# --- Утилиты БД ---
def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f: return json.load(f)
        except: return {}
    return {}

def save_db(db):
    with open(DB_FILE, "w") as f:
        json.dump(db, f)

def has_premium(user_id):
    if str(user_id) == ADMIN_ID: return True
    db = load_db()
    uid = str(user_id)
    if uid in db and db[uid] > time.time(): return True
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

# Безопасное редактирование сообщений
async def safe_edit(message: Message, text: str, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup, disable_web_page_preview=True)
    except TelegramBadRequest:
        pass 

# --- Клавиатуры ---
def get_main_menu(is_admin=False):
    kb = [
        [InlineKeyboardButton(text="🔥 Свежие вакансии", callback_data="jobs:0")],
        [InlineKeyboardButton(text="💎 Купить Премиум", callback_data="buy_premium")],
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="my_status")],
        [InlineKeyboardButton(text="🎁 Пригласить друга", callback_data="invite")],
        [InlineKeyboardButton(text="📢 Наш канал", url="https://t.me/gigsnipervakansii")]
    ]
    if is_admin:
        kb.insert(0, [InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_back_button():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад в меню", callback_data="menu_main")]])

# --- Хендлеры ---
@dp.message(CommandStart())
async def start(message: Message):
    db = load_db()
    uid = str(message.from_user.id)
    is_admin = (uid == ADMIN_ID)
    
    # Уведомление админу о новом юзере
    if uid not in db:
        db[uid] = 0
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
        if ref_id.isdigit() and ref_id != uid:
            if ref_id in db: db[ref_id] += (7 * 86400)
            else: db[ref_id] = time.time() + (7 * 86400)
            try:
                await bot.send_message(ref_id, f"🎁 По твоей ссылке перешел {message.from_user.first_name}! Тебе начислено +7 дней Премиума.")
            except: pass
    save_db(db)

    text = (
        f"👋 <b>Привет, {message.from_user.first_name}!</b>\n\n"
        "🤖 Это <b>Gig Sniper</b> — твой радар свежих IT-заказов и вакансий в удаленке.\n\n"
        "<blockquote>"
        "Мы сканируем мировые биржи (RemoteOK, Freelancer) каждые 10 минут, отсеиваем шлак и присылаем только актуальные цели в долларах и рублях."
        "</blockquote>\n\n"
        "⚡️ <b>Выбери действие на панели ниже:</b>"
    )
    await message.answer(text, reply_markup=get_main_menu(is_admin))

@dp.callback_query(F.data == "menu_main")
async def back_to_menu(callback: CallbackQuery):
    is_admin = (str(callback.from_user.id) == ADMIN_ID)
    text = (
        f"👋 <b>Главное меню.</b>\n\n"
        "Твой радар на связи. Выбирай, что будем делать:"
    )
    await safe_edit(callback.message, text, reply_markup=get_main_menu(is_admin))
    await callback.answer()

@dp.callback_query(F.data.startswith("jobs:"))
async def show_jobs(callback: CallbackQuery):
    page = int(callback.data.split(":")[1])
    
    try:
        r = requests.get(FEED_URL, timeout=10)
        all_jobs = r.json() if r.status_code == 200 else []
    except Exception:
        all_jobs = []

    if not all_jobs:
        await safe_edit(callback.message, "Пока новых вакансий нет. Загляни позже!", reply_markup=get_back_button())
        await callback.answer()
        return

    is_premium = has_premium(callback.from_user.id)
    # Бесплатным показываем только первые 3 вакансии, премиум видит все
    jobs = all_jobs if is_premium else all_jobs[:3]

    total_pages = (len(jobs) - 1) // JOBS_PER_PAGE + 1
    if page >= total_pages: page = total_pages - 1
    if page < 0: page = 0

    start_idx = page * JOBS_PER_PAGE
    end_idx = start_idx + JOBS_PER_PAGE
    page_jobs = jobs[start_idx:end_idx]

    text = f"🔥 <b>Свежие вакансии</b> (Стр. {page + 1}/{total_pages}):\n\n"
    kb = []

    for job in page_jobs:
        title = job.get("title", "Без названия")
        company = job.get("company", "Не указана")
        url = job.get("url", "")
        sal = ""
        if job.get("salary_min") or job.get("salary_max"):
            sal = f"💰 ${job.get('salary_min', 0)} - ${job.get('salary_max', 0)}\n"
            
        text += f"▪️ <b>{title}</b>\n🏢 {company}\n{sal}\n"
        
        # Кнопка под каждую вакансию
        btn_text = f"Открыть: {title[:20]}..."
        kb.append([InlineKeyboardButton(text=btn_text, url=url)])

    # Навигация (листалка)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"jobs:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Ещё ➡️", callback_data=f"jobs:{page+1}"))
    if nav:
        kb.append(nav)

    if not is_premium:
        text += f"🔒 <i>Показано {len(jobs)} из {len(all_jobs)} вакансий.</i>\n"
        text += "💎 Купи Премиум, чтобы снять лимит."
        kb.append([InlineKeyboardButton(text="💎 Купить Премиум", callback_data="buy_premium")])
        
    kb.append([InlineKeyboardButton(text="🔙 В главное меню", callback_data="menu_main")])
    
    await safe_edit(callback.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()

@dp.callback_query(F.data == "buy_premium")
async def buy_premium(callback: CallbackQuery):
    await safe_edit(callback.message, "💎 <b>Счет отправлен ниже.</b>\n\nПосле успешной оплаты доступ откроется автоматически. (Для возврата в меню напиши /start)", reply_markup=get_back_button())
    
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
    db = load_db()
    uid = str(message.from_user.id)
    db[uid] = time.time() + (30 * 86400)
    save_db(db)
    
    is_admin = (uid == ADMIN_ID)
    await message.answer("✅ <b>Оплата прошла!</b> Доступ открыт на 30 дней. Жми «Свежие вакансии»!", reply_markup=get_main_menu(is_admin))

@dp.callback_query(F.data == "invite")
async def invite(callback: CallbackQuery):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{callback.from_user.id}"
    text = (
        f"🎁 <b>Партнерская программа</b>\n\n"
        f"Твоя ссылка:\n<code>{link}</code>\n\n"
        f"<blockquote>За каждого друга, который зайдет по ссылке, ты получаешь <b>+7 дней Премиума</b> бесплатно.</blockquote>"
    )
    await safe_edit(callback.message, text, reply_markup=get_back_button())
    await callback.answer()

@dp.callback_query(F.data == "my_status")
async def my_status(callback: CallbackQuery):
    uid = str(callback.from_user.id)
    if uid == ADMIN_ID:
        text = "👑 <b>Твой статус:</b> Админ\n<blockquote>У тебя безлимитный доступ ко всем функциям навсегда.</blockquote>"
    else:
        db = load_db()
        until = db.get(uid, 0)
        
        if until > time.time():
            days_left = int((until - time.time()) / 86400)
            text = f"💎 <b>Твой статус:</b> Премиум\n<blockquote>Осталось дней: <b>{days_left}</b></blockquote>"
        else:
            text = "👤 <b>Твой статус:</b> Бесплатный\n<blockquote>Доступно только 3 вакансии. Купи Премиум, чтобы видеть их все.</blockquote>"
            
    await safe_edit(callback.message, text, reply_markup=get_back_button())
    await callback.answer()

# --- Админ панель ---
@dp.callback_query(F.data == "admin_panel")
async def admin_panel(callback: CallbackQuery):
    if str(callback.from_user.id) != ADMIN_ID: return
    db = load_db()
    total = len(db)
    premium = sum(1 for uid, until in db.items() if until > time.time())
    
    text = (
        f"🛠 <b>Админ-панель</b>\n\n"
        f"👥 Всего пользователей: <b>{total}</b>\n"
        f"💎 Активных подписок: <b>{premium}</b>\n"
        f"💰 Заработано: <b>{premium * 250} ⭐</b>\n\n"
        f"<i>База сбрасывается при каждом перезапуске сервера.</i>"
    )
    await safe_edit(callback.message, text, reply_markup=get_back_button())
    await callback.answer()

async def main():
    print("Бот запущен...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    Thread(target=run_flask).start()
    asyncio.run(main())
