import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo
from pillbot.storage import Store
from pillbot.service import ReminderService
from pillbot.setup import SetupFlow

MSK = ZoneInfo('Europe/Moscow')
NOW = datetime(2026, 11, 10, 12, tzinfo=MSK)

class CycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'db.sqlite3'
        self.db = Store(self.path)
        self.db.register(100, 100)
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)),
                                  edit_message_reply_markup=AsyncMock())
        self.service = ReminderService(self.bot, self.db, MSK)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_cycle_boundaries_and_snapshot(self):
        first = NOW.date()
        self.db.configure(100, first.isoformat(), '12:00', first.isoformat(), cycle_length=28)
        for offset, day, cycle in [(0,1,1),(27,28,1),(28,1,2),(55,28,2),(56,1,3)]:
            now = NOW + timedelta(days=offset)
            rid = self.db.reserve(100, now.date().isoformat())
            callback = SimpleNamespace(data=f'taken:{rid}', from_user=SimpleNamespace(id=100),
                message=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=42), answer=AsyncMock())
            with patch('pillbot.service.datetime', wraps=datetime) as clock:
                clock.now.return_value = now
                await self.service.confirm(callback)
            text = self.bot.send_message.call_args.kwargs['text']
            self.assertIn(f'Сегодня {day}-й день, цикл {cycle}.', text)
        # Reconfiguration must not rewrite a previously sent reminder.
        self.db.configure(100, first.isoformat(), '12:00', first.isoformat(), cycle_length=7)
        self.assertEqual(self.db.reminder_cycle(1), 28)

    async def test_compliments_rotate_persistently_without_repeating_until_exhausted(self):
        from pillbot.compliments import COMPLIMENTS
        self.assertGreaterEqual(len(COMPLIMENTS), 30)
        self.assertEqual(len(COMPLIMENTS), len(set(COMPLIMENTS)))
        self.db.configure(100, '2026-11-01', '12:00', '2026-11-01')
        texts=[]
        for offset in range(len(COMPLIMENTS) + 1):
            day = (NOW + timedelta(days=offset)).date().isoformat()
            db = Store(self.path)
            rid = db.reserve(100, day)
            self.assertEqual(db.confirm(rid, 100, 100, 42), 'confirmed')
            texts.append(db.reminder_compliment(rid))
            self.assertEqual(db.confirm(rid, 100, 100, 42), 'already')
            self.assertEqual(db.reminder_compliment(rid), texts[-1])
        self.assertEqual(len(set(texts[:-1])), len(COMPLIMENTS))
        self.assertEqual(texts[0], texts[-1])
        self.assertNotEqual(texts[-2], texts[-1])

    async def test_setup_cycle_choice_length_restart_and_validation(self):
        flow = SetupFlow(self.db, MSK, Mock())
        msg = SimpleNamespace(chat=SimpleNamespace(id=100), from_user=SimpleNamespace(id=100), answer=AsyncMock(), text='')
        await flow.start(msg)
        for answer in ['01.11.2026','18:45','Да, по циклам']:
            msg.text = answer
            await flow.handle(msg, NOW)
        self.assertEqual(self.db.profile(100)['setup_step'], 'cycle_length')
        for answer in ['0','-1','2.5','abc','999999999999999999999999999']:
            msg.text = answer
            await flow.handle(msg, NOW)
            self.assertEqual(self.db.profile(100)['setup_step'], 'cycle_length')
        self.assertIsNone(self.db.profile(100)['first_date'])
        flow = SetupFlow(Store(self.path), MSK, Mock())
        msg.text='28'
        await flow.handle(msg, NOW)
        profile = self.db.profile(100)
        self.assertEqual(int(profile['cycle_length']), 28)
        self.assertEqual(profile['first_date'], '2026-11-01')
        self.assertEqual(profile['reminder_time'], '18:45')
        self.assertIsNone(profile['setup_step'])

    async def test_old_profile_continues_during_setup_and_can_disable_cycle(self):
        self.db.configure(100, '2026-11-01','09:00','2026-11-01',cycle_length=28)
        flow = SetupFlow(self.db,MSK,Mock())
        msg=SimpleNamespace(chat=SimpleNamespace(id=100),from_user=SimpleNamespace(id=100),answer=AsyncMock(),text='')
        await flow.start(msg)
        for answer in ['02.11.2026','19:30']:
            msg.text=answer
            await flow.handle(msg,NOW)
        self.assertEqual(self.db.profile(100)['reminder_time'],'09:00')
        self.assertEqual(int(self.db.profile(100)['cycle_length']),28)
        msg.text='Нет, без циклов'
        await flow.handle(msg,NOW)
        self.assertIsNone(self.db.profile(100)['cycle_length'])
        self.assertEqual(self.db.profile(100)['reminder_time'],'19:30')

    async def test_registering_in_second_cycle_uses_original_first_dose_date(self):
        flow = SetupFlow(self.db, MSK, Mock())
        msg = SimpleNamespace(chat=SimpleNamespace(id=100), from_user=SimpleNamespace(id=100),
                              answer=AsyncMock(), text='')
        await flow.start(msg)
        for answer in ['01.10.2026', '18:45', 'Да, по циклам', '28']:
            msg.text = answer
            await flow.handle(msg, NOW)
        # First dose October 1; registration November 10: total day 41,
        # so day 13 of cycle 2, not day 1 of cycle 1.
        self.assertIn('Сегодня 13-й день, цикл 2.', msg.answer.call_args.kwargs['text'])
        await self.service.send_daily(NOW, force=True)
        callback = SimpleNamespace(data='taken:1', from_user=SimpleNamespace(id=100),
            message=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=42), answer=AsyncMock())
        with patch('pillbot.service.datetime', wraps=datetime) as clock:
            clock.now.return_value = NOW
            await self.service.confirm(callback)
        self.assertIn('Сегодня 13-й день, цикл 2.', self.bot.send_message.call_args.kwargs['text'])
