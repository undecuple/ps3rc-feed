'use strict';
// Pure command parser, filter/sort engine and tab completion for ps3rc-feed.
// No DOM, no eval, no shell: every command is looked up in a fixed table.
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.PS3RC = api;
})(typeof self !== 'undefined' ? self : this, function () {
  const DAY = 86400000;

  const TYPES = [
    ['repository_discovered', 'FOUND'],
    ['commits_observed', 'COMMIT'],
    ['release_published', 'RELEASE'],
    ['repository_archived', 'ARCHIVED'],
    ['collection_stale', 'STALE'],
    ['collection_recovered', 'RECOVERED'],
    ['fork_discovered', 'FORK'],
    ['fork_activity_observed', 'FORKACT'],
    ['fork_divergence_changed', 'DIVERGE'],
    ['game_association_changed', 'REASSOC'],
  ].map(([id, label]) => ({ id, label }));

  const ENUMS = {
    category: ['framework', 'game', 'tool', 'unclassified'],
    status: ['active', 'quiet', 'stale', 'archived'],
    fork: ['yes', 'no'],
    type: TYPES.map((t) => t.label.toLowerCase()),
  };
  const FIELDS = ['game', 'repo', 'owner', 'author', 'category', 'status', 'type', 'toolkit', 'fork', 'time', 'stars'];
  const TEXT_FIELDS = ['game', 'repo', 'owner', 'toolkit'];
  const SORT_KEYS = ['time', 'owner', 'author', 'game', 'category', 'status', 'stars', 'commits', 'first-seen'];
  const GROUPS = ['none', 'game', 'owner', 'category', 'status', 'toolkit', 'type'];
  const VIEWS = ['feed', 'table', 'forks', 'about'];
  const ROUTES = [
    '/feed.xml',
    '/feeds/frameworks/ps3recomp.xml',
    '/feeds/forks.xml',
    '/feeds/games/<game-key>.xml',
    '/feeds/repos/<repository-id>.xml',
  ];

  const HELP = {
    help: ['help [command]', 'list commands'],
    filter: ['filter field:value ...', 'fields: game repo owner(author) category status type toolkit fork time stars. -field:value removes, "clear" resets'],
    sort: ['sort [-]key | key asc|desc', 'keys: time owner(author) game category status stars commits first-seen. Leading - means descending'],
    group: ['group none|game|owner|category|status|toolkit|type', 'group rows under headings'],
    search: ['search text', 'match words in game, repo, owner, toolkit, summary; empty clears. Plain text typed at the prompt is a search too'],
    show: ['show <id>', 'details of an event (e..) or repository (r..)'],
    open: ['open <id>', 'print the source link and open it (https://github.com only)'],
    pause: ['pause', 'hold incoming events and count them'],
    resume: ['resume', 'insert held events'],
    order: ['order newest|oldest', 'time order of the list'],
    scroll: ['scroll auto|manual', 'auto follows the newest edge; manual never moves your view'],
    view: ['view feed|table|forks|about', 'also: feed, table, forks, about as commands'],
    subscribe: ['subscribe', 'print the proposed RSS routes'],
    demo: ['demo [n|stop]', 'insert fictional demo events one by one'],
    status: ['status', 'feed freshness and current settings'],
    about: ['about', 'full disclaimer'],
    clear: ['clear', 'clear the output (Ctrl+L)'],
    reset: ['reset', 'reset filters, view and settings; drop demo events'],
  };
  const EXAMPLES = {
    filter: ['filter category:framework status:active', 'filter owner:example time:<7d', 'filter time:>7d stars:>10', 'filter fork:yes game:unknown'],
    sort: ['sort -time', 'sort author', 'sort stars desc'],
  };
  const COMMAND_NAMES = Object.keys(HELP).concat(['feed', 'table', 'forks']).sort();

  function initialState() {
    return { view: 'feed', filters: [], search: '', sort: { key: 'time', dir: 'desc' }, group: 'none', paused: false, scroll: 'auto' };
  }

  // ---------- parsing ----------

  function tokenize(line) {
    const tokens = [];
    let cur = null;
    let quote = false;
    for (let i = 0; i < line.length; i++) {
      const c = line[i];
      if (c === '"') {
        quote = !quote;
        if (!cur) cur = { text: '', start: i, end: i };
        cur.end = i + 1;
        continue;
      }
      if (!quote && /\s/.test(c)) {
        if (cur) tokens.push(cur);
        cur = null;
        continue;
      }
      if (!cur) cur = { text: '', start: i, end: i };
      cur.text += c;
      cur.end = i + 1;
    }
    if (cur) tokens.push(cur);
    return tokens;
  }

  function canonField(name) {
    const f = String(name).toLowerCase();
    if (f === 'author') return 'owner';
    return FIELDS.includes(f) ? f : null;
  }

  function parseDuration(s) {
    const m = /^(\d+)(m|h|d|w)$/.exec(s);
    if (!m) return null;
    return Number(m[1]) * { m: 60000, h: 3600000, d: DAY, w: 7 * DAY }[m[2]];
  }

  function parseTime(value) {
    if (value === 'today') return { kind: 'today' };
    if (value === 'unknown') return { kind: 'unknown' };
    const m = /^([<>]?)(\d+[mhdw])$/.exec(value);
    const ms = m && parseDuration(m[2]);
    if (!m || ms === null) return null;
    return { kind: 'age', op: m[1] || '<', ms };
  }

  function quoteIfNeeded(s) {
    return /\s/.test(s) ? '"' + s + '"' : s;
  }

  // Returns a term object, or { error }.
  function parseTerm(token) {
    const i = token.indexOf(':');
    if (i < 1) return { error: 'expected field:value, got "' + clip(token) + '"' };
    const field = canonField(token.slice(0, i));
    if (!field) return { error: 'unknown field "' + clip(token.slice(0, i)) + '" (' + FIELDS.join(' ') + ')' };
    const value = token.slice(i + 1).trim().toLowerCase();
    if (!value) return { error: 'missing value for ' + field };
    const term = { field, value, raw: field + ':' + quoteIfNeeded(value) };
    if (value === 'unknown') return term;
    if (field === 'time') {
      const t = parseTime(value);
      if (!t) return { error: 'time needs <24h, >7d, 24h or today' };
      return Object.assign(term, t);
    }
    if (field === 'stars') {
      const m = /^(<=|>=|<|>|=)?(\d+)$/.exec(value);
      if (!m) return { error: 'stars needs a number, e.g. stars:>10' };
      return Object.assign(term, { op: m[1] || '=', num: Number(m[2]) });
    }
    if (field === 'fork') {
      const yes = value === 'yes' || value === 'true';
      if (!yes && value !== 'no' && value !== 'false') return { error: 'fork needs yes or no' };
      term.value = yes ? 'yes' : 'no';
      term.raw = 'fork:' + term.value;
    } else if (field === 'type') {
      if (!TYPES.some((t) => typeMatches(value, t.id))) return { error: 'unknown type; try ' + ENUMS.type.join(' ') };
    } else if (ENUMS[field] && !ENUMS[field].includes(value)) {
      return { error: field + ' values: ' + ENUMS[field].join(' ') + ' unknown' };
    }
    return term;
  }

  function parseSort(args) {
    if (!args.length) return { error: 'usage: sort [-]key (keys: ' + SORT_KEYS.join(' ') + ')' };
    let key = args[0].toLowerCase();
    let dir = 'asc';
    if (key[0] === '-') {
      dir = 'desc';
      key = key.slice(1);
    }
    if (!SORT_KEYS.includes(key)) return { error: 'unknown sort key "' + clip(key) + '" (' + SORT_KEYS.join(' ') + ')' };
    if (args[1]) {
      const d = args[1].toLowerCase();
      if (d !== 'asc' && d !== 'desc') return { error: 'direction must be asc or desc' };
      dir = d;
    }
    return { key: key === 'author' ? 'owner' : key, dir };
  }

  function clip(s) {
    return s.length > 40 ? s.slice(0, 40) + '...' : s;
  }

  // Only https://github.com URLs without credentials or port are links.
  function safeGithubUrl(url) {
    if (typeof url !== 'string' || url.length > 2048) return null;
    let u;
    try {
      u = new URL(url);
    } catch (e) {
      return null;
    }
    if (u.protocol !== 'https:' || u.hostname !== 'github.com' || u.port || u.username || u.password) return null;
    return u.href;
  }

  // ---------- data ----------

  function typeLabel(id) {
    const t = TYPES.find((x) => x.id === id);
    return t ? t.label : 'UNKNOWN';
  }

  function typeMatches(value, id) {
    const v = String(value).toLowerCase();
    return v === id || v === typeLabel(id).toLowerCase();
  }

  const str = (v) => (typeof v === 'string' && v.trim() ? v : null);
  const num = (v) => (typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : null);
  const oneOf = (v, list) => (typeof v === 'string' && list.includes(v) ? v : null);
  const ms = (v) => {
    const t = typeof v === 'string' ? Date.parse(v) : NaN;
    return Number.isNaN(t) ? null : t;
  };

  function deriveStatus(repo, now) {
    if (repo.archived) return 'archived';
    if (repo.collection_failing) return 'stale';
    const last = ms(repo.last_commit_at);
    if (last === null) return null;
    return now - last <= 30 * DAY ? 'active' : 'quiet';
  }

  // Joins events with their repository into flat records for filtering.
  function joinData(data, now) {
    const events = Array.isArray(data.events) ? data.events : [];
    const repos = Array.isArray(data.repos) ? data.repos : [];
    const typesByRepo = new Map();
    for (const e of events) {
      if (!typesByRepo.has(e.repo_id)) typesByRepo.set(e.repo_id, []);
      typesByRepo.get(e.repo_id).push(e.type);
    }
    const repoRecs = repos.map((r) => ({
      kind: 'repo',
      id: String(r.id),
      repoId: r.id,
      repo: str(r.repo),
      owner: str(r.owner),
      game: str(r.game),
      gameInferred: r.game_inferred !== false,
      gameInherited: r.game_inherited === true,
      category: oneOf(r.category, ENUMS.category),
      status: deriveStatus(r, now),
      toolkit: str(r.toolkit),
      fork: typeof r.fork === 'boolean' ? r.fork : null,
      parent: str(r.parent),
      root: str(r.root),
      ahead: num(r.ahead),
      behind: num(r.behind),
      comparison: str(r.comparison_state),
      stars: num(r.stars),
      commits: num(r.commits_30d),
      firstSeen: ms(r.first_seen_at),
      time: ms(r.last_commit_at),
      stale: r.collection_failing === true,
      sample: r.sample === true,
      url: r.sample === true ? null : str(r.url),
      types: typesByRepo.get(r.id) || [],
    }));
    const byId = new Map(repoRecs.map((r) => [r.repoId, r]));
    const eventRecs = events.map((e) => {
      const r = byId.get(e.repo_id);
      return Object.assign({}, r || { repo: null, owner: null, game: null, gameInferred: true, gameInherited: false, category: null, status: null, toolkit: null, fork: null, stars: null, commits: null, firstSeen: null }, {
        kind: 'event',
        id: String(e.id),
        repoId: e.repo_id,
        type: e.type,
        typeLabel: typeLabel(e.type),
        types: [e.type],
        summary: str(e.summary),
        time: ms(e.source_at),
        observedAt: ms(e.observed_at),
        stale: Boolean((r && r.stale) || e.type === 'collection_stale'),
        sample: e.sample === true || Boolean(r && r.sample),
        url: e.sample === true || (r && r.sample) ? null : str(e.source_url) || (r && r.url) || null,
      });
    });
    return { repos: repoRecs, events: eventRecs };
  }

  // ---------- filtering and sorting ----------

  function fieldValue(rec, field) {
    if (field === 'fork') return rec.fork === null || rec.fork === undefined ? null : rec.fork ? 'yes' : 'no';
    return rec[field] === undefined || rec[field] === '' ? null : rec[field];
  }

  function matchTerm(t, rec, now) {
    if (t.field === 'type') {
      if (t.value === 'unknown') return rec.types.length === 0;
      return rec.types.some((id) => typeMatches(t.value, id));
    }
    if (t.field === 'time') {
      if (rec.time === null || rec.time === undefined) return t.kind === 'unknown';
      if (t.kind === 'unknown') return false;
      if (t.kind === 'today') return new Date(rec.time).toISOString().slice(0, 10) === new Date(now).toISOString().slice(0, 10);
      return t.op === '<' ? now - rec.time < t.ms : now - rec.time > t.ms;
    }
    const v = fieldValue(rec, t.field);
    if (t.value === 'unknown') return v === null;
    if (v === null) return false;
    if (t.field === 'stars') {
      return { '<': v < t.num, '>': v > t.num, '<=': v <= t.num, '>=': v >= t.num, '=': v === t.num }[t.op];
    }
    if (TEXT_FIELDS.includes(t.field)) return String(v).toLowerCase().includes(t.value);
    return v === t.value;
  }

  // Same field: OR (time and stars: AND). Different fields: AND.
  function matches(rec, filters, search, now) {
    const byField = new Map();
    for (const f of filters) {
      if (!byField.has(f.field)) byField.set(f.field, []);
      byField.get(f.field).push(f);
    }
    for (const [field, terms] of byField) {
      const ok = field === 'time' || field === 'stars' ? terms.every((t) => matchTerm(t, rec, now)) : terms.some((t) => matchTerm(t, rec, now));
      if (!ok) return false;
    }
    if (search) {
      const hay = [rec.game, rec.repo, rec.owner, rec.toolkit, rec.category, rec.typeLabel, rec.summary].filter(Boolean).join(' ').toLowerCase();
      if (!search.toLowerCase().split(/\s+/).every((w) => hay.includes(w))) return false;
    }
    return true;
  }

  function sortValue(rec, key) {
    const v = { time: rec.time, owner: rec.owner, game: rec.game, category: rec.category, status: rec.status, stars: rec.stars, commits: rec.commits, 'first-seen': rec.firstSeen }[key];
    if (typeof v === 'string') return v.trim() ? v.trim().toLowerCase() : null;
    return v === undefined ? null : v;
  }

  // Unknown values always sort after known ones, in either direction.
  function sortRecords(list, sort) {
    const sign = sort.dir === 'desc' ? -1 : 1;
    return list
      .map((r, i) => [r, i, sortValue(r, sort.key)])
      .sort((a, b) => {
        const ua = a[2] === null;
        const ub = b[2] === null;
        if (ua || ub) return ua === ub ? a[1] - b[1] : ua ? 1 : -1;
        const c = a[2] < b[2] ? -1 : a[2] > b[2] ? 1 : 0;
        return c ? c * sign : a[1] - b[1];
      })
      .map((x) => x[0]);
  }

  function groupValue(rec, field) {
    const v = field === 'type' ? rec.typeLabel : rec[field];
    return v === undefined || v === '' ? null : v;
  }

  function groupRecords(list, field) {
    if (field === 'none') return [{ label: null, items: list }];
    const map = new Map();
    for (const r of list) {
      const v = groupValue(r, field);
      const k = v === null ? '￿' : String(v);
      if (!map.has(k)) map.set(k, { label: v === null ? 'unknown' : String(v), items: [] });
      map.get(k).items.push(r);
    }
    return [...map.keys()].sort().map((k) => map.get(k));
  }

  // ---------- formatting ----------

  const p2 = (n) => String(n).padStart(2, '0');
  function hhmm(t) {
    const d = new Date(t);
    return p2(d.getUTCHours()) + ':' + p2(d.getUTCMinutes());
  }
  function fmtTime(t) {
    return t === null || t === undefined || !Number.isFinite(t) ? 'unknown' : new Date(t).toISOString().slice(0, 10) + ' ' + hhmm(t) + ' UTC';
  }
  function ago(t, now) {
    if (t === null || t === undefined || !Number.isFinite(t) || !Number.isFinite(now)) return 'unknown';
    const d = now - t;
    if (d < 60000) return 'just now';
    if (d < 3600000) return Math.floor(d / 60000) + 'm ago';
    if (d < DAY) return Math.floor(d / 3600000) + 'h ago';
    return Math.floor(d / DAY) + 'd ago';
  }
  const orUnknown = (v) => (v === null || v === undefined || v === '' ? 'unknown' : String(v));
  function gameLabel(rec) {
    if (!rec.game) return 'unknown';
    if (rec.gameInherited) return rec.game + ' [inherited / unverified]';
    return rec.game + (rec.gameInferred ? ' [inferred]' : '');
  }
  function eventTime(t, now) {
    if (!Number.isFinite(t) || !Number.isFinite(now)) return '[unknown]';
    const same = new Date(t).toISOString().slice(0, 10) === new Date(now).toISOString().slice(0, 10);
    return '[' + (same ? '' : new Date(t).toISOString().slice(5, 10) + ' ') + hhmm(t) + ']';
  }
  function compareLabel(rec) {
    if (rec.ahead === null || rec.behind === null) return 'unknown';
    return 'ahead ' + rec.ahead + ' / behind ' + rec.behind;
  }

  function linkLine(rec) {
    if (rec.sample) return { text: 'sample entry: no real link' };
    const href = safeGithubUrl(rec.url);
    return href ? { text: 'link:', href } : { text: 'link: none (not a https://github.com URL)' };
  }

  function showLines(rec, now) {
    const lines = [(rec.kind === 'event' ? 'event ' : 'repository ') + rec.id + (rec.kind === 'event' ? '  ' + rec.typeLabel : '')];
    lines.push('repo: ' + orUnknown(rec.repo) + '   owner: ' + orUnknown(rec.owner));
    lines.push('game: ' + gameLabel(rec) + '   category: ' + orUnknown(rec.category));
    lines.push('status: ' + orUnknown(rec.status) + ' (observed activity only)   toolkit: ' + orUnknown(rec.toolkit));
    lines.push('fork: ' + (rec.fork === null ? 'unknown' : rec.fork ? 'yes' : 'no') + '   parent: ' + orUnknown(rec.parent) + '   root: ' + orUnknown(rec.root));
    lines.push('stars: ' + orUnknown(rec.stars) + '   commits 30d: ' + orUnknown(rec.commits) + '   first seen: ' + fmtTime(rec.firstSeen));
    if (rec.kind === 'event') {
      lines.push('source time: ' + fmtTime(rec.time));
      lines.push('collected:   ' + fmtTime(rec.observedAt));
      lines.push('stale: ' + (rec.stale ? 'yes' : 'no'));
      lines.push('summary: ' + orUnknown(rec.summary));
    } else {
      lines.push('latest commit: ' + fmtTime(rec.time) + ' (' + ago(rec.time, now) + ')');
      lines.push('parent comparison: ' + compareLabel(rec) + ' (' + orUnknown(rec.comparison) + ')');
      lines.push('stale: ' + (rec.stale ? 'yes' : 'no'));
    }
    lines.push(linkLine(rec));
    return lines;
  }

  function activeSummary(state) {
    const parts = state.filters.map((f) => f.raw);
    if (state.search) parts.push('search:"' + state.search + '"');
    return parts.length ? parts.join(' ') : 'none';
  }
  const sortLabel = (s) => (s.dir === 'desc' ? '-' : '') + s.key;

  // ---------- commands ----------

  const err = (text) => ({ text, kind: 'error' });
  const ok = (text) => ({ text, kind: 'ok' });

  const COMMANDS = {
    help(s, args, ctx, o) {
      const name = (args[0] || '').toLowerCase();
      if (name) {
        if (!Object.prototype.hasOwnProperty.call(HELP, name)) return o.push(err('no help for "' + clip(name) + '"'));
        o.push(HELP[name][0] + '  -  ' + HELP[name][1]);
        (Object.prototype.hasOwnProperty.call(EXAMPLES, name) ? EXAMPLES[name] : []).forEach((x) => o.push('  e.g. ' + x));
        return;
      }
      Object.keys(HELP).forEach((k) => o.push(HELP[k][0].padEnd(46) + HELP[k][1].split('.')[0]));
      o.push({ text: 'Tab completes, Up/Down history, Esc clears the line, Ctrl+L clears output, / focuses the prompt.', kind: 'dim' });
    },
    filter(s, args, ctx, o) {
      if (!args.length) return o.push(s.filters.length ? 'filters: ' + activeSummary(s) : 'no filters. try: filter category:framework status:active');
      let filters = s.filters.slice();
      for (const a of args) {
        if (a.toLowerCase() === 'clear') {
          filters = [];
        } else if (a[0] === '-') {
          const body = a.slice(1);
          if (body.includes(':')) {
            const t = parseTerm(body);
            if (t.error) return o.push(err(t.error));
            filters = filters.filter((f) => f.raw !== t.raw);
          } else {
            const field = canonField(body);
            if (!field) return o.push(err('unknown field "' + clip(body) + '"'));
            filters = filters.filter((f) => f.field !== field);
          }
        } else {
          const t = parseTerm(a);
          if (t.error) return o.push(err(t.error));
          if (!filters.some((f) => f.raw === t.raw)) filters.push(t);
        }
      }
      s.filters = filters;
      o.push(ok('filters: ' + activeSummary(s)));
    },
    search(s, args, ctx, o) {
      s.search = args.join(' ').trim();
      o.push(ok(s.search ? 'search: ' + s.search : 'search cleared'));
    },
    sort(s, args, ctx, o) {
      const r = parseSort(args);
      if (r.error) return o.push(err(r.error));
      s.sort = r;
      o.push(ok('sort: ' + sortLabel(r) + ' (unknown values last)'));
    },
    group(s, args, ctx, o) {
      const g = (args[0] || '').toLowerCase();
      if (!GROUPS.includes(g)) return o.push(err('usage: group ' + GROUPS.join('|')));
      s.group = g;
      o.push(ok('group: ' + g));
    },
    show(s, args, ctx, o) {
      const rec = args[0] && ctx.find(args[0]);
      if (!rec) return o.push(err(args[0] ? 'no record "' + clip(args[0]) + '"' : 'usage: show <id>'));
      showLines(rec, ctx.now).forEach((l) => o.push(l));
    },
    open(s, args, ctx, o, fx) {
      const rec = args[0] && ctx.find(args[0]);
      if (!rec) return o.push(err(args[0] ? 'no record "' + clip(args[0]) + '"' : 'usage: open <id>'));
      const line = linkLine(rec);
      o.push(line);
      if (line.href) fx.push({ type: 'open', url: line.href });
      else if (!rec.sample) o.push(err('refused: only https://github.com links are opened'));
    },
    pause(s, args, ctx, o) {
      s.paused = true;
      o.push(ok('paused: new events are held and counted'));
    },
    resume(s, args, ctx, o) {
      s.paused = false;
      o.push(ok('resumed'));
    },
    order(s, args, ctx, o) {
      const v = (args[0] || '').toLowerCase();
      if (v !== 'newest' && v !== 'oldest') return o.push(err('usage: order newest|oldest'));
      s.sort = { key: 'time', dir: v === 'newest' ? 'desc' : 'asc' };
      o.push(ok('order: ' + v + ' first'));
    },
    scroll(s, args, ctx, o) {
      const v = (args[0] || '').toLowerCase();
      if (v !== 'auto' && v !== 'manual') return o.push(err('usage: scroll auto|manual'));
      s.scroll = v;
      o.push(ok('scroll: ' + v));
    },
    subscribe(s, args, ctx, o) {
      o.push('Proposed RSS routes (not deployed, nothing to subscribe to yet):');
      ROUTES.forEach((r) => o.push('  ' + r));
    },
    demo(s, args, ctx, o, fx) {
      const a = (args[0] || '').toLowerCase();
      if (a === 'stop') {
        fx.push({ type: 'demo-stop' });
        return o.push('demo stopped');
      }
      const n = a ? Number(a) : 4;
      if (!Number.isInteger(n) || n < 1 || n > 10) return o.push(err('usage: demo [1-10|stop]'));
      fx.push({ type: 'demo', n });
      o.push('DEMO MODE: inserting ' + n + ' fictional events, one every 1.5 s. They are not real activity.');
    },
    status(s, args, ctx, o) {
      (ctx.info || []).forEach((l) => o.push(l));
      o.push('view: ' + s.view + '  sort: ' + sortLabel(s.sort) + '  group: ' + s.group + '  filters: ' + activeSummary(s));
      o.push('paused: ' + (s.paused ? 'yes' : 'no') + '  scroll: ' + s.scroll);
    },
    about(s, args, ctx, o) {
      s.view = 'about';
      o.push('about: see the panel above');
    },
    view(s, args, ctx, o) {
      const v = (args[0] || '').toLowerCase();
      if (!VIEWS.includes(v)) return o.push(err('usage: view ' + VIEWS.join('|')));
      s.view = v;
      o.push(ok('view: ' + v));
    },
    clear(s, args, ctx, o, fx) {
      fx.push({ type: 'clear' });
    },
    reset(s, args, ctx, o, fx) {
      Object.assign(s, initialState());
      fx.push({ type: 'reset' });
      o.push(ok('reset: filters, view, settings and demo events cleared'));
    },
  };
  COMMANDS.feed = (s, a, c, o) => COMMANDS.view(s, ['feed'], c, o);
  COMMANDS.table = (s, a, c, o) => COMMANDS.view(s, ['table'], c, o);
  COMMANDS.forks = (s, a, c, o) => COMMANDS.view(s, ['forks'], c, o);

  const MAX_SEARCH = 100;

  // A lone field:value (or -field:value) term is a filter shorthand, as in "fork:yes".
  function isFieldTerm(tokens) {
    if (tokens.length !== 1) return false;
    const m = /^-?([a-z]+):/i.exec(tokens[0]);
    return !!m && FIELDS.includes(m[1].toLowerCase());
  }

  // What the search box should filter by while typing: the text when it is plain words,
  // null when it is a command or a field:value term (those wait for Enter).
  function liveSearchText(text) {
    const tokens = tokenize(text).map((t) => t.text);
    if (!tokens.length) return '';
    if (Object.prototype.hasOwnProperty.call(COMMANDS, tokens[0].toLowerCase()) || isFieldTerm(tokens)) return null;
    return text.trim().slice(0, MAX_SEARCH);
  }

  // Runs one command line. Returns the new state, output lines and effects.
  function execute(state, input, ctx) {
    const s = Object.assign({}, state);
    const o = [];
    const fx = [];
    const tokens = tokenize(input).map((t) => t.text);
    if (tokens.length) {
      const name = tokens[0].toLowerCase();
      const fn = Object.prototype.hasOwnProperty.call(COMMANDS, name) ? COMMANDS[name] : null;
      if (fn) fn(s, tokens.slice(1), ctx || {}, o, fx);
      else if (isFieldTerm(tokens)) COMMANDS.filter(s, tokens, ctx || {}, o, fx);
      else COMMANDS.search(s, [input.trim().slice(0, MAX_SEARCH)], ctx || {}, o, fx);
    }
    return { state: s, out: o.map((l) => (typeof l === 'string' ? { text: l } : l)), effects: fx };
  }

  // ---------- completion and history ----------

  function valueCandidates(field, ctx) {
    const values = ctx.values || {};
    switch (field) {
      case 'category':
      case 'status':
      case 'fork':
      case 'type':
        return ENUMS[field].concat('unknown');
      case 'time':
        return ['<24h', '<7d', '<30d', '>7d', 'today', 'unknown'];
      case 'stars':
        return ['>10', '>100', '<10', 'unknown'];
      default:
        return values[field] || [];
    }
  }

  function candidates(idx, cmd, cur, ctx) {
    const plain = (list) => list.map((x) => ({ match: x, insert: x }));
    if (idx === 0) return plain(COMMAND_NAMES);
    const values = ctx.values || {};
    switch (cmd) {
      case 'filter': {
        const neg = cur[0] === '-' ? '-' : '';
        const body = neg ? cur.slice(1) : cur;
        const i = body.indexOf(':');
        if (i < 0) return plain(FIELDS.map((f) => neg + f + ':').concat(neg ? [] : ['clear']));
        const field = canonField(body.slice(0, i));
        if (!field) return [];
        const prefix = neg + body.slice(0, i + 1);
        return valueCandidates(field, ctx).map((v) => ({ match: prefix + v, insert: prefix + quoteIfNeeded(v) }));
      }
      case 'sort':
        if (idx === 1) return plain(SORT_KEYS.concat(SORT_KEYS.map((k) => '-' + k)));
        return idx === 2 ? plain(['asc', 'desc']) : [];
      case 'group':
        return idx === 1 ? plain(GROUPS) : [];
      case 'order':
        return idx === 1 ? plain(['newest', 'oldest']) : [];
      case 'scroll':
        return idx === 1 ? plain(['auto', 'manual']) : [];
      case 'view':
        return idx === 1 ? plain(VIEWS) : [];
      case 'demo':
        return idx === 1 ? plain(['stop']) : [];
      case 'help':
        return idx === 1 ? plain(Object.keys(HELP)) : [];
      case 'show':
      case 'open':
        return idx === 1 ? plain(values.id || []) : [];
      default:
        return [];
    }
  }

  function commonPrefix(list) {
    let p = list[0];
    for (const x of list) while (!x.toLowerCase().startsWith(p.toLowerCase())) p = p.slice(0, -1);
    return p;
  }

  // Returns { line, options }. One match completes it; several extend to the common prefix.
  function complete(line, ctx) {
    const inQuote = (line.match(/"/g) || []).length % 2 === 1;
    const trailing = !inQuote && (line === '' || /\s$/.test(line));
    const toks = tokenize(line);
    const idx = trailing ? toks.length : toks.length - 1;
    const cur = trailing ? '' : toks[toks.length - 1].text;
    const start = trailing ? line.length : toks[toks.length - 1].start;
    const cmd = idx > 0 ? toks[0].text.toLowerCase() : null;
    const found = candidates(idx, cmd, cur, ctx || {}).filter((c) => c.match.toLowerCase().startsWith(cur.toLowerCase()));
    if (!found.length) return { line, options: [] };
    const head = line.slice(0, start);
    if (found.length === 1) {
      const c = found[0].insert;
      return { line: head + c + (c.endsWith(':') ? '' : ' '), options: [] };
    }
    const common = commonPrefix(found.map((c) => c.insert));
    const best = common.length > cur.length ? common : null;
    return { line: best ? head + best : line, options: found.map((c) => c.match) };
  }

  // Moves through history; text is null when back at the unsent draft.
  function historyStep(history, index, dir) {
    const next = Math.max(0, Math.min(history.length, index + dir));
    return { index: next, text: next === history.length ? null : history[next] };
  }

  function pushHistory(history, line) {
    const t = line.trim();
    if (!t || history[history.length - 1] === t) return history;
    return history.concat(t).slice(-100);
  }

  return {
    TYPES, ENUMS, FIELDS, SORT_KEYS, GROUPS, VIEWS, ROUTES, COMMAND_NAMES, liveSearchText,
    initialState, tokenize, parseTerm, parseTime, parseSort, safeGithubUrl, typeLabel,
    deriveStatus, joinData, matches, sortRecords, groupRecords, execute, complete,
    historyStep, pushHistory, showLines, activeSummary, sortLabel, fmtTime, hhmm, ago,
    orUnknown, gameLabel, eventTime, compareLabel,
  };
});
