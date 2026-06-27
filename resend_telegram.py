#!/usr/bin/env python3
"""
Resend generated comments to Telegram that were never delivered (Phase 4 retry).

Usage:
  python resend_telegram.py                          # all unsent comments
  python resend_telegram.py --run-key 20260626_070107
  python resend_telegram.py --day 1                  # Mara day number (1=Saturday)
  python resend_telegram.py --group A
  python resend_telegram.py --dry-run                # preview only

Register a group (one-time, if auto-discovery missed it):
  python resend_telegram.py --register-chat -5391717887
  python resend_telegram.py --list-chats
"""

import argparse
import sys
import time

from dotenv import load_dotenv

from db.connection import close_connection
from db.repository import PipelineRepository
from schedule_loader import DAY_NUMBERS
from telegram_chats import (
    bootstrap_from_updates,
    chat_ids,
    get_target_chats,
    no_groups_help,
    register_chat_manual,
    sync_env_chat,
)
from telegram_notifier import send_delay_secs
from telegram_sender import send_single_comment

load_dotenv(override=True)


def _ensure_targets(repo: PipelineRepository) -> list[str]:
    sync_env_chat(repo)
    targets = chat_ids(repo)
    if targets:
        return targets

    n = bootstrap_from_updates(repo)
    if n:
        print(f"Discovered {n} group(s) from pending Telegram updates")
    return chat_ids(repo)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Force-send comments to Telegram that were generated but not delivered.",
    )
    parser.add_argument("--run-key", help="Only comments from this pipeline run_key")
    parser.add_argument("--day", type=int, choices=sorted(DAY_NUMBERS), help="Mara day number")
    parser.add_argument("--group", help="Only comments for this pipeline group (e.g. A)")
    parser.add_argument("--dry-run", action="store_true", help="List comments without sending")
    parser.add_argument(
        "--register-chat",
        metavar="CHAT_ID",
        help="Register a Telegram group by chat ID (one-time setup)",
    )
    parser.add_argument("--title", help="Group title (optional, fetched from Telegram if omitted)")
    parser.add_argument("--list-chats", action="store_true", help="List registered Telegram groups")
    args = parser.parse_args()

    repo = PipelineRepository()

    try:
        if args.list_chats:
            chats = repo.get_active_telegram_chats()
            if not chats:
                print("No groups registered.")
                print(no_groups_help())
                return 1
            for c in chats:
                print(f"  {c.get('title', '?')}  ({c['chat_id']})  type={c.get('chat_type')}")
            return 0

        if args.register_chat:
            result = register_chat_manual(repo, args.register_chat, title=args.title)
            if not result.get("ok"):
                print(f"Failed to register: {result.get('error')}", file=sys.stderr)
                return 1
            print(f"Registered: {result['title']} ({result['chat_id']})")
            return 0

        targets = _ensure_targets(repo)

        if not targets and not args.dry_run:
            print(no_groups_help(), file=sys.stderr)
            return 1

        comments = repo.get_unsent_telegram_comments(
            run_key=args.run_key,
            day_number=args.day,
            group_id=args.group,
        )

        if not comments:
            print("No unsent comments found.")
            return 0

        chats = get_target_chats(repo)
        chat_label = ", ".join(f"{c.get('title', c['chat_id'])}" for c in chats) or "(none)"

        print(f"Found {len(comments)} unsent comment(s)")
        if not args.dry_run:
            print(f"Sending to {len(targets)} Telegram group(s): {chat_label}")

        sent = 0
        failed = 0
        delay = send_delay_secs()

        for i, doc in enumerate(comments, start=1):
            title = (doc.get("post_title") or "")[:50]
            day_name = doc.get("day_name", "?")
            group_id = doc.get("group_id", "?")
            subreddit = doc.get("subreddit_name", "?")
            run_key = doc.get("run_key", "?")
            status = doc.get("publish_status", "?")

            print(
                f"\n[{i}/{len(comments)}] run={run_key} day={day_name} "
                f"group={group_id} r/{subreddit}"
            )
            print(f"  {title}... (status={status})")

            if args.dry_run:
                continue

            result = send_single_comment(repo, doc)
            if result["ok"]:
                sent += 1
                print(f"  OK → {result.get('chats', 0)} group(s)")
            else:
                failed += 1
                print(f"  FAILED: {result.get('error')}", file=sys.stderr)

            if i < len(comments) and delay > 0:
                time.sleep(delay)

        print(f"\nDone: {sent} sent, {failed} failed")
        if args.dry_run:
            print("(dry-run — nothing was sent)")
        return 0 if failed == 0 else 1

    finally:
        close_connection()


if __name__ == "__main__":
    raise SystemExit(main())
