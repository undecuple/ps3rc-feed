'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const T = require('./terminal.js');

const feed = JSON.parse(fs.readFileSync(__dirname + '/testdata/feed.json', 'utf8'));
const NOW = Date.parse(feed.generated_at);
const { repos, events } = T.joinData(feed, NOW);
const ctx = {
  now: NOW,
  find: (id) => events.concat(repos).find((r) => r.id === id) || null,
  values: { game: ['Example Game', 'Sample Racer'], repo: [], owner: ['example-dev', 'example-org'], toolkit: ['ps3recomp'], id: ['e01', 'r1'] },
  info: ['info line'],
};
const run = (line, state) => T.execute(state || T.initialState(), line, ctx);
const shown = (list, filters, search) => list.filter((r) => T.matches(r, filters, search || '', NOW));
const filt = (text) => run('filter ' + text).state.filters;
const ids = (list) => list.map((r) => r.id);

test('help lists every command and per-command help works', () => {
  const out = run('help').out.map((l) => l.text).join('\n');
  for (const c of ['help', 'filter', 'sort', 'group', 'search', 'show', 'open', 'pause', 'resume', 'order', 'scroll', 'subscribe', 'demo', 'status', 'about', 'clear', 'reset', 'untouched']) {
    assert.ok(out.includes(c), c);
  }
  assert.ok(run('help filter').out.length > 1);
  assert.equal(run('help nope').out[0].kind, 'error');
});

test('text that is not a command is a search, and nothing runs', () => {
  const r = run('rm -rf /');
  assert.equal(r.effects.length, 0);
  assert.equal(r.state.search, 'rm -rf /');
  assert.equal(run('constructor').state.search, 'constructor');
  assert.equal(run('__proto__').state.search, '__proto__');
  assert.equal(run('racer').state.search, 'racer');
  assert.equal(run('x'.repeat(500)).state.search.length, 100);
});

test('a lone field:value term is a filter, not a search', () => {
  const r = run('fork:yes');
  assert.equal(r.state.search, '');
  assert.deepEqual(r.state.filters.map((f) => f.raw), ['fork:yes']);
  assert.equal(run('nosuchfield:yes').state.search, 'nosuchfield:yes');
});

test('live search text: words search while typing, commands and field terms wait for Enter', () => {
  assert.equal(T.liveSearchText(''), '');
  assert.equal(T.liveSearchText('   '), '');
  assert.equal(T.liveSearchText('  skyfall recomp '), 'skyfall recomp');
  assert.equal(T.liveSearchText('sort stars'), null);
  assert.equal(T.liveSearchText('help'), null);
  assert.equal(T.liveSearchText('fork:yes'), null);
  assert.equal(T.liveSearchText('-status:stale'), null);
  assert.equal(T.liveSearchText('constructor'), 'constructor');
  assert.equal(T.liveSearchText('x'.repeat(500)).length, 100);
});

test('filter fields: game repo owner author category status type toolkit fork time stars', () => {
  assert.deepEqual(ids(shown(repos, filt('game:racer'))), ['r2', 'r12']);
  assert.deepEqual(ids(shown(repos, filt('repo:ps3recomp-core'))), ['r4', 'r5']);
  assert.ok(shown(repos, filt('owner:example-dev')).every((r) => r.owner === 'example-dev'));
  assert.deepEqual(ids(shown(repos, filt('author:example-dev'))), ids(shown(repos, filt('owner:example-dev'))));
  assert.deepEqual(ids(shown(repos, filt('category:framework'))), ['r4', 'r5', 'r11']);
  assert.deepEqual(ids(shown(repos, filt('status:archived'))), ['r9']);
  assert.deepEqual(ids(shown(repos, filt('status:stale'))), ['r10']);
  assert.deepEqual(ids(shown(repos, filt('toolkit:ps3recomp'))).includes('r6'), false);
  assert.deepEqual(ids(shown(repos, filt('fork:yes'))), ['r5', 'r7', 'r8', 'r11', 'r12']);
  assert.equal(shown(repos, filt('fork:no')).some((r) => r.fork), false);
  assert.deepEqual(ids(shown(repos, filt('stars:>100'))), ['r4']);
  assert.deepEqual(ids(shown(events, filt('type:release'))).length, 3);
  assert.deepEqual(ids(shown(events, filt('type:release_published'))).length, 3);
  assert.equal(shown(events, filt('type:commit')).every((e) => e.type === 'commits_observed'), true);
});

test('status describes observed activity only', () => {
  const statuses = new Set(repos.map((r) => r.status));
  for (const s of statuses) assert.ok(['active', 'quiet', 'stale', 'archived'].includes(s), s);
  assert.equal(run('filter status:playable').out[0].kind, 'error');
  assert.equal(run('filter status:verified').out[0].kind, 'error');
  assert.equal(T.deriveStatus({ last_commit_at: new Date(NOW - 29 * 86400000).toISOString() }, NOW), 'active');
  assert.equal(T.deriveStatus({ last_commit_at: new Date(NOW - 31 * 86400000).toISOString() }, NOW), 'quiet');
  assert.equal(T.deriveStatus({ last_commit_at: null }, NOW), null);
});

test('filter values: OR within a field, AND across fields, removal and clear', () => {
  const f = filt('category:framework category:tool');
  assert.deepEqual(ids(shown(repos, f)), ['r4', 'r5', 'r6', 'r11']);
  const both = filt('category:framework status:active');
  assert.deepEqual(ids(shown(repos, both)), ['r4', 'r5']);
  let s = run('filter category:game status:active').state;
  s = run('filter -status:active', s).state;
  assert.deepEqual(s.filters.map((x) => x.raw), ['category:game']);
  s = run('filter -category', s).state;
  assert.equal(s.filters.length, 0);
  s = run('filter fork:yes owner:example', s).state;
  assert.equal(run('filter clear', s).state.filters.length, 0);
  assert.equal(run('filter category:framework category:framework').state.filters.length, 1);
});

test('filter errors leave the state unchanged', () => {
  const s = run('filter category:game').state;
  for (const bad of ['colour:red', 'category:banana', 'novalue', 'stars:lots', 'time:soon', 'fork:maybe', 'type:nope', 'owner:']) {
    const r = run('filter ' + bad, s);
    assert.equal(r.out[0].kind, 'error', bad);
    assert.deepEqual(r.state.filters, s.filters, bad);
  }
});

test('time expressions', () => {
  const t = (x) => ids(shown(repos, filt('time:' + x)));
  assert.deepEqual(t('<1h'), ['r1', 'r2', 'r3']);
  assert.ok(t('<24h').includes('r7') && !t('<24h').includes('r8'));
  assert.ok(t('24h').includes('r1'));
  assert.ok(t('<7d').includes('r8') && !t('<7d').includes('r6'));
  assert.ok(t('>7d').includes('r6') && !t('>7d').includes('r1'));
  assert.ok(t('>30d').includes('r9'));
  assert.ok(t('<1w').includes('r13'));
  assert.deepEqual(t('today').includes('r7'), true);
  assert.equal(t('today').includes('r13'), false);
  assert.deepEqual(ids(shown(repos, filt('time:>7d time:<60d'))).includes('r6'), true);
  assert.equal(T.parseTime('<24h').ms, 86400000);
  assert.equal(T.parseTime('10x'), null);
  assert.equal(T.parseTime('<h'), null);
});

test('stars operators and unknown values', () => {
  assert.ok(shown(repos, filt('stars:>=84')).every((r) => r.stars >= 84));
  assert.deepEqual(ids(shown(repos, filt('stars:0'))), ['r11', 'r13']);
  assert.deepEqual(ids(shown(repos, filt('stars:unknown'))), ['r3']);
  assert.deepEqual(ids(shown(repos, filt('game:unknown'))).includes('r3'), true);
  assert.equal(shown(repos, filt('stars:<5')).some((r) => r.id === 'r3'), false);
  assert.deepEqual(ids(shown(repos, filt('toolkit:unknown'))), ['r3', 'r6', 'r13']);
  assert.deepEqual(ids(shown(repos, filt('category:unknown'))), []);
});

test('search matches words across text fields', () => {
  assert.deepEqual(ids(shown(repos, [], 'racer ps3recomp')), ['r2', 'r12']);
  assert.deepEqual(ids(shown(events, [], 'release v0.2')), ['e02']);
  const r = run('search hello world');
  assert.equal(r.state.search, 'hello world');
  assert.equal(run('search', r.state).state.search, '');
});

test('sort keys, direction and aliases', () => {
  const sorted = (cmd, list) => T.sortRecords(list || repos, run(cmd).state.sort);
  const times = sorted('sort -time').map((r) => r.time).filter((x) => x !== null);
  assert.deepEqual(times, [...times].sort((a, b) => b - a));
  const asc = sorted('sort time').map((r) => r.time);
  assert.deepEqual(asc, [...asc].sort((a, b) => a - b));
  assert.deepEqual(run('sort author').state.sort, { key: 'owner', dir: 'asc' });
  assert.deepEqual(run('sort -author').state.sort, { key: 'owner', dir: 'desc' });
  assert.deepEqual(run('sort stars desc').state.sort, { key: 'stars', dir: 'desc' });
  assert.deepEqual(run('sort first-seen asc').state.sort, { key: 'first-seen', dir: 'asc' });
  const owners = sorted('sort owner').map((r) => r.owner);
  assert.deepEqual(owners, [...owners].sort());
  for (const k of ['game', 'category', 'status', 'commits']) assert.equal(run('sort ' + k).out[0].kind, 'ok', k);
  assert.equal(run('sort colour').out[0].kind, 'error');
  assert.equal(run('sort time sideways').out[0].kind, 'error');
  assert.equal(run('sort').out[0].kind, 'error');
});

test('unknown values sort last in both directions and are not zero', () => {
  for (const cmd of ['sort stars', 'sort -stars']) {
    const list = T.sortRecords(repos, run(cmd).state.sort);
    assert.equal(list[list.length - 1].id, 'r3', cmd);
    assert.equal(list[list.length - 1].stars, null);
  }
  const byGame = T.sortRecords(repos, run('sort -game').state.sort);
  const firstUnknown = byGame.findIndex((r) => r.game === null);
  assert.ok(byGame.slice(firstUnknown).every((r) => r.game === null));
  const zeroFirst = T.sortRecords(repos, { key: 'stars', dir: 'asc' });
  assert.equal(zeroFirst[0].stars, 0);
  assert.equal(T.orUnknown(null), 'unknown');
  assert.equal(T.orUnknown(0), '0');
});

test('order, scroll, group, view, pause and resume', () => {
  assert.deepEqual(run('order oldest').state.sort, { key: 'time', dir: 'asc' });
  assert.deepEqual(run('order newest').state.sort, { key: 'time', dir: 'desc' });
  assert.equal(run('order sideways').out[0].kind, 'error');
  assert.equal(run('scroll manual').state.scroll, 'manual');
  assert.equal(run('scroll auto').state.scroll, 'auto');
  assert.equal(run('scroll x').out[0].kind, 'error');
  assert.equal(run('group owner').state.group, 'owner');
  assert.equal(run('group nope').out[0].kind, 'error');
  assert.equal(run('table').state.view, 'table');
  assert.equal(run('forks').state.view, 'forks');
  assert.equal(run('feed').state.view, 'feed');
  assert.equal(run('view forks').state.view, 'forks');
  assert.equal(run('recent').state.view, 'recent');
  assert.equal(run('view recent').state.view, 'recent');
  assert.equal(run('about').state.view, 'about');
  // the phosphor theme and CRT effects were removed with the terminal look
  for (const gone of ['theme green', 'crt off']) assert.equal(run(gone).state.search, gone, gone);
  assert.ok(!('theme' in run('status').state) && !('crt' in run('status').state));
  const p = run('pause').state;
  assert.equal(p.paused, true);
  assert.equal(run('resume', p).state.paused, false);
});

test('grouping puts unknown last', () => {
  const g = T.groupRecords(repos, 'game');
  assert.equal(g[g.length - 1].label, 'unknown');
  assert.equal(T.groupRecords(repos, 'none').length, 1);
});

test('show prints details with source time, collection time and stale flag', () => {
  const lines = run('show e04').out;
  const text = lines.map((l) => l.text).join('\n');
  assert.match(text, /source time: 2026-11-09 13:59 UTC/);
  assert.match(text, /collected:\s+2026-11-09 14:02 UTC/);
  assert.match(text, /stale: yes/);
  assert.match(text, /\[inferred\]/);
  assert.match(run('show r7').out.map((l) => l.text).join('\n'), /inherited \/ unverified/);
  assert.equal(run('show zzz').out[0].kind, 'error');
  assert.equal(run('show').out[0].kind, 'error');
});

test('a sample entry never yields a link', () => {
  for (const id of ['e01', 'r1', 'r7']) {
    for (const cmd of ['open', 'show']) {
      const r = run(cmd + ' ' + id);
      assert.deepEqual(r.effects, []);
      assert.equal(r.out.some((l) => l.href), false);
      assert.ok(r.out.some((l) => l.text === 'sample entry: no real link'));
      assert.equal(r.out.some((l) => /https?:/.test(l.text)), false);
    }
  }
  assert.ok(events.concat(repos).every((r) => r.sample && r.url === null));
  const forced = T.joinData({ repos: [{ id: 'z', sample: true, url: 'https://github.com/a/b' }], events: [] }, NOW);
  assert.equal(forced.repos[0].url, null);
});

test('a real (non-sample) github.com entry still gets a link and opens', () => {
  const d = T.joinData({ repos: [{ id: 'g1', repo: 'a/b', url: 'https://github.com/a/b' }], events: [{ id: 'g2', type: 'commits_observed', repo_id: 'g1' }] }, NOW);
  const c = { now: NOW, find: (id) => d.events.concat(d.repos).find((r) => r.id === id) };
  const r = T.execute(T.initialState(), 'open g2', c);
  assert.deepEqual(r.effects, [{ type: 'open', url: 'https://github.com/a/b' }]);
  assert.ok(r.out.some((l) => l.href === 'https://github.com/a/b'));
});

test('non-GitHub links are rejected', () => {
  for (const u of ['http://github.com/a/b', 'https://github.com.evil.example/a', 'https://evil.example/github.com', 'https://user@github.com/a', 'https://github.com:8443/a', 'javascript:alert(1)', 'data:text/html,x', 'ftp://github.com/a', '//github.com/a', 'github.com/a', '', null, undefined, 42]) {
    assert.equal(T.safeGithubUrl(u), null, String(u));
  }
  assert.equal(T.safeGithubUrl('https://github.com/example-dev/example-recomp'), 'https://github.com/example-dev/example-recomp');
  const bad = T.joinData({ repos: [{ id: 'x1', repo: 'a/b', url: 'https://evil.example/a/b' }], events: [{ id: 'x2', type: 'commits_observed', repo_id: 'x1', source_url: 'javascript:alert(1)' }] }, NOW);
  const c = { now: NOW, find: (id) => bad.events.concat(bad.repos).find((r) => r.id === id) };
  const r = T.execute(T.initialState(), 'open x2', c);
  assert.equal(r.effects.length, 0);
  assert.equal(r.out.some((l) => l.href), false);
  assert.equal(r.out.some((l) => l.kind === 'error'), true);
});

test('hostile strings stay inert text', () => {
  const evil = '<img src=x onerror=alert(1)><script>alert(1)</script> @everyone @here `id` $(rm -rf /) ; | && > /etc/passwd';
  const d = T.joinData({
    repos: [{ id: 'h1', repo: evil, owner: '@everyone', game: '<b>x</b>', category: 'game', url: 'javascript:alert(1)' }],
    events: [{ id: 'h2', type: 'commits_observed', repo_id: 'h1', summary: evil, source_at: feed.generated_at, observed_at: feed.generated_at, source_url: 'https://evil.example/' }],
  }, NOW);
  const c = { now: NOW, find: (id) => d.events.concat(d.repos).find((r) => r.id === id) };
  const lines = T.execute(T.initialState(), 'show h2', c).out;
  assert.ok(lines.some((l) => l.text.includes('<script>alert(1)</script>')));
  assert.ok(lines.some((l) => l.text.includes('@everyone')));
  for (const l of lines) {
    assert.deepEqual(Object.keys(l).filter((k) => k !== 'text' && k !== 'kind'), []);
    assert.equal(l.href, undefined);
  }
  // hostile command text never runs anything
  const r = T.execute(T.initialState(), 'filter owner:"@everyone" ; rm -rf / $(id) `id`', c);
  assert.deepEqual(r.effects, []);
  // anything that is not a command is only ever a bounded search string
  for (const text of ['<script>alert(1)</script>', '; ls']) {
    const q = T.execute(T.initialState(), text, c);
    assert.deepEqual(q.effects, []);
    assert.equal(q.state.search, text);
    for (const l of q.out) assert.equal(l.href, undefined);
  }
  assert.deepEqual(ids(d.events.filter((e) => T.matches(e, T.execute(T.initialState(), 'filter owner:@everyone', c).state.filters, '', NOW))), ['h2']);
});

test('subscribe prints proposed routes', () => {
  const out = run('subscribe').out.map((l) => l.text).join('\n');
  assert.match(out, /not deployed/);
  assert.match(out, /\/feed\.xml/);
  assert.match(out, /\/feeds\/forks\.xml/);
});

test('demo, status, clear and reset', () => {
  assert.deepEqual(run('demo').effects, [{ type: 'demo', n: 4 }]);
  assert.deepEqual(run('demo 7').effects, [{ type: 'demo', n: 7 }]);
  assert.deepEqual(run('demo stop').effects, [{ type: 'demo-stop' }]);
  assert.equal(run('demo 99').out[0].kind, 'error');
  assert.match(run('demo').out[0].text, /DEMO MODE/);
  assert.ok(run('status').out.some((l) => l.text === 'info line'));
  assert.deepEqual(run('clear').effects, [{ type: 'clear' }]);
  const s = run('filter fork:yes').state;
  const r = run('reset', s);
  assert.deepEqual(r.state, T.initialState());
  assert.deepEqual(r.effects, [{ type: 'reset' }]);
});

test('tab completion', () => {
  const c = (line) => T.complete(line, ctx);
  assert.equal(c('fil').line, 'filter ');
  assert.equal(c('sub').line, 'subscribe ');
  assert.equal(c('s').line, 's');
  assert.ok(c('s').options.includes('sort') && c('s').options.includes('show'));
  assert.equal(c('filter cat').line, 'filter category:');
  assert.equal(c('filter category:fr').line, 'filter category:framework ');
  assert.equal(c('filter status:').options.includes('status:stale'), true);
  assert.equal(c('filter owner:example-d').line, 'filter owner:example-dev ');
  assert.equal(c('filter game:exa').line, 'filter game:"Example Game" ');
  assert.equal(c('filter time:to').line, 'filter time:today ');
  assert.equal(c('filter fork:y').line, 'filter fork:yes ');
  assert.equal(c('filter -stat').line, 'filter -status:');
  assert.equal(c('sort -ti').line, 'sort -time ');
  assert.equal(c('sort auth').line, 'sort author ');
  assert.equal(c('sort time d').line, 'sort time desc ');
  assert.equal(c('order n').line, 'order newest ');
  assert.equal(c('scroll m').line, 'scroll manual ');
  assert.equal(c('show e0').line, 'show e01 ');
  assert.equal(c('group g').line, 'group game ');
  assert.equal(c('zzz').line, 'zzz');
  assert.equal(c('').options.length > 10, true);
  assert.equal(c('filter ').options.includes('game:'), true);
});

test('history navigation', () => {
  let h = T.pushHistory([], 'help');
  h = T.pushHistory(h, 'sort -time');
  h = T.pushHistory(h, 'sort -time');
  h = T.pushHistory(h, '   ');
  assert.deepEqual(h, ['help', 'sort -time']);
  let step = T.historyStep(h, h.length, -1);
  assert.equal(step.text, 'sort -time');
  step = T.historyStep(h, step.index, -1);
  assert.equal(step.text, 'help');
  step = T.historyStep(h, step.index, -1);
  assert.equal(step.text, 'help');
  step = T.historyStep(h, step.index, 1);
  step = T.historyStep(h, step.index, 1);
  assert.equal(step.text, null);
});

test('tokenizer handles quotes', () => {
  assert.deepEqual(T.tokenize('filter game:"Example Game" fork:yes').map((t) => t.text), ['filter', 'game:Example Game', 'fork:yes']);
  assert.deepEqual(T.tokenize('  a   b ').map((t) => t.text), ['a', 'b']);
  assert.equal(run('filter game:"Example Game"').state.filters[0].raw, 'game:"example game"');
});

test('sample data is fictional and in sync with the embedded fallback', () => {
  const fallback = fs.readFileSync(__dirname + '/testdata/feed-fallback.js', 'utf8');
  assert.equal(fallback.trim(), 'window.PS3RC_FALLBACK = ' + JSON.stringify(feed) + ';');
  assert.equal(feed.sample, true);
  const manifest = JSON.parse(fs.readFileSync(__dirname + '/testdata/manifest.json', 'utf8'));
  assert.equal(manifest.version, feed.version);
  assert.ok(feed.repos.every((r) => r.sample === true && !('url' in r)));
  assert.ok(feed.events.every((e) => e.sample === true));
  assert.ok(feed.events.every((e) => feed.repos.some((r) => r.id === e.repo_id)));
});

test('invalid times show as unknown instead of throwing', () => {
  assert.equal(T.fmtTime(NaN), 'unknown');
  assert.equal(T.ago(NaN, NOW), 'unknown');
  assert.equal(T.ago(NOW, NaN), 'unknown');
  assert.equal(T.eventTime(NaN, NOW), '[unknown]');
  assert.equal(T.eventTime(NOW, NaN), '[unknown]');
});

test('an event without a source time shows its collection time, not the viewer clock', () => {
  const f = {
    generated_at: '2026-11-10T04:15:58Z',
    repos: [{ id: 'r1', repo: 'a/b', owner: 'a', url: 'https://github.com/a/b' }],
    events: [
      { id: 'x1', type: 'repository_discovered', repo_id: 'r1', source_at: null, observed_at: '2026-11-10T04:15:58Z', summary: 's' },
      { id: 'x2', type: 'commit', repo_id: 'r1', source_at: '2026-11-10T03:00:00Z', observed_at: '2026-11-10T04:15:58Z', summary: 's' },
    ],
  };
  const j = T.joinData(f, Date.parse(f.generated_at));
  const [a, b] = j.events;
  assert.equal(a.time, Date.parse('2026-11-10T04:15:58Z'));
  assert.equal(a.sourceTime, null);
  assert.equal(b.time, Date.parse('2026-11-10T03:00:00Z'));
  assert.equal(b.sourceTime, Date.parse('2026-11-10T03:00:00Z'));
  const shown = T.execute(T.initialState(), 'show x1', { now: Date.parse(f.generated_at), find: (id) => j.events.find((r) => r.id === id) || null, values: {}, info: [] });
  assert.ok(shown.out.some((l) => /source time: unknown/.test(l.text)));
  assert.ok(shown.out.some((l) => /collected:\s+2026-11-10 04:15 UTC/.test(l.text)));
});

test('served data/ is the real export, and the fallback can never show the sample', () => {
  const crypto = require('node:crypto');
  const bytes = fs.readFileSync(__dirname + '/data/feed.json');
  const real = JSON.parse(bytes.toString('utf8'));
  const manifest = JSON.parse(fs.readFileSync(__dirname + '/data/manifest.json', 'utf8'));
  assert.equal(fs.readFileSync(__dirname + '/data/feed-fallback.js', 'utf8'), 'window.PS3RC_FALLBACK = null;\n');
  assert.equal(manifest.version, crypto.createHash('sha256').update(bytes).digest('hex').slice(0, 16));
  assert.equal(manifest.generated_at, real.generated_at);
  assert.notEqual(real.sample, true);
  assert.ok(real.repos.length > 0 && real.events.length > 0);
  assert.ok(real.repos.every((r) => r.sample !== true && T.safeGithubUrl(r.url) === r.url));
  assert.ok(real.events.every((e) => e.sample !== true && real.repos.some((r) => r.id === e.repo_id)));
  // no fictional sample names leak into the served feed
  assert.ok(!/example-dev|example-org|Sample Racer/.test(bytes.toString('utf8')));
  // every event time is a collected time from the feed, never the viewer clock
  const later = Date.parse(real.generated_at) + 86400000 * 365;
  const joined = T.joinData(real, later);
  assert.ok(joined.events.every((e, i) => e.time === Date.parse(real.events[i].source_at || real.events[i].observed_at)));
  assert.ok(joined.events.every((e) => e.time <= Date.parse(real.generated_at)));
});

test('untouched forks are hidden by default and a search or the toggle shows them', () => {
  const mk = (id, fork, stars, commits) => ({ id, fork, stars, commits_30d: commits, repo: 'o/r' + id, owner: 'o' });
  const recs = T.joinData({ repos: [mk(1, true, 0, 0), mk(2, true, 3, 0), mk(3, true, 0, 4), mk(4, false, 0, 0)], events: [] }, Date.now());
  assert.deepEqual(recs.repos.map((r) => r.untouched), [true, false, false, false]);
  const s = T.initialState();
  assert.equal(s.hideUntouched, true);
  assert.equal(T.dropUntouched(recs.repos, s).length, 3);
  assert.equal(T.dropUntouched(recs.repos, Object.assign({}, s, { search: 'x' })).length, 4);
  const demo = Object.assign({}, recs.repos[0], { kind: 'event', sample: true });
  assert.equal(T.dropUntouched([demo], s).length, 1);
  const r = run('untouched show', s);
  assert.equal(r.state.hideUntouched, false);
  assert.equal(T.dropUntouched(recs.repos, r.state).length, 4);
  assert.equal(run('untouched maybe', s).out[0].kind, 'error');
  assert.equal(run('reset', r.state).state.hideUntouched, true);
});

test('notice text is built from the feed collection object', () => {
  const at = '2026-12-01T06:00:00Z';
  const t = Date.parse(at);
  const mk = (collection, extra) => Object.assign({ generated_at: at, repos: [], events: [], collection }, extra);
  const base = { complete: true, stopped: null, not_fetched: 0, error_count: 0, last_run_at: at };
  const tail = ' Repository metadata comes from GitHub. Game, category and toolkit labels are inferred and may be wrong. This page is not real time. No game files are hosted here.';
  const common = (s) => {
    assert.ok(!/by hand|three|runs?\b/i.test(s), s);
    assert.ok(s.endsWith(tail), s);
  };

  // complete
  const complete = T.noticeText(mk(base), t);
  assert.equal(complete, 'Last collected: 2026-12-01 06:00 UTC. The last collection was complete.' + tail);
  common(complete);

  // incomplete, with counts
  const inc = T.noticeText(mk(Object.assign({}, base, { complete: false, stopped: 'rate_limited', not_fetched: 12, error_count: 3 })), t);
  assert.equal(inc, 'Last collected: 2026-12-01 06:00 UTC. The last collection was incomplete, so some entries and numbers may be missing. Repositories not fetched: 12 (they keep their previous values and are tried again later). Errors: 3.' + tail);
  common(inc);
  // incomplete, counts unknown
  const incNull = T.noticeText(mk(Object.assign({}, base, { complete: false, not_fetched: null, error_count: undefined })), t);
  assert.ok(incNull.includes('was incomplete') && !/not fetched|Errors/.test(incNull), incNull);

  // missing and malformed: neutral, never complete
  const unknown = 'Last collected: 2026-12-01 06:00 UTC. The collection state is unknown.' + tail;
  const bad = [
    undefined, null, 'complete', [], [base], {},
    Object.assign({}, base, { complete: 'true' }),
    Object.assign({}, base, { complete: 1 }),
    Object.assign({}, base, { not_fetched: -1 }),
    Object.assign({}, base, { not_fetched: 1.5 }),
    Object.assign({}, base, { error_count: '0' }),
    Object.assign({}, base, { complete: false, error_count: NaN }),
    Object.assign({}, base, { complete: false, not_fetched: Infinity }),
    Object.assign({}, base, { stopped: 5 }),
    Object.assign({}, base, { last_run_at: 'yesterday' }),
    Object.assign({}, base, { last_run_at: undefined }),
    Object.assign({}, base, { last_run_at: '2026-11-30T06:00:00Z' }),
    // contradictions: "complete" with a stop reason, missing repositories or errors
    Object.assign({}, base, { stopped: 'budget' }),
    Object.assign({}, base, { not_fetched: 2 }),
    Object.assign({}, base, { error_count: 1 }),
  ];
  for (const c of bad) {
    const s = T.noticeText(mk(c), t);
    assert.equal(s, unknown, JSON.stringify(c));
    assert.ok(!/was complete|was incomplete/.test(s));
  }
  const noKey = mk(base);
  delete noKey.collection;
  assert.equal(T.noticeText(noKey, t), unknown);
  assert.equal(T.collectionState(noKey), null);
  // no feed at all, or no usable time
  const none = 'The collection time is unknown. The collection state is unknown.' + tail;
  assert.equal(T.noticeText(null, t), none);
  assert.equal(T.noticeText(undefined, NaN), none);
  assert.equal(T.noticeText(mk(base, { generated_at: 'nope' }), t), none);

  // stale: older than stale_after_minutes, or the 45 minute default when it is absent or invalid
  const staleLine = (m) => ' This data is older than ' + m + ' minutes and may be out of date.';
  assert.ok(!T.noticeText(mk(base, { stale_after_minutes: 90 }), t + 90 * 60000).includes('older than'));
  assert.ok(T.noticeText(mk(base, { stale_after_minutes: 90 }), t + 90 * 60000 + 1).includes(staleLine(90)));
  assert.ok(!T.noticeText(mk(base), t + 45 * 60000).includes('older than'));
  assert.ok(T.noticeText(mk(base), t + 45 * 60000 + 1).includes(staleLine(45)));
  for (const m of [0, -5, 1.5, '90', NaN, Infinity, null]) {
    assert.ok(T.noticeText(mk(base, { stale_after_minutes: m }), t + 46 * 60000).includes(staleLine(45)), String(m));
  }
  assert.equal(T.feedStale(mk(base), t + 46 * 60000), true);
  assert.equal(T.feedStale(mk(base), NaN), false);
  assert.equal(T.noticeText(mk(base), t + 46 * 60000), 'Last collected: 2026-12-01 06:00 UTC. The last collection was complete.' + staleLine(45) + tail);

  // sample: labelled, and never says the data comes from GitHub
  const sample = T.noticeText(feed, NOW);
  assert.ok(sample.startsWith('SAMPLE (fictional): '), sample);
  assert.ok(!sample.includes('Repository metadata comes from GitHub.'), sample);
  assert.ok(sample.includes('The collection state is unknown.') && sample.includes('Last collected: 2026-11-09 14:32 UTC.'), sample);
  assert.ok(!T.noticeText(mk(base, { sample: 'true' }), t).startsWith('SAMPLE'));
});

test('page text before the script runs is the neutral notice, with no fixed dates or run counts', () => {
  const html = fs.readFileSync(__dirname + '/index.html', 'utf8');
  const m = /<span id="notice">([^<]*)<\/span>/.exec(html);
  assert.ok(m, 'notice span');
  assert.equal(m[1], T.noticeText(null, NOW));
  const app = fs.readFileSync(__dirname + '/app.js', 'utf8');
  for (const src of [html, app]) assert.ok(!/by hand|three read-only|2026-11-10|last run was incomplete/i.test(src));
});

test('empty view message: feed with repositories but no events says so; other cases keep their message', () => {
  const none = 'Nothing here yet.';
  const filtered = 'No rows match the current filters.';
  const noEvents = 'No events yet. The next collection compares against this baseline. The table and forks views list the repositories already found.';
  // [view, rows before filters, repositories, expected]
  const cases = [
    ['feed', 0, 0, none],
    ['feed', 0, 165, noEvents],
    ['feed', 3, 0, filtered],
    ['feed', 3, 165, filtered],
    ['table', 0, 0, none],
    ['table', 0, 165, none],
    ['table', 165, 165, filtered],
    ['forks', 0, 0, none],
    ['forks', 0, 165, none],
    ['forks', 12, 165, filtered],
    ['recent', 0, 0, none],
    ['recent', 0, 165, none],
    ['recent', 165, 165, filtered],
    ['about', 0, 0, null],
    ['about', 0, 165, null],
    ['about', 3, 165, null],
  ];
  for (const [view, total, repoCount, want] of cases) assert.equal(T.emptyMessage(view, total, repoCount), want, view + ' ' + total + ' ' + repoCount);
  // the new sentence uses plain words only
  assert.ok(!/export|collector|json|run_id/i.test(noEvents));
  // the page takes its empty message from emptyMessage and sets it as text
  const app = fs.readFileSync(__dirname + '/app.js', 'utf8');
  assert.ok(/el\('p', 'empty', T\.emptyMessage\(state\.view, all\.length, recs\.repos\.length\)\)/.test(app));
  assert.ok(!app.includes('Nothing here yet.') && !app.includes(noEvents));
});

test('views and commands include recent, with help and completion', () => {
  assert.deepEqual(T.VIEWS, ['feed', 'table', 'forks', 'recent', 'about']);
  for (const c of ['feed', 'table', 'forks', 'recent', 'about']) assert.ok(T.COMMAND_NAMES.includes(c), c);
  assert.match(run('help view').out[0].text, /^view feed\|table\|forks\|recent\|about /);
  assert.match(run('help view').out[0].text, /also: feed, table, forks, recent, about as commands/);
  assert.equal(run('view nope').out[0].text, 'usage: view feed|table|forks|recent|about');
  assert.deepEqual(T.complete('view re', ctx), { line: 'view recent ', options: [] });
  assert.deepEqual(T.complete('rece', ctx), { line: 'recent ', options: [] });
  // typing the shorthand at the prompt waits for Enter instead of searching
  assert.equal(T.liveSearchText('recent'), null);
});

test('entering the recent view with the default sort sorts by first seen, newest first', () => {
  const r = run('recent');
  assert.equal(r.state.view, 'recent');
  assert.deepEqual(r.state.sort, { key: 'first-seen', dir: 'desc' });
  assert.equal(r.out[0].text, 'view: recent  sort: -first-seen');
  assert.deepEqual(run('view recent').state.sort, { key: 'first-seen', dir: 'desc' });
  // a sort the user picked is kept
  const stars = run('sort stars').state;
  assert.deepEqual(run('recent', stars).state.sort, { key: 'stars', dir: 'asc' });
  const oldest = run('order oldest').state;
  assert.deepEqual(run('recent', oldest).state.sort, { key: 'time', dir: 'asc' });
  // inside the view a later sort is the user's, also time desc
  const inRecent = run('sort -time', run('recent').state).state;
  assert.deepEqual(run('view recent', inRecent).state.sort, { key: 'time', dir: 'desc' });
  // other views never change the sort
  for (const v of ['feed', 'table', 'forks', 'about']) assert.deepEqual(run('view ' + v).state.sort, { key: 'time', dir: 'desc' }, v);
  // reset goes back to the feed and the default sort
  assert.deepEqual(run('reset', run('recent').state).state.sort, { key: 'time', dir: 'desc' });
});

test('recent order: first seen, then stars descending, then repository name; unknown last', () => {
  const t = (iso) => Date.parse(iso);
  const mkr = (id, firstSeen, stars, repo) => ({ id, firstSeen, stars, repo });
  const list = [
    mkr('a', t('2026-11-01T00:00:00Z'), 5, 'zeta/z'),
    mkr('b', t('2026-11-03T00:00:00Z'), 1, 'alpha/a'),
    mkr('c', t('2026-11-01T00:00:00Z'), 5, 'Alpha/b'),
    mkr('d', t('2026-11-01T00:00:00Z'), 40, 'mid/m'),
    mkr('e', null, 99, 'none/n'),
    mkr('f', t('2026-11-01T00:00:00Z'), null, 'beta/b'),
  ];
  const desc = T.sortRecent(list, { key: 'first-seen', dir: 'desc' });
  assert.deepEqual(desc.map((r) => r.id), ['b', 'd', 'c', 'a', 'f', 'e']);
  const asc = T.sortRecent(list, { key: 'first-seen', dir: 'asc' });
  assert.deepEqual(asc.map((r) => r.id), ['d', 'c', 'a', 'f', 'b', 'e']);
  // the input is not changed
  assert.deepEqual(list.map((r) => r.id), ['a', 'b', 'c', 'd', 'e', 'f']);
  // the sample feed: every repository, newest first seen first
  const got = T.sortRecent(repos, { key: 'first-seen', dir: 'desc' }).map((r) => r.firstSeen);
  assert.equal(got.length, feed.repos.length);
  assert.ok(got.every((v, i) => i === 0 || got[i - 1] >= v));
});

test('repository records carry the description as plain text, not searched', () => {
  const [r] = T.joinData({ repos: [{ id: 'r9', repo: 'x/y', description: 'A lifted racer' }, { id: 'r8', description: 3 }], events: [] }, NOW).repos;
  assert.equal(r.description, 'A lifted racer');
  assert.equal(T.joinData({ repos: [{ id: 'r8', description: 3 }], events: [] }, NOW).repos[0].description, null);
  assert.equal(T.matches(r, [], 'racer', NOW), false);
});

test('recent view note is plain text and the page builds the view from it', () => {
  assert.equal(T.RECENT_NOTE, 'First seen by this collector. Repositories first listed in a baseline collection all share one time, and the date is not when the repository was created.');
  const app = fs.readFileSync(__dirname + '/app.js', 'utf8');
  assert.ok(app.includes("el('p', 'tablenote', T.RECENT_NOTE)"));
  assert.ok(app.includes("'First seen': 'first-seen'"));
  assert.ok(/state\.view === 'recent' \? T\.sortRecent : T\.sortRecords/.test(app));
  assert.ok(!/innerHTML/.test(app));
});

test('github search link: encoded query, fixed prefix, null for empty, too long or control characters', () => {
  const P = 'https://github.com/search?q=';
  const S = '&type=repositories';
  const cases = [
    ['killzone', P + 'killzone' + S],
    ['  killzone 3  ', P + 'killzone%203' + S],
    ['a b', P + 'a%20b' + S],
    ['a&type=users', P + 'a%26type%3Dusers' + S],
    ['say "hi" it\'s', P + 'say%20%22hi%22%20it%27s' + S],
    ['<script>x</script>', P + '%3Cscript%3Ex%3C%2Fscript%3E' + S],
    ['ゲーム ü', P + '%E3%82%B2%E3%83%BC%E3%83%A0%20%C3%BC' + S],
    ['🎮 port', P + '%F0%9F%8E%AE%20port' + S],
    ['100% done', P + '100%25%20done' + S],
    ['a#b?c/d+e', P + 'a%23b%3Fc%2Fd%2Be' + S],
    ['x'.repeat(100), P + 'x'.repeat(100) + S],
    ['  ' + 'x'.repeat(100) + '  ', P + 'x'.repeat(100) + S],
  ];
  for (const [text, want] of cases) assert.equal(T.githubSearchUrl(text), want, JSON.stringify(text));
  for (const bad of ['', '   ', '\t \n', 'x'.repeat(101), 'a\u0000b', 'a\tb', 'line\nbreak', 'a\u001bb', 'a\u007fb', 'a\u0085b', ' x\n', '\ud83c', null, undefined, 42]) {
    assert.equal(T.githubSearchUrl(bad), null, JSON.stringify(bad));
  }
  // the query part never holds raw spaces, quotes, angle brackets or ampersands, whatever is typed
  for (const text of [' a b ', '"\'<>&', 'x&y=z "q" <b>', '🎮&🎮', '%20 %', 'a'.repeat(99) + '&']) {
    const u = T.githubSearchUrl(text);
    assert.ok(u.startsWith(P) && u.endsWith(S), text);
    assert.ok(!/[ "'<>&]/.test(u.slice(P.length, -S.length)), text);
    assert.equal(decodeURIComponent(u.slice(P.length, -S.length)), text.trim());
    assert.equal(T.safeGithubUrl(u), u);
  }
  assert.equal(T.NO_MATCH_NOTE, 'Not in this list? Our data comes from scheduled collections and may be missing repositories.');
});

test('the page builds the github search link from githubSearchUrl as text, never innerHTML', () => {
  const app = fs.readFileSync(__dirname + '/app.js', 'utf8');
  const start = app.indexOf('function githubSearchLink(');
  assert.ok(start > 0);
  const body = app.slice(start, app.indexOf('\n  }\n', start));
  assert.ok(/const href = T\.githubSearchUrl\(search\);/.test(body));
  assert.ok(/a\.href = href;/.test(body) && /a\.target = '_blank';/.test(body) && /a\.rel = 'noopener noreferrer';/.test(body));
  assert.ok(/el\('a', null, 'Search GitHub for "' \+ shownText \+ '"'\)/.test(body));
  assert.ok(/el\('p', null, T\.NO_MATCH_NOTE\)/.test(body));
  assert.ok(!/innerHTML|outerHTML|insertAdjacentHTML|document\.write/.test(app));
  // it sits under the empty message, and only there
  assert.ok(/T\.emptyMessage\(state\.view, all\.length, recs\.repos\.length\)\)\);\n\s*const more = githubSearchLink\(state\.search\);\n\s*if \(more\) box\.append\(more\);/.test(app));
  assert.equal((app.match(/githubSearchLink\(/g) || []).length, 2);
});
