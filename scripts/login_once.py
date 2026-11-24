"""One time, visible Google sign-in on your own computer (no proxy) that saves the session for CI.

    pip install -r requirements.txt
    python scripts/login_once.py --url "<saved report link>"      # or set GADS_REPORT_URL

A Chrome window opens on the saved report link. Sign in by hand (email, password,
2-step verification). When the Call details report has loaded, the script exports the
Google cookies to gads_session.json and prints how to store them as the GitHub secret
GADS_SESSION. The file is as sensitive as a password: it is git-ignored and never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from selenium import webdriver  # noqa: E402
from selenium.webdriver.chrome.options import Options  # noqa: E402
from selenium.webdriver.chrome.service import Service  # noqa: E402

from src.session import export_cookies, summarize  # noqa: E402

PROFILE_DIR = Path(".login-profile")
OUT_FILE = Path("gads_session.json")


def report_loaded(driver) -> bool:
    url = driver.current_url or ""
    if "ads.google.com/aw" not in url:
        return False
    try:
        n = driver.execute_script(
            "return document.querySelectorAll('[role=columnheader]').length + document.querySelectorAll('[role=row], tr').length"
        )
        text = driver.execute_script("return (document.body && document.body.innerText) || ''") or ""
    except Exception:  # noqa: BLE001
        return False
    return (n or 0) >= 3 or bool(re.search(r"\d+\s*[-–]\s*\d+\s*of\s*\d+|no data|no results", text, re.I))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("GADS_REPORT_URL"), help="saved report link (default: $GADS_REPORT_URL)")
    ap.add_argument("--out", default=str(OUT_FILE))
    ap.add_argument("--wait-minutes", type=int, default=15, help="how long to wait for the manual login")
    args = ap.parse_args()
    if not args.url:
        print("Give the saved report link with --url or set GADS_REPORT_URL.", file=sys.stderr)
        return 2

    opts = Options()
    opts.add_argument(f"--user-data-dir={PROFILE_DIR.resolve()}")
    opts.add_argument("--window-size=1400,900")
    opts.add_argument("--lang=en-US")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    if os.environ.get("CHROME_BINARY"):
        opts.binary_location = os.environ["CHROME_BINARY"]
    service = Service(os.environ["CHROMEDRIVER_PATH"]) if os.environ.get("CHROMEDRIVER_PATH") else Service()
    driver = webdriver.Chrome(service=service, options=opts)
    try:
        driver.get(args.url)
        print("\nA Chrome window is open. Sign in to Google there (email, password, 2-step verification).")
        print("If Google asks to stay signed in or to trust this browser, say yes.")
        print("The script continues by itself once the Call details report has loaded.")
        print(f"(It gives up after {args.wait_minutes} minutes.)\n")
        deadline = time.time() + args.wait_minutes * 60
        while time.time() < deadline:
            if report_loaded(driver):
                break
            time.sleep(2)
        else:
            print("The report did not load in time. Run the script again.", file=sys.stderr)
            return 1
        print("Report loaded. Waiting 5 seconds for cookies to settle...")
        time.sleep(5)
        cookies = export_cookies(driver)
    finally:
        driver.quit()

    payload = {"created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "cookies": cookies}
    Path(args.out).write_text(json.dumps(payload), encoding="utf-8")
    print(f"\nSaved {args.out}: {summarize(cookies)}")
    print("Do NOT commit or share this file.\n")
    print("Store it as the GitHub secret GADS_SESSION (base64, one line):")
    print(f"  macOS:   base64 -i {args.out} | pbcopy")
    print(f"  Linux:   base64 -w0 {args.out} | xclip -selection clipboard")
    print(f"  Windows: [Convert]::ToBase64String([IO.File]::ReadAllBytes('{args.out}')) | Set-Clipboard")
    print("  or:      gh secret set GADS_SESSION < <(base64 -w0 " + args.out + ")")
    print("Then: GitHub repo -> Settings -> Secrets and variables -> Actions -> GADS_SESSION -> paste.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
