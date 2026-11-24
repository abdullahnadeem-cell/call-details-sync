import json

import pytest
import requests

from src.errors import SyncError
from src.supabase_sink import SupabaseSink


class FakeResponse:
    def __init__(self, status, body="", headers=None):
        self.status_code = status
        self.text = body
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, params=None, data=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "data": data, "headers": headers})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def get(self, url, params=None, headers=None, timeout=None):
        return self.request("GET", url, params=params, headers=headers)


def rows(n):
    return [{"call_key": f"k{i}", "start_time": "2026-11-20T08:00:00-10:00", "campaign": "c"} for i in range(n)]


def test_upsert_batches_of_200_with_right_headers():
    s = FakeSession([FakeResponse(201), FakeResponse(201), FakeResponse(201)])
    sink = SupabaseSink("https://x.supabase.co/", "SECRET", session=s, log=lambda m: None)
    assert sink.upsert_call_details(rows(450)) == 450
    assert [len(json.loads(c["data"])) for c in s.calls] == [200, 200, 50]
    c = s.calls[0]
    assert c["method"] == "POST"
    assert c["url"] == "https://x.supabase.co/rest/v1/google_ads_call_details"
    assert c["params"] == {"on_conflict": "call_key"}
    assert c["headers"]["apikey"] == "SECRET"
    assert c["headers"]["Authorization"] == "Bearer SECRET"
    assert c["headers"]["Content-Type"] == "application/json"
    assert c["headers"]["Prefer"] == "resolution=merge-duplicates,return=minimal"


def test_upsert_nothing_for_empty():
    s = FakeSession([])
    sink = SupabaseSink("https://x.supabase.co", "k", session=s, log=lambda m: None)
    assert sink.upsert_call_details([]) == 0
    assert s.calls == []


def test_upsert_refuses_duplicate_keys():
    sink = SupabaseSink("https://x.supabase.co", "k", session=FakeSession([]), log=lambda m: None)
    with pytest.raises(SyncError) as e:
        sink.upsert_call_details(rows(1) + rows(1))
    assert e.value.code == "SUPABASE_ERROR"


def test_non_2xx_raises_with_status_and_body_but_not_key():
    s = FakeSession([FakeResponse(401, '{"message":"JWT invalid"}')])
    sink = SupabaseSink("https://x.supabase.co", "VERYSECRET", session=s, log=lambda m: None)
    with pytest.raises(SyncError) as e:
        sink.upsert_call_details(rows(3))
    msg = str(e.value)
    assert "HTTP 401" in msg and "JWT invalid" in msg and "VERYSECRET" not in msg


def test_retries_5xx_then_succeeds():
    logs = []
    s = FakeSession([FakeResponse(503, "busy"), FakeResponse(201)])
    sink = SupabaseSink("https://x.supabase.co", "k", session=s, log=logs.append)
    import src.supabase_sink as mod

    mod.time.sleep = lambda s: None
    assert sink.upsert_call_details(rows(2)) == 2
    assert len(s.calls) == 2


def test_connection_error_eventually_fails():
    import src.supabase_sink as mod

    mod.time.sleep = lambda s: None
    s = FakeSession([requests.ConnectionError("boom")] * 3)
    sink = SupabaseSink("https://x.supabase.co", "k", session=s, log=lambda m: None)
    with pytest.raises(SyncError) as e:
        sink.upsert_call_details(rows(1))
    assert e.value.code == "SUPABASE_ERROR"


def test_phone_calls_by_campaign():
    body = json.dumps([
        {"campaign_name": "A", "phone_calls": 2},
        {"campaign_name": "B", "phone_calls": 0},
        {"campaign_name": "A", "phone_calls": 1},
    ])
    s = FakeSession([FakeResponse(200, body)])
    sink = SupabaseSink("https://x.supabase.co", "k", session=s, log=lambda m: None)
    assert sink.phone_calls_by_campaign("2026-11-20") == {"A": 3, "B": 0}
    c = s.calls[0]
    assert c["url"].endswith("/rest/v1/gads_campaign_metrics")
    assert c["params"] == {"select": "campaign_name,phone_calls", "date": "eq.2026-11-20"}


def test_stored_count_from_content_range():
    s = FakeSession([FakeResponse(206, "[]", {"Content-Range": "0-0/42"})])
    sink = SupabaseSink("https://x.supabase.co", "k", session=s, log=lambda m: None)
    assert sink.stored_count_for_day("2026-11-20") == 42
