"""CLI entry point.

    python -m src.run                       # last 2 full HST days (yesterday and the day before)
    python -m src.run --date 2026-11-20
    python -m src.run --from 2026-11-01 --to 2026-11-24
    python -m src.run --lookback 5
    python -m src.run --date 2026-11-20 --dry-run
    python -m src.run --dry-run --from-file tests/fixtures/call_details_sample.csv
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import date, datetime, timezone
from pathlib import Path

from . import parse
from .config import Config
from .dates import HST, days_in, default_range, split_range
from .errors import CONFIG_ERROR, DATE_RANGE_MISMATCH, SyncError
from .masking import mask_phone
from .reconcile import format_table, print_reconciliation


def log(msg: str) -> None:
    print(msg, flush=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m src.run", description="Sync Google Ads call details into Supabase.")
    p.add_argument("--date", help="one day, YYYY-MM-DD")
    p.add_argument("--from", dest="date_from", help="range start, YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", help="range end, YYYY-MM-DD (default: same as --from)")
    p.add_argument("--lookback", type=int, default=2, help="with no dates: number of full HST days ending yesterday (default 2)")
    p.add_argument("--dry-run", action="store_true", help="print rows and write a JSON file; no database write")
    p.add_argument("--from-file", help="skip the browser: read a CSV download or a JSON list of raw rows")
    p.add_argument("--out", help="JSON output path for --dry-run (default out/rows_<from>_<to>.json)")
    p.add_argument("--headed", action="store_true", help="show the browser (local debugging)")
    p.add_argument("--no-reconcile", action="store_true", help="skip the reconciliation log")
    return p


def resolve_range(args, now: datetime | None = None) -> tuple[date, date]:
    if args.date and (args.date_from or args.date_to):
        raise SyncError(CONFIG_ERROR, "use either --date or --from/--to, not both")
    if args.date_to and not args.date_from:
        raise SyncError(CONFIG_ERROR, "--to requires --from")
    try:
        if args.date:
            d = date.fromisoformat(args.date)
            return d, d
        if args.date_from:
            start = date.fromisoformat(args.date_from)
            end = date.fromisoformat(args.date_to) if args.date_to else start
            if end < start:
                raise SyncError(CONFIG_ERROR, f"--to {end} is before --from {start}")
            return start, end
    except ValueError as exc:
        raise SyncError(CONFIG_ERROR, f"bad date: {exc}") from exc
    try:
        return default_range(now, lookback=args.lookback)
    except ValueError as exc:
        raise SyncError(CONFIG_ERROR, str(exc)) from exc


def load_raw_rows_from_file(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        raise SyncError(CONFIG_ERROR, f"--from-file {path} not found")
    data = p.read_bytes()
    if p.suffix.lower() == ".json":
        rows = json.loads(data.decode("utf-8-sig"))
        if isinstance(rows, dict) and "rows" in rows:
            rows = rows["rows"]
        if not isinstance(rows, list):
            raise SyncError(CONFIG_ERROR, "--from-file JSON must be a list of {header: cell} objects")
        # accept a previous dry-run output too (normalized rows carry 'raw')
        return [r["raw"] if isinstance(r, dict) and "raw" in r and "call_key" in r else r for r in rows]
    return parse.rows_from_csv_text(decode_download(data))


def decode_download(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


def collect_rows(cfg: Config, start: date, end: date, headed: bool) -> tuple[list[dict], list[str]]:
    """Drive the browser for every chunk of the range. Returns (raw rows, method notes)."""
    from .google_login import Credentials
    from .scrape import ReportScraper  # imported lazily so --from-file works without a browser

    cfg.require_scrape()
    chunks = split_range(start, end)
    log(f"[run] {len(chunks)} page load(s) for {start}..{end}")
    raw_rows: list[dict] = []
    notes: list[str] = []
    with ReportScraper(
        report_url=cfg.report_url,
        account_tz=cfg.account_tz,
        headless=not headed,
        debug_dir=cfg.debug_dir,
        downloads_dir=cfg.downloads_dir,
        log=log,
        profile_dir=cfg.profile_dir,
        credentials=Credentials(cfg.gads_email, cfg.gads_password, cfg.gads_totp_secret),
    ) as scraper:
        log(f"[run] google session: {scraper.login_method}")
        for cs, ce in chunks:
            result = scraper.fetch(cs, ce)
            label = cs.isoformat() if cs == ce else f"{cs}..{ce}"
            log(f"[run] {label}: {len(result.rows)} row(s) via {result.rows_method}; date range set via {result.date_method}; page total={result.reported_total}")
            notes.append(f"{label}: rows={len(result.rows)} rows_method={result.rows_method} date_method={result.date_method}")
            raw_rows.extend(result.rows)
    return raw_rows, notes


def assert_rows_in_range(rows: list[dict], start: date, end: date, tz: str) -> None:
    """Second guard after the on-page check: every call must start inside the range (account tz)."""
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(tz)
    outside = [r for r in rows if not (start <= datetime.fromisoformat(r["start_time"]).astimezone(zone).date() <= end)]
    if outside:
        sample = ", ".join(r["start_time"] for r in outside[:3])
        raise SyncError(DATE_RANGE_MISMATCH, f"{len(outside)} row(s) start outside {start}..{end} (e.g. {sample}); the page did not honour the requested range")


def print_rows(rows: list[dict], limit: int = 50) -> None:
    """Dry-run listing. Phone numbers are masked to the last 4 digits: logs may be public."""
    body = [
        [r["start_time"], r["duration_seconds"], mask_phone(r["caller_phone_raw"]), r["call_type"], r["campaign"], r["call_key"][:10]]
        for r in rows[:limit]
    ]
    log(format_table(["start_time", "dur_s", "caller(masked)", "call_type", "campaign", "call_key"], body))
    if len(rows) > limit:
        log(f"... {len(rows) - limit} more row(s) in the JSON file")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.from_env()
    Path(cfg.debug_dir).mkdir(parents=True, exist_ok=True)
    error_code_file = Path(cfg.debug_dir) / "error_code.txt"
    if error_code_file.exists():
        error_code_file.unlink()
    try:
        start, end = resolve_range(args)
        days = [d.isoformat() for d in days_in(start, end)]
        log(f"[run] range {start}..{end} ({len(days)} day(s)), account tz {cfg.account_tz}, dry_run={args.dry_run}")

        if args.from_file:
            raw_rows = load_raw_rows_from_file(args.from_file)
            log(f"[run] loaded {len(raw_rows)} raw row(s) from {args.from_file}")
        else:
            raw_rows, _ = collect_rows(cfg, start, end, headed=args.headed)

        rows = parse.dedupe(parse.normalize_rows(raw_rows, cfg.account_tz, warn=log), warn=log)
        log(f"[run] {len(rows)} unique row(s) after normalization")
        if not args.from_file:
            assert_rows_in_range(rows, start, end, cfg.account_tz)

        if args.dry_run:
            out = Path(args.out or f"out/rows_{start}_{end}.json")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(rows, indent=2, ensure_ascii=False))
            if rows:
                print_rows(rows)
            log(f"[run] dry run: wrote {len(rows)} row(s) to {out}; nothing written to Supabase")
            if not args.no_reconcile:
                sink = None
                if cfg.supabase_url and cfg.supabase_key:
                    from .supabase_sink import SupabaseSink
                    sink = SupabaseSink(cfg.supabase_url, cfg.supabase_key, log=log)
                print_reconciliation(rows, days, sink, log=log)
            return 0

        cfg.require_supabase()
        from .supabase_sink import SupabaseSink

        sink = SupabaseSink(cfg.supabase_url, cfg.supabase_key, log=log)
        if rows:
            sent = sink.upsert_call_details(rows)
            log(f"[run] upserted {sent} row(s) into google_ads_call_details (idempotent on call_key)")
        else:
            log("[run] zero calls in the report for this range; nothing to write")
        if not args.no_reconcile:
            try:
                print_reconciliation(rows, days, sink, log=log)
            except Exception as exc:  # noqa: BLE001 - informational only
                log(f"[reconcile] skipped: {type(exc).__name__}: {exc}")
        return 0
    except SyncError as exc:
        error_code_file.write_text(exc.code)
        log(f"ERROR {exc}")
        return 2 if exc.code in (CONFIG_ERROR, "MISSING_SECRETS") else 1
    except Exception as exc:  # noqa: BLE001
        error_code_file.write_text("UNEXPECTED")
        log(f"ERROR UNEXPECTED: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
