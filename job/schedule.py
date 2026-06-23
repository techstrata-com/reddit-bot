import os
import random
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from schedule_loader import mara_day_number_from_weekday

LA_TZ = ZoneInfo("America/Los_Angeles")


def la_now() -> datetime:
    return datetime.now(LA_TZ)


def la_today() -> date:
    return la_now().date()


def la_calendar_date_str() -> str:
    return la_today().isoformat()


def mara_day_for_date(d: date) -> int:
    return mara_day_number_from_weekday(d.weekday())


def workflow_trigger_time() -> time:
    hour = int(os.getenv("JOB_WORKFLOW_HOUR", "0"))
    minute = int(os.getenv("JOB_WORKFLOW_MINUTE", "1"))
    return time(hour, minute)


def telegram_slot_hours() -> list[int]:
    start = int(os.getenv("JOB_TELEGRAM_SLOT_START_HOUR", "8"))
    end = int(os.getenv("JOB_TELEGRAM_SLOT_END_HOUR", "16"))
    if start >= end:
        raise ValueError(f"Invalid telegram slot window: {start}:00 - {end}:00")
    return list(range(start, end))


def pick_telegram_slot_hour(exclude_hour: int | None) -> int:
    available = [h for h in telegram_slot_hours() if h != exclude_hour]
    if not available:
        available = telegram_slot_hours()
    return random.choice(available)


def slot_to_datetime(calendar_date: str, hour: int) -> datetime:
    d = date.fromisoformat(calendar_date)
    return datetime.combine(d, time(hour, 0), tzinfo=LA_TZ)


def is_workflow_window(now: datetime | None = None) -> bool:
    now = now or la_now()
    trigger = workflow_trigger_time()
    return now.hour == trigger.hour and now.minute == trigger.minute


def should_run_workflow_today(now: datetime | None = None) -> bool:
    """True at 00:01 LA or later the same day if workflow has not run yet."""
    now = now or la_now()
    trigger = workflow_trigger_time()
    trigger_dt = datetime.combine(now.date(), trigger, tzinfo=LA_TZ)
    return now >= trigger_dt


def is_telegram_due(scheduled_at: datetime, now: datetime | None = None) -> bool:
    now = now or la_now()
    return now >= scheduled_at


def yesterday_calendar_date(d: date | None = None) -> str:
    d = d or la_today()
    return (d - timedelta(days=1)).isoformat()
