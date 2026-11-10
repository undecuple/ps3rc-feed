# ps3rc-feed site

Static page for ps3rc-feed, an automated and unofficial activity feed of PS3 recompilation projects on GitHub.

Status: `data/` holds a real feed exported with `python3 -m collector export`; it is not real time. It indexes public GitHub repositories that match PS3 recompilation searches and forks of selected root repositories. The page builds its notice (collection time, complete or incomplete, repositories not fetched, errors, stale) from the feed's `generated_at`, `collection` and `stale_after_minutes` with `noticeText` in `terminal.js`; when `collection` is missing or does not check out it says the collection state is unknown. A listing does not mean a project is playable, official, safe or endorsed. Game, category and toolkit labels are inferred and may be wrong. No game files are hosted. Links go through the https://github.com check. The RSS routes it prints are proposed only.

Serve the folder from any static host (no build step, no external requests). Over file:// `fetch` is unavailable, and `data/feed-fallback.js` is `window.PS3RC_FALLBACK = null;`, so the page shows that the feed could not be loaded instead of any data.

`testdata/` holds the fictional sample (every name, number and date is made up; entries are flagged `sample: true` and carry no links). It drives the tests and the browser check and is not deployed. Its `feed-fallback.js` must equal its `feed.json`; regenerate it inside `testdata/` with:

    node -e "const f=require('fs');f.writeFileSync('feed-fallback.js','window.PS3RC_FALLBACK = '+JSON.stringify(JSON.parse(f.readFileSync('feed.json','utf8')))+';\n')"

Tests (Node 20 or newer, no dependencies), run inside this folder:

    node --test

Browser check (headless Chromium, local only): `sh tools/browser_check.sh`. It serves two temporary copies of this folder on 127.0.0.1, one with `testdata/` copied into `data/` (sample) and one as is (real feed), and also opens the real copy over file:// to check the failed-load state. A third served copy has the real feed with its events emptied: the feed view must say there are no events yet and the table view must list the repositories. Screenshots go to `shots/` (ignored by git).

When a view has no rows, `emptyMessage` in `terminal.js` picks the text: a feed with repositories but no events (as after a first collection) says so and points to the table and forks views; otherwise it is `Nothing here yet.`, or `No rows match the current filters.` when filters hide every row.

Type `help` at the prompt for the commands.

Look: a dark window with a grey title bar and a green accent, set in IBM Plex Mono (fonts in `fonts/`, SIL Open Font License 1.1, see `fonts/OFL.txt`), on the tiled background `carpet.svg`. The filter prompt at the bottom is a local command parser, never a shell.
