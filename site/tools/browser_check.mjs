// Loads the page in headless Chromium over a local DevTools port and reports problems. Nothing leaves 127.0.0.1.
// Three runs, each on a temporary copy of this folder:
//   sample    - served over http://127.0.0.1 with testdata/ copied into data/ (the fictional sample)
//   real      - served over http://127.0.0.1 as deployed (data/ is the real export, testdata/ left out)
//   real-file - the real copy opened over file://, where fetch is unavailable and the fallback is null:
//               the page must show that the feed could not be loaded, not throw and not show any data
// Both served runs also open the recent view at desktop and phone width: rows ordered by first seen,
// newest first, the first seen note shown, and the First seen heading flips the order.
// Both served runs also check the controls: the reset control is hidden at the defaults of the current view, shows
// after a filter or a sort change and puts them back (view, pause kept), the recent and games views open with their
// own sorts, and at 360px wide, with everything open, nothing scrolls sideways.
// Both served runs also type a search that matches nothing in each list view at desktop and phone width: a link to
// GitHub's own repository search appears under the empty message, and goes away for a matching search, a cleared
// search and the About view.
//   no-events - the real copy with its events emptied, as after a first collection: the feed view says
//               there are no events yet and the table view still lists the repositories
//   aliases   - the sample with the game fields of the alias list added (game_key, game_basis, game_confidence,
//               matched_alias): the games view keys rows by game_key, counts low-confidence names under
//               "unreviewed names (hidden)" until the low-confidence toggle shows them, labelled low
// Usage: node tools/browser_check.mjs <chromium-binary> [--no-sandbox]
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { mkdtempSync, readFileSync, writeFileSync, mkdirSync, existsSync, rmSync, cpSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve, dirname, extname, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const shots = join(root, 'shots');
const [bin, ...flags] = process.argv.slice(2);
mkdirSync(shots, { recursive: true });
const profile = mkdtempSync(join(tmpdir(), 'ps3rc-check-'));

// temporary site copies
const work = mkdtempSync(join(tmpdir(), 'ps3rc-site-'));
const skip = (src) => !src.startsWith(join(root, 'shots')) && !src.startsWith(join(root, 'testdata'));
const sampleDir = join(work, 'sample');
const realDir = join(work, 'real');
cpSync(root, sampleDir, { recursive: true, filter: skip });
rmSync(join(sampleDir, 'data'), { recursive: true, force: true });
cpSync(join(root, 'testdata'), join(sampleDir, 'data'), { recursive: true });
cpSync(root, realDir, { recursive: true, filter: skip });
const noEventsDir = join(work, 'no-events');
cpSync(realDir, noEventsDir, { recursive: true });
{
  const f = JSON.parse(readFileSync(join(noEventsDir, 'data', 'feed.json'), 'utf8'));
  f.events = [];
  writeFileSync(join(noEventsDir, 'data', 'feed.json'), JSON.stringify(f));
}

// the sample with the alias list's game fields; Sample Racer is spelled two ways under one key
const aliasDir = join(work, 'aliases');
cpSync(sampleDir, aliasDir, { recursive: true });
{
  const f = JSON.parse(readFileSync(join(aliasDir, 'data', 'feed.json'), 'utf8'));
  const set = {
    r1: ['example-game', 'alias_name', 'strong', 'example game'],
    r7: ['example-game', 'inherited_from_parent', 'strong', 'example game'],
    r2: ['sample-racer', 'alias_readme_title', 'medium', 'sample racer'],
    r12: ['sample-racer', 'alias_description', 'medium', 'sample racer', 'SAMPLE RACER'],
    r9: ['placeholder-tactics', 'readme_title', 'low', null],
    r10: ['test-arena', null, 'weak', null],
  };
  for (const r of f.repos) {
    const v = set[r.id] || [null, null, 'unknown', null];
    Object.assign(r, { game_key: v[0], game_basis: v[1], game_confidence: v[2], matched_alias: v[3] }, v[4] ? { game: v[4] } : {});
  }
  writeFileSync(join(aliasDir, 'data', 'feed.json'), JSON.stringify(f));
}

const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.json': 'application/json', '.svg': 'image/svg+xml', '.woff2': 'font/woff2', '.woff': 'font/woff', '.txt': 'text/plain; charset=utf-8' };
function serve(dir) {
  const srv = createServer((req, res) => {
    const path = resolve(dir, '.' + decodeURIComponent(new URL(req.url, 'http://x').pathname));
    const file = path === dir || path === dir + sep ? join(dir, 'index.html') : path;
    if (!file.startsWith(dir + sep) || !existsSync(file) || !statSync(file).isFile()) {
      res.writeHead(404).end();
      return;
    }
    res.writeHead(200, { 'content-type': TYPES[extname(file)] || 'application/octet-stream' }).end(readFileSync(file));
  });
  return new Promise((r) => srv.listen(0, '127.0.0.1', () => r({ srv, origin: 'http://127.0.0.1:' + srv.address().port })));
}
const sampleSrv = await serve(sampleDir);
const realSrv = await serve(realDir);
const noEventsSrv = await serve(noEventsDir);
const aliasSrv = await serve(aliasDir);
const chrome = spawn(bin, ['--headless=new', '--disable-gpu', '--hide-scrollbars', '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank', ...flags], { stdio: 'ignore' });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function wsUrl() {
  const f = join(profile, 'DevToolsActivePort');
  for (let i = 0; i < 100; i++) {
    if (existsSync(f)) {
      const [port] = readFileSync(f, 'utf8').split('\n');
      const v = await (await fetch('http://127.0.0.1:' + port + '/json/version')).json();
      return v.webSocketDebuggerUrl;
    }
    await sleep(100);
  }
  throw new Error('browser did not start');
}

const ws = new WebSocket(await wsUrl());
await new Promise((r) => (ws.onopen = r));
let id = 0;
const pending = new Map();
const handlers = [];
ws.onmessage = (m) => {
  const msg = JSON.parse(m.data);
  if (msg.id && pending.has(msg.id)) {
    const { res, rej } = pending.get(msg.id);
    pending.delete(msg.id);
    msg.error ? rej(new Error(msg.error.message)) : res(msg.result);
  } else handlers.forEach((h) => h(msg));
};
const send = (method, params = {}, sessionId) => new Promise((res, rej) => {
  pending.set(++id, { res, rej });
  ws.send(JSON.stringify({ id, method, params, sessionId }));
});

const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
const cmd = (m, p) => send(m, p, sessionId);
const evalJs = async (expression) => (await cmd('Runtime.evaluate', { expression, returnByValue: true })).result.value;

const problems = [];
const requests = [];
handlers.push((m) => {
  if (m.sessionId !== sessionId) return;
  const p = m.params;
  if (m.method === 'Runtime.exceptionThrown') problems.push('exception: ' + (p.exceptionDetails.exception?.description || p.exceptionDetails.text));
  if (m.method === 'Runtime.consoleAPICalled' && ['error', 'warning', 'assert'].includes(p.type)) problems.push('console.' + p.type + ': ' + p.args.map((a) => a.value ?? a.description).join(' '));
  if (m.method === 'Log.entryAdded' && ['error', 'warning'].includes(p.entry.level)) problems.push('log ' + p.entry.level + ' (' + p.entry.source + '): ' + p.entry.text + ' ' + (p.entry.url || ''));
  if (m.method === 'Network.requestWillBeSent') requests.push(p.request.url);
  if (m.method === 'Network.loadingFailed') problems.push('request failed: ' + p.errorText + ' ' + (p.blockedReason || '') + ' id ' + p.requestId);
  if (m.method === 'Network.responseReceived' && p.response.status >= 400) problems.push('HTTP ' + p.response.status + ' ' + p.response.url);
});
for (const d of ['Runtime', 'Log', 'Network', 'Page']) await cmd(d + '.enable');

async function typeLine(text) {
  await evalJs("document.getElementById('cmd').focus()");
  await cmd('Input.insertText', { text });
  for (const type of ['keyDown', 'keyUp']) await cmd('Input.dispatchKeyEvent', { type, key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, text: type === 'keyDown' ? '\r' : undefined });
  await sleep(300);
}
async function typeOnly(text) {
  await evalJs("document.getElementById('cmd').focus()");
  await cmd('Input.insertText', { text });
  await sleep(300);
}
const rows = () => evalJs("[...document.querySelectorAll('#view .row')].map(b => b.dataset.id + ':' + b.textContent)");

const fails = [];
const check = (cond, what) => { if (!cond) fails.push(what); return cond; };
const realFeed = JSON.parse(readFileSync(join(root, 'data', 'feed.json'), 'utf8'));
const realTimes = realFeed.events.map((e) => Date.parse(e.source_at || e.observed_at));
const allowedTimes = new Set(realTimes.flatMap((t) => { const d = new Date(t).toISOString(); return ['[' + d.slice(11, 16) + ']', '[' + d.slice(5, 10) + ' ' + d.slice(11, 16) + ']']; }));
// search word for the real feed: the owner with the most events that does not own every event
const ownerOf = new Map(realFeed.repos.map((r) => [r.id, r.owner]));
const counts = new Map();
realFeed.events.forEach((e) => counts.set(ownerOf.get(e.repo_id), (counts.get(ownerOf.get(e.repo_id)) || 0) + 1));
const realSearch = [...counts].filter(([o, n]) => o && n < realFeed.events.length).sort((a, b) => b[1] - a[1])[0][0];

const report = {};
const runs = [
  ['sample', sampleSrv.origin + '/index.html', 'demo-user'],
  ['real', realSrv.origin + '/index.html', realSearch],
];
for (const [kind, page, search] of runs) {
  for (const [vp, w, h, mobile] of [['desktop-1280x800', 1280, 800, false], ['phone-390x844', 390, 844, true]]) {
    const name = kind + '-' + vp;
    await cmd('Emulation.setDeviceMetricsOverride', { width: w, height: h, deviceScaleFactor: 1, mobile });
    await cmd('Page.navigate', { url: page });
    await sleep(1500);
    const r = { scrollWidth: await evalJs('document.documentElement.scrollWidth'), innerWidth: await evalJs('window.innerWidth') };
    r.horizontalScroll = r.scrollWidth > r.innerWidth;
    check(!r.horizontalScroll, name + ': horizontal scroll at start');
    r.rowsAtStart = (await rows()).length;
    check(r.rowsAtStart > 0, name + ': no rows');
    r.statusline = await evalJs("document.getElementById('statusline').textContent");
    r.badge = await evalJs("document.querySelector('.badge').textContent.slice(0, 60)");
    const shot = async (suffix) => writeFileSync(join(shots, name + suffix + '.png'), Buffer.from((await cmd('Page.captureScreenshot', { format: 'png' })).data, 'base64'));
    await shot('');
    // the notice and the About view are built from the served feed, not fixed text
    {
      const served = JSON.parse(readFileSync(join(kind === 'sample' ? sampleDir : realDir, 'data', 'feed.json'), 'utf8'));
      const iso = new Date(Date.parse(served.generated_at)).toISOString();
      const when = 'Last collected: ' + iso.slice(0, 10) + ' ' + iso.slice(11, 16) + ' UTC.';
      r.notice = await evalJs("document.getElementById('notice').textContent");
      await typeLine('about');
      r.aboutNotice = await evalJs("(document.getElementById('about-notice') || {}).textContent || null");
      r.aboutText = await evalJs("document.getElementById('view').textContent");
      await shot('-about');
      await typeLine('view feed');
      for (const [what, text] of [['notice', r.notice], ['About', r.aboutText]]) {
        check(typeof text === 'string' && text.includes(when), name + ': ' + what + ' does not state the collection time ' + when);
        check(!/by hand|three read-only|collection runs/i.test(text || ''), name + ': ' + what + ' has fixed wording about how the data was published');
        check(/not real time/i.test(text || '') && /No game files/.test(text || '') && /inferred and may be wrong/.test(text || ''), name + ': ' + what + ' lacks a required sentence');
      }
      check(r.aboutNotice === r.notice, name + ': About notice differs from the badge notice');
      if (kind === 'sample') check(r.notice.startsWith('SAMPLE (fictional): '), name + ': sample notice is not labelled');
      else check(!/SAMPLE/.test(r.notice) && r.notice.includes('Repository metadata comes from GitHub.'), name + ': real notice');
      if (!served.collection) check(r.notice.includes('The collection state is unknown.') && !/was complete|was incomplete/.test(r.notice), name + ': feed has no collection object but the notice claims a state');
    }
    {
      // controls: one row of views, the rest under a closed "Filters and options"; the reset control is hidden at the
      // defaults of the current view, appears after a filter or a sort change, and puts them back without touching
      // the view, pause or the untouched forks setting
      const ui = () => evalJs(`(() => {
        const b = document.getElementById('reset-view');
        return {
          resetHidden: b.hidden && b.offsetParent === null, optionsOpen: document.getElementById('options').open,
          statusOpen: document.getElementById('status').open, count: document.getElementById('optcount').hidden ? '' : document.getElementById('optcount').textContent,
          active: document.getElementById('active').textContent, view: document.querySelector('#views [aria-current="page"]').textContent,
          paused: /resume/.test(document.getElementById('toggles').textContent), rows: document.querySelectorAll('#view .row').length,
        };
      })()`);
      const clickChip = (group, text) => evalJs(`[...document.querySelectorAll('#${group} button')].find((b) => b.textContent === ${JSON.stringify(text)}).click()`);
      const clickReset = async () => { await evalJs("document.getElementById('reset-view').click()"); await sleep(200); };
      const start = await ui();
      r.controls = { start };
      check(start.resetHidden && !start.optionsOpen && !start.statusOpen && start.count === '' && /^filters: none \| sort: -time \| group: none \| \d+ of \d+ shown/.test(start.active), name + ': controls at start ' + JSON.stringify(start));
      await evalJs("document.querySelector('#options summary').click()");
      await clickChip('filter-chips', 'category:framework');
      await sleep(200);
      const filtered = await ui();
      check(!filtered.resetHidden && filtered.count === '1 active' && /filters: category:framework/.test(filtered.active), name + ': reset not shown after a filter ' + JSON.stringify(filtered));
      await clickChip('toggles', 'pause');
      await clickReset();
      const afterFilter = await ui();
      r.controls.afterFilterReset = afterFilter;
      check(afterFilter.resetHidden && afterFilter.count === '' && afterFilter.rows === r.rowsAtStart && afterFilter.view === 'feed' && afterFilter.paused && /^filters: none \| sort: -time/.test(afterFilter.active), name + ': reset after a filter ' + JSON.stringify(afterFilter));
      check((await evalJs('document.activeElement.id')) === 'view', name + ': focus after reset is not on the list');
      await clickChip('toggles', 'resume');
      await clickChip('sort-chips', 'sort stars');
      await sleep(200);
      const sorted = await ui();
      check(!sorted.resetHidden && /sort: -stars/.test(sorted.active), name + ': reset not shown after a sort change ' + JSON.stringify(sorted));
      await clickReset();
      const afterSort = await ui();
      check(afterSort.resetHidden && /sort: -time/.test(afterSort.active) && afterSort.view === 'feed', name + ': reset after a sort change ' + JSON.stringify(afterSort));
      // a view's own sort: recent opens by first seen, and games then opens by repository count, not first seen
      await typeLine('view recent');
      const recent = await ui();
      await typeLine('view games');
      const games = await ui();
      r.controls.viewSorts = [recent.active, games.active];
      check(recent.resetHidden && /sort: -first-seen/.test(recent.active) && games.resetHidden && /sort: -repos/.test(games.active), name + ': view default sorts ' + JSON.stringify(r.controls.viewSorts));
      await typeLine('sort stars');
      await clickReset();
      const gamesReset = await ui();
      check(gamesReset.resetHidden && gamesReset.view === 'games' && /sort: -repos/.test(gamesReset.active), name + ': reset in the games view ' + JSON.stringify(gamesReset));
      await typeLine('reset');
      await evalJs("document.getElementById('options').open = false");
      if (mobile) {
        // narrowest phone width, everything open: no sideways scroll and no control past the right edge
        await cmd('Emulation.setDeviceMetricsOverride', { width: 360, height: 780, deviceScaleFactor: 1, mobile: true });
        await evalJs("document.getElementById('options').open = true; document.getElementById('status').open = true");
        await typeLine('filter category:framework status:active time:<7d');
        await sleep(300);
        r.controls.narrow = await evalJs(`(() => {
          const out = [...document.querySelectorAll('.term *')].filter((e) => !e.closest('#view, #out') && e.getClientRects().length)
            .filter((e) => e.getBoundingClientRect().right > window.innerWidth + 0.5).map((e) => e.tagName + '#' + e.id + '.' + e.className);
          return { scrollWidth: document.documentElement.scrollWidth, bodyScroll: document.body.scrollWidth, innerWidth: window.innerWidth, out };
        })()`);
        const n = r.controls.narrow;
        check(n.innerWidth === 360 && n.scrollWidth <= 360 && n.bodyScroll <= 360 && n.out.length === 0, name + ': horizontal overflow at 360px ' + JSON.stringify(n));
        await shot('-360-controls');
        await typeLine('reset');
        await evalJs("document.getElementById('options').open = false; document.getElementById('status').open = false");
        await cmd('Emulation.setDeviceMetricsOverride', { width: w, height: h, deviceScaleFactor: 1, mobile });
        await sleep(300);
      }
    }
    if (kind === 'real') {
      const times = await evalJs("[...document.querySelectorAll('#view .row .time')].map(x => x.textContent)");
      const clock = new Date().toISOString().slice(11, 16);
      r.eventTimesShown = [...new Set(times)];
      r.eventTimesAreCollected = times.length > 0 && times.every((t) => allowedTimes.has(t));
      r.viewerClock = clock;
      check(r.eventTimesAreCollected, name + ': an event time is not a collected time: ' + r.eventTimesShown.join(' '));
      r.noSampleText = !(await evalJs("/SAMPLE DATA|fictional \\(example|design preview/i.test(document.body.innerText)"));
      check(r.noSampleText, name + ': sample wording on the real page');
    }
    r.search = search;
    await typeOnly(search);
    r.liveSearchRows = (await rows()).length;
    r.liveSearchNarrowed = r.liveSearchRows > 0 && r.liveSearchRows < r.rowsAtStart;
    check(r.liveSearchNarrowed, name + ': live search did not narrow');
    await shot('-live-search');
    for (const type of ['keyDown', 'keyUp']) await cmd('Input.dispatchKeyEvent', { type, key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
    await sleep(300);
    r.escapeRestoresRows = (await rows()).length === r.rowsAtStart;
    check(r.escapeRestoresRows, name + ': Esc did not restore rows');
    await evalJs("document.getElementById('cmd').value = ''");
    {
      // a search that matches nothing offers a plain link to GitHub's own search; a matching search, no search
      // and the About view show none
      const ghUrl = (t) => 'https://github.com/search?q=' + encodeURIComponent(t.trim()).replace(/'/g, '%27') + '&type=repositories';
      const ghNote = 'Not in this list? Our data comes from scheduled collections and may be missing repositories.';
      const ghLink = () => evalJs(`(() => {
        const a = document.querySelector('#view #github-search');
        const note = document.querySelector('#view #github-search-note');
        const empty = document.querySelector('#view p.empty');
        const box = document.getElementById('view');
        return a ? {
          href: a.getAttribute('href'), target: a.target, rel: a.rel, text: a.textContent,
          note: note ? note.textContent : null, empty: empty ? empty.textContent : null,
          order: !!(empty && note && (empty.compareDocumentPosition(note) & 4) && (note.compareDocumentPosition(a) & 4)),
          linkRight: Math.round(a.getBoundingClientRect().right), boxRight: Math.round(box.getBoundingClientRect().right),
          rows: document.querySelectorAll('#view .row').length,
        } : { none: true, rows: document.querySelectorAll('#view .row').length };
      })()`);
      const typeFresh = async (t) => {
        await evalJs("document.getElementById('cmd').value = ''");
        await typeOnly(t);
      };
      const esc = async () => {
        for (const type of ['keyDown', 'keyUp']) await cmd('Input.dispatchKeyEvent', { type, key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
        await sleep(300);
      };
      const nonsense = 'zzqx "no<match>" & 100% it\'s';
      const long = 'qzv'.repeat(30);
      r.ghLink = {};
      await typeLine('reset');
      for (const v of ['feed', 'table', 'forks', 'recent', 'games']) {
        await typeLine('view ' + v);
        await typeFresh(nonsense);
        const got = await ghLink();
        r.ghLink[v] = got;
        check(!got.none && got.rows === 0 && got.href === ghUrl(nonsense), name + ': ' + v + ' github search link href ' + JSON.stringify(got));
        check(got.target === '_blank' && /\bnoopener\b/.test(got.rel || '') && /\bnoreferrer\b/.test(got.rel || ''), name + ': ' + v + ' github search link target/rel ' + JSON.stringify(got));
        check(got.text === 'Search GitHub for "' + nonsense + '"' && got.note === ghNote && got.order && /^No rows match|^Nothing here|^No events yet/.test(got.empty || ''), name + ': ' + v + ' github search link text, note or order ' + JSON.stringify(got));
        check(got.linkRight <= got.boxRight + 1 && (await evalJs('document.documentElement.scrollWidth')) <= r.innerWidth, name + ': ' + v + ' github search link overflows');
        if (v === 'feed') await shot('-github-search');
        await esc();
        const cleared = await ghLink();
        check(cleared.none && cleared.rows + (await evalJs("document.querySelectorAll('#view tbody tr').length")) > 0, name + ': ' + v + ' clearing the search did not remove the link ' + JSON.stringify(cleared));
      }
      // long text: the link shows 60 characters and an ellipsis, the href keeps it all, and it wraps on a phone
      await typeLine('view table');
      await typeFresh(long);
      const lg = await ghLink();
      r.ghLink.long = { text: lg.text, href: lg.href, linkRight: lg.linkRight, boxRight: lg.boxRight };
      check(lg.href === ghUrl(long) && lg.text === 'Search GitHub for "' + long.slice(0, 60) + '..."', name + ': long github search link ' + JSON.stringify(r.ghLink.long));
      check(lg.linkRight <= lg.boxRight + 1 && (await evalJs('document.documentElement.scrollWidth')) <= r.innerWidth && (await evalJs("document.getElementById('view').scrollWidth <= document.getElementById('view').clientWidth")), name + ': long github search link does not wrap');
      if (mobile) await shot('-github-search-long');
      await esc();
      // a search that matches shows rows and no link
      await typeLine('view feed');
      await typeFresh(search);
      const matched = await ghLink();
      r.ghLink.matching = matched;
      check(matched.none && matched.rows > 0, name + ': matching search shows the github search link ' + JSON.stringify(matched));
      await esc();
      // the About view never shows it, even with a search that matches nothing
      await evalJs("document.getElementById('cmd').value = ''");
      await typeLine('search zzqxnomatch');
      const before = await ghLink();
      await typeLine('view about');
      const about = await ghLink();
      r.ghLink.about = about;
      check(!before.none && before.href === ghUrl('zzqxnomatch') && about.none && !(await evalJs("!!document.querySelector('#view .empty')")), name + ': about view github search link ' + JSON.stringify({ before, about }));
      await typeLine('view feed');
      await typeLine('search');
      check((await ghLink()).none, name + ': search command clearing did not remove the link');
      await typeLine('reset');
      await evalJs("document.getElementById('cmd').value = ''");
    }
    await typeLine('filter category:framework');
    const afterFilter = await rows();
    await typeLine('sort -stars');
    const afterSort = await rows();
    r.afterFilter = afterFilter.length;
    r.afterFilterSort = afterSort.slice(0, 8).map((x) => x.split(' ')[0].slice(0, 40));
    await typeLine('sort stars');
    const ascending = await rows();
    r.filterChangedRows = afterFilter.length !== r.rowsAtStart;
    r.sortFlipsOrder = ascending.join() !== afterSort.join() && ascending.slice().reverse().join() === afterSort.join();
    if (kind === 'sample') check(r.filterChangedRows && r.sortFlipsOrder, name + ': filter/sort');
    await typeLine('sort -stars');
    r.gutter = await evalJs(`(() => {
      const d = [...document.querySelectorAll('#out div')];
      const first = d[0];
      return {
        lines: d.length,
        numbered: d.every((x, i) => x.dataset.n === String(i + 1)),
        before: first ? getComputedStyle(first, '::before').content : null,
        selectable: first ? getComputedStyle(first, '::before').userSelect : null,
        textHasNoNumber: first ? !/^\d/.test(first.textContent) : false,
        header: document.getElementById('outcount').textContent,
      };
    })()`);
    await shot('-after-commands');
    await typeLine('table');
    await shot('-table');
    r.horizontalScrollAfter = (await evalJs('document.documentElement.scrollWidth')) > r.innerWidth;
    check(!r.horizontalScrollAfter, name + ': horizontal scroll after table');
    {
      // recent view: repositories by first seen, newest first, with the note above the table
      await typeLine('reset');
      await typeLine('view table');
      const tableRows = await evalJs("document.querySelectorAll('#view tbody tr').length");
      await typeLine('view recent');
      const recentCol = () => evalJs(`(() => {
        const ths = [...document.querySelectorAll('#view thead th')];
        const i = ths.findIndex((t) => /^First seen/.test(t.textContent));
        const trs = [...document.querySelectorAll('#view tbody tr')];
        return {
          heads: ths.map((t) => t.textContent.replace(/ [▲▼]/, '')),
          sort: i < 0 ? null : ths[i].getAttribute('aria-sort'),
          times: i < 0 ? [] : trs.map((tr) => tr.children[i].textContent),
          rows: trs.length,
          note: (document.getElementById('recent-note') || {}).textContent || null,
          noteAbove: !!document.querySelector('#view #recent-note + table'),
        };
      })()`);
      const ordered = (v, dir) => {
        const known = v.filter((x) => x !== 'unknown');
        const unknownLast = v.slice(known.length).every((x) => x === 'unknown');
        return unknownLast && known.every((x, i) => /^\d{4}-\d\d-\d\d \d\d:\d\d UTC$/.test(x) && (i === 0 || (dir === 'desc' ? known[i - 1] >= x : known[i - 1] <= x)));
      };
      const first = await recentCol();
      const note = 'First seen by this collector. Repositories first listed in a baseline collection all share one time, and the date is not when the repository was created.';
      r.recent = { rows: first.rows, tableRows, sort: first.sort, top: first.times.slice(0, 3), distinct: new Set(first.times).size };
      check(first.heads.join() === 'First seen,Game,Repository,Stars,Description', name + ': recent headings ' + JSON.stringify(first.heads));
      check(first.rows > 0 && first.rows === tableRows, name + ': recent view does not list the same repositories as the table ' + first.rows + ' vs ' + tableRows);
      check(first.sort === 'descending' && ordered(first.times, 'desc'), name + ': recent view is not newest first seen first ' + JSON.stringify(r.recent));
      check(first.note === note && first.noteAbove, name + ': recent note missing or not above the table ' + JSON.stringify(first.note));
      check((await evalJs('document.documentElement.scrollWidth')) <= r.innerWidth, name + ': horizontal scroll in the recent view');
      check((await evalJs("document.getElementById('view').scrollWidth <= document.getElementById('view').clientWidth")), name + ': the recent table scrolls sideways');
      // the first seen time keeps the date whole: one line on a desktop, date and clock on two lines on a phone
      r.recent.firstSeenLines = await evalJs(`(() => {
        const td = document.querySelector('#view td.firstseen');
        if (!td || !td.firstChild) return null;
        const range = document.createRange();
        range.selectNodeContents(td);
        const lines = [];
        for (const rect of range.getClientRects()) if (!lines.some((t) => Math.abs(t - rect.top) < 3)) lines.push(rect.top);
        range.setEnd(td.firstChild, 10);
        return { lines: lines.length, dateOnOneLine: new Set([...range.getClientRects()].map((x) => Math.round(x.top))).size === 1 };
      })()`);
      check(r.recent.firstSeenLines && r.recent.firstSeenLines.dateOnOneLine && r.recent.firstSeenLines.lines === (mobile ? 2 : 1), name + ': first seen cell wraps badly ' + JSON.stringify(r.recent.firstSeenLines));
      await shot('-recent');
      await evalJs("[...document.querySelectorAll('#view thead th button')].find((b) => /^First seen/.test(b.textContent)).click()");
      await sleep(200);
      const flipped = await recentCol();
      r.recent.afterClick = { sort: flipped.sort, top: flipped.times.slice(0, 3) };
      check(flipped.sort === 'ascending' && flipped.rows === first.rows && ordered(flipped.times, 'asc'), name + ': clicking First seen does not flip the order ' + JSON.stringify(r.recent.afterClick));
      if (r.recent.distinct > 1) check(flipped.times[0] !== first.times[0], name + ': clicking First seen left the first row in place');
      // filters narrow the recent view like the table
      await typeLine('sort -first-seen');
      await typeLine('filter category:framework');
      const filtered = await recentCol();
      r.recent.filtered = filtered.rows;
      check(filtered.rows > 0 && filtered.rows < first.rows && ordered(filtered.times, 'desc'), name + ': filter in the recent view ' + filtered.rows);
      await typeLine('reset');
    }
    {
      // games view: one row per game name; the repository counts add up to the table's rows
      await typeLine('reset');
      await typeLine('view table');
      const tableRows = await evalJs("document.querySelectorAll('#view tbody tr').length");
      await typeLine('view games');
      const games = await evalJs(`(() => {
        const trs = [...document.querySelectorAll('#view tbody tr')].map((tr) => [...tr.children].map((td) => td.textContent));
        return { heads: [...document.querySelectorAll('#view thead th')].map((t) => t.textContent.replace(/ [▲▼]/, '')), rows: trs };
      })()`);
      const counts = games.rows.map((x) => Number(x[2]));
      const known = games.rows.filter((x) => x[0] !== 'unknown').map((x) => Number(x[2]));
      r.games = { rows: games.rows.length, repos: counts.reduce((a, b) => a + b, 0), tableRows, last: games.rows.length ? games.rows[games.rows.length - 1][0] : null };
      check(games.heads.join() === 'Game,Label,Repositories,Forks,Most recent repository,Latest commit,Total stars', name + ': games headings ' + JSON.stringify(games.heads));
      check(games.rows.length > 0 && r.games.repos === tableRows, name + ': games repository counts do not add up to the table ' + JSON.stringify(r.games));
      check(known.every((n, i) => i === 0 || known[i - 1] >= n) && games.rows.findIndex((x) => x[0] === 'unknown') in { [-1]: 1, [games.rows.length - 1]: 1 }, name + ': games order ' + JSON.stringify(games.rows.map((x) => x[0] + ':' + x[2])));
      const names = games.rows.map((x) => x[0]);
      check(new Set(names).size === names.length && names.every((n) => !/\[(inferred|inherited)/.test(n)), name + ': games rows are not one per game name ' + JSON.stringify(names));
      check(games.rows.every((x) => /^(inherited \/ unverified|inferred|not inferred|no game|mixed: \d+ .+)$/.test(x[1])), name + ': games label ' + JSON.stringify(games.rows.map((x) => x[1])));
      check((await evalJs('document.documentElement.scrollWidth')) <= r.innerWidth, name + ': horizontal scroll in the games view');
      await shot('-games');
      await typeLine('reset');
    }
    if (kind === 'real') {
      // the column headings stay at the top of the scrolling view
      await typeLine('reset');
      r.sticky = {};
      for (const v of ['feed', 'table', 'forks', 'recent']) {
        await typeLine('view ' + v);
        r.sticky[v] = await evalJs(`(() => {
          const box = document.getElementById('view');
          const head = box.querySelector('${v === 'feed' ? '.colhead' : 'thead th'}');
          if (!head) return { error: 'no heading' };
          box.scrollTop = 600;
          const scrolled = box.scrollTop;
          const top = head.getBoundingClientRect().top - box.getBoundingClientRect().top;
          return { scrolled, offsetFromTop: Math.round(top), visible: top >= -1 && top <= 6 };
        })()`);
        if (mobile && v === 'feed') {
          // phone rows are stacked, so the feed has no column headings there
          r.sticky[v].hiddenOnPhone = await evalJs("getComputedStyle(document.querySelector('#view .colhead')).display === 'none'");
          check(r.sticky[v].hiddenOnPhone, name + ': feed column heading is hidden on a phone');
          continue;
        }
        check(r.sticky[v].scrolled > 0 && r.sticky[v].visible, name + ': ' + v + ' heading sticks (' + JSON.stringify(r.sticky[v]) + ')');
        if (v !== 'feed') await shot('-sticky-' + v);
      }
      // clicking a column heading sorts by it; a second click flips the direction
      await typeLine('view table');
      const starCol = () => evalJs(`(() => {
        const ths = [...document.querySelectorAll('#view thead th')];
        const i = ths.findIndex((t) => /^Stars/.test(t.textContent));
        const vals = [...document.querySelectorAll('#view tbody tr')].map((tr) => tr.children[i].textContent).filter((v) => /^[0-9]+$/.test(v)).map(Number);
        return { vals: vals.slice(0, 40), sort: ths[i].getAttribute('aria-sort') };
      })()`);
      const clickStars = () => evalJs("[...document.querySelectorAll('#view thead th button')].find((b) => /^Stars/.test(b.textContent)).click()");
      await clickStars();
      const first = await starCol();
      await clickStars();
      const second = await starCol();
      const sorted = (v, dir) => v.every((x, i) => i === 0 || (dir === 'desc' ? v[i - 1] >= x : v[i - 1] <= x));
      r.sortByHeading = { first: first.sort, second: second.sort };
      check(first.vals.length > 5 && first.sort === 'descending' && sorted(first.vals, 'desc'), name + ': first click on Stars sorts descending ' + JSON.stringify(first));
      check(second.sort === 'ascending' && sorted(second.vals, 'asc'), name + ': second click on Stars sorts ascending ' + JSON.stringify(second));
      // the feed has a Stars column too, and its heading sorts
      await typeLine('sort -time');
      await typeLine('view feed');
      if (!mobile) {
        await evalJs("[...document.querySelectorAll('#view .colhead button')].find((b) => /^Stars/.test(b.textContent)).click()");
        r.feedStars = await evalJs(`(() => {
          const vals = [...document.querySelectorAll('#view .row .stars')].map((e) => e.textContent).filter((v) => /^[0-9]+$/.test(v)).map(Number);
          return { rows: document.querySelectorAll('#view .row').length, numeric: vals.length, top: vals.slice(0, 5), headings: [...document.querySelectorAll('#view .colhead > *')].map((e) => e.textContent.replace(/ [▲▼]/, '')) };
        })()`);
        check(r.feedStars.headings.join() === 'Time,Type,Game,Repository,Stars,Summary', name + ': feed headings ' + JSON.stringify(r.feedStars.headings));
        check(r.feedStars.numeric > 5 && r.feedStars.top.every((v, i, a2) => i === 0 || a2[i - 1] >= v), name + ': feed sorts by the Stars heading ' + JSON.stringify(r.feedStars));
        await typeLine('sort -time');
      }
    }
    if (kind === 'real' && !mobile) {
      await typeLine('reset');
      await typeLine('demo 2');
      await sleep(3600);
      const demo = (await rows()).filter((x) => x.includes('[DEMO]'));
      r.demoRows = demo.length;
      r.demoRowsLabelled = demo.length === 2 && demo.every((x) => x.includes('fictional, not real activity'));
      r.demoOutput = await evalJs("[...document.querySelectorAll('#out div')].map(d => d.textContent).find(t => t.startsWith('DEMO MODE')) || null");
      check(r.demoRowsLabelled, name + ': demo rows not labelled');
      await shot('-demo');
      await typeLine('reset');
      r.resetDropsDemo = !(await rows()).some((x) => x.includes('[DEMO]'));
      check(r.resetDropsDemo, name + ': reset kept demo rows');
    }
    report[name] = r;
  }
}

// real copy over file://: fetch is unavailable and the fallback is null
{
  const name = 'real-file-desktop-1280x800';
  await cmd('Emulation.setDeviceMetricsOverride', { width: 1280, height: 800, deviceScaleFactor: 1, mobile: false });
  await cmd('Page.navigate', { url: pathToFileURL(join(realDir, 'index.html')).href });
  await sleep(1500);
  const r = {
    fallbackIsNull: await evalJs('window.PS3RC_FALLBACK === null'),
    rows: (await rows()).length,
    view: await evalJs("document.getElementById('view').textContent"),
    statusline: await evalJs("document.getElementById('statusline').textContent"),
  };
  check(r.fallbackIsNull && r.rows === 0 && /could not be loaded/.test(r.view), name + ': failed state not shown');
  writeFileSync(join(shots, name + '.png'), Buffer.from((await cmd('Page.captureScreenshot', { format: 'png' })).data, 'base64'));
  report[name] = r;
}

// real copy with no events: the feed view explains why it is empty, the table lists the repositories
{
  const name = 'no-events-desktop-1280x800';
  await cmd('Emulation.setDeviceMetricsOverride', { width: 1280, height: 800, deviceScaleFactor: 1, mobile: false });
  await cmd('Page.navigate', { url: noEventsSrv.origin + '/index.html' });
  await sleep(1500);
  const noEvents = 'No events yet. The next collection compares against this baseline. The table and forks views list the repositories already found.';
  const repoCount = JSON.parse(readFileSync(join(noEventsDir, 'data', 'feed.json'), 'utf8')).repos.length;
  const r = {
    repos: repoCount,
    feedRows: (await rows()).length,
    feedView: await evalJs("document.getElementById('view').textContent"),
  };
  await typeLine('view table');
  r.tableRows = await evalJs("document.querySelectorAll('#view tbody tr').length");
  r.tableEmpty = await evalJs("(document.querySelector('#view .empty') || {}).textContent || null");
  check(repoCount > 0 && r.feedRows === 0 && r.feedView === noEvents, name + ': feed view does not say there are no events yet: ' + JSON.stringify(r.feedView));
  check(r.tableRows > 0 && r.tableEmpty === null, name + ': table view does not list the repositories');
  writeFileSync(join(shots, name + '.png'), Buffer.from((await cmd('Page.captureScreenshot', { format: 'png' })).data, 'base64'));
  report[name] = r;
}

// alias fields: rows keyed by game_key, low-confidence names hidden by default, the toggle shows them labelled low
{
  const name = 'aliases-desktop-1280x800';
  await cmd('Emulation.setDeviceMetricsOverride', { width: 1280, height: 800, deviceScaleFactor: 1, mobile: false });
  await cmd('Page.navigate', { url: aliasSrv.origin + '/index.html' });
  await sleep(1500);
  const games = () => evalJs(`(() => ({
    rows: [...document.querySelectorAll('#view tbody tr')].map((tr) => [...tr.children].map((td) => td.textContent)),
    count: document.getElementById('optcount').hidden ? '' : document.getElementById('optcount').textContent,
    active: document.getElementById('active').textContent,
    toggle: [...document.querySelectorAll('#toggles button')].map((b) => b.textContent).find((t) => t.startsWith('low-confidence')) || null,
  }))()`);
  await typeLine('untouched show');
  await typeLine('view games');
  const r = { hidden: await games() };
  const row = (g, n) => (g.rows.find((x) => x[0] === n) || null);
  const h = r.hidden;
  check(h.toggle === 'low-confidence names: hidden' && h.count === '' && /low-confidence names: hidden/.test(h.active), name + ': toggle at start ' + JSON.stringify([h.toggle, h.count, h.active]));
  check(h.rows.length && h.rows[h.rows.length - 1][0] === 'unreviewed names (hidden)' && h.rows[h.rows.length - 1][2] === '2', name + ': hidden row ' + JSON.stringify(h.rows));
  check(!row(h, 'Placeholder Tactics') && !row(h, 'Test Arena'), name + ': a low-confidence name is shown as a game');
  check(h.rows.filter((x) => /sample racer/i.test(x[0])).length === 1 && h.rows.some((x) => /sample racer/i.test(x[0]) && x[2] === '2' && x[1] === 'reviewed alias (medium)'), name + ': game_key does not join spellings ' + JSON.stringify(h.rows));
  check((row(h, 'Example Game') || [])[1] === 'mixed: 2 inherited / unverified, 1 reviewed alias (strong)', name + ': Example Game label ' + JSON.stringify(row(h, 'Example Game')));
  writeFileSync(join(shots, name + '-games.png'), Buffer.from((await cmd('Page.captureScreenshot', { format: 'png' })).data, 'base64'));
  await evalJs("document.getElementById('options').open = true");
  await evalJs("[...document.querySelectorAll('#toggles button')].find((b) => b.textContent.startsWith('low-confidence')).click()");
  await sleep(200);
  r.shown = await games();
  const s = r.shown;
  check(s.toggle === 'low-confidence names: shown' && s.count === '1 active' && /low-confidence names: shown/.test(s.active), name + ': toggle after click ' + JSON.stringify([s.toggle, s.count, s.active]));
  check(!row(s, 'unreviewed names (hidden)') && (row(s, 'Placeholder Tactics') || [])[1] === 'unreviewed heading (low)' && (row(s, 'Test Arena') || [])[1] === 'unreviewed heading (low)', name + ': shown low rows ' + JSON.stringify(s.rows));
  writeFileSync(join(shots, name + '-games-low-shown.png'), Buffer.from((await cmd('Page.captureScreenshot', { format: 'png' })).data, 'base64'));
  await typeLine('sort stars');
  await typeLine('reset view');
  check((await games()).toggle === 'low-confidence names: shown', name + ': reset view changed the low-confidence setting');
  await typeLine('reset');
  check((await games()).toggle === 'low-confidence names: hidden', name + ': reset did not restore the low-confidence setting');
  // details and the confidence filter
  await typeLine('show r2');
  r.details = await evalJs("[...document.querySelectorAll('#out div')].map((d) => d.textContent).find((t) => t.startsWith('game key:')) || null");
  check(r.details === 'game key: sample-racer   basis: alias_readme_title   confidence: medium   matched alias: sample racer', name + ': details ' + JSON.stringify(r.details));
  await typeLine('view table');
  await typeLine('untouched show');
  await typeLine('filter confidence:reviewed');
  r.reviewed = await evalJs("[...document.querySelectorAll('#view tbody tr')].map((tr) => tr.children[0].textContent)");
  check(r.reviewed.length === 4 && r.reviewed.every((t) => /reviewed alias|inherited/.test(t)), name + ': confidence:reviewed ' + JSON.stringify(r.reviewed));
  await typeLine('filter -confidence:reviewed confidence:low');
  r.low = await evalJs("[...document.querySelectorAll('#view tbody tr')].map((tr) => tr.children[0].textContent)");
  check(r.low.length === 2 && r.low.every((t) => t.endsWith('[unreviewed heading (low)]')), name + ': confidence:low ' + JSON.stringify(r.low));
  report[name] = r;
}

const origins = [[aliasSrv.origin, '<aliases>'], [sampleSrv.origin, '<sample>'], [realSrv.origin, '<real>'], [noEventsSrv.origin, '<no-events>'], [pathToFileURL(realDir).href, '<real-file>']];
console.log('requests:', JSON.stringify([...new Set(requests)].map((u) => origins.reduce((s, [o, n]) => s.replace(o, n), u))));
check(requests.every((u) => origins.some(([o]) => u.startsWith(o)) || u === 'about:blank'), 'a request left the local copies');
console.log('problems (console errors/warnings, CSP violations, failed requests):', problems.length ? '\n  ' + problems.join('\n  ') : 'none');
console.log('failed checks:', fails.length ? '\n  ' + fails.join('\n  ') : 'none');
console.log(JSON.stringify(report, null, 1));
ws.close();
chrome.kill();
await sleep(300);
rmSync(profile, { recursive: true, force: true });
sampleSrv.srv.close();
realSrv.srv.close();
noEventsSrv.srv.close();
aliasSrv.srv.close();
rmSync(work, { recursive: true, force: true });
process.exit(problems.length || fails.length ? 1 : 0);
