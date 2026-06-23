"""
Telegram bot listener for Regenerate / Done inline buttons.

Run alongside (or before) sending comments:
  python3 telegram_bot.py
"""

import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from comment_generator import generate_comment, get_llm_config
from db.connection import close_connection
from db.repository import PipelineRepository
from telegram_notifier import (
    answer_callback,
    edit_handoff,
    get_updates,
    reddit_username,
    telegram_chat_id,
)

load_dotenv(override=True)

ROOT = Path(__file__).parent
STATE_PATH = ROOT / ".telegram_bot_state.json"


def _load_offset() -> int | None:
    if not STATE_PATH.exists():
        return None
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        offset = data.get("offset")
        return int(offset) if offset is not None else None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _save_offset(offset: int) -> None:
    STATE_PATH.write_text(json.dumps({"offset": offset}, indent=2), encoding="utf-8")


def _parse_callback(data: str) -> tuple[str, str] | None:
    if ":" not in data:
        return None
    action, comment_id = data.split(":", 1)
    if action not in {"regen", "done"} or not comment_id:
        return None
    return action, comment_id


def _handle_regenerate(repo: PipelineRepository, comment_id: str, callback_id: str) -> None:
    doc = repo.get_comment_by_id(comment_id)
    if not doc:
        answer_callback(callback_id, "Comment not found in database.", alert=True)
        return

    if doc.get("publish_status") == "published":
        answer_callback(callback_id, "Already marked as published.", alert=True)
        return

    prompt_text = doc.get("prompt_text", "").strip()
    if not prompt_text:
        answer_callback(callback_id, "Missing prompt for regeneration.", alert=True)
        return

    answer_callback(callback_id, "Regenerating comment...")
    started = time.perf_counter()
    new_comment = generate_comment(prompt_text)
    latency_ms = int((time.perf_counter() - started) * 1000)
    llm_provider, llm_model = get_llm_config()
    repo.update_generated_comment(
        comment_id,
        new_comment,
        latency_ms=latency_ms,
        llm_provider=llm_provider,
        llm_model=llm_model,
    )

    chat_id = doc.get("telegram_chat_id") or telegram_chat_id()
    message_id = doc.get("telegram_message_id")
    if not message_id:
        answer_callback(callback_id, "Comment saved, but Telegram message ID is missing.", alert=True)
        return

    result = edit_handoff(
        chat_id,
        message_id,
        new_comment,
        doc.get("post_url", ""),
        comment_id,
        reddit_user=doc.get("reddit_username") or reddit_username(),
    )
    if not result["ok"]:
        answer_callback(callback_id, f"Regenerated in DB, but failed to update Telegram: {result['error']}", alert=True)
        return

    answer_callback(callback_id, "Comment regenerated.")


def _handle_done(repo: PipelineRepository, comment_id: str, callback_id: str) -> None:
    doc = repo.get_comment_by_id(comment_id)
    if not doc:
        answer_callback(callback_id, "Comment not found in database.", alert=True)
        return

    if doc.get("publish_status") == "published":
        answer_callback(callback_id, "Already marked as published.")
        return

    repo.mark_comment_published(comment_id)

    chat_id = doc.get("telegram_chat_id") or telegram_chat_id()
    message_id = doc.get("telegram_message_id")
    if message_id:
        edit_handoff(
            chat_id,
            message_id,
            doc.get("generated_comment", ""),
            doc.get("post_url", ""),
            comment_id,
            published=True,
            reddit_user=doc.get("reddit_username") or reddit_username(),
        )

    answer_callback(callback_id, "Marked as published.")


def handle_callback(repo: PipelineRepository, callback: dict) -> None:
    callback_id = callback.get("id")
    data = callback.get("data", "")
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    allowed_chat = str(telegram_chat_id())

    if str(chat.get("id")) != allowed_chat:
        if callback_id:
            answer_callback(callback_id, "Unauthorized chat.", alert=True)
        return

    parsed = _parse_callback(data)
    if not parsed or not callback_id:
        return

    action, comment_id = parsed
    if action == "regen":
        _handle_regenerate(repo, comment_id, callback_id)
    elif action == "done":
        _handle_done(repo, comment_id, callback_id)


def run_bot() -> None:
    print(f"Telegram bot listening for button presses (chat {telegram_chat_id()})...", flush=True)
    print("Press Ctrl+C to stop.", flush=True)

    repo = PipelineRepository()
    offset = _load_offset()

    try:
        while True:
            result = get_updates(offset=offset, timeout=30)
            if not result["ok"]:
                print(f"getUpdates error: {result['error']}", file=sys.stderr, flush=True)
                time.sleep(3)
                continue

            for update in result.get("result") or []:
                offset = int(update["update_id"]) + 1
                _save_offset(offset)

                callback = update.get("callback_query")
                if callback:
                    handle_callback(repo, callback)
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
    finally:
        close_connection()


if __name__ == "__main__":
    run_bot()
