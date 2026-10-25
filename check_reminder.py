"""Send today's real reminder now; uses the same daily duplicate protection."""
import asyncio
import logging

from aiogram import Bot

from pillbot.config import configure_logging, load_settings
from pillbot.service import ReminderService
from pillbot.storage import Store


async def run():
    settings = load_settings()
    store = Store(settings.database)
    if not store.configured_profiles():
        raise RuntimeError('Сначала запусти main.py и отправь /start и укажи дату первого приёма и время.')
    async with Bot(settings.token) as bot:
        await ReminderService(bot, store, settings.timezone).send_daily(force=True)
    logging.info('Проверка завершена. Если запись за сегодня уже есть, повторной отправки не будет.')


if __name__ == '__main__':
    configure_logging()
    try:
        asyncio.run(run())
    except Exception:
        logging.exception('Проверка не завершена')
        raise SystemExit(1) from None
