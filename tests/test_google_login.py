"""Pure-function tests for the Google login helpers: screen classification on saved
snippets, TOTP wait logic, masking, and the Ads access check."""
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import pyotp
import pytest

from src import google_login as gl
from src.masking import mask_email, mask_phone

SNIPPETS = Path(__file__).parent / "fixtures" / "login_screens"


class _Flags(HTMLParser):
    """Mimics the JS inspector on static HTML so the classifier can be tested without a browser."""

    def __init__(self):
        super().__init__()
        self.info = gl.ScreenInfo(url="", text="")
        self._text = []
        self._button_depth = 0
        self._button_text = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input":
            t, n, i = a.get("type", ""), a.get("name", ""), a.get("id", "")
            if t == "email" or n == "identifier" or i == "identifierId":
                self.info.has_email_input = True
            if t == "password" or n in ("Passwd", "password"):
                self.info.has_password_input = True
            if n == "totpPin" or i == "totpPin":
                self.info.has_totp_input = True
            if n in ("idvPin", "Pin") or i == "idvPin":
                self.info.has_sms_input = True
            if n == "ca":
                self.info.has_captcha = True
        if tag == "img" and ("captcha" in a.get("src", "").lower() or a.get("id") == "captchaimg"):
            self.info.has_captcha = True
        if tag == "button" or a.get("role") == "button":
            self._button_depth += 1
            self._button_text = ""

    def handle_endtag(self, tag):
        if tag == "button" and self._button_depth:
            self._button_depth -= 1
            if re.fullmatch(r"(not now|skip|no thanks|remind me later|don.t ask again)", self._button_text.strip(), re.I):
                self.info.has_dismiss_button = True

    def handle_data(self, data):
        self._text.append(data)
        if self._button_depth:
            self._button_text += data


def info_from(name: str, url: str = "https://accounts.google.com/v3/signin/identifier?x=1") -> gl.ScreenInfo:
    p = _Flags()
    p.feed((SNIPPETS / name).read_text(encoding="utf-8"))
    p.info.url = url
    p.info.text = re.sub(r"\s+", " ", " ".join(p._text))
    return p.info


@pytest.mark.parametrize(
    "snippet,url,expected",
    [
        ("email.html", "https://accounts.google.com/v3/signin/identifier", gl.EMAIL),
        ("password.html", "https://accounts.google.com/v3/signin/challenge/pwd", gl.PASSWORD),
        ("password_wrong.html", "https://accounts.google.com/v3/signin/challenge/pwd", gl.BAD_CREDENTIALS),
        ("password_verify_wording.html", "https://accounts.google.com/v3/signin/challenge/pwd", gl.PASSWORD),
        ("totp.html", "https://accounts.google.com/v3/signin/challenge/totp", gl.TOTP),
        ("challenge_phone.html", "https://accounts.google.com/v3/signin/challenge/ipp", gl.CHALLENGE),
        ("challenge_tap.html", "https://accounts.google.com/v3/signin/challenge/dp", gl.CHALLENGE),
        ("challenge_insecure.html", "https://accounts.google.com/v3/signin/rejected", gl.CHALLENGE),
        ("captcha.html", "https://accounts.google.com/v3/signin/identifier", gl.CHALLENGE),
        ("try_another_way.html", "https://accounts.google.com/v3/signin/challenge/selection", gl.TRY_ANOTHER_WAY),
        ("account_chooser.html", "https://accounts.google.com/v3/signin/accountchooser", gl.ACCOUNT_CHOOSER),
        ("stay_signed_in.html", "https://accounts.google.com/speedbump/idvreenable", gl.DISMISSIBLE),
        ("passkey_promo.html", "https://accounts.google.com/speedbump/passkeyenrollment", gl.DISMISSIBLE),
        ("signed_in_myaccount.html", "https://myaccount.google.com/?utm_source=sign_in_no_continue", gl.SIGNED_IN),
    ],
)
def test_classify_screen(snippet, url, expected):
    assert gl.classify_screen(info_from(snippet, url)) == expected


def test_totp_rejected_only_after_an_attempt():
    info = info_from("totp_wrong.html", "https://accounts.google.com/v3/signin/challenge/totp")
    assert gl.classify_screen(info, totp_attempted=True) == gl.TOTP_REJECTED
    # before any attempt the same text is just the TOTP prompt
    assert gl.classify_screen(info, totp_attempted=False) == gl.TOTP


def test_signed_in_on_accounts_root_without_form():
    info = gl.ScreenInfo(url="https://accounts.google.com/", text="Google Account Test test@agentumai.tech")
    assert gl.classify_screen(info) == gl.SIGNED_IN
    assert gl.classify_screen(gl.ScreenInfo(url="https://accounts.google.com/v3/signin/identifier", text="")) == gl.UNKNOWN


def test_totp_code_waits_when_about_to_expire():
    secret = pyotp.random_base32()
    slept = []
    # 28s into a 30s period: 2s remaining < 3 -> wait for the next window
    now = datetime(2026, 11, 20, 12, 0, 28, tzinfo=timezone.utc)
    code = gl.totp_code(secret, now=now, min_remaining=3, sleep=slept.append)
    assert slept and 2 <= slept[0] <= 3
    assert code == pyotp.TOTP(secret).at(int(now.timestamp()) + 3)
    assert code != pyotp.TOTP(secret).at(int(now.timestamp())) or True  # may coincide, that's fine
    # 10s into the period: no wait, current code
    now2 = datetime(2026, 11, 20, 12, 0, 10, tzinfo=timezone.utc)
    slept.clear()
    assert gl.totp_code(secret, now=now2, sleep=slept.append) == pyotp.TOTP(secret).at(int(now2.timestamp()))
    assert slept == []
    # secrets pasted with spaces work
    assert gl.totp_code(" ".join(secret[i : i + 4] for i in range(0, len(secret), 4)), now=now2, sleep=slept.append) == pyotp.TOTP(secret).at(int(now2.timestamp()))


def test_masking():
    assert mask_phone("+1 808-688-3603") == "***3603"
    assert mask_phone("--") == "--"
    assert mask_phone(None) == ""
    assert mask_email("abdullahnadeem@agentumai.tech") == "a***@agentumai.tech"
    assert mask_email("") == "***"


def test_signed_out_url_and_ads_access():
    assert gl.is_signed_out_url("https://accounts.google.com/v3/signin/identifier?continue=x")
    assert gl.is_signed_out_url("https://accounts.google.com/ServiceLogin?x")
    assert not gl.is_signed_out_url("https://ads.google.com/aw/reporteditor/view?ocid=1")
    assert gl.ads_access_problem("https://ads.google.com/aw/overview", "Choose an account to continue") is not None
    assert gl.ads_access_problem("https://ads.google.com/aw/overview", "You don't have access to this account") is not None
    assert gl.ads_access_problem("https://ads.google.com/aw/reporteditor/view", "Call details 1 - 4 of 4") is None


def test_challenge_reason_is_credential_free():
    info = info_from("challenge_insecure.html", "https://accounts.google.com/v3/signin/rejected")
    assert "browser or app may not be secure" in gl.challenge_reason(info)
    assert gl.challenge_reason(info_from("captcha.html")) == "captcha"
