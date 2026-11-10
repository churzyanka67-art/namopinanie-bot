"""Personal reminder setup persisted in SQLite, including unfinished conversations."""
from datetime import datetime, time, timedelta
import re
from zoneinfo import ZoneInfo

from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

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
            text='Когда был первый приём таблеток — самая первая таблетка за весь период? 💊\n'
                 'Укажи начало первого цикла, даже если сейчас идёт уже второй или третий.\n'
                 'Напиши дату в формате ДД.ММ.ГГГГ, например 01.10.2026.\n'
                 'От этой даты я посчитаю текущий день и номер цикла. Дата регистрации в боте на расчёт не влияет.')

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
        if profile['setup_step'] == 'time':
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
            self.store.set_pending_time(message.chat.id, at.strftime('%H:%M'))
            await telegram_call(message.answer,
                text='Нужно считать дни по циклам? 💊\n'
                     'Например, при цикле 28 дней после 28-го дня начнётся 1-й день цикла 2. '
                     'Циклы идут подряд, без перерывов.',
                reply_markup=ReplyKeyboardMarkup(keyboard=[[
                    KeyboardButton(text='Да, по циклам'), KeyboardButton(text='Нет, без циклов')
                ]], resize_keyboard=True, one_time_keyboard=True))
            return
        if profile['setup_step'] == 'cycle_choice':
            if text.casefold() in ('да', 'да, по циклам'):
                self.store.request_cycle_length(message.chat.id)
                await telegram_call(message.answer,
                    text='Сколько дней в одном цикле? Напиши целое число, например 28.',
                    reply_markup=ReplyKeyboardRemove())
                return
            if text.casefold() not in ('нет', 'нет, без циклов'):
                await telegram_call(message.answer, text='Выбери «Да, по циклам» или «Нет, без циклов».')
                return
            length = None
        elif profile['setup_step'] == 'cycle_length':
            try:
                if not text.isascii() or not text.isdecimal() or len(text) > 4:
                    raise ValueError
                length = int(text)
                if not 1 <= length <= 3650:
                    raise ValueError
            except ValueError:
                await telegram_call(message.answer, text='Напиши целое число дней от 1 до 3650, например 28.')
                return
        else:
            return
        at = time.fromisoformat(profile['pending_time'])
        active = now.date() + timedelta(days=int(now.time() >= at))
        self.store.configure(message.chat.id, profile['pending_first_date'], at.strftime('%H:%M'),
                             active.isoformat(), cycle_length=length)
        self.on_configured(message.chat.id)
        number = (now.date() - datetime.fromisoformat(profile['pending_first_date']).date()).days + 1
        progress = (f'Сегодня {(number - 1) % length + 1}-й день, цикл {(number - 1) // length + 1}.'
                    if length else f'Сегодня {number}-й день с первого приёма.')
        mode = f'Цикл: {length} дн., без перерывов.' if length else 'Без циклов.'
        first_day = 'сегодня' if active == now.date() else 'завтра'
        await telegram_call(message.answer,
            text=f'Готово! Буду напоминать каждый день в {at:%H:%M} по Москве 💊\n'
                 f'Начало приёма: {datetime.fromisoformat(profile["pending_first_date"]):%d.%m.%Y}.\n'
                 f'{mode} {progress}\n'
                 f'Первое напоминание по новому расписанию — {first_day}. '
                 'Если напоминание за день уже отправлено, повторного не будет.\n'
                 'Чтобы изменить настройки, отправь /start.', reply_markup=ReplyKeyboardRemove())
