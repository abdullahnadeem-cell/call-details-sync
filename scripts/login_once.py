"""One time, headed Google login that saves the browser session for CI.

    python scripts/login_once.py            # uses GADS_REPORT_URL from the environment
    python scripts/login_once.py --url "<saved report link>"

A visible Chromium window opens on the saved report link. Sign in by hand
(including 2-step verification). When the report has loaded, the script saves
Playwright's storage state to storage_state.json and prints how to store it as
the GitHub secret GADS_STORAGE_STATE. The file is as sensitive as a password:
it is git-ignored and is never printed.
"""
from __future__ import annotations

import argparse
import base64
import os
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE_DIR = Path(".playwright-profile")
STATE_FILE = Path("storage_state.json")


def report_loaded(page) -> bool:
    url = page.url or ""
    if "ads.google.com/aw" not in url:
        return False
    try:
        if page.get_by_role("columnheader").count() >= 3 or page.get_by_role("row").count() > 1:
            return True
        return bool(re.search(r"\d+\s*[-–]\s*\d+\s*of\s*\d+|no data|no results", page.locator("body").inner_text(timeout=5000), re.I))
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("GADS_REPORT_URL"), help="saved report link (default: $GADS_REPORT_URL)")
    ap.add_argument("--out", default=str(STATE_FILE))
    ap.add_argument("--wait-minutes", type=int, default=15, help="how long to wait for the manual login")
    args = ap.parse_args()
    if not args.url:
        print("Give the saved report link with --url or set GADS_REPORT_URL.", file=sys.stderr)
        return 2

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1400, "height": 900},
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(args.url, wait_until="domcontentloaded")
        print("\nA browser window is open. Sign in to Google there (email, password, 2-step verification).")
        print("If Google asks to stay signed in or to trust this browser, say yes.")
        print("The script continues by itself once the Call details report has loaded.")
        print(f"(It gives up after {args.wait_minutes} minutes.)\n")
        deadline = time.time() + args.wait_minutes * 60
        while time.time() < deadline:
            if report_loaded(page):
                break
            time.sleep(2)
        else:
            print("The report did not load in time. Run the script again.", file=sys.stderr)
            context.close()
            return 1
        print("Report loaded. Waiting 5 seconds for cookies to settle...")
        time.sleep(5)
        context.storage_state(path=args.out)
        context.close()

    size = Path(args.out).stat().st_size
    print(f"\nSaved {args.out} ({size} bytes). Do NOT commit or share this file.\n")
    print("Store it as the GitHub secret GADS_STORAGE_STATE (base64, one line):")
    print(f"  macOS:   base64 -i {args.out} | pbcopy")
    print(f"  Linux:   base64 -w0 {args.out} | xclip -selection clipboard")
    print(f"  Windows: [Convert]::ToBase64String([IO.File]::ReadAllBytes('{args.out}')) | Set-Clipboard")
    print("Then: GitHub repo -> Settings -> Secrets and variables -> Actions -> GADS_STORAGE_STATE -> paste.")
    print("Or with the GitHub CLI:")
    print(f"  gh secret set GADS_STORAGE_STATE --body \"$(base64 -w0 {args.out})\"")
    # sanity check that the file round trips, without printing any of it
    base64.b64decode(base64.b64encode(Path(args.out).read_bytes()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
