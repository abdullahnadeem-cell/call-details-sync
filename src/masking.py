"""Masking for anything that reaches a log line. Full values go only to Supabase."""
from __future__ import annotations

import re


def mask_phone(value) -> str:
    """'+1 808-688-3603' -> '***3603'. Non-numbers such as '--' are returned as is."""
    if value is None:
        return ""
    text = str(value)
    digits = re.sub(r"\D", "", text)
    if len(digits) < 4:
        return text
    return "***" + digits[-4:]


def mask_email(value) -> str:
    """'abdullah@example.com' -> 'a***@example.com'."""
    if not value or "@" not in str(value):
        return "***"
    local, _, domain = str(value).partition("@")
    return f"{local[:1]}***@{domain}"
