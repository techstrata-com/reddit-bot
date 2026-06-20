import os
import re
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).parent
DEFAULT_SESSION_DIR = ROOT / ".reddit_browser_session"


def use_old_ui() -> bool:
    return os.getenv("REDDIT_USE_OLD_UI", "false").lower() in ("1", "true", "yes")


def reddit_login_url() -> str:
    configured = os.getenv("REDDIT_LOGIN_URL", "").strip()
    if use_old_ui():
        if configured and "old.reddit" in configured:
            return configured
        return "https://old.reddit.com/login"
    return configured or "https://www.reddit.com/login"


def reddit_settings_url() -> str:
    return "https://old.reddit.com/prefs" if use_old_ui() else "https://www.reddit.com/settings"


def session_dir() -> Path:
    return Path(os.getenv("REDDIT_BROWSER_SESSION_DIR", DEFAULT_SESSION_DIR))


def auto_clear_session_enabled() -> bool:
    return os.getenv("REDDIT_AUTO_CLEAR_SESSION", "true").lower() in ("1", "true", "yes")


def clear_browser_session() -> None:
    path = session_dir()
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
        print(f"  deleted stale browser session: {path}")
    except Exception as err:
        raise RuntimeError(
            f"Could not delete browser session at {path}. Close all Chrome windows and retry."
        ) from err


def _should_auto_clear_session(err: Exception) -> bool:
    if not auto_clear_session_enabled():
        return False
    message = str(err).lower()
    triggers = (
        "login verification failed",
        "login failed",
        "login page did not settle",
        "could not find login fields",
        "reddit blocked",
        "verification did not finish",
        "not logged in on the post page",
    )
    return any(trigger in message for trigger in triggers)


def browser_channel() -> str | None:
    channel = os.getenv("REDDIT_BROWSER_CHANNEL", "chrome").strip()
    return channel or None


def to_reddit(url: str) -> str:
    """Normalize any Reddit URL to the configured UI host."""
    parsed = urlparse(url)
    host = parsed.netloc.removeprefix("www.")
    if host.endswith("reddit.com"):
        netloc = "old.reddit.com" if use_old_ui() else "www.reddit.com"
        return parsed._replace(netloc=netloc).geturl()
    return url


def _credentials() -> tuple[str, str]:
    username = os.getenv("REDDIT_USERNAME", "").strip()
    password = os.getenv("REDDIT_PASSWORD", "").strip()
    return username, password


def _select_all_key() -> str:
    return Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL


def _build_chrome_options() -> Options:
    path = session_dir()
    path.mkdir(parents=True, exist_ok=True)

    options = Options()
    options.add_argument(f"--user-data-dir={path}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--window-size=1280,900")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    if os.getenv("REDDIT_BROWSER_HEADLESS", "false").lower() in ("1", "true", "yes"):
        options.add_argument("--headless=new")

    channel = browser_channel()
    if channel and channel.lower() not in ("chrome", "chromium"):
        options.binary_location = channel

    return options


def _create_driver() -> webdriver.Chrome:
    try:
        driver = webdriver.Chrome(options=_build_chrome_options())
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
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {
            "source": (
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
        },
    )
    return driver


def _body_text(driver: webdriver.Chrome) -> str:
    return driver.find_element(By.TAG_NAME, "body").text.lower()


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


def _deep_query_selector(driver: webdriver.Chrome, selector: str) -> WebElement | None:
    return driver.execute_script(
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


def _wait_for_page(driver: webdriver.Chrome, *, timeout_secs: int = 30) -> None:
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        if _is_blocked(driver):
            raise RuntimeError("Reddit blocked this browser session")
        body = _body_text(driver)
        title = (driver.title or "").lower()
        url = driver.current_url.lower()
        if "please wait for verification" not in body and "please wait for verification" not in title:
            if not _is_reddit_challenge(url):
                return
        time.sleep(2)


def _navigate(driver: webdriver.Chrome, url: str) -> None:
    driver.get(url)
    _wait_for_page(driver)


def _find_username_field(driver: webdriver.Chrome) -> WebElement | None:
    if use_old_ui():
        return _find_first(
            driver,
            ['input#user_login', 'input[name="user"]', 'input[name="username"]'],
        )

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
    if use_old_ui():
        return _find_first(
            driver,
            ['input#passwd', 'input[name="passwd"]', 'input[type="password"]'],
        )

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
    if use_old_ui():
        button = _find_first(
            driver,
            [
                'form#login_login-main button[type="submit"]',
                'form.login-form button[type="submit"]',
                'button[type="submit"]',
            ],
        )
        if button:
            return button
        try:
            return driver.find_element(By.XPATH, "//button[contains(., 'log in')]")
        except Exception:
            return None

    button = _find_first(driver, ['button[type="submit"]', 'faceplate-button[type="submit"]'])
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
    """Check logged-in UI on the current page only (no navigation)."""
    if _is_blocked(driver):
        return False
    if _login_form_visible(driver):
        return False

    if use_old_ui():
        return _find_first(
            driver,
            [
                '#header-bottom-right a[href*="logout"]',
                ".user a.logout",
                "span.user",
            ],
        ) is not None

    return _find_first(
        driver,
        [
            "#expand-user-drawer-button",
            'button[id="expand-user-drawer-button"]',
            '[data-testid="user-drawer-button"]',
            'faceplate-tracker[noun="avatar"]',
        ],
    ) is not None


def _verify_login(driver: webdriver.Chrome) -> bool:
    """
    Reliable login check: settings/prefs redirects to login when logged out.
    """
    driver.get(reddit_settings_url())
    time.sleep(2)
    _wait_for_page(driver, timeout_secs=20)

    url = driver.current_url.lower()
    if "login" in url or _login_form_visible(driver):
        return False
    if _is_blocked(driver):
        return False
    return True


def _login_page_settled(driver: webdriver.Chrome) -> str | None:
    """
    On the login page, detect whether credentials are needed or session redirected.
    Returns: "form", "active", or None if still loading.
    """
    if _login_form_visible(driver):
        return "form"

    url = driver.current_url.lower()
    if "reddit.com" in url and "/login" not in url.split("?")[0]:
        return "active"
    return None


def _wait_for_login_redirect_or_form(driver: webdriver.Chrome) -> str:
    deadline = time.time() + 20
    while time.time() < deadline:
        _wait_for_page(driver, timeout_secs=5)
        state = _login_page_settled(driver)
        if state:
            return state
        time.sleep(1)

    state = _login_page_settled(driver)
    if state:
        return state
    raise RuntimeError(
        "Reddit login page did not settle. The browser session may be stale."
    )


def login_with_password(driver: webdriver.Chrome) -> None:
    username, password = _credentials()
    if not username or not password:
        raise RuntimeError("Set REDDIT_USERNAME and REDDIT_PASSWORD in .env")

    if _verify_login(driver):
        print("  reddit session already active")
        return

    print(f"  opening {reddit_login_url()} for automated login...")
    _navigate(driver, reddit_login_url())
    if _is_blocked(driver):
        raise RuntimeError("Reddit blocked the automated browser session")

    state = _wait_for_login_redirect_or_form(driver)
    if state == "active":
        if _verify_login(driver):
            print("  reddit session already active")
            return
        print("  login redirect did not create a session, retrying login form...")
        _navigate(driver, reddit_login_url())

    username_el = _find_username_field(driver)
    password_el = _find_password_field(driver)
    if not username_el or not password_el:
        raise RuntimeError(f"Could not find login fields at {reddit_login_url()}")

    print("  submitting credentials...")
    _type_into_field(username_el, username)
    time.sleep(0.5)
    _type_into_field(password_el, password)
    time.sleep(0.5)

    login_button = _find_login_button(driver)
    if not login_button:
        raise RuntimeError("Could not find Reddit Log In button")
    login_button.click()

    deadline = time.time() + 45
    while time.time() < deadline:
        time.sleep(2)
        if _verify_login(driver):
            print("  login successful")
            return

    raise RuntimeError(
        "Reddit login failed. Check REDDIT_USERNAME / REDDIT_PASSWORD, 2FA, or captcha."
    )


def ensure_logged_in(driver: webdriver.Chrome) -> None:
    ui = "old.reddit.com" if use_old_ui() else "www.reddit.com"
    print(f"  ensuring reddit login ({ui})...")
    login_with_password(driver)
    if not _verify_login(driver):
        raise RuntimeError(
            "Reddit login verification failed. Check credentials or complete captcha in Chrome."
        )
    print("  reddit login verified")


def _assert_logged_in_on_post(driver: webdriver.Chrome) -> bool:
    if _session_is_active(driver):
        return True
    body = _body_text(driver)
    if any(
        phrase in body
        for phrase in (
            "log in to leave a comment",
            "you must log in",
            "log in to comment",
            "sign in to comment",
            "log in to add a comment",
        )
    ):
        return False
    return _verify_login(driver)


def _post_is_locked(driver: webdriver.Chrome) -> bool:
    body = _body_text(driver)
    locked_phrases = (
        "archived and can no longer be commented on",
        "this post is archived",
        "comments are locked",
        "commenting is locked",
        "you cannot comment on this post",
        "commenting is disabled",
        "locked by the moderators",
    )
    return any(phrase in body for phrase in locked_phrases)


def _safe_focus(driver: webdriver.Chrome, element: WebElement) -> None:
    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", element
    )
    time.sleep(0.3)
    try:
        WebDriverWait(driver, 5).until(EC.element_to_be_clickable(element))
        ActionChains(driver).move_to_element(element).pause(0.2).click(element).perform()
        return
    except Exception:
        pass
    driver.execute_script(
        """
        const el = arguments[0];
        el.focus();
        el.click();
        """,
        element,
    )


def _find_comment_composer_old(driver: webdriver.Chrome) -> WebElement:
    selectors = [
        "form.usertext-edit textarea[name='text']",
        "div.commentarea form textarea[name='text']",
        "textarea[name='text']",
    ]
    for selector in selectors:
        for candidate in driver.find_elements(By.CSS_SELECTOR, selector):
            try:
                if candidate.is_displayed() and candidate.is_enabled():
                    return candidate
            except Exception:
                continue
    raise RuntimeError("Comment box not found on old.reddit.com")


def _find_comment_composer_new(driver: webdriver.Chrome) -> WebElement:
    composer_selectors = [
        'textarea[placeholder="Join the conversation"]',
        'textarea[name="body"]',
        "shreddit-composer textarea",
        'div[contenteditable="true"][role="textbox"]',
    ]

    for scroll_y in (0, 400, 800, 1200):
        driver.execute_script(f"window.scrollTo(0, {scroll_y});")
        time.sleep(2)

        for selector in composer_selectors:
            candidate = _deep_query_selector(driver, selector)
            if candidate:
                try:
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

    raise RuntimeError("Comment box not found on www.reddit.com")


def _find_comment_composer(driver: webdriver.Chrome) -> WebElement:
    if not _assert_logged_in_on_post(driver):
        print("  not logged in on post page, logging in again...")
        login_with_password(driver)
        _navigate(driver, driver.current_url)
        time.sleep(2)
        if not _assert_logged_in_on_post(driver):
            raise RuntimeError("Not logged in on the post page after login attempt")

    if _post_is_locked(driver):
        raise RuntimeError("Post is archived or locked — comments are disabled")

    if use_old_ui():
        return _find_comment_composer_old(driver)
    return _find_comment_composer_new(driver)


def _set_composer_text(driver: webdriver.Chrome, composer: WebElement, text: str) -> None:
    tag = (composer.tag_name or "").lower()
    is_textarea = tag == "textarea" or composer.get_attribute("name") in ("text", "body")

    _safe_focus(driver, composer)
    time.sleep(0.4)

    if is_textarea:
        try:
            select_all = _select_all_key()
            composer.send_keys(select_all, "a")
            composer.send_keys(Keys.BACKSPACE)
            composer.send_keys(text)
        except Exception:
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
    else:
        driver.execute_script(
            """
            const el = arguments[0];
            const value = arguments[1];
            el.textContent = value;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            """,
            composer,
            text,
        )

    time.sleep(0.5)
    typed = (composer.get_attribute("value") or composer.text or "").strip()
    if len(typed) < max(1, len(text.strip()) // 2):
        driver.execute_script(
            """
            const el = arguments[0];
            const value = arguments[1];
            if ('value' in el) el.value = value;
            else el.textContent = value;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            """,
            composer,
            text,
        )
        time.sleep(0.5)


def _type_comment(driver: webdriver.Chrome, composer: WebElement, text: str) -> None:
    _set_composer_text(driver, composer, text)
    time.sleep(1)


def _click_submit_old(driver: webdriver.Chrome) -> None:
    selectors = [
        "form.usertext-edit button[type='submit']",
        "div.commentarea form button[type='submit']",
    ]
    for selector in selectors:
        for button in driver.find_elements(By.CSS_SELECTOR, selector):
            try:
                if not button.is_displayed() or not button.is_enabled():
                    continue
                label = (button.text or button.get_attribute("value") or "").strip().lower()
                if label and label not in ("save", "comment", "reply"):
                    continue
                _safe_focus(driver, button)
                button.click()
                time.sleep(4)
                return
            except Exception:
                continue

    for button in driver.find_elements(By.XPATH, "//button[@type='submit']"):
        try:
            if not button.is_displayed() or not button.is_enabled():
                continue
            label = (button.text or button.get_attribute("value") or "").strip().lower()
            if label in ("save", "comment", "reply", ""):
                _safe_focus(driver, button)
                button.click()
                time.sleep(4)
                return
        except Exception:
            continue

    raise RuntimeError("Comment submit button not found or not enabled on old.reddit.com")


def _click_submit_new(driver: webdriver.Chrome) -> None:
    time.sleep(1.5)

    selectors = [
        "shreddit-composer button[slot='submit-button']",
        "shreddit-composer button",
        "button[slot='submit-button']",
    ]
    for selector in selectors:
        button = _deep_query_selector(driver, selector)
        if button:
            try:
                if button.is_enabled() and "comment" in (button.text or "").lower():
                    _safe_focus(driver, button)
                    button.click()
                    time.sleep(5)
                    return
            except Exception:
                continue

        try:
            button = WebDriverWait(driver, 8).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
            )
            if button.is_enabled() and "comment" in button.text.lower():
                _safe_focus(driver, button)
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
                _safe_focus(driver, button)
                button.click()
                time.sleep(5)
                return
        except Exception:
            continue

    raise RuntimeError("Comment submit button not found or not enabled")


def _click_submit(driver: webdriver.Chrome) -> None:
    if use_old_ui():
        _click_submit_old(driver)
    else:
        _click_submit_new(driver)


def _comment_snippet(text: str, length: int = 80) -> str:
    cleaned = " ".join(text.strip().split())
    return cleaned[:length].lower()


def _verify_comment_published(driver: webdriver.Chrome, text: str) -> bool:
    snippet = _comment_snippet(text)
    if not snippet:
        return False

    deadline = time.time() + 20
    while time.time() < deadline:
        body = _body_text(driver)
        if snippet in body:
            return True
        time.sleep(2)
        try:
            driver.refresh()
            _wait_for_page(driver, timeout_secs=20)
        except Exception:
            time.sleep(2)
    return False


def automated_login() -> None:
    """Log into Reddit with REDDIT_USERNAME / REDDIT_PASSWORD and save the session."""
    driver = _create_driver()
    try:
        try:
            login_with_password(driver)
        except RuntimeError as err:
            driver.quit()
            if _should_auto_clear_session(err):
                clear_browser_session()
                print("  retrying login with a fresh browser session...")
                driver = _create_driver()
                login_with_password(driver)
            else:
                raise
        print(f"Session saved in {session_dir()}")
    finally:
        driver.quit()


class BrowserPoster:
    """Reuse one browser session for multiple comment posts on www.reddit.com."""

    def __init__(self) -> None:
        self._driver: webdriver.Chrome | None = None

    def _close_driver(self) -> None:
        if self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None

    def _start_with_login(self) -> None:
        self._driver = _create_driver()
        ensure_logged_in(self._driver)

    def __enter__(self) -> "BrowserPoster":
        try:
            self._start_with_login()
        except RuntimeError as err:
            self._close_driver()
            if _should_auto_clear_session(err):
                clear_browser_session()
                print("  retrying login with a fresh browser session...")
                self._start_with_login()
            else:
                raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._close_driver()

    def _open_post(self, post_url: str) -> str:
        reddit_url = to_reddit(post_url)
        print(f"  opening post: {reddit_url}")
        _navigate(self._driver, reddit_url)
        time.sleep(3)
        return reddit_url

    def verify_post(self, post_url: str) -> None:
        assert self._driver is not None
        self._open_post(post_url)
        if _is_blocked(self._driver):
            raise RuntimeError("Reddit blocked this browser session")
        _find_comment_composer(self._driver)
        print("  comment box found")

    def post(self, post_url: str, text: str) -> dict:
        driver = self._driver
        assert driver is not None

        reddit_url = self._open_post(post_url)
        if _is_blocked(driver):
            raise RuntimeError("Reddit blocked this browser session")

        composer = _find_comment_composer(driver)
        _type_comment(driver, composer, text)
        _click_submit(driver)

        if not _verify_comment_published(driver, text):
            raise RuntimeError(
                "Comment submit finished but the comment text was not found on the page"
            )

        print("  comment verified on page")
        return {
            "reddit_comment_id": "browser",
            "reddit_comment_url": reddit_url,
        }
