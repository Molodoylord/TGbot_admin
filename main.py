# main.py (Финальная версия с Polling и Web-сервером)
import asyncio
import logging
import json
import hmac
import hashlib
import os
from os import getenv
from urllib.parse import unquote, parse_qsl
from collections import OrderedDict
from datetime import timedelta
import asyncpg

from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton,
    ChatMemberUpdated, CallbackQuery, ChatPermissions
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError

from aiohttp import web
from dotenv import load_dotenv
import aiohttp_cors

import database

# --- 1. НАСТРОЙКА ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(name)s] - %(message)s')
logger = logging.getLogger(__name__)
load_dotenv()

# --- 2. ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ---
BOT_TOKEN = getenv("BOT_TOKEN")
WEB_APP_URL = getenv("WEB_APP_URL")
# Переменные для веб-сервера
WEB_SERVER_HOST = getenv("WEB_SERVER_HOST", "0.0.0.0")
WEB_SERVER_PORT = getenv("PORT", "8080")
# Переменные для БД
DB_USER, DB_PASS, DB_NAME, DB_HOST, DB_PORT = getenv("DB_USER"), getenv("DB_PASSWORD"), getenv("DB_NAME"), getenv("DB_HOST"), getenv("DB_PORT")
DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# --- 3. ИНИЦИАЛИЗАЦИЯ ---
dp = Dispatcher()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
chat_recent_members = {} # Кэш недавних участников
MAX_RECENT_MEMBERS_PER_CHAT = 100

# --- 4. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def is_valid_init_data(init_data: str, bot_token: str) -> bool:
    try:
        parsed_data = dict(parse_qsl(unquote(init_data), keep_blank_values=True))
        received_hash = parsed_data.pop("hash")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new("WebAppData".encode(), bot_token.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        return calculated_hash == received_hash
    except Exception:
        return False

async def is_user_admin_in_chat(user_id: int, chat_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
    except TelegramAPIError:
        return False

# --- 5. ОБРАБОТЧИКИ КОМАНД И СОБЫТИЙ БОТА ---

@dp.message(CommandStart(), F.chat.type == "private")
async def command_start_handler(message: Message):
    await message.answer(
        "Привет! Я бот для управления группами.\n\n"
        "1. Добавьте меня в свою группу.\n"
        "2. Дайте мне права администратора.\n"
        "3. Используйте команду /admin в этом чате, чтобы получить доступ к панели."
    )

@dp.my_chat_member()
async def on_my_chat_member(update: ChatMemberUpdated, db_pool: asyncpg.Pool):
    chat_id, chat_title = update.chat.id, update.chat.title
    new_status = update.new_chat_member.status
    if new_status == ChatMemberStatus.ADMINISTRATOR:
        await database.add_chat(db_pool, chat_id, chat_title)
        me = await bot.get_me()
        status_text = f"✅ Панель для группы '{chat_title}' активна."
        keyboard = InlineKeyboardBuilder().button(text="🤖 Перейти к боту", url=f"https://t.me/{me.username}?start=group_admin")
        try:
            await bot.send_message(update.chat.id, status_text, reply_markup=keyboard.as_markup())
        except TelegramAPIError as e:
            logger.error(f"Не удалось отправить сообщение в чат {chat_id}: {e}")
    elif new_status in [ChatMemberStatus.MEMBER, ChatMemberStatus.LEFT, ChatMemberStatus.KICKED]:
        await database.remove_chat(db_pool, chat_id)
        if chat_id in chat_recent_members: del chat_recent_members[chat_id]

@dp.message(Command("admin"), F.chat.type == "private")
async def command_admin_panel(message: Message, db_pool: asyncpg.Pool):
    user_id = message.from_user.id
    all_managed_chats = await database.get_managed_chats(db_pool)
    admin_in_chats = [chat for chat in all_managed_chats if await is_user_admin_in_chat(user_id, chat['chat_id'])]
    if not admin_in_chats:
        return await message.answer("Я не нашел групп, где вы администратор. Убедитесь, что добавили меня в группу и выдали права.")
    builder = InlineKeyboardBuilder()
    for chat in admin_in_chats:
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

@dp.message(F.chat.type.in_(['group', 'supergroup']), ~F.text.startswith('/'))
async def remember_member_handler(message: Message):
    chat_id = message.chat.id
    user = message.from_user
    if user.is_bot: return
    if chat_id not in chat_recent_members: chat_recent_members[chat_id] = OrderedDict()
    user_info = {"id": user.id, "first_name": user.first_name, "last_name": user.last_name or "", "username": user.username or ""}
    chat_recent_members[chat_id][user.id] = user_info
    if len(chat_recent_members[chat_id]) > MAX_RECENT_MEMBERS_PER_CHAT:
        chat_recent_members[chat_id].popitem(last=False)

@dp.message(F.web_app_data)
async def web_app_data_handler(message: Message, db_pool: asyncpg.Pool):
    logger.info(f"Получены данные от WebApp: {message.web_app_data.data}")
    try:
        data = json.loads(message.web_app_data.data)
        action, user_id, chat_id_str = data.get("action"), data.get("user_id"), data.get("chat_id")
        
        if not all([action, user_id, chat_id_str]):
            return await message.answer("Ошибка: получены неполные данные.")
        
        chat_id = int(chat_id_str)
        admin_id = message.from_user.id

        if not await is_user_admin_in_chat(user_id=admin_id, chat_id=chat_id):
            return await message.answer("<b>Ошибка прав:</b> Вы не администратор в целевом чате.")

        user_info = await bot.get_chat(user_id)
        user_mention = f"<a href='tg://user?id={user_id}'>{user_info.full_name}</a>"
        admin_mention = message.from_user.full_name

        if action == "ban":
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            await database.ban_user(db_pool, chat_id, user_id, admin_id)
            await bot.send_message(chat_id, f"🚫 Администратор {admin_mention} забанил пользователя {user_mention}.")
            await message.answer(f"✅ Пользователь <b>{user_info.full_name}</b> успешно забанен.")
        elif action == "kick":
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            await bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)
            await bot.send_message(chat_id, f"👋 Администратор {admin_mention} исключил пользователя {user_mention}.")
            await message.answer(f"✅ Пользователь <b>{user_info.full_name}</b> успешно кикнут.")
        elif action == "mute":
            await bot.restrict_chat_member(
                chat_id=chat_id, user_id=user_id,
                permissions=types.ChatPermissions(can_send_messages=False),
                until_date=timedelta(hours=1)
            )
            await bot.send_message(chat_id, f"🔇 Администратор {admin_mention} ограничил возможность писать для {user_mention} на 1 час.")
            await message.answer(f"✅ Пользователь <b>{user_info.full_name}</b> был заглушен на 1 час.")
        elif action == "warn":
            await bot.send_message(chat_id, f"⚠️ Администратор {admin_mention} вынес предупреждение {user_mention}.")
            await message.answer(f"✅ Предупреждение пользователю <b>{user_info.full_name}</b> отправлено.")
        else:
            await message.answer(f"Ошибка: неизвестное действие '{action}'.")

    except TelegramAPIError as e:
        logger.error(f"Ошибка API Telegram: {e.message}", exc_info=True)
        await message.answer(f"❌ <b>Не удалось выполнить:</b>\n{e.message}\n<i>Убедитесь, что у бота есть права.</i>")
    except Exception as e:
        logger.error(f"Непредвиденная ошибка в web_app_data_handler: {e}", exc_info=True)
        await message.answer("❌ Произошла внутренняя ошибка сервера.")

# --- 6. API ДЛЯ WEB APP (остается без изменений) ---
async def index_handler(request: web.Request):
    return web.FileResponse(os.path.join(os.path.dirname(__file__), 'index.html'))

async def get_chat_info_api_handler(request: web.Request):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("tma ") or not is_valid_init_data(auth_header.split(" ", 1)[1], bot.token):
        return web.json_response({"error": "Требуется авторизация"}, status=401)
    
    try:
        chat_id = int(request.query.get("chat_id"))
        user_info = json.loads(dict(parse_qsl(unquote(auth_header.split(" ", 1)[1]))).get("user", "{}"))
        if not user_info.get("id") or not await is_user_admin_in_chat(user_id=user_info.get("id"), chat_id=chat_id):
            return web.json_response({"error": "Доступ только для администраторов"}, status=403)

        db_pool = request.app['db_pool']
        chat_info_db = await db_pool.fetchrow("SELECT chat_title FROM managed_chats WHERE chat_id = $1", chat_id)
        if not chat_info_db: return web.json_response({"error": "Бот не управляет этим чатом"}, status=404)

        all_members = OrderedDict()
        admins = await bot.get_chat_administrators(chat_id)
        for admin in filter(lambda m: not m.user.is_bot, admins):
            all_members[admin.user.id] = {"id": admin.user.id, "first_name": admin.user.first_name, "last_name": admin.user.last_name or "", "username": admin.user.username or "", "status": admin.status.name.lower()}
        
        if chat_id in chat_recent_members:
            for uid, uinfo in reversed(chat_recent_members[chat_id].items()):
                if uid not in all_members: all_members[uid] = uinfo | {'status': 'member'}
        
        final_list = []
        for user_id_key, user_data in all_members.items():
            user_data['is_banned'] = await database.is_user_banned(db_pool, chat_id, user_data['id'])
            try:
                photos = await bot.get_user_profile_photos(user_id_key, limit=1)
                user_data['photo_url'] = (await bot.get_file(photos.photos[0][-1].file_id)).file_path if photos.photos else None
                if user_data['photo_url']: user_data['photo_url'] = f"https://api.telegram.org/file/bot{bot.token}/{user_data['photo_url']}"
            except Exception: user_data['photo_url'] = None
            final_list.append(user_data)
            
        return web.json_response({"chat_title": chat_info_db['chat_title'], "members": final_list})
    except Exception as e:
        logger.error(f"Ошибка API: {e}", exc_info=True)
        return web.json_response({"error": "Внутренняя ошибка сервера"}, status=500)

# --- 7. ФУНКЦИИ ЗАПУСКА ---
async def start_bot(db_pool):
    """Запускает опрос обновлений от Telegram."""
    dp.workflow_data['db_pool'] = db_pool # Передаем пул соединений в обработчики
    await bot.delete_webhook(drop_pending_updates=True) # На всякий случай удаляем старый вебхук
    logger.info("Запуск бота в режиме Polling...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

async def start_web_server(db_pool):
    """Запускает веб-сервер для Web App."""
    app = web.Application()
    app['db_pool'] = db_pool # Передаем пул соединений в API
    app.router.add_get('/', index_handler)
    app.router.add_get('/api/chat_info', get_chat_info_api_handler)
    
    # Настройка CORS
    cors = aiohttp_cors.setup(app, defaults={"*": aiohttp_cors.ResourceOptions(allow_credentials=True, expose_headers="*", allow_headers="*")})
    for route in list(app.router.routes()): cors.add(route)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_SERVER_HOST, int(WEB_SERVER_PORT))
    logger.info(f"Веб-сервер запущен на http://{WEB_SERVER_HOST}:{WEB_SERVER_PORT}")
    await site.start()
    await asyncio.Event().wait() # Сервер будет работать вечно

async def main():
    if not all([BOT_TOKEN, WEB_APP_URL, DATABASE_URL]):
        return logger.critical("КРИТИЧЕСКАЯ ОШИБКА: Не установлены переменные BOT_TOKEN, WEB_APP_URL или для БД!")

    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL)
        await database.init_db(db_pool)
    except Exception as e:
        return logger.critical(f"Не удалось подключиться к БД: {e}", exc_info=True)
    
    # Запускаем бота и веб-сервер параллельно
    await asyncio.gather(
        start_bot(db_pool),
        start_web_server(db_pool)
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")

