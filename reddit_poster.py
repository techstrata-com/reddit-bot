import os
import time

import praw
from praw.exceptions import RedditAPIException


def get_reddit_client() -> praw.Reddit:
    required = {
        "REDDIT_CLIENT_ID": os.getenv("REDDIT_CLIENT_ID"),
        "REDDIT_CLIENT_SECRET": os.getenv("REDDIT_CLIENT_SECRET"),
        "REDDIT_USERNAME": os.getenv("REDDIT_USERNAME"),
        "REDDIT_PASSWORD": os.getenv("REDDIT_PASSWORD"),
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise ValueError(f"Missing Reddit credentials in .env: {', '.join(missing)}")

    user_agent = os.getenv(
        "REDDIT_USER_AGENT",
        f"reddit-bot/1.0 (by u/{required['REDDIT_USERNAME']})",
    )

    return praw.Reddit(
        client_id=required["REDDIT_CLIENT_ID"],
        client_secret=required["REDDIT_CLIENT_SECRET"],
        username=required["REDDIT_USERNAME"],
        password=required["REDDIT_PASSWORD"],
        user_agent=user_agent,
    )


def post_comment(post_url: str, text: str) -> dict:
    """Post a comment on a Reddit submission. Returns comment id and permalink."""
    reddit = get_reddit_client()
    submission = reddit.submission(url=post_url)
    comment = submission.reply(text)
    return {
        "reddit_comment_id": comment.id,
        "reddit_comment_url": f"https://www.reddit.com{comment.permalink}",
    }


def post_comment_safe(post_url: str, text: str) -> dict:
    """Post with normalized success/error result."""
    try:
        result = post_comment(post_url, text)
        return {"ok": True, **result}
    except RedditAPIException as err:
        messages = "; ".join(f"{e.error_type}: {e.message}" for e in err.items)
        return {"ok": False, "error": messages}
    except Exception as err:
        return {"ok": False, "error": str(err)}


def post_delay_secs() -> int:
    return int(os.getenv("REDDIT_POST_DELAY_SECS", "45"))
