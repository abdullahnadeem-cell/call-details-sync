from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src import parse
from src.errors import SyncError

FIXTURE = Path(__file__).parent / "fixtures" / "call_details_sample.csv"
TZ = "Pacific/Honolulu"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("0:35", 35),
        ("00:35", 35),
        ("1:02:03", 3723),
        ("35 sec", 35),
        ("35 secs", 35),
        ("35 seconds", 35),
        ("1 min 5 sec", 65),
        ("2 min", 120),
        ("1m 5s", 65),
        ("1 hr 1 min 5 sec", 3665),
        ("35", 35),
        ("35.0", 35),
        ("--", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_duration(text, expected):
    assert parse.parse_duration(text) == expected


def test_parse_duration_rejects_garbage():
    with pytest.raises(SyncError) as e:
        parse.parse_duration("lots")
    assert e.value.code == "PARSE_ERROR"


@pytest.mark.parametrize(
    "text",
    [
        "2026-11-20 14:35:10",
        "2026-11-20T14:35:10",
        "Nov 20, 2026, 2:35:10 PM",
        "Nov 20, 2026 2:35:10 PM",
        "Nov 20, 2026, 2:35:10 PM",  # narrow no-break space before PM
        "November 20, 2026, 2:35:10 PM",
        "11/20/2026 2:35:10 PM",
        "11/20/2026 14:35:10",
        "20 Nov 2026 14:35:10",
    ],
)
def test_parse_start_time_formats(text):
    dt = parse.parse_start_time(text, TZ)
    assert dt == datetime(2026, 11, 20, 14, 35, 10, tzinfo=ZoneInfo(TZ))
    assert dt.isoformat() == "2026-11-20T14:35:10-10:00"
    assert parse.utc_iso(dt) == "2026-11-21T00:35:10+00:00"


def test_parse_start_time_without_seconds():
    dt = parse.parse_start_time("Nov 20, 2026, 2:35 PM", TZ)
    assert dt.isoformat() == "2026-11-20T14:35:00-10:00"


def test_parse_start_time_keeps_explicit_offset():
    dt = parse.parse_start_time("2026-11-20T14:35:10+00:00", TZ)
    assert dt == datetime(2026, 11, 20, 14, 35, 10, tzinfo=timezone.utc)


def test_parse_start_time_rejects_garbage():
    with pytest.raises(SyncError):
        parse.parse_start_time("yesterday-ish", TZ)


def test_call_key_is_sha1_of_utc_iso_and_fields():
    dt = datetime(2026, 11, 20, 14, 35, 10, tzinfo=ZoneInfo(TZ))
    import hashlib

    expected = hashlib.sha1(
        "2026-11-21T00:35:10+00:00|+1 808-688-3603|35|TCA - Search - Brand".encode()
    ).hexdigest()
    assert parse.call_key(dt, "+1 808-688-3603", 35, "TCA - Search - Brand") == expected
    # same instant in another zone gives the same key
    dt_utc = dt.astimezone(timezone.utc)
    assert parse.call_key(dt_utc, "+1 808-688-3603", 35, "TCA - Search - Brand") == expected
    # None fields are empty strings in the key
    assert parse.call_key(dt, None, None, None) == hashlib.sha1(
        b"2026-11-21T00:35:10+00:00|||"
    ).hexdigest()


def test_map_headers_by_ui_labels_and_internal_names():
    ui = ["Start time", "Duration", "Caller country code", "Caller area code", "Caller ID",
          "Call recording", "Call type", "Display location", "Ad telephony property group", "Campaign"]
    assert list(parse.map_headers(ui, warn=lambda m: None).values()) == parse.REPORT_COLUMNS
    internal = ["CallStartTime", "CallDuration", "CallerCountryCallingCode", "CallerNationalDesignatedCode",
                "CallerId", "CallRecordingReplay", "GoogleVoiceCallType", "CallTrackingDisplayLocation",
                "AdTelephonyPropertyGroup", "Campaign"]
    assert list(parse.map_headers(internal, warn=lambda m: None).values()) == parse.REPORT_COLUMNS


def test_map_headers_positional_fallback_warns():
    headers = ["Start time", "Duration", "Country", "Area", "Caller ID", "Replay",
               "Call type", "Shown at", "Group", "Campaign"]
    warnings = []
    mapping = parse.map_headers(headers, warn=warnings.append)
    assert mapping["Country"] == "caller_country_code"
    assert mapping["Shown at"] == "display_location"
    assert any("position" in w for w in warnings)


def test_map_headers_fails_without_start_time():
    with pytest.raises(SyncError):
        parse.map_headers(["Caller ID", "Campaign", "Duration"], warn=lambda m: None)


def test_csv_fixture_round_trip():
    raw = parse.rows_from_csv_text(FIXTURE.read_text())
    assert len(raw) == 5  # preamble and Total row skipped
    assert raw[0]["Caller ID"] == "+1 808-688-3603"
    rows = parse.normalize_rows(raw, TZ, warn=lambda m: None)
    first = rows[0]
    assert first["start_time"] == "2026-11-20T08:05:12-10:00"
    assert first["duration_seconds"] == 35
    assert first["caller_country_code"] == "1"
    assert first["caller_area_code"] == "808"
    assert first["caller_phone_raw"] == "+1 808-688-3603"
    assert first["recording_url"] is None
    assert first["call_type"] == "Mobile click-to-call"
    assert first["display_location"] == "Ad"
    assert first["ad_telephony_group"] == "Call extension"
    assert first["campaign"] == "TCA - Search - Brand"
    assert first["status"] is None
    assert first["raw"]["Start time"] == "Nov 20, 2026, 8:05:12 AM"
    assert set(first["raw"]) == set(raw[0])
    assert rows[1]["recording_url"] == "https://ads.google.com/recordings/abc123"
    # masked caller is kept exactly as shown, not dropped
    assert rows[2]["caller_phone_raw"] == "--"
    assert rows[4]["duration_seconds"] == 3723
    # no columns the trigger owns are sent
    for r in rows:
        for forbidden in ("caller_phone_e164", "campaign_id", "call_date_hst", "caller_phone_number", "call_source", "ad_group"):
            assert forbidden not in r


def test_dedupe_by_call_key():
    raw = parse.rows_from_csv_text(FIXTURE.read_text())
    rows = parse.normalize_rows(raw, TZ, warn=lambda m: None)
    assert rows[2]["call_key"] == rows[3]["call_key"]
    deduped = parse.dedupe(rows, warn=lambda m: None)
    assert len(deduped) == 4
    assert len({r["call_key"] for r in deduped}) == 4


def test_csv_without_preamble_and_tsv():
    text = "Start time\tDuration\tCaller ID\tCampaign\n2026-11-20 08:05:12\t35\t+1 808-688-3603\tX\n"
    raw = parse.rows_from_csv_text(text)
    assert raw == [{"Start time": "2026-11-20 08:05:12", "Duration": "35", "Caller ID": "+1 808-688-3603", "Campaign": "X"}]


def test_csv_without_header_fails():
    with pytest.raises(SyncError):
        parse.rows_from_csv_text("just,some,cells\n1,2,3\n")
