import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from apify_client import ApifyClient
from dotenv import load_dotenv

from comment_generator import generate_comment, get_llm_config, render_prompt
from db.connection import close_connection, ssh_enabled
from db.repository import PipelineRepository
from schedule_loader import (
    DAY_NUMBERS,
    SCHEDULE_PATH,
    Group,
    build_groups,
    format_subreddit_rules,
    get_groups_for_day,
    subreddit_context_for_post,
)
from scraper import scrape_subreddit
from telegram_notifier import chat_id_for_group, reddit_username, send_delay_secs, send_handoff
from select_posts import select_top_posts

load_dotenv(override=True)

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "actor_config.json"
SCHEDULE_FILE = Path(os.getenv("MARA_SCHEDULE_PATH", SCHEDULE_PATH))


def scrape_all_subreddits(client: ApifyClient, base_config: dict, group: Group):
    for i, subreddit in enumerate(group.subreddits, start=1):
        print(f"  [{i}/{len(group.subreddits)}] scraping {subreddit.name}...")
        result = scrape_subreddit(
            client, base_config, subreddit.url, subreddit_name=subreddit.name
        )
        print(f"  [{i}/{len(group.subreddits)}] {subreddit.name}: {len(result['items'])} posts")
        yield subreddit, result


def _post_from_selected(doc: dict) -> dict:
    """Rebuild post dict for comment generation from a selected_posts document."""
    if doc.get("raw"):
        return doc["raw"]
    return {
        "id": doc.get("reddit_post_id"),
        "communityName": doc.get("subreddit_name"),
        "title": doc.get("title"),
        "body": doc.get("body"),
        "url": doc.get("url"),
        "upVotes": doc.get("upvotes"),
        "numberOfComments": doc.get("comment_count"),
    }


def generate_comments(
    repo: PipelineRepository,
    pipeline_run_id: str,
    group_run_id: str,
    run_key: str,
    day_number: int,
    day_name: str,
    group: Group,
    posts: list[dict],
) -> int:
    repo.set_group_phase(group_run_id, "generating_comments")
    repo.set_run_phase(pipeline_run_id, f"comments_group_{group.id}")

    llm_provider, llm_model = get_llm_config()
    generated = 0

    for post in posts:
        reddit_post_id = post.get("id") or post.get("parsedId") or post.get("reddit_post_id")
        if repo.has_comment(pipeline_run_id, reddit_post_id):
            print(f"  skip [{reddit_post_id}] - comment already exists")
            continue

        subreddit_name, subreddit_rules = subreddit_context_for_post(post, group)
        title = (post.get("title") or "")[:70]
        print(f"  generating comment for [{subreddit_name}] {title}...")

        prompt_input = {
            "subreddit_name": subreddit_name,
            "subreddit_rules": format_subreddit_rules(subreddit_rules),
            "post_title": post.get("title", ""),
            "post_body": post.get("body", ""),
            "upvote_count": post.get("upVotes") or post.get("upvotes"),
            "comment_count": post.get("numberOfComments") or post.get("comment_count"),
        }
        prompt_text = render_prompt(
            subreddit_name=subreddit_name,
            subreddit_rules=subreddit_rules,
            post_title=post.get("title", ""),
            post_body=post.get("body", ""),
            upvote_count=prompt_input["upvote_count"],
            comment_count=prompt_input["comment_count"],
        )

        started = time.perf_counter()
        generated_comment = generate_comment(prompt_text)
        latency_ms = int((time.perf_counter() - started) * 1000)

        repo.insert_comment(
            pipeline_run_id=pipeline_run_id,
            group_run_id=group_run_id,
            run_key=run_key,
            day_number=day_number,
            day_name=day_name,
            group_id=group.id,
            reddit_post_id=reddit_post_id,
            subreddit_name=subreddit_name,
            post_url=post.get("url", ""),
            post_title=post.get("title", ""),
            prompt_input=prompt_input,
            prompt_text=prompt_text,
            generated_comment=generated_comment,
            llm_provider=llm_provider,
            llm_model=llm_model,
            latency_ms=latency_ms,
            upvotes=prompt_input["upvote_count"],
            comment_count=prompt_input["comment_count"],
        )
        repo.increment_stat(pipeline_run_id, "comments")
        generated += 1
        print(f"  OK comment saved ({latency_ms}ms)")

    return generated


def process_group_comments_only(
    repo: PipelineRepository,
    pipeline_run_id: str,
    run_key: str,
    day_number: int,
    day_name: str,
    group: Group,
) -> None:
    selected_docs = repo.get_selected_posts(pipeline_run_id, group.id)
    if not selected_docs:
        raise ValueError(f"No selected_posts found for group {group.id} in run {run_key}")

    group_run = repo.get_group_run(pipeline_run_id, group.id)
    group_run_id = str(group_run["_id"]) if group_run else str(selected_docs[0]["group_run_id"])

    posts = [_post_from_selected(doc) for doc in selected_docs]
    print(f"  loaded {len(posts)} selected posts from DB")

    count = generate_comments(
        repo, pipeline_run_id, group_run_id, run_key, day_number, day_name, group, posts
    )
    print(f"  Phase 3 complete: {count} comments saved to comments collection")


def process_group_send_telegram(
    repo: PipelineRepository,
    pipeline_run_id: str,
    group: Group,
    *,
    dry_run: bool = False,
) -> None:
    comments = repo.get_unsent_comments(pipeline_run_id, group.id)
    if not comments:
        print(f"  no pending comments for group {group.id}")
        return

    try:
        chat_id = chat_id_for_group(group.id)
    except ValueError as err:
        raise ValueError(f"Group {group.id}: {err}") from err

    delay = send_delay_secs()
    sent = 0
    failed = 0
    pending: list[tuple[int, str, str, str, str, str]] = []

    for i, doc in enumerate(comments, start=1):
        post_url = doc.get("post_url")
        text = doc.get("generated_comment", "").strip()
        title = (doc.get("post_title") or "")[:60]
        comment_id = str(doc["_id"])

        if not post_url:
            print(f"  [{i}/{len(comments)}] skipped - missing post_url")
            repo.mark_comment_failed(comment_id, "missing post_url")
            failed += 1
            continue

        if not text:
            print(f"  [{i}/{len(comments)}] skipped - empty comment")
            repo.mark_comment_failed(comment_id, "empty generated_comment")
            failed += 1
            continue

        if dry_run:
            print(f"  [{i}/{len(comments)}] r/{doc.get('subreddit_name')} - {title}...")
            print(f"    (dry-run) would send to Telegram chat {chat_id}")
            print(f"    comment ({len(text)} chars) + URL: {post_url}")
            continue

        pending.append((i, comment_id, post_url, text, doc.get("subreddit_name"), title))

    if dry_run or not pending:
        print(f"  Phase 4 complete: {sent} sent, {failed} failed")
        return

    repo.set_run_phase(pipeline_run_id, f"telegram_group_{group.id}")
    print(f"  sending {len(pending)} comment(s) to Telegram (chat {chat_id})...")

    for idx, (i, comment_id, post_url, text, subreddit, title) in enumerate(pending):
        print(f"  [{i}/{len(comments)}] r/{subreddit} - {title}...")
        result = send_handoff(
            chat_id, text, post_url, comment_id, reddit_user=reddit_username()
        )
        if result["ok"]:
            repo.mark_comment_sent(
                comment_id,
                telegram_message_id=result["telegram_message_id"],
                telegram_chat_id=result["telegram_chat_id"],
                reddit_username=reddit_username(),
            )
            sent += 1
            print(f"    OK sent (message_id={result['telegram_message_id']})")
        else:
            repo.mark_comment_failed(comment_id, result["error"])
            failed += 1
            print(f"    ERROR {result['error']}")

        if idx < len(pending) - 1 and delay > 0:
            print(f"    waiting {delay}s before next message...")
            time.sleep(delay)

    print(f"  Phase 4 complete: {sent} sent, {failed} failed")


def process_group(
    repo: PipelineRepository,
    pipeline_run_id: str,
    run_key: str,
    day_number: int,
    day_name: str,
    group: Group,
    top_n: int,
    *,
    client: ApifyClient,
    base_config: dict,
    skip_comments: bool,
    post_comments: bool = False,
    dry_run_telegram: bool = False,
) -> None:
    group_run_id = repo.create_group_run(
        pipeline_run_id,
        run_key=run_key,
        day_number=day_number,
        day_name=day_name,
        group_id=group.id,
        group_title=group.title,
        subreddit_names=[s.name for s in group.subreddits],
    )

    try:
        repo.set_group_phase(group_run_id, "scraping")
        repo.set_run_phase(pipeline_run_id, f"scraping_group_{group.id}")

        all_items: list[dict] = []
        total_saved = 0

        for subreddit, result in scrape_all_subreddits(client, base_config, group):
            saved = repo.save_raw_posts(
                pipeline_run_id=pipeline_run_id,
                group_run_id=group_run_id,
                run_key=run_key,
                day_number=day_number,
                day_name=day_name,
                group_id=group.id,
                group_title=group.title,
                subreddit_name=subreddit.name,
                items=result["items"],
                apify_run_id=result["apify_run_id"],
            )
            total_saved += saved
            all_items.extend(result["items"])
            print(f"  OK saved {saved} raw posts to DB (total so far: {total_saved})")

        repo.increment_stat(pipeline_run_id, "raw_posts", total_saved)
        print(f"\n  Phase 1 complete: {len(all_items)} posts scraped, {total_saved} saved to raw_posts")

        repo.set_group_phase(group_run_id, "selecting")
        repo.set_run_phase(pipeline_run_id, f"selecting_group_{group.id}")

        top_posts = select_top_posts(all_items, top_n)
        selected_count = repo.save_selected_posts(
            pipeline_run_id=pipeline_run_id,
            group_run_id=group_run_id,
            run_key=run_key,
            day_number=day_number,
            day_name=day_name,
            group_id=group.id,
            group_title=group.title,
            top_n=top_n,
            selected=top_posts,
        )
        repo.increment_stat(pipeline_run_id, "selected_posts", selected_count)
        repo.increment_stat(pipeline_run_id, "groups_completed")
        print(f"  Phase 2 complete: top {selected_count} saved to selected_posts")

        for rank, post in enumerate(top_posts, start=1):
            title = (post.get("title") or "")[:60]
            comments = post.get("numberOfComments", 0)
            upvotes = post.get("upVotes", 0)
            sub = post.get("communityName", "?")
            print(f"    #{rank} [{sub}] {title}... ({comments} comments, {upvotes} upvotes)")

        if skip_comments:
            repo.finish_group_run(group_run_id, raw_count=total_saved, selected_count=selected_count)
            return

        generate_comments(
            repo, pipeline_run_id, group_run_id, run_key, day_number, day_name, group, top_posts
        )
        repo.finish_group_run(group_run_id, raw_count=total_saved, selected_count=selected_count)

        if post_comments:
            print(f"\n  --- Phase 4: send to Telegram ---")
            process_group_send_telegram(
                repo,
                pipeline_run_id,
                group,
                dry_run=dry_run_telegram,
            )

    except Exception as err:
        repo.fail_group_run(group_run_id, str(err))
        raise


def filter_groups(groups: list[Group], group_ids: list[str] | None) -> list[Group]:
    if not group_ids:
        return groups
    allowed = {g.upper() for g in group_ids}
    return [g for g in groups if g.id.upper() in allowed]


def main() -> None:
    day_help = ", ".join(f"{n}={name}" for n, name in DAY_NUMBERS.items())
    parser = argparse.ArgumentParser(
        description="Scrape -> rank -> save to MongoDB (raw_posts + selected_posts).",
        epilog=f"Day numbers: {day_help}. Monday (3) is REST.",
    )
    parser.add_argument("day", type=int, choices=sorted(DAY_NUMBERS), help="Weekday number")
    parser.add_argument("top_n", type=int, nargs="?", default=3, help="Top posts per group (default: 3)")
    parser.add_argument("--skip-comments", action="store_true", help="Stop after scrape + select")
    parser.add_argument("--comments-only", action="store_true", help="Generate comments from existing selected_posts")
    parser.add_argument("--send-telegram", action="store_true", help="Also send comments to Telegram (Phase 4)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --send-telegram: show what would be sent without sending",
    )
    parser.add_argument("--resume", metavar="RUN_KEY", help="Resume an existing run (e.g. 20260608_122706)")
    parser.add_argument("--group", action="append", dest="groups", help="Process only specific group(s), e.g. --group A")
    args = parser.parse_args()

    if not ssh_enabled() and not os.getenv("MONGODB_URI"):
        raise ValueError("Set MONGODB_URI or enable MONGODB_SSH_ENABLED in .env")

    day_name, groups = get_groups_for_day(SCHEDULE_FILE, args.day)
    if not groups and not args.comments_only:
        print(f"{day_name} is REST - no groups scheduled.")
        return

    groups = filter_groups(groups, args.groups)
    all_groups_map = build_groups(SCHEDULE_FILE)

    print("Connecting to MongoDB...", flush=True)
    repo = PipelineRepository()
    print("MongoDB connection ready.", flush=True)

    if args.resume:
        run_doc = repo.get_run_by_key(args.resume)
        if not run_doc:
            raise ValueError(f"Run not found: {args.resume}")
        pipeline_run_id = str(run_doc["_id"])
        run_key = run_doc["run_key"]
        day_number = run_doc["day_number"]
        day_name = run_doc["day_name"]
        print(f"Resuming run: {run_key} (day {day_number} / {day_name})")
    else:
        if args.comments_only:
            raise ValueError("--comments-only requires --resume RUN_KEY")
        if not os.getenv("APIFY_API_TOKEN"):
            raise ValueError("APIFY_API_TOKEN is not set in .env")
        run_key = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        with CONFIG_PATH.open(encoding="utf-8") as f:
            base_config = json.load(f)
        pipeline_run_id = repo.create_run(
            run_key=run_key,
            day_number=args.day,
            day_name=day_name,
            top_n=args.top_n,
            group_ids=[g.id for g in groups],
            config_snapshot=base_config,
        )

    if args.send_telegram and args.resume and not args.comments_only:
        if not groups:
            group_ids_in_db = repo.comments.distinct("group_id", {"run_key": run_key})
            groups = [all_groups_map[gid] for gid in group_ids_in_db if gid in all_groups_map]

        groups = filter_groups(groups, args.groups)
        if not groups:
            raise ValueError("No groups to process")

        mode = "dry-run" if args.dry_run else "live"
        print(f"Send to Telegram only ({mode}) - groups: {[g.id for g in groups]}")

        try:
            for group in groups:
                print(f"\n{'='*50}")
                print(f"GROUP [{group.id}] {group.title} - send to Telegram")
                print(f"{'='*50}")
                process_group_send_telegram(
                    repo,
                    pipeline_run_id,
                    group,
                    dry_run=args.dry_run,
                )
            print(f"\nDone. Query: db.comments.find({{run_key:\"{run_key}\"}})")
        finally:
            close_connection()
        return

    if args.send_telegram and args.skip_comments:
        raise ValueError("--send-telegram cannot be used with --skip-comments")

    if args.comments_only:
        if not groups:
            # resolve groups from selected_posts in this run
            group_ids_in_db = repo.selected_posts.distinct(
                "group_id", {"run_key": run_key}
            )
            groups = [all_groups_map[gid] for gid in group_ids_in_db if gid in all_groups_map]

        groups = filter_groups(groups, args.groups)
        if not groups:
            raise ValueError("No groups to process")

        print(f"Comments only - groups: {[g.id for g in groups]}")

        try:
            for group in groups:
                print(f"\n{'='*50}")
                print(f"GROUP [{group.id}] {group.title} - generate comments")
                print(f"{'='*50}")
                process_group_comments_only(
                    repo, pipeline_run_id, run_key, day_number, day_name, group
                )
            print(f"\nDone. Query: db.comments.find({{run_key:\"{run_key}\"}})")
        finally:
            close_connection()
        return

    client = ApifyClient(os.getenv("APIFY_API_TOKEN"))
    with CONFIG_PATH.open(encoding="utf-8") as f:
        base_config = json.load(f)

    phases = "scrape -> select"
    if not args.skip_comments:
        phases += " -> generate"
        if args.send_telegram:
            phases += " -> telegram"
    print(f"Day {args.day} ({day_name}) - groups {[g.id for g in groups]} - top {args.top_n}/group")
    print(f"Pipeline: {phases}")
    print(f"Run: {run_key}")

    try:
        for group in groups:
            print(f"\n{'='*50}")
            print(f"GROUP [{group.id}] {group.title} - {len(group.subreddits)} subreddits")
            print(f"{'='*50}")
            process_group(
                repo,
                pipeline_run_id,
                run_key,
                args.day,
                day_name,
                group,
                args.top_n,
                client=client,
                base_config=base_config,
                skip_comments=args.skip_comments,
                post_comments=args.send_telegram,
                dry_run_telegram=args.dry_run,
            )

        repo.finish_run(pipeline_run_id)
        print(f"\nDone. Query MongoDB:")
        print(f'  db.raw_posts.find({{run_key:"{run_key}"}})')
        print(f'  db.selected_posts.find({{run_key:"{run_key}"}})')

    except Exception as err:
        repo.fail_run(pipeline_run_id, str(err))
        raise
    finally:
        close_connection()


if __name__ == "__main__":
    main()
