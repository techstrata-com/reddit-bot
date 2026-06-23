import html
import json
import os
import urllib.error
import urllib.request


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


def format_handoff_message(
    comment: str,
    post_url: str,
    *,
    published: bool = False,
    reddit_user: str | None = None,
) -> str:
    user = reddit_user or reddit_username()
    status = "\n\n✅ <b>Published</b>" if published else ""
    return (
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


def send_handoff(
    chat_id: str,
    comment: str,
    post_url: str,
    comment_id: str,
    *,
    reddit_user: str | None = None,
) -> dict:
    payload = {
        "chat_id": chat_id,
        "text": format_handoff_message(comment, post_url, reddit_user=reddit_user),
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
) -> dict:
    payload = {
        "chat_id": chat_id,
        "message_id": int(message_id),
        "text": format_handoff_message(
            comment, post_url, published=published, reddit_user=reddit_user
        ),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    keyboard = handoff_keyboard(comment_id, published=published)
    if keyboard:
        payload["reply_markup"] = keyboard
    else:
        payload["reply_markup"] = {"inline_keyboard": []}

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
