# ps3rc-feed site

Static page for ps3rc-feed, an automated and unofficial activity feed of PS3 recompilation projects on GitHub.

Status: design preview with sample data. Every name, number and date in `data/` is fictional. The sample data has no links: sample entries are flagged `sample: true`, carry no URL, and `open` / `show` print "sample entry: no real link". Real (non-sample) entries still go through the https://github.com check. Nothing here is deployed, and the RSS routes it prints are proposed only.

Open `index.html` directly (file://) or serve the folder from any static host. No build step, no external requests. When `fetch` is unavailable (file://) the page falls back to `data/feed-fallback.js`, which must equal `data/feed.json`; regenerate it with:

    node -e "const f=require('fs');f.writeFileSync('data/feed-fallback.js','window.PS3RC_FALLBACK = '+JSON.stringify(JSON.parse(f.readFileSync('data/feed.json','utf8')))+';\n')"

Tests (Node 20 or newer, no dependencies), run inside this folder:

    node --test

Type `help` at the prompt for the commands.

Look: a dark window with a grey title bar and a green accent, set in IBM Plex Mono (fonts in `fonts/`, SIL Open Font License 1.1, see `fonts/OFL.txt`), on the tiled background `carpet.svg`. The filter prompt at the bottom is a local command parser, never a shell.
