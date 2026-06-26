"""Send individual Telegram handoff messages."""

from db.repository import PipelineRepository
from telegram_notifier import chat_id_for_group, handoff_context_from_doc, reddit_username, send_handoff


def send_single_comment(repo: PipelineRepository, doc: dict) -> dict:
    comment_id = str(doc["_id"])
    post_url = doc.get("post_url", "")
    text = (doc.get("generated_comment") or "").strip()
    group_id = doc.get("group_id", "?")

    if not post_url:
        repo.mark_comment_failed(comment_id, "missing post_url")
        return {"ok": False, "comment_id": comment_id, "error": "missing post_url"}

    if not text:
        repo.mark_comment_failed(comment_id, "empty generated_comment")
        return {"ok": False, "comment_id": comment_id, "error": "empty generated_comment"}

    chat_id = chat_id_for_group(group_id)
    result = send_handoff(
        chat_id,
        text,
        post_url,
        comment_id,
        reddit_user=doc.get("reddit_username") or reddit_username(),
        **handoff_context_from_doc(doc),
    )

    if result["ok"]:
        repo.mark_comment_sent(
            comment_id,
            telegram_message_id=result["telegram_message_id"],
            telegram_chat_id=result["telegram_chat_id"],
            reddit_username=doc.get("reddit_username") or reddit_username(),
        )
        return {
            "ok": True,
            "comment_id": comment_id,
            "message_id": result["telegram_message_id"],
            "subreddit": doc.get("subreddit_name"),
            "title": (doc.get("post_title") or "")[:60],
        }

    repo.mark_comment_failed(comment_id, result.get("error", "send failed"))
    return {
        "ok": False,
        "comment_id": comment_id,
        "error": result.get("error"),
        "subreddit": doc.get("subreddit_name"),
    }
