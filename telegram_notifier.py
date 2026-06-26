import html
import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

LA_TZ = ZoneInfo("America/Los_Angeles")
logger = logging.getLogger("reddit-bot.telegram")


def bot_token() -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set in .env")
    return token


def telegram_chat_id() -> str:
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if chat_id:
        return chat_id

    legacy = os.getenv("TELEGRAM_CHAT_IDS", "").strip()
    if not legacy:
        raise ValueError("TELEGRAM_CHAT_ID is not set in .env")

    if legacy.startswith("{"):
        raise ValueError(
            "TELEGRAM_CHAT_ID is not set. Use a single chat ID for your Telegram group, "
            "e.g. TELEGRAM_CHAT_ID=-5391717887"
        )

    return legacy


def chat_id_for_group(group_id: str) -> str:
    return telegram_chat_id()


def send_delay_secs() -> float:
    return float(os.getenv("TELEGRAM_SEND_DELAY_SECS", "1"))


def reddit_username() -> str:
    username = os.getenv("REDDIT_USERNAME", "").strip()
    if not username:
        return "unknown"
    return username if username.startswith("u/") else f"u/{username}"


def _format_published_status(
    *,
    published_by_username: str | None = None,
    published_by_name: str | None = None,
) -> str:
    if published_by_username:
        who = f"@{html.escape(published_by_username.lstrip('@'))}"
    elif published_by_name:
        who = html.escape(published_by_name)
    else:
        who = "team member"
    return f"\n\n<b>Status:</b> ✅ Published\n<b>Posted by:</b> {who}"


def handoff_context_from_doc(doc: dict) -> dict:
    return {
        "day_name": doc.get("day_name"),
        "day_number": doc.get("day_number"),
        "group_id": doc.get("group_id"),
        "group_title": doc.get("group_title"),
    }


def _format_day_group_header(
    *,
    day_name: str | None = None,
    day_number: int | None = None,
    group_id: str | None = None,
    group_title: str | None = None,
) -> str:
    lines: list[str] = []
    if day_name:
        label = f"{day_name} (day {day_number})" if day_number is not None else day_name
        lines.append(f"<b>Day:</b> {html.escape(label)}")
    if group_id:
        group_label = html.escape(str(group_id))
        if group_title:
            group_label = f"{html.escape(str(group_id))} — {html.escape(group_title)}"
        lines.append(f"<b>Group:</b> {group_label}")
    return "\n".join(lines) + "\n\n" if lines else ""


def format_handoff_message(
    comment: str,
    post_url: str,
    *,
    published: bool = False,
    reddit_user: str | None = None,
    published_by_username: str | None = None,
    published_by_name: str | None = None,
    published_at: datetime | None = None,
    day_name: str | None = None,
    day_number: int | None = None,
    group_id: str | None = None,
    group_title: str | None = None,
) -> str:
    user = reddit_user or reddit_username()
    status = ""
    if published:
        status = _format_published_status(
            published_by_username=published_by_username,
            published_by_name=published_by_name,
        )
    header = _format_day_group_header(
        day_name=day_name,
        day_number=day_number,
        group_id=group_id,
        group_title=group_title,
    )
    return (
        f"{header}"
        f"<b>Reddit account:</b> <code>{html.escape(user)}</code>\n\n"
        "<b>Comment:</b>\n"
        f"<pre>{html.escape(comment.strip())}</pre>\n\n"
        "<b>Post URL:</b>\n"
        f"<code>{html.escape(post_url.strip())}</code>"
        f"{status}"
    )


def handoff_keyboard(comment_id: str, *, published: bool = False) -> dict | None:
    if published:
        return None
    return {
        "inline_keyboard": [
            [
                {"text": "Regenerate", "callback_data": f"regen:{comment_id}"},
                {"text": "Done", "callback_data": f"done:{comment_id}"},
            ]
        ]
    }


def _telegram_api(method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{bot_token()}/{method}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", errors="replace")
        return {"ok": False, "error": f"HTTP {err.code}: {detail}"}
    except urllib.error.URLError as err:
        return {"ok": False, "error": str(err.reason)}

    if not body.get("ok"):
        return {"ok": False, "error": body.get("description", "Telegram API error")}
    return {"ok": True, "result": body.get("result")}


def ensure_polling_mode() -> dict:
    """Remove webhook so getUpdates (button callbacks) works."""
    return _telegram_api("deleteWebhook", {"drop_pending_updates": False})


def send_handoff(
    chat_id: str,
    comment: str,
    post_url: str,
    comment_id: str,
    *,
    reddit_user: str | None = None,
    day_name: str | None = None,
    day_number: int | None = None,
    group_id: str | None = None,
    group_title: str | None = None,
) -> dict:
    payload = {
        "chat_id": chat_id,
        "text": format_handoff_message(
            comment,
            post_url,
            reddit_user=reddit_user,
            day_name=day_name,
            day_number=day_number,
            group_id=group_id,
            group_title=group_title,
        ),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    keyboard = handoff_keyboard(comment_id)
    if keyboard:
        payload["reply_markup"] = keyboard

    result = _telegram_api("sendMessage", payload)
    if not result["ok"]:
        return result

    message = result["result"] or {}
    return {
        "ok": True,
        "telegram_message_id": message.get("message_id"),
        "telegram_chat_id": str(message.get("chat", {}).get("id", chat_id)),
    }


def edit_handoff(
    chat_id: str,
    message_id: int | str,
    comment: str,
    post_url: str,
    comment_id: str,
    *,
    published: bool = False,
    reddit_user: str | None = None,
    published_by_username: str | None = None,
    published_by_name: str | None = None,
    published_at: datetime | None = None,
    day_name: str | None = None,
    day_number: int | None = None,
    group_id: str | None = None,
    group_title: str | None = None,
) -> dict:
    payload = {
        "chat_id": chat_id,
        "message_id": int(message_id),
        "text": format_handoff_message(
            comment,
            post_url,
            published=published,
            reddit_user=reddit_user,
            published_by_username=published_by_username,
            published_by_name=published_by_name,
            published_at=published_at,
            day_name=day_name,
            day_number=day_number,
            group_id=group_id,
            group_title=group_title,
        ),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    keyboard = handoff_keyboard(comment_id, published=published)
    payload["reply_markup"] = keyboard if keyboard else {"inline_keyboard": []}

    return _telegram_api("editMessageText", payload)


def answer_callback(callback_query_id: str, text: str, *, alert: bool = False) -> dict:
    return _telegram_api(
        "answerCallbackQuery",
        {
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": alert,
        },
    )


def get_updates(offset: int | None = None, timeout: int = 30) -> dict:
    payload: dict = {"timeout": timeout, "allowed_updates": ["callback_query"]}
    if offset is not None:
        payload["offset"] = offset
    return _telegram_api("getUpdates", payload)
