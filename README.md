# call-details-sync

Pulls the **Call details** report from the Google Ads web UI once a day and upserts
every call (including the caller's phone number, which the Google Ads API never
exposes) into the Supabase table `public.google_ads_call_details`. The TCA Wireless
dashboard uses that table to tie call leads to sales.

- Python 3.12 + Selenium (Chrome). No frontend, no n8n, no schema changes.
- **Reliable run mode: locally, in a browser you signed in to once** (`scripts/login_once.py` or
  `scripts/chrome.sh`). The GitHub Actions schedule exists, but Google refuses scripted sign-ins from
  GitHub's datacenter addresses with CAPTCHAs and phone prompts, so until a self-hosted runner on a
  normal connection is set up, run it from your own computer (§3).
- Authenticates in this order: a **saved session** captured once on a normal computer
  (`GADS_SESSION`), then, only if that is rejected, a self sign-in as the dedicated Workspace
  account (password + authenticator code from GitHub secrets). Plain Selenium, no stealth tricks:
  if Google challenges the login the run fails with a clear code.
- Idempotent: re-running a day never creates duplicates (`call_key` upsert).

```
call-details-sync/
  src/run.py             CLI entry point
  src/report_url.py      rewrites the date range inside the saved report link
  src/google_login.py    self sign-in (email, password, authenticator), screen detection
  src/session.py         saved session (cookies) decode / inject
  scripts/login_once.py  one time visible sign-in on your computer that exports the session
  src/scrape.py          drives Chrome, returns raw rows
  src/parse.py           normalizes rows (times, durations, call_key)
  src/supabase_sink.py   REST upsert + reconciliation reads
  src/reconcile.py       informational per-day table
  src/masking.py         masks phone numbers / emails in logs
  scripts/rows_to_sql.py turns dry-run JSON into upsert SQL (manual loads)
  tests/                 unit tests + local fake report and fake sign-in pages for the browser tests
  .github/workflows/call-details-sync.yml   the daily job
  .github/workflows/tests.yml               pytest on every push
```

---

## 1. How it works

**Two time zones, both on purpose.** The Google Ads *account* is on Pacific time
(`America/Los_Angeles`): the report shows every call time in it, so the parser reads the displayed
time in that zone and stores the exact UTC instant in `start_time` (a call shown as Nov 6, 12:00 PM
is `2026-11-06T20:00:00Z`; minutes and seconds are kept; daylight saving is handled by the IANA zone,
which switched from UTC-7 to UTC-8 on Nov 1, 2026). The database trigger then derives
`call_date_hst` in Hawaii time (`Pacific/Honolulu`) because the calls go to a Hawaii business and are
matched to Hawaii-day sales. Do not "fix" one to match the other.

1. **Date range.** With no arguments the run covers the last 2 full days in Hawaii
   time (yesterday and the day before), because Google delivers call data late and
   the upsert is idempotent. `--date`, `--from/--to` and `--lookback N` override this.
2. **Sign in.** If `GADS_SESSION` is set, its Google cookies are injected into Chrome before
   the report opens; the log then says whether Google **accepted** the saved session (the report
   rendered) or **rejected** it (a sign-in page appeared). Without a usable session, Chrome opens
   `accounts.google.com`. If the reused profile is still signed in, nothing else happens.
   Otherwise the job types the email and password,
   answers the 2-Step Verification prompt with a code from the authenticator secret,
   dismisses "stay signed in" style screens and picks the right account in the chooser.
   Anything else (phone prompt, SMS, passkey, captcha, "Verify it's you") fails the run
   with `LOGIN_CHALLENGE`.
3. **Report URL.** The saved report link stores its custom date range inside the
   `reportState` parameter (base64 protobuf) as two plain `YYYY-MM-DD` strings. The
   job swaps the two dates (same length, so every length prefix stays valid) and loads
   that URL. If the page does not show the requested range, it falls back to the UI date
   picker and logs which method was used. A range longer than 7 days is loaded one day at a time.
4. **Verify.** The page must show the requested range (checked with the data grid text
   removed) and every parsed row must start inside the range.
5. **Read rows.** Preferred: the report's own **Download → .csv** (headless downloads are
   allowed through Chrome DevTools). Fallback: scrape the grid (rows per page maximised,
   scroll, paginate). The row count must equal the total the page reports ("1 – 25 of 25")
   or the run fails with `ROWCOUNT_MISMATCH`. Zero calls is valid (nothing written, exit 0).
6. **Write.** Rows are normalised (§4), de-duplicated by `call_key`, and POSTed to the
   Supabase REST API in batches of 200 with `on_conflict=call_key` and
   `Prefer: resolution=merge-duplicates`. The service role key is required (RLS is on
   with no policies).
7. **Reconcile (informational).** For each day: calls per campaign in the report vs
   `phone_calls` in `gads_campaign_metrics`, plus how many rows have a masked caller (`--`).
   Mismatches are expected (Google only counts calls above a minimum duration as
   conversions); this never fails the run.

Phone numbers are **masked to the last 4 digits in every log line**. Full numbers go only
to Supabase (and to the `downloads/`, `out/` and `debug/` folders, which the workflow deletes
or uploads only when the repository is private).

## 2. Setup

### 2.1 The Google account

Use a **dedicated Google Workspace user** (currently `abdullahnadeem@agentumai.tech`) that
has access to the TCA Google Ads account. Recommended: read-only (standard) access in
Google Ads, 2-Step Verification on with an **authenticator app** as the method, and no
phone-prompt / passkey as the default second step (the script can only answer
authenticator codes). Keep the recovery options current so Google is less likely to
challenge sign-ins.

To get the authenticator secret: Google Account → Security → 2-Step Verification →
Authenticator → "Set up" (or "Change") → **"Can't scan it?"** shows the base32 secret
(letters and digits, spaces are fine). Also scan the QR code into a phone app so a human
keeps a working second factor.

### 2.2 Capture the saved session once (recommended first path)

Scripted sign-ins from GitHub's datacenter addresses are usually met with a CAPTCHA (see §6),
while a session that was created on a normal connection is typically accepted for weeks. So
the first thing to try is a saved session:

```bash
pip install -r requirements.txt
python scripts/login_once.py --url "<saved report link>"
```

A Chrome window opens; sign in by hand (email, password, 2-step verification). When the
Call details report has loaded, the script writes `gads_session.json` (git-ignored) and prints
the one-liner to store it as the secret `GADS_SESSION` (base64 of the file). Nothing is
printed from the file itself. When Google later rejects that session the run fails with
`SESSION_EXPIRED` (or falls back to the credentials if they are set); run the script again
and update the secret.

### 2.3 GitHub secrets and variables

Repo → **Settings → Secrets and variables → Actions**. Never paste these anywhere else.

| Name | Type | Value |
| --- | --- | --- |
| `GADS_EMAIL` | secret | the dedicated account's email |
| `GADS_PASSWORD` | secret | its password |
| `GADS_TOTP_SECRET` | secret | the authenticator base32 secret (see §2.1) |
| `GADS_SESSION` | secret | base64 of `gads_session.json` from `scripts/login_once.py` (see §2.2); with it, email/password are only a fallback |
| `GADS_PROXY_URL` | secret, optional | `http://host:port` or `socks5://host:port` the browser should use; leave empty for a direct connection. Chrome cannot send proxy passwords, so use an IP-allowlisted proxy |
| `GADS_REPORT_URL` | secret | the full saved "Call details" report link |
| `SUPABASE_URL` | secret | `https://hdwkfqjgnuahqmmaseor.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | secret | Supabase → Project Settings → API → `service_role` |
| `ALERT_WEBHOOK_URL` | secret, optional | any webhook that accepts a JSON POST (`{"text": "..."}`) |
| `GADS_ACCOUNT_TZ` | **variable**, optional | Google Ads account time zone, default `America/Los_Angeles` |
| `RUNNER_LABEL` | **variable**, optional | runner label, default `ubuntu-latest` (see §6) |
| `GADS_PROFILE_DIR` | **variable**, optional | persistent Chrome profile folder on a self-hosted runner |

The workflow's first step checks these and fails with `MISSING_SECRETS`, printing only the
**names** that are empty. Required: `GADS_REPORT_URL` plus either `GADS_SESSION` or
`GADS_EMAIL` + `GADS_PASSWORD`.

**The repository must be private before real data runs.** Logs are masked, but failure
screenshots, downloads and the cached Chrome profile (cookies) are only kept when the
repository is private; the workflow checks this itself and skips them otherwise.
Settings → General → Danger Zone → Change visibility.

### 2.4 Enabling the schedule

The workflow is kept disabled until the secrets exist. Turn it on with:

```bash
gh workflow enable call-details-sync.yml
```

## 3. Running

### GitHub Actions

Actions → **call-details-sync** → **Run workflow**. Inputs: `date` (one day), `from`/`to`
(range), `dry_run` (print only, no Supabase write). Leave all empty for the default window.
Scheduled runs happen at 16:30 UTC (06:30 HST) and 04:30 UTC (18:30 HST). Two runs never
overlap (concurrency group) and each is limited to 15 minutes.

### Locally, in a browser you signed in to (the mode that works)

One-time setup on your computer (macOS or Linux, Chrome installed):

```bash
git clone https://github.com/abdullahnadeem-cell/call-details-sync && cd call-details-sync
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # then fill in SUPABASE_SERVICE_ROLE_KEY and GADS_REPORT_URL
```

`.env` is git-ignored and read automatically (the real environment wins over it). Then sign in
once, either way:

- **Saved session (headless afterwards).** A visible Chrome opens on the report; sign in by hand
  (password, 2FA, any "verify it's you"); when the report loads the cookies are written to
  `gads_session.json` (git-ignored) and every later run reuses them without any scripted login:

  ```bash
  python scripts/login_once.py
  ```

- **Your own signed-in Chrome window (attach).** A dedicated Chrome profile, signed in by hand once,
  then started with a DevTools port that the sync attaches to; the sync works in a new tab and
  closes only that tab:

  ```bash
  scripts/chrome.sh signin     # sign in, open the Call details report once, then
  scripts/chrome.sh start      # prints GADS_CHROME_DEBUG_URL=127.0.0.1:9333 -> put it in .env
  ```

Then:

```bash
python -m src.run --date 2026-11-20 --dry-run        # browser + parse, no DB write, JSON in out/
```

```bash
python -m src.run --date 2026-11-20                  # real write
```

```bash
python -m src.run --from 2026-11-01 --to 2026-11-23  # backfill (one page load per day when > 7 days)
```

```bash
python -m src.run --dry-run --from-file tests/fixtures/call_details_sample.csv   # no browser at all
```

Other flags: `--headed` shows the browser, `--lookback N`, `--out path.json`, `--no-reconcile`.
`--from-file` accepts a CSV download from the report or a JSON list of raw rows (a previous
dry-run file also works). Optional env: `GADS_SESSION_FILE` (default `gads_session.json`),
`GADS_CHROME_DEBUG_URL`, `GADS_PROFILE_DIR` (Chrome profile folder, default `.chrome-profile`),
`CHROME_BINARY`, `CHROMEDRIVER_PATH` (override Selenium Manager). If Google shows a sign-in or
verification page, the run stops with `SESSION_EXPIRED`: sign in again by hand (`login_once.py`
or `chrome.sh signin`); nothing scripts around it.

### Backfill

The end date can be at most today in the account time zone (Google greys out future days; the
run refuses a later date). Ranges over 7 days load one day per page.

```bash
python -m src.run --from 2026-11-01 --to $(TZ=America/Los_Angeles date +%F)      # macOS: same command
```

Re-running any part of the range is safe: rows are upserted on `call_key`, so the row count does
not change on a second pass.

### Loading rows through SQL instead of the REST key

Parse with `--dry-run`, turn the JSON into upsert SQL and run it in the Supabase SQL editor.
Same upsert on `call_key`, so re-running never duplicates.

```bash
python scripts/rows_to_sql.py out/rows_2026-11-20_2026-11-20.json > out/load.sql
```

### Verify in Supabase (SQL editor)

```sql
select call_date_hst, count(*) from google_ads_call_details group by 1 order by 1;
```

```sql
select count(*) filter (where caller_phone_e164 is null) as no_phone,
       count(*) filter (where campaign_id is null)       as no_campaign_id,
       count(*)
from google_ads_call_details;
```

## 4. Column mapping

| Report column | Supabase column | Notes |
| --- | --- | --- |
| CallStartTime | `start_time` | parsed in `GADS_ACCOUNT_TZ`, sent as ISO 8601 with offset |
| CallDuration | `duration_seconds` | `0:35`, `1:02:03`, `35 sec`, `1 min 5 sec`, `35` → integer seconds |
| CallerCountryCallingCode | `caller_country_code` | text |
| CallerNationalDesignatedCode | `caller_area_code` | text |
| CallerId | `caller_phone_raw` | exactly as shown, including `--` (row is kept) |
| CallRecordingReplay | `recording_url` | URL (href of the link in the grid) or null |
| GoogleVoiceCallType | `call_type` | |
| CallTrackingDisplayLocation | `display_location` | |
| AdTelephonyPropertyGroup | `ad_telephony_group` | |
| Campaign | `campaign` | exactly as shown |
| whole row | `raw` | jsonb `{original header: cell}` |
| computed | `call_key` | `sha1("{start_time ISO UTC}|{caller_phone_raw}|{duration_seconds}|{campaign}")`, e.g. `2026-11-21T00:35:10+00:00|+1 808-688-3603|35|TCA - Search - Brand`; a missing value is an empty string |

Not sent: `status` (so a value set elsewhere survives re-runs), and the trigger-owned
`caller_phone_e164`, `campaign_id`, `call_date_hst`. Left empty on purpose:
`caller_phone_number`, `call_source`, `ad_group`. Cells showing `--` become null for every
column except `caller_phone_raw`. Headers are matched by name (internal names such as
`CallStartTime` and UI labels such as "Start time"), with a positional fallback in the
report's column order; the log warns when the fallback is used.

## 5. Session reuse (GitHub Actions)

Google challenges fresh logins from new machines far more often than returning browsers.
The job therefore keeps a Chrome profile (`.chrome-profile/`, cookies included) and carries
it from run to run with `actions/cache` (restore at start, trimmed and saved at the end under
`chrome-profile-v1-<os>-<run id>`, restored by prefix so the newest wins). On the next run the
account is usually still signed in and no password or code is typed at all. If the restored
profile is rejected, the job simply signs in again with the secrets.

Because the profile contains cookies, caching is **only enabled when the repository is
private**. On a self-hosted runner set the variable `GADS_PROFILE_DIR` to a persistent folder
and the cache steps are skipped.

## 6. If Google blocks sign-ins from GitHub's IPs

Scripted Google logins from datacenter addresses are sometimes refused outright
("This browser or app may not be secure", "Couldn't sign you in", endless "Verify it's you").
The job reports these as `LOGIN_CHALLENGE` and takes a screenshot of the Google page
(uploaded only when the repo is private). **Do not keep re-running**: repeated failed attempts
can lock the account. After two honest attempts:

1. Sign in once by hand in a normal browser with that account, approve any "new sign-in"
   notice and check Security → Recent security activity.
2. Run the job from a normal home or office connection: install the GitHub Actions
   runner on a machine there (`Settings → Actions → Runners → New self-hosted runner`,
   Chrome installed), then set the repository variable `RUNNER_LABEL=self-hosted` and
   `GADS_PROFILE_DIR` to a folder on that machine (for example `/home/runner/gads-profile`).
   No code change is needed; the schedule, inputs and secrets stay the same.

## 7. Troubleshooting by error code

The last log line reads `ERROR <CODE>: <detail>`; the workflow posts `<CODE>` to
`ALERT_WEBHOOK_URL` when set and uploads `debug/` (3 day retention, private repos only).

| Code | Meaning | What to do |
| --- | --- | --- |
| `MISSING_SECRETS` | One or more required secrets are empty (names are printed). | Add them under Settings → Secrets and variables → Actions. |
| `LOGIN_BAD_CREDENTIALS` | Google rejected the email or password. | Check `GADS_EMAIL` / `GADS_PASSWORD`; sign in by hand once to confirm the password. |
| `LOGIN_TOTP_MISSING` | Google asked for an authenticator code but `GADS_TOTP_SECRET` is empty. | Add the base32 secret (§2.1). |
| `LOGIN_TOTP_REJECTED` | The code was refused. | Wrong secret, or the account's authenticator was re-enrolled: generate a new secret and update it. Runner clocks are accurate, so skew is unlikely. |
| `LOGIN_CHALLENGE` | Google asked for something a script cannot do (phone tap, SMS, passkey, captcha, "Verify it's you", "browser not secure"), or showed an unknown screen. | See the screenshot `debug/login-*.png`. Follow §6. Make the authenticator the default second step and remove phone prompts if possible. |
| `ADS_NO_ACCESS` | Signed in, but Google Ads shows an account chooser or "no access" for the report. | Give the dedicated user access to the Ads account in the saved link (customer id in `ocid=`). |
| `SESSION_EXPIRED` | The saved session was rejected (log says `saved session rejected`) and no credentials could sign in, or a sign-in page appeared even after the retry. | Run `scripts/login_once.py` again and update `GADS_SESSION`; or look for a `LOGIN_*` cause further up the log. |
| `TABLE_NOT_RENDERED` | The report page loaded but neither the grid nor a "no data" message appeared within 90 s. | Open the saved link in a browser: is the report still there, same account? Google Ads UI change → check the HTML in `debug/`. Transient → re-run. |
| `DATE_RANGE_MISMATCH` | The page did not show the requested range after the URL rewrite and the date picker fallback, or rows start outside it. | Check the report link still has a *custom* date range. If the date picker layout changed, see `_set_range_via_ui` in `src/scrape.py`. |
| `ROWCOUNT_MISMATCH` | Rows captured ≠ the total the page shows. | Re-run once (slow render). If it repeats, check the CSV download in a browser; maybe it includes extra summary rows (`parse.rows_from_csv_text`). |
| `SUPABASE_ERROR` | Non-2xx from Supabase REST; status and body are in the log. 401/403 = wrong key (must be `service_role`); 400 = column problem. | Check `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY`. Re-running is safe. |
| `PARSE_ERROR` | A cell could not be parsed (unexpected time or duration format, missing columns). | The detail names the cell. Add the format to `src/parse.py` (`TIME_FORMATS`, `parse_duration`). |
| `PAGE_LOAD_FAILED` | Navigation failed twice. | Network / Google outage; re-run. |
| `CONFIG_ERROR` | Bad CLI dates or missing Supabase settings for a real run. | See the message. |

Notes:

- The job never calls GoHighLevel or any tagging webhook. Contact linking is done in the database.
- Tests: `python -m pytest -q`. The browser tests drive real Chrome against
  `tests/fixtures/fake_report/` and `tests/fixtures/fake_login/` and are skipped when Chrome
  or a matching driver is not available (set `CHROME_BINARY` / `CHROMEDRIVER_PATH` locally).
