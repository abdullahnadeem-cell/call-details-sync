"""Drive Chrome (Selenium) to read the saved "Call details" report for a date range.

Flow per date range:
  1. open the report URL with the range rewritten in reportState (report_url.py)
  2. refuse to continue on any Google sign in / verification page (SESSION_EXPIRED,
     after one self-login retry when credentials are configured)
  3. wait for the report grid (or its "no data" message) -> else TABLE_NOT_RENDERED
  4. verify the date range the page shows (grid text removed); if the URL rewrite
     was ignored, set it through the date picker and verify again -> DATE_RANGE_MISMATCH
  5. read rows: the report's Download > .csv first, grid scraping as fallback
  6. compare the row count with the total the page reports -> ROWCOUNT_MISMATCH

Controls are found by visible text and aria-label (XPath), never by generated
class names. Plain Selenium, no stealth tricks. A screenshot and the page HTML
land in ``debug/`` only on failure. Phone numbers are never logged.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from .errors import (
    DATE_RANGE_MISMATCH,
    PAGE_LOAD_FAILED,
    ROWCOUNT_MISMATCH,
    SESSION_EXPIRED,
    TABLE_NOT_RENDERED,
    SyncError,
)
from .google_login import Credentials, assert_ads_access, ensure_logged_in, is_signed_out_url
from .parse import REPORT_COLUMNS, map_headers, rows_from_csv_text
from .report_url import build_report_url
from .session import inject_cookies, summarize

SIGNIN_TEXT = re.compile(
    r"verify it.s you|sign in to continue|choose an account|2-step verification|"
    r"couldn.t sign you in|use your google account|enter your password|confirm it.s you|"
    r"this browser or app may not be secure",
    re.I,
)
NO_DATA_TEXT = re.compile(
    r"no data to display|there.s no data|no results|nothing to show|no calls|no rows|"
    r"no data for the selected|your report has no data|0 results",
    re.I,
)
TOTAL_RE = re.compile(r"(\d[\d,]*)\s*[-–—]\s*(\d[\d,]*)\s*of\s*(\d[\d,]*)", re.I)
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTHS_LONG = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
LOWER = "abcdefghijklmnopqrstuvwxyz"
UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


@dataclass
class ScrapeResult:
    rows: list[dict]
    rows_method: str  # csv | grid | none
    date_method: str  # url | ui
    reported_total: int | None
    notes: list[str] = field(default_factory=list)


def expected_range_texts(start: date, end: date) -> list[str]:
    """Strings the page may show for the chosen range (en-US and ISO forms)."""

    def md(d: date, long=False) -> str:
        return f"{(MONTHS_LONG if long else MONTHS)[d.month - 1]} {d.day}"

    out: list[str] = []
    if start == end:
        for long in (False, True):
            out.append(f"{md(start, long)}, {start.year}")
        out += [start.isoformat(), f"{start.month}/{start.day}/{start.year}", f"{start.month:02d}/{start.day:02d}/{start.year}"]
        return out
    for dash in ("–", "-", "—"):
        for long in (False, True):
            if start.year == end.year and start.month == end.month:
                out.append(f"{md(start, long)} {dash} {end.day}, {end.year}")
                out.append(f"{md(start, long)}{dash}{end.day}, {end.year}")
            if start.year == end.year:
                out.append(f"{md(start, long)} {dash} {md(end, long)}, {end.year}")
            out.append(f"{md(start, long)}, {start.year} {dash} {md(end, long)}, {end.year}")
        out.append(f"{start.isoformat()} {dash} {end.isoformat()}")
        out.append(f"{start.month}/{start.day}/{start.year} {dash} {end.month}/{end.day}/{end.year}")
    return out


def _squash(text: str) -> str:
    return re.sub(r"[\s   ]+", " ", text or "")


def _int(s: str) -> int:
    return int(s.replace(",", ""))


def _host(url: str) -> str:
    m = re.match(r"^https?://([^/]+)", url or "")
    return m.group(1).lower() if m else ""


def xp_ci(label: str, tags: str = "*") -> str:
    """XPath for elements whose normalized text or aria-label contains ``label`` (case-insensitive)."""
    low = label.lower().replace('"', "")
    return (
        f"//{tags}[contains(translate(normalize-space(.), '{UPPER}', '{LOWER}'), \"{low}\")"
        f" or contains(translate(@aria-label, '{UPPER}', '{LOWER}'), \"{low}\")]"
    )


class ReportScraper:
    def __init__(
        self,
        report_url: str,
        account_tz: str,
        headless: bool = True,
        debug_dir: str = "debug",
        downloads_dir: str = "downloads",
        timeout_ms: int = 90_000,
        log: Callable[[str], None] = print,
        profile_dir: str | None = None,
        credentials: Credentials | None = None,
        accounts_url: str | None = None,
        session_cookies: list[dict] | None = None,
        proxy_url: str | None = None,
    ):
        self.report_url = report_url
        self.account_tz = account_tz
        self.headless = headless
        self.debug_dir = Path(debug_dir)
        self.downloads_dir = Path(downloads_dir).resolve()
        self.timeout_ms = timeout_ms
        self.log = log
        self.profile_dir = profile_dir
        self.credentials = credentials
        self.accounts_url = accounts_url
        self.session_cookies = session_cookies or []
        self.proxy_url = proxy_url
        self.driver: webdriver.Chrome | None = None
        self.login_method: str | None = None
        self.session_outcome: str | None = None  # accepted | rejected | not-used
        self._relogin_done = False

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "ReportScraper":
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.driver = self._build_driver()
        if self.session_cookies:
            n = inject_cookies(self.driver, self.session_cookies)
            self.log(f"[scrape] injected saved session: {summarize(self.session_cookies)}")
            self.login_method = f"saved-session({n})"
            self.session_outcome = "pending"
        elif self.credentials:
            kwargs = {"accounts_url": self.accounts_url} if self.accounts_url else {}
            self.login_method = ensure_logged_in(self.driver, self.credentials, log=self.log, debug_dir=self.debug_dir, **kwargs)
        else:
            self.login_method = "profile-only"
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self.driver:
                self.driver.quit()
        except Exception:  # noqa: BLE001
            pass

    def _build_driver(self) -> webdriver.Chrome:
        opts = Options()
        if self.headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1600,1000")
        opts.add_argument("--lang=en-US")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        if self.profile_dir:
            opts.add_argument(f"--user-data-dir={Path(self.profile_dir).resolve()}")
        if self.proxy_url:
            if "@" in self.proxy_url.split("://", 1)[-1]:
                self.log("[scrape] GADS_PROXY_URL contains credentials; Chrome cannot send proxy passwords, use an IP-allowlisted proxy")
            opts.add_argument(f"--proxy-server={self.proxy_url}")
            self.log(f"[scrape] using proxy host {_host(self.proxy_url) or self.proxy_url.split('@')[-1]}")
        opts.add_experimental_option(
            "prefs",
            {
                "download.default_directory": str(self.downloads_dir),
                "download.prompt_for_download": False,
                "download.directory_upgrade": True,
                "safebrowsing.enabled": True,
                "intl.accept_languages": "en-US,en",
                "credentials_enable_service": False,
                "profile.password_manager_enabled": False,
            },
        )
        binary = os.environ.get("CHROME_BINARY")
        if binary:
            opts.binary_location = binary
        driver_path = os.environ.get("CHROMEDRIVER_PATH")
        service = Service(driver_path) if driver_path else Service()  # Selenium Manager otherwise
        driver = webdriver.Chrome(service=service, options=opts)
        driver.set_page_load_timeout(self.timeout_ms / 1000)
        driver.set_script_timeout(30)
        try:
            driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": self.account_tz})
        except WebDriverException as exc:
            self.log(f"[scrape] could not set browser time zone: {type(exc).__name__}")
        for cmd in ("Browser.setDownloadBehavior", "Page.setDownloadBehavior"):
            try:
                driver.execute_cdp_cmd(cmd, {"behavior": "allow", "downloadPath": str(self.downloads_dir)})
                break
            except WebDriverException:
                continue
        return driver

    # -- public ------------------------------------------------------------
    def fetch(self, start: date, end: date) -> ScrapeResult:
        label = start.isoformat() if start == end else f"{start}_{end}"
        try:
            try:
                return self._fetch(start, end, label)
            except SyncError:
                raise
            except TimeoutException as exc:
                raise SyncError(TABLE_NOT_RENDERED, f"timed out while reading the report: {str(exc).splitlines()[0]}") from exc
        except SyncError as exc:
            if exc.code == SESSION_EXPIRED and self.session_outcome == "pending":
                self.session_outcome = "rejected"
            self._dump(f"{label}-{exc.code}")
            raise

    # -- steps -------------------------------------------------------------
    def _fetch(self, start: date, end: date, label: str) -> ScrapeResult:
        d = self.driver
        url = build_report_url(self.report_url, start, end)
        self._goto_with_retry(url)
        self._assert_signed_in(url)
        assert_ads_access(d, log=self.log)
        self._wait_for_report(url)
        self._assert_signed_in(url)
        if self.session_outcome == "pending":
            self.session_outcome = "accepted"
            self.log("[scrape] saved session accepted: report rendered without any sign-in page")

        date_method = "url"
        if not self._range_matches(start, end):
            self.log("[scrape] page does not show the requested range after URL rewrite; trying the date picker")
            self._set_range_via_ui(start, end)
            self._wait_for_report(url)
            if not self._range_matches(start, end):
                raise SyncError(DATE_RANGE_MISMATCH, f"page shows {self._visible_date_like_text()!r}, wanted {start}..{end}")
            date_method = "ui"
        self.log(f"[scrape] date range {start}..{end} confirmed on page (set via {date_method})")

        total = self._reported_total()
        no_data = self._no_data_shown()
        if total is None and no_data:
            total = 0
        self.log(f"[scrape] page reports total={total} no_data_message={no_data}")

        rows: list[dict] = []
        rows_method = "none"
        if total != 0:
            try:
                rows = self._download_csv(label)
                rows_method = "csv"
            except Exception as exc:  # noqa: BLE001 - fall back to the grid
                self.log(f"[scrape] CSV download not usable ({type(exc).__name__}: {str(exc).splitlines()[0][:200]}); scraping the grid")
                rows = self._scrape_grid(total)
                rows_method = "grid"
        if total is None:
            grid_rows = self._count_grid_rows()
            if grid_rows is None and not rows and not no_data:
                raise SyncError(TABLE_NOT_RENDERED, "no row total, no grid rows and no 'no data' message on the page")
            total = grid_rows if grid_rows is not None else len(rows)
            self.log(f"[scrape] page shows no 'x - y of N' total; using grid row count {total}")
        if len(rows) != total:
            raise SyncError(ROWCOUNT_MISMATCH, f"captured {len(rows)} row(s) via {rows_method} but the page reports {total}")
        return ScrapeResult(rows=rows, rows_method=rows_method, date_method=date_method, reported_total=total)

    def _goto_with_retry(self, url: str) -> None:
        last: Exception | None = None
        for attempt in (1, 2):
            try:
                self.driver.get(url)
                self._wait_document_complete(45)
                return
            except TimeoutException as exc:
                last = exc
                self.log(f"[scrape] page load attempt {attempt} timed out")
            except WebDriverException as exc:
                last = exc
                self.log(f"[scrape] page load attempt {attempt} failed: {type(exc).__name__}: {str(exc).splitlines()[0][:200]}")
            time.sleep(5)
        raise SyncError(PAGE_LOAD_FAILED, f"could not load the report page: {type(last).__name__}")

    def _wait_document_complete(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            try:
                if self.driver.execute_script("return document.readyState") == "complete":
                    return
            except WebDriverException:
                pass
            time.sleep(0.5)

    def _assert_signed_in(self, report_url: str) -> None:
        url = self.driver.current_url or ""
        signed_out = is_signed_out_url(url)
        if not signed_out:
            try:
                title = self.driver.title or ""
            except WebDriverException:
                title = ""
            if re.search(r"sign in|verify", title, re.I) and "ads.google.com/aw" not in url:
                signed_out = True
        if not signed_out and self._signin_text_shown():
            signed_out = True
        if not signed_out:
            return
        if self.session_outcome == "pending":
            self.session_outcome = "rejected"
            self.log(f"[scrape] saved session rejected: landed on {_host(url)} ({(self.driver.title or '')[:60]!r})")
        if self.credentials and not self._relogin_done:
            self._relogin_done = True
            self.log(f"[scrape] landed on a sign-in page at {_host(url)}; signing in with the configured account")
            kwargs = {"accounts_url": self.accounts_url} if self.accounts_url else {}
            self.login_method = ensure_logged_in(self.driver, self.credentials, log=self.log, debug_dir=self.debug_dir, **kwargs)
            self._goto_with_retry(report_url)
            if not is_signed_out_url(self.driver.current_url or "") and not self._signin_text_shown():
                return
        raise SyncError(SESSION_EXPIRED, f"landed on a Google sign in page ({_host(self.driver.current_url)}) instead of the report")

    def _wait_for_report(self, report_url: str) -> None:
        deadline = time.time() + self.timeout_ms / 1000
        while time.time() < deadline:
            if self._report_visible() or self._no_data_shown() or self._reported_total() is not None:
                time.sleep(1.5)  # let the grid finish painting
                return
            if self._signin_text_shown() or is_signed_out_url(self.driver.current_url or ""):
                self._assert_signed_in(report_url)  # raises SESSION_EXPIRED, or signs in again and reloads
                continue
            time.sleep(1)
        raise SyncError(TABLE_NOT_RENDERED, "report grid did not render and no 'no data' message appeared")

    # -- page inspection ---------------------------------------------------
    def _js(self, script: str, *args):
        try:
            return self.driver.execute_script(script, *args)
        except WebDriverException:
            return None

    def _body_text(self, limit: int = 200_000) -> str:
        text = self._js("return (document.body && document.body.innerText) || ''") or ""
        return _squash(text)[:limit]

    def _report_visible(self) -> bool:
        n = self._js(
            "return document.querySelectorAll('[role=row]:has([role=cell],[role=gridcell]), tr:has(td)').length"
            " + (document.querySelectorAll('[role=columnheader]').length >= 3 ? 1 : 0)"
        )
        return bool(n)

    def _no_data_shown(self) -> bool:
        return bool(NO_DATA_TEXT.search(self._body_text()))

    def _signin_text_shown(self) -> bool:
        return bool(SIGNIN_TEXT.search(self._body_text(limit=20_000))) and not self._report_visible()

    def _chrome_text(self) -> str:
        """Page text with the report grid removed, so dates inside data rows cannot pass for the date control."""
        text = self._js(
            """let out = document.body.innerText || '';
               for (const g of document.querySelectorAll('[role=grid],[role=treegrid],[role=table],table,[role=rowgroup]')) {
                 const t = (g.innerText || '').trim();
                 if (t) out = out.split(t).join(' ');
               }
               return out;"""
        )
        return _squash(text) if text is not None else self._body_text()

    def _range_matches(self, start: date, end: date) -> bool:
        chrome = self._chrome_text()
        hits = [t for t in expected_range_texts(start, end) if t in chrome]
        if hits:
            self.log(f"[scrape] range text on page: {hits[0]!r}")
            return True
        if start == end:
            for long in (False, True):
                mon = (MONTHS_LONG if long else MONTHS)[start.month - 1]
                if re.search(rf"{mon} {start.day}\s*[-–—]\s*{start.day}, {start.year}", chrome):
                    return True
        return False

    def _visible_date_like_text(self) -> str:
        body = self._chrome_text()
        m = re.findall(r"(?:" + "|".join(MONTHS) + r")[a-z]* \d{1,2}(?:, \d{4})?(?:\s*[-–—]\s*(?:(?:" + "|".join(MONTHS) + r")[a-z]* )?\d{1,2}(?:, \d{4})?)?", body)
        return "; ".join(m[:5])

    def _reported_total(self) -> int | None:
        matches = TOTAL_RE.findall(self._body_text())
        if not matches:
            return None
        return max(_int(m[2]) for m in matches)

    # -- locating controls -------------------------------------------------
    def _visible(self, xpath: str):
        for el in self.driver.find_elements(By.XPATH, xpath):
            try:
                if el.is_displayed():
                    return el
            except WebDriverException:
                continue
        return None

    def _click(self, el) -> None:
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        try:
            el.click()
        except WebDriverException:
            self.driver.execute_script("arguments[0].click();", el)

    def _wait_visible(self, xpath: str, seconds: float):
        end = time.time() + seconds
        while time.time() < end:
            el = self._visible(xpath)
            if el is not None:
                return el
            time.sleep(0.3)
        return None

    # -- date picker fallback ----------------------------------------------
    def _set_range_via_ui(self, start: date, end: date) -> None:
        """Best effort: open the date picker, choose Custom, type both dates, Apply."""
        picker = (
            self._visible(xp_ci("date range", "*[self::button or @role='button']"))
            or self._visible(f"//*[self::button or @role='button'][contains(normalize-space(.), '{start.year}') or contains(normalize-space(.), '{end.year}') or contains(normalize-space(.), '2026')]")
        )
        if picker is None:
            raise SyncError(DATE_RANGE_MISMATCH, "could not find the date range control")
        self._click(picker)
        time.sleep(0.8)
        custom = self._visible(xp_ci("custom", "*[self::li or self::button or @role='menuitem' or @role='option' or @role='button' or @role='radio']"))
        if custom is not None:
            self._click(custom)
            time.sleep(0.5)
        def field_xpath(word: str) -> str:
            return (
                xp_ci(word, "input")
                + f" | //label[contains(translate(normalize-space(.), '{UPPER}', '{LOWER}'), '{word}')]//input"
                + f" | //input[@id=//label[contains(translate(normalize-space(.), '{UPPER}', '{LOWER}'), '{word}')]/@for]"
            )

        start_box = self._wait_visible(field_xpath("start"), 5)
        end_box = next((el for el in self.driver.find_elements(By.XPATH, field_xpath("end")) if el.is_displayed() and el != start_box), None)
        if start_box is None or end_box is None:
            raise SyncError(DATE_RANGE_MISMATCH, "date picker start/end fields not found")
        for box, dd in ((start_box, start), (end_box, end)):
            self._click(box)
            box.send_keys(Keys.CONTROL, "a")
            box.send_keys(f"{MONTHS[dd.month - 1]} {dd.day}, {dd.year}")
            box.send_keys(Keys.TAB)
        apply_btn = self._visible(xp_ci("apply", "*[self::button or @role='button']"))
        if apply_btn is None:
            raise SyncError(DATE_RANGE_MISMATCH, "date picker Apply button not found")
        self._click(apply_btn)
        self._wait_document_complete(30)
        time.sleep(1.5)

    # -- rows: CSV download ------------------------------------------------
    def _download_csv(self, label: str) -> list[dict]:
        before = {p.name for p in self.downloads_dir.iterdir()}
        button = self._visible(xp_ci("download", "*[self::button or @role='button' or self::a]"))
        if button is None:
            raise RuntimeError("no Download button found")
        self._click(button)
        time.sleep(0.6)
        item = None
        for xp in (
            "//*[@role='menuitem' or @role='option' or self::li][translate(normalize-space(.), 'CSV.', 'csv')='csv']",
            "//*[@role='menuitem' or @role='option' or self::li][contains(translate(normalize-space(.), 'CSV', 'csv'), 'csv') and not(contains(translate(normalize-space(.), 'EXCEL', 'excel'), 'excel'))]",
            "//*[@role='menuitem' or @role='option' or self::li][contains(translate(normalize-space(.), 'CSV', 'csv'), 'csv')]",
            "//*[translate(normalize-space(.), 'CSV.', 'csv')='csv']",
        ):
            item = self._wait_visible(xp, 3)
            if item is not None:
                break
        if item is None:
            self.driver.switch_to.active_element.send_keys(Keys.ESCAPE)
            raise RuntimeError("no CSV option in the Download menu")
        self._click(item)
        path = self._wait_for_download(before, timeout=60)
        target = self.downloads_dir / f"call_details_{label}.csv"
        if path != target:
            if target.exists():
                target.unlink()
            path.rename(target)
        data = target.read_bytes()
        text = data.decode("utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else data.decode("utf-8-sig")
        rows = rows_from_csv_text(text)
        self.log(f"[scrape] CSV download: {len(rows)} data row(s), headers={list(rows[0].keys()) if rows else 'n/a'}")
        return rows

    def _wait_for_download(self, before: set[str], timeout: float) -> Path:
        end = time.time() + timeout
        last_size = -1
        candidate: Path | None = None
        while time.time() < end:
            new = [p for p in self.downloads_dir.iterdir() if p.name not in before]
            finished = [p for p in new if not p.name.endswith((".crdownload", ".tmp", ".part"))]
            if finished:
                candidate = max(finished, key=lambda p: p.stat().st_mtime)
                size = candidate.stat().st_size
                if size > 0 and size == last_size:
                    return candidate
                last_size = size
            time.sleep(0.5)
        raise RuntimeError("download did not finish in time")

    # -- rows: grid scraping -----------------------------------------------
    def _count_grid_rows(self) -> int | None:
        n = self._js("return document.querySelectorAll('[role=row]:has([role=cell],[role=gridcell]), tr:has(td)').length")
        return n if n else None

    def _grid_headers(self) -> list[str]:
        headers = self._js(
            "return Array.from(document.querySelectorAll('[role=columnheader], th')).map(h => (h.innerText||'').trim()).filter(Boolean)"
        ) or []
        headers = [_squash(h) for h in headers]
        return headers or [f"col{i}" for i in range(len(REPORT_COLUMNS))]

    def _maximize_rows_per_page(self) -> None:
        combo = self._visible(xp_ci("rows per page", "*[@role='combobox' or @role='button' or self::button or self::select]")) or self._visible(
            xp_ci("show rows", "*[@role='combobox' or @role='button' or self::button or self::select]")
        )
        if combo is None:
            return
        try:
            if combo.tag_name.lower() == "select":
                from selenium.webdriver.support.ui import Select

                sel = Select(combo)
                best = max((o for o in sel.options if o.text.strip().replace(",", "").isdigit()), key=lambda o: int(o.text.strip().replace(",", "")), default=None)
                if best is not None:
                    sel.select_by_visible_text(best.text)
                    self.log(f"[scrape] rows per page set to {best.text.strip()}")
                return
            self._click(combo)
            time.sleep(0.8)
            options = [o for o in self.driver.find_elements(By.XPATH, "//*[@role='option' or @role='menuitem' or self::li]") if o.is_displayed()]
            best, best_val = None, -1
            for o in options:
                txt = o.text.strip().replace(",", "")
                if txt.isdigit() and int(txt) > best_val:
                    best, best_val = o, int(txt)
            if best is not None:
                self._click(best)
                time.sleep(1.5)
                self.log(f"[scrape] rows per page set to {best_val}")
            else:
                self.driver.switch_to.active_element.send_keys(Keys.ESCAPE)
        except WebDriverException as exc:
            self.log(f"[scrape] could not change rows per page: {type(exc).__name__}")

    def _read_visible_rows(self) -> list[list[dict]]:
        return self._js(
            """const rows = Array.from(document.querySelectorAll('[role=row], tr'))
                 .filter(r => r.querySelector('[role=cell], [role=gridcell], td'));
               return rows.map(r => Array.from(r.querySelectorAll('[role=cell], [role=gridcell], td')).map(c => {
                 const a = c.querySelector('a[href]');
                 return { text: (c.innerText || '').replace(/\\s+/g, ' ').trim(), href: a ? a.href : null };
               }));"""
        ) or []

    def _scrape_grid(self, total: int | None) -> list[dict]:
        self._maximize_rows_per_page()
        headers = self._grid_headers()
        mapping = map_headers(headers, warn=self.log) if len(headers) >= 2 else {}
        recording_header = next((h for h, c in mapping.items() if c == "recording_url"), None)
        collected: dict[str, dict] = {}
        pages_seen = 0
        while True:
            pages_seen += 1
            stable = 0
            last_count = -1
            for _ in range(200):  # virtualized rows: scroll until no new rows appear
                for cells in self._read_visible_rows():
                    if len(cells) < 2:
                        continue
                    row = {}
                    for i, h in enumerate(headers):
                        cell = cells[i] if i < len(cells) else {"text": "", "href": None}
                        value = cell["text"]
                        if h == recording_header and cell["href"] and not re.search(r"https?://", value):
                            value = cell["href"]
                        row[h] = value
                    key = "\x1f".join(f"{c['text']}|{c['href'] or ''}" for c in cells) + f"|p{pages_seen}"
                    collected.setdefault(key, row)
                if len(collected) == last_count:
                    stable += 1
                    if stable >= 3:
                        break
                else:
                    stable = 0
                    last_count = len(collected)
                self._js("window.scrollBy(0, 800); for (const el of document.querySelectorAll('[role=grid],[role=table],table')) { el.scrollTop += 800; if (el.parentElement) el.parentElement.scrollTop += 800; }")
                time.sleep(0.4)
            if total is not None and len(collected) >= total:
                break
            nxt = self._visible(xp_ci("next page", "*[self::button or @role='button']")) or self._visible(xp_ci("next", "*[self::button or @role='button']"))
            if nxt is None or not nxt.is_enabled() or nxt.get_attribute("aria-disabled") == "true":
                break
            self._click(nxt)
            time.sleep(2)
        rows = list(collected.values())
        self.log(f"[scrape] grid: {len(rows)} row(s) across {pages_seen} page(s), headers={headers}")
        return rows

    # -- debugging ---------------------------------------------------------
    def _dump(self, name: str) -> None:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
        try:
            self.driver.save_screenshot(str(self.debug_dir / f"{safe}.png"))
            (self.debug_dir / f"{safe}.html").write_text(self.driver.page_source, encoding="utf-8")
            self.log(f"[scrape] saved debug/{safe}.png and debug/{safe}.html (url host: {_host(self.driver.current_url)})")
        except Exception as exc:  # noqa: BLE001
            self.log(f"[scrape] could not save debug output: {type(exc).__name__}")
