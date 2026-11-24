import { ImapFlow } from 'imapflow';
import { simpleParser } from 'mailparser';

const user = process.env.GMAIL_USER!;
const pass = (process.env.GMAIL_APP_PASSWORD ?? '').replace(/\s+/g, '');
if (!user || !pass) throw new Error('GMAIL_USER / GMAIL_APP_PASSWORD missing');

const client = new ImapFlow({
  host: 'imap.gmail.com',
  port: 993,
  secure: true,
  auth: { user, pass },
  logger: false,
});

await client.connect();
const lock = await client.getMailboxLock('INBOX');
try {
  const since = new Date(Date.now() - 14 * 24 * 3600 * 1000);
  const uids = (await client.search({ since, from: 'google' })) || [];
  console.log(`Messages from Google in last 14 days: ${uids.length}`);
  for (const uid of uids.slice(-10)) {
    const msg = await client.fetchOne(String(uid), { source: true, envelope: true });
    if (!msg) continue;
    const parsed = await simpleParser(msg.source);
    const links = [...(parsed.html || parsed.text || '').matchAll(/https?:\/\/[^\s"'<>]+/g)]
      .map((m) => new URL(m[0]).host)
      .filter((h, i, a) => a.indexOf(h) === i)
      .slice(0, 5);
    console.log('---');
    console.log(`date:    ${parsed.date?.toISOString()}`);
    console.log(`from:    ${parsed.from?.text}`);
    console.log(`subject: ${parsed.subject}`);
    console.log(
      `attachments: ${parsed.attachments.map((a) => `${a.filename} (${a.contentType}, ${a.size}B)`).join('; ') || 'NONE'}`,
    );
    console.log(`link hosts: ${links.join(', ') || 'none'}`);
  }
} finally {
  lock.release();
  await client.logout();
}
