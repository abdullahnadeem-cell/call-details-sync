"""Environment configuration. Secrets are read from the environment only and never logged."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .dates import HST
from .errors import CONFIG_ERROR, MISSING_SECRETS, SyncError


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

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            report_url=os.environ.get("GADS_REPORT_URL") or None,
            supabase_url=os.environ.get("SUPABASE_URL") or None,
            supabase_key=os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or None,
            account_tz=os.environ.get("GADS_ACCOUNT_TZ") or HST,
            debug_dir=os.environ.get("DEBUG_DIR", "debug"),
            downloads_dir=os.environ.get("DOWNLOADS_DIR", "downloads"),
            gads_email=os.environ.get("GADS_EMAIL") or None,
            gads_password=os.environ.get("GADS_PASSWORD") or None,
            gads_totp_secret=os.environ.get("GADS_TOTP_SECRET") or None,
            profile_dir=os.environ.get("GADS_PROFILE_DIR") or ".chrome-profile",
        )

    def missing_login_secrets(self) -> list[str]:
        return [n for n, v in (("GADS_EMAIL", self.gads_email), ("GADS_PASSWORD", self.gads_password)) if not v]

    def require_scrape(self) -> None:
        if not self.report_url:
            raise SyncError(MISSING_SECRETS, "GADS_REPORT_URL is not set")
        missing = self.missing_login_secrets()
        if missing:
            raise SyncError(MISSING_SECRETS, f"missing: {', '.join(missing)} (GADS_TOTP_SECRET is needed too when the account has 2-Step Verification)")

    def require_supabase(self) -> None:
        if not self.supabase_url or not self.supabase_key:
            raise SyncError(CONFIG_ERROR, "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required (or use --dry-run)")
