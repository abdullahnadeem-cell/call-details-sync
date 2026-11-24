"""Informational reconciliation printed after a successful write. Never fails the run."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from .dates import HST


def call_day_hst(start_time_iso: str) -> str:
    return datetime.fromisoformat(start_time_iso).astimezone(ZoneInfo(HST)).date().isoformat()


def group_by_day_and_campaign(rows: list[dict]) -> dict[str, Counter]:
    out: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        out[call_day_hst(r["start_time"])][r.get("campaign") or "(no campaign)"] += 1
    return out


def format_table(headers: list[str], body: list[list]) -> str:
    cells = [headers] + [[str(c) for c in row] for row in body]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    lines = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(row)) for row in cells]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def print_reconciliation(rows: list[dict], days: list[str], sink, log: Callable[[str], None] = print) -> None:
    """For each synced day: calls in the report per campaign vs phone_calls in gads_campaign_metrics."""
    by_day = group_by_day_and_campaign(rows)
    for day in days:
        try:
            metrics = sink.phone_calls_by_campaign(day) if sink else {}
        except Exception as exc:  # noqa: BLE001 - informational only
            log(f"[reconcile] {day}: could not read gads_campaign_metrics ({type(exc).__name__}); showing report counts only")
            metrics = {}
        report = by_day.get(day, Counter())
        names = sorted(set(report) | {n for n, v in metrics.items() if v})
        body = [[day, name, report.get(name, 0), metrics.get(name, "n/a")] for name in names]
        body.append([day, "TOTAL", sum(report.values()), sum(v for v in metrics.values()) if metrics else "n/a"])
        log(f"\n[reconcile] {day}: report calls vs gads_campaign_metrics.phone_calls (Google only counts calls above a minimum duration, so these need not match)")
        log(format_table(["day", "campaign", "report_calls", "metrics_phone_calls"], body))
        if sink:
            stored = sink.stored_count_for_day(day)
            if stored is not None:
                log(f"[reconcile] {day}: rows now stored in google_ads_call_details = {stored}")
    masked = [r for r in rows if (r.get("caller_phone_raw") or "").strip() in {"--", ""}]
    log(f"\n[reconcile] rows with caller_phone_raw '--' or empty: {len(masked)}")
    for r in masked:
        log(f"  {r['start_time']}  dur={r.get('duration_seconds')}  campaign={r.get('campaign')}  type={r.get('call_type')}")
