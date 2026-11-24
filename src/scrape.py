"""Drive Chromium to read the saved "Call details" report for a date range.

Flow per date range:
  1. open the report URL with the range rewritten in reportState (report_url.py)
  2. refuse to continue on any Google sign in / verification page -> SESSION_EXPIRED
  3. wait for the report grid (or its "no data" message) -> else TABLE_NOT_RENDERED
  4. verify the date range the page shows; if the URL rewrite was ignored, set
     it through the date picker and verify again -> else DATE_RANGE_MISMATCH
  5. read rows: the report's Download > .csv first, grid scraping as fallback
  6. compare the row count with the total the page reports -> ROWCOUNT_MISMATCH

Role/text based locators only. A screenshot and the page HTML land in
``debug/`` on any failure. Cookie values are never logged.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

from playwright.sync_api import Browser, BrowserContext, Download, Locator, Page, TimeoutError as PWTimeout, sync_playwright

from .errors import (
    DATE_RANGE_MISMATCH,
    PAGE_LOAD_FAILED,
    ROWCOUNT_MISMATCH,
    SESSION_EXPIRED,
    TABLE_NOT_RENDERED,
    SyncError,
)
from .parse import REPORT_COLUMNS, map_headers, rows_from_csv_text
from .report_url import build_report_url

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
SIGNIN_HOSTS = ("accounts.google.com", "accounts.youtube.com", "myaccount.google.com")
SIGNIN_URL_HINTS = ("/signin", "/ServiceLogin", "/challenge", "/speedbump", "/v3/signin", "InteractiveLogin")
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
            out += [f"{md(start, long)}, {start.year}"]
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


class ReportScraper:
    def __init__(
        self,
        report_url: str,
        storage_state_path: str,
        account_tz: str,
        headless: bool = True,
        debug_dir: str = "debug",
        downloads_dir: str = "downloads",
        timeout_ms: int = 90_000,
        log: Callable[[str], None] = print,
    ):
        self.report_url = report_url
        self.storage_state_path = storage_state_path
        self.account_tz = account_tz
        self.headless = headless
        self.debug_dir = Path(debug_dir)
        self.downloads_dir = Path(downloads_dir)
        self.timeout_ms = timeout_ms
        self.log = log
        self._pw = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "ReportScraper":
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        launch_kwargs = dict(headless=self.headless, args=["--disable-blink-features=AutomationControlled", "--lang=en-US"])
        exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        if exe:
            launch_kwargs["executable_path"] = exe
        self.browser = self._pw.chromium.launch(**launch_kwargs)
        self.context = self.browser.new_context(
            storage_state=self.storage_state_path,
            accept_downloads=True,
            viewport={"width": 1600, "height": 1000},
            locale="en-US",
            timezone_id=self.account_tz,
            user_agent=USER_AGENT,
        )
        self.context.set_default_timeout(self.timeout_ms)
        return self

    def __exit__(self, *exc) -> None:
        for closer in (lambda: self.context and self.context.close(), lambda: self.browser and self.browser.close(), lambda: self._pw and self._pw.stop()):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass

    # -- public ------------------------------------------------------------
    def fetch(self, start: date, end: date) -> ScrapeResult:
        label = start.isoformat() if start == end else f"{start}_{end}"
        page = self.context.new_page()
        try:
            try:
                return self._fetch(page, start, end, label)
            except SyncError:
                raise
            except PWTimeout as exc:
                raise SyncError(TABLE_NOT_RENDERED, f"timed out while reading the report: {str(exc).splitlines()[0]}") from exc
        except SyncError as exc:
            self._dump(page, f"{label}-{exc.code}")
            raise
        finally:
            page.close()

    # -- steps -------------------------------------------------------------
    def _fetch(self, page: Page, start: date, end: date, label: str) -> ScrapeResult:
        url = build_report_url(self.report_url, start, end)
        self._goto_with_retry(page, url)
        self._assert_signed_in(page)
        self._wait_for_report(page)
        self._assert_signed_in(page)

        date_method = "url"
        if not self._range_matches(page, start, end):
            self.log("[scrape] page does not show the requested range after URL rewrite; trying the date picker")
            self._set_range_via_ui(page, start, end)
            self._wait_for_report(page)
            if not self._range_matches(page, start, end):
                shown = self._visible_date_like_text(page)
                raise SyncError(DATE_RANGE_MISMATCH, f"page shows {shown!r}, wanted {start}..{end}")
            date_method = "ui"
        self.log(f"[scrape] date range {start}..{end} confirmed on page (set via {date_method})")

        total = self._reported_total(page)
        no_data = self._no_data_shown(page)
        if total is None and no_data:
            total = 0
        self.log(f"[scrape] page reports total={total} no_data_message={no_data}")

        rows: list[dict] = []
        rows_method = "none"
        if total != 0:
            try:
                rows = self._download_csv(page, label)
                rows_method = "csv"
            except Exception as exc:  # noqa: BLE001 - fall back to the grid
                self.log(f"[scrape] CSV download not usable ({type(exc).__name__}: {str(exc).splitlines()[0][:200]}); scraping the grid")
                rows = self._scrape_grid(page, total)
                rows_method = "grid"
        if total is None:
            grid_rows = self._count_grid_rows(page)
            if grid_rows is None and not rows and not no_data:
                raise SyncError(TABLE_NOT_RENDERED, "no row total, no grid rows and no 'no data' message on the page")
            total = grid_rows if grid_rows is not None else len(rows)
            self.log(f"[scrape] page shows no 'x - y of N' total; using grid row count {total}")
        if len(rows) != total:
            raise SyncError(ROWCOUNT_MISMATCH, f"captured {len(rows)} row(s) via {rows_method} but the page reports {total}")
        return ScrapeResult(rows=rows, rows_method=rows_method, date_method=date_method, reported_total=total)

    def _goto_with_retry(self, page: Page, url: str) -> None:
        last: Exception | None = None
        for attempt in (1, 2):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=45_000)
                except PWTimeout:
                    self.log("[scrape] network did not go idle in 45s; continuing")
                return
            except PWTimeout as exc:
                last = exc
                self.log(f"[scrape] page load attempt {attempt} timed out")
            except Exception as exc:  # noqa: BLE001
                last = exc
                self.log(f"[scrape] page load attempt {attempt} failed: {type(exc).__name__}: {str(exc).splitlines()[0]}")
            time.sleep(5)
        raise SyncError(PAGE_LOAD_FAILED, f"could not load the report page: {type(last).__name__}")

    def _assert_signed_in(self, page: Page) -> None:
        url = page.url or ""
        host = _host(url)
        if any(host == h or host.endswith("." + h) for h in SIGNIN_HOSTS) or any(h.lower() in url.lower() for h in SIGNIN_URL_HINTS):
            raise SyncError(SESSION_EXPIRED, f"landed on a Google sign in page ({host}); re-run scripts/login_once.py and update GADS_STORAGE_STATE")
        try:
            title = page.title() or ""
        except Exception:  # noqa: BLE001
            title = ""
        if re.search(r"sign in|verify", title, re.I) and "ads.google.com/aw" not in url:
            raise SyncError(SESSION_EXPIRED, f"page title {title!r} looks like a sign in page")
        if self._signin_text_shown(page):
            raise SyncError(SESSION_EXPIRED, "page asks to sign in or verify identity; re-run scripts/login_once.py and update GADS_STORAGE_STATE")

    def _signin_text_shown(self, page: Page) -> bool:
        body = self._body_text(page, limit=20_000)
        return bool(SIGNIN_TEXT.search(body)) and not self._report_visible(page)

    def _wait_for_report(self, page: Page) -> None:
        deadline = time.time() + self.timeout_ms / 1000
        while time.time() < deadline:
            if self._report_visible(page) or self._no_data_shown(page) or self._reported_total(page) is not None:
                page.wait_for_timeout(1500)  # let the grid finish painting
                return
            self._assert_signed_in(page)
            page.wait_for_timeout(1000)
        if self._signin_text_shown(page):
            raise SyncError(SESSION_EXPIRED, "page asks to sign in or verify identity instead of showing the report")
        raise SyncError(TABLE_NOT_RENDERED, "report grid did not render and no 'no data' message appeared")

    def _report_visible(self, page: Page) -> bool:
        try:
            if self._grid_rows_locator(page).count() > 0:
                return True
            if page.get_by_role("columnheader").count() >= 3:
                return True
        except Exception:  # noqa: BLE001
            return False
        return False

    def _no_data_shown(self, page: Page) -> bool:
        return bool(NO_DATA_TEXT.search(self._body_text(page)))

    def _body_text(self, page: Page, limit: int = 200_000) -> str:
        try:
            return _squash(page.locator("body").inner_text(timeout=10_000))[:limit]
        except Exception:  # noqa: BLE001
            return ""

    def _chrome_text(self, page: Page) -> str:
        """Page text with the report grid removed, so dates inside data rows cannot
        pass for the date range control."""
        try:
            text = page.evaluate(
                """() => {
                  let out = document.body.innerText || '';
                  for (const g of document.querySelectorAll('[role=grid],[role=treegrid],[role=table],table,[role=rowgroup]')) {
                    const t = (g.innerText || '').trim();
                    if (t) out = out.split(t).join(' ');
                  }
                  return out;
                }"""
            )
            return _squash(text)
        except Exception:  # noqa: BLE001
            return self._body_text(page)

    def _range_matches(self, page: Page, start: date, end: date) -> bool:
        chrome = self._chrome_text(page)
        hits = [t for t in expected_range_texts(start, end) if t in chrome]
        if hits:
            self.log(f"[scrape] range text on page: {hits[0]!r}")
            return True
        # a single day may also be rendered as a range of the same day, e.g. "Nov 20 – 20, 2026"
        if start == end:
            for long in (False, True):
                mon = (MONTHS_LONG if long else MONTHS)[start.month - 1]
                if re.search(rf"{mon} {start.day}\s*[-–—]\s*{start.day}, {start.year}", chrome):
                    return True
        return False

    def _visible_date_like_text(self, page: Page) -> str:
        body = self._chrome_text(page)
        m = re.findall(r"(?:" + "|".join(MONTHS) + r")[a-z]* \d{1,2}(?:, \d{4})?(?:\s*[-–—]\s*(?:(?:" + "|".join(MONTHS) + r")[a-z]* )?\d{1,2}(?:, \d{4})?)?", body)
        return "; ".join(m[:5])

    def _set_range_via_ui(self, page: Page, start: date, end: date) -> None:
        """Best effort: open the date picker, choose Custom, type both dates, Apply."""
        picker = page.get_by_role("button", name=re.compile(r"\d{4}|date range|custom", re.I)).first
        picker.click(timeout=15_000)
        page.wait_for_timeout(800)
        custom = page.get_by_role("menuitem", name=re.compile(r"^custom", re.I)).or_(page.get_by_text(re.compile(r"^custom$", re.I)))
        if custom.count():
            custom.first.click(timeout=10_000)
            page.wait_for_timeout(500)
        start_box = page.get_by_role("textbox", name=re.compile(r"start", re.I)).first
        end_box = page.get_by_role("textbox", name=re.compile(r"end", re.I)).first
        for box, d in ((start_box, start), (end_box, end)):
            box.click(timeout=10_000)
            box.press("Control+A")
            box.fill(f"{MONTHS[d.month - 1]} {d.day}, {d.year}")
            box.press("Tab")
        page.get_by_role("button", name=re.compile(r"^apply$", re.I)).first.click(timeout=10_000)
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except PWTimeout:
            pass

    def _reported_total(self, page: Page) -> int | None:
        body = self._body_text(page)
        matches = TOTAL_RE.findall(body)
        if not matches:
            return None
        totals = {_int(m[2]) for m in matches}
        # the report grid's paginator; if several paginators exist, prefer the largest plausible total
        return max(totals)

    # -- rows: CSV download ---------------------------------------------------
    def _download_csv(self, page: Page, label: str) -> list[dict]:
        button = page.get_by_role("button", name=re.compile(r"download", re.I))
        if button.count() == 0:
            button = page.get_by_label(re.compile(r"download", re.I))
        if button.count() == 0:
            raise RuntimeError("no Download button found")
        button.first.click(timeout=15_000)
        page.wait_for_timeout(600)
        item = page.get_by_role("menuitem", name=re.compile(r"^\s*\.?csv\s*$", re.I))
        if item.count() == 0:
            item = page.get_by_role("menuitem", name=re.compile(r"csv", re.I)).filter(has_not_text=re.compile("excel", re.I))
        if item.count() == 0:
            item = page.get_by_role("menuitem", name=re.compile(r"csv", re.I))
        if item.count() == 0:
            item = page.get_by_text(re.compile(r"^\s*\.?csv\s*$", re.I))
        if item.count() == 0:
            page.keyboard.press("Escape")
            raise RuntimeError("no CSV option in the Download menu")
        with page.expect_download(timeout=60_000) as dl_info:
            item.first.click(timeout=10_000)
        download: Download = dl_info.value
        target = self.downloads_dir / f"call_details_{label}.csv"
        download.save_as(str(target))
        data = target.read_bytes()
        text = data.decode("utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else data.decode("utf-8-sig")
        rows = rows_from_csv_text(text)
        self.log(f"[scrape] CSV download: {len(rows)} data row(s), headers={list(rows[0].keys()) if rows else 'n/a'}")
        return rows

    # -- rows: grid scraping ---------------------------------------------------
    def _grid_rows_locator(self, page: Page) -> Locator:
        return page.get_by_role("row").filter(has=page.get_by_role("cell").or_(page.get_by_role("gridcell")))

    def _count_grid_rows(self, page: Page) -> int | None:
        try:
            n = self._grid_rows_locator(page).count()
            return n if n > 0 else None
        except Exception:  # noqa: BLE001
            return None

    def _grid_headers(self, page: Page) -> list[str]:
        headers = [h.strip() for h in page.get_by_role("columnheader").all_inner_texts()]
        headers = [_squash(h) for h in headers if h.strip()]
        if not headers:
            headers = [f"col{i}" for i in range(len(REPORT_COLUMNS))]
        return headers

    def _maximize_rows_per_page(self, page: Page) -> None:
        combo = page.get_by_role("combobox", name=re.compile(r"rows per page|show rows|page size", re.I))
        if combo.count() == 0:
            combo = page.get_by_role("button", name=re.compile(r"rows per page|show rows", re.I))
        if combo.count() == 0:
            return
        try:
            combo.first.click(timeout=5_000)
            options = page.get_by_role("option")
            if options.count() == 0:
                options = page.get_by_role("menuitem")
            best, best_val = None, -1
            for i in range(options.count()):
                txt = options.nth(i).inner_text().strip().replace(",", "")
                if txt.isdigit() and int(txt) > best_val:
                    best, best_val = options.nth(i), int(txt)
            if best is not None:
                best.click(timeout=5_000)
                page.wait_for_timeout(1500)
                self.log(f"[scrape] rows per page set to {best_val}")
            else:
                page.keyboard.press("Escape")
        except Exception as exc:  # noqa: BLE001
            self.log(f"[scrape] could not change rows per page: {type(exc).__name__}")
            page.keyboard.press("Escape")

    def _read_visible_rows(self, page: Page) -> list[list[dict]]:
        """Each row as a list of {text, href} cells, read in one evaluate call."""
        return page.evaluate(
            """() => {
              const rows = Array.from(document.querySelectorAll('[role=row], tr'))
                .filter(r => r.querySelector('[role=cell], [role=gridcell], td'));
              return rows.map(r => Array.from(r.querySelectorAll('[role=cell], [role=gridcell], td')).map(c => {
                const a = c.querySelector('a[href]');
                return { text: (c.innerText || '').replace(/\\s+/g, ' ').trim(), href: a ? a.href : null };
              }));
            }"""
        )

    def _scrape_grid(self, page: Page, total: int | None) -> list[dict]:
        self._maximize_rows_per_page(page)
        headers = self._grid_headers(page)
        mapping = map_headers(headers, warn=self.log) if len(headers) >= 2 else {}
        recording_header = next((h for h, c in mapping.items() if c == "recording_url"), None)
        collected: dict[str, dict] = {}
        pages_seen = 0
        while True:
            pages_seen += 1
            stable = 0
            last_count = -1
            for _ in range(200):  # virtualized rows: scroll until no new rows appear
                for cells in self._read_visible_rows(page):
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
                page.mouse.wheel(0, 800)
                page.keyboard.press("PageDown")
                page.wait_for_timeout(400)
            if total is not None and len(collected) >= total:
                break
            nxt = page.get_by_role("button", name=re.compile(r"next page|next", re.I))
            if nxt.count() == 0 or not nxt.first.is_enabled():
                break
            nxt.first.click(timeout=10_000)
            page.wait_for_timeout(2000)
        rows = list(collected.values())
        self.log(f"[scrape] grid: {len(rows)} row(s) across {pages_seen} page(s), headers={headers}")
        return rows

    # -- debugging ---------------------------------------------------------
    def _dump(self, page: Page, name: str) -> None:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
        try:
            page.screenshot(path=str(self.debug_dir / f"{safe}.png"), full_page=True)
            (self.debug_dir / f"{safe}.html").write_text(page.content(), encoding="utf-8")
            self.log(f"[scrape] saved debug/{safe}.png and debug/{safe}.html (url host: {_host(page.url)})")
        except Exception as exc:  # noqa: BLE001
            self.log(f"[scrape] could not save debug output: {type(exc).__name__}")
