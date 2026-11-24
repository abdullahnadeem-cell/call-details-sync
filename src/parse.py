"""Normalize raw report rows into Supabase rows for public.google_ads_call_details.

Raw rows are ``{header: cell_text}`` dicts, exactly as read from the CSV
download or the on-screen grid. Headers are matched by name (several
spellings per column) with a positional fallback based on the report's known
column order. Times are parsed in the Google Ads account time zone.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .errors import PARSE_ERROR, SyncError

# Canonical column order of the saved "Call details" report (from reportState).
REPORT_COLUMNS = [
    "start_time",
    "duration_seconds",
    "caller_country_code",
    "caller_area_code",
    "caller_phone_raw",
    "recording_url",
    "call_type",
    "display_location",
    "ad_telephony_group",
    "campaign",
]

# Known header spellings, normalized with _norm(). Internal names first, then UI labels.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "start_time": ("callstarttime", "starttime", "calltime", "callstart", "time", "datetime", "startdatetime"),
    "duration_seconds": ("callduration", "duration", "durationseconds", "durationsec", "durations", "callduration(s)"),
    "caller_country_code": ("callercountrycallingcode", "callercountrycode", "countrycallingcode", "countrycode", "callercountry"),
    "caller_area_code": ("callernationaldesignatedcode", "callerareacode", "areacode", "nationaldesignatedcode", "callernationalcode"),
    "caller_phone_raw": ("callerid", "callerphonenumber", "phonenumber", "callernumber", "caller", "callerphone", "phone"),
    "recording_url": ("callrecordingreplay", "callrecording", "recording", "recordingreplay", "callrecordingurl"),
    "call_type": ("googlevoicecalltype", "calltype", "type", "voicecalltype"),
    "display_location": ("calltrackingdisplaylocation", "displaylocation", "calltrackinglocation", "location"),
    "ad_telephony_group": ("adtelephonypropertygroup", "adtelephonygroup", "telephonypropertygroup", "propertygroup", "adtelephony"),
    "campaign": ("campaign", "campaignname"),
}

EMPTY_CELLS = {"", "--", "—", "-", "n/a", "none", "null"}
_WS_RE = re.compile(r"[\s   ]+")

TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%b %d, %Y, %I:%M:%S %p",
    "%b %d, %Y %I:%M:%S %p",
    "%b %d, %Y, %I:%M %p",
    "%b %d, %Y %I:%M %p",
    "%B %d, %Y, %I:%M:%S %p",
    "%B %d, %Y %I:%M:%S %p",
    "%B %d, %Y, %I:%M %p",
    "%B %d, %Y %I:%M %p",
    "%b %d, %Y, %H:%M:%S",
    "%b %d, %Y %H:%M:%S",
    "%b %d, %Y, %H:%M",
    "%b %d, %Y %H:%M",
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%y %I:%M:%S %p",
    "%m/%d/%y %I:%M %p",
    "%d %b %Y %H:%M:%S",
    "%d %b %Y, %H:%M:%S",
    "%d %b %Y %H:%M",
    "%Y/%m/%d %H:%M:%S",
)


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9()]+", "", header.strip().lower())


def clean_text(value) -> str:
    if value is None:
        return ""
    return _WS_RE.sub(" ", str(value)).strip()


def is_empty_cell(value) -> bool:
    return clean_text(value).lower() in EMPTY_CELLS


def map_headers(headers: list[str], warn=print) -> dict[str, str]:
    """Map raw header -> canonical column. Unknown headers are kept in raw only.

    Falls back to the report's column order for headers that match no alias
    when the header count equals the report's column count.
    """
    mapping: dict[str, str] = {}
    taken: set[str] = set()
    for h in headers:
        n = _norm(h)
        for canon, aliases in COLUMN_ALIASES.items():
            if canon in taken:
                continue
            if n in aliases or n.replace("(s)", "") in aliases:
                mapping[h] = canon
                taken.add(canon)
                break
    if len(headers) == len(REPORT_COLUMNS):
        for i, h in enumerate(headers):
            canon = REPORT_COLUMNS[i]
            if h not in mapping and canon not in taken:
                warn(f"[parse] header {h!r} matched no alias; using position {i} -> {canon}")
                mapping[h] = canon
                taken.add(canon)
    missing = [c for c in ("start_time", "campaign") if c not in taken]
    if missing:
        raise SyncError(PARSE_ERROR, f"could not find columns {missing} in headers {headers}")
    for canon in REPORT_COLUMNS:
        if canon not in taken:
            warn(f"[parse] column {canon} not found in headers; it will be null")
    return mapping


def parse_duration(value) -> int | None:
    """'0:35' -> 35, '1:02:03' -> 3723, '35 sec' -> 35, '1 min 5 sec' -> 65, '35' -> 35, '--' -> None."""
    text = clean_text(value).lower()
    if text in EMPTY_CELLS:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return int(round(float(text)))
    if re.fullmatch(r"\d+(:\d{1,2}){1,2}", text):
        parts = [int(p) for p in text.split(":")]
        total = 0
        for p in parts:
            total = total * 60 + p
        return total
    total = 0
    found = False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(h(?:ou)?rs?|h|m(?:in)?(?:ute)?s?|m|s(?:ec)?(?:ond)?s?|s)\b", text):
        found = True
        n = float(num)
        if unit.startswith("h"):
            total += n * 3600
        elif unit.startswith("m"):
            total += n * 60
        else:
            total += n
    if found:
        return int(round(total))
    raise SyncError(PARSE_ERROR, f"cannot parse duration {value!r}")


def parse_start_time(value, tz: str) -> datetime:
    """Parse the CallStartTime cell as a wall clock time in ``tz``; returns an aware datetime."""
    text = clean_text(value)
    if not text:
        raise SyncError(PARSE_ERROR, "empty start time")
    zone = ZoneInfo(tz)
    try:
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=zone)
    except ValueError:
        pass
    normalized = text.replace("Sept ", "Sep ").replace(" at ", " ")
    normalized = re.sub(r"\b(a\.m\.|am)\b", "AM", normalized, flags=re.I)
    normalized = re.sub(r"\b(p\.m\.|pm)\b", "PM", normalized, flags=re.I)
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(normalized, fmt).replace(tzinfo=zone)
        except ValueError:
            continue
    raise SyncError(PARSE_ERROR, f"cannot parse start time {value!r}")


def utc_iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).astimezone(timezone.utc).isoformat()


def call_key(start_time: datetime, caller_phone_raw: str | None, duration_seconds: int | None, campaign: str | None) -> str:
    """sha1 of '{start_time ISO in UTC}|{caller_phone_raw}|{duration_seconds}|{campaign}'."""
    parts = [
        utc_iso(start_time),
        caller_phone_raw or "",
        "" if duration_seconds is None else str(duration_seconds),
        campaign or "",
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def _text_or_none(value) -> str | None:
    return None if is_empty_cell(value) else clean_text(value)


def _url_or_none(value) -> str | None:
    text = clean_text(value)
    m = re.search(r"https?://\S+", text)
    return m.group(0) if m else None


def normalize_row(raw: dict, mapping: dict[str, str], tz: str) -> dict:
    """Turn one raw row into a Supabase row. Never drops the row for a masked caller."""
    by_canon = {canon: raw.get(h) for h, canon in mapping.items()}
    start = parse_start_time(by_canon.get("start_time"), tz)
    duration = parse_duration(by_canon.get("duration_seconds"))
    phone_cell = by_canon.get("caller_phone_raw")
    caller_phone_raw = clean_text(phone_cell) if phone_cell is not None else None
    campaign = _text_or_none(by_canon.get("campaign"))
    row = {
        "start_time": start.isoformat(),
        "duration_seconds": duration,
        "caller_country_code": _text_or_none(by_canon.get("caller_country_code")),
        "caller_area_code": _text_or_none(by_canon.get("caller_area_code")),
        "caller_phone_raw": caller_phone_raw,
        "recording_url": _url_or_none(by_canon.get("recording_url")),
        "call_type": _text_or_none(by_canon.get("call_type")),
        "display_location": _text_or_none(by_canon.get("display_location")),
        "ad_telephony_group": _text_or_none(by_canon.get("ad_telephony_group")),
        "campaign": campaign,
        "status": None,
        "raw": {k: (None if v is None else str(v)) for k, v in raw.items()},
    }
    row["call_key"] = call_key(start, caller_phone_raw, duration, campaign)
    return row


def normalize_rows(raw_rows: list[dict], tz: str, warn=print) -> list[dict]:
    if not raw_rows:
        return []
    headers = list(raw_rows[0].keys())
    mapping = map_headers(headers, warn=warn)
    return [normalize_row(r, mapping, tz) for r in raw_rows]


def dedupe(rows: list[dict], warn=print) -> list[dict]:
    """Keep the first row per call_key so an upsert batch never carries duplicates."""
    seen: set[str] = set()
    out = []
    for r in rows:
        if r["call_key"] in seen:
            continue
        seen.add(r["call_key"])
        out.append(r)
    if len(out) != len(rows):
        warn(f"[parse] dropped {len(rows) - len(out)} duplicate row(s) by call_key")
    return out


def _looks_like_header(cells: list[str]) -> bool:
    hits = 0
    for c in cells:
        n = _norm(c)
        if any(n in aliases for aliases in COLUMN_ALIASES.values()):
            hits += 1
    return hits >= 3


def rows_from_csv_text(text: str) -> list[dict]:
    """Read a Google Ads report download (CSV or TSV).

    Skips the title/date range preamble lines, stops at 'Total' rows and
    returns ``{header: cell}`` dicts with the original header names.
    """
    text = text.lstrip("﻿")
    lines = text.splitlines()
    delimiter = "\t" if lines and lines[0].count("\t") > lines[0].count(",") else ","
    sample = "\n".join(lines[:20])
    if sample.count("\t") > sample.count(","):
        delimiter = "\t"
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    headers: list[str] | None = None
    out: list[dict] = []
    for cells in reader:
        if headers is None:
            if _looks_like_header(cells):
                headers = [c.strip() for c in cells]
            continue
        if not any(c.strip() for c in cells):
            continue
        first = cells[0].strip().lower()
        if first.startswith("total") or first.startswith("grand total"):
            continue
        if len(cells) < len(headers):
            cells = cells + [""] * (len(headers) - len(cells))
        out.append({h: cells[i] for i, h in enumerate(headers)})
    if headers is None:
        raise SyncError(PARSE_ERROR, "no header row found in CSV download")
    return out
