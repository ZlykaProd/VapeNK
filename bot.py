import asyncio
import base64
import json
import logging
import os
import re
from io import BytesIO

import aiohttp
import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup,
    InlineKeyboardButton
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()
DB_PATH = os.getenv("DB_PATH", "inventory.db")

# Администраторы задаются по username без @.
ADMIN_USERNAMES = {"p0huy02", "yazlyka", "miii69_54"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("inventory-bot")

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в .env")

bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher(storage=MemoryStorage())


class AdminStates(StatesGroup):
    waiting_photo = State()
    waiting_delete = State()


# Для временного хранения результата распознавания до подтверждения.
pending_products: dict[int, list[dict]] = {}


def is_admin(user) -> bool:
    username = (user.username or "").lower()
    return username in ADMIN_USERNAMES


def main_keyboard(admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📦  КАТАЛОГ ТОВАРОВ", callback_data="catalog")],
        [InlineKeyboardButton(text="ℹ️  КАК ЭТО РАБОТАЕТ", callback_data="help")],
    ]
    if admin:
        rows.append([InlineKeyboardButton(text="⚙️  АДМИН-ПАНЕЛЬ", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 Добавить по фото", callback_data="add_photo")],
        [InlineKeyboardButton(text="📦 Все товары", callback_data="catalog")],
        [InlineKeyboardButton(text="🗑 Удалить товар", callback_data="delete")],
        [InlineKeyboardButton(text="📋 Заявки", callback_data="requests")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")],
    ])


async def safe_edit(message: Message, text: str, keyboard=None):
    try:
        await message.edit_text(text, reply_markup=keyboard)
    except Exception:
        await message.answer(text, reply_markup=keyboard)


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                product_id INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                username TEXT
            )
        """)
        await db.commit()


async def register_admin(user):
    if not is_admin(user):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO admins(user_id, username) VALUES (?, ?)",
            (user.id, user.username or "")
        )
        await db.commit()


async def get_admin_ids() -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM admins") as cur:
            return [row[0] for row in await cur.fetchall()]


async def add_product(name: str, quantity: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO products(name, quantity) VALUES (?, ?)",
            (name, quantity)
        )
        await db.commit()


async def get_products():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, name, quantity FROM products ORDER BY id"
        ) as cur:
            return await cur.fetchall()


async def get_product(product_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, name, quantity FROM products WHERE id = ?",
            (product_id,)
        ) as cur:
            return await cur.fetchone()


async def delete_product(product_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM products WHERE id = ?", (product_id,))
        await db.commit()
        return cur.rowcount > 0


async def save_request(user_id: int, username: str, product):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO requests(user_id, username, product_id, product_name)
            VALUES (?, ?, ?, ?)
        """, (user_id, username, product[0], product[1]))
        await db.commit()


async def get_requests():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT id, user_id, username, product_id, product_name, created_at
            FROM requests ORDER BY id DESC LIMIT 40
        """) as cur:
            return await cur.fetchall()


async def analyze_photo(image_bytes: bytes) -> list[dict]:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("Не задан DEEPSEEK_API_KEY в .env")

    encoded = base64.b64encode(image_bytes).decode("ascii")
    prompt = """
Проанализируй фотографию накладной и извлеки товары.
Верни только JSON такого вида:
{"products":[{"name":"Название товара","quantity":2}]}

Правила:
- Используй только товары, которые реально видны на накладной.
- Количество бери из соответствующей строки.
- Если количество отсутствует или неразборчиво, поставь 1.
- Не добавляй пояснений и Markdown.
"""

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{encoded}"
                    }
                }
            ]
        }],
        "temperature": 0.1
    }

    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            "https://api.deepseek.com/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json"
            },
            json=payload
        ) as response:
            body = await response.text()
            if response.status != 200:
                raise RuntimeError(
                    f"DeepSeek API вернул HTTP {response.status}: {body[:600]}"
                )
            data = json.loads(body)

    content = data["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )

    content = re.sub(r"```(?:json)?", "", str(content), flags=re.I).replace("```", "").strip()
    match = re.search(r"\{.*\}", content, re.S)
    if not match:
        raise ValueError("ИИ не вернул JSON. Попробуй фото получше.")

    parsed = json.loads(match.group())
    result = []
    for item in parsed.get("products", []):
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        try:
            quantity = int(item.get("quantity", 1))
        except (TypeError, ValueError):
            quantity = 1
        result.append({"name": name[:180], "quantity": max(1, quantity)})

    return result


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    admin = is_admin(message.from_user)
    if admin:
        await register_admin(message.from_user)

    role = "Администратор" if admin else "Пользователь"
    text = (
        "╔══════════════════════╗\n"
        "      <b>INVENTORY STORE</b>\n"
        "╚══════════════════════╝\n\n"
        "👋 Добро пожаловать!\n\n"
        "📦 Просматривайте каталог и выбирайте товары.\n"
        "🛒 Чтобы запросить товар, нажмите кнопку под его названием.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Статус: <b>{role}</b>\n"
        f"🆔 ID: <code>{message.from_user.id}</code>\n\n"
        "Выберите раздел:"
    )
    await message.answer(text, reply_markup=main_keyboard(admin))


@dp.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    pending_products.pop(message.from_user.id, None)
    await message.answer(
        "❌ Действие отменено.",
        reply_markup=main_keyboard(is_admin(message.from_user))
    )


@dp.message(Command("myid"))
async def my_id(message: Message):
    await message.answer(
        f"Ваш Telegram ID: <code>{message.from_user.id}</code>\n"
        f"Username: @{message.from_user.username or 'не задан'}"
    )


@dp.callback_query(F.data == "home")
async def home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(
        call.message,
        "🏠 <b>ГЛАВНОЕ МЕНЮ</b>\n\nВыберите нужный раздел:",
        main_keyboard(is_admin(call.from_user))
    )
    await call.answer()


@dp.callback_query(F.data == "help")
async def help_menu(call: CallbackQuery):
    await safe_edit(
        call.message,
        "ℹ️ <b>КАК ПОЛЬЗОВАТЬСЯ</b>\n\n"
        "1. Откройте каталог товаров.\n"
        "2. Нажмите кнопку нужного товара.\n"
        "3. Бот отправит заявку администраторам.\n\n"
        "Остаток не уменьшается автоматически.",
        main_keyboard(is_admin(call.from_user))
    )
    await call.answer()


@dp.callback_query(F.data == "admin")
async def admin_panel(call: CallbackQuery):
    if not is_admin(call.from_user):
        await call.answer("⛔ Нет доступа.", show_alert=True)
        return
    await register_admin(call.from_user)
    await safe_edit(
        call.message,
        "╔══════════════════════╗\n"
        "        <b>ADMIN PANEL</b>\n"
        "╚══════════════════════╝\n\n"
        "⚙️ Управление складом\n\n"
        "📸 Добавление товаров по фотографии\n"
        "📦 Просмотр остатков\n"
        "🗑 Удаление товара по номеру\n"
        "📋 Просмотр заявок",
        admin_keyboard()
    )
    await call.answer()


@dp.callback_query(F.data == "add_photo")
async def add_photo_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user):
        await call.answer("⛔ Нет доступа.", show_alert=True)
        return
    await register_admin(call.from_user)
    await state.set_state(AdminStates.waiting_photo)
    await safe_edit(
        call.message,
        "📸 <b>ДОБАВЛЕНИЕ ПО ФОТО</b>\n\n"
        "Отправьте фотографию накладной.\n"
        "ИИ попробует распознать названия и количество.\n"
        "Если количество не указано, будет установлена 1 штука.\n\n"
        "После распознавания вы сможете проверить список.\n\n"
        "Для отмены отправьте /cancel."
    )
    await call.answer()


@dp.message(AdminStates.waiting_photo, F.photo)
async def process_photo(message: Message, state: FSMContext):
    if not is_admin(message.from_user):
        return

    status = await message.answer("🔍 <b>АНАЛИЗИРУЮ НАКЛАДНУЮ...</b>\nПожалуйста, подождите.")
    try:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        buffer = BytesIO()
        await bot.download_file(file_info.file_path, destination=buffer)

        products = await analyze_photo(buffer.getvalue())
        if not products:
            await status.edit_text(
                "⚠️ Не удалось распознать товары.\n"
                "Попробуйте отправить более чёткое фото."
            )
            return

        pending_products[message.from_user.id] = products
        lines = ["╔══════════════════════╗", "   <b>РАСПОЗНАННЫЕ ТОВАРЫ</b>", "╚══════════════════════╝", ""]
        for index, product in enumerate(products, 1):
            lines.append(
                f"<b>{index}. {product['name']}</b>\n"
                f"   📦 Количество: <b>{product['quantity']} шт.</b>\n"
            )
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("Проверьте список. Сохранить товары на склад?")
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить добавление", callback_data="confirm_products")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_products")]
        ])
        await status.edit_text("\n".join(lines), reply_markup=keyboard)
        await state.clear()

    except Exception as exc:
        log.exception("Ошибка распознавания фото")
        await status.edit_text(
            "❌ <b>ОШИБКА РАСПОЗНАВАНИЯ</b>\n\n"
            f"<code>{str(exc)[:700]}</code>"
        )


@dp.message(AdminStates.waiting_photo)
async def wrong_photo(message: Message):
    await message.answer("⚠️ Отправьте фотографию накладной или /cancel.")


@dp.callback_query(F.data == "confirm_products")
async def confirm_products(call: CallbackQuery):
    if not is_admin(call.from_user):
        await call.answer("⛔ Нет доступа.", show_alert=True)
        return
    products = pending_products.pop(call.from_user.id, None)
    if not products:
        await call.answer("Список не найден или уже обработан.", show_alert=True)
        return

    for item in products:
        await add_product(item["name"], item["quantity"])

    await safe_edit(
        call.message,
        "✅ <b>ТОВАРЫ ДОБАВЛЕНЫ</b>\n\n"
        f"📦 Позиций добавлено: <b>{len(products)}</b>",
        admin_keyboard()
    )
    await call.answer("Сохранено!")


@dp.callback_query(F.data == "cancel_products")
async def cancel_products(call: CallbackQuery):
    if not is_admin(call.from_user):
        await call.answer("⛔ Нет доступа.", show_alert=True)
        return
    pending_products.pop(call.from_user.id, None)
    await safe_edit(call.message, "❌ Добавление отменено.", admin_keyboard())
    await call.answer("Отменено")


@dp.callback_query(F.data == "catalog")
async def catalog(call: CallbackQuery):
    products = await get_products()
    if not products:
        await safe_edit(
            call.message,
            "📦 <b>КАТАЛОГ ТОВАРОВ</b>\n\nПока товаров нет.",
            main_keyboard(is_admin(call.from_user))
        )
        await call.answer()
        return

    lines = ["╔══════════════════════╗", "       <b>КАТАЛОГ ТОВАРОВ</b>", "╚══════════════════════╝", ""]
    buttons = []
    for product_id, name, quantity in products:
        lines.append(
            f"🔹 <b>#{product_id} — {name}</b>\n"
            f"   📦 Остаток: <b>{quantity} шт.</b>\n"
            "━━━━━━━━━━━━━━━━━━━━"
        )
        if quantity > 0:
            buttons.append([
                InlineKeyboardButton(
                    text=f"🛒 Выбрать товар #{product_id}",
                    callback_data=f"buy:{product_id}"
                )
            ])
    lines.append("\n👇 <b>Нажмите на товар, чтобы оставить заявку.</b>")
    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")])
    await safe_edit(
        call.message,
        "\n".join(lines)[:4000],
        InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await call.answer()


@dp.callback_query(F.data.startswith("buy:"))
async def buy_product(call: CallbackQuery):
    try:
        product_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("Некорректный номер товара.", show_alert=True)
        return

    product = await get_product(product_id)
    if not product:
        await call.answer("Товар не найден.", show_alert=True)
        return
    if product[2] <= 0:
        await call.answer("Товар закончился.", show_alert=True)
        return

    user = call.from_user
    username = user.username or ""
    await save_request(user.id, username, product)

    username_display = f"@{username}" if username else "Без username"
    notification = (
        "╔══════════════════════╗\n"
        "        <b>НОВАЯ ЗАЯВКА</b>\n"
        "╚══════════════════════╝\n\n"
        f"👤 Пользователь: {username_display}\n"
        f"🆔 ID: <code>{user.id}</code>\n\n"
        f"📦 Товар: <b>{product[1]}</b>\n"
        f"🔢 Номер: <b>#{product[0]}</b>\n\n"
        "⏳ Статус: ожидает обработки"
    )

    admin_ids = await get_admin_ids()
    sent = 0
    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, notification)
            sent += 1
        except Exception:
            log.exception("Не удалось отправить уведомление админу %s", admin_id)

    await safe_edit(
        call.message,
        "✅ <b>ЗАЯВКА ОФОРМЛЕНА</b>\n\n"
        f"📦 Товар: <b>{product[1]}</b>\n"
        f"🔢 Номер: #{product[0]}\n\n"
        "Администраторы получили уведомление.\n"
        "Ожидайте связи.",
        main_keyboard(is_admin(user))
    )
    await call.answer("Заявка отправлена!" if sent else "Заявка сохранена!")


@dp.callback_query(F.data == "delete")
async def delete_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user):
        await call.answer("⛔ Нет доступа.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_delete)
    await safe_edit(
        call.message,
        "🗑 <b>УДАЛЕНИЕ ТОВАРА</b>\n\n"
        "Отправьте номер товара, например: <code>12</code>\n\n"
        "Для отмены отправьте /cancel."
    )
    await call.answer()


@dp.message(AdminStates.waiting_delete)
async def delete_process(message: Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    if not message.text or not message.text.strip().isdigit():
        await message.answer("⚠️ Введите номер товара цифрами.")
        return

    product_id = int(message.text.strip())
    product = await get_product(product_id)
    if not product:
        await message.answer("❌ Товар с таким номером не найден. Попробуйте ещё раз или /cancel.")
        return

    await delete_product(product_id)
    await state.clear()
    await message.answer(
        "✅ <b>ТОВАР УДАЛЁН</b>\n\n"
        f"📦 {product[1]}\n"
        f"🔢 Номер: #{product_id}",
        reply_markup=admin_keyboard()
    )


@dp.callback_query(F.data == "requests")
async def requests_list(call: CallbackQuery):
    if not is_admin(call.from_user):
        await call.answer("⛔ Нет доступа.", show_alert=True)
        return

    requests = await get_requests()
    if not requests:
        await safe_edit(call.message, "📋 <b>ЗАЯВКИ</b>\n\nПока заявок нет.", admin_keyboard())
        await call.answer()
        return

    lines = ["📋 <b>ПОСЛЕДНИЕ ЗАЯВКИ</b>", ""]
    for req_id, user_id, username, product_id, name, created in requests:
        username_text = f"@{username}" if username else "Без username"
        lines.append(
            f"📝 <b>Заявка #{req_id}</b>\n"
            f"👤 {username_text}\n"
            f"🆔 <code>{user_id}</code>\n"
            f"📦 {name} (#{product_id})\n"
            f"🕒 {created}\n"
            "━━━━━━━━━━━━━━━━━━━━"
        )

    await safe_edit(call.message, "\n".join(lines)[:4000], admin_keyboard())
    await call.answer()


async def main():
    await init_db()
    log.info("Inventory bot запущен")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
