// Loads index.html from file:// in headless Chromium over a local DevTools port and reports problems.
// Usage: node tools/browser_check.mjs <chromium-binary> [--no-sandbox]
import { spawn } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync, mkdirSync, existsSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const shots = join(root, 'shots');
const page = pathToFileURL(join(root, 'index.html')).href;
const [bin, ...flags] = process.argv.slice(2);
mkdirSync(shots, { recursive: true });
const profile = mkdtempSync(join(tmpdir(), 'ps3rc-check-'));
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

const report = {};
for (const [name, w, h, mobile] of [['desktop-1280x800', 1280, 800, false], ['phone-390x844', 390, 844, true]]) {
  await cmd('Emulation.setDeviceMetricsOverride', { width: w, height: h, deviceScaleFactor: 1, mobile });
  await cmd('Page.navigate', { url: page });
  await sleep(1500);
  const r = { scrollWidth: await evalJs('document.documentElement.scrollWidth'), innerWidth: await evalJs('window.innerWidth') };
  r.horizontalScroll = r.scrollWidth > r.innerWidth;
  r.rowsAtStart = (await rows()).length;
  const shot = async (suffix) => writeFileSync(join(shots, name + suffix + '.png'), Buffer.from((await cmd('Page.captureScreenshot', { format: 'png' })).data, 'base64'));
  await shot('');
  const firstName = 'demo-user';
  await typeOnly(firstName);
  r.liveSearchRows = (await rows()).length;
  r.liveSearchNarrowed = r.liveSearchRows > 0 && r.liveSearchRows < r.rowsAtStart;
  await shot('-live-search');
  for (const type of ['keyDown', 'keyUp']) await cmd('Input.dispatchKeyEvent', { type, key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
  await sleep(300);
  r.escapeRestoresRows = (await rows()).length === r.rowsAtStart;
  await evalJs("document.getElementById('cmd').value = ''");
  await typeLine('filter category:framework');
  const afterFilter = await rows();
  await typeLine('sort -stars');
  const afterSort = await rows();
  r.afterFilter = afterFilter.length;
  r.afterFilterSort = afterSort.map((x) => x.split(' ')[0].slice(0, 40));
  await typeLine('sort stars');
  const ascending = await rows();
  r.filterChangedRows = afterFilter.length !== r.rowsAtStart;
  r.sortFlipsOrder = ascending.join() !== afterSort.join() && ascending.slice().reverse().join() === afterSort.join();
  await typeLine('sort -stars');
  await shot('-after-commands');
  await typeLine('table');
  await shot('-table');
  r.horizontalScrollAfter = (await evalJs('document.documentElement.scrollWidth')) > r.innerWidth;
  report[name] = r;
}

console.log('requests:', JSON.stringify([...new Set(requests)].map((u) => u.replace(pathToFileURL(root).href, '<site>'))));
console.log('problems (console errors/warnings, CSP violations, failed requests):', problems.length ? '\n  ' + problems.join('\n  ') : 'none');
console.log(JSON.stringify(report, null, 1));
ws.close();
chrome.kill();
await sleep(300);
rmSync(profile, { recursive: true, force: true });
process.exit(problems.length ? 1 : 0);
