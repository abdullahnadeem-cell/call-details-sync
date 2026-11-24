"""Turn parsed rows (the JSON written by `python -m src.run --dry-run`) into upsert SQL.

Used when rows are loaded through a SQL console or the Supabase connector instead of
the REST sink. Same columns as src/supabase_sink.py; the trigger fills
caller_phone_e164, campaign_id and call_date_hst; status, call_source, ad_group and
caller_phone_number are never written.

    python scripts/rows_to_sql.py out/rows_2026-11-20_2026-11-20.json > out/load.sql
    python scripts/rows_to_sql.py rows.json --chunk 100 --table public.google_ads_call_details
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

COLUMNS = [
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
    "raw",
    "call_key",
]
TABLE = "public.google_ads_call_details"
_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


def quote_text(value: str) -> str:
    """Single-quoted SQL string literal. Assumes standard_conforming_strings (the Postgres default),
    so backslashes are literal; NUL bytes cannot be stored and are dropped."""
    return "'" + value.replace("\x00", "").replace("'", "''") + "'"


def literal(column: str, value) -> str:
    if value is None:
        return "NULL"
    if column == "raw":
        return quote_text(json.dumps(value, ensure_ascii=False, separators=(",", ":"))) + "::jsonb"
    if column == "start_time":
        return quote_text(str(value)) + "::timestamptz"
    if column == "duration_seconds":
        return str(int(value))
    return quote_text(str(value))


def build_statement(rows: list[dict], table: str = TABLE) -> str:
    if not _TABLE_RE.match(table):
        raise ValueError(f"unsafe table name {table!r}")
    keys = [r["call_key"] for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate call_key values in one statement; dedupe first")
    values = ",\n".join(
        "  (" + ", ".join(literal(c, r.get(c)) for c in COLUMNS) + ")" for r in rows
    )
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS if c != "call_key")
    return (
        f"INSERT INTO {table} ({', '.join(COLUMNS)})\nVALUES\n{values}\n"
        f"ON CONFLICT (call_key) DO UPDATE SET {updates};"
    )


def build_sql(rows: list[dict], chunk: int = 200, table: str = TABLE) -> list[str]:
    return [build_statement(rows[i : i + chunk], table) for i in range(0, len(rows), chunk)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rows_json", help="JSON list of parsed rows (output of --dry-run)")
    ap.add_argument("--chunk", type=int, default=200, help="rows per INSERT statement (default 200)")
    ap.add_argument("--table", default=TABLE)
    args = ap.parse_args(argv)
    rows = json.loads(Path(args.rows_json).read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        print("rows_json must be a JSON list", file=sys.stderr)
        return 2
    if not rows:
        print("-- no rows: nothing to load")
        return 0
    print("\n\n".join(build_sql(rows, args.chunk, args.table)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
