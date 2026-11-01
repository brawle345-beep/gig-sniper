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

TOKEN = os.environ.get("BOT_TOKEN")
FEED_URL = "https://raw.githubusercontent.com/brawle345-beep/gig-sniper/main/feed.json"
DB_FILE = "users.json"

# Твой ID вшит сюда
ADMIN_ID = "7183342708"

# 🚨 ВЫКЛЮЧАТЕЛЬ ТЕХ. ОБСЛУЖИВАНИЯ 🚨
MAINTENANCE_MODE = False

def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return json.load(f)
    return {}

def save_db(db):
    with open(DB_FILE, "w") as f:
        json.dump(db, f)

def has_premium(user_id):
    if str(user_id) == ADMIN_ID:
        return True
    db = load_db()
    uid = str(user_id)
    if uid in db:
        if db[uid] > time.time():
            return True
        else:
            del db[uid]
            save_db(db)
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

# --- ПРОВЕРКА ТЕХ. ОБСЛУЖИВАНИЯ ---
class MaintenanceMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        if MAINTENANCE_MODE:
            if event.from_user and str(event.from_user.id) == ADMIN_ID:
                return await handler(event, data)
            
            if isinstance(event, Message):
                await event.answer("🛠 <b>Бот на техническом обслуживании.</b>\n\nМы обновляем базу вакансий. Пожалуйста, зайдите чуть позже!")
            elif isinstance(event, CallbackQuery):
                await event.answer("🛠 Бот на тех. обслуживании. Зайдите чуть позже!", show_alert=True)
            return 
        
        return await handler(event, data)

dp.message.middleware(MaintenanceMiddleware())
dp.callback_query.middleware(MaintenanceMiddleware())

def get_main_menu():
    kb = [
        [InlineKeyboardButton(text="🔥 Свежие вакансии", callback_data="show_jobs")],
        [InlineKeyboardButton(text="💎 Купить Премиум", callback_data="buy_premium")],
        [InlineKeyboardButton(text="🎁 Пригласить друга", callback_data="invite")],
        [InlineKeyboardButton(text="📊 Мой статус", callback_data="my_status")],
        [InlineKeyboardButton(text="📢 Перейти в канал", url="https://t.me/gigsnipervakansii")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

@dp.message(CommandStart())
async def start(message: Message):
    db = load_db()
    uid = str(message.from_user.id)
    
    # Уведомление тебе о новом пользователе
    is_new_user = uid not in db
    if is_new_user:
        db[uid] = 0
        if uid != ADMIN_ID:
            try:
                user_name = message.from_user.first_name
                if message.from_user.username:
                    user_name += f" (@{message.from_user.username})"
                await bot.send_message(ADMIN_ID, f"👤 <b>Новый пользователь!</b>\nИмя: {user_name}\nID: <code>{uid}</code>")
            except Exception as e:
                print(f"Ошибка при отправке уведомления: {e}", file=sys.stderr)
        
    args = message.text.split()
    if len(args) > 1 and args[1].startswith("ref_"):
        ref_id = args[1].replace("ref_", "")
        if ref_id.isdigit() and ref_id != uid:
            if ref_id in db:
                db[ref_id] += (7 * 86400)
            else:
                db[ref_id] = time.time() + (7 * 86400)
            save_db(db)
            
            friend_name = message.from_user.first_name
            if message.from_user.username:
                friend_name += f" (@{message.from_user.username})"
            try:
                await bot.send_message(ref_id, f"🎁 По твоей ссылке перешел {friend_name}! Тебе начислено +7 дней Премиума.")
            except Exception:
                pass
    else:
        save_db(db)

    # НОВЫЙ ТЕКСТ ПРИВЕТСТВИЯ
    text = (
        "<b>Снайпер на позиции. 🎯</b>\n\n"
        "Радар сканирует мировые биржи каждые 10 минут. Свежие цели (вакансии) уже в прицеле, мусор отсеян.\n\n"
        "<b>Твои действия:</b>\n"
        "1️⃣ <b>«🔥 Свежие вакансии»</b> — забрать 3 бесплатных заказа.\n"
        "2️⃣ <b>«💎 Купить Премиум»</b> — снять лимит и видеть все 30+ целей.\n"
        "3️⃣ <b>«🎁 Пригласить друга»</b> — получить +7 дней к прицелу бесплатно.\n\n"
        "Выбирай кнопку ниже. Работаем."
    )
    await message.answer(text, reply_markup=get_main_menu())

@dp.callback_query(F.data == "show_jobs")
async def show_jobs(callback: CallbackQuery):
    loading_msg = await callback.message.answer("🔄 Загружаю свежие вакансии...")
    await callback.answer()
    
    try:
        r = requests.get(FEED_URL, timeout=10)
        jobs = r.json() if r.status_code == 200 else []
    except Exception:
        jobs = []

    if not jobs:
        await loading_msg.edit_text(
            "Пока новых вакансий нет. Я проверяю доски каждые 10 минут. Загляни позже!", 
            reply_markup=get_main_menu()
        )
        return

    is_premium = has_premium(callback.from_user.id)
    limit = 10 if is_premium else 3
    text = "🔥 <b>Свежие вакансии:</b>\n\n"
    
    for job in jobs[:limit]:
        title = job.get("title", "Без названия")
        company = job.get("company", "Не указана")
        url = job.get("url", "")
        text += f"▪️ <a href='{url}'>{title}</a>\n🏢 {company}\n\n"
    
    if not is_premium:
        text += f"🔒 <i>Показано {limit} из {len(jobs)} вакансий.</i>\n"
        text += "💎 Купи Премиум, чтобы видеть их все."
    
    await loading_msg.edit_text(text, reply_markup=get_main_menu(), disable_web_page_preview=True)

@dp.callback_query(F.data == "buy_premium")
async def buy_premium(callback: CallbackQuery):
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
    await message.answer("✅ <b>Оплата прошла!</b> Доступ открыт на 30 дней. Жми «Свежие вакансии»!", reply_markup=get_main_menu())

@dp.callback_query(F.data == "invite")
async def invite(callback: CallbackQuery):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{callback.from_user.id}"
    text = f"🎁 <b>Реферальная программа</b>\n\nТвоя ссылка:\n<code>{link}</code>\n\nЗа друга — +7 дней Премиума!"
    await callback.message.answer(text, reply_markup=get_main_menu(), disable_web_page_preview=True)
    await callback.answer()

@dp.callback_query(F.data == "my_status")
async def my_status(callback: CallbackQuery):
    if str(callback.from_user.id) == ADMIN_ID:
        text = "📊 <b>Твой статус:</b> Админ (Премиум навсегда)"
    else:
        db = load_db()
        uid = str(callback.from_user.id)
        until = db.get(uid, 0)
        
        if until > time.time():
            days_left = int((until - time.time()) / 86400)
            text = f"📊 <b>Твой статус:</b> Премиум\nОсталось дней: {days_left}"
        else:
            text = "📊 <b>Твой статус:</b> Бесплатный\nКупи Премиум, чтобы видеть все вакансии."
            
    await callback.message.answer(text, reply_markup=get_main_menu())
    await callback.answer()

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if str(message.from_user.id) != ADMIN_ID:
        return
        
    db = load_db()
    total_users = len(db)
    premium_users = sum(1 for uid, until in db.items() if until > time.time())
    
    text = (
        f"📊 <b>Статистика бота:</b>\n\n"
        f"👥 Всего пользователей: <b>{total_users}</b>\n"
        f"💎 С активным Премиумом: <b>{premium_users}</b>"
    )
    await message.answer(text)

async def main():
    print("Бот запущен...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    Thread(target=run_flask).start()
    asyncio.run(main())
