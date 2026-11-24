# call-details-sync

Pulls the **Call details** report from the Google Ads web UI once a day and upserts
every call (including the caller's phone number, which the Google Ads API never
exposes) into the Supabase table `public.google_ads_call_details`. The TCA Wireless
dashboard uses that table to tie call leads to sales.

- Runs on GitHub Actions (06:30 HST and 18:30 HST), or by hand for any day or range.
- Python 3.12 + Playwright (Chromium). No frontend, no n8n, no schema changes.
- Idempotent: re-running a day never creates duplicates (`call_key` upsert).
- Authentication is a saved browser session captured once by a human. **No password,
  2FA code or app password is ever scripted.**

```
call-details-sync/
  src/run.py             CLI entry point
  src/report_url.py      rewrites the date range inside the saved report link
  src/scrape.py          drives Chromium, returns raw rows
  src/parse.py           normalizes rows (times, durations, call_key)
  src/supabase_sink.py   REST upsert + reconciliation reads
  src/reconcile.py       informational per-day table
  scripts/login_once.py  one time headed login that saves storage_state.json
  tests/                 unit tests + a local fake report page for the scraper
  .github/workflows/call-details-sync.yml
```

---

## 1. How it works

1. **Date range.** With no arguments the run covers the last 2 full days in Hawaii
   time (yesterday and the day before), because Google delivers call data late and
   the upsert is idempotent. `--date`, `--from/--to` and `--lookback N` override this.
2. **Report URL.** The saved report link stores its custom date range inside the
   `reportState` parameter (base64 protobuf) as two plain `YYYY-MM-DD` strings. The
   job decodes it, swaps the two dates (same length, so every length prefix stays
   valid), re-encodes with the same base64 flavour and loads that URL. If the page
   does not show the requested range, it falls back to the UI date picker and logs
   which method was used. A range longer than 7 days is loaded one day at a time.
3. **Session check.** If the browser lands on `accounts.google.com`, a sign-in or a
   "Verify it's you" page, the run stops with `SESSION_EXPIRED`.
4. **Verify.** The date range shown on the page (outside the data grid) must match
   the request, and every parsed row must start inside the range.
5. **Read rows.** Preferred: the report's own **Download → .csv** (complete result).
   Fallback: scrape the grid (rows per page maximised, virtual scroll, pagination).
   The row count must equal the total the page reports ("1 – 25 of 25") or the run
   fails with `ROWCOUNT_MISMATCH`. Zero calls is valid (nothing written, exit 0).
6. **Write.** Rows are normalised (see §4), de-duplicated by `call_key`, and POSTed to
   Supabase REST in batches of 200 with `on_conflict=call_key` and
   `Prefer: resolution=merge-duplicates`. The service role key is required (RLS is on
   with no policies, so the anon key is rejected on purpose).
7. **Reconcile (informational).** For each day: calls per campaign in the report vs
   `phone_calls` in `gads_campaign_metrics` for that campaign and date, plus the rows
   whose caller is shown as `--`. Mismatches are expected (Google only counts calls
   above a minimum duration as conversions); this never fails the run.

## 2. One time setup (Roch)

### 2.1 Which Google account

Use a **dedicated Google user** that has **read-only** access to the TCA Google Ads
account, with **2-Step Verification on**. Do not use a personal login. The saved
session is as sensitive as that user's password.

### 2.2 Install locally (once, on your own computer)

```bash
git clone <this repo> && cd call-details-sync
python3.12 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

### 2.3 Capture the browser session, screen by screen

```bash
export GADS_REPORT_URL='<the saved Call details report link>'   # Windows PowerShell: $env:GADS_REPORT_URL='...'
python scripts/login_once.py
```

1. A Chromium window opens on the saved report link. Google shows **"Sign in"**.
   Enter the dedicated user's email → Next.
2. **Password** → Next.
3. **2-Step Verification**: approve the prompt on the phone / enter the code.
4. If Google asks **"Stay signed in?"** or **"Trust this browser?"** → choose **Yes**.
   If it offers "Don't ask again on this device", tick it.
5. Google Ads opens the **Call details** report. If an account chooser appears, pick
   the TCA account. Wait until the table (or "No data") is visible.
6. The terminal prints `Saved storage_state.json`. The window closes by itself.

`storage_state.json` is git-ignored. Never commit it, email it or paste it anywhere
except the GitHub secret below.

### 2.4 Add the GitHub secrets and variable

Repo → **Settings → Secrets and variables → Actions**.

| Name | Type | Value |
| --- | --- | --- |
| `GADS_STORAGE_STATE` | secret | base64 of `storage_state.json`, one line (see below) |
| `GADS_REPORT_URL` | secret | the full saved report link |
| `SUPABASE_URL` | secret | `https://hdwkfqjgnuahqmmaseor.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | secret | Supabase → Project Settings → API → `service_role` |
| `ALERT_WEBHOOK_URL` | secret, optional | any webhook that accepts a JSON POST (`{"text": "..."}`) |
| `GADS_ACCOUNT_TZ` | **variable**, optional | Google Ads account time zone, default `Pacific/Honolulu` |

Base64 of the session file, copied to the clipboard:

```bash
# macOS
base64 -i storage_state.json | pbcopy
```

```bash
# Linux
base64 -w0 storage_state.json | xclip -selection clipboard
```

```powershell
# Windows PowerShell
[Convert]::ToBase64String([IO.File]::ReadAllBytes('storage_state.json')) | Set-Clipboard
```

Or with the GitHub CLI:

```bash
gh secret set GADS_STORAGE_STATE --body "$(base64 -w0 storage_state.json)"
```

The repository **must stay private**: failure screenshots and dry-run logs contain phone numbers.
Check Settings → General → Danger Zone → Change visibility.

## 3. Running

### GitHub Actions

Actions → **call-details-sync** → **Run workflow**. Inputs: `date` (one day), `from`/`to`
(range), `dry_run` (print only). Leave all empty for the default window.
Scheduled runs happen at 16:30 UTC (06:30 HST) and 04:30 UTC (18:30 HST).
Two runs never overlap (concurrency group) and each is limited to 15 minutes.

### Locally

```bash
export GADS_REPORT_URL='...' SUPABASE_URL='...' SUPABASE_SERVICE_ROLE_KEY='...'
python -m src.run --date 2026-11-20 --dry-run        # browser + parse, no DB write, JSON in out/
```

```bash
python -m src.run --date 2026-11-20                  # real write
```

```bash
python -m src.run --from 2026-11-01 --to 2026-11-23  # backfill (one page load per day when > 7 days)
```

```bash
python -m src.run --lookback 5                       # the last 5 full HST days
```

```bash
python -m src.run --dry-run --from-file tests/fixtures/call_details_sample.csv   # no browser at all
```

Other flags: `--headed` shows the browser, `--out path.json` sets the dry-run output,
`--no-reconcile` skips the reconciliation table. `--from-file` accepts a CSV download
from the report or a JSON list of raw rows (a previous dry-run file also works).

### Backfill

```bash
python -m src.run --from 2026-11-01 --to $(date -u -d 'yesterday' +%F)
```

or run the workflow with `from=2026-11-01`, `to=<yesterday>`. Re-running any part of
the range is safe.

### Loading rows through SQL instead of the REST key

If the rows were read some other way (for example from a CSV downloaded by hand) and you
want to load them without the service role key, parse them with `--dry-run`, then turn the
JSON into upsert SQL and run it in the Supabase SQL editor or through the Supabase connector.
It is the same upsert on `call_key`, so re-running never duplicates.

```bash
python -m src.run --from-file "Call details.csv" --date 2026-11-20 --dry-run --out out/rows.json
```

```bash
python scripts/rows_to_sql.py out/rows.json > out/load.sql
```

`status` is never written, so a status set elsewhere is not overwritten by a re-run.

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

```sql
-- recent calls with their campaign match
select start_time, duration_seconds, caller_phone_raw, caller_phone_e164, campaign, campaign_id
from google_ads_call_details order by start_time desc limit 50;
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
| computed | `status` | null (this report has no status column) |

Not sent, filled by the database trigger: `caller_phone_e164`, `campaign_id`,
`call_date_hst`. Left empty on purpose: `caller_phone_number`, `call_source`, `ad_group`.
Cells showing `--` become null for every column except `caller_phone_raw`.

Headers are matched by name (internal names such as `CallStartTime` and UI labels such
as "Start time"), with a positional fallback in the report's column order when a header
is unknown; the log shows a warning when the fallback is used.

## 5. Session refresh (expected every few weeks)

Google sessions captured this way usually last **several weeks**, not forever. They end
sooner if the user signs out everywhere, changes the password, or Google flags the
GitHub runner's IP. When a run fails with `SESSION_EXPIRED`:

1. On your computer, run `python scripts/login_once.py` again (§2.3).
2. Update the `GADS_STORAGE_STATE` secret with the new base64 (§2.4).
3. Re-run the failed workflow (Actions → the failed run → **Re-run jobs**), or run it by
   hand with `from`/`to` covering the days that were missed.

Nothing else needs to change. Put a monthly reminder in the calendar to refresh
proactively.

## 6. Troubleshooting by error code

The last log line reads `ERROR <CODE>: <detail>`; the workflow uploads `debug/`
(screenshot + page HTML, 3 day retention) and posts `<CODE>` to `ALERT_WEBHOOK_URL`.

| Code | Meaning | What to do |
| --- | --- | --- |
| `SESSION_EXPIRED` | Landed on a Google sign-in / "Verify it's you" page, or the secret is empty or not valid base64 of `storage_state.json`. | Refresh the session (§5). Check the screenshot in the debug artifact. |
| `TABLE_NOT_RENDERED` | The report page loaded but neither the grid nor a "no data" message appeared within the timeout. | Open the saved link in a browser: is the report still there, same account? Google Ads UI change → check the HTML in `debug/`. Transient → re-run. |
| `DATE_RANGE_MISMATCH` | The page did not show the requested range after the URL rewrite and the date picker fallback, or rows start outside it. | Check the report link still has a *custom* date range. If the date picker layout changed, see `_set_range_via_ui` in `src/scrape.py`. |
| `ROWCOUNT_MISMATCH` | Rows captured ≠ the total the page shows. | Re-run once (slow render). If it repeats, check the CSV download in a browser; maybe the download includes extra summary rows (`parse.rows_from_csv_text`). |
| `SUPABASE_ERROR` | Non-2xx from Supabase REST; status and body are in the log. 401/403 = wrong key (must be `service_role`); 400 = column problem. | Check `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY`. Nothing was partially committed per batch, re-running is safe. |
| `PARSE_ERROR` | A cell could not be parsed (unexpected time or duration format, missing columns). | The detail names the cell. Add the format to `src/parse.py` (`TIME_FORMATS`, `parse_duration`). |
| `PAGE_LOAD_FAILED` | Navigation failed twice. | Network / Google outage; re-run. |
| `CONFIG_ERROR` | Missing secret, bad CLI dates, missing `storage_state.json`. | See the message. |

Notes:

- Dry runs print the rows to the log; real runs print counts only. Keep the Actions log
  private too.
- The job never calls GoHighLevel or any tagging webhook. Contact linking is done in the
  database.
- Tests: `python -m pytest -q`. The scraper tests run the real browser against
  `tests/fixtures/fake_report/index.html` and are skipped when Chromium is not installed.
