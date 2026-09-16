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
  lastJobSignature: '',
};

const PAGE_TITLES = { photos: 'Photos', health: 'Health',
                      activity: 'Activity', duplicates: 'Duplicates',
                      settings: 'Settings' };
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
  if (state.view === 'health') { renderHealth(s); renderPlan(); }
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

async function renderPlan() {
  const panel = $('#storagePanel');
  let p;
  try { p = await api('plan'); } catch { panel.hidden = true; return; }

  if (!p.sharded) {
    panel.hidden = false;
    $('#storageSub').textContent =
      `Every device holds a complete copy. Any one of them can restore your `
      + `whole library on its own.`;
    $('#storageBars').innerHTML = '';
    return;
  }

  panel.hidden = false;
  $('#storageSub').innerHTML = p.ok
    ? `Sharded: photos are split across devices, ${p.shard_copies_needed} shard `
      + `cop${p.shard_copies_needed === 1 ? 'y' : 'ies'} each. `
      + `<strong>No single drive is complete</strong> — restoring needs all of them.`
    : `<span style="color:var(--bad)">${num(p.unplaceable)} photos cannot reach `
      + `full redundancy — the drives are too small.</span>`;

  $('#storageBars').innerHTML = p.replicas.map((r) => {
    const pct = r.fill === null ? null : Math.min(100, r.fill * 100);
    const colour = pct === null ? 'var(--muted)'
      : pct > 95 ? 'var(--bad)' : pct > 80 ? 'var(--warn)' : 'var(--ok)';
    const cap = r.capacity ? bytes(r.capacity) : 'unknown';
    return `<div class="hrow">
      <span class="hrow__label">${r.name}</span>
      <span class="hrow__track"><span class="hrow__bar"
        style="width:${pct ?? 0}%;background:${colour}"></span></span>
      <span class="hrow__n" title="${num(r.files)} files of ${cap}">
        ${pct === null ? '—' : pct.toFixed(0) + '%'}</span>
    </div>`;
  }).join('');
}




/* ----------------------------------------------------------------- uploads */

const MEDIA_EXT = new Set([
  'jpg', 'jpeg', 'png', 'heic', 'heif', 'tif', 'tiff', 'gif', 'webp', 'bmp',
  'dng', 'cr2', 'cr3', 'nef', 'arw', 'raf', 'orf', 'rw2',
  'mov', 'mp4', 'm4v', 'avi', 'mkv', '3gp', 'mts', 'm2ts', 'webm',
]);

const upload = { queue: [], done: 0, bytes: 0, total: 0, cancelled: false,
                 running: false, errors: [] };

const isMedia = (name) => MEDIA_EXT.has((name.split('.').pop() || '').toLowerCase());

/** Walk a dropped directory. The DataTransfer entry API is the only way to
 *  see inside a dropped folder; a plain `files` list gives only the top level. */
async function readEntry(entry, prefix = '') {
  if (entry.isFile) {
    const file = await new Promise((res, rej) => entry.file(res, rej));
    return isMedia(file.name) ? [{ file, path: prefix + file.name }] : [];
  }
  if (!entry.isDirectory) return [];
  const reader = entry.createReader();
  const out = [];
  // readEntries returns at most ~100 per call, so it must be drained in a loop.
  for (;;) {
    const batch = await new Promise((res, rej) => reader.readEntries(res, rej));
    if (!batch.length) break;
    for (const child of batch) {
      out.push(...await readEntry(child, `${prefix}${entry.name}/`));
    }
  }
  return out;
}

function enqueue(items) {
  const fresh = items.filter((i) => isMedia(i.path));
  if (!fresh.length) {
    toast('Nothing to upload — no photos or videos in that selection.');
    return;
  }
  upload.queue.push(...fresh);
  upload.total += fresh.reduce((n, i) => n + i.file.size, 0);
  if (!upload.running) runUploads();
}

async function runUploads() {
  upload.running = true;
  upload.cancelled = false;
  $('#uploadPanel').hidden = false;
  $('#uploadErrors').innerHTML = '';

  const CONCURRENCY = 3;
  const workers = Array.from({ length: CONCURRENCY }, async () => {
    while (upload.queue.length && !upload.cancelled) {
      const item = upload.queue.shift();
      try {
        const res = await fetch('/api/upload', {
          method: 'POST',
          headers: { 'X-PV-Path': encodeURIComponent(item.path),
                     'Content-Type': 'application/octet-stream' },
          body: item.file,
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          upload.errors.push(`${item.path}: ${err.error || res.status}`);
        }
      } catch (err) {
        upload.errors.push(`${item.path}: ${err.message}`);
      }
      upload.done += 1;
      upload.bytes += item.file.size;
      renderUploadProgress();
    }
  });
  await Promise.all(workers);

  upload.running = false;
  $('#uploadPanel').hidden = true;
  const failed = upload.errors.length;
  upload.queue = []; upload.done = 0; upload.bytes = 0; upload.total = 0;
  upload.errors = [];
  toast(upload.cancelled ? 'Upload cancelled'
        : failed ? `Uploaded with ${failed} problem${failed === 1 ? '' : 's'}`
        : 'Upload complete');
  loadStaged();
}

function renderUploadProgress() {
  const pct = upload.total ? Math.min(100, (upload.bytes / upload.total) * 100) : 0;
  $('#uploadFill').style.width = `${pct}%`;
  $('#uploadNote').textContent =
    `${num(upload.done)} of ${num(upload.done + upload.queue.length)} files`
    + ` · ${bytes(upload.bytes)}`;
  if (upload.errors.length) {
    $('#uploadErrors').innerHTML =
      upload.errors.slice(-8).map((e) => `<li>${e.replace(/</g, '&lt;')}</li>`).join('');
  }
}

async function loadStaged() {
  let s;
  try { s = await api('uploads'); } catch { return; }
  const panel = $('#stagedPanel');
  panel.hidden = s.files === 0;
  if (!s.files) return;
  $('#stagedNote').textContent =
    `${num(s.files)} file${s.files === 1 ? '' : 's'} · ${bytes(s.bytes)}`
    + ` — not in your library yet.`;
}

function attachUploads() {
  $('#addPhotos').addEventListener('click', () => {
    // A folder picker cannot also accept loose files, so offer the choice.
    if (confirm('Add a whole folder?\n\nOK — choose a folder (sub-folders included)'
                + '\nCancel — choose individual files')) {
      $('#folderPicker').click();
    } else {
      $('#filePicker').click();
    }
  });

  for (const id of ['filePicker', 'folderPicker']) {
    $(`#${id}`).addEventListener('change', (e) => {
      const items = Array.from(e.target.files).map((file) => ({
        file, path: file.webkitRelativePath || file.name }));
      e.target.value = '';
      enqueue(items);
    });
  }

  $('#uploadCancel').addEventListener('click', () => { upload.cancelled = true; });

  $('#stagedImport').addEventListener('click', () => {
    $('#stagedPanel').hidden = true;
    startJob('upload_ingest');
  });

  $('#stagedDiscard').addEventListener('click', async () => {
    if (!confirm('Discard the uploaded files that have not been imported yet?')) return;
    const res = await post('uploads/discard', {});
    toast(`Discarded ${num(res.discarded)} files`);
    loadStaged();
  });

  let depth = 0;
  window.addEventListener('dragenter', (e) => {
    if (!e.dataTransfer?.types?.includes('Files')) return;
    depth += 1;
    $('#dropzone').hidden = false;
  });
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('dragleave', () => {
    // dragleave fires for every child element, so count enter/leave pairs.
    depth = Math.max(0, depth - 1);
    if (!depth) $('#dropzone').hidden = true;
  });
  window.addEventListener('drop', async (e) => {
    e.preventDefault();
    depth = 0;
    $('#dropzone').hidden = true;
    const entries = Array.from(e.dataTransfer.items || [])
      .map((i) => i.webkitGetAsEntry?.()).filter(Boolean);
    if (entries.length) {
      const nested = await Promise.all(entries.map((en) => readEntry(en)));
      enqueue(nested.flat());
    } else {
      enqueue(Array.from(e.dataTransfer.files || [])
        .map((file) => ({ file, path: file.name })));
    }
  });
}

/* -------------------------------------------------------------- duplicates */

const dup = { groups: [], total: 0, offset: 0, pageSize: 40, recoverable: 0 };

function dupBanner(text, kind) {
  const el = $('#dupBanner');
  el.hidden = !text;
  el.className = `banner banner--${kind}`;
  el.innerHTML = text || '';
}

async function loadDuplicates(append = false) {
  if (!append) { dup.offset = 0; dup.groups = []; }
  let data;
  try {
    data = await api(`duplicates?offset=${dup.offset}&limit=${dup.pageSize}`);
  } catch (err) {
    dupBanner(err.message, 'bad');
    return;
  }

  dup.total = data.total_groups;
  dup.recoverable = data.recoverable_bytes;
  dup.groups = append ? dup.groups.concat(data.groups) : data.groups;
  dup.offset = dup.groups.length;

  if (!data.can_analyse) {
    dupBanner('No image decoder available. Install Pillow '
      + '(<code>pip install Pillow</code>) to analyse photos.', 'bad');
  } else if (data.unanalysed) {
    dupBanner(`${num(data.unanalysed)} photos have not been analysed yet — `
      + `press <strong>Find duplicates</strong>.`, 'ok');
  } else {
    dupBanner('', 'ok');
  }

  $('#dupSummary').textContent = dup.total
    ? `${num(dup.total)} groups · ${bytes(dup.recoverable)} recoverable`
    : '';
  renderDuplicates();
}

function renderDuplicates() {
  const marked = dup.groups.reduce(
    (n, g) => n + g.members.filter((m) => m.action === 'delete').length, 0);
  const btn = $('#dupApply');
  btn.disabled = marked === 0;
  btn.textContent = marked ? `Delete ${num(marked)} marked` : 'Delete marked';

  if (!dup.groups.length) {
    $('#dupGroups').innerHTML =
      '<p class="muted">No near-duplicates found. Exact copies are already '
      + 'collapsed automatically when photos are imported.</p>';
    $('#dupMore').hidden = true;
    return;
  }

  $('#dupGroups').innerHTML = dup.groups.map((g, gi) => {
    const items = g.members.map((m, mi) => {
      const state = m.action === 'delete' ? 'is-delete'
        : m.action === 'keep' ? 'is-keep' : '';
      const tag = m.action === 'delete'
        ? '<span class="dupitem__tag tag-delete">DELETE</span>'
        : m.action === 'keep'
        ? '<span class="dupitem__tag tag-keep">KEEP</span>'
        : m.hash === g.suggested_keep
        ? '<span class="dupitem__tag tag-best">best</span>' : '';
      const dims = m.width ? `${m.width}×${m.height}` : 'unknown size';
      const warn = m.copies < 2
        ? `<span class="dupitem__warn">only ${m.copies} copy</span>`
        : `<span>${m.copies} copies</span>`;
      return `<button class="dupitem ${state}" data-g="${gi}" data-m="${mi}"
                      title="${m.rel_path}">
        <span class="dupitem__frame">
          <img loading="lazy" src="/api/photo/${m.hash}/thumb" alt="">
          ${tag}
        </span>
        <span class="dupitem__meta">
          <b>${dims}</b>
          <span>${bytes(m.size)} · ${m.ext.toUpperCase()}</span><br>
          ${warn}
        </span>
      </button>`;
    }).join('');

    return `<div class="dupgroup">
      <div class="dupgroup__head">
        <strong>${g.members.length} near-identical photos</strong>
        <span>${bytes(g.wasted_bytes)} recoverable</span>
        <span class="dupgroup__actions">
          <button class="btn btn--sm" data-auto="${gi}">Keep the best</button>
          <button class="btn btn--sm btn--ghost" data-skip="${gi}">Skip</button>
        </span>
      </div>
      <div class="dupitems">${items}</div>
    </div>`;
  }).join('');

  $('#dupMore').hidden = dup.groups.length >= dup.total;
}

/** Choosing a keeper marks every other member of that group for deletion. */
function keepOnly(groupIndex, keepHash) {
  const g = dup.groups[groupIndex];
  const decisions = {};
  for (const m of g.members) {
    m.action = m.hash === keepHash ? 'keep' : 'delete';
    decisions[m.hash] = m.action;
  }
  return decisions;
}

function skipGroup(groupIndex) {
  const g = dup.groups[groupIndex];
  const decisions = {};
  for (const m of g.members) { m.action = ''; decisions[m.hash] = ''; }
  return decisions;
}

async function recordDecisions(decisions) {
  renderDuplicates();
  try {
    await post('duplicates/decide', { decisions });
  } catch (err) {
    dupBanner(`Could not save your decision: ${err.message}`, 'bad');
  }
}

function attachDuplicates() {
  $('#dupScan').addEventListener('click', () => startJob('dupscan'));
  $('#dupMore').addEventListener('click', () => loadDuplicates(true));

  $('#dupApply').addEventListener('click', async () => {
    const marked = dup.groups.reduce(
      (n, g) => n + g.members.filter((m) => m.action === 'delete').length, 0);
    if (!marked) return;
    if (!confirm(`Permanently delete ${marked} photos from every device?\n\n`
      + `Each one is only removed if the photo you kept has been re-read and `
      + `confirmed on enough devices first. This cannot be undone.`)) return;
    try {
      const res = await post('jobs', { action: 'dupapply', confirm: true });
      if (res.error) return dupBanner(res.error, 'bad');
      dupBanner('Deleting… see Activity for progress.', 'ok');
      refreshJobs();
    } catch (err) {
      dupBanner(err.message, 'bad');
    }
  });

  $('#dupGroups').addEventListener('click', (e) => {
    const auto = e.target.closest('[data-auto]');
    if (auto) {
      const gi = Number(auto.dataset.auto);
      return recordDecisions(keepOnly(gi, dup.groups[gi].suggested_keep));
    }
    const skip = e.target.closest('[data-skip]');
    if (skip) return recordDecisions(skipGroup(Number(skip.dataset.skip)));

    const item = e.target.closest('.dupitem');
    if (item) {
      const gi = Number(item.dataset.g);
      const m = dup.groups[gi].members[Number(item.dataset.m)];
      return recordDecisions(keepOnly(gi, m.hash));
    }
  });
}

/* -------------------------------------------------------------- settings */

let draft = null;          // the config being edited, saved only on Save
let drives = [];

async function loadSettings() {
  const [cfg, d] = await Promise.all([
    api('config'),
    api('drives').catch(() => ({ drives: [] })),
  ]);
  draft = cfg.config;
  drives = d.drives || [];
  $('#settingsPath').textContent = cfg.path;
  renderSettings();
}

function banner(text, kind) {
  const el = $('#settingsBanner');
  el.hidden = !text;
  el.className = `banner banner--${kind}`;
  el.textContent = text;
}

function renderSettings() {
  if (!draft) return;
  const v = draft.vault || {};

  $('#cfgPrimary').innerHTML = (draft.replica || [])
    .map((r) => `<option value="${r.name}"${r.name === v.primary ? ' selected' : ''}>${r.name}</option>`)
    .join('') || '<option value="">no devices yet</option>';
  $('#cfgMinCopies').value = v.min_copies ?? 3;
  $('#cfgScrubDays').value = v.scrub_days ?? 30;
  $('#cfgOffline').checked = v.require_offline_copy !== false;

  $('#detectedDrives').innerHTML = drives.length
    ? `<span class="muted" style="font-size:12.5px;align-self:center">Detected:</span>`
      + drives.map((dr, i) => `<button class="drive-chip" data-drive="${i}">
          ${dr.label} <small>${bytes(dr.free)} free</small></button>`).join('')
    : '';

  $('#replicaRows').innerHTML = (draft.replica || []).map((r, i) => `
    <div class="row" data-kind="replica" data-i="${i}">
      <input type="text" value="${r.name ?? ''}" data-field="name" placeholder="name">
      <select data-field="kind">
        ${['local', 'rsync', 'gcs'].map((k) =>
          `<option value="${k}"${r.kind === k ? ' selected' : ''}>${k}</option>`).join('')}
      </select>
      <input type="text" value="${r.root ?? ''}" data-field="root"
             placeholder="${r.kind === 'rsync' ? '/d/PhotoVault/library' : '/Volumes/Drive/PhotoVault/library'}">
      <span class="row__flags">
        <label><input type="checkbox" data-field="offline"${r.offline ? ' checked' : ''}>offline</label>
        <label><input type="checkbox" data-field="shard"${r.mode === 'shard' ? ' checked' : ''}>shard</label>
      </span>
      <button class="row__del" data-del="replica" data-i="${i}" title="Remove">&times;</button>
    </div>
    ${r.kind === 'rsync' ? `<div class="row" data-kind="replica" data-i="${i}"
        style="grid-template-columns:1fr">
      <input type="text" value="${r.host ?? ''}" data-field="host"
             placeholder="SSH host, e.g. you@192.168.1.50"></div>` : ''}`).join('')
    || '<p class="muted">No devices yet. Add one above.</p>';

  $('#sourceRows').innerHTML = (draft.source || []).map((s, i) => `
    <div class="row row--source" data-kind="source" data-i="${i}">
      <input type="text" value="${s.device ?? ''}" data-field="device" placeholder="name">
      <select data-field="kind">
        ${['local', 'adb'].map((k) =>
          `<option value="${k}"${s.kind === k ? ' selected' : ''}>${k}</option>`).join('')}
      </select>
      <input type="text" value="${s.path ?? ''}" data-field="path"
             placeholder="${s.kind === 'adb' ? '/sdcard/DCIM' : '/path/to/folder'}">
      <span class="row__flags">
        <label title="Delete originals once they have min_copies verified copies">
          <input type="checkbox" data-field="clear_after_import"${s.clear_after_import ? ' checked' : ''}>clear after import</label>
      </span>
      <button class="row__del" data-del="source" data-i="${i}" title="Remove">&times;</button>
    </div>`).join('')
    || '<p class="muted">No sources yet. Add one above.</p>';
}

function collect() {
  const v = draft.vault = draft.vault || {};
  v.primary = $('#cfgPrimary').value;
  v.min_copies = Number($('#cfgMinCopies').value) || 3;
  v.scrub_days = Number($('#cfgScrubDays').value) || 30;
  v.require_offline_copy = $('#cfgOffline').checked;

  for (const row of $$('#replicaRows .row, #sourceRows .row')) {
    const list = row.dataset.kind === 'replica' ? draft.replica : draft.source;
    const item = list[Number(row.dataset.i)];
    if (!item) continue;
    for (const el of row.querySelectorAll('[data-field]')) {
      const f = el.dataset.field;
      if (f === 'shard') item.mode = el.checked ? 'shard' : 'full';
      else item[f] = el.type === 'checkbox' ? el.checked : el.value.trim();
    }
  }
  return draft;
}

async function saveSettings() {
  const btn = $('#settingsSave');
  btn.disabled = true;
  try {
    const res = await post('config', { config: collect() });
    draft = res.config;
    banner('Saved. The change is live — no restart needed.', 'ok');
    renderSettings();
    refreshStatus();
  } catch (err) {
    banner(err.message, 'bad');
  } finally {
    btn.disabled = false;
  }
}

function attachSettings() {
  $('#settingsSave').addEventListener('click', saveSettings);
  $('#settingsReload').addEventListener('click', () => {
    loadSettings().then(() => banner('Reloaded from disk.', 'ok'));
  });

  document.addEventListener('click', (e) => {
    const add = e.target.closest('[data-add]');
    if (add && draft) {
      collect();
      if (add.dataset.add === 'replica') {
        (draft.replica = draft.replica || []).push(
          { name: `drive${draft.replica.length + 1}`, kind: 'local', root: '',
            offline: true, mode: 'full', capacity: 'auto' });
      } else {
        (draft.source = draft.source || []).push(
          { device: 'phone', kind: 'local', path: '', clear_after_import: false });
      }
      renderSettings();
      return;
    }

    const del = e.target.closest('[data-del]');
    if (del && draft) {
      collect();
      const list = del.dataset.del === 'replica' ? draft.replica : draft.source;
      list.splice(Number(del.dataset.i), 1);
      renderSettings();
      return;
    }

    const chip = e.target.closest('[data-drive]');
    if (chip && draft) {
      collect();
      const dr = drives[Number(chip.dataset.drive)];
      const target = (draft.replica || []).find((r) => !r.root);
      const row = target || { name: dr.label.toLowerCase().replace(/[^a-z0-9]+/g, ''),
                              kind: 'local', offline: true, mode: 'full',
                              capacity: 'auto' };
      row.root = `${dr.path}/PhotoVault/library`;
      if (!target) (draft.replica = draft.replica || []).push(row);
      renderSettings();
      banner(`Added ${dr.label}. Review the path, then Save.`, 'ok');
    }
  });

  // Re-render when a kind changes so placeholders and extra fields follow.
  document.addEventListener('change', (e) => {
    if (e.target.matches('.rows [data-field="kind"]') && draft) {
      collect();
      renderSettings();
    }
  });
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

  // Refresh on the *newest job's identity and state*, not on the progress bar
  // becoming hidden. A job that starts and finishes between two polls never
  // shows a bar, and the views would then silently never update.
  const newest = jobs[0];
  const signature = newest ? `${newest.id}:${newest.state}` : '';
  if (signature !== state.lastJobSignature) {
    const settled = newest && newest.state !== 'running';
    state.lastJobSignature = signature;
    if (settled) {
      refreshStatus();
      loadTimeline();
      loadPhotos();
      loadStaged();
      if (state.view === 'duplicates') loadDuplicates();
    }
  }

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
  } else {
    bar.hidden = true;
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
  if (view === 'duplicates') loadDuplicates();
  if (view === 'settings') loadSettings().catch((e) => banner(e.message, 'bad'));
}

function attach() {
  attachSettings();
  attachDuplicates();
  attachUploads();
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
  await Promise.all([refreshStatus(), loadTimeline(), loadPhotos(), loadStaged()]);
  refreshJobs();
  // Poll rather than push: for a single-user local app this is a few bytes a
  // second and avoids a WebSocket's reconnection and lifecycle handling.
  setInterval(refreshJobs, 1000);
  setInterval(() => { if (!state.busy) refreshStatus(); }, 15000);
}

init().catch((err) => toast(`Could not start: ${err.message}`));
