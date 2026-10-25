"""Two-step setup persisted in SQLite, including unfinished conversations."""
from datetime import datetime, time, timedelta
import re
from zoneinfo import ZoneInfo

from aiogram.types import Message

from .service import telegram_call
from .storage import Store


class SetupFlow:
    def __init__(self, store: Store, timezone: ZoneInfo, on_configured):
        self.store, self.timezone, self.on_configured = store, timezone, on_configured

    async def start(self, message: Message) -> None:
        if message.from_user is None:
            return
        chat_id = message.chat.id
        self.store.register(chat_id, message.from_user.id)
        self.store.begin_setup(chat_id)
        await telegram_call(message.answer,
            text='Когда был первый приём таблеток? 💊\n'
                 'Напиши дату в формате ДД.ММ.ГГГГ, например 01.10.2026.\n'
                 'Этот день будет считаться первым.')

    async def handle(self, message: Message, now: datetime | None = None) -> None:
        profile = self.store.profile(message.chat.id)
        if not profile or not profile['setup_step'] or message.from_user is None:
            return
        if profile['user_id'] != message.from_user.id:
            return
        now = (now or datetime.now(self.timezone)).astimezone(self.timezone)
        text = (message.text or '').strip()
        if profile['setup_step'] == 'date':
            try:
                first = datetime.strptime(text, '%d.%m.%Y').date()
                if first > now.date():
                    raise ValueError
            except ValueError:
                await telegram_call(message.answer,
                    text='Нужна существующая дата не позже сегодняшней, в формате ДД.ММ.ГГГГ. '
                         'Например: 01.10.2026.')
                return
            self.store.set_pending_date(message.chat.id, first.isoformat())
            await telegram_call(message.answer,
                text='Во сколько напоминать каждый день? ⏰\n'
                     'Напиши время по Москве в формате ЧЧ:ММ, например 22:59.')
            return
        try:
            if not re.fullmatch(r'\d{1,2}:\d{2}', text):
                raise ValueError
            hour, minute = map(int, text.split(':'))
            at = time(hour, minute)
        except ValueError:
            await telegram_call(message.answer,
                text='Введи время от 00:00 до 23:59 в формате ЧЧ:ММ. Например: 09:30. '
                     'Время московское.')
            return
        active = now.date() + timedelta(days=int(now.time() >= at))
        self.store.configure(message.chat.id, profile['pending_first_date'], at.strftime('%H:%M'), active.isoformat())
        self.on_configured(message.chat.id)
        number = (now.date() - datetime.fromisoformat(profile['pending_first_date']).date()).days + 1
        first_day = 'сегодня' if active == now.date() else 'завтра'
        await telegram_call(message.answer,
            text=f'Готово! Буду напоминать каждый день в {at:%H:%M} по Москве 💊\n'
                 f'Сегодня {number}-й день с первого приёма.\n'
                 f'Первое напоминание по новому расписанию — {first_day}. '
                 'Если напоминание за день уже отправлено, повторного не будет.\n'
                 'Чтобы изменить дату или время, отправь /start.')
