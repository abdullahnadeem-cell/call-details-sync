import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src import run
from src.errors import SyncError

FIXTURE = str(Path(__file__).parent / "fixtures" / "call_details_sample.csv")


def test_resolve_range_variants():
    p = run.build_parser()
    assert run.resolve_range(p.parse_args(["--date", "2026-11-20"])) == (date(2026, 11, 20), date(2026, 11, 20))
    assert run.resolve_range(p.parse_args(["--from", "2026-11-01", "--to", "2026-11-24"])) == (date(2026, 11, 1), date(2026, 11, 24))
    assert run.resolve_range(p.parse_args(["--from", "2026-11-01"])) == (date(2026, 11, 1), date(2026, 11, 1))
    now = datetime(2026, 11, 24, 16, 30, tzinfo=timezone.utc)
    assert run.resolve_range(p.parse_args([]), now) == (date(2026, 11, 22), date(2026, 11, 23))
    assert run.resolve_range(p.parse_args(["--lookback", "3"]), now) == (date(2026, 11, 21), date(2026, 11, 23))
    for bad in (["--date", "2026-11-20", "--from", "2026-11-01"], ["--to", "2026-11-02"], ["--from", "2026-11-05", "--to", "2026-11-01"], ["--date", "nope"]):
        with pytest.raises(SyncError):
            run.resolve_range(p.parse_args(bad))


def test_dry_run_from_csv_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    out = tmp_path / "rows.json"
    rc = run.main(["--date", "2026-11-20", "--dry-run", "--from-file", FIXTURE, "--out", str(out)])
    assert rc == 0
    rows = json.loads(out.read_text())
    assert len(rows) == 4
    assert rows[0]["caller_phone_raw"] == "+1 808-688-3603"
    printed = capsys.readouterr().out
    assert "nothing written to Supabase" in printed
    assert "***3603" in printed and "+1 808-688-3603" not in printed
    assert "caller_phone_raw '--' or empty: 1" in printed
    assert not (tmp_path / "debug" / "error_code.txt").exists()


def test_dry_run_from_previous_json_output(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out1 = tmp_path / "a.json"
    assert run.main(["--date", "2026-11-20", "--dry-run", "--from-file", FIXTURE, "--out", str(out1), "--no-reconcile"]) == 0
    out2 = tmp_path / "b.json"
    assert run.main(["--date", "2026-11-20", "--dry-run", "--from-file", str(out1), "--out", str(out2), "--no-reconcile"]) == 0
    assert json.loads(out1.read_text()) == json.loads(out2.read_text())


def test_real_run_without_supabase_config_is_config_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    rc = run.main(["--date", "2026-11-20", "--from-file", FIXTURE])
    assert rc == 2
    assert (tmp_path / "debug" / "error_code.txt").read_text() == "CONFIG_ERROR"


def test_real_run_uses_sink_and_dedupes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k")
    sent = {}

    class FakeSink:
        def __init__(self, *a, **k):
            pass

        def upsert_call_details(self, rows):
            sent["rows"] = rows
            return len(rows)

        def phone_calls_by_campaign(self, day):
            return {"TCA - Search - Brand": 1}

        def stored_count_for_day(self, day):
            return 4

    import src.supabase_sink as mod

    monkeypatch.setattr(mod, "SupabaseSink", FakeSink)
    rc = run.main(["--date", "2026-11-20", "--from-file", FIXTURE])
    assert rc == 0
    assert len(sent["rows"]) == 4
    assert len({r["call_key"] for r in sent["rows"]}) == 4


def test_scrape_failure_writes_error_code(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GADS_REPORT_URL", "https://ads.google.com/aw/reporteditor/view?reportState=x")
    monkeypatch.setenv("GADS_EMAIL", "x@example.com")
    monkeypatch.setenv("GADS_PASSWORD", "pw")

    def boom(cfg, start, end, headed):
        raise SyncError("SESSION_EXPIRED", "landed on accounts.google.com")

    monkeypatch.setattr(run, "collect_rows", boom)
    rc = run.main(["--date", "2026-11-20", "--dry-run"])
    assert rc == 1
    assert (tmp_path / "debug" / "error_code.txt").read_text() == "SESSION_EXPIRED"


def test_assert_rows_in_range():
    rows = [{"start_time": "2026-11-20T23:30:00-10:00"}, {"start_time": "2026-11-21T00:10:00-10:00"}]
    run.assert_rows_in_range(rows, date(2026, 11, 20), date(2026, 11, 21), "Pacific/Honolulu")
    with pytest.raises(SyncError) as e:
        run.assert_rows_in_range(rows, date(2026, 11, 20), date(2026, 11, 20), "Pacific/Honolulu")
    assert e.value.code == "DATE_RANGE_MISMATCH"


def test_missing_login_secrets_is_reported_by_name(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GADS_REPORT_URL", "https://ads.google.com/aw/reporteditor/view?reportState=x")
    for name in ("GADS_EMAIL", "GADS_PASSWORD", "GADS_TOTP_SECRET"):
        monkeypatch.delenv(name, raising=False)
    rc = run.main(["--date", "2026-11-20", "--dry-run"])
    assert rc == 2
    assert (tmp_path / "debug" / "error_code.txt").read_text() == "MISSING_SECRETS"
    out = capsys.readouterr().out
    assert "GADS_EMAIL" in out and "GADS_PASSWORD" in out


def test_dry_run_masks_phone_numbers(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = run.main(["--date", "2026-11-20", "--dry-run", "--from-file", FIXTURE, "--out", str(tmp_path / "r.json"), "--no-reconcile"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "***3603" in out and "808-688-3603" not in out


def test_saved_session_alone_satisfies_preflight(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GADS_REPORT_URL", "https://ads.google.com/aw/reporteditor/view?reportState=x")
    for name in ("GADS_EMAIL", "GADS_PASSWORD", "GADS_TOTP_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GADS_SESSION", json.dumps([{"name": "SID", "value": "v", "domain": ".google.com"}]))
    seen = {}

    def fake_collect(cfg, start, end, headed):
        seen["session"] = cfg.gads_session
        return [], []

    monkeypatch.setattr(run, "collect_rows", fake_collect)
    assert run.main(["--date", "2026-11-20", "--dry-run", "--no-reconcile"]) == 0
    assert seen["session"]
