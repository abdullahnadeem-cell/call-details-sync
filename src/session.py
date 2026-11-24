"""Saved Google session (cookies) captured once by a human and injected on the runner.

The secret GADS_SESSION holds the JSON written by scripts/login_once.py, either raw or
base64 encoded. Cookie values never reach logs; only counts and domains do.
"""
from __future__ import annotations

import base64
import binascii
import json
from collections import Counter
from typing import Any

from .errors import CONFIG_ERROR, SyncError

_KEEP = ("name", "value", "domain", "path", "secure", "httpOnly", "sameSite", "expires")
_SAMESITE = {"strict": "Strict", "lax": "Lax", "none": "None"}


def load_session(text: str | None) -> list[dict[str, Any]]:
    """Return the cookie list from the GADS_SESSION secret (raw JSON or base64 of it)."""
    if not text or not text.strip():
        return []
    raw = text.strip()
    data = None
    if raw[:1] in "{[":
        data = json.loads(raw)
    else:
        try:
            decoded = base64.b64decode("".join(raw.split()), validate=True)
            data = json.loads(decoded.decode("utf-8-sig"))
        except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
            raise SyncError(CONFIG_ERROR, f"GADS_SESSION is neither JSON nor base64 JSON ({type(exc).__name__})") from exc
    cookies = data.get("cookies") if isinstance(data, dict) else data
    if not isinstance(cookies, list):
        raise SyncError(CONFIG_ERROR, "GADS_SESSION must be a JSON list of cookies or {'cookies': [...]}")
    out = []
    for c in cookies:
        if not isinstance(c, dict) or not c.get("name") or "value" not in c or not c.get("domain"):
            continue
        out.append(normalize_cookie(c))
    if not out:
        raise SyncError(CONFIG_ERROR, "GADS_SESSION contains no usable cookies")
    return out


def normalize_cookie(c: dict[str, Any]) -> dict[str, Any]:
    """Map a cookie from Selenium get_cookies() / CDP getAllCookies to CDP Network.setCookies shape."""
    out: dict[str, Any] = {
        "name": str(c["name"]),
        "value": str(c["value"]),
        "domain": str(c["domain"]),
        "path": str(c.get("path") or "/"),
        "secure": bool(c.get("secure", False)),
        "httpOnly": bool(c.get("httpOnly", False)),
    }
    expires = c.get("expires", c.get("expiry"))
    try:
        expires = float(expires) if expires is not None else None
    except (TypeError, ValueError):
        expires = None
    if expires and expires > 0:
        out["expires"] = expires
    same = c.get("sameSite")
    if isinstance(same, str) and same.lower() in _SAMESITE:
        out["sameSite"] = _SAMESITE[same.lower()]
    if out["sameSite"] == "None" if "sameSite" in out else False:
        out["secure"] = True
    return out


def summarize(cookies: list[dict[str, Any]]) -> str:
    """Credential-free one-liner: counts per domain."""
    counts = Counter(c["domain"] for c in cookies)
    parts = ", ".join(f"{d}={n}" for d, n in sorted(counts.items()))
    return f"{len(cookies)} cookie(s): {parts}"


def inject_cookies(driver, cookies: list[dict[str, Any]]) -> int:
    """Set cookies through DevTools (works for any domain without navigating). Returns count set."""
    if not cookies:
        return 0
    driver.execute_cdp_cmd("Network.enable", {})
    driver.execute_cdp_cmd("Network.setCookies", {"cookies": cookies})
    return len(cookies)


def export_cookies(driver, domain_filter: tuple[str, ...] = ("google.com", "youtube.com", "gstatic.com", "googleusercontent.com")) -> list[dict[str, Any]]:
    """All cookies in the browser profile (CDP), limited to Google domains."""
    res = driver.execute_cdp_cmd("Network.getAllCookies", {})
    out = []
    for c in res.get("cookies", []):
        dom = (c.get("domain") or "").lstrip(".")
        if any(dom == f or dom.endswith("." + f) for f in domain_filter):
            out.append(normalize_cookie(c))
    return out
