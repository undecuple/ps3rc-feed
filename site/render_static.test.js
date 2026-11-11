'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const T = require('./terminal.js');
const R = require('./tools/render_static.js');

const SCRIPT = path.join(__dirname, 'tools', 'render_static.js');
const NOW = Date.parse('2026-11-10T12:00:00Z');

function repo(i, extra) {
  return Object.assign({
    id: 'r' + i, repo: 'owner' + i + '/repo' + i, owner: 'owner' + i, url: 'https://github.com/owner' + i + '/repo' + i,
    fork: false, stars: i, commits_30d: 1, first_seen_at: new Date(NOW - i * 60000).toISOString(),
    last_commit_at: '2026-11-01T00:00:00Z', game: 'Game ' + i,
  }, extra);
}
const feedOf = (repos) => ({ generated_at: '2026-11-10T11:30:00Z', events: [], repos });
const rows = (html) => (html.match(/<tr>/g) || []).length - 1; // minus the head row
const hrefs = (html) => [...html.matchAll(/href="([^"]*)"/g)].map((m) => m[1]);

test('hostile names are escaped and never become markup or links', () => {
  const evil = '<script>alert(1)</script>"\'&';
  const html = R.render(feedOf([
    repo(1, { repo: evil, game: evil, owner: evil }),
    repo(2, { url: 'javascript:alert(1)' }),
    repo(3, { url: 'https://github.com/a/b"><img src=x onerror=alert(1)>' }),
    repo(4, { url: 'https://evil.example/owner4/repo4' }),
    repo(5, { url: 'https://user:pw@github.com/owner5/repo5' }),
    repo(6, { url: 'https://github.com/owner6/repo6?tab=x' }),
    repo(7, { url: 'https://github.com/owner7/repo7/../../evil' }),
    repo(8, { sample: true }),
  ]), NOW);
  assert.ok(!html.includes('<script'));
  assert.ok(!/<img/i.test(html));
  assert.ok(html.includes('&lt;script&gt;alert(1)&lt;/script&gt;&quot;&#39;&amp;'));
  const links = hrefs(html).filter((h) => h.startsWith('https://github.com/owner'));
  assert.deepEqual(links, ['https://github.com/owner1/repo1']);
  for (const h of hrefs(html)) assert.ok(/^(https:\/\/github\.com\/|style\.css$|favicon\.svg$|index\.html$|feed\.atom$)/.test(h), h);
  assert.ok(!/ on[a-z]+=/i.test(html));
  assert.ok(!/style=/i.test(html));
});

test('repoHref accepts only https://github.com/<owner>/<repo>', () => {
  assert.equal(R.repoHref({ url: 'https://github.com/a/b' }), 'https://github.com/a/b');
  assert.equal(R.repoHref({ url: 'https://github.com/a' }), null);
  assert.equal(R.repoHref({ url: 'https://github.com/a/b/issues' }), null);
  assert.equal(R.repoHref({ url: 'https://github.com:444/a/b' }), null);
  assert.equal(R.repoHref({ url: 'https://github.com/a/b#x' }), null);
  assert.equal(R.repoHref({ url: null }), null);
  assert.equal(R.repoHref({ url: 'https://github.com/a/b', sample: true }), null);
});

test('rows are capped at 200 and the page says so', () => {
  const many = Array.from({ length: 250 }, (_, i) => repo(i));
  const html = R.render(feedOf(many), NOW);
  assert.equal(rows(html), R.CAP);
  assert.ok(html.includes('Showing the 200 most recently discovered of 250'));
  // most recently discovered first: repo0 was seen last
  assert.ok(html.indexOf('owner0/repo0') < html.indexOf('owner1/repo1'));
  assert.ok(!html.includes('owner249/repo249'));
  const few = R.render(feedOf(many.slice(0, 200)), NOW);
  assert.equal(rows(few), 200);
  assert.ok(!few.includes('Showing the'));
});

test('order and hiding come from terminal.js', () => {
  const list = [repo(1), repo(2), repo(3, { fork: true, stars: 0, commits_30d: 0 })];
  const html = R.render(feedOf(list), NOW);
  const recs = T.sortRecent(T.dropUntouched(T.joinData(feedOf(list), NOW).repos, T.initialState()), { key: 'first-seen', dir: 'desc' });
  const order = recs.map((r) => html.indexOf('>' + r.repo + '<'));
  assert.ok(order.every((x, i) => x > 0 && (i === 0 || x > order[i - 1])));
  assert.ok(!html.includes('owner3/repo3'));
  assert.ok(html.includes('1 untouched fork not listed'));
  assert.ok(html.includes('<td>not a fork</td>'));
});

test('an empty or broken feed still renders the notice and an empty message', () => {
  for (const feed of [feedOf([]), {}, null, [], { repos: 'x', events: 3 }]) {
    const html = R.render(feed, NOW);
    assert.equal(rows(html), 1);
    assert.ok(html.includes('Nothing here yet.'));
    assert.ok(html.includes(T.noticeText(feed && !Array.isArray(feed) ? feed : {}, NOW)));
  }
});

test('the notice, disclaimer and footer match index.html', () => {
  const index = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
  const html = R.render(feedOf([repo(1)]), NOW);
  assert.ok(html.includes(T.noticeText(feedOf([repo(1)]), NOW)));
  assert.ok(index.includes('<p class="disclaimer">' + R.DISCLAIMER + '</p>'));
  assert.ok(html.includes('<p class="disclaimer">' + R.DISCLAIMER + '</p>'));
  const footer = /<footer class="links">([\s\S]*?)<\/footer>/.exec(index)[1];
  const indexLinks = hrefs(footer).filter((h) => h.startsWith('https://'));
  assert.deepEqual(indexLinks, R.FOOTER.map((f) => f[0]));
  for (const [href, text] of R.FOOTER) assert.ok(html.includes('<a href="' + href + '">' + text + '</a>'));
  assert.ok(index.includes('<a href="static.html">no JavaScript? static list</a>'));
});

test('the command line output is deterministic for a given now and needs no script', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'render-static-'));
  try {
    const feedPath = path.join(dir, 'feed.json');
    fs.writeFileSync(feedPath, JSON.stringify(feedOf([repo(1), repo(2)])));
    const a = path.join(dir, 'a.html');
    const b = path.join(dir, 'b.html');
    execFileSync(process.execPath, [SCRIPT, feedPath, a, '2026-11-10T12:00:00Z']);
    execFileSync(process.execPath, [SCRIPT, feedPath, b, '2026-11-10T12:00:00Z']);
    const html = fs.readFileSync(a, 'utf8');
    assert.equal(html, fs.readFileSync(b, 'utf8'));
    assert.equal(html, R.render(feedOf([repo(1), repo(2)]), NOW));
    assert.ok(html.includes('Rendered 2026-11-10 12:00 UTC'));
    assert.ok(!/<script/i.test(html));
    assert.ok(!/<style/i.test(html));
    assert.ok(html.includes('<link rel="stylesheet" href="style.css">'));
    assert.throws(() => execFileSync(process.execPath, [SCRIPT, feedPath, a, 'not-a-date'], { stdio: 'pipe' }));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('the committed feed renders', () => {
  const feed = JSON.parse(fs.readFileSync(path.join(__dirname, 'data', 'feed.json'), 'utf8'));
  const html = R.render(feed, Date.parse(feed.generated_at));
  assert.ok(rows(html) > 0 && rows(html) <= R.CAP);
});

test('low-confidence game names are marked unreviewed, reviewed aliases say so', () => {
  const html = R.render(feedOf([
    repo(1, { game: 'Heading Name', game_key: 'heading-name', game_basis: 'readme_title', game_confidence: 'low' }),
    repo(2, { game: 'Old Heading', game_confidence: 'weak' }),
    repo(3, { game: 'Real Title', game_key: 'real-title', game_basis: 'alias_name', game_confidence: 'strong', matched_alias: 'real title' }),
  ]), NOW);
  assert.ok(html.includes('<td>Heading Name [unreviewed heading (low)]</td>'));
  assert.ok(html.includes('<td>Old Heading [unreviewed heading (low)]</td>'));
  assert.ok(html.includes('<td>Real Title [reviewed alias (strong)]</td>'));
});
