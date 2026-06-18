import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).parent
DEFAULT_SESSION_DIR = ROOT / ".reddit_browser_session"
REDDIT_LOGIN_URL = os.getenv("REDDIT_LOGIN_URL", "https://reddit.com/login").strip()


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


def to_old_reddit(url: str) -> str:
    """Normalize post URLs to old.reddit.com for reliable comment posting."""
    parsed = urlparse(url)
    host = parsed.netloc.removeprefix("www.")
    if host in ("reddit.com", "old.reddit.com", "www.reddit.com"):
        return parsed._replace(netloc="old.reddit.com").geturl()
    return url


def use_old_reddit_for_comments() -> bool:
    return os.getenv("REDDIT_USE_OLD_UI", "true").lower() in ("1", "true", "yes")


def _credentials() -> tuple[str, str]:
    username = os.getenv("REDDIT_USERNAME", "").strip()
    password = os.getenv("REDDIT_PASSWORD", "").strip()
    return username, password


def _select_all_key() -> str:
    return Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL


def _build_chrome_options(*, headless_mode: bool) -> Options:
    if headless_mode:
        print("  warning: REDDIT_BROWSER_HEADLESS=true — Reddit hides the comment box; use false")

    path = session_dir()
    path.mkdir(parents=True, exist_ok=True)

    options = Options()
    options.add_argument(f"--user-data-dir={path}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--window-size=1280,900")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    if headless_mode:
        options.add_argument("--headless=new")

    channel = browser_channel()
    if channel and channel.lower() not in ("chrome", "chromium"):
        options.binary_location = channel

    return options


def _create_driver(*, headless_mode: bool) -> webdriver.Chrome:
    options = _build_chrome_options(headless_mode=headless_mode)
    try:
        driver = webdriver.Chrome(options=options)
    except WebDriverException as err:
        message = str(err).lower()
        if "user data directory is already in use" in message or "profile is in use" in message:
            raise RuntimeError(
                "Reddit browser profile is already open. Close the Chrome window from a "
                "previous run, then retry."
            ) from err
        if "cannot find chrome binary" in message or "chrome not reachable" in message:
            raise RuntimeError(
                "Google Chrome is not installed or not found. Install Chrome, or set "
                "REDDIT_BROWSER_CHANNEL to your Chrome executable path."
            ) from err
        raise

    driver.set_page_load_timeout(60)
    _prepare_driver(driver)
    return driver


def _prepare_driver(driver: webdriver.Chrome) -> None:
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {
            "source": (
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
        },
    )


def _body_text(driver: webdriver.Chrome) -> str:
    return driver.find_element(By.TAG_NAME, "body").text.lower()


def _page_title(driver: webdriver.Chrome) -> str:
    return (driver.title or "").lower()


def _is_verification_pending(driver: webdriver.Chrome) -> bool:
    body = _body_text(driver)
    title = _page_title(driver)
    url = driver.current_url.lower()
    return (
        "please wait for verification" in body
        or "please wait for verification" in title
        or _is_reddit_challenge(url)
    )


def _wait_for_reddit_ready(driver: webdriver.Chrome, *, timeout_secs: int = 90) -> None:
    """Wait until Reddit finishes bot/JS verification pages."""
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        if not _is_verification_pending(driver) and not _is_blocked(driver):
            return
        time.sleep(2)
    raise RuntimeError(
        "Reddit verification did not finish. Check REDDIT_USERNAME / REDDIT_PASSWORD and retry."
    )


def _navigate(driver: webdriver.Chrome, url: str) -> None:
    driver.get(url)
    _wait_for_reddit_ready(driver)


def _is_blocked(driver: webdriver.Chrome) -> bool:
    body = _body_text(driver)
    return "blocked by network security" in body or "whoa there, pardner" in body


def _is_reddit_challenge(value: str) -> bool:
    lower = value.lower()
    return "js_challenge=1" in lower or "reddit.com/challenge" in lower


def _find_first(driver: webdriver.Chrome, selectors: list[str]) -> WebElement | None:
    for selector in selectors:
        try:
            element = driver.find_element(By.CSS_SELECTOR, selector)
            if element.is_displayed():
                return element
        except Exception:
            continue
    return None


def _find_in_shadow_roots(
    driver: webdriver.Chrome,
    host_selectors: list[str],
    inner_selector: str,
) -> WebElement | None:
    for host_selector in host_selectors:
        for host in driver.find_elements(By.CSS_SELECTOR, host_selector):
            try:
                root = host.shadow_root
                element = root.find_element(By.CSS_SELECTOR, inner_selector)
                if element.is_displayed():
                    return element
            except Exception:
                continue
    return None


def _find_username_field(driver: webdriver.Chrome) -> WebElement | None:
    direct = _find_first(
        driver,
        [
            'input[name="username"]',
            'input#loginUsername',
            'input[autocomplete="username"]',
        ],
    )
    if direct:
        return direct
    return _find_in_shadow_roots(
        driver,
        [
            'faceplate-text-input[name="username"]',
            'faceplate-text-input[id="username"]',
            "faceplate-text-input",
        ],
        "input",
    )


def _find_password_field(driver: webdriver.Chrome) -> WebElement | None:
    direct = _find_first(
        driver,
        [
            'input[name="password"]',
            'input#loginPassword',
            'input[type="password"]',
        ],
    )
    if direct:
        return direct
    return _find_in_shadow_roots(
        driver,
        [
            'faceplate-text-input[name="password"]',
            'faceplate-text-input[type="password"]',
            "faceplate-text-input",
        ],
        'input[type="password"], input',
    )


def _find_login_button(driver: webdriver.Chrome) -> WebElement | None:
    button = _find_first(
        driver,
        [
            'button[type="submit"]',
            'faceplate-button[type="submit"]',
        ],
    )
    if button:
        return button
    try:
        return driver.find_element(By.XPATH, "//button[contains(., 'Log In')]")
    except Exception:
        return _find_in_shadow_roots(
            driver,
            ["faceplate-button", "button"],
            "button, [role='button']",
        )


def _type_into_field(element: WebElement, value: str) -> None:
    element.click()
    time.sleep(0.3)
    select_all = _select_all_key()
    element.send_keys(select_all, "a")
    element.send_keys(Keys.BACKSPACE)
    for char in value:
        element.send_keys(char)
        time.sleep(0.03)


def _login_form_visible(driver: webdriver.Chrome) -> bool:
    return _find_username_field(driver) is not None


def _session_is_active(driver: webdriver.Chrome) -> bool:
    """
    Return True when Reddit looks logged in.

    If /login redirects to home (saved session), there is no login form and the
    URL no longer contains /login — treat that as logged in.
    """
    if _is_blocked(driver) or _is_verification_pending(driver):
        return False
    if _login_form_visible(driver):
        return False

    if _find_first(
        driver,
        [
            "#expand-user-drawer-button",
            'button[id="expand-user-drawer-button"]',
            '[data-testid="user-drawer-button"]',
            'faceplate-tracker[noun="avatar"]',
            "#header .user",
            "form.logout",
            'a[href*="/logout"]',
        ],
    ):
        return True

    current_url = driver.current_url.lower()
    if "old.reddit.com" in current_url:
        return _find_first(driver, ["#header .user", "form.logout"]) is not None

    # /login -> home redirect with no login form means session is already active
    if "reddit.com" in current_url and "/login" not in current_url.split("?")[0]:
        return True

    return False


def _has_logged_in_ui(driver: webdriver.Chrome) -> bool:
    return _session_is_active(driver)


def _wait_for_login_redirect_or_form(driver: webdriver.Chrome) -> str:
    """
    After opening /login, wait until either:
    - session redirect completes (logged in), or
    - login form appears (need credentials)
    Returns: "active" | "form"
    """
    deadline = time.time() + 20
    while time.time() < deadline:
        _wait_for_reddit_ready(driver, timeout_secs=5)
        if _session_is_active(driver):
            return "active"
        if _login_form_visible(driver):
            return "form"
        time.sleep(1)
    if _session_is_active(driver):
        return "active"
    if _login_form_visible(driver):
        return "form"
    raise RuntimeError(
        "Reddit login page did not settle. Session may be invalid — "
        "delete .reddit_browser_session and retry."
    )


def is_logged_in(driver: webdriver.Chrome) -> bool:
    """Check session by opening the login page."""
    _navigate(driver, REDDIT_LOGIN_URL)
    time.sleep(2)
    return _session_is_active(driver)


def login_with_password(driver: webdriver.Chrome) -> None:
    username, password = _credentials()
    if not username or not password:
        raise RuntimeError("Set REDDIT_USERNAME and REDDIT_PASSWORD in .env")

    print(f"  opening {REDDIT_LOGIN_URL} for automated login...")
    _navigate(driver, REDDIT_LOGIN_URL)
    if _is_blocked(driver):
        raise RuntimeError("Reddit blocked the automated browser session")

    state = _wait_for_login_redirect_or_form(driver)
    if state == "active":
        print("  reddit session already active")
        return

    username_el = _find_username_field(driver)
    password_el = _find_password_field(driver)
    if not username_el or not password_el:
        raise RuntimeError(f"Could not find login fields at {REDDIT_LOGIN_URL}")

    print("  submitting credentials...")
    _type_into_field(username_el, username)
    time.sleep(0.5)
    _type_into_field(password_el, password)
    time.sleep(0.5)

    login_button = _find_login_button(driver)
    if not login_button:
        raise RuntimeError("Could not find Reddit Log In button")
    login_button.click()

    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(2)
        _wait_for_reddit_ready(driver, timeout_secs=5)
        if _session_is_active(driver):
            print("  login successful")
            return

    raise RuntimeError(
        "Reddit login failed using REDDIT_USERNAME / REDDIT_PASSWORD. "
        "Check credentials, 2FA, or captcha requirements."
    )


def ensure_logged_in(driver: webdriver.Chrome) -> None:
    login_with_password(driver)


def _assert_logged_in_on_post(driver: webdriver.Chrome) -> None:
    body = _body_text(driver)
    if "log in to leave a comment" in body or "you must log in" in body:
        raise RuntimeError("Not logged in on the post page")
    for link in driver.find_elements(By.XPATH, "//a[contains(., 'Log In')]"):
        if link.is_displayed():
            raise RuntimeError("Not logged in on the post page")


def _post_is_locked(driver: webdriver.Chrome) -> bool:
    body = _body_text(driver)
    return (
        "archived" in body and "comment" in body
    ) or "locked" in body and "comments are locked" in body


def _deep_query_selector(driver: webdriver.Chrome, selector: str) -> WebElement | None:
    """Search through nested shadow roots for an element."""
    element = driver.execute_script(
        """
        function deepQuery(root, selector) {
            const direct = root.querySelector(selector);
            if (direct) return direct;
            for (const host of root.querySelectorAll('*')) {
                if (!host.shadowRoot) continue;
                const found = deepQuery(host.shadowRoot, selector);
                if (found) return found;
            }
            return null;
        }
        return deepQuery(document, arguments[0]);
        """,
        selector,
    )
    return element


def _find_comment_composer_new_reddit(driver: webdriver.Chrome) -> WebElement:
    composer_selectors = [
        'textarea[placeholder="Join the conversation"]',
        'textarea[name="body"]',
        "shreddit-composer textarea",
        'div[contenteditable="true"][role="textbox"]',
    ]

    for scroll_y in (0, 500, 900, 1300):
        driver.execute_script(f"window.scrollTo(0, {scroll_y});")
        time.sleep(2)

        for selector in composer_selectors:
            candidate = _deep_query_selector(driver, selector)
            if candidate:
                try:
                    if candidate.is_displayed():
                        driver.execute_script(
                            "arguments[0].scrollIntoView({block: 'center'});", candidate
                        )
                        time.sleep(0.5)
                        return candidate
                except Exception:
                    continue

            for candidate in driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    if candidate.is_displayed():
                        driver.execute_script(
                            "arguments[0].scrollIntoView({block: 'center'});", candidate
                        )
                        time.sleep(0.5)
                        return candidate
                except Exception:
                    continue

    raise RuntimeError("Comment box not found on new Reddit UI")


def _find_comment_composer_old_reddit(driver: webdriver.Chrome) -> WebElement:
    selectors = [
        'form.usertext.cloneable textarea[name="text"]',
        'div.commentarea textarea[name="text"]',
        'div.usertext-edit textarea',
        'textarea[name="text"]',
    ]
    deadline = time.time() + 20
    while time.time() < deadline:
        for selector in selectors:
            for candidate in driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    if candidate.is_displayed() and candidate.is_enabled():
                        driver.execute_script(
                            "arguments[0].scrollIntoView({block: 'center'});", candidate
                        )
                        time.sleep(0.5)
                        return candidate
                except Exception:
                    continue
        time.sleep(1)

    raise RuntimeError("Comment box not found on old.reddit.com")


def _find_comment_composer(driver: webdriver.Chrome, *, use_old_ui: bool) -> WebElement:
    _assert_logged_in_on_post(driver)
    if _post_is_locked(driver):
        raise RuntimeError("Post is archived or locked — comments are disabled")

    if use_old_ui:
        return _find_comment_composer_old_reddit(driver)
    try:
        return _find_comment_composer_new_reddit(driver)
    except RuntimeError:
        print("  new Reddit comment box not found, trying old.reddit.com...")
        old_url = to_old_reddit(driver.current_url)
        _navigate(driver, old_url)
        time.sleep(2)
        return _find_comment_composer_old_reddit(driver)


def _type_comment(driver: webdriver.Chrome, composer: WebElement, text: str) -> None:
    composer.click()
    time.sleep(0.5)
    select_all = _select_all_key()
    composer.send_keys(select_all, "a")
    composer.send_keys(Keys.BACKSPACE)
    for char in text:
        composer.send_keys(char)
        time.sleep(0.02)
    time.sleep(1)

    typed = (composer.get_attribute("value") or "").strip()
    if not typed:
        driver.execute_script(
            """
            const el = arguments[0];
            const value = arguments[1];
            el.value = value;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            """,
            composer,
            text,
        )
        time.sleep(0.5)


def _click_submit_old_reddit(driver: webdriver.Chrome) -> None:
    selectors = [
        'form.usertext.cloneable button[type="submit"]',
        'div.usertext-buttons button[type="submit"]',
        'button.save',
    ]
    for selector in selectors:
        try:
            button = WebDriverWait(driver, 8).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
            )
            if button.is_enabled():
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", button
                )
                button.click()
                time.sleep(5)
                return
        except TimeoutException:
            continue
        except Exception:
            continue

    for button in driver.find_elements(By.XPATH, "//button[contains(translate(., 'SAVE', 'save'), 'save')]"):
        if button.is_displayed() and button.is_enabled():
            button.click()
            time.sleep(5)
            return

    raise RuntimeError("Comment submit button not found on old.reddit.com")


def _click_submit(driver: webdriver.Chrome, *, use_old_ui: bool) -> None:
    if use_old_ui or "old.reddit.com" in driver.current_url.lower():
        _click_submit_old_reddit(driver)
        return
    time.sleep(1.5)

    selectors = [
        "shreddit-composer button[slot='submit-button']",
        "shreddit-composer button",
        "button[slot='submit-button']",
    ]
    for selector in selectors:
        try:
            button = WebDriverWait(driver, 8).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
            )
            if button.is_enabled() and "comment" in button.text.lower():
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", button
                )
                button.click()
                time.sleep(5)
                return
        except TimeoutException:
            continue
        except Exception:
            continue

    for button in driver.find_elements(By.XPATH, "//button"):
        try:
            if not button.is_displayed() or not button.is_enabled():
                continue
            label = (button.text or "").strip()
            if not re.fullmatch(r"comment", label, re.I):
                continue
            location = button.location
            size = button.size
            if size.get("width", 0) >= 60 and location.get("y", 9999) < 900:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", button
                )
                button.click()
                time.sleep(5)
                return
        except Exception:
            continue

    raise RuntimeError("Comment submit button not found or not enabled")


def automated_login() -> None:
    """Log into Reddit with REDDIT_USERNAME / REDDIT_PASSWORD and save the session."""
    driver = _create_driver(headless_mode=False)
    try:
        login_with_password(driver)
        print(f"Session saved in {session_dir()}")
    finally:
        driver.quit()


class BrowserPoster:
    """Reuse one browser session for multiple comment posts."""

    def __init__(self) -> None:
        self._driver: webdriver.Chrome | None = None

    def _open_driver(self, *, headless_mode: bool) -> None:
        self._driver = _create_driver(headless_mode=headless_mode)

    def _close_driver(self) -> None:
        if self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None

    def __enter__(self) -> "BrowserPoster":
        self._open_driver(headless_mode=False)
        ensure_logged_in(self._driver)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._close_driver()

    def _open_post(self, post_url: str) -> tuple[str, bool]:
        use_old_ui = use_old_reddit_for_comments()
        reddit_url = to_old_reddit(post_url) if use_old_ui else to_reddit(post_url)
        ui_label = "old.reddit.com" if use_old_ui else "www.reddit.com"
        print(f"  opening post on {ui_label}: {reddit_url}")
        _navigate(self._driver, reddit_url)
        time.sleep(3)
        return reddit_url, use_old_ui

    def verify_post(self, post_url: str) -> None:
        """Login and confirm the comment box is available on a post."""
        assert self._driver is not None

        _, use_old_ui = self._open_post(post_url)

        if _is_blocked(self._driver):
            raise RuntimeError("Reddit blocked this browser session")

        _find_comment_composer(self._driver, use_old_ui=use_old_ui)
        print("  comment box found")

    def post(self, post_url: str, text: str) -> dict:
        driver = self._driver
        assert driver is not None

        reddit_url, use_old_ui = self._open_post(post_url)

        if _is_blocked(driver):
            raise RuntimeError("Reddit blocked this browser session")

        composer = _find_comment_composer(driver, use_old_ui=use_old_ui)
        _type_comment(driver, composer, text)
        _click_submit(driver, use_old_ui=use_old_ui)

        return {
            "reddit_comment_id": "browser",
            "reddit_comment_url": reddit_url,
        }
