import json
from datetime import date, datetime, timezone

import pytest

from src.config import Config, load_dotenv
from src.dates import ACCOUNT_TZ_DEFAULT, today_in


def test_default_account_tz_is_pacific(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GADS_ACCOUNT_TZ", raising=False)
    assert ACCOUNT_TZ_DEFAULT == "America/Los_Angeles"
    assert Config.from_env().account_tz == "America/Los_Angeles"


def test_dotenv_loads_without_overriding(tmp_path, monkeypatch):
    env = {"GADS_ACCOUNT_TZ": "Europe/Paris"}
    f = tmp_path / ".env"
    f.write_text('# comment\nSUPABASE_URL="https://x.supabase.co"\nexport GADS_REPORT_URL=\'https://ads.google.com/aw?x=1\'\nGADS_ACCOUNT_TZ=America/Los_Angeles\nBROKEN LINE\n')
    loaded = load_dotenv(f, env)
    assert loaded == ["SUPABASE_URL", "GADS_REPORT_URL"]
    assert env["SUPABASE_URL"] == "https://x.supabase.co"
    assert env["GADS_REPORT_URL"] == "https://ads.google.com/aw?x=1"
    assert env["GADS_ACCOUNT_TZ"] == "Europe/Paris"  # real environment wins
    assert load_dotenv(tmp_path / "missing.env", env) == []


def test_config_reads_dotenv_and_session_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for k in ("GADS_SESSION", "GADS_SESSION_FILE", "GADS_REPORT_URL", "GADS_EMAIL", "GADS_PASSWORD", "GADS_CHROME_DEBUG_URL"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / ".env").write_text("GADS_REPORT_URL=https://ads.google.com/aw/reporteditor/view?reportState=x\n")
    (tmp_path / "gads_session.json").write_text(json.dumps({"cookies": [{"name": "SID", "value": "v", "domain": ".google.com"}]}))
    cfg = Config.from_env()
    assert cfg.report_url.startswith("https://ads.google.com")
    assert cfg.session_source == "file:gads_session.json" and "SID" in cfg.gads_session
    cfg.require_scrape()  # session alone is enough
    monkeypatch.delenv("GADS_REPORT_URL")  # loaded by dotenv into os.environ; clean for other tests


def test_attached_chrome_satisfies_scrape_requirements(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GADS_REPORT_URL", "https://ads.google.com/aw/reporteditor/view?reportState=x")
    monkeypatch.setenv("GADS_CHROME_DEBUG_URL", "127.0.0.1:9333")
    for k in ("GADS_SESSION", "GADS_EMAIL", "GADS_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    Config.from_env(dotenv=None).require_scrape()


def test_today_in_account_tz():
    # 2026-11-24 07:30 UTC is still Nov 23 in Los Angeles (UTC-8)
    assert today_in("America/Los_Angeles", datetime(2026, 11, 24, 7, 30, tzinfo=timezone.utc)) == date(2026, 11, 23)
    assert today_in("America/Los_Angeles", datetime(2026, 11, 24, 8, 30, tzinfo=timezone.utc)) == date(2026, 11, 24)
