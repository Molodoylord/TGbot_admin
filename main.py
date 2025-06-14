import asyncio
import logging
import json
import hmac
import hashlib
import os
from os import getenv
from urllib.parse import urljoin, parse_qsl

from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import CommandStart, Command, Filter
from aiogram.types import Message, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton, ChatMemberUpdated, \
    CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError

from aiohttp import web
from dotenv import load_dotenv
import aiohttp_cors

# --- НАСТРОЙКА ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(name)s] - %(message)s')
logger = logging.getLogger(__name__)
load_dotenv()

# --- ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ---
BOT_TOKEN = getenv("BOT_TOKEN")
WEB_APP_URL = getenv("WEB_APP_URL")
BASE_WEBHOOK_URL = getenv("BASE_WEBHOOK_URL")
WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = int(getenv("WEB_SERVER_PORT", 8080))
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_SECRET = getenv("WEBHOOK_SECRET", "your-super-secret-string")
ADMIN_ID = getenv("ADMIN_ID")
CHATS_DATA_FILE = "managed_chats.json"  # Файл для хранения чатов

if not all([BOT_TOKEN, WEB_APP_URL, BASE_WEBHOOK_URL, ADMIN_ID]):
    logger.critical("КРИТИЧЕСКАЯ ОШИБКА: Не все переменные .env загружены!")
    exit()

ADMIN_ID = int(ADMIN_ID)


# --- УПРАВЛЕНИЕ ЧАТАМИ (С СОХРАНЕНИЕМ В ФАЙЛ) ---

def load_managed_chats() -> dict:
    """Загружает управляемые чаты из файла JSON."""
    if not os.path.exists(CHATS_DATA_FILE):
        return {}
    try:
        with open(CHATS_DATA_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            # JSON хранит ключи как строки, конвертируем их обратно в int
            return {int(k): v for k, v in data.items()}
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Не удалось загрузить файл с чатами ({CHATS_DATA_FILE}): {e}")
        return {}


def save_managed_chats(chats: dict):
    """Сохраняет управляемые чаты в файл JSON."""
    try:
        with open(CHATS_DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(chats, f, ensure_ascii=False, indent=4)
    except IOError as e:
        logger.error(f"Не удалось сохранить файл с чатами ({CHATS_DATA_FILE}): {e}")


# Загружаем чаты при старте
managed_chats = load_managed_chats()
logger.info(f"Загружено {len(managed_chats)} управляемых чатов из файла.")

dp = Dispatcher()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


# --- ФУНКЦИИ БЕЗОПАСНОСТИ ---
def is_valid_init_data(init_data: str, bot_token: str) -> bool:
    """Проверяет валидность initData, полученных из Telegram Web App."""
    try:
        parsed_data = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed_data.pop("hash")
        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(parsed_data.items())
        )
        secret_key = hmac.new("WebAppData".encode(), bot_token.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        return calculated_hash == received_hash
    except Exception as e:
        logger.error(f"Ошибка валидации initData: {e}")
        return False


# --- ФИЛЬТР АДМИНИСТРАТОРА ---
class AdminFilter(Filter):
    async def __call__(self, message: types.Message | types.CallbackQuery) -> bool:
        return message.from_user.id == ADMIN_ID


# --- ОБРАБОТЧИКИ БОТА ---
@dp.my_chat_member()
async def on_my_chat_member(update: ChatMemberUpdated):
    global managed_chats
    chat_id = update.chat.id
    chat_title = update.chat.title

    # Бот добавлен как администратор
    if update.new_chat_member.status == ChatMemberStatus.ADMINISTRATOR:
        if chat_id not in managed_chats:
            managed_chats[chat_id] = chat_title
            save_managed_chats(managed_chats)  # Сохраняем изменения в файл
            logger.info(f"Бот назначен администратором в чате '{chat_title}' (ID: {chat_id}). Список обновлен.")
            try:
                await bot.send_message(ADMIN_ID, f"✅ Я теперь администратор в группе: <b>{chat_title}</b>.")
            except TelegramAPIError as e:
                logger.warning(f"Не удалось отправить уведомление админу: {e}")

    # Бот удален или лишен прав
    elif update.new_chat_member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.LEFT, ChatMemberStatus.KICKED]:
        if chat_id in managed_chats:
            removed_chat_title = managed_chats.pop(chat_id)
            save_managed_chats(managed_chats)  # Сохраняем изменения в файл
            logger.info(f"Бот удален или лишен прав в чате '{removed_chat_title}'. Список обновлен.")
            try:
                await bot.send_message(ADMIN_ID, f"❌ Я больше не администратор в группе: <b>{removed_chat_title}</b>.")
            except TelegramAPIError as e:
                logger.warning(f"Не удалось отправить уведомление админу: {e}")


@dp.message(CommandStart(), AdminFilter())
async def command_start_admin(message: Message):
    await message.answer("👋 Привет, администратор! Используйте /admin для управления группами.")


@dp.message(Command("admin"), AdminFilter())
async def command_admin_panel(message: Message):
    if not managed_chats:
        await message.answer(
            "Я пока не управляю ни одной группой. Сделайте меня администратором в нужной группе, и я ее запомню.")
        return
    builder = InlineKeyboardBuilder()
    for chat_id, chat_title in managed_chats.items():
        builder.button(text=chat_title, callback_data=f"manage_chat_{chat_id}")
    builder.adjust(1)
    await message.answer("Выберите группу для управления:", reply_markup=builder.as_markup())


@dp.callback_query(F.data.startswith("manage_chat_"), AdminFilter())
async def select_chat_callback(query: CallbackQuery):
    chat_id_str = query.data.split("_")[2]
    chat_id = int(chat_id_str)
    chat_title = managed_chats.get(chat_id, "Неизвестный чат")
    url = f"{WEB_APP_URL.rstrip('/')}?chat_id={chat_id}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"🚀 Открыть панель для '{chat_title}'", web_app=WebAppInfo(url=url))
    ]])
    await query.message.answer(f"Управление группой <b>{chat_title}</b>.", reply_markup=keyboard)
    await query.answer()


@dp.message(F.web_app_data)
async def web_app_data_handler(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        data = json.loads(message.web_app_data.data)
        action = data.get("action")
        chat_id = int(data.get("chat_id"))
        user_id = int(data.get("user_id"))
        if action == 'kick':
            await bot.kick_chat_member(chat_id=chat_id, user_id=user_id)
            await message.answer(f"✅ Пользователь {user_id} был кикнут из чата.")
        # Добавьте другие действия (ban, mute...) по аналогии
    except Exception as e:
        logger.error(f"Ошибка обработки данных из Web App: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка.")


# --- API ДЛЯ WEB APP ---
async def get_chat_info_handler(request: web.Request):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("tma "):
        return web.json_response({"error": "Authorization required"}, status=401)

    init_data = auth_header.split(" ", 1)[1]
    if not is_valid_init_data(init_data, BOT_TOKEN):
        return web.json_response({"error": "Invalid initData"}, status=403)

    user_info = json.loads(dict(parse_qsl(init_data)).get("user", "{}"))
    if not user_info or user_info.get("id") != ADMIN_ID:
        return web.json_response({"error": "Admin access required"}, status=403)

    chat_id_str = request.query.get("chat_id")
    if not chat_id_str:
        return web.json_response({"error": "chat_id is required"}, status=400)

    try:
        chat_id = int(chat_id_str)
        chat_info = await bot.get_chat(chat_id)
        admins = await bot.get_chat_administrators(chat_id)
        members = [
            {"id": admin.user.id, "name": admin.user.full_name, "username": admin.user.username or ""}
            for admin in admins
        ]
        return web.json_response({
            "chat_title": chat_info.title,
            "members": members
        })
    except Exception as e:
        logger.error(f"Ошибка получения информации о чате {chat_id_str}: {e}")
        return web.json_response({"error": "Internal server error"}, status=500)


# --- ЗАПУСК ВЕБ-СЕРВЕРА И БОТА ---
async def aiohttp_webhook_handler(request: web.Request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return web.Response(status=403)

    update = types.Update.model_validate_json(await request.text(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return web.Response()


async def on_startup(bot_instance: Bot):
    webhook_url = urljoin(BASE_WEBHOOK_URL.rstrip('/'), WEBHOOK_PATH)
    await bot_instance.set_webhook(url=webhook_url, secret_token=WEBHOOK_SECRET, drop_pending_updates=True)
    logger.info(f"Вебхук установлен: {webhook_url}")
    logger.info(f"URL веб-приложения: {WEB_APP_URL}")


async def on_shutdown(bot_instance: Bot):
    await bot_instance.delete_webhook()
    logger.info("Вебхук удален.")


async def main():
    app = web.Application()
    app['bot'] = bot

    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True, expose_headers="*", allow_headers="*", allow_methods="*"
        )
    })

    app.router.add_post(WEBHOOK_PATH, aiohttp_webhook_handler)
    api_route = app.router.add_get("/api/chat_info", get_chat_info_handler)
    cors.add(api_route)

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_SERVER_HOST, WEB_SERVER_PORT)
    await site.start()

    logger.info(f"Сервер запущен на http://{WEB_SERVER_HOST}:{WEB_SERVER_PORT}")
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")