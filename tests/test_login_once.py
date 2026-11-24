import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("login_once", Path(__file__).parents[1] / "scripts" / "login_once.py")
login_once = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(login_once)


def test_report_url_comes_from_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("GADS_REPORT_URL", raising=False)
    env = tmp_path / ".env"
    env.write_text("GADS_REPORT_URL=https://ads.google.com/aw/reporteditor/view?reportState=abc\n")
    assert login_once.resolve_report_url(None, str(env)) == "https://ads.google.com/aw/reporteditor/view?reportState=abc"
    monkeypatch.delenv("GADS_REPORT_URL", raising=False)


def test_cli_value_wins_and_empty_dotenv_gives_none(tmp_path, monkeypatch):
    monkeypatch.delenv("GADS_REPORT_URL", raising=False)
    assert login_once.resolve_report_url("https://x/cli", str(tmp_path / "none.env")) == "https://x/cli"
    env = tmp_path / ".env"
    env.write_text("GADS_REPORT_URL=\nSUPABASE_URL=https://x.supabase.co\n")
    assert login_once.resolve_report_url(None, str(env)) is None
    monkeypatch.delenv("GADS_REPORT_URL", raising=False)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
