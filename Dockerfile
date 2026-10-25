FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TIMEZONE=Europe/Moscow

WORKDIR /app

COPY requirements.txt requirements.lock.txt ./
RUN python -m pip install --no-cache-dir -r requirements.lock.txt \
    && python -m pip check

COPY main.py check_reminder.py ./
COPY pillbot/ ./pillbot/

RUN python -c "import aiogram; import apscheduler; import pillbot.main" \
    && mkdir -p /app/data

CMD ["python", "main.py"]
