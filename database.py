# database.py
import asyncpg
import logging

logger = logging.getLogger(__name__)

async def init_db(pool: asyncpg.Pool):
    """
    Инициализирует таблицы в базе данных, если они еще не созданы.
    """
    async with pool.acquire() as connection:
        # Таблица для отслеживания чатов, где бот является администратором
        await connection.execute("""
            CREATE TABLE IF NOT EXISTS managed_chats (
                chat_id BIGINT PRIMARY KEY,
                chat_title TEXT NOT NULL
            );
        """)
        # Таблица для хранения забаненных пользователей
        await connection.execute("""
            CREATE TABLE IF NOT EXISTS banned_users (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                ban_time TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                admin_id BIGINT,
                UNIQUE(chat_id, user_id) -- Уникальная пара чат-пользователь
            );
        """)
        logger.info("Таблицы базы данных успешно инициализированы.")

# --- Функции для управления чатами ---

async def add_chat(pool: asyncpg.Pool, chat_id: int, chat_title: str):
    """Добавляет чат в БД или обновляет его название."""
    sql = """
        INSERT INTO managed_chats (chat_id, chat_title)
        VALUES ($1, $2)
        ON CONFLICT (chat_id) DO UPDATE SET chat_title = $2;
    """
    async with pool.acquire() as connection:
        await connection.execute(sql, chat_id, chat_title)
    logger.info(f"Чат '{chat_title}' ({chat_id}) добавлен/обновлен в БД.")

async def remove_chat(pool: asyncpg.Pool, chat_id: int):
    """Удаляет чат из БД."""
    async with pool.acquire() as connection:
        await connection.execute("DELETE FROM managed_chats WHERE chat_id = $1", chat_id)
    logger.info(f"Чат {chat_id} удален из БД.")

async def get_managed_chats(pool: asyncpg.Pool) -> list[dict]:
    """Возвращает список всех управляемых чатов из БД."""
    async with pool.acquire() as connection:
        rows = await connection.fetch("SELECT chat_id, chat_title FROM managed_chats")
        return [dict(row) for row in rows]

# --- Функции для модерации (баны) ---

async def ban_user(pool: asyncpg.Pool, chat_id: int, user_id: int, admin_id: int):
    """Добавляет запись о бане пользователя в БД."""
    sql = """
        INSERT INTO banned_users (chat_id, user_id, admin_id)
        VALUES ($1, $2, $3)
        ON CONFLICT (chat_id, user_id) DO NOTHING;
    """
    async with pool.acquire() as connection:
        await connection.execute(sql, chat_id, user_id, admin_id)

async def unban_user(pool: asyncpg.Pool, chat_id: int, user_id: int):
    """Удаляет запись о бане пользователя из БД."""
    async with pool.acquire() as connection:
        await connection.execute("DELETE FROM banned_users WHERE chat_id = $1 AND user_id = $2", chat_id, user_id)

async def is_user_banned(pool: asyncpg.Pool, chat_id: int, user_id: int) -> bool:
    """Проверяет, забанен ли пользователь в чате."""
    async with pool.acquire() as connection:
        result = await connection.fetchval(
            "SELECT 1 FROM banned_users WHERE chat_id = $1 AND user_id = $2",
            chat_id, user_id
        )
        return result is not None

