# main.py
import asyncio
import logging
import json
import hmac
import hashlib
from os import getenv
from urllib.parse import unquote, parse_qsl
from collections import OrderedDict

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
WEB_APP_URL = getenv("WEB_APP_URL")
WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = getenv("PORT", "8080") # Используем PORT, если доступно (для хостингов)
API_PATH = "/api/chat_info"

if not all([BOT_TOKEN, WEB_APP_URL]):
    logger.critical("КРИТИЧЕСКАЯ ОШИБКА: Не загружены переменные BOT_TOKEN или WEB_APP_URL!")
    exit()

# --- 4. ИНИЦИАЛИЗАЦИЯ БОТА, ДИСПЕТЧЕРА И ХРАНИЛИЩ ---
dp = Dispatcher()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

# { chat_id: chat_title }
managed_chats = {} 
# { chat_id: OrderedDict({user_id: user_object}) }
chat_recent_members = {}
MAX_RECENT_MEMBERS_PER_CHAT = 100 # Храним до 100 последних активных участников

logger.info(f"Бот инициализирован. Web App URL: {WEB_APP_URL}")

# --- 5. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def is_valid_init_data(init_data: str, bot_token: str) -> bool:
    """Валидирует строку initData из Telegram Web App."""
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
    """Проверяет, является ли пользователь администратором или создателем в чате."""
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
    except TelegramAPIError:
        return False

# --- 6. ОБРАБОТЧИКИ СОБЫТИЙ БОТА (Handlers) ---

@dp.my_chat_member()
async def on_my_chat_member(update: ChatMemberUpdated):
    """Отслеживает добавление/удаление бота из чатов."""
    chat_id = update.chat.id
    chat_title = update.chat.title

    if update.new_chat_member.status == ChatMemberStatus.ADMINISTRATOR:
        managed_chats[chat_id] = chat_title
        logger.info(f"Бот назначен администратором в чате '{chat_title}' (ID: {chat_id}).")
        
        me = await bot.get_me()
        keyboard = InlineKeyboardBuilder()
        keyboard.button(text="🤖 Перейти к боту для управления", url=f"https://t.me/{me.username}?start=group_admin")

        message_text = (
            f"✅ <b>Панель управления для группы '{chat_title}' активна!</b>\n\n"
            "Чтобы управлять участниками, администраторам необходимо перейти в личный чат со мной и "
            "использовать команду /admin."
        )
        await bot.send_message(chat_id, message_text, reply_markup=keyboard.as_markup())

    elif update.new_chat_member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.LEFT, ChatMemberStatus.KICKED]:
        if chat_id in managed_chats:
            removed_chat_title = managed_chats.pop(chat_id)
            if chat_id in chat_recent_members:
                del chat_recent_members[chat_id] # Очищаем кэш участников
            logger.info(f"Бот удален или лишен прав в чате '{removed_chat_title}'.")

@dp.message(CommandStart())
async def command_start_handler(message: Message):
    """Обработчик команды /start."""
    await message.answer(f"👋 Привет! Я бот для управления группами. Если вы администратор группы, где я тоже администратор, используйте /admin, чтобы получить панель управления.")

@dp.message(Command("admin"))
async def command_admin_panel(message: Message):
    """Показывает список групп для управления."""
    if not managed_chats:
        return await message.answer("Я пока не управляю ни одной группой. Сделайте меня администратором в нужном чате.")

    builder = InlineKeyboardBuilder()
    for chat_id, chat_title in managed_chats.items():
        builder.button(text=chat_title, callback_data=f"manage_chat_{chat_id}")
    builder.adjust(1)
    await message.answer("Выберите группу. Доступ к панели будет предоставлен только её администраторам:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("manage_chat_"))
async def select_chat_callback(query: CallbackQuery):
    """Открывает Web App для выбранного чата."""
    chat_id = int(query.data.split("_")[2])
    
    if not await is_user_admin_in_chat(user_id=query.from_user.id, chat_id=chat_id):
        return await query.answer("Доступ запрещен. Вы не администратор в этой группе.", show_alert=True)
    
    chat_title = managed_chats.get(chat_id, "Неизвестный чат")
    url = f"{WEB_APP_URL.rstrip('/')}?chat_id={chat_id}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"🚀 Открыть панель '{chat_title}'", web_app=WebAppInfo(url=url))
    ]])
    await query.message.edit_text(f"Управление группой <b>{chat_title}</b>.", reply_markup=keyboard)
    await query.answer()

@dp.message(F.chat.type.in_(['group', 'supergroup']), ~F.text.startswith('/'))
async def remember_member_handler(message: Message):
    """Запоминает активных участников чата."""
    chat_id = message.chat.id
    user = message.from_user
    if user.is_bot: return

    if chat_id not in chat_recent_members:
        chat_recent_members[chat_id] = OrderedDict()

    user_info = {
        "id": user.id,
        "first_name": user.first_name,
        "last_name": user.last_name or "",
        "username": user.username or "",
    }
    chat_recent_members[chat_id][user.id] = user_info
    chat_recent_members[chat_id].move_to_end(user.id) # Обновляем "свежесть"

    while len(chat_recent_members[chat_id]) > MAX_RECENT_MEMBERS_PER_CHAT:
        chat_recent_members[chat_id].popitem(last=False)

@dp.message(F.web_app_data)
async def web_app_data_handler(message: Message):
    """Обрабатывает данные из Web App."""
    try:
        data = json.loads(message.web_app_data.data)
        action, user_to_moderate, chat_id = data.get("action"), data.get("user_id"), data.get("chat_id")
        admin_id = message.from_user.id

        if not all([action, user_to_moderate, chat_id]):
            raise ValueError("Неполные данные из WebApp")
        chat_id = int(chat_id)

        if not await is_user_admin_in_chat(user_id=admin_id, chat_id=chat_id):
             return await message.answer(f"❌ Действие отклонено. У вас нет прав администратора.")

        if action == "ban":
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_to_moderate)
            await message.answer(f"✅ Пользователь {user_to_moderate} забанен.")
        elif action == "kick":
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_to_moderate)
            await bot.unban_chat_member(chat_id=chat_id, user_id=user_to_moderate, only_if_banned=True)
            await message.answer(f"✅ Пользователь {user_to_moderate} кикнут.")
        else:
             await message.answer(f"Неизвестное действие: {action}")

    except Exception as e:
        logger.error(f"Ошибка обработки данных из Web App: {e}", exc_info=True)
        await message.answer("❌ Ошибка при выполнении действия.")

# --- 7. API ДЛЯ WEB APP ---

async def get_chat_info_api_handler(request: web.Request):
    bot_from_app = request.app["bot"]
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
        
        chat_info = await bot_from_app.get_chat(chat_id)
        
        # Собираем всех участников: сначала админов, потом недавно активных
        all_members = OrderedDict()

        # 1. Получаем администраторов
        admins = await bot_from_app.get_chat_administrators(chat_id)
        for admin in admins:
            if admin.user.is_bot: continue
            all_members[admin.user.id] = {
                "id": admin.user.id, "first_name": admin.user.first_name,
                "last_name": admin.user.last_name or "", "username": admin.user.username or ""
            }

        # 2. Добавляем недавно активных, избегая дубликатов
        if chat_id in chat_recent_members:
            for recent_user_id, recent_user_info in reversed(chat_recent_members[chat_id].items()):
                if recent_user_id not in all_members:
                    all_members[recent_user_id] = recent_user_info
        
        # 3. Получаем фото для всех собранных участников
        final_list_with_photos = []
        for user in all_members.values():
            try:
                profile_photos = await bot_from_app.get_user_profile_photos(user['id'], limit=1)
                photo_url = None
                if profile_photos.photos:
                    file = await bot_from_app.get_file(profile_photos.photos[0][-1].file_id)
                    photo_url = f"https://api.telegram.org/file/bot{bot_from_app.token}/{file.file_path}"
                user['photo_url'] = photo_url
            except TelegramAPIError:
                user['photo_url'] = None
            final_list_with_photos.append(user)

        logger.info(f"Отправлена информация для чата '{chat_info.title}'. Пользователей в списке: {len(final_list_with_photos)}")
        return web.json_response({"chat_title": chat_info.title, "members": final_list_with_photos})

    except (ValueError, TypeError):
         return web.json_response({"error": "Invalid chat_id"}, status=400)
    except Exception as e:
        logger.error(f"Ошибка API: {e}", exc_info=True)
        return web.json_response({"error": "Internal server error"}, status=500)

async def index_handler(request: web.Request):
    index_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html')
    try:
        with open(index_path, 'r', encoding='utf-8') as f:
            return web.Response(text=f.read(), content_type='text/html')
    except FileNotFoundError:
        return web.Response(text='index.html not found', status=404)

# --- 8. ЗАПУСК БОТА И ВЕБ-СЕРВЕРА ---

async def main():
    app = web.Application()
    app["bot"] = bot
    
    app.router.add_get('/', index_handler)
    app.router.add_get(API_PATH, get_chat_info_api_handler)
    
    cors = aiohttp_cors.setup(app, defaults={"*": aiohttp_cors.ResourceOptions(
        allow_credentials=True, expose_headers="*", allow_headers="*", allow_methods="*"
    )})
    for route in list(app.router.routes()):
        cors.add(route)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_SERVER_HOST, int(WEB_SERVER_PORT))
    
    try:
        await site.start()
        logger.info(f"Веб-сервер запущен на http://{WEB_SERVER_HOST}:{WEB_SERVER_PORT}")
        me = await bot.get_me()
        logger.info(f"Бот @{me.username} запущен...")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await runner.cleanup()
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")

