# language: Python 3.11, file: bot.py
import os
import asyncio
from threading import Thread
from flask import Flask
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice, PreCheckoutQuery
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

TOKEN = os.environ.get("BOT_TOKEN")

# --- Flask (нужен, чтобы Render не засыпал) ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

# --- Aiogram (сам бот) ---
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
    # В будущем здесь можно читать feed.json с GitHub
    await callback.message.edit_text(
        "🔥 <b>Свежие вакансии</b>\n\n"
        "Здесь будут появляться вакансии. (Настройка фида в процессе)",
        reply_markup=get_main_menu()
    )
    await callback.answer()

@dp.callback_query(F.data == "buy_premium")
async def buy_premium(callback: CallbackQuery):
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Премиум доступ",
        description="Полный доступ к базе вакансий на 30 дней",
        payload="premium_30",
        provider_token="", # Для Telegram Stars оставляем пустым
        currency="XTR",
        prices=[LabeledPrice(label="Доступ", amount=150)],
    )
    await callback.answer()

@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)

@dp.message(F.successful_payment)
async def on_paid(message: Message):
    await message.answer("✅ <b>Оплата прошла!</b> Доступ открыт на 30 дней. Скоро здесь появятся вакансии.", reply_markup=get_main_menu())

@dp.callback_query(F.data == "invite")
async def invite(callback: CallbackQuery):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{callback.from_user.id}"
    text = (
        "🎁 <b>Реферальная программа</b>\n\n"
        f"Твоя ссылка:\n<code>{link}</code>\n\n"
        "За каждого друга, который зайдет по ней, ты получаешь +7 дней Премиума бесплатно!"
    )
    await callback.message.edit_text(text, reply_markup=get_main_menu(), disable_web_page_preview=True)
    await callback.answer()

async def main():
    print("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    Thread(target=run_flask).start()
    asyncio.run(main())
