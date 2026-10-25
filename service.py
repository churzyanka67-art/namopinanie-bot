import asyncio
from datetime import datetime, time, timedelta
import logging
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from .storage import Store

log = logging.getLogger(__name__)


async def telegram_call(operation, *, retry_until: datetime | None = None, **kwargs):
    """Retry only explicit flood-control rejections, never ambiguous sends."""
    attempt = 0
    while True:
        try:
            return await operation(**kwargs)
        except TelegramRetryAfter as exc:
            log.warning('Telegram rate limit: retry_after=%s', exc.retry_after)
            attempt += 1
            if retry_until is None:
                if attempt >= 3:
                    raise
            elif datetime.now(retry_until.tzinfo) + timedelta(seconds=exc.retry_after) >= retry_until:
                raise  # Do not deliver yesterday's reminder after midnight.
            await asyncio.sleep(exc.retry_after)


class ReminderService:
    def __init__(self, bot: Bot, store: Store, timezone: ZoneInfo):
        self.bot, self.store, self.timezone = bot, store, timezone

    async def send_daily(self, now: datetime | None = None, *, chat_id: int | None = None,
                         force: bool = False) -> None:
        local_now = (now or datetime.now(self.timezone)).astimezone(self.timezone)
        day = local_now.date().isoformat()
        deadline = datetime.combine(local_now.date() + timedelta(days=1), time(), self.timezone)
        for profile in self.store.configured_profiles():
            if chat_id is not None and profile['chat_id'] != chat_id:
                continue
            if not force and profile['active_from'] > day:
                continue
            await self.send_one(profile['chat_id'], day, deadline)

    async def send_one(self, chat_id: int, day: str, deadline: datetime) -> None:
        rid = self.store.reserve(chat_id, day)
        if rid is None:
            return
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='✅ Я выпила', callback_data=f'taken:{rid}')
        ]])
        try:
            message = await telegram_call(self.bot.send_message, chat_id=chat_id,
                text='💊 Время выпить таблетку!', reply_markup=keyboard, retry_until=deadline)
        except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter) as exc:
            self.store.mark(rid, 'failed')
            log.error('Reminder rejected: chat=%s day=%s error=%s', chat_id, day, type(exc).__name__)
        except TelegramAPIError as exc:
            self.store.mark(rid, 'uncertain')
            log.error('Delivery uncertain; no resend: chat=%s day=%s error=%s',
                      chat_id, day, type(exc).__name__)
        else:
            self.store.mark(rid, 'sent', message.message_id)
            log.info('Reminder sent: chat=%s day=%s message=%s', chat_id, day, message.message_id)

    async def answer(self, callback: CallbackQuery, text: str | None = None) -> None:
        try:
            await callback.answer(text=text)
        except TelegramAPIError as exc:
            log.warning('Cannot answer callback: %s', type(exc).__name__)

    async def confirm(self, callback: CallbackQuery) -> None:
        try:
            prefix, raw_id = (callback.data or '').split(':', 1)
            rid = int(raw_id)
            if prefix != 'taken' or not 0 < rid < 2**63 or callback.message is None:
                raise ValueError
        except (ValueError, TypeError):
            await self.answer(callback, 'Кнопка недействительна.')
            return
        chat_id = callback.message.chat.id
        message_id = callback.message.message_id
        result = self.store.confirm(rid, callback.from_user.id, chat_id, message_id)
        if result == 'invalid':
            await self.answer(callback, 'Это напоминание недоступно.')
            return
        # State is committed before any await: even simultaneous clicks are safe.
        await self.answer(callback, 'Уже отмечено ✅' if result == 'already' else 'Отмечено ✅')
        try:
            await telegram_call(self.bot.edit_message_reply_markup,
                chat_id=chat_id, message_id=message_id, reply_markup=None)
        except TelegramAPIError as exc:
            log.warning('Cannot remove keyboard (confirmation remains saved): %s', type(exc).__name__)
        if result == 'already':
            return
        day, number = self.store.intake_info(rid)
        text = 'Ты умничка! ✨'  # Messages from the old version have no start date.
        if number is not None:
            today = datetime.now(self.timezone).date().isoformat()
            label = 'Сегодня' if day == today else f'За {datetime.fromisoformat(day):%d.%m.%Y} —'
            text = f'{label} {number}-й день, ты умничка! ✨'
        try:
            await telegram_call(self.bot.send_message, chat_id=chat_id, text=text)
        except TelegramAPIError as exc:
            log.error('Cannot deliver praise (confirmation remains saved): %s', type(exc).__name__)
        log.info('Intake confirmed: reminder=%s chat=%s', rid, chat_id)
