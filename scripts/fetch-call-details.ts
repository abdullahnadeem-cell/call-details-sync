
import { chromium } from 'playwright-extra';
// @ts-ignore - no types shipped
import StealthPlugin from 'puppeteer-extra-plugin-stealth';
import { createClient } from '@supabase/supabase-js';
import * as XLSX from 'xlsx';
import * as OTPAuth from 'otpauth';
import { writeFile, readFile, mkdir } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import path from 'node:path';

chromium.use(StealthPlugin());

const {
  GOOGLE_STORAGE_STATE_B64,
  GOOGLE_EMAIL,
  GOOGLE_PASSWORD,
  GOOGLE_TOTP_SECRET,
  GOOGLE_ADS_REPORT_URL,
  SUPABASE_URL,
  SUPABASE_SERVICE_ROLE_KEY,
} = process.env;

// When set (e.g. http://127.0.0.1:9333), attach to an already-running Chrome that a
// person signed in to by hand, instead of launching a browser and logging in.
const CDP_URL = process.env.CHROME_CDP_URL;

const REQUIRED = {
  ...(CDP_URL ? {} : { GOOGLE_EMAIL, GOOGLE_PASSWORD, GOOGLE_TOTP_SECRET }),
  GOOGLE_ADS_REPORT_URL,
  SUPABASE_URL,
  SUPABASE_SERVICE_ROLE_KEY,
};
for (const [k, v] of Object.entries(REQUIRED)) {
  if (!v) throw new Error(`Missing required env var: ${k}`);
}

const DOWNLOAD_DIR = path.resolve('downloads');

// ---- Debug helper: screenshot + page text, uploaded as a workflow artifact ---

const dumpDebug = async (page: import('playwright').Page, label: string) => {
  try {
    await mkdir(DOWNLOAD_DIR, { recursive: true });
    const shot = path.join(DOWNLOAD_DIR, `debug-${label}.png`);
    await page.screenshot({ path: shot, fullPage: true });
    const text = (await page.locator('body').innerText().catch(() => '')).slice(0, 1500);
    console.log(`[debug:${label}] url=${page.url()}`);
    console.log(`[debug:${label}] title=${await page.title().catch(() => '')}`);
    console.log(`[debug:${label}] body=${text.replace(/\s+/g, ' ')}`);
  } catch (e) {
    console.log(`[debug:${label}] could not capture: ${String(e)}`);
  }
};

// ---- Session file hydration -------------------------------------------------

const hydrateStorageState = async (): Promise<string | undefined> => {
  if (!GOOGLE_STORAGE_STATE_B64) return undefined;
  await mkdir(DOWNLOAD_DIR, { recursive: true });
  const target = path.resolve('storage-state.json');
  await writeFile(target, Buffer.from(GOOGLE_STORAGE_STATE_B64, 'base64'));
  return target;
};

// ---- Login fallback (TOTP) --------------------------------------------------

const loginWithPassword = async (page: import('playwright').Page) => {
  console.log('Session expired. Logging in fresh with email + password + TOTP.');
  await page.goto(
    'https://accounts.google.com/signin/v2/identifier?hl=en&flowName=GlifWebSignIn&flowEntry=ServiceLogin',
    { waitUntil: 'domcontentloaded' },
  );
  await page.waitForTimeout(2_000);
  await dumpDebug(page, '1-login-page');

  const emailSel = 'input[name="identifier"], #identifierId, input[type="email"], input[autocomplete="username"]';
  const emailField = page.locator(emailSel).first();
  await emailField.waitFor({ state: 'visible', timeout: 30_000 });
  await emailField.fill(GOOGLE_EMAIL!);
  const emailNext = page.locator('#identifierNext button, #identifierNext, button:has-text("Next")').first();
  if (await emailNext.isVisible().catch(() => false)) {
    await emailNext.click();
  } else {
    await page.keyboard.press('Enter');
  }

  await page.waitForTimeout(2_000);
  await dumpDebug(page, '2-after-email');

  const passwordSel = 'input[name="Passwd"], input[name="password"], input[type="password"]';
  const passwordField = page.locator(passwordSel).first();
  await passwordField.waitFor({ state: 'visible', timeout: 30_000 });
  // Google animates the password field in; give it a beat.
  await page.waitForTimeout(1_500);
  await passwordField.fill(GOOGLE_PASSWORD!);
  const passwordNext = page.locator('#passwordNext button, #passwordNext, button:has-text("Next")').first();
  if (await passwordNext.isVisible().catch(() => false)) {
    await passwordNext.click();
  } else {
    await page.keyboard.press('Enter');
  }
  await page.waitForTimeout(3_000);
  await dumpDebug(page, '3-after-password');

  // Wait for either the TOTP challenge or a successful landing page.
  try {
    await page.waitForSelector('input[name="totpPin"], input[type="tel"]', { timeout: 15_000 });
    const totp = new OTPAuth.TOTP({
      issuer: 'Google',
      label: GOOGLE_EMAIL!,
      algorithm: 'SHA1',
      digits: 6,
      period: 30,
      secret: OTPAuth.Secret.fromBase32(GOOGLE_TOTP_SECRET!.replace(/\s+/g, '')),
    });
    const code = totp.generate();
    const totpField = page.locator('input[name="totpPin"], input[type="tel"]').first();
    await totpField.fill(code);
    await page.keyboard.press('Enter');
  } catch {
    // No TOTP prompt — already logged in or Google chose a different challenge.
  }
  await page.waitForTimeout(2_000);
  await dumpDebug(page, '4-after-totp');

  // Deal with "Don't ask on this device" / consent screens if they show up.
  const notNow = page.locator('button:has-text("Not now")');
  if (await notNow.first().isVisible().catch(() => false)) {
    await notNow.first().click();
  }
};

// ---- Download & parse -------------------------------------------------------

type CallRow = {
  start_time: string;
  duration_seconds: number | null;
  caller_country_code: string | null;
  caller_area_code: string | null;
  caller_phone_number: string | null;
  status: string | null;
  call_source: string | null;
  call_type: string | null;
  campaign: string | null;
  ad_group: string | null;
};

// Google Ads column headers vary by locale. Map whatever we find to our schema.
const normalizeHeader = (h: string): keyof CallRow | null => {
  const key = h.trim().toLowerCase();
  if (key.includes('start time') || key === 'date') return 'start_time';
  if (key.includes('duration')) return 'duration_seconds';
  if (key.includes('country code')) return 'caller_country_code';
  if (key.includes('area code')) return 'caller_area_code';
  if (key.includes('phone number')) return 'caller_phone_number';
  if (key === 'status') return 'status';
  if (key.includes('call source') || key === 'source') return 'call_source';
  if (key.includes('call type') || key === 'type') return 'call_type';
  if (key === 'campaign') return 'campaign';
  if (key === 'ad group') return 'ad_group';
  return null;
};

const parseXlsx = (buf: Buffer): CallRow[] => {
  const wb = XLSX.read(buf, { type: 'buffer', cellDates: true });
  const sheet = wb.Sheets[wb.SheetNames[0]];
  const raw = XLSX.utils.sheet_to_json<Record<string, unknown>>(sheet, { defval: null });

  return raw
    .map((r) => {
      const out: Partial<CallRow> = {};
      for (const [header, value] of Object.entries(r)) {
        const col = normalizeHeader(header);
        if (!col) continue;
        if (col === 'start_time' && value) {
          out.start_time = value instanceof Date
            ? value.toISOString()
            : new Date(String(value)).toISOString();
        } else if (col === 'duration_seconds') {
          out.duration_seconds = value == null ? null : Number(value);
        } else {
          (out as any)[col] = value == null ? null : String(value).trim();
        }
      }
      return out as CallRow;
    })
    .filter((r) => r.start_time);
};

// ---- Supabase ---------------------------------------------------------------

const upsertRows = async (rows: CallRow[]) => {
  if (rows.length === 0) {
    console.log('No rows to upsert.');
    return;
  }
  const supabase = createClient(SUPABASE_URL!, SUPABASE_SERVICE_ROLE_KEY!, {
    auth: { persistSession: false },
  });
  const { data, error } = await supabase
    .from('google_ads_call_details')
    .upsert(rows, { onConflict: 'start_time,caller_phone_number', ignoreDuplicates: false })
    .select('id');
  if (error) throw error;
  console.log(`Upserted ${data?.length ?? 0} rows into google_ads_call_details.`);
};

// ---- Main -------------------------------------------------------------------

let activePage: import('playwright').Page | null = null;

const run = async () => {
  await mkdir(DOWNLOAD_DIR, { recursive: true });

  let browser: import('playwright').Browser;
  let context: import('playwright').BrowserContext;
  if (CDP_URL) {
    const { chromium: plainChromium } = await import('playwright');
    browser = await plainChromium.connectOverCDP(CDP_URL);
    context = browser.contexts()[0];
  } else {
    const storageStatePath = await hydrateStorageState();
    browser = await chromium.launch({
      headless: process.env.HEADLESS !== 'false',
      args: ['--disable-blink-features=AutomationControlled'],
    });
    context = await browser.newContext({
      storageState: storageStatePath && existsSync(storageStatePath) ? storageStatePath : undefined,
      acceptDownloads: true,
      viewport: { width: 1440, height: 900 },
      userAgent:
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ' +
        '(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36',
    });
  }

  const page = await context.newPage();
  activePage = page;

  // Try to open the saved report. If we land on a login screen, re-auth.
  await page.goto(GOOGLE_ADS_REPORT_URL!, { waitUntil: 'domcontentloaded' });
  if (/accounts\.google\.com/.test(page.url())) {
    if (CDP_URL) {
      throw new Error(
        'The dedicated Chrome profile is signed out of Google. Run "scripts/chrome.sh signin", ' +
        'sign in by hand, then run "scripts/chrome.sh start".',
      );
    }
    await loginWithPassword(page);
    await page.goto(GOOGLE_ADS_REPORT_URL!, { waitUntil: 'domcontentloaded' });
  }

  // Let the report table render.
  await page.waitForLoadState('networkidle', { timeout: 60_000 });
  await page.waitForTimeout(3_000);

  // Trigger download: click Download button, then the Excel (.xlsx) option.
  const [download] = await Promise.all([
    page.waitForEvent('download', { timeout: 60_000 }),
    (async () => {
      // "Download" in the header. Role=button is the most stable selector across locales.
      await page.getByRole('button', { name: /download/i }).first().click();
      // Menu item for Excel .xlsx.
      await page.getByRole('menuitem', { name: /excel|\.xlsx/i }).first().click();
    })(),
  ]);

  const dest = path.join(DOWNLOAD_DIR, `call-details-${Date.now()}.xlsx`);
  await download.saveAs(dest);
  console.log(`Downloaded: ${dest}`);

  if (CDP_URL) {
    // Leave the person's Chrome running; only close our tab and disconnect.
    await page.close();
  } else {
    // Refresh the storage state so future runs skip the login.
    await context.storageState({ path: 'storage-state.json' });
  }
  await browser.close();

  const buf = await readFile(dest);
  const rows = parseXlsx(buf);
  console.log(`Parsed ${rows.length} rows from xlsx.`);

  await upsertRows(rows);
};

run().catch(async (err) => {
  console.error('FATAL:', err);
  if (activePage) await dumpDebug(activePage, 'fatal');
  process.exit(1);
});
