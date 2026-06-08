import os

from reddit_browser_poster import BrowserPoster, interactive_login


def post_comment_safe(post_url: str, text: str, *, browser: BrowserPoster | None = None) -> dict:
    try:
        if browser is None:
            with BrowserPoster() as session:
                result = session.post(post_url, text)
        else:
            result = browser.post(post_url, text)
        return {"ok": True, **result}
    except Exception as err:
        return {"ok": False, "error": str(err)}


def post_delay_secs() -> int:
    return int(os.getenv("REDDIT_POST_DELAY_SECS", "45"))


def reddit_login() -> None:
    interactive_login()
