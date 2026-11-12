#!/usr/bin/env node
// Renders the Atom feed: newly discovered repositories and published releases, newest first.
// Usage: node site/tools/render_atom.js <feed.json> <out.atom>
// Same input, same output: every time in the file comes from the feed, never from the clock.
'use strict';
const fs = require('node:fs');
const T = require('../terminal.js');
const { repoHref, DISCLAIMER } = require('./render_static.js');

const SITE = 'https://undecuple.github.io/ps3rc-feed/';
const TYPES = new Set(['repository_discovered', 'release_published']);
const CAP = 100;
const EPOCH = '1970-01-01T00:00:00Z';

const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;' };
// XML 1.0 allows tab, newline, carriage return and the printable ranges only; anything else is dropped.
const esc = (s) => String(s).replace(/[^\t\n\r -퟿-�\u{10000}-\u{10FFFF}]/gu, '').replace(/[&<>"']/g, (c) => ESC[c]);
const iso = (ms) => new Date(ms).toISOString().replace('.000Z', 'Z');

function entry(rec) {
  const href = repoHref(rec);
  const name = T.orUnknown(rec.repo);
  const parts = [rec.summary || rec.typeLabel];
  const game = T.gameLabel(rec);
  if (rec.game) parts.push('Game: ' + game + '.');
  parts.push('Automated and unverified.');
  // A release carries the time GitHub gave it; a discovery has none, so its time is when the collector first saw it.
  const published = Number.isFinite(rec.sourceTime) ? rec.sourceTime : rec.observedAt;
  return [
    '<entry>',
    '<id>urn:ps3rc-feed:event:' + esc(encodeURIComponent(rec.id)) + '</id>',
    '<title>' + esc(name + ': ' + (rec.summary || rec.typeLabel)) + '</title>',
    '<published>' + iso(published) + '</published>',
    '<updated>' + iso(Math.max(published, rec.observedAt)) + '</updated>',
    ...(href ? ['<link rel="alternate" href="' + esc(href) + '"/>'] : []),
    '<category term="' + esc(rec.type) + '"/>',
    '<summary type="text">' + esc(parts.join(' ')) + '</summary>',
    '</entry>',
  ].join('\n');
}

// Returns the Atom document for a parsed feed. Events without a usable observed_at are left out.
function render(feed) {
  const data = feed && typeof feed === 'object' && !Array.isArray(feed) ? feed : {};
  const events = T.joinData(data, 0).events
    .filter((e) => TYPES.has(e.type) && !e.sample && Number.isFinite(e.observedAt))
    .sort((a, b) => b.observedAt - a.observedAt || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
    .slice(0, CAP);
  const generated = Date.parse(data.generated_at);
  const updated = Number.isFinite(generated) ? iso(generated) : events.length ? iso(events[0].observedAt) : EPOCH;
  return [
    '<?xml version="1.0" encoding="utf-8"?>',
    '<feed xmlns="http://www.w3.org/2005/Atom">',
    '<id>' + SITE + '</id>',
    '<title>ps3rc-feed</title>',
    '<subtitle>' + esc('Newly discovered PS3 recompilation repositories and releases. ' + DISCLAIMER) + '</subtitle>',
    '<updated>' + updated + '</updated>',
    '<link rel="alternate" href="' + SITE + '"/>',
    '<link rel="self" href="' + SITE + 'feed.atom"/>',
    ...events.map(entry),
    '</feed>',
    '',
  ].join('\n');
}

function main(argv) {
  if (argv.length !== 2) {
    process.stderr.write('usage: node render_atom.js <feed.json> <out.atom>\n');
    return 2;
  }
  const feed = JSON.parse(fs.readFileSync(argv[0], 'utf8'));
  fs.writeFileSync(argv[1], render(feed));
  return 0;
}

if (require.main === module) process.exitCode = main(process.argv.slice(2));

module.exports = { render, TYPES, CAP, SITE };
