"""
Daily job daemon: workflow at 00:01 LA, Telegram handoff in scheduled hourly slot.

Also runs the Telegram button listener in a background thread.
"""

import argparse
import sys
import threading
import time
import traceback

from dotenv import load_dotenv

from db.connection import close_connection
from db.repository import PipelineRepository, utcnow
from job.logging import DailyJobLogger
from job.schedule import (
    is_telegram_due,
    la_calendar_date_str,
    la_now,
    mara_day_for_date,
    la_today,
    pick_telegram_slot_hour,
    should_run_workflow_today,
    slot_to_datetime,
)
from pipeline_runner import run_workflow, send_telegram_for_run
from schedule_loader import DAY_NUMBERS
from telegram_bot import run_bot

load_dotenv(override=True)

TOP_N = int(__import__("os").getenv("JOB_TOP_N", "3"))
POLL_SECS = float(__import__("os").getenv("JOB_POLL_SECS", "30"))


def _logger_for_today() -> DailyJobLogger:
    return DailyJobLogger(la_calendar_date_str())


def _run_workflow_job(log: DailyJobLogger, repo: PipelineRepository, calendar_date: str) -> None:
    day_number = mara_day_for_date(la_today())
    day_name = DAY_NUMBERS[day_number]
    job = repo.ensure_job_run(calendar_date, day_number, day_name)

    if job.get("workflow_status") in {"completed", "skipped"}:
        log.info(f"Workflow already {job['workflow_status']} for {calendar_date} — skipping")
        return

    log.section(f"DAILY WORKFLOW START — {day_name} (day {day_number})")
    log.info(f"Calendar date (LA): {calendar_date}")
    log.info(f"Command equivalent: python app.py {day_number} {TOP_N}")

    repo.update_job_run(
        calendar_date,
        workflow_status="running",
        workflow_started_at=utcnow(),
    )

    result = run_workflow(day_number, TOP_N)

    if result.skipped:
        log.info(f"Workflow skipped: {result.skip_reason}")
        repo.update_job_run(
            calendar_date,
            workflow_status="skipped",
            workflow_finished_at=utcnow(),
            workflow_duration_secs=round(result.duration_secs, 2),
            workflow_error=None,
            telegram_status="skipped",
        )
        log.info(f"Duration: {result.duration_secs:.1f}s")
        return

    if not result.success:
        log.error(f"Workflow failed after {result.duration_secs:.1f}s", exc=Exception(result.error or "unknown"))
        repo.update_job_run(
            calendar_date,
            workflow_status="failed",
            workflow_finished_at=utcnow(),
            workflow_duration_secs=round(result.duration_secs, 2),
            workflow_error=result.error,
            telegram_status="failed",
            telegram_error="Workflow failed — no comments to send",
        )
        return

    yesterday_slot = repo.get_yesterday_telegram_slot(calendar_date)
    slot_hour = pick_telegram_slot_hour(yesterday_slot)
    scheduled_at = slot_to_datetime(calendar_date, slot_hour)

    log.info(f"Workflow completed in {result.duration_secs:.1f}s")
    log.info(f"Run key: {result.run_key}")
    log.info(f"Groups: {result.group_ids}")

    if yesterday_slot is not None:
        log.info(f"Yesterday's Telegram slot was {yesterday_slot}:00 LA — excluded from today's pick")
    log.info(f"Telegram handoff scheduled for {scheduled_at.strftime('%Y-%m-%d %H:%M %Z')} (slot {slot_hour}:00)")

    repo.update_job_run(
        calendar_date,
        workflow_status="completed",
        workflow_run_key=result.run_key,
        workflow_finished_at=utcnow(),
        workflow_duration_secs=round(result.duration_secs, 2),
        workflow_groups=result.group_ids,
        workflow_error=None,
        telegram_slot_hour=slot_hour,
        telegram_scheduled_at=scheduled_at,
        telegram_status="scheduled",
    )
    log.section("DAILY WORKFLOW COMPLETE")


def _run_telegram_job(
    log: DailyJobLogger,
    repo: PipelineRepository,
    calendar_date: str,
    *,
    force: bool = False,
) -> None:
    job = repo.get_job_run(calendar_date)
    if not job:
        return

    if job.get("telegram_status") in {"sent", "skipped", "failed"}:
        return

    if job.get("workflow_status") not in {"completed"}:
        return

    scheduled_at = job.get("telegram_scheduled_at")
    if not scheduled_at:
        return

    if scheduled_at.tzinfo is None:
        from zoneinfo import ZoneInfo

        scheduled_at = scheduled_at.replace(tzinfo=ZoneInfo("America/Los_Angeles"))

    if not force and not is_telegram_due(scheduled_at):
        return

    run_key = job.get("workflow_run_key")
    if not run_key:
        log.error("Telegram due but workflow_run_key is missing")
        repo.update_job_run(
            calendar_date,
            telegram_status="failed",
            telegram_error="Missing workflow_run_key",
        )
        return

    log.section("TELEGRAM HANDOFF START")
    log.info(f"Scheduled time: {scheduled_at.strftime('%Y-%m-%d %H:%M %Z')}")
    log.info(f"Run key: {run_key}")

    repo.update_job_run(
        calendar_date,
        telegram_status="running",
        telegram_started_at=utcnow(),
    )

    result = send_telegram_for_run(run_key, job.get("workflow_groups"))

    if result.error:
        log.error(f"Telegram send failed after {result.duration_secs:.1f}s", exc=Exception(result.error))
        repo.update_job_run(
            calendar_date,
            telegram_status="failed",
            telegram_finished_at=utcnow(),
            telegram_duration_secs=round(result.duration_secs, 2),
            telegram_error=result.error,
            telegram_comments_sent=result.sent,
            telegram_comments_failed=result.failed,
        )
        return

    status = "sent" if result.failed == 0 else "partial"
    log.info(f"Telegram handoff {status}: {result.sent} sent, {result.failed} failed")
    log.info(f"Duration: {result.duration_secs:.1f}s")
    log.info("Users can copy comments and tap Done when published on Reddit")

    repo.update_job_run(
        calendar_date,
        telegram_status=status,
        telegram_finished_at=utcnow(),
        telegram_duration_secs=round(result.duration_secs, 2),
        telegram_error=None if result.failed == 0 else f"{result.failed} message(s) failed",
        telegram_comments_sent=result.sent,
        telegram_comments_failed=result.failed,
    )
    log.section("TELEGRAM HANDOFF COMPLETE")


def tick(log: DailyJobLogger | None = None) -> None:
    log = log or _logger_for_today()
    calendar_date = la_calendar_date_str()
    now = la_now()
    repo = PipelineRepository()

    try:
        job = repo.get_job_run(calendar_date)
        if job is None:
            day_number = mara_day_for_date(la_today())
            day_name = DAY_NUMBERS[day_number]
            job = repo.ensure_job_run(calendar_date, day_number, day_name)

        workflow_status = job.get("workflow_status", "pending")
        if workflow_status == "pending" and should_run_workflow_today(now):
            _run_workflow_job(log, repo, calendar_date)
            job = repo.get_job_run(calendar_date)

        if job and job.get("telegram_status") == "sent":
            run_key = job.get("workflow_run_key")
            if run_key:
                published = repo.count_published_comments_for_run(run_key)
                if published != job.get("telegram_comments_published", 0):
                    log.info(f"Published on Reddit so far: {published} comment(s)")
                    repo.update_job_run(
                        calendar_date,
                        telegram_comments_published=published,
                    )

        _run_telegram_job(log, repo, calendar_date)
    finally:
        close_connection()


def run_daemon() -> None:
    log = _logger_for_today()
    log.section("JOB DAEMON STARTED")
    log.info(f"LA time: {la_now().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    log.info(f"Workflow trigger: 00:01 LA (daily)")
    log.info(f"Telegram window: hourly slots (see JOB_TELEGRAM_SLOT_* env vars)")
    log.info(f"Log file: {log.path}")
    log.info(f"Poll interval: {POLL_SECS}s")

    bot_thread = threading.Thread(target=run_bot, name="telegram-bot", daemon=True)
    bot_thread.start()
    log.info("Telegram button listener started (Regenerate / Done)")

    last_tick_key: tuple[int, ...] | None = None

    try:
        while True:
            now = la_now()
            tick_key = (now.year, now.month, now.day, now.hour, now.minute)
            if tick_key != last_tick_key:
                last_tick_key = tick_key
                tick(_logger_for_today())
            time.sleep(POLL_SECS)
    except KeyboardInterrupt:
        log.info("Daemon stopped by user")
    except Exception as err:
        log.error("Daemon crashed", exc=err)
        traceback.print_exc()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Reddit bot daily job daemon")
    parser.add_argument(
        "--tick",
        action="store_true",
        help="Run one scheduler tick now (workflow if 00:01, telegram if due)",
    )
    parser.add_argument(
        "--force-workflow",
        action="store_true",
        help="Run today's workflow immediately (ignore schedule/idempotency guard in tick only)",
    )
    parser.add_argument(
        "--force-telegram",
        action="store_true",
        help="Send today's Telegram handoff immediately",
    )
    args = parser.parse_args()

    if args.force_workflow:
        log = _logger_for_today()
        repo = PipelineRepository()
        calendar_date = la_calendar_date_str()
        day_number = mara_day_for_date(la_today())
        day_name = DAY_NUMBERS[day_number]
        repo.ensure_job_run(calendar_date, day_number, day_name)
        repo.update_job_run(calendar_date, workflow_status="pending")
        try:
            _run_workflow_job(log, repo, calendar_date)
        finally:
            close_connection()
        return

    if args.force_telegram:
        log = _logger_for_today()
        repo = PipelineRepository()
        calendar_date = la_calendar_date_str()
        job = repo.get_job_run(calendar_date)
        if not job or not job.get("workflow_run_key"):
            print("No completed workflow for today.", file=sys.stderr)
            sys.exit(1)
        repo.update_job_run(calendar_date, telegram_status="scheduled")
        try:
            _run_telegram_job(log, repo, calendar_date, force=True)
        finally:
            close_connection()
        return

    if args.tick:
        tick()
        return

    run_daemon()


if __name__ == "__main__":
    main()
