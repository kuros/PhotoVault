/* PhotoVault UI.
   Plain JavaScript, no framework and no build step: the browser loads exactly
   the file on disk. For an app this size a framework would add a toolchain to
   maintain without removing much work. */

const $  = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  view: 'photos',
  filter: { year: null, month: null, kind: '', undated: false },
  offset: 0,
  pageSize: 120,
  photos: [],
  total: 0,
  viewerIndex: -1,
  busy: false,
};

const PAGE_TITLES = { photos: 'Photos', health: 'Health', activity: 'Activity' };
const MONTHS = ['', 'January', 'February', 'March', 'April', 'May', 'June',
                'July', 'August', 'September', 'October', 'November', 'December'];

async function api(path, options) {
  const res = await fetch(`/api/${path}`, options);
  const data = await res.json().catch(() => ({ error: 'bad response from server' }));
  if (!res.ok) throw new Error(data.error || `request failed (${res.status})`);
  return data;
}

const post = (path, body) =>
  api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(body || {}) });

function bytes(n) {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
}

const num = (n) => (n ?? 0).toLocaleString();

function when(iso) {
  if (!iso) return 'unknown date';
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString(undefined,
    { year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit' });
}

let toastTimer;
function toast(msg) {
  const el = $('#toast');
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3800);
}

/* ------------------------------------------------------------------ status */

async function refreshStatus() {
  let s;
  try {
    s = await api('status');
  } catch (err) {
    setPill('bad', 'server unreachable');
    return;
  }
  state.busy = s.busy;
  $$('.actions .btn').forEach((b) => { b.disabled = s.busy; });

  if (!s.total_assets) setPill('idle', 'empty library');
  else if (s.healthy) setPill('ok', 'fully protected');
  else {
    const failed = s.checks.filter((c) => !c.ok).length;
    setPill('bad', `${failed} issue${failed === 1 ? '' : 's'}`);
  }

  renderDevices(s);
  if (state.view === 'health') renderHealth(s);
  return s;
}

function setPill(kind, text) {
  $('#healthPill').className = `pill pill--${kind}`;
  $('#healthText').textContent = text;
}

function renderDevices(s) {
  $('#deviceList').innerHTML = s.replicas.map((r) => {
    const colour = r.corrupt ? 'var(--bad)'
      : r.reachable ? 'var(--ok)'
      : r.offline ? 'var(--muted)' : 'var(--warn)';
    const note = r.reachable ? num(r.present)
      : r.offline ? 'unplugged' : 'offline';
    return `<li class="device" title="${r.kind} · ${r.reachable ? 'reachable' : 'not reachable'}">
      <span class="device__dot" style="background:${colour}"></span>
      <span class="device__name">${r.name}${r.name === s.primary ? ' ★' : ''}</span>
      <span class="device__meta">${note}</span>
    </li>`;
  }).join('');
}

function renderHealth(s) {
  $('#checks').innerHTML = s.checks.map((c) => `
    <div class="check ${c.ok ? '' : 'check--bad'}">
      <div class="check__status">${c.ok ? 'PASS' : 'ACTION NEEDED'}</div>
      <div class="check__label">${c.label}</div>
      <div class="check__detail">${c.ok ? 'All photos satisfy this.'
        : `${num(c.count)} photo${c.count === 1 ? '' : 's'} affected`}</div>
    </div>`).join('');

  const hist = Object.entries(s.copies_histogram)
    .map(([k, v]) => [Number(k), v]).sort((a, b) => a[0] - b[0]);
  const max = Math.max(1, ...hist.map(([, v]) => v));
  $('#histogram').innerHTML = hist.length ? hist.map(([copies, n]) => {
    const colour = copies <= 1 ? 'var(--bad)'
      : copies < s.min_copies ? 'var(--warn)' : 'var(--ok)';
    return `<div class="hrow">
      <span class="hrow__label">${copies} cop${copies === 1 ? 'y' : 'ies'}</span>
      <span class="hrow__track"><span class="hrow__bar"
        style="width:${(n / max) * 100}%;background:${colour}"></span></span>
      <span class="hrow__n">${num(n)}</span>
    </div>`;
  }).join('') : '<p class="muted">Nothing imported yet.</p>';

  $('#replicaTable').innerHTML = `
    <thead><tr><th>Device</th><th>Type</th><th>Status</th>
      <th class="num">Holds</th><th class="num">Missing</th>
      <th class="num">Corrupt</th><th class="num">Size</th></tr></thead>
    <tbody>${s.replicas.map((r) => `<tr>
      <td><strong>${r.name}</strong>${r.name === s.primary
        ? ' <span class="muted">primary</span>' : ''}</td>
      <td class="muted">${r.kind}${r.offline ? ' · offline' : ''}</td>
      <td>${r.reachable ? '<span style="color:var(--ok)">connected</span>'
        : r.offline ? '<span class="muted">unplugged</span>'
        : '<span style="color:var(--warn)">unreachable</span>'}</td>
      <td class="num">${num(r.present)}</td>
      <td class="num">${r.missing ? `<span style="color:var(--warn)">${num(r.missing)}</span>` : '0'}</td>
      <td class="num">${r.corrupt ? `<span style="color:var(--bad)">${num(r.corrupt)}</span>` : '0'}</td>
      <td class="num">${bytes(r.bytes)}</td>
    </tr>`).join('')}</tbody>`;
}

/* ---------------------------------------------------------------- timeline */

async function loadTimeline() {
  const { months, undated } = await api('timeline');
  const years = new Map();
  for (const m of months) {
    if (!years.has(m.year)) years.set(m.year, { n: 0, months: [] });
    const y = years.get(m.year);
    y.n += m.n;
    y.months.push(m);
  }

  const active = (y, m) => state.filter.year === y && state.filter.month === m
    ? ' is-active' : '';
  const all = !state.filter.year && !state.filter.undated ? ' is-active' : '';

  let html = `<li><button class="tl-all${all}" data-all="1">
    <span>All photos</span></button></li>`;

  for (const [year, info] of years) {
    html += `<li><button class="tl-year${active(year, null)}" data-year="${year}">
      <span>${year}</span><span class="tl-count">${num(info.n)}</span></button></li>`;
    if (state.filter.year === year) {
      for (const m of info.months) {
        html += `<li><button class="tl-month${active(year, m.month)}"
          data-year="${year}" data-month="${m.month}">
          <span>${MONTHS[Number(m.month)]}</span>
          <span class="tl-count">${num(m.n)}</span></button></li>`;
      }
    }
  }
  if (undated) {
    html += `<li><button class="tl-year${state.filter.undated ? ' is-active' : ''}"
      data-undated="1"><span>No date</span>
      <span class="tl-count">${num(undated)}</span></button></li>`;
  }
  $('#timeline').innerHTML = html;
}

/* -------------------------------------------------------------------- grid */

async function loadPhotos(append = false) {
  if (!append) { state.offset = 0; state.photos = []; }
  const p = new URLSearchParams({ offset: state.offset, limit: state.pageSize });
  if (state.filter.undated) p.set('undated', '1');
  else {
    if (state.filter.year) p.set('year', state.filter.year);
    if (state.filter.month) p.set('month', state.filter.month);
  }
  if (state.filter.kind) p.set('kind', state.filter.kind);

  const data = await api(`photos?${p}`);
  state.total = data.total;
  state.photos = append ? state.photos.concat(data.photos) : data.photos;
  state.offset = state.photos.length;
  renderGrid();
}

function renderGrid() {
  const grid = $('#grid');
  const empty = $('#gridEmpty');

  $('#gridTitle').textContent = state.filter.undated ? 'Photos with no date'
    : state.filter.month ? `${MONTHS[Number(state.filter.month)]} ${state.filter.year}`
    : state.filter.year ? String(state.filter.year)
    : 'All photos';
  $('#gridCount').textContent = state.total
    ? `${num(state.total)} item${state.total === 1 ? '' : 's'}` : '';

  if (!state.photos.length) {
    grid.innerHTML = '';
    empty.hidden = false;
    empty.innerHTML = state.total === 0 && !state.filter.year && !state.filter.kind
      ? `<h3>No photos yet</h3>
         <p>Press <strong>Import</strong> above, or run <code>photovault ingest</code>.</p>`
      : '<h3>Nothing here</h3><p>No photos match this filter.</p>';
    $('#loadMore').hidden = true;
    return;
  }
  empty.hidden = true;

  grid.innerHTML = state.photos.map((p, i) => {
    const badges = [];
    if (p.bad) badges.push('<span class="badge badge--bad">corrupt</span>');
    else if (p.copies <= 1) badges.push('<span class="badge badge--warn">1 copy</span>');
    if (p.kind === 'video') badges.push('<span class="badge">video</span>');
    const date = p.captured_at ? p.captured_at.slice(0, 10) : 'no date';
    return `<button class="cell" data-i="${i}" title="${p.rel_path}">
      <img loading="lazy" src="/api/photo/${p.hash}/thumb" alt="">
      <span class="cell__fallback">${p.ext.toUpperCase()}</span>
      <span class="cell__badges">${badges.join('')}</span>
      <span class="cell__date">${date}</span>
    </button>`;
  }).join('');

  // Reveal the image only once it decodes, so broken thumbnails fall back to
  // the extension label instead of showing a torn placeholder.
  grid.querySelectorAll('img').forEach((img) => {
    if (img.complete && img.naturalWidth) img.classList.add('is-loaded');
    img.addEventListener('load', () => img.classList.add('is-loaded'));
    img.addEventListener('error', () => img.remove());
  });

  $('#loadMore').hidden = state.photos.length >= state.total;
}

/* ------------------------------------------------------------------ viewer */

async function openViewer(index) {
  const photo = state.photos[index];
  if (!photo) return;
  state.viewerIndex = index;

  $('#viewer').hidden = false;
  $('#viewerImg').src = photo.kind === 'video'
    ? `/api/photo/${photo.hash}/thumb` : `/api/photo/${photo.hash}/full`;
  $('#viewerName').textContent = photo.rel_path;

  const d = await api(`photo/${photo.hash}`).catch(() => null);
  if (!d || state.viewerIndex !== index) return;

  $('#viewerFacts').innerHTML = `
    <dt>Taken</dt><dd>${when(d.asset.captured_at)}</dd>
    <dt>Date from</dt><dd>${d.asset.time_source}</dd>
    <dt>Type</dt><dd>${d.asset.ext.toUpperCase()} · ${d.asset.media_kind}</dd>
    <dt>Size</dt><dd>${bytes(d.asset.size)}</dd>
    <dt>Copies</dt><dd>${d.placements.filter((p) => p.state === 'present').length}</dd>
    <dt>Fingerprint</dt><dd style="font-family:ui-monospace,monospace;font-size:11px">${d.asset.hash.slice(0, 16)}…</dd>`;

  $('#viewerCopies').innerHTML = d.placements.map((p) => {
    const colour = p.state === 'present' ? 'var(--ok)'
      : p.state === 'corrupt' ? 'var(--bad)' : 'var(--warn)';
    return `<li><span class="device__dot" style="background:${colour}"></span>
      ${p.replica}<span class="state" style="color:${colour}">${p.state}</span></li>`;
  }).join('') || '<li class="muted">Not stored anywhere.</li>';

  $('#viewerSources').innerHTML = d.sources.map(
    (s) => `<li><strong>${s.device}</strong><br>${s.abs_path}</li>`).join('')
    || '<li class="muted">No source recorded.</li>';
}

function closeViewer() {
  $('#viewer').hidden = true;
  $('#viewerImg').src = '';
  state.viewerIndex = -1;
}

const stepViewer = (d) => openViewer(
  Math.max(0, Math.min(state.photos.length - 1, state.viewerIndex + d)));

/* -------------------------------------------------------------------- jobs */

async function refreshJobs() {
  let jobs = [];
  try { ({ jobs } = await api('jobs')); } catch { return; }

  const active = jobs.find((j) => j.state === 'running');
  const bar = $('#jobBar');
  if (active) {
    bar.hidden = false;
    $('#jobLabel').textContent = active.label;
    $('#jobMessage').textContent = active.message || '';
    const fill = $('#jobFill');
    if (active.percent === null) {
      fill.classList.add('is-indeterminate');
      $('#jobPercent').textContent = num(active.done);
    } else {
      fill.classList.remove('is-indeterminate');
      fill.style.width = `${active.percent}%`;
      $('#jobPercent').textContent = `${active.percent}%`;
    }
    $('#jobCancel').dataset.id = active.id;
  } else if (!bar.hidden) {
    bar.hidden = true;
    refreshStatus();
    loadTimeline();
    loadPhotos();
  }

  if (state.view === 'activity') renderJobs(jobs);
  return jobs;
}

function renderJobs(jobs) {
  $('#jobList').innerHTML = jobs.length ? jobs.map((j) => {
    const summary = Object.entries(j.result || {})
      .map(([k, v]) => `${k.replace(/_/g, ' ')}: ${num(v)}`).join(' · ');
    return `<li class="job">
      <div class="job__head">
        <span class="job__state state-${j.state}">${j.state.toUpperCase()}</span>
        <strong>${j.label}</strong>
        <span class="job__time">${when(j.started_at)}</span>
      </div>
      <div class="job__msg">${j.message || ''}${summary ? ` — ${summary}` : ''}</div>
      ${j.errors?.length ? `<div class="job__errors">${
        j.errors.map((e) => e.replace(/</g, '&lt;')).join('\n')}</div>` : ''}
    </li>`;
  }).join('') : '<li class="muted">Nothing has run yet.</li>';
}

async function loadEvents() {
  const { events } = await api('log').catch(() => ({ events: [] }));
  $('#eventList').innerHTML = events.length ? events.map((e) => `
    <li><time>${e.at.replace('T', ' ')}</time>
      <span class="kind">${e.kind}</span>
      <span class="muted">${e.detail}</span></li>`).join('')
    : '<li class="muted">No history yet.</li>';
}

async function startJob(action) {
  try {
    const res = await post('jobs', { action });
    if (res.error) return toast(res.error);
    toast(`Started: ${res.label}`);
    $('#jobBar').hidden = false;
    refreshJobs();
  } catch (err) {
    toast(err.message);
  }
}

/* ------------------------------------------------------------------- wiring */

function switchView(view) {
  state.view = view;
  $$('.tab').forEach((t) => t.classList.toggle('is-active', t.dataset.view === view));
  $$('.view').forEach((v) => v.classList.toggle('is-active', v.dataset.view === view));
  document.title = `PhotoVault · ${PAGE_TITLES[view]}`;
  if (view === 'health') refreshStatus();
  if (view === 'activity') { refreshJobs(); loadEvents(); }
}

function attach() {
  $$('.tab').forEach((t) =>
    t.addEventListener('click', () => switchView(t.dataset.view)));

  $$('.actions .btn').forEach((b) =>
    b.addEventListener('click', () => startJob(b.dataset.action)));

  $('#jobCancel').addEventListener('click', (e) =>
    post('jobs/cancel', { id: Number(e.target.dataset.id) })
      .then(() => toast('Cancelling after the current file…')));

  $('#timeline').addEventListener('click', (e) => {
    const btn = e.target.closest('button');
    if (!btn) return;
    const { year, month, all, undated } = btn.dataset;
    state.filter.undated = Boolean(undated);
    if (all || undated) { state.filter.year = null; state.filter.month = null; }
    else {
      // Clicking the already-open year collapses it back to the year view.
      const sameYear = state.filter.year === year;
      state.filter.year = year;
      state.filter.month = month || (sameYear && !month ? null : null);
    }
    loadTimeline();
    loadPhotos();
  });

  $('#kindFilter').addEventListener('click', (e) => {
    const btn = e.target.closest('button');
    if (!btn) return;
    $$('#kindFilter button').forEach((b) => b.classList.toggle('is-active', b === btn));
    state.filter.kind = btn.dataset.kind;
    loadPhotos();
  });

  $('#grid').addEventListener('click', (e) => {
    const cell = e.target.closest('.cell');
    if (cell) openViewer(Number(cell.dataset.i));
  });

  $('#loadMore').addEventListener('click', () => loadPhotos(true));
  $('#viewerClose').addEventListener('click', closeViewer);
  $('#viewerPrev').addEventListener('click', () => stepViewer(-1));
  $('#viewerNext').addEventListener('click', () => stepViewer(1));
  $('#viewer').addEventListener('click', (e) => {
    if (e.target.id === 'viewer' || e.target.classList.contains('viewer__stage'))
      closeViewer();
  });

  document.addEventListener('keydown', (e) => {
    if ($('#viewer').hidden) return;
    if (e.key === 'Escape') closeViewer();
    if (e.key === 'ArrowLeft') stepViewer(-1);
    if (e.key === 'ArrowRight') stepViewer(1);
  });
}

async function init() {
  attach();
  switchView('photos');
  await Promise.all([refreshStatus(), loadTimeline(), loadPhotos()]);
  refreshJobs();
  // Poll rather than push: for a single-user local app this is a few bytes a
  // second and avoids a WebSocket's reconnection and lifecycle handling.
  setInterval(refreshJobs, 1000);
  setInterval(() => { if (!state.busy) refreshStatus(); }, 15000);
}

init().catch((err) => toast(`Could not start: ${err.message}`));
