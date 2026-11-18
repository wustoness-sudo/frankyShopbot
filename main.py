"""
Телеграм-бот магазина (пепельницы и брелоки) — версия для бесплатного хостинга Render (webhook).
Переменные окружения: BOT_TOKEN, ADMIN_ID, WEBHOOK_SECRET (Render сам даёт RENDER_EXTERNAL_URL и PORT).
"""
import logging
import os
import re

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me-secret")
BASE_URL = os.getenv("BASE_URL") or os.getenv("RENDER_EXTERNAL_URL", "")
PORT = int(os.getenv("PORT", "8080"))
WEBHOOK_PATH = "/webhook"

# ---------- Каталог: редактируй под себя ----------
PRODUCTS = {
    "ash1": {"name": "Пепельница «Классика»", "price": 800},
    "ash2": {"name": "Пепельница «Бетон»", "price": 1200},
    "key1": {"name": "Брелок «Кожаный»", "price": 400},
    "key2": {"name": "Брелок «Металл»", "price": 500},
}
DELIVERY = {
    "cdek": "СДЭК",
    "post": "Почта России",
    "courier": "Курьер",
    "pickup": "Самовывоз",
}
# --------------------------------------------------

router = Router()
ID_RE = re.compile(r"ID: (\d+)")  # id покупателя берём прямо из текста сообщения у админа


class Order(StatesGroup):
    delivery = State()
    address = State()
    confirm = State()


def catalog_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{p['name']} — {p['price']} ₽", callback_data=f"item:{k}")]
            for k, p in PRODUCTS.items()
        ]
    )


def delivery_kb():
    rows = [[InlineKeyboardButton(text=v, callback_data=f"deliv:{k}")] for k, v in DELIVERY.items()]
    rows.append([InlineKeyboardButton(text="⬅️ В каталог", callback_data="catalog")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="catalog"),
        ]]
    )


def user_label(obj) -> str:
    u = obj.from_user
    return f"{u.full_name} (@{u.username})" if u.username else u.full_name


# ---------- Админ отвечает покупателю (Reply на сообщение бота) ----------
@router.message(F.chat.id == ADMIN_ID, F.reply_to_message)
async def admin_reply(message: Message, bot: Bot):
    src = message.reply_to_message
    m = ID_RE.search(src.text or src.caption or "")
    if not m:
        await message.reply("Не вижу, кому отвечать. Ответь (Reply) на сообщение с заказом или на заголовок 💬 от покупателя.")
        return
    try:
        await bot.copy_message(int(m.group(1)), message.chat.id, message.message_id)
        await message.reply("✅ Отправлено")
    except Exception as e:
        await message.reply(f"Не удалось отправить: {e}")


@router.message(F.chat.id == ADMIN_ID)
async def admin_other(message: Message):
    await message.answer("Чтобы ответить покупателю, свайпни по его сообщению и ответь (Reply).")


# ---------- Покупатель ----------
@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Привет! Выбери товар:", reply_markup=catalog_kb())


@router.callback_query(F.data == "catalog")
async def back_to_catalog(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Выбери товар:", reply_markup=catalog_kb())
    await call.answer()


@router.callback_query(F.data.startswith("item:"))
async def choose_item(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":")[1]
    if key not in PRODUCTS:
        await call.answer("Товар не найден", show_alert=True)
        return
    await state.update_data(item=key)
    await state.set_state(Order.delivery)
    p = PRODUCTS[key]
    await call.message.edit_text(
        f"Ты выбрал: {p['name']} — {p['price']} ₽\n\nКак доставить?", reply_markup=delivery_kb()
    )
    await call.answer()


@router.callback_query(Order.delivery, F.data.startswith("deliv:"))
async def choose_delivery(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":")[1]
    await state.update_data(delivery=key)
    if key == "pickup":
        await state.update_data(address="— (самовывоз)")
        await state.set_state(Order.confirm)
        await show_summary(call.message, state, edit=True)
    else:
        await state.set_state(Order.address)
        await call.message.edit_text(
            "Напиши адрес доставки (город, улица, дом, кв. / пункт выдачи) "
            "и номер телефона для связи одним сообщением:"
        )
    await call.answer()


@router.message(Order.address, F.text)
async def got_address(message: Message, state: FSMContext):
    await state.update_data(address=message.text)
    await state.set_state(Order.confirm)
    await show_summary(message, state, edit=False)


async def show_summary(message: Message, state: FSMContext, edit: bool):
    d = await state.get_data()
    p = PRODUCTS[d["item"]]
    text = (
        "Проверь заказ:\n\n"
        f"Товар: {p['name']}\n"
        f"Цена: {p['price']} ₽\n"
        f"Доставка: {DELIVERY[d['delivery']]}\n"
        f"Адрес: {d['address']}"
    )
    if edit:
        await message.edit_text(text, reply_markup=confirm_kb())
    else:
        await message.answer(text, reply_markup=confirm_kb())


@router.callback_query(Order.confirm, F.data == "confirm")
async def confirm_order(call: CallbackQuery, state: FSMContext, bot: Bot):
    d = await state.get_data()
    p = PRODUCTS[d["item"]]
    await bot.send_message(
        ADMIN_ID,
        "🛒 НОВЫЙ ЗАКАЗ\n\n"
        f"Покупатель: {user_label(call)}\n"
        f"ID: {call.from_user.id}\n\n"
        f"Товар: {p['name']}\n"
        f"Цена: {p['price']} ₽\n"
        f"Доставка: {DELIVERY[d['delivery']]}\n"
        f"Адрес: {d['address']}\n\n"
        "↩️ Ответь на это сообщение (Reply), чтобы написать покупателю.",
    )
    await state.clear()
    await call.message.edit_text(
        "Спасибо! Заказ принят ✅\nПродавец скоро напишет тебе здесь. "
        "Можешь писать сообщения прямо в этот чат.\n\n/start — новый заказ"
    )
    await call.answer()


# ---------- Любое другое сообщение покупателя -> админу ----------
@router.message(StateFilter(None), F.chat.type == "private")
async def buyer_message(message: Message, bot: Bot):
    if message.text:
        await bot.send_message(
            ADMIN_ID,
            f"💬 {user_label(message)}\nID: {message.from_user.id}\n\n{message.text}\n\n↩️ Ответь на это сообщение (Reply).",
        )
    else:  # фото, голосовые, стикеры и т.п.
        await bot.send_message(
            ADMIN_ID,
            f"💬 {user_label(message)}\nID: {message.from_user.id}\n↩️ Ответь (Reply) на ЭТО сообщение, а не на вложение ниже.",
        )
        await bot.copy_message(ADMIN_ID, message.chat.id, message.message_id)


# ---------- Запуск через webhook ----------
async def on_startup(bot: Bot):
    await bot.set_webhook(f"{BASE_URL}{WEBHOOK_PATH}", secret_token=WEBHOOK_SECRET)


async def health(_):
    return web.Response(text="ok")


def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)

    app = web.Application()
    app.router.add_get("/", health)  # для проверки живости / пинга
    SimpleRequestHandler(dp, bot, secret_token=WEBHOOK_SECRET).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
