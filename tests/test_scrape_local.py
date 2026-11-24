"""Runs the real Selenium scraper against local stand-in pages:
tests/fixtures/fake_report (the Google Ads report) and tests/fixtures/fake_login (Google sign-in).

Skipped when Chrome + chromedriver are not available. Locally you can point
CHROME_BINARY and CHROMEDRIVER_PATH at a matching pair; on GitHub's ubuntu runners
Selenium Manager finds them by itself.
"""
import os
import socket
import threading
from datetime import date
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pyotp
import pytest

from src.errors import SyncError
from src.google_login import Credentials, ensure_logged_in
from tests.conftest import REPORT_STATE

FIXTURES = Path(__file__).parent / "fixtures"
TOTP_SECRET = pyotp.random_base32()


def _chrome_available() -> bool:
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

        o = Options()
        o.add_argument("--headless=new")
        o.add_argument("--no-sandbox")
        o.add_argument("--disable-dev-shm-usage")
        if os.environ.get("CHROME_BINARY"):
            o.binary_location = os.environ["CHROME_BINARY"]
        svc = Service(os.environ["CHROMEDRIVER_PATH"]) if os.environ.get("CHROMEDRIVER_PATH") else Service()
        webdriver.Chrome(service=svc, options=o).quit()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _chrome_available(), reason="Chrome/chromedriver not available to Selenium")


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    httpd = ThreadingHTTPServer(("127.0.0.1", port), partial(QuietHandler, directory=str(FIXTURES)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


def creds(totp=True):
    return Credentials("test@agentumai.tech", "correct horse", TOTP_SECRET if totp else None)


@pytest.fixture
def scraper_factory(server, tmp_path):
    from src.scrape import ReportScraper

    def make(mode="normal", timeout_ms=20_000, login_mode=None, credentials=None, profile=False):
        url = f"{server}/fake_report/index.html?ocid=1&mode={mode}&reportState={REPORT_STATE}"
        return ReportScraper(
            report_url=url,
            account_tz="Pacific/Honolulu",
            headless=True,
            debug_dir=str(tmp_path / "debug"),
            downloads_dir=str(tmp_path / "downloads"),
            timeout_ms=timeout_ms,
            log=lambda m: print(m),
            profile_dir=str(tmp_path / "profile") if profile else None,
            credentials=credentials,
            accounts_url=f"{server}/fake_login/index.html?mode={login_mode}" if login_mode else None,
        )

    return make


# ---- report reading -----------------------------------------------------------

def test_csv_path_single_day(scraper_factory, tmp_path):
    with scraper_factory(profile=True) as s:
        r = s.fetch(date(2026, 11, 20), date(2026, 11, 20))
    assert r.rows_method == "csv" and r.date_method == "url" and r.reported_total == 4
    assert len(r.rows) == 4
    assert r.rows[0]["Caller ID"] == "+1 808-688-3603"
    assert r.rows[1]["Call recording"] == "https://ads.google.com/recordings/abc123"
    assert (tmp_path / "downloads" / "call_details_2026-11-20.csv").exists()
    assert not list((tmp_path / "debug").glob("*.png"))
    assert (tmp_path / "profile").exists()  # user-data-dir was used


def test_browser_timezone_is_account_zone(scraper_factory):
    with scraper_factory() as s:
        s.driver.get("data:text/html,<p>tz</p>")
        assert s.driver.execute_script("return Intl.DateTimeFormat().resolvedOptions().timeZone") == "Pacific/Honolulu"


def test_csv_path_range(scraper_factory):
    with scraper_factory() as s:
        r = s.fetch(date(2026, 11, 1), date(2026, 11, 7))
    assert len(r.rows) == 4 and r.date_method == "url"


def test_grid_fallback_when_no_download(scraper_factory):
    with scraper_factory("nocsv") as s:
        r = s.fetch(date(2026, 11, 20), date(2026, 11, 20))
    assert r.rows_method == "grid" and len(r.rows) == 4
    assert list(r.rows[0].keys())[0] == "Start time"
    assert r.rows[0]["Campaign"] == "TCA - Search - Brand"
    assert r.rows[1]["Call recording"] == "https://ads.google.com/recordings/abc123"
    assert r.rows[0]["Call recording"] == "--"


def test_no_data_is_zero_rows(scraper_factory):
    with scraper_factory("nodata") as s:
        r = s.fetch(date(2026, 11, 20), date(2026, 11, 20))
    assert r.rows == [] and r.reported_total == 0 and r.rows_method == "none"


def test_signin_page_is_session_expired(scraper_factory, tmp_path):
    with scraper_factory("signin") as s, pytest.raises(SyncError) as e:
        s.fetch(date(2026, 11, 20), date(2026, 11, 20))
    assert e.value.code == "SESSION_EXPIRED"
    assert list((tmp_path / "debug").glob("*SESSION_EXPIRED.png"))
    assert list((tmp_path / "debug").glob("*SESSION_EXPIRED.html"))


def test_rowcount_mismatch(scraper_factory):
    with scraper_factory("mismatch") as s, pytest.raises(SyncError) as e:
        s.fetch(date(2026, 11, 20), date(2026, 11, 20))
    assert e.value.code == "ROWCOUNT_MISMATCH"


def test_table_never_renders(scraper_factory):
    with scraper_factory("hang", timeout_ms=6_000) as s, pytest.raises(SyncError) as e:
        s.fetch(date(2026, 11, 20), date(2026, 11, 20))
    assert e.value.code == "TABLE_NOT_RENDERED"


def test_ui_date_picker_fallback(scraper_factory):
    with scraper_factory("ignore") as s:
        r = s.fetch(date(2026, 11, 20), date(2026, 11, 20))
    assert r.date_method == "ui" and len(r.rows) == 4


# ---- self login -----------------------------------------------------------------

def test_login_then_report(scraper_factory):
    with scraper_factory(login_mode="ok", credentials=creds()) as s:
        assert s.login_method == "password+totp"
        r = s.fetch(date(2026, 11, 20), date(2026, 11, 20))
    assert len(r.rows) == 4


def test_login_already_signed_in(scraper_factory, server):
    with scraper_factory() as s:
        how = ensure_logged_in(s.driver, creds(), log=print, accounts_url=f"{server}/fake_login/signedin.html", step_pause=0.2)
    assert how == "already"


def test_login_account_chooser_and_password_only(scraper_factory, server):
    with scraper_factory() as s:
        how = ensure_logged_in(s.driver, creds(), log=print, accounts_url=f"{server}/fake_login/index.html?mode=chooser", step_pause=0.2)
        assert how == "password+totp"
    with scraper_factory() as s:
        how = ensure_logged_in(s.driver, creds(), log=print, accounts_url=f"{server}/fake_login/index.html?mode=pwonly", step_pause=0.2)
        assert how == "password"


@pytest.mark.parametrize(
    "mode,totp,code",
    [
        ("badpass", True, "LOGIN_BAD_CREDENTIALS"),
        ("noaccount", True, "LOGIN_BAD_CREDENTIALS"),
        ("challenge", True, "LOGIN_CHALLENGE"),
        ("badtotp", True, "LOGIN_TOTP_REJECTED"),
        ("ok", False, "LOGIN_TOTP_MISSING"),
    ],
)
def test_login_failures(scraper_factory, server, tmp_path, mode, totp, code):
    with scraper_factory() as s, pytest.raises(SyncError) as e:
        ensure_logged_in(s.driver, creds(totp), log=print, debug_dir=tmp_path / "debug", accounts_url=f"{server}/fake_login/index.html?mode={mode}", step_pause=0.2)
    assert e.value.code == code
    assert list((tmp_path / "debug").glob("login-*.png")), "a screenshot of the login page is saved on failure"
    assert "correct horse" not in str(e.value) and "correct horse" not in "".join(p.name for p in (tmp_path / "debug").iterdir())
