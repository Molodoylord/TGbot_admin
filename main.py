# main.py
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
    ChatMemberUpdated, CallbackQuery, ChatPermissions, Update
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError
from aiohttp import web
from dotenv import load_dotenv
import aiohttp_cors

import database

# --- 1. НАСТРОЙКА ЛОГГИРОВАНИЯ ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(name)s] - %(message)s')
logger = logging.getLogger(__name__)
load_dotenv()

# --- 2. ЧТЕНИЕ ПЕРЕМЕННЫХ ---
BOT_TOKEN = getenv("BOT_TOKEN")
# URL, на котором доступно ваше веб-приложение (например, https://your-domain.com)
WEB_APP_URL = getenv("WEB_APP_URL")
# Хост и порт для запуска веб-сервера
WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = getenv("PORT", "8080")

# --- Настройки Webhook ---
# Путь для получения обновлений от Telegram. Делаем его "секретным" с помощью токена.
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
# Полный URL для вебхука.
WEBHOOK_URL = f"{WEB_APP_URL.rstrip('/')}{WEBHOOK_PATH}"

# Путь к API для получения информации о чате
API_PATH = "/api/chat_info"

# --- 3. ПОДКЛЮЧЕНИЕ К БАЗЕ ДАННЫХ ---
DB_USER = getenv("DB_USER")
DB_PASS = getenv("DB_PASSWORD")
DB_NAME = getenv("DB_NAME")
DB_HOST = getenv("DB_HOST")
DB_PORT = getenv("DB_PORT")

if all([DB_USER, DB_PASS, DB_NAME, DB_HOST, DB_PORT]):
    DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    logger.info("Строка подключения к БД успешно собрана.")
else:
    logger.critical("Не все переменные для подключения к БД установлены! Бот не может запуститься.")
    exit()

# --- 4. ИНИЦИАЛИЗАЦИЯ ---
dp = Dispatcher()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
# Словарь для хранения последних активных участников чата
chat_recent_members = {}
MAX_RECENT_MEMBERS_PER_CHAT = 100

# --- 5. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def is_valid_init_data(init_data: str, bot_token: str) -> bool:
    """Проверяет валидность данных, полученных от Telegram Web App."""
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
    """Проверяет, является ли пользователь админом в чате."""
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
    except TelegramAPIError:
        return False

# --- 6. ОБРАБОТЧИКИ СОБЫТИЙ БОТА (Handlers) ---
@dp.message(CommandStart(), F.chat.type == "private")
async def command_start_handler(message: Message):
    await message.answer(
        "Привет! Я бот для управления группами.\n\n"
        "1. Добавьте меня в свою группу.\n"
        "2. Дайте мне права администратора (обязательно с возможностью бана и ограничения участников).\n"
        "3. Используйте команду /admin здесь, чтобы получить доступ к панели управления."
    )

@dp.my_chat_member()
async def on_my_chat_member(update: ChatMemberUpdated, db_pool: asyncpg.Pool):
    chat_id, chat_title = update.chat.id, update.chat.title
    new_status = update.new_chat_member.status
    if new_status == ChatMemberStatus.ADMINISTRATOR:
        await database.add_chat(db_pool, chat_id, chat_title)
        me = await bot.get_me()
        can_ban = update.new_chat_member.can_ban_members
        can_restrict = update.new_chat_member.can_restrict_members
        status_text = f"✅ Панель для группы '{chat_title}' активна.\n"
        status_text += "Администраторы могут вызвать её командой /admin в личном чате со мной."
        if not can_ban or not can_restrict:
            status_text += "\n\n⚠️ <b>Внимание!</b> Мне не хватает разрешений:\n"
            if not can_ban: status_text += "- <b>Блокировка участников</b>\n"
            if not can_restrict: status_text += "- <b>Ограничение участников</b>\n"
            status_text += "Кнопки в панели управления могут не работать."
        keyboard = InlineKeyboardBuilder().button(text="🤖 Перейти к боту", url=f"https://t.me/{me.username}?start=group_admin")
        await bot.send_message(update.chat.id, status_text, reply_markup=keyboard.as_markup())
    elif new_status in [ChatMemberStatus.MEMBER, ChatMemberStatus.LEFT, ChatMemberStatus.KICKED]:
        await database.remove_chat(db_pool, chat_id)
        if chat_id in chat_recent_members: del chat_recent_members[chat_id]

@dp.message(Command("admin"), F.chat.type == "private")
async def command_admin_panel(message: Message, db_pool: asyncpg.Pool):
    user_id = message.from_user.id
    all_managed_chats = await database.get_managed_chats(db_pool)
    admin_in_chats = []
    check_tasks = [is_user_admin_in_chat(user_id, chat['chat_id']) for chat in all_managed_chats]
    results = await asyncio.gather(*check_tasks, return_exceptions=True)
    for i, res in enumerate(results):
        if isinstance(res, Exception):
            logger.warning(f"Не удалось проверить права для чата {all_managed_chats[i]['chat_id']}: {res}")
            continue
        if res:
            admin_in_chats.append(all_managed_chats[i])
    if not admin_in_chats:
        return await message.answer("Я не нашел групп, где вы являетесь администратором и я тоже добавлен с нужными правами.")
    builder = InlineKeyboardBuilder()
    for chat in admin_in_chats:
        builder.button(text=chat['chat_title'], callback_data=f"manage_chat_{chat['chat_id']}")
    builder.adjust(1)
    await message.answer("Выберите группу для управления:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("manage_chat_"))
async def select_chat_callback(query: CallbackQuery, db_pool: asyncpg.Pool):
    chat_id = int(query.data.split("_")[2])
    if not await is_user_admin_in_chat(user_id=query.from_user.id, chat_id=chat_id):
        return await query.answer("Доступ запрещен. Вы не администратор в этом чате.", show_alert=True)
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
    if chat_id not in chat_recent_members:
        chat_recent_members[chat_id] = OrderedDict()
    user_info = {"id": user.id, "first_name": user.first_name, "last_name": user.last_name or "", "username": user.username or ""}
    chat_recent_members[chat_id][user.id] = user_info
    chat_recent_members[chat_id].move_to_end(user.id)
    while len(chat_recent_members[chat_id]) > MAX_RECENT_MEMBERS_PER_CHAT:
        chat_recent_members[chat_id].popitem(last=False)

@dp.message(F.web_app_data)
async def web_app_data_handler(message: Message, db_pool: asyncpg.Pool):
    admin_id = message.from_user.id
    logger.info(f"--- ПОЛУЧЕН ЗАПРОС ОТ WEBAPP от {admin_id} ---")
    try:
        data = json.loads(message.web_app_data.data)
        action = data.get("action")
        user_id_to_moderate = data.get("user_id")
        chat_id_str = data.get("chat_id")
        if not all([action, user_id_to_moderate, chat_id_str]):
            raise ValueError(f"Неполные данные от WebApp: {data}")
        chat_id = int(chat_id_str)
        if not await is_user_admin_in_chat(user_id=admin_id, chat_id=chat_id):
            logger.warning(f"ОТКАЗ: Пользователь {admin_id} не является админом в чате {chat_id}.")
            return await message.answer("<b>Ошибка прав:</b> Вы не являетесь администратором в целевом чате.")
        
        user_to_moderate_info = await bot.get_chat(user_id_to_moderate)
        user_name = user_to_moderate_info.full_name
        user_mention = f"<a href='tg://user?id={user_id_to_moderate}'>{user_name}</a>"
        admin_mention = message.from_user.full_name

        try:
            if action == "ban":
                await bot.ban_chat_member(chat_id=chat_id, user_id=user_id_to_moderate)
                await database.ban_user(db_pool, chat_id, user_id_to_moderate, admin_id)
                await bot.send_message(chat_id, f"🚫 Администратор {admin_mention} забанил пользователя {user_mention}.")
                await message.answer(f"✅ Пользователь <b>{user_name}</b> успешно забанен.")
            elif action == "kick":
                await bot.ban_chat_member(chat_id=chat_id, user_id=user_id_to_moderate)
                await bot.unban_chat_member(chat_id=chat_id, user_id=user_id_to_moderate, only_if_banned=True)
                await bot.send_message(chat_id, f"👋 Администратор {admin_mention} исключил пользователя {user_mention}.")
                await message.answer(f"✅ Пользователь <b>{user_name}</b> успешно кикнут.")
            elif action == "warn":
                await bot.send_message(chat_id, f"⚠️ Администратор {admin_mention} вынес предупреждение пользователю {user_mention}. Пожалуйста, соблюдайте правила.")
                await message.answer(f"✅ Предупреждение пользователю <b>{user_name}</b> отправлено.")
            elif action == "mute":
                await bot.restrict_chat_member(
                    chat_id=chat_id, user_id=user_id_to_moderate,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=timedelta(hours=1)
                )
                await bot.send_message(chat_id, f"🔇 Администратор {admin_mention} ограничил возможность писать для {user_mention} на 1 час.")
                await message.answer(f"✅ Пользователь <b>{user_name}</b> был заглушен на 1 час.")
            else:
                raise ValueError(f"Неизвестное действие: `{action}`")
        except TelegramAPIError as e:
            logger.error(f"ОШИБКА API TELEGRAM при действии '{action}': {e.message}")
            await message.answer(
                f"❌ <b>Не удалось выполнить действие '{action}'.</b>\n"
                f"<b>Причина от Telegram:</b> {e.message}\n\n"
                f"<i>Пожалуйста, проверьте права бота в группе.</i>"
            )
    except Exception as e:
        logger.error(f"Критическая ошибка в web_app_data_handler: {e}", exc_info=True)
        await message.answer(f"❌ Произошла внутренняя ошибка сервера: {e}")
    finally:
        logger.info("--- Обработка запроса от WebApp завершена ---")

# --- 7. API ДЛЯ WEB APP (без изменений) ---
async def get_chat_info_api_handler(request: web.Request):
    db_pool = request.app['db_pool']
    bot_from_app = request.app["bot"]
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("tma "):
        return web.json_response({"error": "Требуется авторизация (initData)"}, status=401)
    init_data = auth_header.split(" ", 1)[1]
    if not is_valid_init_data(init_data, bot_from_app.token):
        return web.json_response({"error": "Неверные данные авторизации (initData)"}, status=403)
    try:
        chat_id = int(request.query.get("chat_id"))
        query_params = dict(parse_qsl(unquote(init_data)))
        user_info = json.loads(query_params.get("user", "{}"))
        user_id = user_info.get("id")
        if not user_id or not await is_user_admin_in_chat(user_id=user_id, chat_id=chat_id):
            return web.json_response({"error": "Доступ только для администраторов чата"}, status=403)
        
        chat_info_db = await db_pool.fetchrow("SELECT chat_title FROM managed_chats WHERE chat_id = $1", chat_id)
        if not chat_info_db: return web.json_response({"error": "Бот не управляет этим чатом"}, status=404)
        
        all_members = OrderedDict()
        admins = await bot_from_app.get_chat_administrators(chat_id)
        for admin in admins:
            if admin.user.is_bot: continue
            all_members[admin.user.id] = {"id": admin.user.id, "first_name": admin.user.first_name, "last_name": admin.user.last_name or "", "username": admin.user.username or ""}
        
        if chat_id in chat_recent_members:
            for recent_user_id, recent_user_info in reversed(chat_recent_members[chat_id].items()):
                if recent_user_id not in all_members: all_members[recent_user_id] = recent_user_info
        
        final_list = []
        for user_id_key, user in all_members.items():
            user['is_banned'] = await database.is_user_banned(db_pool, chat_id, user['id'])
            # Получаем свежий URL фото
            try:
                user_profile_photos = await bot.get_user_profile_photos(user_id_key, limit=1)
                if user_profile_photos.photos:
                    file = await bot.get_file(user_profile_photos.photos[0][-1].file_id)
                    user['photo_url'] = f"https://api.telegram.org/file/bot{bot.token}/{file.file_path}"
                else:
                    user['photo_url'] = None
            except Exception:
                user['photo_url'] = None

            final_list.append(user)
        
        return web.json_response({"chat_title": chat_info_db['chat_title'], "members": final_list})
    except Exception as e:
        logger.error(f"Ошибка API: {e}", exc_info=True)
        return web.json_response({"error": "Внутренняя ошибка сервера"}, status=500)

async def index_handler(request: web.Request):
    index_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html')
    try:
        return web.FileResponse(index_path)
    except FileNotFoundError:
        logger.error(f"Файл index.html не найден по пути: {index_path}")
        return web.Response(text='index.html not found', status=404)

# --- ИСПРАВЛЕНИЕ: Обработчик для Webhook ---
async def webhook_route_handler(request: web.Request):
    """
    Принимает обновления от Telegram и КОРРЕКТНО передает их в диспетчер.
    """
    try:
        # Получаем пул соединений из контекста приложения aiohttp
        db_pool = request.app['db_pool']
        
        # Получаем данные, создаем объект Update
        update_data = await request.json()
        logger.info(f"ПОЛУЧЕН WEBHOOK: {update_data}")
        update = Update.model_validate(update_data, context={"bot": bot})
        
        # **КЛЮЧЕВОЕ ИЗМЕНЕНИЕ**: Передаем и update, и db_pool в диспетчер
        # Теперь aiogram сможет передать db_pool в нужные хэндлеры
        await dp.feed_update(update, db_pool=db_pool)
        
        return web.Response()
    except Exception as e:
        logger.error(f"Ошибка в обработчике webhook: {e}", exc_info=True)
        return web.Response(status=500)

# --- 8. ЗАПУСК БОТА И ВЕБ-СЕРВЕРА ---
async def on_startup(app: web.Application):
    """Действия при старте приложения."""
    logger.info("Установка webhook...")
    await bot.set_webhook(
        url=WEBHOOK_URL,
        allowed_updates=dp.resolve_used_update_types(),
        drop_pending_updates=True
    )
    logger.info(f"Webhook успешно установлен на URL: {WEBHOOK_URL}")

async def on_shutdown(app: web.Application):
    """Действия при остановке приложения."""
    logger.info("Остановка приложения... Удаление webhook.")
    await bot.delete_webhook()
    
    db_pool = app.get('db_pool')
    if db_pool:
        await db_pool.close()
    logger.info("Пул подключений к БД закрыт.")
    await bot.session.close()
    logger.info("Сессия бота закрыта.")


async def main():
    if not all([BOT_TOKEN, DATABASE_URL, WEB_APP_URL]):
        logger.critical("Одна из критически важных переменных не найдена (BOT_TOKEN, DATABASE_URL, WEB_APP_URL)! Завершение работы.")
        return

    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL)
        await database.init_db(db_pool)
        logger.info("Пул подключений к БД успешно создан и таблицы инициализированы.")
    except Exception as e:
        logger.critical(f"Не удалось подключиться к базе данных: {e}", exc_info=True)
        exit(1)
    
    app = web.Application()
    app["bot"] = bot
    app["db_pool"] = db_pool
    
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    app.router.add_get('/', index_handler)
    app.router.add_get(API_PATH, get_chat_info_api_handler)
    app.router.add_post(WEBHOOK_PATH, webhook_route_handler)

    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True, 
            expose_headers="*", 
            allow_headers="*", 
            allow_methods="*"
        )
    })
    for route in list(app.router.routes()):
        cors.add(route)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_SERVER_HOST, int(WEB_SERVER_PORT))
    
    logger.info(f"Веб-сервер готовится к запуску на http://{WEB_SERVER_HOST}:{WEB_SERVER_PORT}")
    await site.start()
    logger.info("Приложение успешно запущено.")
    
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен вручную.")


