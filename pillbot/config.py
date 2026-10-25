from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    token: str
    timezone: ZoneInfo
    database: Path


def load_settings() -> Settings:
    load_dotenv(ROOT / '.env')
    token = os.getenv('BOT_TOKEN', '').strip()
    if not token or token == 'your_telegram_bot_token':
        raise ValueError('Укажи BOT_TOKEN в .env')
    zone = os.getenv('TIMEZONE', 'Europe/Moscow').strip()
    if zone != 'Europe/Moscow':
        raise ValueError('TIMEZONE должен быть Europe/Moscow')
    return Settings(token, ZoneInfo(zone), ROOT / 'data' / 'bot.sqlite3')


class SafeFormatter(logging.Formatter):
    def format(self, record):
        result = super().format(record)
        return re.sub(r'\b\d{6,}:[A-Za-z0-9_-]{20,}\b', '[BOT_TOKEN]', result)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(SafeFormatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
