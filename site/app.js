'use strict';
// Feed logic: polling, rendering, scrolling rules and the prompt. All repository text goes in via textContent.
(function () {
  const T = window.PS3RC;
  const $ = (id) => document.getElementById(id);
  const POLL_MS = 60000;
  const MAX_BACKOFF_MS = 900000;
  const MAX_EVENTS = 500;

  let state = T.initialState();
  let data = null; // last loaded feed
  let outLines = 0; // running line number for command output
  let fromFallback = false; // data is the embedded sample, so the next real feed is a first load
  let preType = null; // search text before the prompt started live searching
  let live = []; // raw events shown
  let held = []; // raw events held while paused
  let seen = new Set();
  let unseen = 0;
  let announce = '';
  let version = null;
  let failures = 0;
  let conn = 'starting';
  let timer = null;
  let demoTimer = null;
  let demoSeq = 0;
  let clockOffset = 0;
  let history = [];
  let histIndex = 0;
  let draft = '';
  let recs = { repos: [], events: [] };

  const now = () => Date.now() + clockOffset;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  // ---------- data loading ----------

  async function getJson(path) {
    if (location.protocol === 'file:') throw new Error('fetch is unavailable on file://');
    const r = await fetch(path, { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }

  const validRec = (r) => !!r && typeof r === 'object' && typeof r.id === 'string' && r.id !== '';

  function validFeed(f) {
    return !!f && Array.isArray(f.repos) && Array.isArray(f.events) && typeof f.generated_at === 'string' &&
      Number.isFinite(Date.parse(f.generated_at)) && f.repos.every(validRec) && f.events.every(validRec);
  }

  function applyFeed(f, v) {
    const first = data === null || fromFallback;
    fromFallback = v === 'embedded-sample';
    data = f;
    version = v;
    if (first) clockOffset = f.sample ? Date.parse(f.generated_at) - Date.now() : 0;
    if (first) {
      live = f.events.slice(0, MAX_EVENTS);
      seen = new Set(live.map((e) => e.id));
      return;
    }
    incoming(f.events);
  }

  async function poll() {
    clearTimeout(timer);
    try {
      const m = await getJson('data/manifest.json');
      if (m.version !== version) {
        const f = await getJson('data/feed.json');
        if (!validFeed(f)) throw new Error('bad feed');
        applyFeed(f, m.version);
      }
      failures = 0;
      conn = 'polling every ' + POLL_MS / 1000 + 's';
    } catch (e) {
      failures += 1;
      if (data === null && validFeed(window.PS3RC_FALLBACK)) {
        applyFeed(window.PS3RC_FALLBACK, 'embedded-sample');
        conn = 'offline, showing embedded sample';
      } else {
        conn = 'retrying (backoff ' + Math.round(nextDelay() / 1000) + 's)';
      }
    }
    try {
      render();
    } finally {
      schedule();
    }
  }

  function nextDelay() {
    return failures ? Math.min(POLL_MS * 2 ** failures, MAX_BACKOFF_MS) : POLL_MS;
  }

  function schedule() {
    clearTimeout(timer);
    if (!document.hidden) timer = setTimeout(poll, nextDelay());
  }

  // ---------- incoming events ----------

  function incoming(events) {
    const fresh = events.filter((e) => !seen.has(e.id));
    if (!fresh.length) return;
    fresh.forEach((e) => seen.add(e.id));
    if (state.paused) {
      held = held.concat(fresh);
      announce = held.length + ' new events held while paused';
      return;
    }
    const box = $('view');
    const before = box.scrollHeight;
    live = fresh.concat(live).slice(0, MAX_EVENTS);
    const atEdge = isAtEdge();
    if (state.scroll === 'auto' && atEdge) {
      unseen = 0;
      announce = fresh.length + ' new events added';
    } else {
      unseen += fresh.length;
      announce = unseen + ' new events (your position is kept)';
    }
    render({ keepFrom: before, edge: atEdge });
  }

  function flushHeld() {
    const h = held;
    held = [];
    h.forEach((e) => seen.delete(e.id));
    announce = '';
    incoming(h);
  }

  const timeAscending = () => state.sort.key === 'time' && state.sort.dir === 'asc';
  function isAtEdge() {
    const box = $('view');
    if (timeAscending()) return box.scrollHeight - box.scrollTop - box.clientHeight < 4;
    return box.scrollTop < 4;
  }

  function demoEvent(i) {
    const repos = data ? data.repos : [];
    if (!repos.length) return null;
    const spec = [
      ['commits_observed', '2 commits observed'],
      ['release_published', 'v9.9.9-demo published'],
      ['fork_activity_observed', 'fork activity'],
      ['collection_stale', 'refresh failure'],
    ][i % 4];
    const t = new Date(now()).toISOString();
    return { id: 'd' + ++demoSeq, type: spec[0], repo_id: repos[(i * 3 + demoSeq) % repos.length].id, source_at: t, observed_at: t, sample: true, summary: '[DEMO] fictional, not real activity of this repository: ' + spec[1] };
  }

  function stopDemo() {
    clearInterval(demoTimer);
    demoTimer = null;
  }
  function startDemo(n) {
    stopDemo();
    let i = 0;
    demoTimer = setInterval(() => {
      const e = i < n ? demoEvent(i++) : null;
      if (!e) return stopDemo();
      incoming([e]);
      render();
    }, 1500);
  }

  // ---------- rendering ----------

  function ctx() {
    const t = now();
    const g = (k) => [...new Set(recs.repos.map((r) => r[k]).filter(Boolean))].sort();
    const stale = data ? T.feedStale(data, t) : false;
    return {
      now: t,
      stale,
      find: (id) => recs.events.concat(recs.repos).find((r) => r.id.toLowerCase() === String(id).toLowerCase()) || null,
      values: { game: g('game'), repo: g('repo'), owner: g('owner'), toolkit: g('toolkit'), id: recs.events.map((e) => e.id).concat(recs.repos.map((r) => r.id)) },
      info: data
        ? [
            'collected: ' + T.fmtTime(Date.parse(data.generated_at)) + ' (' + T.ago(Date.parse(data.generated_at), t) + ')',
            'feed stale: ' + (stale ? 'yes' : 'no') + '   connection: ' + conn,
            'events: ' + live.length + '   held: ' + held.length + '   data: ' + (data.sample ? 'SAMPLE (fictional)' : 'generated'),
          ]
        : ['no data loaded yet'],
    };
  }

  function visibleItems() {
    const t = now();
    let items = state.view === 'feed' ? recs.events : recs.repos;
    if (state.view === 'forks') items = items.filter((r) => r.fork === true);
    const kept = T.dropUntouched(items, state);
    const sort = state.view === 'recent' ? T.sortRecent : T.sortRecords;
    return { all: items, shown: sort(kept.filter((r) => T.matches(r, state.filters, state.search, t)), state.sort), hidden: items.length - kept.length };
  }

  function rowButton(rec, cls) {
    const b = el('button', cls);
    b.type = 'button';
    b.dataset.id = rec.id;
    return b;
  }

  // Column headings that can sort: clicking one runs the same `sort` command as the sort chips.
  const HEAD_SORT = { Time: 'time', Game: 'game', 'Game / inferred title': 'game', 'Game association': 'game', Owner: 'owner', 'Latest commit': 'time', 'Commits 30d': 'commits', Stars: 'stars', Status: 'status', 'First seen': 'first-seen', Repositories: 'repos', 'Total stars': 'stars' };

  function sortHead(tag, label) {
    const node = el(tag);
    const key = HEAD_SORT[label];
    if (!key) {
      node.textContent = label;
      return node;
    }
    const on = state.sort.key === key;
    const b = el('button', 'sorthead', label + (on ? (state.sort.dir === 'asc' ? ' ▲' : ' ▼') : ''));
    b.type = 'button';
    b.title = 'Sort by ' + label.toLowerCase();
    b.addEventListener('click', () => run('sort ' + (on ? (state.sort.dir === 'asc' ? '-' : '') : DESC_FIRST.includes(key) ? '-' : '') + key));
    if (tag === 'th') node.setAttribute('aria-sort', on ? (state.sort.dir === 'asc' ? 'ascending' : 'descending') : 'none');
    node.append(b);
    return node;
  }

  function feedRow(rec) {
    const li = el('li');
    const b = rowButton(rec, 'row');
    b.append(
      el('span', 'time', T.eventTime(rec.time === null ? now() : rec.time, now())),
      el('span', 'label', rec.typeLabel),
      el('span', 'game', T.orUnknown(rec.game)),
      el('span', 'repo', T.orUnknown(rec.repo)),
      el('span', 'stars', T.orUnknown(rec.stars)),
      el('span', 'sum', (rec.summary || '') + (rec.stale ? ' (stale)' : '') + (rec.sample ? ' (sample entry, no link)' : '')),
    );
    li.append(b);
    return li;
  }

  function cell(tr, text) {
    tr.append(el('td', null, text));
  }

  function repoCell(tr, rec) {
    const td = el('td');
    const b = rowButton(rec, 'row');
    b.textContent = T.orUnknown(rec.repo);
    td.append(b);
    tr.append(td);
  }

  function tableView(rows, forks) {
    const wrap = el('div', 'tablewrap');
    const table = el('table');
    table.append(el('caption', null, forks ? 'Fork network (parent comparison is relative to a recorded ref and time; game association is not verified)' : 'Catalog (one row per repository)'));
    const heads = forks
      ? ['Repository', 'Owner', 'Parent', 'Network root', 'Ahead / behind', 'Game association', 'Stars', 'Latest commit']
      : ['Game / inferred title', 'Repository', 'Owner', 'Toolkit', 'Latest commit', 'Commits 30d', 'Stars', 'Fork', 'Status'];
    const tr0 = el('tr');
    heads.forEach((h) => tr0.append(sortHead('th', h)));
    const thead = el('thead');
    thead.append(tr0);
    table.append(thead);
    const tbody = el('tbody');
    for (const r of rows) {
      const tr = el('tr');
      if (forks) {
        repoCell(tr, r);
        [r.owner, r.parent, r.root].forEach((v) => cell(tr, T.orUnknown(v)));
        cell(tr, T.compareLabel(r) + (r.comparison ? ' (' + r.comparison + ')' : ''));
        cell(tr, T.gameLabel(r));
        cell(tr, T.orUnknown(r.stars));
        cell(tr, T.ago(r.time, now()));
      } else {
        cell(tr, T.gameLabel(r));
        repoCell(tr, r);
        cell(tr, T.orUnknown(r.owner));
        cell(tr, T.orUnknown(r.toolkit));
        cell(tr, T.ago(r.time, now()));
        cell(tr, T.orUnknown(r.commits));
        cell(tr, T.orUnknown(r.stars));
        cell(tr, r.fork === null ? 'unknown' : r.fork ? 'FORK' : 'no');
        cell(tr, T.orUnknown(r.status));
      }
      tbody.append(tr);
    }
    table.append(tbody);
    wrap.append(table);
    return wrap;
  }

  // Repositories by when this collector first saw them. Same row rules as the table.
  function recentView(rows) {
    const wrap = el('div', 'tablewrap recent');
    const note = el('p', 'tablenote', T.RECENT_NOTE);
    note.id = 'recent-note';
    wrap.append(note);
    const table = el('table');
    table.append(el('caption', null, 'Recently discovered (one row per repository)'));
    const tr0 = el('tr');
    ['First seen', 'Game', 'Repository', 'Stars', 'Description'].forEach((h) => tr0.append(sortHead('th', h)));
    const thead = el('thead');
    thead.append(tr0);
    table.append(thead);
    const tbody = el('tbody');
    for (const r of rows) {
      const tr = el('tr');
      tr.append(el('td', 'firstseen', T.fmtTime(r.firstSeen)));
      cell(tr, T.gameLabel(r));
      repoCell(tr, r);
      cell(tr, T.orUnknown(r.stars));
      tr.append(el('td', 'desc', r.description || ''));
      tbody.append(tr);
    }
    table.append(tbody);
    wrap.append(table);
    return wrap;
  }

  // One row per game name, from the repositories the table would show.
  function gamesView(repos) {
    const wrap = el('div', 'tablewrap');
    const table = el('table');
    table.append(el('caption', null, 'Games (one row per game name; Label says how the name was obtained; repositories without a game are counted under unknown)'));
    const tr0 = el('tr');
    ['Game', 'Label', 'Repositories', 'Forks', 'Most recent repository', 'Latest commit', 'Total stars'].forEach((h) => tr0.append(sortHead('th', h)));
    const thead = el('thead');
    thead.append(tr0);
    table.append(thead);
    const tbody = el('tbody');
    for (const g of T.gamesRows(repos, state.sort)) {
      const tr = el('tr');
      cell(tr, g.game);
      cell(tr, g.label);
      cell(tr, String(g.repos));
      cell(tr, String(g.forks));
      if (g.latest) repoCell(tr, g.latest);
      else cell(tr, 'unknown');
      cell(tr, g.latest ? T.ago(g.latest.time, now()) : 'unknown');
      cell(tr, T.orUnknown(g.stars));
      tbody.append(tr);
    }
    table.append(tbody);
    wrap.append(table);
    return wrap;
  }

  function aboutView() {
    const d = el('div', 'about');
    const add = (tag, text) => d.append(el(tag, null, text));
    add('h2', 'Disclaimer');
    add('p', 'ps3rc-feed is an automated, unofficial index. Project discovery, game associations, toolkit detection and repository descriptions may be incorrect or outdated. Entries do not verify authorship, endorsement, safety, playability or completion. Activity and timestamps reflect collected GitHub data. Follow source links to evaluate each project.');
    add('h2', 'This build');
    add('p', 'Public GitHub repositories that match PS3 recompilation searches, plus forks of selected root repositories.');
    const n = el('p', null, T.noticeText(data, now()));
    n.id = 'about-notice';
    d.append(n);
    add('h2', 'What "live" means');
    add('p', 'Nothing here is real time. The feed changes only when a new export is published. The page checks a small manifest every 60 seconds (paused in hidden tabs, with backoff on failure) and reloads the feed only when its version changes. Event times are GitHub source times, or collection times when GitHub gives none.');
    add('h2', 'Status values');
    add('p', 'Statuses describe observed activity only: active (activity in the last 30 days), quiet (older), stale (collection is failing), archived. They say nothing about whether a project works. unknown means missing, never zero.');
    add('h2', 'Fork and game labels');
    add('p', 'Fork ancestry, inferred game and detected toolkit are separate. A game inherited from a parent is shown as inherited / unverified. Ahead/behind is relative to a recorded ref and time. The games view has one row per game name, and its Label column says whether the name was inherited, inferred, not inferred, or mixed, with counts.');
    add('h2', 'Keys');
    add('p', 'Type commands at the prompt (help lists them). Tab completes, Up/Down recall history, Esc clears the line, Ctrl+L clears output, / focuses the prompt.');
    return d;
  }

  // Under an empty view after a search: a plain link to GitHub's own search, opened in a new tab. No request from here.
  const SHOW_CHARS = 60;
  function githubSearchLink(search) {
    const href = T.githubSearchUrl(search);
    if (!href) return null;
    const chars = Array.from(search.trim());
    const shownText = chars.length > SHOW_CHARS ? chars.slice(0, SHOW_CHARS).join('') + '...' : chars.join('');
    const d = el('div', 'empty ghsearch');
    const note = el('p', null, T.NO_MATCH_NOTE);
    note.id = 'github-search-note';
    const a = el('a', null, 'Search GitHub for "' + shownText + '"');
    a.id = 'github-search';
    a.href = href;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    const p = el('p');
    p.append(a);
    d.append(note, p);
    return d;
  }

  function render(opts) {
    recs = T.joinData({ repos: data ? data.repos : [], events: live }, now());
    const box = $('view');
    const prevTop = box.scrollTop;
    const { all, shown, hidden } = visibleItems();
    box.replaceChildren();
    box.setAttribute('aria-label', { feed: 'Feed', table: 'Catalog table', forks: 'Fork network', recent: 'Recently discovered', games: 'Games', about: 'About' }[state.view]);

    if (state.view === 'about') {
      box.append(aboutView());
    } else if (!data) {
      box.append(el('p', 'empty', failures ? 'The feed could not be loaded, so nothing is shown. Connection: ' + conn + '.' : 'loading...'));
    } else if (!shown.length) {
      box.append(el('p', 'empty', T.emptyMessage(state.view, all.length, recs.repos.length)));
      const more = githubSearchLink(state.search);
      if (more) box.append(more);
    } else if (state.view === 'feed') {
      const ul = el('ul', 'feed');
      const head = el('li', 'colhead');
      ['Time', 'Type', 'Game', 'Repository', 'Stars', 'Summary'].forEach((h) => head.append(sortHead('span', h)));
      head.children[4].className = 'stars';
      ul.append(head);
      for (const g of T.groupRecords(shown, state.group)) {
        if (g.label !== null) ul.append(el('li', 'head', '== ' + state.group + ': ' + g.label + ' (' + g.items.length + ') =='));
        g.items.forEach((r) => ul.append(feedRow(r)));
      }
      box.append(ul);
    } else if (state.view === 'recent') {
      box.append(recentView(shown));
    } else if (state.view === 'games') {
      box.append(gamesView(shown));
    } else {
      box.append(tableView(shown, state.view === 'forks'));
    }

    keepPosition(box, prevTop, opts);
    renderChrome(all.length, shown.length, hidden);
  }

  function keepPosition(box, prevTop, opts) {
    if (!opts || opts.keepFrom === undefined) {
      box.scrollTop = prevTop;
      return;
    }
    if (timeAscending()) {
      if (state.scroll === 'auto' && opts.edge) box.scrollTop = box.scrollHeight;
      else box.scrollTop = prevTop;
    } else if (state.sort.key === 'time' && (state.scroll === 'manual' || !opts.edge)) {
      box.scrollTop = prevTop + (box.scrollHeight - opts.keepFrom);
    } else {
      box.scrollTop = prevTop;
    }
  }

  function chip(label, onClick, pressed, current) {
    const b = el('button', 'chip', label);
    b.type = 'button';
    if (pressed !== undefined) b.setAttribute('aria-pressed', String(pressed));
    if (current) b.setAttribute('aria-current', 'page');
    b.addEventListener('click', onClick);
    return b;
  }

  const QUICK_FILTERS = ['category:framework', 'category:game', 'category:tool', 'category:unclassified', 'status:active', 'status:quiet', 'status:stale', 'status:archived', 'fork:yes', 'time:<24h', 'time:<7d', 'toolkit:ps3recomp'];
  const QUICK_SORTS = ['time', 'stars', 'commits', 'owner', 'game', 'first-seen'];
  const DESC_FIRST = ['time', 'stars', 'commits', 'first-seen', 'repos'];

  function renderChrome(total, count, hidden) {
    const c = ctx();
    const notice = T.noticeText(data, c.now);
    if ($('notice').textContent !== notice) $('notice').textContent = notice;
    const collected = data ? 'collected ' + T.hhmm(Date.parse(data.generated_at)) + ' UTC (' + T.ago(Date.parse(data.generated_at), c.now) + ')' : '';
    $('statusline').textContent = data
      ? collected + ' | feed stale: ' + (c.stale ? 'yes' : 'no') + ' | connection: ' + conn + ' | times shown are source times, or collection times when the source gives none, UTC'
      : 'connection: ' + conn;
    // the closed summary keeps what needs attention: a stale feed or a failing connection
    $('statusbrief').textContent = data ? collected + (c.stale ? ' | feed stale' : '') + (failures ? ' | connection: ' + conn : '') : 'connection: ' + conn;

    const views = $('views');
    views.replaceChildren(...T.VIEWS.map((v) => chip(v, () => run('view ' + v), undefined, state.view === v)));
    const toggles = $('toggles');
    toggles.replaceChildren(
      chip(state.paused ? 'resume' : 'pause', () => run(state.paused ? 'resume' : 'pause'), state.paused),
      chip('scroll: ' + state.scroll, () => run('scroll ' + (state.scroll === 'auto' ? 'manual' : 'auto'))),
      chip('untouched forks: ' + (state.hideUntouched ? 'hidden' : 'shown'), () => run('untouched ' + (state.hideUntouched ? 'show' : 'hide')), !state.hideUntouched),
      chip('demo', () => run('demo')),
    );
    $('filter-chips').replaceChildren(...QUICK_FILTERS.map((f) => {
      const on = state.filters.some((x) => x.raw === f);
      return chip(f, () => run('filter ' + (on ? '-' : '') + f), on);
    }));
    $('sort-chips').replaceChildren(...QUICK_SORTS.map((k) => {
      const on = state.sort.key === k;
      const label = on ? 'sort ' + k + ' ' + state.sort.dir : 'sort ' + k;
      const next = on ? (state.sort.dir === 'asc' ? '-' : '') : DESC_FIRST.includes(k) ? '-' : '';
      return chip(label, () => run('sort ' + next + k), on);
    }));

    const active = state.filters.length + (state.search ? 1 : 0);
    $('optcount').hidden = active === 0;
    $('optcount').textContent = active + ' active';
    $('active').textContent = 'filters: ' + T.activeSummary(state) + ' | sort: ' + T.sortLabel(state.sort) + ' | group: ' + state.group + (state.view === 'about' ? '' : ' | ' + count + ' of ' + total + ' shown' + (hidden ? ' (' + hidden + ' untouched forks hidden)' : ''));
    $('reset-view').hidden = !T.viewChanged(state);

    const msg = state.paused ? held.length + ' new events held (paused)' : unseen > 0 ? unseen + ' new events (scrolled away)' : announce;
    const nc = $('newcount');
    if (nc.textContent !== msg) nc.textContent = msg;
    $('jump').hidden = unseen === 0 || state.paused;
  }

  // ---------- prompt and output ----------

  function print(line, kind) {
    const out = $('out');
    const d = el('div', line.kind || kind || '');
    d.dataset.n = String(++outLines);
    d.append(document.createTextNode(line.text));
    const href = line.href ? T.safeGithubUrl(line.href) : null;
    if (href) {
      const a = el('a', null, href);
      a.href = href;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      d.append(document.createTextNode(' '), a);
    }
    out.append(d);
    while (out.childNodes.length > 300) out.firstChild.remove();
    out.scrollTop = out.scrollHeight;
    $('outcount').textContent = outLines + (outLines === 1 ? ' line' : ' lines');
  }

  function clearOut() {
    outLines = 0;
    $('out').replaceChildren();
    $('outcount').textContent = '0 lines';
  }

  function run(line) {
    const wasPaused = state.paused;
    const shape = () => JSON.stringify([state.view, state.filters, state.search, state.sort, state.group]);
    const before = shape();
    print({ text: '> ' + line }, 'dim');
    const res = T.execute(state, line, ctx());
    state = res.state;
    res.out.forEach((l) => print(l));
    for (const fx of res.effects) {
      if (fx.type === 'open') {
        const url = T.safeGithubUrl(fx.url);
        if (url) window.open(url, '_blank', 'noopener,noreferrer');
      } else if (fx.type === 'demo') startDemo(fx.n);
      else if (fx.type === 'demo-stop') stopDemo();
      else if (fx.type === 'clear') clearOut();
      else if (fx.type === 'reset') resetData();
    }
    if (wasPaused && !state.paused) flushHeld();
    if (!wasPaused && state.paused) announce = '';
    if (shape() !== before) $('view').scrollTop = 0;
    render();
  }

  function resetData() {
    stopDemo();
    held = [];
    unseen = 0;
    announce = '';
    live = data ? data.events.slice() : [];
    seen = new Set(live.map((e) => e.id));
  }

  function onKey(ev) {
    const input = ev.target;
    if (ev.ctrlKey && ev.key.toLowerCase() === 'l') {
      ev.preventDefault();
      clearOut();
    } else if (ev.key === 'Enter') {
      const line = input.value.trim();
      preType = null;
      input.value = '';
      draft = '';
      if (line) {
        history = T.pushHistory(history, line);
        run(line);
      }
      histIndex = history.length;
    } else if (ev.key === 'Escape') {
      input.value = '';
      preType = null;
      histIndex = history.length;
      if (state.search) run('search');
    } else if (ev.key === 'ArrowUp' || ev.key === 'ArrowDown') {
      ev.preventDefault();
      if (histIndex === history.length) draft = input.value;
      const step = T.historyStep(history, histIndex, ev.key === 'ArrowUp' ? -1 : 1);
      histIndex = step.index;
      input.value = step.text === null ? draft : step.text;
    } else if (ev.key === 'Tab' && !ev.shiftKey && input.value.trim()) {
      const res = T.complete(input.value, ctx());
      if (res.line === input.value && !res.options.length) return;
      ev.preventDefault();
      input.value = res.line;
      if (res.options.length) print({ text: res.options.slice(0, 40).join('  ') + (res.options.length > 40 ? '  ...' : '') }, 'dim');
    }
  }

  // Plain words typed at the prompt filter the list as you type; commands wait for Enter.
  function onType(ev) {
    const text = T.liveSearchText(ev.target.value);
    if (text === null) {
      // the line turned into a command: drop the partial search typed on the way
      if (preType !== null) {
        state = Object.assign({}, state, { search: preType });
        preType = null;
        render();
      }
      return;
    }
    if (text === state.search) return;
    if (preType === null) preType = state.search;
    state = Object.assign({}, state, { search: text });
    $('view').scrollTop = 0;
    render();
  }

  // ---------- wiring ----------

  function init() {
    $('cmd').addEventListener('keydown', onKey);
    $('cmd').addEventListener('input', onType);
    $('reset-view').addEventListener('click', () => {
      if (preType !== null) {
        // a search typed at the prompt is part of what reset clears
        $('cmd').value = '';
        preType = null;
      }
      run('reset view');
      $('view').focus(); // the button hides itself, so focus goes to the list
    });
    $('jump').addEventListener('click', () => {
      const box = $('view');
      box.scrollTop = timeAscending() ? box.scrollHeight : 0;
      unseen = 0;
      announce = '';
      render();
    });
    $('view').addEventListener('click', (ev) => {
      const b = ev.target.closest('button[data-id]');
      if (b) run('show ' + b.dataset.id);
    });
    $('view').addEventListener('scroll', () => {
      if (unseen > 0 && state.scroll === 'auto' && isAtEdge()) {
        unseen = 0;
        render();
      }
    });
    document.addEventListener('keydown', (ev) => {
      if (ev.key === '/' && !['INPUT', 'TEXTAREA'].includes(ev.target.tagName) && !ev.ctrlKey && !ev.metaKey) {
        ev.preventDefault();
        $('cmd').focus();
      }
    });
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) clearTimeout(timer);
      else poll();
    });
    print({ text: 'ps3rc-feed: unofficial index of public GitHub repositories, not real time. Type help or about. Press / to focus the prompt.' }, 'dim');
    poll();
  }

  init();
})();
