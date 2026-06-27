"""Send individual Telegram handoff messages to all bot groups."""

from db.repository import PipelineRepository
from telegram_chats import chat_ids, handle_chat_migration
from telegram_notifier import handoff_context_from_doc, reddit_username, send_handoff_to_chats

def send_single_comment(repo: PipelineRepository, doc: dict) -> dict:
    comment_id = str(doc["_id"])
    post_url = doc.get("post_url", "")
    text = (doc.get("generated_comment") or "").strip()

    if not post_url:
        repo.mark_comment_failed(comment_id, "missing post_url")
        return {"ok": False, "comment_id": comment_id, "error": "missing post_url"}

    if not text:
        repo.mark_comment_failed(comment_id, "empty generated_comment")
        return {"ok": False, "comment_id": comment_id, "error": "empty generated_comment"}

    targets = chat_ids(repo)
    result = send_handoff_to_chats(
        targets,
        text,
        post_url,
        comment_id,
        reddit_user=doc.get("reddit_username") or reddit_username(),
        on_chat_migrate=lambda old, new: handle_chat_migration(repo, old, new),
        **handoff_context_from_doc(doc),
    )

    if result.get("ok"):
        repo.mark_comment_sent(
            comment_id,
            deliveries=result["deliveries"],
            reddit_username=doc.get("reddit_username") or reddit_username(),
        )
        return {
            "ok": True,
            "comment_id": comment_id,
            "chats": len(result["deliveries"]),
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
