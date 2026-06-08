import os
import re
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page, sync_playwright

ROOT = Path(__file__).parent
DEFAULT_SESSION_DIR = ROOT / ".reddit_browser_session"


def session_dir() -> Path:
    return Path(os.getenv("REDDIT_BROWSER_SESSION_DIR", DEFAULT_SESSION_DIR))


def headless() -> bool:
    return os.getenv("REDDIT_BROWSER_HEADLESS", "true").lower() in ("1", "true", "yes")


def to_old_reddit(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.replace("www.", "")
    if host != "reddit.com":
        return url
    return parsed._replace(netloc="old.reddit.com").geturl()


def _credentials() -> tuple[str, str]:
    username = os.getenv("REDDIT_USERNAME", "").strip()
    password = os.getenv("REDDIT_PASSWORD", "").strip()
    if not username or not password:
        raise ValueError("Set REDDIT_USERNAME and REDDIT_PASSWORD in .env")
    return username, password


def is_logged_in(page: Page) -> bool:
    page.goto("https://old.reddit.com/", wait_until="domcontentloaded", timeout=60000)
    return page.locator("#header-bottom-right .user").count() > 0


def login_with_password(page: Page) -> None:
    username, password = _credentials()
    page.goto("https://old.reddit.com/login", wait_until="domcontentloaded", timeout=60000)
    page.fill("#user_login", username)
    page.fill("#passwd_login", password)
    page.click("form.login-form button[type='submit']")
    page.wait_for_load_state("networkidle", timeout=60000)

    if not is_logged_in(page):
        raise RuntimeError(
            "Reddit login failed. If 2FA or captcha is enabled, run: "
            "python3 app.py 1 --reddit-login"
        )


def ensure_logged_in(page: Page) -> None:
    if not is_logged_in(page):
        login_with_password(page)


def interactive_login() -> None:
    """Open a visible browser so the user can log in manually (2FA/captcha safe)."""
    path = session_dir()
    path.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(path),
            headless=False,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://old.reddit.com/login", wait_until="domcontentloaded")
        print("\nBrowser opened. Log in to Reddit (complete 2FA if asked).")
        print("When you see your username in the top-right, press Enter here...")
        input()
        if not is_logged_in(page):
            raise RuntimeError("Still not logged in — try again.")
        print(f"Session saved in {path}")
        context.close()


class BrowserPoster:
    """Reuse one browser session for multiple comment posts."""

    def __init__(self) -> None:
        self._playwright = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    def __enter__(self) -> "BrowserPoster":
        path = session_dir()
        path.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(path),
            headless=headless(),
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        ensure_logged_in(self._page)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._context:
            self._context.close()
        if self._playwright:
            self._playwright.stop()

    def post(self, post_url: str, text: str) -> dict:
        page = self._page
        assert page is not None

        old_url = to_old_reddit(post_url)
        page.goto(old_url, wait_until="domcontentloaded", timeout=60000)

        textarea = page.locator("form.usertext textarea[name='text']").first
        if textarea.count() == 0:
            raise RuntimeError("comment box not found (logged out or page blocked)")

        textarea.fill(text)
        page.locator("form.usertext button.save").first.click()
        page.wait_for_load_state("networkidle", timeout=60000)

        username, _ = _credentials()
        author = page.locator(f".comment .author:has-text('{username}')").first
        if author.count() == 0:
            return {
                "reddit_comment_id": "browser",
                "reddit_comment_url": old_url,
            }

        link = author.locator(
            "xpath=ancestor::div[contains(@class,'comment')]//a[contains(@href,'/comments/')]"
        ).first
        href = link.get_attribute("href") if link.count() else None
        if href:
            full_url = href if href.startswith("http") else f"https://old.reddit.com{href}"
            match = re.search(r"/comments/[^/]+/[^/]+/([^/?#]+)", full_url)
            comment_id = match.group(1) if match else "browser"
            return {
                "reddit_comment_id": comment_id,
                "reddit_comment_url": full_url,
            }

        return {
            "reddit_comment_id": "browser",
            "reddit_comment_url": old_url,
        }
