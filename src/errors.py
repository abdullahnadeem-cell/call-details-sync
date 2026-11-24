"""Error codes shared by the runner, the workflow and the README."""
from __future__ import annotations


class SyncError(Exception):
    """A failure with a stable machine readable code (see README, Troubleshooting)."""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


# report / data
SESSION_EXPIRED = "SESSION_EXPIRED"
TABLE_NOT_RENDERED = "TABLE_NOT_RENDERED"
ROWCOUNT_MISMATCH = "ROWCOUNT_MISMATCH"
SUPABASE_ERROR = "SUPABASE_ERROR"
DATE_RANGE_MISMATCH = "DATE_RANGE_MISMATCH"
CONFIG_ERROR = "CONFIG_ERROR"
PAGE_LOAD_FAILED = "PAGE_LOAD_FAILED"
PARSE_ERROR = "PARSE_ERROR"
MISSING_SECRETS = "MISSING_SECRETS"
# google login
LOGIN_BAD_CREDENTIALS = "LOGIN_BAD_CREDENTIALS"
LOGIN_CHALLENGE = "LOGIN_CHALLENGE"
LOGIN_TOTP_MISSING = "LOGIN_TOTP_MISSING"
LOGIN_TOTP_REJECTED = "LOGIN_TOTP_REJECTED"
ADS_NO_ACCESS = "ADS_NO_ACCESS"
