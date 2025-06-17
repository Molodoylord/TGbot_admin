# main.py
import asyncio
import logging
import json
import hmac
import hashlib
from os import getenv
from urllib.parse import unquote, parse_qsl
from collections import OrderedDict

import asyncpg
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton,
    ChatMemberUpdated, CallbackQuery
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError
from aiohttp import web
from dotenv import load_dotenv
import aiohttp_cors

import database # <-- Импортируем наш новый модуль

# --- 1. НАСТРОЙКА ЛОГГИРОВАНИЯ И ПЕРЕМЕННЫХ ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(name)s] - %(message)s')
logger = logging.getLogger(__name__)
load_dotenv()

# --- 2. ЧТЕНИЕ ПЕРЕМЕННЫХ ИЗ .ENV ---
BOT_TOKEN = getenv("BOT_TOKEN")
WEB_APP_URL = getenv("WEB_APP_URL")
WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = getenv("PORT", "8080")
API_PATH = "/api/chat_info"

# --- 4. ИНИЦИАЛИЗАЦИЯ ---
dp = Dispatcher()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
chat_recent_members = {} # Кэш недавних участников остается в памяти, это нормально
MAX_RECENT_MEMBERS_PER_CHAT = 100

# --- 5. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
# ... (is_valid_init_data и is_user_admin_in_chat остаются без изменений)
def is_valid_init_data(init_data: str, bot_token: str) -> bool:
    try:
        parsed_data = dict(parse_qsl(unquote(init_data), keep_blank_values=True))
        received_hash = parsed_data.pop("hash")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new("WebAppData".encode(), bot_token.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        return calculated_hash == received_hash
    except Exception as e:
        logger.error(f"Ошибка валидации initData: {e}")
        return False

async def is_user_admin_in_chat(user_id: int, chat_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
    except TelegramAPIError:
        return False

# --- 6. ОБРАБОТЧИКИ СОБЫТИЙ БОТА (Handlers) ---
@dp.my_chat_member()
async def on_my_chat_member(update: ChatMemberUpdated, db_pool: asyncpg.Pool):
    chat_id, chat_title = update.chat.id, update.chat.title
    if update.new_chat_member.status == ChatMemberStatus.ADMINISTRATOR:
        await database.add_chat(db_pool, chat_id, chat_title)
        me = await bot.get_me()
        keyboard = InlineKeyboardBuilder().button(text="🤖 Перейти к боту", url=f"https://t.me/{me.username}?start=group_admin")
        await bot.send_message(update.chat.id, f"✅ Панель для группы '{chat_title}' активна. Администраторы могут вызвать её командой /admin.", reply_markup=keyboard.as_markup())
    elif update.new_chat_member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.LEFT, ChatMemberStatus.KICKED]:
        await database.remove_chat(db_pool, chat_id)
        if chat_id in chat_recent_members: del chat_recent_members[chat_id]

@dp.message(Command("admin"))
async def command_admin_panel(message: Message, db_pool: asyncpg.Pool):
    managed_chats = await database.get_managed_chats(db_pool)
    if not managed_chats:
        return await message.answer("Я пока не управляю ни одной группой.")
    builder = InlineKeyboardBuilder()
    for chat in managed_chats:
        builder.button(text=chat['chat_title'], callback_data=f"manage_chat_{chat['chat_id']}")
    builder.adjust(1)
    await message.answer("Выберите группу для управления:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("manage_chat_"))
async def select_chat_callback(query: CallbackQuery, db_pool: asyncpg.Pool):
    chat_id = int(query.data.split("_")[2])
    if not await is_user_admin_in_chat(user_id=query.from_user.id, chat_id=chat_id):
        return await query.answer("Доступ запрещен.", show_alert=True)
    
    chats = await database.get_managed_chats(db_pool)
    chat_title = next((c['chat_title'] for c in chats if c['chat_id'] == chat_id), "Неизвестный чат")
    
    url = f"{WEB_APP_URL.rstrip('/')}?chat_id={chat_id}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"🚀 Открыть панель '{chat_title}'", web_app=WebAppInfo(url=url))]])
    await query.message.edit_text(f"Управление группой <b>{chat_title}</b>.", reply_markup=keyboard)
    await query.answer()

#... (remember_member_handler остается без изменений) ...
@dp.message(F.chat.type.in_(['group', 'supergroup']), ~F.text.startswith('/'))
async def remember_member_handler(message: Message):
    chat_id = message.chat.id
    user = message.from_user
    if user.is_bot: return
    if chat_id not in chat_recent_members:
        chat_recent_members[chat_id] = OrderedDict()
    user_info = {"id": user.id, "first_name": user.first_name, "last_name": user.last_name or "", "username": user.username or ""}
    chat_recent_members[chat_id][user.id] = user_info
    chat_recent_members[chat_id].move_to_end(user.id)
    while len(chat_recent_members[chat_id]) > MAX_RECENT_MEMBERS_PER_CHAT:
        chat_recent_members[chat_id].popitem(last=False)


@dp.message(F.web_app_data)
async def web_app_data_handler(message: Message, db_pool: asyncpg.Pool):
    try:
        data = json.loads(message.web_app_data.data)
        action, user_to_moderate, chat_id = data.get("action"), data.get("user_id"), int(data.get("chat_id"))
        admin_id = message.from_user.id

        if not all([action, user_to_moderate, chat_id]): raise ValueError("Неполные данные")
        if not await is_user_admin_in_chat(user_id=admin_id, chat_id=chat_id): return await message.answer("Нет прав.")

        if action == "ban":
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_to_moderate)
            await database.ban_user(db_pool, chat_id, user_to_moderate, admin_id)
            await message.answer(f"✅ Пользователь {user_to_moderate} забанен.")
        elif action == "kick": # Kick - это бан + последующий разбан
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_to_moderate)
            await bot.unban_chat_member(chat_id=chat_id, user_id=user_to_moderate, only_if_banned=True)
            await database.unban_user(db_pool, chat_id, user_to_moderate) # Убираем из списка банов
            await message.answer(f"✅ Пользователь {user_to_moderate} кикнут.")
    except Exception as e:
        logger.error(f"Ошибка WebApp: {e}", exc_info=True)
        await message.answer("❌ Ошибка при выполнении действия.")

# --- 7. API ДЛЯ WEB APP ---
async def get_chat_info_api_handler(request: web.Request):
    db_pool = request.app['db_pool']
    bot_from_app = request.app["bot"]
    # ... (валидация initData остается такой же)
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("tma "):
        return web.json_response({"error": "Authorization required"}, status=401)
    init_data = auth_header.split(" ", 1)[1]
    if not is_valid_init_data(init_data, bot_from_app.token):
        return web.json_response({"error": "Invalid initData"}, status=403)

    try:
        chat_id = int(request.query.get("chat_id"))
        user_info = json.loads(dict(parse_qsl(unquote(init_data))).get("user", "{}"))
        user_id = user_info.get("id")

        if not user_id or not await is_user_admin_in_chat(user_id=user_id, chat_id=chat_id):
            return web.json_response({"error": "Admin access required"}, status=403)
        
        chat_info_db = await db_pool.fetchrow("SELECT chat_title FROM managed_chats WHERE chat_id = $1", chat_id)
        if not chat_info_db: return web.json_response({"error": "Chat not managed"}, status=404)

        all_members = OrderedDict()
        admins = await bot_from_app.get_chat_administrators(chat_id)
        for admin in admins:
            if admin.user.is_bot: continue
            all_members[admin.user.id] = {"id": admin.user.id, "first_name": admin.user.first_name, "last_name": admin.user.last_name or "", "username": admin.user.username or ""}

        if chat_id in chat_recent_members:
            for recent_user_id, recent_user_info in reversed(chat_recent_members[chat_id].items()):
                if recent_user_id not in all_members: all_members[recent_user_id] = recent_user_info
        
        final_list = []
        for user in all_members.values():
            user['is_banned'] = await database.is_user_banned(db_pool, chat_id, user['id'])
            # Получение фото можно опустить для скорости или добавить при необходимости
            final_list.append(user)
        
        return web.json_response({"chat_title": chat_info_db['chat_title'], "members": final_list})
    except Exception as e:
        logger.error(f"Ошибка API: {e}", exc_info=True)
        return web.json_response({"error": "Internal server error"}, status=500)

async def index_handler(request: web.Request):
    #... (без изменений)
    index_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html')
    try:
        with open(index_path, 'r', encoding='utf-8') as f:
            return web.Response(text=f.read(), content_type='text/html')
    except FileNotFoundError:
        return web.Response(text='index.html not found', status=404)


# --- 8. ЗАПУСК БОТА И ВЕБ-СЕРВЕРА ---
async def on_startup(app: web.Application):
    """Выполняется при старте веб-сервера."""
    logger.info("Создание пула подключений к базе данных...")
    try:
        pool = await asyncpg.create_pool(DATABASE_URL)
        app['db_pool'] = pool
        await database.init_db(pool)
        logger.info("Пул подключений к БД создан.")
    except Exception as e:
        logger.critical(f"Не удалось подключиться к базе данных: {e}")
        exit() # Завершаем работу, если нет БД

async def on_shutdown(app: web.Application):
    """Выполняется при остановке веб-сервера."""
    logger.info("Закрытие пула подключений к базе данных...")
    await app['db_pool'].close()
    logger.info("Пул подключений закрыт.")

async def main():
    app = web.Application()
    app["bot"] = bot
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    
    app.router.add_get('/', index_handler)
    app.router.add_get(API_PATH, get_chat_info_api_handler)
    
    cors = aiohttp_cors.setup(app, defaults={"*": aiohttp_cors.ResourceOptions(allow_credentials=True, expose_headers="*", allow_headers="*", allow_methods="*")})
    for route in list(app.router.routes()): cors.add(route)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_SERVER_HOST, int(WEB_SERVER_PORT))
    
    try:
        await site.start()
        logger.info(f"Веб-сервер запущен на http://{WEB_SERVER_HOST}:{WEB_SERVER_PORT}")
        # Передаем пул соединений в хэндлеры aiogram
        dp['db_pool'] = app['db_pool']
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await runner.cleanup()
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")

