import base64
import json

import pytest

from src import session
from src.errors import SyncError

COOKIES = [
    {"name": "SID", "value": "abc", "domain": ".google.com", "path": "/", "secure": True, "httpOnly": False, "sameSite": "None", "expires": 1800000000.5},
    {"name": "__Host-GAPS", "value": "x", "domain": "accounts.google.com", "path": "/", "secure": True, "httpOnly": True, "sameSite": "lax", "expiry": 1800000000},
    {"name": "junk", "value": "v", "domain": "", "path": "/"},  # dropped: no domain
    {"name": "noexp", "value": "v", "domain": ".google.com", "expires": -1},
]


def test_load_raw_json_and_base64_and_wrapped():
    raw = json.dumps(COOKIES)
    assert len(session.load_session(raw)) == 3
    b64 = base64.b64encode(json.dumps({"created": "x", "cookies": COOKIES}).encode()).decode()
    cookies = session.load_session(b64)
    assert [c["name"] for c in cookies] == ["SID", "__Host-GAPS", "noexp"]
    # base64 pasted with line breaks
    assert len(session.load_session(b64[:40] + "\n" + b64[40:])) == 3
    assert session.load_session("") == [] and session.load_session(None) == []


def test_normalize_cookie_shape():
    c = session.normalize_cookie(COOKIES[0])
    assert c == {"name": "SID", "value": "abc", "domain": ".google.com", "path": "/", "secure": True, "httpOnly": False, "expires": 1800000000.5, "sameSite": "None"}
    c2 = session.normalize_cookie(COOKIES[1])
    assert c2["sameSite"] == "Lax" and c2["expires"] == 1800000000.0 and c2["httpOnly"] is True
    assert "expires" not in session.normalize_cookie(COOKIES[3])


def test_summary_has_no_values():
    text = session.summarize(session.load_session(json.dumps(COOKIES)))
    assert "abc" not in text and ".google.com=2" in text and "accounts.google.com=1" in text


@pytest.mark.parametrize("bad", ["not base64 or json!!", base64.b64encode(b"\"a string\"").decode(), "[]", json.dumps([{"name": "a"}])])
def test_bad_session_is_config_error(bad):
    with pytest.raises(SyncError) as e:
        session.load_session(bad)
    assert e.value.code == "CONFIG_ERROR"
