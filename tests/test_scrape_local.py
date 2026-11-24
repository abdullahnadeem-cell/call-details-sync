"""Runs the real scraper against tests/fixtures/fake_report (a local stand-in page).

Skipped when Chromium cannot be launched. Set PLAYWRIGHT_CHROMIUM_EXECUTABLE to
use a specific browser binary.
"""
import json
import os
import socket
import threading
from datetime import date
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from src.errors import SyncError
from tests.conftest import REPORT_STATE

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "fake_report"


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        kwargs = {}
        exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        if exe:
            kwargs["executable_path"] = exe
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True, **kwargs).close()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _chromium_available(), reason="Chromium not available to Playwright")


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass


@pytest.fixture(scope="module")
def server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    httpd = ThreadingHTTPServer(("127.0.0.1", port), partial(QuietHandler, directory=str(FIXTURE_DIR)))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


@pytest.fixture
def scraper_factory(server, tmp_path):
    from src.scrape import ReportScraper

    state = tmp_path / "storage_state.json"
    state.write_text(json.dumps({"cookies": [], "origins": []}))

    def make(mode="normal", timeout_ms=20_000):
        url = f"{server}/index.html?ocid=1&mode={mode}&reportState={REPORT_STATE}"
        return ReportScraper(
            report_url=url,
            storage_state_path=str(state),
            account_tz="Pacific/Honolulu",
            headless=True,
            debug_dir=str(tmp_path / "debug"),
            downloads_dir=str(tmp_path / "downloads"),
            timeout_ms=timeout_ms,
            log=lambda m: print(m),
        )

    return make


def test_csv_path_single_day(scraper_factory, tmp_path):
    with scraper_factory() as s:
        r = s.fetch(date(2026, 11, 20), date(2026, 11, 20))
    assert r.rows_method == "csv" and r.date_method == "url" and r.reported_total == 4
    assert len(r.rows) == 4
    assert r.rows[0]["Caller ID"] == "+1 808-688-3603"
    assert r.rows[1]["Call recording"] == "https://ads.google.com/recordings/abc123"
    assert not list((tmp_path / "debug").glob("*.png"))


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
    assert r.rows[1]["Call recording"] == "https://ads.google.com/recordings/abc123"  # href captured
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
