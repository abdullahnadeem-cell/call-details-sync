import base64
from datetime import date

import pytest

from src import report_url as ru


def test_decodes_original_state(report_state):
    s = ru.decode_report_state(report_state)
    assert s.report_name == "Call details"
    assert (s.start, s.end) == ("2026-11-01", "2026-11-24")
    assert s.urlsafe is False and s.padded is False
    for col in [
        b"CallStartTime", b"CallDuration", b"CallerCountryCallingCode", b"CallerNationalDesignatedCode",
        b"CallerId", b"CallRecordingReplay", b"GoogleVoiceCallType", b"CallTrackingDisplayLocation",
        b"AdTelephonyPropertyGroup", b"Campaign",
    ]:
        assert col in s.raw


def test_round_trip_unchanged(report_state, report_url):
    assert ru.rewrite_report_state(report_state, "2026-11-01", "2026-11-24") == report_state
    assert ru.build_report_url(report_url, date(2026, 11, 1), date(2026, 11, 24)) == report_url


def test_rewrite_changes_only_the_dates(report_state):
    new = ru.rewrite_report_state(report_state, date(2026, 11, 20), date(2026, 11, 20))
    assert new != report_state
    assert len(new) == len(report_state)
    s = ru.decode_report_state(new)
    assert (s.start, s.end) == ("2026-11-20", "2026-11-20")
    old_raw = ru.decode_report_state(report_state).raw
    assert len(s.raw) == len(old_raw)
    # everything except the 20 date bytes is identical
    diff = [i for i, (a, b) in enumerate(zip(old_raw, s.raw)) if a != b]
    assert diff and all(old_raw[i : i + 1].isdigit() for i in diff)
    assert s.report_name == "Call details"
    assert b"CallerId" in s.raw


def test_build_url_keeps_other_params(report_url):
    new = ru.build_report_url(report_url, "2026-10-05", "2026-10-06")
    assert new.startswith(report_url.split("&reportState=")[0] + "&reportState=")
    assert ru.url_dates(new) == ("2026-10-05", "2026-10-06")
    assert "&reportState=" in new and new.count("reportState=") == 1


def test_percent_encoded_param_stays_percent_encoded(report_state):
    raw = base64.b64decode(report_state) + b"\x50\x00"  # 299 bytes -> one '=' of padding
    padded = base64.b64encode(raw).decode()
    assert padded.endswith("=")
    url = "https://x/view?a=1&reportState=" + padded.replace("=", "%3D") + "&b=2"
    new = ru.build_report_url(url, "2026-01-01", "2026-01-02")
    assert new.endswith("%3D&b=2")
    assert ru.url_dates(new) == ("2026-01-01", "2026-01-02")


def test_urlsafe_flavor_is_preserved():
    raw = b"\x22\x18\x0a\x0a2026-01-01\x12\x0a2026-01-31" + bytes([0xFB, 0xFF, 0xBF])
    text = base64.urlsafe_b64encode(raw).decode()
    assert "-" in text or "_" in text
    new = ru.rewrite_report_state(text, "2026-02-01", "2026-02-28")
    assert "+" not in new and "/" not in new
    assert ru.extract_dates(base64.urlsafe_b64decode(new)) == ("2026-02-01", "2026-02-28")


@pytest.mark.parametrize("bad", ["2026/11/01", "20261101", "2026-13-01", ""])
def test_rejects_bad_dates(report_state, bad):
    with pytest.raises(ru.ReportUrlError):
        ru.rewrite_report_state(report_state, bad, "2026-11-24")


def test_rejects_reversed_range(report_state):
    with pytest.raises(ru.ReportUrlError):
        ru.rewrite_report_state(report_state, "2026-11-24", "2026-11-01")


def test_missing_param():
    with pytest.raises(ru.ReportUrlError):
        ru.build_report_url("https://ads.google.com/aw/reporteditor/view?ocid=1", "2026-01-01", "2026-01-01")
