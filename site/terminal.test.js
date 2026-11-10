'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const T = require('./terminal.js');

const feed = JSON.parse(fs.readFileSync(__dirname + '/data/feed.json', 'utf8'));
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
  for (const c of ['help', 'filter', 'sort', 'group', 'search', 'show', 'open', 'pause', 'resume', 'order', 'scroll', 'subscribe', 'demo', 'status', 'about', 'clear', 'reset']) {
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
  const fallback = fs.readFileSync(__dirname + '/data/feed-fallback.js', 'utf8');
  assert.equal(fallback.trim(), 'window.PS3RC_FALLBACK = ' + JSON.stringify(feed) + ';');
  assert.equal(feed.sample, true);
  const manifest = JSON.parse(fs.readFileSync(__dirname + '/data/manifest.json', 'utf8'));
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
