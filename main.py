# main.py
import asyncio
import logging
import json
import hmac
import hashlib
from os import getenv
from urllib.parse import unquote, parse_qsl
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
# ADMIN_ID больше не является обязательным для основной логики.
# Его можно использовать для отправки личных уведомлений о статусе бота.
ADMIN_ID = getenv("ADMIN_ID")
WEB_APP_URL = getenv("WEB_APP_URL")
BASE_WEBHOOK_URL = getenv("BASE_WEBHOOK_URL")
WEBHOOK_SECRET = getenv("WEBHOOK_SECRET", "your-super-secret-string-for-security")
WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = "8080"
API_PATH = "/api/chat_info"

# --- 3. ПРОВЕРКА КРИТИЧЕСКИ ВАЖНЫХ ПЕРЕМЕННЫХ ---
if not all([BOT_TOKEN, WEB_APP_URL, BASE_WEBHOOK_URL]):
    logger.critical("КРИТИЧЕСКАЯ ОШИБКА: Не все переменные .env загружены (BOT_TOKEN, WEB_APP_URL, BASE_WEBHOOK_URL)!")
    exit()

# Конвертируем ADMIN_ID в число, если он задан
if ADMIN_ID:
    try:
        ADMIN_ID = int(ADMIN_ID)
    except (ValueError, TypeError):
        logger.warning(f"Переменная ADMIN_ID ('{ADMIN_ID}') указана, но не является числом. Личные уведомления могут не работать.")
        ADMIN_ID = None

# --- 4. ИНИЦИАЛИЗАЦИЯ БОТА И ДИСПЕТЧЕРА ---
managed_chats = {}
dp = Dispatcher()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

logger.info(f"Бот инициализирован с токеном: {BOT_TOKEN[:10]}...")
logger.info(f"WEB_APP_URL: {WEB_APP_URL}")

# --- 5. ФУНКЦИИ БЕЗОПАСНОСТИ И ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def is_valid_init_data(init_data: str, bot_token: str) -> bool:
    """
    Валидирует строку initData из Telegram Web App.
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

async def is_user_admin_in_chat(user_id: int, chat_id: int) -> bool:
    """
    Проверяет, является ли пользователь администратором или создателем в чате.
    """
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
    except TelegramAPIError as e:
        logger.error(f"Не удалось проверить статус участника {user_id} в чате {chat_id}: {e}")
        return False

# --- 6. ОБРАБОТЧИКИ СОБЫТИЙ БОТА (Handlers) ---

@dp.my_chat_member()
async def on_my_chat_member(update: ChatMemberUpdated):
    """
    Отслеживает добавление/удаление бота из чатов.
    """
    chat_id = update.chat.id
    chat_title = update.chat.title

    # Если бота назначили администратором
    if update.new_chat_member.status == ChatMemberStatus.ADMINISTRATOR:
        managed_chats[chat_id] = chat_title
        logger.info(f"Бот назначен администратором в чате '{chat_title}' (ID: {chat_id}).")
        await bot.send_message(chat_id, f"✅ Панель управления для группы <b>{chat_title}</b> теперь активна. Администраторы могут вызвать её командой /admin в личном чате со мной.")

    # Если бота удалили или лишили прав
    elif update.new_chat_member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.LEFT, ChatMemberStatus.KICKED]:
        if chat_id in managed_chats:
            removed_chat_title = managed_chats.pop(chat_id)
            logger.info(f"Бот удален или лишен прав в чате '{removed_chat_title}'.")
            if ADMIN_ID: # Отправляем уведомление "супер-админу", если он задан
                 await bot.send_message(ADMIN_ID, f"❌ Я больше не администратор в группе: <b>{removed_chat_title}</b>.")

@dp.message(CommandStart())
async def command_start_handler(message: Message):
    """
    Обработчик команды /start. Доступен всем пользователям.
    """
    logger.info(f"Получена команда /start от пользователя {message.from_user.id}")
    await message.answer(f"👋 Привет! Я бот для управления группами. Если вы администратор группы, где я тоже администратор, используйте /admin, чтобы получить панель управления.")

@dp.message(Command("admin"))
async def command_admin_panel(message: Message):
    """
    Показывает список групп, где бот является админом. Доступен всем.
    Проверка прав будет при выборе группы.
    """
    if not managed_chats:
        return await message.answer("Я пока не управляю ни одной группой. Сделайте меня администратором в нужном чате, и он появится здесь.")

    builder = InlineKeyboardBuilder()
    for chat_id, chat_title in managed_chats.items():
        builder.button(text=chat_title, callback_data=f"manage_chat_{chat_id}")
    builder.adjust(1)
    await message.answer("Выберите группу для управления. Доступ к панели будет предоставлен только администраторам выбранной группы:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("manage_chat_"))
async def select_chat_callback(query: CallbackQuery):
    """
    Открывает Web App для выбранного чата.
    """
    chat_id = int(query.data.split("_")[2])
    
    # Проверяем, является ли пользователь админом этого чата
    if not await is_user_admin_in_chat(user_id=query.from_user.id, chat_id=chat_id):
        return await query.answer("Доступ запрещен. Вы не являетесь администратором в этой группе.", show_alert=True)
    
    chat_title = managed_chats.get(chat_id, "Неизвестный чат")
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
    """
    Обрабатывает данные, полученные из Web App (например, команду 'ban').
    """
    try:
        data = json.loads(message.web_app_data.data)
        logger.info(f"Получены данные из Web App: {data}")

        action = data.get("action")
        user_id_to_moderate = data.get("user_id")
        chat_id = data.get("chat_id")
        admin_id = message.from_user.id # ID того, кто нажал кнопку в Web App

        if not all([action, user_id_to_moderate, chat_id]):
            raise ValueError("Неполные данные из WebApp")

        # Ключевая проверка: может ли пользователь, отправивший команду, модерировать этот чат
        if not await is_user_admin_in_chat(user_id=admin_id, chat_id=int(chat_id)):
             await message.answer(f"❌ Действие отклонено. У вас нет прав администратора в чате {chat_id}.")
             return

        if action == "ban":
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_id_to_moderate)
            await message.answer(f"✅ Пользователь {user_id_to_moderate} был забанен в чате {chat_id}.")
        elif action == "kick":
            # Kick - это временный бан, для удаления используется unban
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_id_to_moderate)
            await bot.unban_chat_member(chat_id=chat_id, user_id=user_id_to_moderate)
            await message.answer(f"✅ Пользователь {user_id_to_moderate} был кикнут из чата {chat_id}.")
        else:
             await message.answer(f"Неизвестное действие: {action}")

    except Exception as e:
        logger.error(f"Ошибка обработки данных из Web App: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка при выполнении действия.")


# --- 7. API ДЛЯ WEB APP (HTTP-сервер) ---

async def get_chat_info_api_handler(request: web.Request):
    """
    API эндпоинт для Web App. Отдает информацию о чате и его участниках.
    """
    bot_from_app = request.app["bot"]
    auth_header = request.headers.get("Authorization")

    if not auth_header or not auth_header.startswith("tma "):
        return web.json_response({"error": "Authorization required"}, status=401)
    
    init_data = auth_header.split(" ", 1)[1]
    if not is_valid_init_data(init_data, bot_from_app.token):
        logger.warning(f"Попытка доступа с невалидным initData.")
        return web.json_response({"error": "Invalid initData"}, status=403)

    try:
        chat_id_str = request.query.get("chat_id")
        if not chat_id_str:
            return web.json_response({"error": "chat_id is required"}, status=400)
        chat_id = int(chat_id_str)
        
        user_info_raw = dict(parse_qsl(unquote(init_data))).get("user", "{}")
        user_info = json.loads(user_info_raw)
        user_id = user_info.get("id")

        if not user_id:
            return web.json_response({"error": "Invalid user data in initData"}, status=403)

        # Главная проверка: является ли пользователь, открывший WebApp, админом в чате
        if not await is_user_admin_in_chat(user_id=user_id, chat_id=chat_id):
            logger.warning(f"Пользователь {user_id} попытался получить доступ к чату {chat_id} без прав администратора.")
            return web.json_response({"error": "Admin access required"}, status=403)
        
        chat_info = await bot_from_app.get_chat(chat_id)
        # Получаем ВСЕХ администраторов чата
        admins = await bot_from_app.get_chat_administrators(chat_id)
        members = []
        for admin in admins:
            if admin.user.is_bot: continue # Пропускаем ботов

            try:
                # Получаем фото профиля
                profile_photos = await bot_from_app.get_user_profile_photos(admin.user.id, limit=1)
                photo_url = None
                if profile_photos.photos:
                    file_info = await bot_from_app.get_file(profile_photos.photos[0][-1].file_id)
                    photo_url = f"https://api.telegram.org/file/bot{bot_from_app.token}/{file_info.file_path}"
            except Exception:
                photo_url = None # Если фото нет или ошибка
            
            members.append({
                "id": admin.user.id,
                "first_name": admin.user.first_name,
                "last_name": admin.user.last_name or "",
                "username": admin.user.username or "",
                "photo_url": photo_url
            })
            
        logger.info(f"Отправлена информация для чата '{chat_info.title}'. Пользователь {user_id}. Найдено администраторов: {len(members)}")
        return web.json_response({
            "chat_title": chat_info.title,
            "members": members
        })

    except ValueError:
         return web.json_response({"error": "Invalid chat_id"}, status=400)
    except Exception as e:
        logger.error(f"Ошибка получения информации о чате: {e}", exc_info=True)
        return web.json_response({"error": "Internal server error"}, status=500)

async def index_handler(request: web.Request):
    """Отдает файл index.html"""
    index_path = os.path.join(os.path.dirname(__file__), 'index.html')
    try:
        with open(index_path, 'r', encoding='utf-8') as f:
            html = f.read()
        return web.Response(text=html, content_type='text/html')
    except Exception as e:
        return web.Response(text=f'Ошибка загрузки index.html: {e}', status=500)

# --- 8. ЗАПУСК БОТА И ВЕБ-СЕРВЕРА ---

async def on_startup(app: web.Application):
    logger.info("Веб-сервер запущен")
    # Восстанавливаем список управляемых чатов при запуске (если необходимо)
    # Здесь может быть логика чтения из базы данных или файла
    # Для простоты пока оставляем пустым

async def on_shutdown(app: web.Application):
    logger.info("Остановка веб-сервера...")

def main():
    app = web.Application()
    app["bot"] = bot
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    
    app.router.add_get('/', index_handler)
    app.router.add_get(API_PATH, get_chat_info_api_handler)
    
    # Настройка CORS
    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True, expose_headers="*", allow_headers="*", allow_methods="*"
        )
    })
    for route in list(app.router.routes()):
        cors.add(route)
    
    async def start_bot_and_server():
        # Запускаем веб-сервер
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, WEB_SERVER_HOST, int(WEB_SERVER_PORT))
        await site.start()
        logger.info(f"Веб-сервер запущен на http://{WEB_SERVER_HOST}:{WEB_SERVER_PORT}")

        # Запускаем бота
        try:
            me = await bot.get_me()
            logger.info(f"Бот @{me.username} успешно подключен!")
            logger.info("Запуск бота в режиме long polling...")
            await dp.start_polling(bot)
        finally:
            await runner.cleanup()

    try:
        asyncio.run(start_bot_and_server())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен вручную.")

if __name__ == "__main__":
    main()


