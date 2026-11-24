"""Error codes shared by the runner, the workflow and the README."""


class SyncError(Exception):
    """A failure with a stable machine readable code.

    Codes used by the workflow alert step and documented in README:
    SESSION_EXPIRED, TABLE_NOT_RENDERED, ROWCOUNT_MISMATCH, SUPABASE_ERROR,
    DATE_RANGE_MISMATCH, CONFIG_ERROR, PAGE_LOAD_FAILED.
    """

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


SESSION_EXPIRED = "SESSION_EXPIRED"
TABLE_NOT_RENDERED = "TABLE_NOT_RENDERED"
ROWCOUNT_MISMATCH = "ROWCOUNT_MISMATCH"
SUPABASE_ERROR = "SUPABASE_ERROR"
DATE_RANGE_MISMATCH = "DATE_RANGE_MISMATCH"
CONFIG_ERROR = "CONFIG_ERROR"
PAGE_LOAD_FAILED = "PAGE_LOAD_FAILED"
PARSE_ERROR = "PARSE_ERROR"
