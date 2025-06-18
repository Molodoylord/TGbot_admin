# Используем официальный образ Python
FROM python:3.11-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем файл зависимостей
COPY requirements.txt .

# Устанавливаем Python зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код приложения в рабочую директорию
COPY . .

# Создаем пользователя без привилегий для безопасности
RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app
USER app

# Открываем порт, на котором будет работать веб-сервер для Web App
EXPOSE 8080

# Команда для запуска приложения при старте контейнера
CMD ["python", "main.py"]

