import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

from pillbot.storage import Store

MSK = ZoneInfo('Europe/Moscow')
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=MSK)


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'test.sqlite3'
        self.store = Store(self.path)
        self.store.register(100, 100)

    def tearDown(self):
        self.temp.cleanup()

    def test_settings_and_pending_setup_survive_restart(self):
        self.store.begin_setup(100)
        self.store.set_pending_date(100, '2026-10-01')
        profile = Store(self.path).profile(100)
        self.assertEqual(profile['setup_step'], 'time')
        self.assertEqual(profile['pending_first_date'], '2026-10-01')
        self.store.configure(100, '2026-10-01', '09:30', '2026-10-26')
        profile = Store(self.path).profile(100)
        self.assertEqual(profile['first_date'], '2026-10-01')
        self.assertEqual(profile['reminder_time'], '09:30')
        self.assertIsNone(profile['setup_step'])

    def test_reconfiguration_keeps_old_settings_until_complete(self):
        self.store.configure(100, '2026-10-01', '09:30', '2026-10-25')
        self.store.begin_setup(100)
        self.store.set_pending_date(100, '2026-10-02')
        self.assertEqual(self.store.profile(100)['first_date'], '2026-10-01')
        self.assertEqual(self.store.profile(100)['reminder_time'], '09:30')

    def test_reminder_keeps_original_start_date_after_settings_change(self):
        self.store.configure(100, '2026-10-01', '09:30', '2026-10-25')
        rid = self.store.reserve(100, '2026-10-25')
        self.store.configure(100, '2026-10-20', '10:00', '2026-10-26')
        self.assertEqual(self.store.intake_info(rid), ('2026-10-25', 25))

    def test_old_database_migration_preserves_confirmed_reminder(self):
        old = Path(self.temp.name) / 'old.sqlite3'
        with sqlite3.connect(old) as conn:
            conn.executescript('''
                CREATE TABLE recipients(chat_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL);
                CREATE TABLE reminders(id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL, day TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'reserved',
                    message_id INTEGER, confirmed_at TEXT, UNIQUE(chat_id,day));
                INSERT INTO recipients VALUES(100,100);
                INSERT INTO reminders VALUES(1,100,100,'2026-10-25','sent',42,'2026-10-25');
            ''')
        migrated = Store(old)
        self.assertEqual(migrated.confirm(1, 100, 100, 42), 'already')
        self.assertIsNone(migrated.reserve(100, '2026-10-25'))
        self.assertIsNone(migrated.profile(100)['first_date'])
        self.assertEqual(migrated.intake_info(1), ('2026-10-25', None))


class SetupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from pillbot.setup import SetupFlow
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'test.sqlite3'
        self.store = Store(self.path)
        self.refresh = Mock()
        self.flow = SetupFlow(self.store, MSK, self.refresh)
        self.message = SimpleNamespace(chat=SimpleNamespace(id=100), from_user=SimpleNamespace(id=100),
                                       text='/start', answer=AsyncMock())
        await self.flow.start(self.message)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_date_then_time_and_resume_after_restart(self):
        from pillbot.setup import SetupFlow
        self.message.text = '01.10.2026'
        await self.flow.handle(self.message, NOW)
        self.flow = SetupFlow(Store(self.path), MSK, self.refresh)
        self.message.text = '18:45'
        await self.flow.handle(self.message, NOW)
        profile = self.store.profile(100)
        self.assertEqual(profile['first_date'], '2026-10-01')
        self.assertEqual(profile['reminder_time'], '18:45')
        self.assertEqual(profile['active_from'], '2026-10-25')
        self.refresh.assert_called_once_with(100)

    async def test_reject_invalid_future_dates_and_invalid_times(self):
        for text in ['31.02.2026', '26.10.2026', 'завтра']:
            self.message.text = text
            await self.flow.handle(self.message, NOW)
            self.assertEqual(self.store.profile(100)['setup_step'], 'date')
        self.message.text = '25.10.2026'
        await self.flow.handle(self.message, NOW)
        for text in ['24:00', '10:60', 'вечером']:
            self.message.text = text
            await self.flow.handle(self.message, NOW)
            self.assertEqual(self.store.profile(100)['setup_step'], 'time')
        self.refresh.assert_not_called()

    async def test_time_already_passed_starts_tomorrow(self):
        self.message.text = '25.10.2026'
        await self.flow.handle(self.message, NOW)
        self.message.text = '9:05'
        await self.flow.handle(self.message, NOW)
        self.assertEqual(self.store.profile(100)['active_from'], '2026-10-26')
        self.assertEqual(self.store.profile(100)['reminder_time'], '09:05')


class PersonalScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from pillbot.scheduler import ReminderScheduler
        from pillbot.service import ReminderService
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'test.sqlite3')
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)),
                                   edit_message_reply_markup=AsyncMock())
        self.service = ReminderService(self.bot, self.store, MSK)
        self.scheduler = ReminderScheduler(self.service)
        for chat, at in [(100, '09:00'), (200, '18:45')]:
            self.store.register(chat, chat)
            self.store.configure(chat, '2026-10-01', at, '2026-10-25')

    async def asyncTearDown(self):
        if self.scheduler.scheduler.running:
            self.scheduler.scheduler.shutdown(wait=False)
        self.temp.cleanup()

    async def test_two_personal_jobs_and_changed_time_replace_old_job(self):
        self.scheduler.restore()
        self.scheduler.scheduler.start(paused=True)
        jobs = self.scheduler.scheduler.get_jobs()
        self.assertEqual(len(jobs), 2)
        expected = {100: '2026-10-26T09:00:00+03:00', 200: '2026-10-25T18:45:00+03:00'}
        for job in jobs:
            self.assertEqual(job.trigger.get_next_fire_time(None, NOW).isoformat(), expected[job.kwargs['chat_id']])
        self.store.configure(100, '2026-10-01', '20:15', '2026-10-25')
        self.scheduler.refresh(100)
        self.assertEqual(len(self.scheduler.scheduler.get_jobs()), 2)
        self.assertEqual(self.scheduler.scheduler.get_job('pill:100').trigger.get_next_fire_time(None, NOW).hour, 20)

    async def test_startup_catches_up_only_due_users_and_no_duplicates(self):
        await self.scheduler.catch_up(NOW)
        await self.scheduler.catch_up(NOW)
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertEqual(self.bot.send_message.call_args.kwargs['chat_id'], 100)

    async def test_no_delivery_before_setup_or_before_active_date(self):
        self.store.register(300, 300)
        self.store.configure(100, '2026-10-01', '09:00', '2026-10-26')
        await self.scheduler.catch_up(NOW)
        self.bot.send_message.assert_not_awaited()
        await self.service.send_daily(NOW, chat_id=200)
        self.assertEqual(self.bot.send_message.call_args.kwargs['chat_id'], 200)

    async def test_calendar_day_praise_and_old_message_date(self):
        from unittest.mock import patch
        await self.service.send_daily(NOW, chat_id=100)
        callback = SimpleNamespace(data='taken:1', from_user=SimpleNamespace(id=100),
            message=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=42), answer=AsyncMock())
        self.bot.send_message.reset_mock()
        with patch('pillbot.service.datetime', wraps=datetime) as clock:
            clock.now.return_value = NOW
            await self.service.confirm(callback)
        self.bot.send_message.assert_awaited_once_with(chat_id=100, text='Сегодня 25-й день, ты умничка! ✨')
        await self.service.send_daily(NOW, chat_id=200)
        callback.data, callback.from_user.id, callback.message.chat.id = 'taken:2', 200, 200
        self.bot.send_message.reset_mock()
        with patch('pillbot.service.datetime', wraps=datetime) as clock:
            clock.now.return_value = NOW.replace(day=26)
            await self.service.confirm(callback)
        self.bot.send_message.assert_awaited_once_with(chat_id=200, text='За 25.10.2026 — 25-й день, ты умничка! ✨')


class DispatcherFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_date_time_routes_to_personal_schedule(self):
        from aiogram import Bot
        from aiogram.client.session.base import BaseSession
        from aiogram.methods import SendMessage
        from aiogram.types import Chat, Message, MessageEntity, Update, User
        from pillbot.main import make_dispatcher
        from pillbot.scheduler import ReminderScheduler
        from pillbot.service import ReminderService

        class LocalSession(BaseSession):
            def __init__(self):
                super().__init__()
                self.sent = []

            async def close(self):
                pass

            async def stream_content(self, *args, **kwargs):
                if False:
                    yield b''

            async def make_request(self, bot, method, timeout=None):
                if not isinstance(method, SendMessage):
                    raise AssertionError(type(method).__name__)
                self.sent.append(method)
                return Message(message_id=len(self.sent), date=NOW,
                               chat=Chat(id=method.chat_id, type='private'), text=method.text)

        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'bot.sqlite3')
            session = LocalSession()
            async with Bot('123456:offline-test-token', session=session) as bot:
                service = ReminderService(bot, store, MSK)
                schedule = ReminderScheduler(service)
                dp = make_dispatcher(service, schedule)
                for i, text in enumerate(['/start', '01.01.2020', '18:45'], start=1):
                    entities = [MessageEntity(type='bot_command', offset=0, length=6)] if i == 1 else None
                    message = Message(message_id=i, date=NOW, chat=Chat(id=100, type='private'),
                                      from_user=User(id=100, is_bot=False, first_name='Test'),
                                      text=text, entities=entities)
                    await dp.feed_update(bot, Update(update_id=i, message=message))
                self.assertIn('Когда был первый приём', session.sent[0].text)
                self.assertIn('Во сколько', session.sent[1].text)
                self.assertIn('18:45 по Москве', session.sent[2].text)
                self.assertEqual(store.profile(100)['first_date'], '2020-01-01')
                self.assertEqual(store.profile(100)['reminder_time'], '18:45')
                self.assertEqual(len(schedule.scheduler.get_jobs()), 1)
