import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, ErrorEvent, Message

from .config import configure_logging, load_settings
from .service import ReminderService, telegram_call
from .storage import Store
from .scheduler import ReminderScheduler
from .setup import SetupFlow

log = logging.getLogger(__name__)


def make_dispatcher(service: ReminderService, schedule: ReminderScheduler) -> Dispatcher:
    dp = Dispatcher()
    setup = SetupFlow(service.store, service.timezone, schedule.refresh)

    @dp.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
    async def start(message: Message):
        await setup.start(message)

    @dp.message(CommandStart())
    async def start_in_group(message: Message):
        await telegram_call(message.answer, text='Отправь /start в личном чате со мной.')

    @dp.message(F.chat.type == ChatType.PRIVATE, F.text)
    async def setup_input(message: Message):
        await setup.handle(message)

    @dp.callback_query(F.data.startswith('taken:'))
    async def taken(callback: CallbackQuery):
        await service.confirm(callback)

    @dp.errors()
    async def error(event: ErrorEvent):
        log.error('Update processing failed: %s', type(event.exception).__name__,
                  exc_info=(type(event.exception), event.exception, event.exception.__traceback__))
        return True

    return dp


async def run() -> None:
    settings = load_settings()
    store = Store(settings.database)
    async with Bot(settings.token) as bot:
        me = await bot.get_me()
        webhook = await bot.get_webhook_info()
        if webhook.url:
            raise RuntimeError('У бота установлен webhook. Отключи прежний сервис и webhook перед polling.')
        service = ReminderService(bot, store, settings.timezone)
        schedule = ReminderScheduler(service)
        dp = make_dispatcher(service, schedule)
        schedule.restore()
        schedule.scheduler.start()
        try:
            log.info('Bot @%s started. Personal reminders: Europe/Moscow', me.username)
            await schedule.catch_up()
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(),
                                   close_bot_session=False, handle_as_tasks=False)
        finally:
            schedule.scheduler.shutdown(wait=False)
            await asyncio.sleep(0)  # Let AsyncIOScheduler finish its shutdown callback.


def main() -> None:
    configure_logging()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception('Bot stopped with an error')
        raise SystemExit(1) from None
