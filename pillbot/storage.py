"""Small, transactional SQLite store. Every operation closes its connection."""
from contextlib import closing
from datetime import date
from pathlib import Path
import sqlite3

from .compliments import COMPLIMENTS


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as conn, conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS recipients (
                    chat_id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL REFERENCES recipients(chat_id),
                    user_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'reserved',
                    message_id INTEGER,
                    confirmed_at TEXT,
                    UNIQUE(chat_id, day)
                );
            ''')
            # Migrate existing databases in place; keep recipients and history.
            conn.execute('BEGIN IMMEDIATE')
            additions = {
                'recipients': ('first_date', 'reminder_time', 'active_from',
                               'setup_step', 'pending_first_date', 'pending_time', 'cycle_length', 'pending_cycle_length'),
                'reminders': ('first_date', 'cycle_length', 'compliment_index'),
            }
            for table, names in additions.items():
                existing = {r['name'] for r in conn.execute(f'PRAGMA table_info({table})')}
                for name in names:
                    if name not in existing:
                        conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} TEXT')

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    def register(self, chat_id: int, user_id: int) -> None:
        with closing(self.connect()) as conn, conn:
            conn.execute('INSERT INTO recipients(chat_id, user_id) VALUES (?, ?) ON CONFLICT(chat_id) '
                         'DO UPDATE SET user_id=excluded.user_id', (chat_id, user_id))

    def recipients(self) -> list[tuple[int, int]]:
        with closing(self.connect()) as conn:
            return [(r['chat_id'], r['user_id']) for r in conn.execute('SELECT * FROM recipients')]

    def profile(self, chat_id: int) -> dict | None:
        with closing(self.connect()) as conn:
            row = conn.execute('SELECT * FROM recipients WHERE chat_id=?', (chat_id,)).fetchone()
            return dict(row) if row is not None else None

    def configured_profiles(self) -> list[dict]:
        with closing(self.connect()) as conn:
            return [dict(row) for row in conn.execute(
                'SELECT * FROM recipients WHERE first_date IS NOT NULL AND reminder_time IS NOT NULL')]

    def begin_setup(self, chat_id: int) -> None:
        with closing(self.connect()) as conn, conn:
            conn.execute("UPDATE recipients SET setup_step='date', pending_first_date=NULL, pending_time=NULL, pending_cycle_length=NULL WHERE chat_id=?",
                         (chat_id,))

    def set_pending_date(self, chat_id: int, first_date: str) -> None:
        with closing(self.connect()) as conn, conn:
            conn.execute("UPDATE recipients SET pending_first_date=?, setup_step='time' WHERE chat_id=?",
                         (first_date, chat_id))

    def set_pending_time(self, chat_id: int, reminder_time: str) -> None:
        with closing(self.connect()) as conn, conn:
            conn.execute("UPDATE recipients SET pending_time=?, setup_step='cycle_choice' WHERE chat_id=?",
                         (reminder_time, chat_id))

    def request_cycle_length(self, chat_id: int) -> None:
        with closing(self.connect()) as conn, conn:
            conn.execute("UPDATE recipients SET setup_step='cycle_length' WHERE chat_id=?", (chat_id,))

    def request_cycle_first_date(self, chat_id: int, length: int) -> None:
        with closing(self.connect()) as conn, conn:
            conn.execute("UPDATE recipients SET pending_cycle_length=?, setup_step='cycle_first_date' WHERE chat_id=?",
                         (length, chat_id))

    def configure(self, chat_id: int, first_date: str, reminder_time: str, active_from: str,
                  cycle_length: int | None = None) -> None:
        if cycle_length is not None and (type(cycle_length) is not int or not 1 <= cycle_length <= 3650):
            raise ValueError('Cycle length must be an integer from 1 to 3650')
        with closing(self.connect()) as conn, conn:
            conn.execute('UPDATE recipients SET first_date=?, reminder_time=?, active_from=?, '
                         'cycle_length=?, setup_step=NULL, pending_first_date=NULL, pending_time=NULL, pending_cycle_length=NULL WHERE chat_id=?',
                         (first_date, reminder_time, active_from, cycle_length, chat_id))

    def intake_info(self, reminder_id: int) -> tuple[str, int | None]:
        with closing(self.connect()) as conn:
            row = conn.execute('SELECT day, first_date FROM reminders WHERE id=?', (reminder_id,)).fetchone()
            number = ((date.fromisoformat(row['day']) - date.fromisoformat(row['first_date'])).days + 1
                      if row['first_date'] else None)
            return row['day'], number

    def reminder_cycle(self, reminder_id: int) -> int | None:
        with closing(self.connect()) as conn:
            value = conn.execute('SELECT cycle_length FROM reminders WHERE id=?', (reminder_id,)).fetchone()[0]
            return int(value) if value is not None else None

    def reminder_compliment(self, reminder_id: int) -> str:
        with closing(self.connect()) as conn:
            value = conn.execute('SELECT compliment_index FROM reminders WHERE id=?', (reminder_id,)).fetchone()[0]
            return COMPLIMENTS[int(value or 0) % len(COMPLIMENTS)]

    def reserve(self, chat_id: int, day: str) -> int | None:
        # Commit BEFORE sendMessage: a crash or an unknown delivery result must
        # never cause another attempt for this chat/date after a restart.
        with closing(self.connect()) as conn, conn:
            cursor = conn.execute('''
                INSERT INTO reminders(chat_id, user_id, day, first_date, cycle_length)
                SELECT chat_id, user_id, ?, first_date, cycle_length FROM recipients WHERE chat_id=?
                ON CONFLICT(chat_id, day) DO NOTHING
            ''', (day, chat_id))
            return cursor.lastrowid if cursor.rowcount == 1 else None

    def mark(self, reminder_id: int, status: str, message_id: int | None = None) -> None:
        with closing(self.connect()) as conn, conn:
            conn.execute('UPDATE reminders SET status=?, message_id=COALESCE(message_id, ?) WHERE id=?',
                         (status, message_id, reminder_id))

    def confirm(self, reminder_id: int, user_id: int, chat_id: int, message_id: int) -> str:
        with closing(self.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM reminders WHERE id=?', (reminder_id,)).fetchone()
            if (row is None or row['user_id'] != user_id or row['chat_id'] != chat_id
                    or row['status'] == 'failed'
                    or (row['message_id'] is not None and row['message_id'] != message_id)):
                return 'invalid'
            if row['confirmed_at'] is not None:
                return 'already'
            index = conn.execute('SELECT COUNT(*) FROM reminders WHERE chat_id=? AND confirmed_at IS NOT NULL',
                                 (chat_id,)).fetchone()[0] % len(COMPLIMENTS)
            conn.execute('UPDATE reminders SET confirmed_at=CURRENT_TIMESTAMP, compliment_index=?, '
                         'message_id=COALESCE(message_id, ?) WHERE id=?', (index, message_id, reminder_id))
            return 'confirmed'
