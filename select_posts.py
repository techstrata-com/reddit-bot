def is_non_empty(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def has_title_and_body(item: dict) -> bool:
    return is_non_empty(item.get("title")) and is_non_empty(item.get("body"))


def ranking_key(item: dict) -> tuple:
    return (
        0 if has_title_and_body(item) else 1,
        -(item.get("numberOfComments") or 0),
        -(item.get("upVotes") or 0),
    )


def select_top_posts(items: list[dict], top_n: int = 3) -> list[dict]:
    """Select top N posts across an entire group (all subreddits combined)."""
    posts = [item for item in items if item.get("dataType") == "post"]
    return sorted(posts, key=ranking_key)[:top_n]
