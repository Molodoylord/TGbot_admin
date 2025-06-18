import logging
import os
import asyncio
from aiohttp import web
from dotenv import load_dotenv

# --- 1. Базовая настройка ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(name)s] - %(message)s')
logger = logging.getLogger(__name__)
load_dotenv()

# --- 2. Переменные окружения ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = os.getenv("PORT", "8080")
WEB_SERVER_HOST = "0.0.0.0"

# Проверяем, что токен есть
if not BOT_TOKEN:
    logger.critical("BOT_TOKEN не найден в переменных окружения!")
    exit()

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"

# --- 3. Обработчик, который ловит ВСЕ ---
async def diagnostic_webhook_handler(request: web.Request):
    """
    Этот обработчик принимает любой POST запрос на наш webhook path
    и максимально подробно логирует всё, что получает.
    """
    try:
        # Главный маркер, который мы ищем в логах
        logger.info("--- [ДИАГНОСТИЧЕСКИЙ СЕРВЕР] ПОЛУЧЕН ЗАПРОС ---")
        
        # Логируем метод и путь
        logger.info(f"Method: {request.method}")
        logger.info(f"Path: {request.path}")
        
        # Логируем все заголовки запроса
        headers_str = "\n".join(f"  {k}: {v}" for k, v in request.headers.items())
        logger.info(f"Headers:\n{headers_str}")
        
        # Логируем "сырое" тело запроса
        body = await request.text()
        logger.info(f"Raw Body:\n{body}")
        
        # Отвечаем Telegram, что все в порядке, чтобы он не слал запрос повторно
        return web.Response(text="OK", status=200)

    except Exception as e:
        logger.error(f"Критическая ошибка в обработчике: {e}", exc_info=True)
        return web.Response(status=500)

# --- 4. Запуск веб-сервера ---
async def main():
    app = web.Application()
    # Регистрируем наш единственный обработчик
    app.router.add_post(WEBHOOK_PATH, diagnostic_webhook_handler)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_SERVER_HOST, int(PORT))
    
    logger.info("--- ДИАГНОСТИЧЕСКИЙ СЕРВЕР ЗАПУЩЕН ---")
    logger.info(f"Сервер слушает http://{WEB_SERVER_HOST}:{PORT}")
    logger.info(f"Путь для вебхука: {WEBHOOK_PATH}")
    
    await site.start()
    # Бесконечно ждем запросов
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Диагностический сервер остановлен.")
