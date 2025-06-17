# main.py
import asyncio
import logging
import json
import hmac
import hashlib
from os import getenv
from urllib.parse import urljoin, unquote, parse_qsl

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
import os

# --- 1. НАСТРОЙКА ЛОГГИРОВАНИЯ И ПЕРЕМЕННЫХ ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(name)s] - %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()

# --- 2. ЧТЕНИЕ ПЕРЕМЕННЫХ ИЗ .ENV ---
BOT_TOKEN = getenv("BOT_TOKEN")
ADMIN_ID = getenv("ADMIN_ID")
WEB_APP_URL = getenv("WEB_APP_URL")
BASE_WEBHOOK_URL = getenv("BASE_WEBHOOK_URL")
WEBHOOK_SECRET = getenv("WEBHOOK_SECRET", "your-super-secret-string-for-security")

WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = "8080"
WEBHOOK_PATH = "/webhook/"
API_PATH = "/api/chat_info"


# --- 3. ПРОВЕРКА КРИТИЧЕСКИ ВАЖНЫХ ПЕРЕМЕННЫХ ---
if not all([BOT_TOKEN, WEB_APP_URL, BASE_WEBHOOK_URL, ADMIN_ID]):
    logger.critical("КРИТИЧЕСКАЯ ОШИБКА: Не все переменные .env загружены!")
    exit()
try:
    ADMIN_ID = int(ADMIN_ID)
except (ValueError, TypeError):
    logger.critical(f"КРИТИЧЕСКАЯ ОШИБКА: ADMIN_ID '{ADMIN_ID}' должен быть числом!")
    exit()


# --- 4. ИНИЦИАЛИЗАЦИЯ БОТА И ДИСПЕТЧЕРА ---
managed_chats = {}
dp = Dispatcher()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

# Добавляем логирование для диагностики
logger.info(f"Бот инициализирован с токеном: {BOT_TOKEN[:10]}...")
logger.info(f"ADMIN_ID: {ADMIN_ID}")
logger.info(f"WEB_APP_URL: {WEB_APP_URL}")
logger.info(f"BASE_WEBHOOK_URL: {BASE_WEBHOOK_URL}")


# --- 5. ФУНКЦИИ БЕЗОПАСНОСТИ И ФИЛЬТРЫ ---
def is_valid_init_data(init_data: str, bot_token: str) -> bool:
    """
    Validates the initData string from a Telegram Web App.
    """
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

# --- 6. ОБРАБОТЧИКИ СОБЫТИЙ БОТА (Handlers) ---
@dp.my_chat_member()
async def on_my_chat_member(update: ChatMemberUpdated):
    chat_id = update.chat.id
    chat_title = update.chat.title
    if update.new_chat_member.status == ChatMemberStatus.ADMINISTRATOR:
        managed_chats[chat_id] = chat_title
        logger.info(f"Бот назначен администратором в чате '{chat_title}' (ID: {chat_id}).")
        await bot.send_message(ADMIN_ID, f"✅ Я теперь администратор в группе: <b>{chat_title}</b>.")
    elif update.new_chat_member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.LEFT, ChatMemberStatus.KICKED]:
        if chat_id in managed_chats:
            removed_chat_title = managed_chats.pop(chat_id)
            logger.info(f"Бот удален или лишен прав в чате '{removed_chat_title}'.")
            await bot.send_message(ADMIN_ID, f"❌ Я больше не администратор в группе: <b>{removed_chat_title}</b>.")

@dp.message(CommandStart())
async def command_start_handler(message: Message):
    logger.info(f"Получена команда /start от пользователя {message.from_user.id}")
    if message.from_user.id == ADMIN_ID:
        await message.answer(f"👋 Привет, администратор! Используйте /admin для вызова панели управления.")
        logger.info("Отправлен ответ администратору")
    else:
        await message.answer("Этот бот предназначен только для администратора.")
        logger.info(f"Отправлен ответ обычному пользователю {message.from_user.id}")

@dp.message(Command("admin"))
async def command_admin_panel(message: Message):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("Эта команда только для администратора.")
    if not managed_chats:
        return await message.answer("Я пока не управляю ни одной группой. Сделайте меня администратором в нужном чате.")
    builder = InlineKeyboardBuilder()
    for chat_id, chat_title in managed_chats.items():
        builder.button(text=chat_title, callback_data=f"manage_chat_{chat_id}")
    builder.adjust(1)
    await message.answer("Выберите группу для управления:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("manage_chat_"))
async def select_chat_callback(query: CallbackQuery):
    if query.from_user.id != ADMIN_ID:
        return await query.answer("Недостаточно прав.", show_alert=True)
    chat_id = query.data.split("_")[2]
    chat_title = managed_chats.get(int(chat_id), "Неизвестный чат")
    url = f"{WEB_APP_URL.rstrip('/')}?chat_id={chat_id}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"🚀 Открыть панель для '{chat_title}'",
            web_app=WebAppInfo(url=url)
        )
    ]])
    await query.message.edit_text(f"Управление группой <b>{chat_title}</b>.", reply_markup=keyboard)
    await query.answer()

@dp.message(F.web_app_data)
async def web_app_data_handler(message: Message):
    if message.from_user.id != ADMIN_ID: return
    try:
        data = json.loads(message.web_app_data.data)
        logger.info(f"Получены данные из Web App: {data}")
        action = data.get("action")
        user_id = data.get("user_id")
        chat_id = data.get("chat_id")
        if not all([action, user_id, chat_id]):
            raise ValueError("Неполные данные из WebApp")
        if action == "ban":
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            await message.answer(f"Пользователь {user_id} был забанен в чате {chat_id}.")
        elif action == "kick":
            await bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
            await message.answer(f"Пользователь {user_id} был кикнут из чата {chat_id}.")
    except Exception as e:
        logger.error(f"Ошибка обработки данных из Web App: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка при выполнении действия.")

@dp.message()
async def echo_handler(message: Message):
    """Обработчик для всех сообщений (тестовый)"""
    logger.info(f"Получено сообщение от {message.from_user.id}: {message.text}")
    await message.answer(f"Получил ваше сообщение: {message.text}")
    logger.info(f"Отправлен ответ пользователю {message.from_user.id}")

# --- 7. API ДЛЯ WEB APP (HTTP-сервер) ---
async def get_chat_info_api_handler(request: web.Request):
    bot_from_app = request.app["bot"]
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("tma "):
        return web.json_response({"error": "Authorization required"}, status=401)
    init_data = auth_header.split(" ", 1)[1]
    if not is_valid_init_data(init_data, bot_from_app.token):
        logger.warning(f"Попытка доступа с невалидным initData.")
        return web.json_response({"error": "Invalid initData"}, status=403)
    try:
        user_info_raw = dict(parse_qsl(unquote(init_data))).get("user", "{}")
        user_info = json.loads(user_info_raw)
        if not user_info or user_info.get("id") != ADMIN_ID:
            return web.json_response({"error": "Admin access required"}, status=403)
    except (json.JSONDecodeError, AttributeError):
        return web.json_response({"error": "Invalid user data in initData"}, status=403)
    chat_id_str = request.query.get("chat_id")
    if not chat_id_str:
        return web.json_response({"error": "chat_id is required"}, status=400)
    try:
        chat_id = int(chat_id_str)
        chat_info = await bot_from_app.get_chat(chat_id)
        admins = await bot_from_app.get_chat_administrators(chat_id)
        members = []
        for admin in admins:
            if not admin.user.is_bot:
                try:
                    profile_photos = await bot_from_app.get_user_profile_photos(admin.user.id, limit=1)
                    photo_url = None
                    if profile_photos.photos:
                        file_info = await bot_from_app.get_file(profile_photos.photos[0][-1].file_id)
                        photo_url = f"https://api.telegram.org/file/bot{bot_from_app.token}/{file_info.file_path}"
                except Exception:
                    photo_url = None
                members.append({
                    "id": admin.user.id,
                    "first_name": admin.user.first_name,
                    "last_name": admin.user.last_name or "",
                    "username": admin.user.username or "",
                    "photo_url": photo_url
                })
        logger.info(f"Отправлена информация для чата '{chat_info.title}'. Найдено администраторов: {len(members)}")
        return web.json_response({
            "chat_title": chat_info.title,
            "members": members
        })
    except Exception as e:
        logger.error(f"Ошибка получения информации о чате {chat_id_str}: {e}", exc_info=True)
        return web.json_response({"error": "Internal server error"}, status=500)

# --- 7.1. ОБРАБОТЧИК ДЛЯ ОТДАЧИ index.html ---
async def index_handler(request: web.Request):
    index_path = os.path.join(os.path.dirname(__file__), 'index.html')
    try:
        with open(index_path, 'r', encoding='utf-8') as f:
            html = f.read()
        return web.Response(text=html, content_type='text/html')
    except Exception as e:
        return web.Response(text=f'Ошибка загрузки index.html: {e}', status=500)

async def favicon_handler(request: web.Request):
    """Обработчик для favicon.ico"""
    return web.Response(status=204)  # No Content

# --- 8. ИСПРАВЛЕННАЯ ФУНКЦИЯ ЗАПУСКА ДЛЯ AIOGRAM 3.X ---
async def start_bot():
    """Запуск бота в режиме long polling"""
    try:
        me = await bot.get_me()
        logger.info(f"Бот @{me.username} успешно подключен!")
        logger.info("Запуск бота в режиме long polling...")
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Ошибка запуска бота: {e}", exc_info=True)

async def on_startup(app: web.Application):
    logger.info("Веб-сервер запущен")

async def on_shutdown(app: web.Application):
    logger.info("Остановка веб-сервера...")

def main():
    app = web.Application()
    app["bot"] = bot
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.router.add_get('/', index_handler)
    app.router.add_get('/favicon.ico', favicon_handler)
    app.router.add_get(API_PATH, get_chat_info_api_handler)
    
    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True, expose_headers="*", allow_headers="*", allow_methods="*"
        )
    })
    for route in list(app.router.routes()):
        cors.add(route)
    
    logger.info("Запуск веб-сервера...")

    async def run_all():
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, WEB_SERVER_HOST, WEB_SERVER_PORT)
        await site.start()
        logger.info(f"Веб-сервер запущен на http://{WEB_SERVER_HOST}:{WEB_SERVER_PORT}")

        await start_bot()
    
    try:
        asyncio.run(run_all())
    except KeyboardInterrupt:
        logger.info("Бот остановлен вручную.")

if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен вручную.")


