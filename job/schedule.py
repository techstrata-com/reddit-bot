import random
from datetime import datetime, timedelta

from zoneinfo import ZoneInfo

LA_TZ = ZoneInfo("America/Los_Angeles")


def la_now() -> datetime:
    return datetime.now(LA_TZ)


def la_today():
    return la_now().date()


def la_calendar_date_str() -> str:
    return la_today().isoformat()


def mara_day_for_date(d) -> int:
    from schedule_loader import mara_day_number_from_weekday

    return mara_day_number_from_weekday(d.weekday())


def workflow_trigger_time():
    import os
    from datetime import time

    hour = int(os.getenv("JOB_WORKFLOW_HOUR", "0"))
    minute = int(os.getenv("JOB_WORKFLOW_MINUTE", "1"))
    return time(hour, minute)


def telegram_slot_hours() -> list[int]:
    import os

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
    from datetime import date, time

    d = date.fromisoformat(calendar_date)
    return datetime.combine(d, time(hour, 0), tzinfo=LA_TZ)


def should_run_workflow_today(now: datetime | None = None) -> bool:
    now = now or la_now()
    trigger = workflow_trigger_time()
    trigger_dt = datetime.combine(now.date(), trigger, tzinfo=LA_TZ)
    return now >= trigger_dt


def is_telegram_due(scheduled_at: datetime, now: datetime | None = None) -> bool:
    now = now or la_now()
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=LA_TZ)
    return now >= scheduled_at


def assign_comment_slots(
    num_comments: int,
    calendar_date: str,
    exclude_hour: int | None,
) -> list[datetime]:
    """Assign one LA hourly slot per comment, spread across the day."""
    if num_comments == 0:
        return []

    hours = list(telegram_slot_hours())
    if exclude_hour in hours and len(hours) > 1:
        hours = [h for h in hours if h != exclude_hour]

    first_hour = pick_telegram_slot_hour(exclude_hour)
    if first_hour in hours:
        idx = hours.index(first_hour)
        hours = hours[idx:] + hours[:idx]

    slots: list[datetime] = []
    for i in range(num_comments):
        hour = hours[i % len(hours)]
        base = slot_to_datetime(calendar_date, hour)
        # If more comments than hours, stagger by 10 minutes within the same hour
        cycle = i // len(hours)
        slots.append(base + timedelta(minutes=cycle * 10))
    return slots


def format_la_time(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LA_TZ)
    return dt.astimezone(LA_TZ).strftime("%Y-%m-%d %H:%M %Z")


def yesterday_calendar_date(d=None) -> str:
    from datetime import timedelta

    d = d or la_today()
    return (d - timedelta(days=1)).isoformat()
