"""Build the Google Ads Report Editor URL for a given date range.

The saved report link carries a ``reportState`` query parameter: base64 of a
small protobuf. Inside it the custom date range is stored as two plain
``YYYY-MM-DD`` strings, each prefixed by its length. Replacing them with other
10 character dates keeps every length prefix valid, so we can rewrite the
range without re-encoding the protobuf. We keep the original base64 alphabet
and padding, and touch nothing else in the URL.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import quote, unquote

_DATE = rb"\d{4}-\d{2}-\d{2}"
# field 1 (len 10) = start, field 2 (len 10) = end, inside the date range message
_RANGE_RE = re.compile(rb"\x0a\x0a(" + _DATE + rb")\x12\x0a(" + _DATE + rb")")
_PARAM_RE = re.compile(r"(?P<prefix>(?:^|[?&])reportState=)(?P<value>[^&#]*)")


class ReportUrlError(ValueError):
    pass


@dataclass(frozen=True)
class ReportState:
    raw: bytes
    start: str
    end: str
    urlsafe: bool
    padded: bool

    @property
    def report_name(self) -> str | None:
        m = re.match(rb"\x08[\x00-\xff]\x12([\x00-\x7f])", self.raw)
        if not m:
            return None
        n = m.group(1)[0]
        return self.raw[m.end() : m.end() + n].decode("utf-8", "replace")


def _b64_flavor(text: str) -> tuple[bool, bool]:
    """Return (urlsafe, padded). Ambiguous input (no +/ or -_ ) counts as standard."""
    urlsafe = ("-" in text or "_" in text) and not ("+" in text or "/" in text)
    return urlsafe, text.endswith("=")


def decode_report_state(text: str) -> ReportState:
    text = text.strip()
    if not text:
        raise ReportUrlError("empty reportState")
    urlsafe, padded = _b64_flavor(text)
    body = text + "=" * (-len(text) % 4)
    try:
        raw = base64.urlsafe_b64decode(body) if urlsafe else base64.b64decode(body, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ReportUrlError(f"reportState is not valid base64: {exc}") from exc
    start, end = extract_dates(raw)
    return ReportState(raw=raw, start=start, end=end, urlsafe=urlsafe, padded=padded)


def extract_dates(raw: bytes) -> tuple[str, str]:
    m = _RANGE_RE.search(raw)
    if m:
        return m.group(1).decode(), m.group(2).decode()
    found = re.findall(_DATE, raw)
    if len(found) == 2:
        return found[0].decode(), found[1].decode()
    raise ReportUrlError(
        f"could not find exactly one custom date range in reportState (found {len(found)} dates)"
    )


def encode_report_state(raw: bytes, urlsafe: bool, padded: bool) -> str:
    text = (base64.urlsafe_b64encode(raw) if urlsafe else base64.b64encode(raw)).decode()
    return text if padded else text.rstrip("=")


def _iso(d: date | str) -> str:
    s = d.isoformat() if isinstance(d, date) else str(d)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise ReportUrlError(f"date must be YYYY-MM-DD, got {s!r}")
    try:
        date.fromisoformat(s)
    except ValueError as exc:
        raise ReportUrlError(f"invalid date {s!r}: {exc}") from exc
    return s


def rewrite_report_state(text: str, start: date | str, end: date | str) -> str:
    """Return ``text`` (a reportState value) with the custom range replaced."""
    start_s, end_s = _iso(start), _iso(end)
    if end_s < start_s:
        raise ReportUrlError(f"end {end_s} is before start {start_s}")
    state = decode_report_state(text)
    m = _RANGE_RE.search(state.raw)
    if m:
        new_raw = (
            state.raw[: m.start(1)] + start_s.encode()
            + state.raw[m.end(1) : m.start(2)] + end_s.encode()
            + state.raw[m.end(2) :]
        )
    else:
        new_raw = state.raw.replace(state.start.encode(), start_s.encode(), 1)
        new_raw = new_raw.replace(state.end.encode(), end_s.encode(), 1)
    assert len(new_raw) == len(state.raw)
    return encode_report_state(new_raw, state.urlsafe, state.padded)


def get_report_state_param(url: str) -> str:
    m = _PARAM_RE.search(url)
    if not m:
        raise ReportUrlError("URL has no reportState query parameter")
    return unquote(m.group("value"))


def build_report_url(url: str, start: date | str, end: date | str) -> str:
    """Return the saved report URL with its custom date range set to start..end."""
    m = _PARAM_RE.search(url)
    if not m:
        raise ReportUrlError("URL has no reportState query parameter")
    raw_value = m.group("value")
    was_percent_encoded = raw_value != unquote(raw_value)
    new_value = rewrite_report_state(unquote(raw_value), start, end)
    if was_percent_encoded:
        new_value = quote(new_value, safe="")
    else:
        # only '+' and '/' would need escaping; the digit bytes we change never
        # produce them, but stay safe anyway
        new_value = new_value.replace("+", "%2B").replace("/", "%2F")
    return url[: m.start("value")] + new_value + url[m.end("value") :]


def url_dates(url: str) -> tuple[str, str]:
    """Dates currently stored in a report URL."""
    s = decode_report_state(get_report_state_param(url))
    return s.start, s.end
