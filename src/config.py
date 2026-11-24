"""Environment configuration. Secrets are read from the environment only and never logged."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .dates import ACCOUNT_TZ_DEFAULT
from .errors import CONFIG_ERROR, MISSING_SECRETS, SyncError

DEFAULT_SESSION_FILE = "gads_session.json"


def load_dotenv(path: str | Path = ".env", environ: dict | None = None) -> list[str]:
    """Load KEY=VALUE lines from a local .env into the environment without overriding
    variables that are already set. Returns the names that were loaded."""
    env = os.environ if environ is None else environ
    p = Path(path)
    if not p.exists():
        return []
    loaded = []
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in env:
            env[key] = value
            loaded.append(key)
    return loaded


@dataclass
class Config:
    report_url: str | None
    supabase_url: str | None
    supabase_key: str | None
    account_tz: str
    debug_dir: str
    downloads_dir: str
    gads_email: str | None = None
    gads_password: str | None = field(default=None, repr=False)
    gads_totp_secret: str | None = field(default=None, repr=False)
    profile_dir: str | None = None
    gads_session: str | None = field(default=None, repr=False)
    proxy_url: str | None = None
    chrome_debug_url: str | None = None
    session_source: str = "none"

    @classmethod
    def from_env(cls, dotenv: str | Path | None = ".env") -> "Config":
        if dotenv:
            load_dotenv(dotenv)
        session = os.environ.get("GADS_SESSION") or None
        session_source = "env" if session else "none"
        session_file = os.environ.get("GADS_SESSION_FILE") or DEFAULT_SESSION_FILE
        if not session and Path(session_file).exists():
            session = Path(session_file).read_text(encoding="utf-8")
            session_source = f"file:{session_file}"
        return cls(
            report_url=os.environ.get("GADS_REPORT_URL") or None,
            supabase_url=os.environ.get("SUPABASE_URL") or None,
            supabase_key=os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or None,
            account_tz=os.environ.get("GADS_ACCOUNT_TZ") or ACCOUNT_TZ_DEFAULT,
            debug_dir=os.environ.get("DEBUG_DIR", "debug"),
            downloads_dir=os.environ.get("DOWNLOADS_DIR", "downloads"),
            gads_email=os.environ.get("GADS_EMAIL") or None,
            gads_password=os.environ.get("GADS_PASSWORD") or None,
            gads_totp_secret=os.environ.get("GADS_TOTP_SECRET") or None,
            profile_dir=os.environ.get("GADS_PROFILE_DIR") or ".chrome-profile",
            gads_session=session,
            proxy_url=os.environ.get("GADS_PROXY_URL") or None,
            chrome_debug_url=os.environ.get("GADS_CHROME_DEBUG_URL") or None,
            session_source=session_source,
        )

    def missing_login_secrets(self) -> list[str]:
        return [n for n, v in (("GADS_EMAIL", self.gads_email), ("GADS_PASSWORD", self.gads_password)) if not v]

    def require_scrape(self) -> None:
        if not self.report_url:
            raise SyncError(MISSING_SECRETS, "GADS_REPORT_URL is not set")
        if self.gads_session or self.chrome_debug_url:
            return  # a saved session or an attached signed-in Chrome is enough; credentials are only a fallback
        missing = self.missing_login_secrets()
        if missing:
            raise SyncError(MISSING_SECRETS, f"missing: {', '.join(missing)} or GADS_SESSION (GADS_TOTP_SECRET is needed too when the account has 2-Step Verification)")

    def require_supabase(self) -> None:
        if not self.supabase_url or not self.supabase_key:
            raise SyncError(CONFIG_ERROR, "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required (or use --dry-run)")
