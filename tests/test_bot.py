import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage

from pillbot.storage import Store
from pillbot.service import ReminderService
from pillbot.scheduler import make_trigger


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'bot.sqlite3'
        self.db = Store(self.path)
        self.db.register(100, 100)

    def tearDown(self):
        self.temp.cleanup()

    def test_registration_survives_restart_and_is_idempotent(self):
        self.db.register(100, 100)
        self.assertEqual(Store(self.path).recipients(), [(100, 100)])

    def test_daily_reservation_survives_restart_and_next_day_is_fresh(self):
        first = self.db.reserve(100, '2026-10-25')
        self.assertIsNotNone(first)
        self.assertIsNone(Store(self.path).reserve(100, '2026-10-25'))
        self.assertNotEqual(Store(self.path).reserve(100, '2026-10-26'), first)

    def test_confirmation_checks_owner_chat_message_and_survives_restart(self):
        rid = self.db.reserve(100, '2026-10-25')
        self.db.mark(rid, 'sent', 42)
        for user, chat, message in [(101, 100, 42), (100, 101, 42), (100, 100, 43)]:
            self.assertEqual(self.db.confirm(rid, user, chat, message), 'invalid')
        self.assertEqual(self.db.confirm(rid, 100, 100, 42), 'confirmed')
        self.assertEqual(Store(self.path).confirm(rid, 100, 100, 42), 'already')

    def test_delivered_message_can_be_confirmed_after_crash_before_save(self):
        rid = self.db.reserve(100, '2026-10-25')
        self.assertEqual(Store(self.path).confirm(rid, 100, 100, 42), 'confirmed')
        self.assertEqual(self.db.confirm(rid, 100, 100, 43), 'invalid')

    def test_two_connections_cannot_claim_same_day(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: Store(self.path).reserve(100, '2026-10-25'), range(2)))
        self.assertEqual(sum(value is not None for value in claims), 1)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'bot.sqlite3'
        self.db = Store(self.path)
        self.db.register(100, 100)
        self.db.configure(100, '2026-10-01', '22:59', '2026-10-25')
        self.bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)),
            edit_message_reply_markup=AsyncMock(),
        )
        self.service = ReminderService(self.bot, self.db, ZoneInfo('Europe/Moscow'))
        self.now = datetime(2026, 10, 25, 22, 59, tzinfo=ZoneInfo('Europe/Moscow'))
        clock_patch = patch('pillbot.service.datetime', wraps=datetime)
        self.clock = clock_patch.start()
        self.clock.now.return_value = self.now
        self.addCleanup(clock_patch.stop)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def callback(self, rid=1, user=100, chat=100, mid=42):
        return SimpleNamespace(data=f'taken:{rid}', from_user=SimpleNamespace(id=user),
            message=SimpleNamespace(chat=SimpleNamespace(id=chat), message_id=mid),
            answer=AsyncMock())

    async def test_daily_delivery_is_unique_and_next_day_has_new_button(self):
        await asyncio.gather(self.service.send_daily(self.now), self.service.send_daily(self.now))
        self.assertEqual(self.bot.send_message.await_count, 1)
        call = self.bot.send_message.call_args.kwargs
        self.assertEqual(call['text'], '💊 Время выпить таблетку!')
        button = call['reply_markup'].inline_keyboard[0][0]
        self.assertEqual(button.text, '✅ Я выпила')
        await ReminderService(self.bot, Store(self.path), self.service.timezone).send_daily(self.now)
        self.assertEqual(self.bot.send_message.await_count, 1)
        await self.service.send_daily(self.now.replace(day=26))
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertNotEqual(button.callback_data,
            self.bot.send_message.call_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data)

    async def test_double_click_sends_one_separate_praise_and_removes_button(self):
        await self.service.send_daily(self.now)
        self.bot.send_message.reset_mock()
        await asyncio.gather(self.service.confirm(self.callback()), self.service.confirm(self.callback()))
        self.bot.send_message.assert_awaited_once_with(chat_id=100, text='Сегодня 25-й день, ты умничка! ✨')
        self.assertEqual(self.bot.edit_message_reply_markup.call_args.kwargs['reply_markup'], None)

    async def test_foreign_and_malformed_callbacks_do_not_confirm(self):
        await self.service.send_daily(self.now)
        self.bot.send_message.reset_mock()
        for callback in [self.callback(user=101), self.callback(chat=101), self.callback(mid=43)]:
            await self.service.confirm(callback)
        bad = self.callback()
        bad.data = 'taken:broken'
        await self.service.confirm(bad)
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(self.db.confirm(1, 100, 100, 42), 'confirmed')

    async def test_network_timeout_does_not_duplicate_after_restart(self):
        self.bot.send_message.side_effect = TelegramNetworkError(method=SendMessage(chat_id=100, text='x'), message='timeout')
        with self.assertLogs('pillbot', level='WARNING'):
            await self.service.send_daily(self.now)
        await ReminderService(self.bot, Store(self.path), self.service.timezone).send_daily(self.now)
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_rate_limit_retries_rejected_request(self):
        self.bot.send_message.side_effect = [TelegramRetryAfter(method=SendMessage(chat_id=100, text='x'), message='limit', retry_after=0), SimpleNamespace(message_id=42)]
        with self.assertLogs('pillbot', level='WARNING'):
            await self.service.send_daily(self.now)
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertEqual(self.db.confirm(1, 100, 100, 42), 'confirmed')

    async def test_long_and_repeated_rate_limit_does_not_lose_daily_reminder(self):
        method = SendMessage(chat_id=100, text='x')
        self.bot.send_message.side_effect = [
            TelegramRetryAfter(method=method, message='limit', retry_after=31),
            *[TelegramRetryAfter(method=method, message='limit', retry_after=0) for _ in range(3)],
            SimpleNamespace(message_id=42),
        ]
        with patch('pillbot.service.asyncio.sleep', new_callable=AsyncMock) as sleep:
            with self.assertLogs('pillbot', level='WARNING'):
                await self.service.send_daily(self.now)
        self.assertEqual(self.bot.send_message.await_count, 5)
        sleep.assert_any_await(31)
        self.assertEqual(self.db.confirm(1, 100, 100, 42), 'confirmed')

    async def test_rate_limit_does_not_deliver_old_reminder_tomorrow(self):
        self.clock.now.return_value = self.now.replace(hour=23, minute=59, second=50)
        self.bot.send_message.side_effect = TelegramRetryAfter(
            method=SendMessage(chat_id=100, text='x'), message='limit', retry_after=20)
        with patch('pillbot.service.asyncio.sleep', new_callable=AsyncMock) as sleep:
            with self.assertLogs('pillbot', level='WARNING'):
                await self.service.send_daily(self.clock.now.return_value)
        sleep.assert_not_awaited()
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertEqual(self.db.confirm(1, 100, 100, 42), 'invalid')

    async def test_edit_failure_does_not_undo_confirmation(self):
        await self.service.send_daily(self.now)
        self.bot.send_message.reset_mock()
        self.bot.edit_message_reply_markup.side_effect = TelegramBadRequest(method=SendMessage(chat_id=100, text='x'), message='cannot edit')
        with self.assertLogs('pillbot', level='WARNING'):
            await self.service.confirm(self.callback())
        self.bot.send_message.assert_awaited_once_with(chat_id=100, text='Сегодня 25-й день, ты умничка! ✨')
        self.assertEqual(self.db.confirm(1, 100, 100, 42), 'already')

    async def test_moscow_date_is_used_when_server_clock_is_utc(self):
        await self.service.send_daily(datetime(2026, 10, 25, 21, 10, tzinfo=timezone.utc))
        self.assertIsNone(self.db.reserve(100, '2026-10-26'))


class ScheduleTests(unittest.TestCase):
    def test_next_run_is_2259_moscow_from_utc(self):
        trigger = make_trigger(ZoneInfo('Europe/Moscow'), '22:59')
        now = datetime(2026, 10, 25, 19, 58, tzinfo=timezone.utc)
        next_run = trigger.get_next_fire_time(None, now)
        self.assertEqual(next_run.isoformat(), '2026-10-25T22:59:00+03:00')
        self.assertEqual(trigger.get_next_fire_time(next_run, next_run).isoformat(), '2026-10-26T22:59:00+03:00')
