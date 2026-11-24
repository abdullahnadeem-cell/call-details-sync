import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("rows_to_sql", Path(__file__).parents[1] / "scripts" / "rows_to_sql.py")
r2s = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(r2s)

from src import parse  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "call_details_sample.csv"


def sample_rows():
    raw = parse.rows_from_csv_text(FIXTURE.read_text())
    return parse.dedupe(parse.normalize_rows(raw, "Pacific/Honolulu", warn=lambda m: None), warn=lambda m: None)


def test_statement_shape_and_columns():
    sql = r2s.build_statement(sample_rows())
    assert sql.startswith("INSERT INTO public.google_ads_call_details (start_time, duration_seconds,")
    assert "ON CONFLICT (call_key) DO UPDATE SET" in sql
    assert sql.rstrip().endswith(";")
    assert sql.count("\n  (") == 4  # one VALUES tuple per row
    for forbidden in ("caller_phone_e164", "campaign_id", "call_date_hst", "caller_phone_number", "call_source", "ad_group", "status", "imported_at"):
        assert forbidden not in sql
    assert "'2026-11-20T08:05:12-10:00'::timestamptz" in sql
    assert "'+1 808-688-3603'" in sql and "'--'" in sql  # masked caller kept as shown


def test_escaping_quotes_backslashes_unicode_and_nul():
    row = {
        "call_key": "k1",
        "start_time": "2026-11-20T08:00:00-10:00",
        "duration_seconds": 5,
        "campaign": "O'Brien \\ Sons; DROP TABLE x; --",
        "call_type": "café – \x00zero",
        "caller_phone_raw": None,
        "raw": {"Campaign": "it's", "k": "a\\b", "n": None},
    }
    sql = r2s.build_statement([row])
    assert "'O''Brien \\ Sons; DROP TABLE x; --'" in sql
    assert "\x00" not in sql
    assert "café – zero" in sql
    assert "NULL" in sql
    assert '{"Campaign":"it\'\'s","k":"a\\\\b","n":null}' in sql


def test_chunking_and_duplicate_guard():
    rows = [{"call_key": f"k{i}", "start_time": "2026-11-20T08:00:00-10:00", "raw": {}} for i in range(5)]
    assert len(r2s.build_sql(rows, chunk=2)) == 3
    with pytest.raises(ValueError):
        r2s.build_statement(rows + rows[:1])


def test_unsafe_table_name_rejected():
    with pytest.raises(ValueError):
        r2s.build_statement(sample_rows(), table="x; drop table y")


def test_cli_round_trip(tmp_path, capsys):
    f = tmp_path / "rows.json"
    f.write_text(json.dumps(sample_rows()))
    assert r2s.main([str(f), "--chunk", "3"]) == 0
    out = capsys.readouterr().out
    assert out.count("INSERT INTO") == 2
    empty = tmp_path / "empty.json"
    empty.write_text("[]")
    assert r2s.main([str(empty)]) == 0
    assert "nothing to load" in capsys.readouterr().out
