"""Environment configuration. Secrets are read from the environment only."""
from __future__ import annotations

import os
from dataclasses import dataclass

from .dates import HST
from .errors import CONFIG_ERROR, SyncError


@dataclass
class Config:
    report_url: str | None
    storage_state_path: str
    supabase_url: str | None
    supabase_key: str | None
    account_tz: str
    debug_dir: str
    downloads_dir: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            report_url=os.environ.get("GADS_REPORT_URL") or None,
            storage_state_path=os.environ.get("GADS_STORAGE_STATE_PATH", "storage_state.json"),
            supabase_url=os.environ.get("SUPABASE_URL") or None,
            supabase_key=os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or None,
            account_tz=os.environ.get("GADS_ACCOUNT_TZ") or HST,
            debug_dir=os.environ.get("DEBUG_DIR", "debug"),
            downloads_dir=os.environ.get("DOWNLOADS_DIR", "downloads"),
        )

    def require_scrape(self) -> None:
        if not self.report_url:
            raise SyncError(CONFIG_ERROR, "GADS_REPORT_URL is not set")
        if not os.path.exists(self.storage_state_path):
            raise SyncError(CONFIG_ERROR, f"storage state file {self.storage_state_path} not found; run scripts/login_once.py")

    def require_supabase(self) -> None:
        if not self.supabase_url or not self.supabase_key:
            raise SyncError(CONFIG_ERROR, "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required (or use --dry-run)")
