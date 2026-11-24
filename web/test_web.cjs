#!/usr/bin/env node
// End-to-end tests for web/index.html and the HTML that ig_followback.py writes.
//
//   node web/test_web.cjs
//
// Needs Node 18+, Python 3, and Playwright with Chromium
// (npm i -g playwright && npx playwright install chromium).
// Builds its own fixture exports, so no personal data is involved.
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const zlib = require('node:zlib');
const { execFileSync } = require('node:child_process');
const { chromium } = require('playwright');

const ROOT = path.resolve(__dirname, '..');
const PAGE = 'file://' + path.join(__dirname, 'index.html');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'followback-web-'));

// ---------------------------------------------------------------- fixtures --

// Minimal zip writer: enough to produce archives like Instagram's.
function crc32(buf) {
  let c, crc = 0xffffffff;
  for (const b of buf) {
    c = (crc ^ b) & 0xff;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    crc = (crc >>> 8) ^ c;
  }
  return (crc ^ 0xffffffff) >>> 0;
}
function makeZip(files, { store = false } = {}) {
  const locals = [], centrals = [];
  let offset = 0;
  for (const [name, text] of Object.entries(files)) {
    const raw = Buffer.from(text), nameBuf = Buffer.from(name);
    const data = store ? raw : zlib.deflateRawSync(raw);
    const method = store ? 0 : 8, crc = crc32(raw);
    const lh = Buffer.alloc(30);
    lh.writeUInt32LE(0x04034b50, 0); lh.writeUInt16LE(20, 4); lh.writeUInt16LE(0x0800, 6);
    lh.writeUInt16LE(method, 8); lh.writeUInt32LE(crc, 14); lh.writeUInt32LE(data.length, 18);
    lh.writeUInt32LE(raw.length, 22); lh.writeUInt16LE(nameBuf.length, 26);
    const ch = Buffer.alloc(46);
    ch.writeUInt32LE(0x02014b50, 0); ch.writeUInt16LE(20, 4); ch.writeUInt16LE(20, 6); ch.writeUInt16LE(0x0800, 8);
    ch.writeUInt16LE(method, 10); ch.writeUInt32LE(crc, 16); ch.writeUInt32LE(data.length, 20);
    ch.writeUInt32LE(raw.length, 24); ch.writeUInt16LE(nameBuf.length, 28); ch.writeUInt32LE(offset, 42);
    locals.push(lh, nameBuf, data);
    centrals.push(ch, nameBuf);
    offset += 30 + nameBuf.length + data.length;
  }
  const cd = Buffer.concat(centrals);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0); end.writeUInt16LE(Object.keys(files).length, 8);
  end.writeUInt16LE(Object.keys(files).length, 10); end.writeUInt32LE(cd.length, 12); end.writeUInt32LE(offset, 16);
  return Buffer.concat([...locals, cd, end]);
}

// The shapes real exports use: following has the username in "title" and an
// /_u/ deeplink; followers have it in "value"; pending uses label_values.
const followingBlock = (u, ts) => ({ title: u, string_list_data: [{ href: `https://www.instagram.com/_u/${u}`, timestamp: ts }] });
const followerBlock = (u, ts) => ({ title: '', string_list_data: [{ href: `https://www.instagram.com/${u}`, value: u, timestamp: ts }] });
const DIR = 'instagram-tester-2026-11-01-abc/connections/followers_and_following/';

function exportFiles({ following, followers1, followers2 = [], extra = {} }) {
  return {
    'instagram-tester-2026-11-01-abc/messages/inbox.json': '{}',
    [DIR + 'following.json']: JSON.stringify({ relationships_following: following }),
    [DIR + 'followers_1.json']: JSON.stringify(followers1),
    ...(followers2.length ? { [DIR + 'followers_2.json']: JSON.stringify(followers2) } : {}),
    ...Object.fromEntries(Object.entries(extra).map(([k, v]) => [DIR + k, JSON.stringify(v)])),
  };
}

const NEW = exportFiles({
  following: [followingBlock('alice', 1600000000), followingBlock('Bob', 1600000100),
              followingBlock('ghost', 1500000000), followingBlock('bestie', 1700000000),
              followingBlock('brand', 1650000000)],
  followers1: [followerBlock('alice', 1600000000), followerBlock('bob', 1600000200)],
  followers2: [followerBlock('superfan', 1760000000)],
  extra: {
    'pending_follow_requests.json': { relationships_follow_requests_sent: [
      { timestamp: 1750000000, label_values: [{ label: 'Name', value: 'Private Person' }, { label: 'Username', value: 'private.acct' }] }] },
    'close_friends.json': { relationships_close_friends: [followerBlock('bestie', 1700000000)] },
    'recently_unfollowed_profiles.json': { relationships_unfollowed_users: [followingBlock('old.crush', 1755000000)] },
  },
});
const OLD = exportFiles({
  following: [followingBlock('alice', 1600000000), followingBlock('Bob', 1600000100)],
  followers1: [followerBlock('alice', 1600000000), followerBlock('bob', 1600000200), followerBlock('leaver', 1550000000)],
});

const write = (name, buf) => { const p = path.join(tmp, name); fs.writeFileSync(p, buf); return p; };
const newZip = write('instagram-new.zip', makeZip(NEW));
const storedZip = write('instagram-stored.zip', makeZip(NEW, { store: true }));
const oldZip = write('instagram-old.zip', makeZip(OLD));
const htmlZip = write('instagram-html.zip', makeZip({ [DIR + 'following.html']: '<html></html>' }));
const looseDir = path.join(tmp, 'loose');
fs.mkdirSync(looseDir);
const looseFiles = Object.entries(NEW).filter(([n]) => n.startsWith(DIR)).map(([n, text]) => {
  const p = path.join(looseDir, path.basename(n)); fs.writeFileSync(p, text); return p;
});

// Run the Python tool on the same export, for parity.
function python(args, cwd) {
  return execFileSync('python3', [path.join(ROOT, 'ig_followback.py'), ...args], { cwd, encoding: 'utf8' });
}
const pyRun = path.join(tmp, 'py');
fs.mkdirSync(pyRun);
python([newZip, '--since', oldZip], pyRun);
const pyList = f => fs.readFileSync(path.join(pyRun, f), 'utf8').split('\n').filter(Boolean).sort();

// ------------------------------------------------------------------- tests --

const results = [];
async function test(name, fn) {
  try { await fn(); results.push(['ok', name]); }
  catch (err) { results.push(['FAIL', name, err]); }
}

(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({ acceptDownloads: true });
  const requests = [];
  context.on('request', r => { if (!r.url().startsWith('file:')) requests.push(r.url()); });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('dialog', d => d.accept());  // confirms and alerts

  const names = () => page.$$eval('#rows .row a.u', as => as.map(a => a.textContent).sort());
  const tab = title => page.click(`#tabs .tab:has-text("${title}")`);
  const open = async files => {
    await page.goto(PAGE);
    await page.setInputFiles('#file', files);
    await page.waitForSelector('#results:not([hidden]), #error:not([hidden])');
  };

  await test('reads a deflated export zip and matches the Python tool', async () => {
    await open(newZip);
    assert.deepEqual(await names(), pyList('not_following_back.txt'));
    assert.deepEqual(await names(), ['bestie', 'brand', 'ghost']);
    await tab("You don't follow back");
    assert.deepEqual(await names(), pyList('you_dont_follow_back.txt'));
    await tab('Pending requests');
    assert.deepEqual(await names(), pyList('pending_requests.txt'));
    await tab('Recently unfollowed');
    assert.deepEqual(await names(), pyList('recently_unfollowed.txt'));
  });

  await test('sorts oldest follow first and tags close friends', async () => {
    await open(newZip);
    const order = await page.$$eval('#rows .row a.u', as => as.map(a => a.textContent));
    assert.deepEqual(order, ['ghost', 'brand', 'bestie']);
    const tagged = await page.$$eval('#rows .row', rs => rs.filter(r => r.querySelector('.tag')).map(r => r.querySelector('a.u').textContent));
    assert.deepEqual(tagged, ['bestie']);
  });

  await test('reads a stored (uncompressed) zip', async () => {
    await open(storedZip);
    assert.deepEqual(await names(), ['bestie', 'brand', 'ghost']);
  });

  await test('reads loose .json files from an unzipped export', async () => {
    await open(looseFiles);
    assert.deepEqual(await names(), ['bestie', 'brand', 'ghost']);
  });

  await test('names an HTML-format export instead of failing vaguely', async () => {
    await open(htmlZip);
    assert.match(await page.textContent('#error'), /Format: JSON/);
  });

  await test('keep hides an account, survives a reload, and is undoable', async () => {
    await open(newZip);
    await page.click('#rows .row:has(a.u:text-is("brand")) button.keep');
    assert.deepEqual(await names(), ['bestie', 'ghost']);
    await open(newZip);
    assert.deepEqual(await names(), ['bestie', 'ghost'], 'mark kept across reloads');
    await page.check('#showHidden');
    assert.deepEqual(await names(), ['bestie', 'brand', 'ghost']);
    await page.click('#rows .row:has(a.u:text-is("brand")) button.keep');
    assert.deepEqual(await names(), ['bestie', 'brand', 'ghost']);
    assert.equal(await page.isChecked('#showHidden'), false, 'toggle resets once nothing is hidden');
    await page.click('#rows .row:has(a.u:text-is("ghost")) button.dead');
    assert.deepEqual(await names(), ['bestie', 'brand'], 'a new mark hides again');
    await page.click('#clearMarks');
  });

  await test('saved ignore.txt is read by the Python tool', async () => {
    await open(newZip);
    await page.click('#rows .row:has(a.u:text-is("brand")) button.keep');
    await page.click('#rows .row:has(a.u:text-is("ghost")) button.dead');
    const [dl] = await Promise.all([page.waitForEvent('download'), page.click('#saveIgnore')]);
    const text = fs.readFileSync(await dl.path(), 'utf8');
    assert.match(text, /# Dead[^\n]*\nghost/);
    assert.match(text, /# Keep[^\n]*\nbrand/);
    const run = fs.mkdtempSync(path.join(tmp, 'ign-'));
    fs.writeFileSync(path.join(run, 'ignore.txt'), text);
    python([newZip], run);
    assert.equal(fs.readFileSync(path.join(run, 'not_following_back.txt'), 'utf8'), 'bestie');
    await page.click('#clearMarks');
  });

  await test('importing ignore.txt restores marks', async () => {
    await open(newZip);
    await page.click('#clearMarks');
    const f = write('ignore.txt', '# mine\n@Ghost\n\n# Keep: following on purpose\nbrand\n');
    await page.setInputFiles('#ignorefile', f);
    await page.waitForFunction(() => document.querySelectorAll('#rows .row').length === 1);
    assert.deepEqual(await names(), ['bestie']);
    await page.click('#clearMarks');
  });

  await test('comparing exports matches the Python tool', async () => {
    await open(newZip);
    await page.setInputFiles('#oldfile', oldZip);
    await page.waitForSelector('#tabs .tab:has-text("Stopped following you")');
    await tab('Stopped following you');
    assert.deepEqual(await names(), pyList('unfollowed_you.txt'));
    assert.deepEqual(await names(), ['leaver']);
    await tab('New followers');
    assert.deepEqual(await names(), pyList('new_followers.txt'));
    assert.equal(await page.isVisible('#warn'), false);
  });

  await test('swapped exports are detected and can be swapped back', async () => {
    await open(oldZip);
    await page.setInputFiles('#oldfile', newZip);
    await page.waitForSelector('#warn:not([hidden])');
    assert.match(await page.textContent('#warn'), /swapped/);
    await page.click('#warn button');
    await tab('Stopped following you');
    assert.deepEqual(await names(), ['leaver']);
  });

  await test('CSV download includes marks and close friends', async () => {
    await open(newZip);
    const [dl] = await Promise.all([page.waitForEvent('download'), page.click('#csv')]);
    const csv = fs.readFileSync(await dl.path(), 'utf8');
    assert.match(csv, /^username,profile_url,followed_on,close_friend,mark\n/);
    assert.match(csv, /bestie,https:\/\/instagram\.com\/bestie,2023-11-14,yes,/);
  });

  await test('phone width has no horizontal scroll', async () => {
    await page.setViewportSize({ width: 360, height: 740 });
    await open(newZip);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    assert.ok(overflow <= 0, `page overflows by ${overflow}px`);
    await page.setViewportSize({ width: 1280, height: 800 });
  });

  // The page the Python tool writes: keep + save must merge with the existing ignore file.
  await test('cleanup.html save carries over the existing ignore list', async () => {
    const run = fs.mkdtempSync(path.join(tmp, 'merge-'));
    fs.writeFileSync(path.join(run, 'ignore.txt'), 'ghost\n');
    python([newZip], run);
    await page.goto('file://' + path.join(run, 'cleanup.html'));
    await page.click('tr[data-username="brand"] button.keep');
    assert.equal(await page.getAttribute('tr[data-username="brand"]', 'class'), 'kept');
    await page.fill('#q', 'BES');
    assert.equal(await page.isVisible('tr[data-username="bestie"]'), true, 'filter is case-insensitive');
    const [dl] = await Promise.all([page.waitForEvent('download'), page.click('#save')]);
    const text = fs.readFileSync(await dl.path(), 'utf8');
    assert.match(text, /# Already in your ignore list\nghost/);
    assert.match(text, /# Keep[^\n]*\nbrand/);
  });

  await test('the page makes no network requests and throws no errors', async () => {
    assert.deepEqual(requests, []);
    assert.deepEqual(errors, []);
  });

  await browser.close();
  fs.rmSync(tmp, { recursive: true, force: true });
  for (const [status, name, err] of results) {
    console.log(`${status === 'ok' ? 'ok  ' : 'FAIL'} ${name}`);
    if (err) console.log('     ' + String(err.stack || err).split('\n').slice(0, 6).join('\n     '));
  }
  const failed = results.filter(r => r[0] !== 'ok').length;
  console.log(`\n${results.length - failed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
