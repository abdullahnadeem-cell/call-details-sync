"""Upsert rows into public.google_ads_call_details through the Supabase REST API.

Uses the service role key (the table has RLS on and no policies). Never logs
keys or row contents; on a non 2xx response it prints status and body and
raises SyncError(SUPABASE_ERROR).
"""
from __future__ import annotations

import json
import time
from typing import Callable

import requests

from .errors import SUPABASE_ERROR, SyncError

TABLE = "google_ads_call_details"
METRICS_TABLE = "gads_campaign_metrics"
BATCH_SIZE = 200
TIMEOUT = 60


class SupabaseSink:
    def __init__(self, url: str, service_role_key: str, session: requests.Session | None = None, log: Callable[[str], None] = print):
        if not url or not service_role_key:
            raise SyncError("CONFIG_ERROR", "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required")
        self.url = url.rstrip("/")
        self._key = service_role_key
        self.session = session or requests.Session()
        self.log = log

    # -- helpers -----------------------------------------------------------
    def _headers(self, prefer: str | None = None) -> dict[str, str]:
        h = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if prefer:
            h["Prefer"] = prefer
        return h

    def _request(self, method: str, path: str, *, params=None, body=None, prefer=None, attempts: int = 3) -> requests.Response:
        last_err: str | None = None
        for attempt in range(1, attempts + 1):
            try:
                resp = self.session.request(
                    method,
                    f"{self.url}{path}",
                    params=params,
                    data=None if body is None else json.dumps(body),
                    headers=self._headers(prefer),
                    timeout=TIMEOUT,
                )
            except requests.RequestException as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                self.log(f"[supabase] {method} {path} attempt {attempt} failed: {last_err}")
                time.sleep(min(2 ** attempt, 10))
                continue
            if 200 <= resp.status_code < 300:
                return resp
            body_text = (resp.text or "")[:2000]
            if resp.status_code >= 500 and attempt < attempts:
                self.log(f"[supabase] {method} {path} HTTP {resp.status_code} (attempt {attempt}): {body_text}")
                time.sleep(min(2 ** attempt, 10))
                continue
            raise SyncError(SUPABASE_ERROR, f"{method} {path} -> HTTP {resp.status_code}: {body_text}")
        raise SyncError(SUPABASE_ERROR, f"{method} {path} failed after {attempts} attempts: {last_err}")

    # -- writes ------------------------------------------------------------
    def upsert_call_details(self, rows: list[dict], batch_size: int = BATCH_SIZE) -> int:
        """POST rows in batches with on_conflict=call_key, merge-duplicates. Returns rows sent."""
        keys = [r["call_key"] for r in rows]
        if len(keys) != len(set(keys)):
            raise SyncError(SUPABASE_ERROR, "batch contains duplicate call_key values; dedupe before upserting")
        sent = 0
        for i in range(0, len(rows), batch_size):
            batch = rows[i : i + batch_size]
            self._request(
                "POST",
                f"/rest/v1/{TABLE}",
                params={"on_conflict": "call_key"},
                body=batch,
                prefer="resolution=merge-duplicates,return=minimal",
            )
            sent += len(batch)
            self.log(f"[supabase] upserted batch {i // batch_size + 1}: {len(batch)} row(s)")
        return sent

    # -- reads (reconciliation) -------------------------------------------
    def phone_calls_by_campaign(self, day: str) -> dict[str, int]:
        """{campaign_name: phone_calls} from gads_campaign_metrics for one YYYY-MM-DD day."""
        resp = self._request(
            "GET",
            f"/rest/v1/{METRICS_TABLE}",
            params={"select": "campaign_name,phone_calls", "date": f"eq.{day}"},
        )
        out: dict[str, int] = {}
        for item in resp.json():
            name = item.get("campaign_name")
            if name is None:
                continue
            out[name] = out.get(name, 0) + int(item.get("phone_calls") or 0)
        return out

    def stored_count_for_day(self, day: str) -> int | None:
        """Rows already in google_ads_call_details for a call_date_hst (None if unavailable)."""
        try:
            resp = self.session.get(
                f"{self.url}/rest/v1/{TABLE}",
                params={"select": "id", "call_date_hst": f"eq.{day}"},
                headers={**self._headers("count=exact"), "Range-Unit": "items", "Range": "0-0"},
                timeout=TIMEOUT,
            )
        except requests.RequestException:
            return None
        if resp.status_code not in (200, 206):
            return None
        content_range = resp.headers.get("Content-Range", "")
        if "/" in content_range and content_range.rsplit("/", 1)[1].isdigit():
            return int(content_range.rsplit("/", 1)[1])
        return None
