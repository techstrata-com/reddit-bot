"""
Daily job daemon: workflow at 00:01 LA, then send all comments to Telegram at once.

Also runs the Telegram button listener in a background thread.
"""

import argparse
import os
import sys
import threading
import time
import traceback

from dotenv import load_dotenv

from db.connection import close_connection
from db.repository import PipelineRepository, utcnow
from job.logging import DailyJobLogger
from job.schedule import la_calendar_date_str, la_now, la_today, mara_day_for_date, should_run_workflow_today
from pipeline_runner import run_workflow, send_telegram_for_run
from schedule_loader import DAY_NUMBERS
from telegram_bot import run_bot

load_dotenv(override=True)

TOP_N = int(os.getenv("JOB_TOP_N", "3"))
POLL_SECS = float(os.getenv("JOB_POLL_SECS", "30"))


def _logger_for_today() -> DailyJobLogger:
    return DailyJobLogger(la_calendar_date_str())


def _send_telegram_for_job(
    log: DailyJobLogger,
    repo: PipelineRepository,
    calendar_date: str,
    run_key: str,
    group_ids: list[str],
) -> None:
    pending = repo.count_run_telegram_pending(run_key)
    if pending == 0:
        log.info("No pending comments to send to Telegram")
        return

    log.section("TELEGRAM HANDOFF START")
    log.info(f"Run key: {run_key}")
    log.info(f"Sending {pending} comment(s) to Telegram (all at once)...")

    repo.update_job_run(
        calendar_date,
        telegram_status="running",
        telegram_started_at=utcnow(),
    )

    result = send_telegram_for_run(run_key, group_ids)

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
    log.info("Team can post on Reddit whenever ready — tap Done when published")

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

    repo.update_job_run(calendar_date, workflow_status="running", workflow_started_at=utcnow())

    result = run_workflow(day_number, TOP_N)

    if result.skipped:
        log.info(f"Workflow skipped: {result.skip_reason}")
        repo.update_job_run(
            calendar_date,
            workflow_status="skipped",
            workflow_finished_at=utcnow(),
            workflow_duration_secs=round(result.duration_secs, 2),
            telegram_status="skipped",
        )
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

    log.info(f"Workflow completed in {result.duration_secs:.1f}s")
    log.info(f"Run key: {result.run_key}")
    log.info(f"Groups: {result.group_ids}")

    repo.update_job_run(
        calendar_date,
        workflow_status="completed",
        workflow_run_key=result.run_key,
        workflow_finished_at=utcnow(),
        workflow_duration_secs=round(result.duration_secs, 2),
        workflow_groups=result.group_ids,
        workflow_error=None,
        telegram_status="pending",
    )
    log.section("DAILY WORKFLOW COMPLETE")

    _send_telegram_for_job(log, repo, calendar_date, result.run_key, result.group_ids)


def _sync_published_count(log: DailyJobLogger, repo: PipelineRepository, calendar_date: str) -> None:
    job = repo.get_job_run(calendar_date)
    if not job or not job.get("workflow_run_key"):
        return

    run_key = job["workflow_run_key"]
    published = repo.count_run_telegram_published(run_key)
    if published != job.get("telegram_comments_published", 0):
        log.info(f"Published on Reddit: {published} comment(s) (run {run_key})")
        repo.update_job_run(calendar_date, telegram_comments_published=published)


def _retry_pending_telegram(log: DailyJobLogger, repo: PipelineRepository, calendar_date: str) -> None:
    """Catch-up if workflow finished but Telegram send did not complete."""
    job = repo.get_job_run(calendar_date)
    if not job or job.get("workflow_status") != "completed":
        return

    run_key = job.get("workflow_run_key")
    if not run_key:
        return

    if job.get("telegram_status") in {"sent", "running"}:
        return

    pending = repo.count_run_telegram_pending(run_key)
    if pending == 0:
        return

    log.warning(f"Retrying Telegram send — {pending} comment(s) still pending")
    _send_telegram_for_job(log, repo, calendar_date, run_key, job.get("workflow_groups") or [])


def tick(log: DailyJobLogger | None = None, *, close_db: bool = True) -> None:
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

        if job.get("workflow_status", "pending") == "pending" and should_run_workflow_today(now):
            _run_workflow_job(log, repo, calendar_date)
        else:
            _retry_pending_telegram(log, repo, calendar_date)

        _sync_published_count(log, repo, calendar_date)
    finally:
        if close_db:
            close_connection()


def run_daemon() -> None:
    log = _logger_for_today()
    log.section("JOB DAEMON STARTED")
    log.info(f"LA time: {la_now().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    log.info("Workflow trigger: 00:01 LA (daily)")
    log.info("Telegram: all comments sent together when workflow finishes")
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
                tick(_logger_for_today(), close_db=False)
            time.sleep(POLL_SECS)
    except KeyboardInterrupt:
        log.info("Daemon stopped by user")
    except Exception as err:
        log.error("Daemon crashed", exc=err)
        traceback.print_exc()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Reddit bot daily job daemon")
    parser.add_argument("--tick", action="store_true", help="Run one scheduler tick now")
    parser.add_argument("--force-workflow", action="store_true", help="Run today's workflow now")
    parser.add_argument(
        "--force-telegram",
        action="store_true",
        help="Send all pending Telegram messages for today's run now",
    )
    args = parser.parse_args()

    if args.force_workflow:
        log = _logger_for_today()
        repo = PipelineRepository()
        calendar_date = la_calendar_date_str()
        day_number = mara_day_for_date(la_today())
        day_name = DAY_NUMBERS[day_number]
        repo.ensure_job_run(calendar_date, day_number, day_name)
        repo.update_job_run(calendar_date, workflow_status="pending", telegram_status="pending")
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
        try:
            repo.update_job_run(calendar_date, telegram_status="pending")
            _send_telegram_for_job(
                log,
                repo,
                calendar_date,
                job["workflow_run_key"],
                job.get("workflow_groups") or [],
            )
        finally:
            close_connection()
        return

    if args.tick:
        tick()
        return

    run_daemon()


if __name__ == "__main__":
    main()
