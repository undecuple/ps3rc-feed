#!/usr/bin/env node
// Renders the no-JavaScript page: the notice, the disclaimer and the most recently discovered repositories.
// Usage: node site/tools/render_static.js <feed.json> <out.html> [now-ISO]
// Filtering, the untouched-fork rule, the order and the notice wording all come from terminal.js.
'use strict';
const fs = require('node:fs');
const T = require('../terminal.js');

const CAP = 200;
const SORT = { key: 'first-seen', dir: 'desc' };

const DISCLAIMER = 'Automated, unofficial index. Associations, toolkit detection and activity may be wrong or out of date. A listing does not mean a project is playable, official, safe or endorsed.';

// The same links as the footer of index.html (a test keeps them in step).
const FOOTER = [
  ['https://github.com/undecuple/ps3rc-feed', 'Source and README'],
  ['https://github.com/undecuple/ps3rc-feed/tree/feed-history', 'History archive'],
  ['https://github.com/undecuple/ps3rc-feed/blob/main/docs/history.md', 'How to read the history'],
];

const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ESC[c]);

// A link only when the app would make one (T.safeGithubUrl) and it is exactly https://github.com/<owner>/<repo>.
function repoHref(rec) {
  if (rec.sample) return null;
  const href = T.safeGithubUrl(rec.url);
  if (!href) return null;
  const u = new URL(href);
  return !u.search && !u.hash && /^\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(u.pathname) ? href : null;
}

function row(rec) {
  const href = repoHref(rec);
  const name = esc(T.orUnknown(rec.repo));
  const kind = rec.fork === null ? 'unknown' : rec.fork ? 'fork' : 'not a fork';
  return (
    '<tr>' +
    '<td>' + (href ? '<a href="' + esc(href) + '" rel="noopener noreferrer">' + name + '</a>' : name) + '</td>' +
    '<td>' + esc(T.gameLabel(rec)) + '</td>' +
    '<td>' + kind + '</td>' +
    '<td class="firstseen">' + esc(T.fmtTime(rec.firstSeen)) + '</td>' +
    '<td class="firstseen">' + esc(T.fmtTime(rec.time)) + '</td>' +
    '<td>' + esc(T.orUnknown(rec.stars)) + '</td>' +
    '</tr>'
  );
}

// Returns the page for a parsed feed at time now (ms). Same input, same output.
function render(feed, now) {
  const data = feed && typeof feed === 'object' && !Array.isArray(feed) ? feed : {};
  const repos = T.joinData(data, now).repos;
  const kept = T.dropUntouched(repos, T.initialState());
  const sorted = T.sortRecent(kept, SORT);
  const shown = sorted.slice(0, CAP);

  const facts = ['Rendered ' + T.fmtTime(now) + ' when the page was deployed; it does not update itself.'];
  facts.push(repos.length + (repos.length === 1 ? ' repository' : ' repositories') + ' in the feed.');
  const hidden = repos.length - kept.length;
  if (hidden > 0) facts.push(hidden + ' untouched ' + (hidden === 1 ? 'fork' : 'forks') + ' not listed (no stars, and no commits in 30 days or nothing ahead of the parent).');
  if (sorted.length > CAP) facts.push('Showing the ' + CAP + ' most recently discovered of ' + sorted.length + '; the interactive page lists them all.');

  const body = shown.length
    ? shown.map(row).join('\n')
    : '<tr><td colspan="6" class="empty">' + esc(T.emptyMessage('recent', kept.length, repos.length)) + '</td></tr>';

  return [
    '<!doctype html>',
    '<html lang="en">',
    '<head>',
    '<meta charset="utf-8">',
    '<meta name="viewport" content="width=device-width, initial-scale=1">',
    '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'self\'; img-src \'self\' data:; font-src \'self\'; base-uri \'none\'; form-action \'none\'">',
    '<meta name="description" content="ps3rc-feed static list: recently discovered PS3 recompilation repositories on GitHub. Not real time.">',
    '<title>ps3rc-feed static list</title>',
    '<link rel="icon" href="favicon.svg" type="image/svg+xml">',
    '<link rel="stylesheet" href="style.css">',
    '</head>',
    '<body>',
    '<main class="term">',
    '<header class="titlebar"><span class="dots" aria-hidden="true"><i></i><i></i><i></i></span><h1>ps3rc-feed :: static list</h1></header>',
    '<p class="lede">Public GitHub projects that rebuild PlayStation 3 games to run natively (static recompilation), most recently discovered first. This is the version of the page without JavaScript.</p>',
    '<section class="notice" aria-label="Notices">',
    '<p class="badge"><strong>NOT REAL TIME</strong> <span>' + esc(T.noticeText(data, now)) + '</span></p>',
    '<p class="disclaimer">' + esc(DISCLAIMER) + '</p>',
    '<p class="statusline">' + esc(facts.join(' ')) + '</p>',
    '</section>',
    '<div class="tablewrap recent">',
    '<p class="tablenote">' + esc(T.RECENT_NOTE) + '</p>',
    '<table>',
    '<caption>Recently discovered (one row per repository)</caption>',
    '<thead><tr><th scope="col">Repository</th><th scope="col">Game</th><th scope="col">Kind</th><th scope="col">First seen</th><th scope="col">Last commit</th><th scope="col">Stars</th></tr></thead>',
    '<tbody>',
    body,
    '</tbody>',
    '</table>',
    '</div>',
    '<footer class="links">',
    '<a href="index.html">Interactive page</a>',
    ...FOOTER.map(([href, text]) => '<a href="' + esc(href) + '">' + esc(text) + '</a>'),
    '</footer>',
    '</main>',
    '</body>',
    '</html>',
    '',
  ].join('\n');
}

function main(argv) {
  if (argv.length < 2 || argv.length > 3) {
    process.stderr.write('usage: node render_static.js <feed.json> <out.html> [now-ISO]\n');
    return 2;
  }
  const now = argv.length === 3 ? Date.parse(argv[2]) : Date.now();
  if (!Number.isFinite(now)) {
    process.stderr.write('now is not a date: ' + argv[2] + '\n');
    return 2;
  }
  const feed = JSON.parse(fs.readFileSync(argv[0], 'utf8'));
  fs.writeFileSync(argv[1], render(feed, now));
  return 0;
}

if (require.main === module) process.exitCode = main(process.argv.slice(2));

module.exports = { render, repoHref, CAP, DISCLAIMER, FOOTER };
