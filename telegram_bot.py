"""
Telegram bot listener for Regenerate / Done inline buttons.

Run standalone or as part of job daemon:
  python3 telegram_bot.py
"""

import json
import logging
import sys
import time
import traceback
from pathlib import Path

from dotenv import load_dotenv

from comment_generator import generate_comment, get_llm_config
from db.connection import close_connection
from db.repository import PipelineRepository, utcnow
from telegram_notifier import (
    answer_callback,
    edit_handoff,
    ensure_polling_mode,
    get_updates,
    handoff_context_from_doc,
    reddit_username,
    telegram_chat_id,
)

load_dotenv(override=True)

ROOT = Path(__file__).parent
STATE_PATH = ROOT / ".telegram_bot_state.json"
logger = logging.getLogger("reddit-bot.telegram-bot")


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


def _telegram_user_label(from_user: dict) -> str:
    if from_user.get("username"):
        return f"@{from_user['username']}"
    parts = [from_user.get("first_name"), from_user.get("last_name")]
    name = " ".join(p for p in parts if p)
    return name or f"user {from_user.get('id', '?')}"


def _handle_regenerate(repo: PipelineRepository, comment_id: str, callback: dict) -> None:
    callback_id = callback.get("id")
    doc = repo.get_comment_by_id(comment_id)
    if not doc:
        answer_callback(callback_id, "Comment not found in database.", alert=True)
        return

    if doc.get("publish_status") == "published":
        answer_callback(callback_id, "Already published on Reddit.", alert=True)
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
        answer_callback(callback_id, "Saved in DB, but Telegram message ID is missing.", alert=True)
        return

    meta = handoff_context_from_doc(doc)
    result = edit_handoff(
        chat_id,
        message_id,
        new_comment,
        doc.get("post_url", ""),
        comment_id,
        reddit_user=doc.get("reddit_username") or reddit_username(),
        **meta,
    )
    if not result["ok"]:
        logger.error("Regenerate edit failed for %s: %s", comment_id, result.get("error"))
        answer_callback(callback_id, "Regenerated in DB, Telegram update failed.", alert=True)
        return

    logger.info("Regenerated comment %s", comment_id)
    answer_callback(callback_id, "New comment ready — copy and post on Reddit.")


def _handle_done(repo: PipelineRepository, comment_id: str, callback: dict) -> None:
    callback_id = callback.get("id")
    from_user = callback.get("from") or {}

    doc = repo.get_comment_by_id(comment_id)
    if not doc:
        answer_callback(callback_id, "Comment not found in database.", alert=True)
        return

    if doc.get("publish_status") == "published":
        who = _telegram_user_label(from_user)
        answer_callback(callback_id, f"Already marked published by {who}.")
        return

    repo.mark_comment_published(
        comment_id,
        published_by_telegram_id=from_user.get("id"),
        published_by_username=from_user.get("username"),
        published_by_name=" ".join(
            p for p in [from_user.get("first_name"), from_user.get("last_name")] if p
        ) or None,
    )

    doc = repo.get_comment_by_id(comment_id)
    chat_id = doc.get("telegram_chat_id") or telegram_chat_id()
    message_id = doc.get("telegram_message_id")
    published_at = doc.get("published_on_reddit_at") or utcnow()

    meta = handoff_context_from_doc(doc)
    if message_id:
        result = edit_handoff(
            chat_id,
            message_id,
            doc.get("generated_comment", ""),
            doc.get("post_url", ""),
            comment_id,
            published=True,
            reddit_user=doc.get("reddit_username") or reddit_username(),
            published_by_username=doc.get("published_by_username"),
            published_by_name=doc.get("published_by_name"),
            published_at=published_at,
            **meta,
        )
        if not result["ok"]:
            logger.error("Done edit failed for %s: %s", comment_id, result.get("error"))
            answer_callback(
                callback_id,
                "Marked published in DB, but could not update Telegram message.",
                alert=True,
            )
            return

    who = _telegram_user_label(from_user)
    logger.info("Comment %s marked published by %s", comment_id, who)
    answer_callback(callback_id, f"Published — recorded for {who}.")


def handle_callback(callback: dict) -> None:
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
    repo = PipelineRepository()
    try:
        if action == "regen":
            _handle_regenerate(repo, comment_id, callback)
        elif action == "done":
            _handle_done(repo, comment_id, callback)
    except Exception as err:
        logger.error("Callback error (%s %s): %s", action, comment_id, err)
        traceback.print_exc()
        answer_callback(callback_id, f"Error: {err}", alert=True)
    finally:
        close_connection()


def run_bot() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    polling = ensure_polling_mode()
    if not polling.get("ok"):
        logger.error("deleteWebhook failed: %s", polling.get("error"))
    else:
        logger.info("Telegram polling mode active (webhook cleared)")

    logger.info("Listening for button presses in chat %s", telegram_chat_id())

    offset = _load_offset()

    while True:
        try:
            result = get_updates(offset=offset, timeout=30)
            if not result["ok"]:
                logger.error("getUpdates error: %s", result.get("error"))
                time.sleep(3)
                continue

            for update in result.get("result") or []:
                offset = int(update["update_id"]) + 1
                _save_offset(offset)

                callback = update.get("callback_query")
                if callback:
                    handle_callback(callback)
        except Exception as err:
            logger.error("Bot loop error: %s", err)
            traceback.print_exc()
            time.sleep(5)


if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
