# Используем официальный образ Python
FROM python:3.11-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем файл зависимостей
COPY requirements.txt .

# Устанавливаем Python зависимости
# --no-cache-dir чтобы не хранить кэш и уменьшить размер образа
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код приложения в рабочую директорию
COPY main.py .
COPY database.py .
COPY index.html .

# Создаем пользователя без привилегий для безопасности
RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app
USER app

# Открываем порт, на котором будет работать веб-сервер
EXPOSE 8080

# Устанавливаем переменные окружения по умолчанию. 
# Они могут быть переопределены при запуске контейнера.
ENV PORT=8080
ENV WEB_SERVER_HOST=0.0.0.0

# Команда для запуска приложения при старте контейнера
CMD ["python", "main.py"]

