"""Discover and track Telegram groups where the bot is a member."""

import logging
import os

from db.repository import PipelineRepository
from telegram_notifier import get_chat_info, get_updates, telegram_chat_id

logger = logging.getLogger("reddit-bot.telegram-chats")

GROUP_TYPES = {"group", "supergroup"}


def _chat_from_update(update: dict) -> tuple[str, str, str] | None:
    """Return (chat_id, chat_title, chat_type) if update references a group."""
    chat = None

    if "my_chat_member" in update:
        chat = update["my_chat_member"].get("chat")
        member = update["my_chat_member"].get("new_chat_member") or {}
        status = member.get("status")
        if status not in {"member", "administrator"}:
            return None
    elif "message" in update:
        chat = update["message"].get("chat")
    elif "callback_query" in update:
        chat = (update["callback_query"].get("message") or {}).get("chat")

    if not chat:
        return None

    chat_type = chat.get("type", "")
    if chat_type not in GROUP_TYPES:
        return None

    chat_id = str(chat["id"])
    title = chat.get("title") or chat_id
    return chat_id, title, chat_type


def register_from_update(repo: PipelineRepository, update: dict) -> str | None:
    """Register group from a Telegram update. Returns chat_id if registered."""
    if "my_chat_member" in update:
        member = update["my_chat_member"].get("new_chat_member") or {}
        status = member.get("status")
        chat = update["my_chat_member"].get("chat") or {}
        chat_id = str(chat.get("id", ""))
        if status in {"left", "kicked"} and chat_id:
            repo.deactivate_telegram_chat(chat_id)
            logger.info("Bot removed from group %s (%s)", chat.get("title"), chat_id)
            return None

    parsed = _chat_from_update(update)
    if not parsed:
        return None

    chat_id, title, chat_type = parsed
    repo.register_telegram_chat(chat_id, title, chat_type)
    return chat_id


def register_chat_manual(
    repo: PipelineRepository,
    chat_id: str,
    title: str | None = None,
) -> dict:
    """Register a group by chat ID. Validates via Telegram getChat API."""
    info = get_chat_info(chat_id)
    if not info.get("ok"):
        return {"ok": False, "error": info.get("error", "getChat failed")}

    chat = info.get("result") or {}
    chat_type = chat.get("type", "")
    if chat_type not in GROUP_TYPES:
        return {"ok": False, "error": f"Chat {chat_id} is not a group (type={chat_type})"}

    resolved_title = title or chat.get("title") or chat_id
    repo.register_telegram_chat(str(chat_id), resolved_title, chat_type)
    return {"ok": True, "chat_id": str(chat_id), "title": resolved_title}


def sync_env_chat(repo: PipelineRepository) -> str | None:
    """If TELEGRAM_CHAT_ID is in .env, persist it to the database."""
    chat_id = telegram_chat_id()
    if not chat_id:
        return None
    result = register_chat_manual(repo, chat_id)
    if result.get("ok"):
        return result["chat_id"]
    logger.warning("Could not register TELEGRAM_CHAT_ID %s: %s", chat_id, result.get("error"))
    return None


def bootstrap_from_updates(repo: PipelineRepository) -> int:
    """Scan pending Telegram updates and register any groups found."""
    result = get_updates(
        timeout=0,
        allowed_updates=["my_chat_member", "message", "callback_query"],
    )
    if not result.get("ok"):
        logger.warning("bootstrap getUpdates failed: %s", result.get("error"))
        return 0

    found: set[str] = set()
    for update in result.get("result") or []:
        chat_id = register_from_update(repo, update)
        if chat_id:
            found.add(chat_id)

    return len(found)


def get_target_chats(repo: PipelineRepository) -> list[dict]:
    """All active group chats to send handoff messages to."""
    override = telegram_chat_id()
    if override:
        sync_env_chat(repo)
        chats = repo.get_active_telegram_chats()
        if any(c["chat_id"] == override for c in chats):
            return [c for c in chats if c["chat_id"] == override]
        return [{"chat_id": override, "title": "configured"}]

    chats = repo.get_active_telegram_chats()
    if chats:
        return chats

    bootstrap_from_updates(repo)
    return repo.get_active_telegram_chats()


def chat_ids(repo: PipelineRepository) -> list[str]:
    return [c["chat_id"] for c in get_target_chats(repo)]


def handle_chat_migration(repo: PipelineRepository, old_id: str, new_id: str) -> None:
    """Telegram upgraded a basic group to supergroup — persist the new chat ID."""
    info = get_chat_info(new_id)
    if info.get("ok"):
        chat = info.get("result") or {}
        title = chat.get("title") or new_id
        chat_type = chat.get("type") or "supergroup"
    else:
        existing = next(
            (c for c in repo.get_active_telegram_chats() if c["chat_id"] == str(old_id)),
            None,
        )
        title = (existing or {}).get("title") or str(new_id)
        chat_type = "supergroup"

    repo.migrate_telegram_chat(str(old_id), str(new_id), title=title, chat_type=chat_type)
    logger.info("Migrated Telegram chat %s -> %s (%s)", old_id, new_id, title)


def no_groups_help() -> str:
    return (
        "No Telegram groups registered.\n\n"
        "Telegram only notifies the bot about NEW events — if the bot was added "
        "before the daemon started, register the group once manually:\n\n"
        "  python resend_telegram.py --register-chat -1001234567890\n\n"
        "To find your chat ID: add @userinfobot or @RawDataBot to the group, "
        "or forward a group message to @RawDataBot.\n\n"
        "Alternatively set TELEGRAM_CHAT_ID in .env (one-time)."
    )
