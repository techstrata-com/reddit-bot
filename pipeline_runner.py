"""Programmatic pipeline execution for the daily job and CLI."""

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from apify_client import ApifyClient
from dotenv import load_dotenv

from app import process_group, process_group_send_telegram
from db.connection import close_connection
from db.repository import PipelineRepository
from schedule_loader import SCHEDULE_PATH, get_groups_for_day

load_dotenv(override=True)

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "actor_config.json"
SCHEDULE_FILE = Path(os.getenv("MARA_SCHEDULE_PATH", SCHEDULE_PATH))


@dataclass
class WorkflowResult:
    success: bool
    run_key: str | None = None
    pipeline_run_id: str | None = None
    day_number: int | None = None
    day_name: str | None = None
    group_ids: list[str] = field(default_factory=list)
    duration_secs: float = 0.0
    error: str | None = None
    skipped: bool = False
    skip_reason: str | None = None


@dataclass
class TelegramSendResult:
    success: bool
    sent: int = 0
    failed: int = 0
    duration_secs: float = 0.0
    error: str | None = None


def run_workflow(day_number: int, top_n: int = 3) -> WorkflowResult:
    started = time.perf_counter()
    day_name, groups = get_groups_for_day(SCHEDULE_FILE, day_number)

    if not groups:
        return WorkflowResult(
            success=True,
            skipped=True,
            skip_reason=f"{day_name} is REST — no groups scheduled",
            day_number=day_number,
            day_name=day_name,
            duration_secs=time.perf_counter() - started,
        )

    if not os.getenv("APIFY_API_TOKEN"):
        return WorkflowResult(
            success=False,
            day_number=day_number,
            day_name=day_name,
            error="APIFY_API_TOKEN is not set",
            duration_secs=time.perf_counter() - started,
        )

    repo = PipelineRepository()
    run_key = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    try:
        with CONFIG_PATH.open(encoding="utf-8") as f:
            base_config = json.load(f)

        pipeline_run_id = repo.create_run(
            run_key=run_key,
            day_number=day_number,
            day_name=day_name,
            top_n=top_n,
            group_ids=[g.id for g in groups],
            config_snapshot=base_config,
        )

        client = ApifyClient(os.getenv("APIFY_API_TOKEN"))
        for group in groups:
            process_group(
                repo,
                pipeline_run_id,
                run_key,
                day_number,
                day_name,
                group,
                top_n,
                client=client,
                base_config=base_config,
                skip_comments=False,
                post_comments=False,
            )

        repo.finish_run(pipeline_run_id)
        return WorkflowResult(
            success=True,
            run_key=run_key,
            pipeline_run_id=pipeline_run_id,
            day_number=day_number,
            day_name=day_name,
            group_ids=[g.id for g in groups],
            duration_secs=time.perf_counter() - started,
        )
    except Exception as err:
        try:
            run_doc = repo.get_run_by_key(run_key)
            if run_doc:
                repo.fail_run(str(run_doc["_id"]), str(err))
        except Exception:
            pass
        return WorkflowResult(
            success=False,
            run_key=run_key,
            day_number=day_number,
            day_name=day_name,
            error=str(err),
            duration_secs=time.perf_counter() - started,
        )
    finally:
        close_connection()


def send_telegram_for_run(run_key: str, group_ids: list[str] | None = None) -> TelegramSendResult:
    started = time.perf_counter()
    repo = PipelineRepository()

    try:
        run_doc = repo.get_run_by_key(run_key)
        if not run_doc:
            return TelegramSendResult(success=False, error=f"Run not found: {run_key}")

        pipeline_run_id = str(run_doc["_id"])
        _, all_groups = get_groups_for_day(SCHEDULE_FILE, run_doc["day_number"])
        groups_by_id = {g.id: g for g in all_groups}

        if not group_ids:
            group_ids = run_doc.get("group_ids") or list(
                repo.comments.distinct("group_id", {"run_key": run_key})
            )

        total_sent = 0

        for gid in group_ids:
            group = groups_by_id.get(gid)
            if not group:
                from schedule_loader import build_groups

                group = build_groups(SCHEDULE_FILE).get(gid)
            if not group:
                continue

            before_sent = repo.comments.count_documents({
                "run_key": run_key,
                "group_id": gid,
                "publish_status": "sent",
            })
            process_group_send_telegram(repo, pipeline_run_id, group)
            after_sent = repo.comments.count_documents({
                "run_key": run_key,
                "group_id": gid,
                "publish_status": "sent",
            })
            total_sent += after_sent - before_sent

        total_failed = repo.comments.count_documents({
            "run_key": run_key,
            "publish_status": "failed",
        })

        return TelegramSendResult(
            success=total_sent > 0 or total_failed == 0,
            sent=total_sent,
            failed=total_failed,
            duration_secs=time.perf_counter() - started,
        )
    except Exception as err:
        return TelegramSendResult(
            success=False,
            error=str(err),
            duration_secs=time.perf_counter() - started,
        )
    finally:
        close_connection()
