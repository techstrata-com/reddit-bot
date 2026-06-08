import os
import re
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Locator, Page, sync_playwright

ROOT = Path(__file__).parent
DEFAULT_SESSION_DIR = ROOT / ".reddit_browser_session"


def session_dir() -> Path:
    return Path(os.getenv("REDDIT_BROWSER_SESSION_DIR", DEFAULT_SESSION_DIR))


def headless() -> bool:
    return os.getenv("REDDIT_BROWSER_HEADLESS", "false").lower() in ("1", "true", "yes")


def browser_channel() -> str | None:
    channel = os.getenv("REDDIT_BROWSER_CHANNEL", "chrome").strip()
    return channel or None


def to_reddit(url: str) -> str:
    """Normalize any Reddit URL to www.reddit.com (never opens old.reddit.com)."""
    parsed = urlparse(url)
    host = parsed.netloc.removeprefix("www.")
    if host in ("reddit.com", "old.reddit.com"):
        return parsed._replace(netloc="www.reddit.com").geturl()
    return url


def _credentials() -> tuple[str, str]:
    username = os.getenv("REDDIT_USERNAME", "").strip()
    password = os.getenv("REDDIT_PASSWORD", "").strip()
    return username, password


def _launch_context(playwright, *, headless_mode: bool) -> BrowserContext:
    if headless_mode:
        print("  warning: REDDIT_BROWSER_HEADLESS=true — Reddit hides the comment box; use false")

    path = session_dir()
    path.mkdir(parents=True, exist_ok=True)
    kwargs = {
        "user_data_dir": str(path),
        "headless": headless_mode,
        "viewport": {"width": 1280, "height": 900},
        "args": ["--disable-blink-features=AutomationControlled"],
        "ignore_default_args": ["--enable-automation"],
    }
    channel = browser_channel()
    if channel:
        kwargs["channel"] = channel

    try:
        return playwright.chromium.launch_persistent_context(**kwargs)
    except Exception as err:
        message = str(err).lower()
        if "already in use" in message or "existing browser session" in message:
            raise RuntimeError(
                "Reddit browser profile is already open. Close the Chrome window from a "
                "previous run, then retry."
            ) from err
        if channel:
            raise RuntimeError(
                f"Could not launch browser channel '{channel}'. Install Google Chrome, or run:\n"
                "  python3 -m playwright install chrome"
            ) from err
        raise


def _prepare_page(page: Page) -> None:
    page.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )


def _is_blocked(page: Page) -> bool:
    body = page.inner_text("body").lower()
    return "blocked by network security" in body or "whoa there, pardner" in body


def _wait_for_login_form(page: Page) -> None:
    page.locator('input[name="username"]').wait_for(state="visible", timeout=25000)


def is_logged_in(page: Page) -> bool:
    page.goto("https://www.reddit.com/settings", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2000)
    if _is_blocked(page):
        return False
    return "login" not in page.url.lower()


def login_with_password(page: Page) -> None:
    username, password = _credentials()
    if not username or not password:
        raise RuntimeError(
            "Not logged in and no saved session. Run: python3 app.py 1 --reddit-login"
        )

    page.goto("https://www.reddit.com/login/", wait_until="domcontentloaded", timeout=60000)
    if _is_blocked(page):
        raise RuntimeError(
            "Reddit blocked the automated browser. Run: python3 app.py 1 --reddit-login"
        )
    _wait_for_login_form(page)

    page.locator('input[name="username"]').fill(username)
    page.locator('input[name="password"]').fill(password)
    page.get_by_role("button", name="Log In").click()
    page.wait_for_timeout(5000)

    if not is_logged_in(page):
        raise RuntimeError(
            "Reddit login failed. If 2FA or captcha is enabled, run: "
            "python3 app.py 1 --reddit-login"
        )


def ensure_logged_in(page: Page) -> None:
    if not is_logged_in(page):
        login_with_password(page)


def _assert_logged_in_on_post(page: Page) -> None:
    login_link = page.get_by_role("link", name="Log In")
    if login_link.count() and login_link.first.is_visible():
        raise RuntimeError(
            "Not logged in on the post page. Close all Chrome windows and run:\n"
            "  python3 app.py 1 --reddit-login"
        )


def _find_comment_composer(page: Page) -> Locator:
    _assert_logged_in_on_post(page)

    for scroll_y in (0, 500, 900, 1300):
        page.evaluate(f"window.scrollTo(0, {scroll_y})")
        page.wait_for_timeout(2500)

        textareas = page.locator('textarea[placeholder="Join the conversation"]')
        for i in range(textareas.count()):
            candidate = textareas.nth(i)
            try:
                if candidate.is_visible():
                    candidate.scroll_into_view_if_needed()
                    page.wait_for_timeout(500)
                    return candidate
            except Exception:
                continue

    raise RuntimeError(
        "Comment box not found. Post may be locked/archived, or Reddit blocked the browser. "
        "Close all Chrome windows and run: python3 app.py 1 --reddit-login"
    )


def _type_comment(page: Page, composer: Locator, text: str) -> None:
    composer.click()
    page.wait_for_timeout(500)
    # Never use fill() — Reddit's Lexical editor times out on it.
    page.keyboard.press("Meta+A")
    page.keyboard.press("Backspace")
    page.keyboard.type(text, delay=20)
    page.wait_for_timeout(1000)

    typed = composer.input_value().strip()
    if not typed:
        composer.evaluate(
            """(el, value) => {
                el.value = value;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            text,
        )
        page.wait_for_timeout(500)


def _click_submit(page: Page) -> None:
    page.wait_for_timeout(1500)

    # After typing, Reddit swaps textarea → Lexical editor (composer bbox becomes None).
    # The submit button lives inside shreddit-composer.
    selectors = [
        "shreddit-composer button[slot='submit-button']",
        "shreddit-composer button:has-text('Comment')",
        "button[slot='submit-button']:has-text('Comment')",
    ]
    for selector in selectors:
        button = page.locator(selector).first
        try:
            button.wait_for(state="visible", timeout=8000)
            if button.is_enabled():
                button.scroll_into_view_if_needed()
                button.click()
                page.wait_for_timeout(5000)
                return
        except Exception:
            continue

    # Fallback: top-of-page Comment button (main post composer, not reply buttons).
    for button in page.get_by_role("button", name=re.compile(r"^Comment$", re.I)).all():
        if not button.is_visible() or not button.is_enabled():
            continue
        box = button.bounding_box()
        if box and box["width"] >= 60 and box["y"] < 900:
            button.scroll_into_view_if_needed()
            button.click()
            page.wait_for_timeout(5000)
            return

    raise RuntimeError("Comment submit button not found or not enabled")


def interactive_login() -> None:
    """Open a visible browser so the user can log in manually (2FA/captcha safe)."""
    with sync_playwright() as playwright:
        context = _launch_context(playwright, headless_mode=False)
        page = context.pages[0] if context.pages else context.new_page()
        _prepare_page(page)
        page.goto("https://www.reddit.com/", wait_until="domcontentloaded")
        print("\nBrowser opened. Click Log In and sign in to Reddit (complete 2FA if asked).")
        print("When you are logged in, press Enter here...")
        input()
        if not is_logged_in(page):
            raise RuntimeError("Still not logged in — try again.")
        print(f"Session saved in {session_dir()}")
        context.close()


class BrowserPoster:
    """Reuse one browser session for multiple comment posts."""

    def __init__(self) -> None:
        self._playwright = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    def __enter__(self) -> "BrowserPoster":
        self._playwright = sync_playwright().start()
        self._context = _launch_context(self._playwright, headless_mode=headless())
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        _prepare_page(self._page)
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

        reddit_url = to_reddit(post_url)
        page.goto(reddit_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        if _is_blocked(page):
            raise RuntimeError("Reddit blocked this browser session — run: python3 app.py 1 --reddit-login")

        composer = _find_comment_composer(page)
        _type_comment(page, composer, text)
        _click_submit(page)

        return {
            "reddit_comment_id": "browser",
            "reddit_comment_url": reddit_url,
        }
