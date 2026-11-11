'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const A = require('./tools/render_atom.js');

const SCRIPT = path.join(__dirname, 'tools', 'render_atom.js');
const GEN = '2026-11-10T12:00:00Z';

function repo(i, extra) {
  return Object.assign({
    id: 'r' + i, repo: 'owner' + i + '/repo' + i, owner: 'owner' + i, url: 'https://github.com/owner' + i + '/repo' + i,
    fork: false, stars: i, game: 'Game ' + i,
  }, extra);
}
function event(i, type, minutes, extra) {
  return Object.assign({
    id: 'repo-' + i + ':' + type, type, repo_id: 'r' + i, summary: type + ' ' + i,
    observed_at: new Date(Date.parse(GEN) - minutes * 60000).toISOString(), source_at: null,
  }, extra);
}
const feedOf = (repos, events) => ({ generated_at: GEN, repos, events });
const entries = (xml) => xml.split('<entry>').length - 1;

test('only discoveries and releases become entries, newest first', () => {
  const xml = A.render(feedOf([repo(1), repo(2), repo(3)], [
    event(1, 'repository_discovered', 30),
    event(2, 'release_published', 10),
    event(3, 'commits_observed', 5),
    event(3, 'game_association_changed', 4),
    event(3, 'fork_discovered', 3),
  ]));
  assert.equal(entries(xml), 2);
  assert.ok(xml.indexOf('release_published 2') < xml.indexOf('repository_discovered 1'));
  assert.ok(xml.includes('<updated>2026-11-10T12:00:00Z</updated>'));
});

test('hostile text is escaped, control characters dropped, and only safe links appear', () => {
  const evil = '<script>alert(1)</script>"\'&\u0000\u0008';
  const xml = A.render(feedOf([
    repo(1, { repo: evil, game: evil }),
    repo(2, { url: 'javascript:alert(1)' }),
    repo(3, { url: 'https://evil.example/owner3/repo3' }),
    repo(4, { sample: true }),
  ], [
    event(1, 'repository_discovered', 1, { summary: evil }),
    event(2, 'repository_discovered', 2),
    event(3, 'repository_discovered', 3),
    event(4, 'repository_discovered', 4),
  ]));
  assert.ok(!xml.includes('<script'));
  assert.ok(!/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(xml));
  assert.ok(xml.includes('&lt;script&gt;alert(1)&lt;/script&gt;&quot;&apos;&amp;'));
  const links = [...xml.matchAll(/<link rel="alternate" href="([^"]*)"\/>/g)].map((m) => m[1]).filter((h) => h.includes('owner'));
  assert.deepEqual(links, ['https://github.com/owner1/repo1']);
  assert.equal(entries(xml), 3); // the sample repository's event is left out
});

test('ids are stable, unique and url-safe; entries without a usable time are skipped', () => {
  const feed = feedOf([repo(1), repo(2)], [
    event(1, 'repository_discovered', 5),
    event(2, 'repository_discovered', 5, { observed_at: 'not a date' }),
    event(2, 'release_published', 5, { id: 'repo-2:release:9 &x' }),
  ]);
  const xml = A.render(feed);
  assert.equal(xml, A.render(feed));
  assert.equal(entries(xml), 2);
  const ids = [...xml.matchAll(/<id>urn:ps3rc-feed:event:([^<]*)<\/id>/g)].map((m) => m[1]);
  assert.deepEqual(ids.sort(), ['repo-1%3Arepository_discovered', 'repo-2%3Arelease%3A9%20%26x']);
});

test('the feed is capped, and an empty or malformed feed still gives a valid document', () => {
  const many = Array.from({ length: A.CAP + 20 }, (_, i) => event(1, 'release_published', i, { id: 'e' + i }));
  assert.equal(entries(A.render(feedOf([repo(1)], many))), A.CAP);
  for (const bad of [null, [], 'x', {}, { events: 'x', repos: 3 }]) {
    const xml = A.render(bad);
    assert.ok(xml.startsWith('<?xml version="1.0" encoding="utf-8"?>\n<feed xmlns="http://www.w3.org/2005/Atom">'));
    assert.ok(xml.includes('<updated>1970-01-01T00:00:00Z</updated>'));
    assert.equal(entries(xml), 0);
  }
});

test('the command line writes the file and refuses the wrong arguments', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'atom-'));
  try {
    const input = path.join(dir, 'feed.json');
    const out = path.join(dir, 'feed.atom');
    fs.writeFileSync(input, JSON.stringify(feedOf([repo(1)], [event(1, 'repository_discovered', 1)])));
    execFileSync(process.execPath, [SCRIPT, input, out]);
    assert.equal(fs.readFileSync(out, 'utf8'), A.render(JSON.parse(fs.readFileSync(input, 'utf8'))));
    assert.throws(() => execFileSync(process.execPath, [SCRIPT, input], { stdio: 'pipe' }), (e) => e.status === 2);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
