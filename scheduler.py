from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .service import ReminderService


def make_trigger(timezone, reminder_time: str, active_from: str | None = None) -> CronTrigger:
    hour, minute = map(int, reminder_time.split(':'))
    start = datetime.fromisoformat(active_from).replace(tzinfo=timezone) if active_from else None
    return CronTrigger(hour=hour, minute=minute, second=0, timezone=timezone, start_date=start)


class ReminderScheduler:
    def __init__(self, service: ReminderService):
        self.service = service
        self.scheduler = AsyncIOScheduler(timezone=service.timezone)

    def refresh(self, chat_id: int) -> None:
        profile = self.service.store.profile(chat_id)
        if not profile or not profile['first_date'] or not profile['reminder_time']:
            return
        self.scheduler.add_job(self.service.send_daily,
            trigger=make_trigger(self.service.timezone, profile['reminder_time'], profile['active_from']),
            kwargs={'chat_id': chat_id}, id=f'pill:{chat_id}', replace_existing=True,
            max_instances=1, coalesce=True, misfire_grace_time=60)

    def restore(self) -> None:
        for profile in self.service.store.configured_profiles():
            self.refresh(profile['chat_id'])

    async def catch_up(self, now: datetime | None = None) -> None:
        local = (now or datetime.now(self.service.timezone)).astimezone(self.service.timezone)
        for profile in self.service.store.configured_profiles():
            if profile['active_from'] <= local.date().isoformat() and profile['reminder_time'] <= local.strftime('%H:%M'):
                await self.service.send_daily(local, chat_id=profile['chat_id'])
