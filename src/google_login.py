"""Sign in to Google with the dedicated account, using plain Selenium.

No stealth tricks: if Google asks for anything a script cannot answer, the run
fails with a specific code. Credentials come from the environment, are never
logged and never written to disk. Screenshots are taken only on login pages
and error states.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .errors import (
    ADS_NO_ACCESS,
    LOGIN_BAD_CREDENTIALS,
    LOGIN_CHALLENGE,
    LOGIN_TOTP_MISSING,
    LOGIN_TOTP_REJECTED,
    SyncError,
)
from .masking import mask_email

GOOGLE_ACCOUNTS_URL = "https://accounts.google.com/"

# screens
SIGNED_IN = "signed_in"
EMAIL = "email"
PASSWORD = "password"
TOTP = "totp"
ACCOUNT_CHOOSER = "account_chooser"
TRY_ANOTHER_WAY = "try_another_way"
DISMISSIBLE = "dismissible"
BAD_CREDENTIALS = "bad_credentials"
TOTP_REJECTED = "totp_rejected"
CHALLENGE = "challenge"
UNKNOWN = "unknown"

BAD_CREDENTIALS_RE = re.compile(
    r"wrong password|couldn.t find your google account|enter a valid email|"
    r"this email or phone isn.t|that.s not the right password|incorrect password|"
    r"we couldn.t find an account",
    re.I,
)
TOTP_REJECTED_RE = re.compile(r"wrong code|invalid code|enter a valid code|that code didn.t work|code is incorrect|try again later", re.I)
CHALLENGE_RE = re.compile(
    r"verify it.s you|confirm your recovery|recovery email|recovery phone|verification code (?:was )?(?:sent|texted)|"
    r"get a verification code at|text message|sms|tap yes|check your phone|open the gmail app|"
    r"security key|passkey|this browser or app may not be secure|couldn.t sign you in|"
    r"unusual activity|suspicious|something went wrong|account has been disabled|account was disabled|"
    r"we noticed unusual|confirm it.s you|verify your identity|prove it.s you|captcha|type the text you hear",
    re.I,
)
TOTP_RE = re.compile(r"google authenticator|authenticator app|enter the 6-digit code|2-step verification|two-step verification", re.I)
CHOOSER_RE = re.compile(r"choose an account", re.I)
TRY_ANOTHER_RE = re.compile(r"try another way|choose how you want to sign in|more ways to verify", re.I)
DISMISS_LABELS = ("Not now", "Skip", "Don't ask again", "Yes", "Continue", "Done", "I understand", "Confirm", "No thanks", "Remind me later")
DISMISSIBLE_RE = re.compile(
    r"stay signed in|simplify your sign-in|protect your account|make your account safer|add a recovery|"
    r"not now|skip|don.t ask again|use your phone to sign in|set up passkey|sign in faster",
    re.I,
)
SIGNIN_PATH_RE = re.compile(r"/signin|/challenge|/speedbump|/v3/|/identifier|/pwd|/selectchallenge|/totp|servicelogin|interactivelogin", re.I)
ADS_NO_ACCESS_RE = re.compile(r"don.t have access|doesn.t have access|does not have access|request access|no access to this account|isn.t associated with|not authorized", re.I)


@dataclass(frozen=True)
class Credentials:
    email: str
    password: str
    totp_secret: str | None = None


@dataclass
class ScreenInfo:
    url: str
    text: str
    has_email_input: bool = False
    has_password_input: bool = False
    has_totp_input: bool = False
    has_sms_input: bool = False
    has_captcha: bool = False
    has_dismiss_button: bool = False


_INSPECT_JS = """
const vis = el => !!el && !!(el.offsetParent || el.getClientRects().length);
const q = s => Array.from(document.querySelectorAll(s)).some(vis);
return {
  email: q('input[type="email"], input[name="identifier"], #identifierId'),
  password: q('input[type="password"], input[name="Passwd"], input[name="password"]'),
  totp: q('input[name="totpPin"], #totpPin, input[autocomplete="one-time-code"]'),
  sms: q('input[name="idvPin"], #idvPin, input[name="Pin"]'),
  captcha: q('img[src*="captcha" i], #captchaimg, iframe[src*="recaptcha" i], input[name="ca"]'),
  dismiss: Array.from(document.querySelectorAll('button, [role=button], a')).some(b => /^(not now|skip|no thanks|remind me later|don.t ask again)$/i.test((b.innerText||'').trim())),
  text: (document.body && document.body.innerText) || ''
};
"""


def inspect(driver) -> ScreenInfo:
    try:
        d = driver.execute_script(_INSPECT_JS) or {}
    except Exception:  # noqa: BLE001
        d = {}
    return ScreenInfo(
        url=driver.current_url or "",
        text=re.sub(r"\s+", " ", d.get("text") or ""),
        has_email_input=bool(d.get("email")),
        has_password_input=bool(d.get("password")),
        has_totp_input=bool(d.get("totp")),
        has_sms_input=bool(d.get("sms")),
        has_captcha=bool(d.get("captcha")),
        has_dismiss_button=bool(d.get("dismiss")),
    )


def challenge_reason(info: ScreenInfo) -> str:
    """Short, credential-free description of why a screen counts as a challenge (for logs)."""
    if info.has_captcha:
        return "captcha"
    if info.has_sms_input:
        return "sms/phone code field"
    m = CHALLENGE_RE.search(info.text)
    return f"text matched {m.group(0)!r}" if m else "unrecognised"


def classify_screen(info: ScreenInfo, totp_attempted: bool = False) -> str:
    """Decide which sign-in screen is showing. Pure function, unit tested on saved snippets."""
    text = info.text
    host = (urlparse(info.url).hostname or "").lower()
    if BAD_CREDENTIALS_RE.search(text):
        return BAD_CREDENTIALS
    if totp_attempted and TOTP_REJECTED_RE.search(text) and (info.has_totp_input or TOTP_RE.search(text)):
        return TOTP_REJECTED
    if info.has_dismiss_button and not (info.has_password_input or info.has_email_input or info.has_totp_input or info.has_sms_input or info.has_captcha):
        return DISMISSIBLE
    if TRY_ANOTHER_RE.search(text) and re.search(r"authenticator", text, re.I) and not (info.has_password_input or info.has_email_input or info.has_totp_input):
        return TRY_ANOTHER_WAY
    if info.has_captcha or info.has_sms_input:
        return CHALLENGE
    # a real password / code field wins over challenge-like wording: Google's password page
    # itself says "To continue, first verify it's you"
    if info.has_totp_input:
        return TOTP
    if info.has_password_input:
        return PASSWORD
    if CHALLENGE_RE.search(text):
        return CHALLENGE
    if TOTP_RE.search(text) and not info.has_email_input:
        return TOTP
    if info.has_email_input:
        return EMAIL
    if CHOOSER_RE.search(text):
        return ACCOUNT_CHOOSER
    if TRY_ANOTHER_RE.search(text):
        return TRY_ANOTHER_WAY
    if DISMISSIBLE_RE.search(text):
        return DISMISSIBLE
    if host.startswith("myaccount.") or (not SIGNIN_PATH_RE.search(urlparse(info.url).path) and host != "accounts.google.com"):
        return SIGNED_IN
    if host == "accounts.google.com" and not SIGNIN_PATH_RE.search(info.url):
        return SIGNED_IN
    return UNKNOWN


def totp_code(secret: str, now: datetime | None = None, min_remaining: int = 3, sleep: Callable[[float], None] = time.sleep, period: int = 30) -> str:
    """Current TOTP code; if the current code expires in under ``min_remaining`` seconds, wait for the next one."""
    import pyotp

    totp = pyotp.TOTP(secret.replace(" ", "").strip(), interval=period)
    now = now or datetime.now(timezone.utc)
    ts = now.timestamp()
    remaining = period - (ts % period)
    if remaining < min_remaining:
        sleep(remaining + 0.5)
        ts += remaining + 0.5
    return totp.at(int(ts))


def is_signed_out_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in ("accounts.google.com", "accounts.youtube.com") or bool(SIGNIN_PATH_RE.search(urlparse(url).path or "")) and "google" in host


def ads_access_problem(url: str, page_text: str) -> str | None:
    """Return a reason when the Ads page shows no access / account chooser, else None."""
    text = re.sub(r"\s+", " ", page_text or "")
    if CHOOSER_RE.search(text) and "ads.google.com" in url:
        return "the Ads page asks to choose an account"
    if ADS_NO_ACCESS_RE.search(text) and "ads.google.com" in url:
        return "the Ads page says this account has no access"
    return None


# -- Selenium actions --------------------------------------------------------

def _xp_text_or_label(labels: tuple[str, ...], tags: str = "button | a | div[@role='button'] | span[@role='button'] | li | div[@role='link'] | div[@role='option']") -> str:
    conds = []
    for lab in labels:
        low = lab.lower()
        conds.append(
            f"contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), \"{low}\")"
        )
        conds.append(
            f"contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), \"{low}\")"
        )
    return f"//*[self::{' or self::'.join(t.strip() for t in tags.split('|'))}][{' or '.join(conds)}]"


def _click_first(driver, labels: tuple[str, ...]) -> bool:
    from selenium.webdriver.common.by import By

    for lab in labels:
        xp = _xp_text_or_label((lab,))
        for el in driver.find_elements(By.XPATH, xp):
            try:
                if el.is_displayed() and el.is_enabled():
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                    el.click()
                    return True
            except Exception:  # noqa: BLE001
                continue
    return False


def _type(driver, css_candidates: tuple[str, ...], value: str) -> bool:
    from selenium.webdriver.common.by import By

    for css in css_candidates:
        for el in driver.find_elements(By.CSS_SELECTOR, css):
            try:
                if el.is_displayed():
                    el.click()
                    el.clear()
                    el.send_keys(value)
                    return True
            except Exception:  # noqa: BLE001
                continue
    return False


def _next(driver) -> None:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys

    if _click_first(driver, ("Next", "Sign in", "Verify", "Continue")):
        return
    for el in driver.find_elements(By.CSS_SELECTOR, "button[type=submit], input[type=submit]"):
        if el.is_displayed():
            el.click()
            return
    driver.switch_to.active_element.send_keys(Keys.ENTER)


def _dump(driver, debug_dir: Path, name: str, log) -> None:
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
        driver.save_screenshot(str(debug_dir / f"login-{safe}.png"))
        (debug_dir / f"login-{safe}.html").write_text(driver.page_source, encoding="utf-8")
        log(f"[login] saved debug/login-{safe}.png and .html")
    except Exception as exc:  # noqa: BLE001
        log(f"[login] could not save debug output: {type(exc).__name__}")


def _settle(driver, seconds: float = 2.0) -> None:
    """Short human-like pause plus wait for document.readyState."""
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(0.25)
    try:
        for _ in range(40):
            if driver.execute_script("return document.readyState") == "complete":
                break
            time.sleep(0.25)
    except Exception:  # noqa: BLE001
        pass


def ensure_logged_in(
    driver,
    creds: Credentials,
    log: Callable[[str], None] = print,
    debug_dir: str | Path = "debug",
    accounts_url: str = GOOGLE_ACCOUNTS_URL,
    max_steps: int = 14,
    step_pause: float = 1.5,
) -> str:
    """Drive the Google sign-in flow until the account is signed in.

    Returns how the session was obtained: 'already', 'password' or 'password+totp'.
    Raises SyncError with LOGIN_* codes.
    """
    debug_dir = Path(debug_dir)
    who = mask_email(creds.email)
    log(f"[login] opening Google sign-in as {who}")
    driver.get(accounts_url)
    _settle(driver, step_pause)
    used: list[str] = []
    totp_attempted = False
    tried_another_way = False
    unknown_streak = 0
    last_screen = None
    for step in range(max_steps):
        info = inspect(driver)
        screen = classify_screen(info, totp_attempted)
        if screen != last_screen:
            log(f"[login] step {step + 1}: screen={screen}")
        last_screen = screen
        if screen == SIGNED_IN:
            how = "already" if not used else "+".join(used)
            log(f"[login] signed in as {who} ({how})")
            return how
        if screen == BAD_CREDENTIALS:
            _dump(driver, debug_dir, "bad-credentials", log)
            raise SyncError(LOGIN_BAD_CREDENTIALS, f"Google rejected the email or password for {who}")
        if screen == TOTP_REJECTED:
            _dump(driver, debug_dir, "totp-rejected", log)
            raise SyncError(LOGIN_TOTP_REJECTED, "Google refused the authenticator code (wrong GADS_TOTP_SECRET or clock skew)")
        if screen == CHALLENGE and not tried_another_way and not info.has_captcha and TRY_ANOTHER_RE.search(info.text):
            # e.g. "Tap Yes on your phone" with a "Try another way" link: ask for the list of methods once
            tried_another_way = True
            if _click_first(driver, ("Try another way",)):
                log("[login] challenge offers other methods; opening the list")
                _settle(driver, step_pause)
                continue
        if screen == CHALLENGE:
            _dump(driver, debug_dir, "challenge", log)
            path = urlparse(info.url).path
            heading = info.text[:160]
            raise SyncError(LOGIN_CHALLENGE, f"Google asked for a verification a script cannot answer ({challenge_reason(info)}; page path {path!r}; page starts {heading!r})")
        if screen == EMAIL:
            if not _type(driver, ('input[type="email"]', 'input[name="identifier"]', "#identifierId"), creds.email):
                raise SyncError(LOGIN_CHALLENGE, "email field not usable")
            _next(driver)
            used = used or []
        elif screen == PASSWORD:
            if not _type(driver, ('input[type="password"]', 'input[name="Passwd"]', 'input[name="password"]'), creds.password):
                raise SyncError(LOGIN_CHALLENGE, "password field not usable")
            _next(driver)
            if "password" not in used:
                used.append("password")
        elif screen == TOTP:
            if not creds.totp_secret:
                _dump(driver, debug_dir, "totp-missing", log)
                raise SyncError(LOGIN_TOTP_MISSING, "Google asks for an authenticator code but GADS_TOTP_SECRET is not set")
            code = totp_code(creds.totp_secret)
            if not _type(driver, ('input[name="totpPin"]', "#totpPin", 'input[autocomplete="one-time-code"]', 'input[type="tel"]', 'input[type="text"]'), code):
                raise SyncError(LOGIN_CHALLENGE, "authenticator code field not usable")
            totp_attempted = True
            if "totp" not in used:
                used.append("totp")
            _next(driver)
        elif screen == ACCOUNT_CHOOSER:
            if not _click_first(driver, (creds.email,)):
                if not _click_first(driver, ("Use another account",)):
                    raise SyncError(LOGIN_CHALLENGE, "account chooser without the expected account")
        elif screen == TRY_ANOTHER_WAY:
            if not _click_first(driver, ("Google Authenticator", "Authenticator app", "authenticator")):
                _dump(driver, debug_dir, "no-authenticator-option", log)
                raise SyncError(LOGIN_CHALLENGE, "Google offered only verification methods a script cannot use")
        elif screen == DISMISSIBLE:
            if not _click_first(driver, DISMISS_LABELS):
                _dump(driver, debug_dir, "undismissable", log)
                raise SyncError(LOGIN_CHALLENGE, "an interstitial could not be dismissed")
        else:  # unknown
            unknown_streak += 1
            if unknown_streak >= 4:
                _dump(driver, debug_dir, "unknown-screen", log)
                raise SyncError(LOGIN_CHALLENGE, f"unrecognised sign-in screen at host {urlparse(info.url).hostname}")
            _settle(driver, step_pause)
            continue
        unknown_streak = 0
        _settle(driver, step_pause)
    _dump(driver, debug_dir, "too-many-steps", log)
    raise SyncError(LOGIN_CHALLENGE, f"sign-in did not finish within {max_steps} screens")


def assert_ads_access(driver, log: Callable[[str], None] = print) -> None:
    url = driver.current_url or ""
    try:
        text = driver.execute_script("return (document.body && document.body.innerText) || ''") or ""
    except Exception:  # noqa: BLE001
        text = ""
    problem = ads_access_problem(url, text)
    if problem:
        raise SyncError(ADS_NO_ACCESS, problem)
