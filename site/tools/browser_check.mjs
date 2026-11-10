// Loads the page in headless Chromium over a local DevTools port and reports problems. Nothing leaves 127.0.0.1.
// Three runs, each on a temporary copy of this folder:
//   sample    - served over http://127.0.0.1 with testdata/ copied into data/ (the fictional sample)
//   real      - served over http://127.0.0.1 as deployed (data/ is the real export, testdata/ left out)
//   real-file - the real copy opened over file://, where fetch is unavailable and the fallback is null:
//               the page must show that the feed could not be loaded, not throw and not show any data
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

const origins = [[sampleSrv.origin, '<sample>'], [realSrv.origin, '<real>'], [pathToFileURL(realDir).href, '<real-file>']];
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
rmSync(work, { recursive: true, force: true });
process.exit(problems.length || fails.length ? 1 : 0);
