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

When a search leaves the feed, table, forks, recent or games view empty, a link under that message opens GitHub's own repository search for the same text in a new tab. `githubSearchUrl` in `terminal.js` builds the link, and the page itself makes no request for it.

The `recent` view (or `view recent`) lists repositories by the time this collector first saw them, from each repository's `first_seen_at`. Ties go to more stars, then to the repository name. Entering the view with the default sort (`-time`) switches to `-first-seen`; a sort you picked yourself is kept. Filters, search and the untouched forks setting work as in the table. Repositories found in a baseline collection share one time, and that time is not when a repository was created. The browser check opens this view on both served copies at desktop and phone width.

The `games` view (or `view games`) has one row per game name (the repository's `game`), and repositories without a game share one `unknown` row at the end. A Label column says how the name was obtained from the row's repositories: `inherited / unverified` or `inferred` when all of them are, `not inferred` when none is either, otherwise `mixed` with the counts (for example `mixed: 26 inherited / unverified, 1 inferred`), so a mixed or inherited row is never shown as confirmed. Each row shows the number of repositories and of forks, the repository with the newest commit and its time, and the total of known stars. `gamesRows` in `terminal.js` builds the rows from the repositories the table would show, so filters, search and the untouched forks setting apply. Rows go by repository count, most first, then by name; entering the view with the default sort switches to `-repos` (leaving it with that sort goes back to `-time`), and the Game, Repositories, Latest commit and Total stars headings sort by name, count, newest commit and stars. The `unknown` row stays last in every order.

Type `help` at the prompt for the commands.

No JavaScript: both deploying workflows (`pages.yml` and the refresh deploy) run `node site/tools/render_static.js out/data/feed.json out/static.html` after the feed overlay, and `index.html` links the result as "no JavaScript? static list". It is one HTML file with no script and no inline style (it links `style.css`): the notice from `noticeText`, the disclaimer, the footer links and the 200 most recently discovered repositories (`sortRecent` by first seen, untouched forks dropped with `dropUntouched`), with a note when more exist. Repository links are only `https://github.com/<owner>/<repo>` URLs that pass `safeGithubUrl`. It is rendered once per deployment and does not update itself. Preview it locally with `node tools/render_static.js data/feed.json /tmp/static.html 2026-01-01T00:00:00Z` (the last argument fixes the time, so the output is the same on every run).

Look: a dark window with a grey title bar and a green accent, set in IBM Plex Mono (fonts in `fonts/`, SIL Open Font License 1.1, see `fonts/OFL.txt`), on the tiled background `carpet.svg`. The filter prompt at the bottom is a local command parser, never a shell.
