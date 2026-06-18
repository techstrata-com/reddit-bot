"""
Test Selenium login + comment posting for one Reddit post.

Usage:
  python3 test_reddit_comment.py --dry-run   # login + find comment box only
  python3 test_reddit_comment.py --post      # actually post the comment
"""

import argparse
import sys

from dotenv import load_dotenv

from reddit_browser_poster import BrowserPoster
from reddit_poster import post_comment_safe

POST_URL = (
    "https://www.reddit.com/r/FuckAI/comments/1pkc7op/"
    "small_business_owners_using_ai_advertising/"
)
COMMENT_TEXT = (
    "the restaurant one especially makes me insane. if your actual food can't be "
    "photographed, why am I supposed to trust the fake shiny AI plate instead lol\n\n"
    "and yeah, when a small business does it, it feels extra bleak because they "
    "absolutely know what it means to have your work undervalued."
)


def run_dry_run() -> int:
    print(f"Post URL: {POST_URL}")
    print("Mode: dry-run (login + comment box check, no posting)")
    try:
        with BrowserPoster() as browser:
            browser.verify_post(POST_URL)
        print("\nOK — login and comment box look good.")
        return 0
    except Exception as err:
        print(f"\nFAILED: {err}", file=sys.stderr)
        return 1


def run_post() -> int:
    print(f"Post URL: {POST_URL}")
    print(f"Comment ({len(COMMENT_TEXT)} chars):")
    print("-" * 40)
    print(COMMENT_TEXT)
    print("-" * 40)
    print("Mode: live post")

    result = post_comment_safe(POST_URL, COMMENT_TEXT)
    if result["ok"]:
        print("\nOK — comment posted.")
        print(f"URL: {result.get('reddit_comment_url')}")
        return 0

    print(f"\nFAILED: {result['error']}", file=sys.stderr)
    return 1


def main() -> None:
    load_dotenv(override=True)

    parser = argparse.ArgumentParser(description="Test Reddit Selenium comment posting")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="Login and verify the comment box without posting",
    )
    group.add_argument(
        "--post",
        action="store_true",
        help="Login and post the test comment",
    )
    args = parser.parse_args()

    if args.dry_run:
        raise SystemExit(run_dry_run())
    raise SystemExit(run_post())


if __name__ == "__main__":
    main()
