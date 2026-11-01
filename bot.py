# language: Python 3.11, file: bot.py
import os
import asyncio
import json
import time
import requests
from threading import Thread
from flask import Flask
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice, PreCheckoutQuery
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

TOKEN = os.environ.get("BOT_TOKEN")
FEED_URL = "https://raw.githubusercontent.com/brawle345-beep/gig-sniper/main/feed.json"
DB_FILE = "users.json"

def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return json.load(f)
    return {}

def save_db(db):
    with open(DB_FILE, "w") as f:
        json.dump(db, f)

def has_premium(user_id):
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

def get_main_menu():
    kb = [
        [InlineKeyboardButton(text="🔥 Свежие вакансии", callback_data="show_jobs")],
        [InlineKeyboardButton(text="💎 Купить Премиум", callback_data="buy_premium")],
        [InlineKeyboardButton(text="🎁 Пригласить друга", callback_data="invite")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

@dp.message(CommandStart())
async def start(message: Message):
    text = (
        "👋 <b>Привет! Я твой карманный снайпер.</b>\n\n"
        "Я сканирую биржи труда и присылаю только свежие заказы.\n"
        "Жми на кнопки ниже, чтобы начать."
    )
    await message.answer(text, reply_markup=get_main_menu())

@dp.callback_query(F.data == "show_jobs")
async def show_jobs(callback: CallbackQuery):
    # Отправляем новое сообщение вместо редактирования старого
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

async def main():
    print("Бот запущен...")
    # Удаляем вебхук на всякий случай, чтобы избежать конфликта
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    Thread(target=run_flask).start()
    asyncio.run(main())
