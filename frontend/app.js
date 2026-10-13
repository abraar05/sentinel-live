import { Player } from './player.js';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  devices: [],
  players: new Map(),      // deviceId -> Player
  layout: Number(localStorage.getItem('sentinel.layout') || 4),
  user: null,
  healthTimer: null,
};

// ------------------------------------------------------------------- utils

function toast(message, kind = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = message;
  $('#toasts').append(el);
  setTimeout(() => {
    el.style.opacity = '0';
    el.style.transition = 'opacity .3s';
    setTimeout(() => el.remove(), 320);
  }, 3200);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (res.status === 401) {
    showGate('Your session has expired. Please sign in again.');
    throw new Error('authentication required');
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* not json */ }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

// ------------------------------------------------------------------ gate

function showGate(message = '', needsSetup = false) {
  const gate = $('#gate');
  const sub = $('#gate-sub');
  const error = $('#gate-error');
  gate.hidden = false;
  sub.textContent = needsSetup
    ? 'First run — create the admin account for this instance.'
    : 'Sign in to view your cameras.';
  error.hidden = !message;
  error.textContent = message;
  setTimeout(() => $('#gate-user')?.focus(), 30);
}

function hideGate() {
  $('#gate').hidden = true;
}

async function submitLogin(event) {
  event.preventDefault();
  const button = $('#gate-submit');
  const error = $('#gate-error');
  button.disabled = true;
  error.hidden = true;
  try {
    await api('/api/auth/login', {
      method: 'POST',
      body: { username: $('#gate-user').value.trim(), password: $('#gate-pass').value },
    });
    $('#gate-pass').value = '';
    hideGate();
    await refresh();
  } catch (err) {
    error.hidden = false;
    error.textContent = err.message === 'authentication required'
      ? 'Incorrect username or password.'
      : err.message;
    $('#gate-pass').value = '';
    $('#gate-pass').focus();
  } finally {
    button.disabled = false;
  }
}

async function checkSession() {
  try {
    const res = await fetch('/api/auth/session', { credentials: 'same-origin' });
    const body = await res.json();
    if (body.authenticated) {
      state.user = body.user;
      hideGate();
      return true;
    }
    state.user = null;
    showGate('', body.needsSetup);
    return false;
  } catch {
    showGate('Cannot reach the gateway.');
    return false;
  }
}

function closeModal() {
  $('#modal-root').hidden = true;
  $('#modal-root').innerHTML = '';
}

function openModal(html, onMount) {
  const root = $('#modal-root');
  root.hidden = false;
  root.innerHTML = html;
  $$('[data-close]', root).forEach((b) => (b.onclick = closeModal));
  root.onclick = (e) => { if (e.target === root) closeModal(); };
  if (onMount) onMount(root);
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeModal();
});

const fmtBytes = (n) => {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
};
const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString();

// ------------------------------------------------------------------ render

function layoutFor(n) {
  if (n === 1) return { cols: 1, rows: 1 };
  if (n === 2) return { cols: 2, rows: 1 };
  if (n === 4) return { cols: 2, rows: 2 };
  if (n === 6) return { cols: 3, rows: 2 };
  if (n === 9) return { cols: 3, rows: 3 };
  if (n === 16) return { cols: 4, rows: 4 };
  return { cols: 4, rows: 4 };
}

function activeDevices() {
  const order = JSON.parse(localStorage.getItem('sentinel.order') || '[]');
  const byId = new Map(state.devices.map((d) => [d.id, d]));
  const ordered = order.map((id) => byId.get(id)).filter(Boolean);
  const rest = state.devices.filter((d) => !order.includes(d.id));
  return [...ordered, ...rest];
}

function statusClass(device) {
  const h = device.health;
  if (!h) return '';
  if (h.state === 'live') return 'live';
  if (h.state === 'reconnecting' || h.state === 'connecting') return 'warn';
  return 'bad';
}

function renderDevices() {
  const devices = activeDevices();
  $('#empty').classList.toggle('show', devices.length === 0);

  const grid = $('#grid');
  const cells = state.layout;
  const { cols } = layoutFor(cells);
  grid.dataset.columns = String(cols);

  const visible = devices.slice(0, cells);
  grid.style.gridTemplateRows = `repeat(${layoutFor(cells).rows}, minmax(0, 1fr))`;

  // Reconcile tiles.
  const wanted = new Set(visible.map((d) => d.id));
  for (const [id, player] of state.players) {
    if (!wanted.has(id)) {
      player.close();
      player.canvas.remove();
      state.players.delete(id);
    }
  }
  for (const device of visible) {
    let tile = grid.querySelector(`[data-device="${device.id}"]`);
    if (!tile) {
      tile = buildTile(device);
      grid.append(tile);
    }
    updateTile(tile, device);
  }
  $$('.tile', grid).forEach((tile) => {
    if (!wanted.has(tile.dataset.device)) tile.remove();
  });

  renderDeviceList(devices);

  if (!state.mayManage) {
    $('#btn-add').hidden = true;
    $('#btn-import').hidden = true;
    $('#empty-import').hidden = true;
  }
  if (!state.mayPtz) $$('.ptz').forEach((pad) => { pad.style.display = 'none'; });
  if (!state.mayRecord) {
    $$('[data-act="record"]').forEach((b) => { b.hidden = true; });
  }
}

function buildTile(device) {
  const tile = document.createElement('div');
  tile.className = 'tile';
  tile.dataset.device = device.id;
  tile.innerHTML = `
    <div class="tile-overlay">
      <div class="tile-top">
        <span class="badge"><span class="dot"></span><span class="st-label">idle</span></span>
        <span class="tile-title">${escapeHtml(device.name)}</span>
      </div>
      <div class="tile-bottom">
        <span class="tile-sub st-sub"></span>
        <span class="badge rec-badge hidden">REC</span>
      </div>
    </div>
    <div class="tile-tools">
      <button data-act="snapshot" title="Snapshot">⤓</button>
      <button data-act="record" title="Record">●</button>
      <button data-act="motion" title="Motion detection">◈</button>
      <button data-act="ptz" title="PTZ pad">✥</button>
      <button data-act="expand" title="Fullscreen">⛶</button>
      <button data-act="menu" title="More">⋯</button>
    </div>
    <div class="tile-placeholder">
      <div class="big">◌</div>
      <div class="ph-text">starting…</div>
    </div>
    <div class="ptz">
      <span class="spacer"></span><button data-ptz="DirectionUp">▲</button><span class="spacer"></span>
      <button data-ptz="DirectionLeft">◀</button><span class="spacer"></span><button data-ptz="DirectionRight">▶</button>
      <span class="spacer"></span><button data-ptz="DirectionDown">▼</button><span class="spacer"></span>
      <button data-ptz="ZoomWide">－</button><button data-ptz="FocusFar">AF</button><button data-ptz="ZoomTile">＋</button>
    </div>`;

  const player = new Player(device.id, device.name);
  player.onstatus = () => updateTileStatus(tile, player, device);
  tile.insertBefore(player.canvas, tile.firstChild);
  state.players.set(device.id, player);
  player.connect();

  wireTile(tile, device, player);
  return tile;
}

function updateTile(tile, device) {
  const h = device.health;
  const dot = $('.dot', tile);
  const label = $('.st-label', tile);
  dot.className = `dot ${statusClass(device)}`;
  label.textContent = h ? h.state : 'idle';

  const bits = [];
  if (h?.codec) bits.push(h.codec.toUpperCase());
  if (h?.width) bits.push(`${h.width}x${h.height}`);
  if (h?.fps) bits.push(`${h.fps} fps`);
  if (h?.kbps) bits.push(`${Math.round(h.kbps)} kbps`);
  if (!h && device.cloud_id && !device.host) bits.push('no host');
  $('.st-sub', tile).textContent = bits.join(' · ');

  $('.rec-badge', tile).classList.toggle('hidden', !device.recording);
  $('[data-act="record"]', tile).classList.toggle('on', !!device.recording);
  $('[data-act="motion"]', tile).classList.toggle('on', !!device.motion);
}

function updateTileStatus(tile, player, device) {
  const ph = $('.tile-placeholder', tile);
  if (player.mode === 'live' || player.mode === 'snapshot-live') {
    ph.style.display = 'none';
  } else {
    ph.style.display = '';
    $('.ph-text', ph).textContent =
      player.detail ? `${player.state}: ${player.detail}` : (player.state || 'starting…');
  }
  const s = player.status();
  const h = device.health || {};
  $('.st-sub', tile).textContent = [
    s.codec ? s.codec.split('.')[0].toUpperCase() : h.codec?.toUpperCase(),
    s.width ? `${s.width}x${s.height}` : (h.width ? `${h.width}x${h.height}` : ''),
    s.fps ? `${s.fps} fps` : (h.fps ? `${h.fps} fps` : ''),
    s.mode === 'snapshot' ? 'still mode' : '',
  ].filter(Boolean).join(' · ');
}

function renderDeviceList(devices) {
  const list = $('#device-list');
  list.innerHTML = devices.map((d) => `
    <li class="device-row" data-device="${d.id}">
      <span class="dot ${statusClass(d)}"></span>
      <div class="meta">
        <div class="name">${escapeHtml(d.name)}</div>
        <div class="host">${escapeHtml(d.host || `cloud:${d.cloud_id || '?'}`)}:${d.port}</div>
        <div class="flags">
          ${d.recording ? '<span class="chip live">rec</span>' : ''}
          ${d.motion ? '<span class="chip live">motion</span>' : ''}
          ${d.cloud_id && !d.host ? '<span class="chip cloud">p2p id</span>' : ''}
          ${d.health?.state ? `<span class="chip">${d.health.state}</span>` : ''}
        </div>
      </div>
      <button class="icon-btn" data-act="show" title="Show in grid">▶</button>
      <button class="icon-btn" data-act="edit" title="Edit">✎</button>
    </li>`).join('');
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ------------------------------------------------------------- tile wiring

function wireTile(tile, device, player) {
  tile.addEventListener('click', (e) => {
    const tileEl = e.target.closest('.tile');
    $$('.tile').forEach((t) => t.classList.remove('focused'));
    tile.classList.add('focused');
    if (e.target.closest('.tile-tools, .ptz')) return;
    void tileEl;
  });

  $$('.tile-tools button', tile).forEach((btn) => {
    btn.onclick = (e) => {
      e.stopPropagation();
      const act = btn.dataset.act;
      if (act === 'snapshot') snapshot(device.id);
      else if (act === 'record') toggleRecord(device.id);
      else if (act === 'motion') toggleMotion(device.id);
      else if (act === 'ptz') tile.classList.toggle('menu-open');
      else if (act === 'expand') openFullscreen(tile, device);
      else if (act === 'menu') deviceMenu(device);
    };
  });

  $$('[data-ptz]', tile).forEach((btn) => {
    const command = btn.dataset.ptz;
    const press = async (e) => {
      e.preventDefault();
      e.stopPropagation();
      await ptz(device.id, command, 'Start');
      const release = async () => {
        window.removeEventListener('pointerup', release);
        window.removeEventListener('pointercancel', release);
        await ptz(device.id, command, 'Stop');
      };
      window.addEventListener('pointerup', release);
      window.addEventListener('pointercancel', release);
    };
    btn.addEventListener('pointerdown', press);
  });

  player.canvas.addEventListener('dblclick', () => openFullscreen(tile, device));
}

async function snapshot(deviceId) {
  try {
    const res = await fetch(`/api/devices/${deviceId}/snapshot.jpg`, { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `sentinel-${deviceId}-${Date.now()}.jpg`;
    a.click();
    URL.revokeObjectURL(url);
    toast('Snapshot saved', 'good');
  } catch (err) {
    toast(`Snapshot failed: ${err.message}`, 'bad');
  }
}

async function toggleRecord(deviceId) {
  const device = state.devices.find((d) => d.id === deviceId);
  try {
    await api(`/api/devices/${deviceId}/record`, {
      method: 'POST',
      body: { on: !device?.recording },
    });
    await refresh();
  } catch (err) {
    toast(err.message, 'bad');
  }
}

async function toggleMotion(deviceId) {
  const device = state.devices.find((d) => d.id === deviceId);
  try {
    await api(`/api/devices/${deviceId}/motion`, {
      method: 'POST',
      body: { on: !device?.motion },
    });
    await refresh();
  } catch (err) {
    toast(err.message, 'bad');
  }
}

async function ptz(deviceId, command, pattern) {
  try {
    await api(`/api/devices/${deviceId}/ptz`, {
      method: 'POST',
      body: { command, pattern },
    });
  } catch (err) {
    toast(`PTZ: ${err.message}`, 'bad');
  }
}

async function openFullscreen(tile, device) {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await tile.requestFullscreen();
  } catch {
    toast('Fullscreen blocked by the browser', 'bad');
  }
  void device;
}

function deviceMenu(device) {
  openModal(`
    <div class="modal">
      <h3>${escapeHtml(device.name)}</h3>
      <p class="hint">${escapeHtml(device.host || 'no host configured')} · port ${device.port}</p>
      <div class="modal-actions">
        <button class="ghost" id="m-probe">Probe</button>
        <button class="ghost" id="m-edit">Edit</button>
        <button class="ghost danger" id="m-del">Delete</button>
        <button class="primary" data-close>Done</button>
      </div>
    </div>`, () => {
    $('#m-probe').onclick = async () => {
      closeModal();
      try {
        const info = await api(`/api/devices/${device.id}/probe`, { method: 'POST' });
        toast(`${device.name}: ${info.systemInfo?.DeviceModel || 'probed OK'}`);
      } catch (err) {
        toast(`Probe failed: ${err.message}`, 'bad');
      }
    };
    $('#m-edit').onclick = () => { closeModal(); editDevice(device); };
    $('#m-del').onclick = async () => {
      closeModal();
      await api(`/api/devices/${device.id}`, { method: 'DELETE' });
      toast('Device deleted');
      refresh();
    };
  });
}

// ------------------------------------------------------------ device forms

function deviceForm(device) {
  const d = device || {};
  return `
    <div class="modal">
      <h3>${device ? 'Edit device' : 'Add device'}</h3>
      <p class="hint">Xiongmai DVRIP is the default. One device entry may cover several channels on an NVR.</p>
      <div class="field"><label>Name</label><input id="f-name" value="${escapeHtml(d.name || '')}" placeholder="Front gate"></div>
      <div class="field"><div class="row">
        <div><label>Host</label><input id="f-host" value="${escapeHtml(d.host || '')}" placeholder="192.168.1.64"></div>
        <div><label>Port</label><input id="f-port" type="number" value="${d.port || 34567}"></div>
      </div></div>
      <div class="field"><div class="row">
        <div><label>Username</label><input id="f-user" value="${escapeHtml(d.username || 'admin')}" autocomplete="off"></div>
        <div><label>Password</label><input id="f-pass" type="password" placeholder="${d.has_password ? '•••••• (unchanged)' : ''}" autocomplete="new-password"></div>
      </div></div>
      <div class="field"><label>Transport</label>
        <select id="f-transport">
          <option value="dvrip"${d.transport === 'dvrip' ? ' selected' : ''}>DVRIP (Xiongmai native)</option>
        </select>
      </div>
      <div class="field"><label>Storage Path (NAS/Network)</label>
        <input id="f-storage" value="${escapeHtml(d.storage_path || '')}" placeholder="\\\\nas\\share\\recordings or /mnt/nas/recordings">
        <p class="hint">Leave empty to use default. Supports UNC paths and mounted network drives.</p>
      </div>
      ${d.cloud_id ? `<div class="warnbox">Imported from a P2P export — cloud id <code>${escapeHtml(d.cloud_id)}</code>. That ID is only dialable through XMeye's proprietary cloud, so set an IP the device can be reached at.</div>` : ''}
      <div class="modal-actions">
        <button class="ghost" data-close>Cancel</button>
        <button class="primary" id="f-save">${device ? 'Save' : 'Add'}</button>
      </div>
    </div>`;
}

function editDevice(device) {
  openModal(deviceForm(device), () => {
    $('#f-save').onclick = async () => {
      const body = {
        name: $('#f-name').value.trim(),
        host: $('#f-host').value.trim(),
        port: Number($('#f-port').value) || 34567,
        username: $('#f-user').value.trim(),
      };
      const pass = $('#f-pass').value;
      if (pass) body.password = pass;
      const storagePath = $('#f-storage').value.trim();
      if (storagePath) body.storage_path = storagePath;
      if (!body.name) return toast('Name is required', 'bad');
      try {
        if (device) await api(`/api/devices/${device.id}`, { method: 'PATCH', body });
        else await api('/api/devices', { method: 'POST', body });
        closeModal();
        toast(device ? 'Device updated' : 'Device added', 'good');
        refresh();
      } catch (err) {
        toast(err.message, 'bad');
      }
    };
  });
}

function importModal() {
  openModal(`
    <div class="modal">
      <h3>Import DeviceManager.xml</h3>
      <p class="hint">Paste the XML exported from XMeye / gDMSS. Names and credentials come across; hosts do not, because P2P cloud ids cannot be dialled directly.</p>
      <div class="field"><textarea id="f-xml" placeholder="&lt;DeviceManager version=&quot;2.0&quot;&gt;…&lt;/DeviceManager&gt;"></textarea></div>
      <div class="modal-actions">
        <button class="ghost" data-close>Cancel</button>
        <button class="primary" id="i-go">Import</button>
      </div>
    </div>`, () => {
    $('#i-go').onclick = async () => {
      const xml = $('#f-xml').value.trim();
      if (!xml) return toast('Paste some XML first', 'bad');
      try {
        const res = await api('/api/devices/import', { method: 'POST', body: { xml } });
        closeModal();
        toast(`Imported ${res.imported.length} device(s)`, 'good');
        if (res.warnings.length) toast(res.warnings[0], '');
        await refresh();
        if (res.imported.length) editDevice(state.devices.find((d) => d.id === res.imported[0].id) || res.imported[0]);
      } catch (err) {
        toast(err.message, 'bad');
      }
    };
  });
}

function healthModal() {
  openModal(`
    <div class="modal">
      <h3>Stream health</h3>
      <p class="hint">Live counters from the preview sessions.</p>
      <div id="health-body"><p class="hint">Loading…</p></div>
      <div class="modal-actions"><button class="primary" data-close>Close</button></div>
    </div>`, loadHealthBody);
}

async function loadHealthBody() {
  const body = $('#health-body');
  if (!body) return;
  try {
    const data = await api('/api/health');
    if (!data.sessions.length) {
      body.innerHTML = '<p class="hint">No active sessions.</p>';
      return;
    }
    body.innerHTML = data.sessions.map((s) => `
      <div style="margin-bottom:12px">
        <div class="kv"><span>${escapeHtml(s.deviceName)}</span><span>${s.state}</span></div>
        <div class="kv"><span>${s.width || '?'}x${s.height || '?'} · ${(s.codec || '?').toUpperCase()}</span><span>${s.fps} fps · ${Math.round(s.kbps)} kbps</span></div>
        <div class="kv"><span>Staleness</span><span>${s.latencyMs == null ? '—' : `${s.latencyMs} ms`}</span></div>
        <div class="kv"><span>Reconnects · frames</span><span>${s.reconnects} · ${s.frames}</span></div>
        ${s.detail ? `<div class="warnbox">${escapeHtml(s.detail)}</div>` : ''}
      </div>`).join('');
  } catch (err) {
    body.innerHTML = `<p class="hint">${escapeHtml(err.message)}</p>`;
  }
}

function recordingsModal() {
  openModal(`
    <div class="modal">
      <h3>Recorded clips</h3>
      <p class="hint">Raw elementary streams, one file per segment.</p>
      <div id="rec-body"><p class="hint">Loading…</p></div>
      <div class="modal-actions"><button class="primary" data-close>Close</button></div>
    </div>`, loadRecordings);

  async function loadRecordings() {
    const body = $('#rec-body');
    if (!body) return;
    try {
      const { recordings } = await api('/api/recordings');
      body.innerHTML = recordings.length
        ? recordings.slice(0, 60).map((r) => `
          <div class="kv">
            <span>${escapeHtml(r.file)}</span>
            <span>${fmtBytes(r.size)} · ${fmtTime(r.modified)}
              <a href="/api/recordings/download?path=${encodeURIComponent(`${r.deviceId}/${r.file}`)}" download>⤓</a>
            </span>
          </div>`).join('')
        : '<p class="hint">Nothing recorded yet. Hit ● on a tile to start.</p>';
    } catch (err) {
      body.innerHTML = `<p class="hint">${escapeHtml(err.message)}</p>`;
    }
  }
}

function storageModal() {
  openModal(`
    <div class="modal">
      <h3>Storage Settings</h3>
      <p class="hint">Configure where recordings are saved. Supports local paths, NAS UNC paths, and mounted network drives.</p>
      <div class="field"><label>Base Recordings Directory</label>
        <input id="s-basedir" value="" placeholder="\\\\nas\\share\\recordings or /mnt/nas/recordings">
        <p class="hint">Leave empty to use default. All device recordings will be stored under this directory.</p>
      </div>
      <div id="storage-status"></div>
      <div class="modal-actions">
        <button class="ghost" data-close>Cancel</button>
        <button class="primary" id="s-save">Save</button>
      </div>
    </div>`, loadStorageStatus);

  async function loadStorageStatus() {
    const statusEl = $('#storage-status');
    if (!statusEl) return;
    try {
      const data = await api('/api/storage/status');
      statusEl.innerHTML = `
        <div class="kv"><span>Base Directory</span><span>${escapeHtml(data.base_dir || 'default')}</span></div>
        <div class="kv"><span>Recordings Dir</span><span>${escapeHtml(data.recordings_dir || 'default')}</span></div>
      `;
    } catch (err) {
      statusEl.innerHTML = `<p class="hint">${escapeHtml(err.message)}</p>`;
    }
  }

  $('#s-save').onclick = async () => {
    const baseDir = $('#s-basedir').value.trim();
    try {
      const res = await api('/api/storage/base-dir', {
        method: 'POST',
        body: { base_dir: baseDir },
      });
      toast(`Storage configured: ${res.recordings_dir}`, 'good');
      closeModal();
    } catch (err) {
      toast(err.message, 'bad');
    }
  };
}

function setupModal() {
  openModal(`
    <div class="modal">
      <h3>Getting video out of these devices</h3>
      <div class="prose">
        <p>Your <code>DeviceManager.xml</code> lists XMeye <b>P2P cloud ids</b> (like
        <code>AA0000000AAAAA1</code>). XMeye P2P is a proprietary UDP rendezvous
        in which the cloud brokers every connection, so those ids are not
        dialable by any third-party app — only by XMeye or gDMSS, which ship the
        vendor SDK.</p>
        <h4>Sentinel streams them directly instead</h4>
        <p>It speaks DVRIP — the Xiongmai protocol underneath, on port 34567 —
        against a host you can reach. Any one of these gets you there:</p>
        <ul>
          <li><b>LAN</b> — put the phone on the same network as the NVR and use its IP.</li>
          <li><b>Port forward</b> — forward TCP 34567 on the router to the NVR.</li>
          <li><b>VPN</b> — WireGuard/Tailscale to your network, then use the private IP.</li>
        </ul>
        <p>Credentials are stored AES-256-GCM encrypted in
        <code>data/device.key</code> + <code>data/devices.json</code>.</p>
      </div>
      <div class="modal-actions"><button class="primary" data-close>Got it</button></div>
    </div>`);
}

// ------------------------------------------------------------------- boot

async function refresh() {
  try {
    const data = await api('/api/devices');
    state.devices = data.devices;
    state.user = data.user || state.user;
    state.mayManage = data.mayManage;
    state.mayPtz = data.mayPtz;
    state.mayRecord = data.mayRecord;
    renderDevices();
    localStorage.setItem('sentinel.order', JSON.stringify(activeDevices().map((d) => d.id)));
    renderEvents();
  } catch (err) {
    if (err.message !== 'authentication required') toast(err.message, 'bad');
  }
}

async function renderEvents() {
  try {
    const data = await api('/api/health');
    const list = $('#event-list');
    if (!list) return;
    list.innerHTML = data.events.length
      ? data.events.map((e) => `
        <li class="event-row">
          <div class="who">${escapeHtml(e.deviceName || e.deviceId)}</div>
          <div class="when">${new Date((e.timestamp || e.ts) * 1000).toLocaleString()} · score ${e.score ?? '—'}</div>
        </li>`).join('')
      : '<li class="hint" style="color:var(--muted)">No motion events yet. Enable ◈ on a tile.</li>';
  } catch { /* non-fatal */ }
}

function wireChrome() {
  $$('.layouts button').forEach((btn) => {
    btn.onclick = () => {
      state.layout = Number(btn.dataset.layout);
      localStorage.setItem('sentinel.layout', String(state.layout));
      $$('.layouts button').forEach((b) => b.classList.toggle('active', b === btn));
      renderDevices();
    };
    btn.classList.toggle('active', Number(btn.dataset.layout) === state.layout);
  });

  $$('.side-tabs button').forEach((btn) => {
    btn.onclick = () => {
      $$('.side-tabs button').forEach((b) => b.classList.toggle('active', b === btn));
      $$('.tab-panel').forEach((p) => p.classList.toggle('active', p.dataset.panel === btn.dataset.tab));
    };
  });

  $('#btn-add').onclick = () => editDevice(null);
  $('#btn-import').onclick = importModal;
  $('#btn-health').onclick = healthModal;
  $('#btn-recordings').onclick = recordingsModal;
  $('#btn-storage').onclick = storageModal;
  $('#empty-import').onclick = importModal;

  $('#btn-probe-all').onclick = async () => {
    for (const device of state.devices) {
      try {
        await api(`/api/devices/${device.id}/probe`, { method: 'POST' });
      } catch (err) {
        toast(`${device.name}: ${err.message}`, 'bad');
      }
    }
    toast('Probe complete', 'good');
    refresh();
  };

  $('#btn-stop-all').onclick = async () => {
    for (const device of state.devices) {
      if (device.recording) await api(`/api/devices/${device.id}/record`, { method: 'POST', body: { on: false } });
    }
    for (const player of state.players.values()) player.close();
    await refresh();
    toast('Streams stopped');
  };

  $('#device-list').addEventListener('click', async (e) => {
    const row = e.target.closest('.device-row');
    if (!row) return;
    const device = state.devices.find((d) => d.id === row.dataset.device);
    if (!device) return;
    if (e.target.dataset.act === 'edit') return editDevice(device);
    if (e.target.dataset.act === 'show') {
      const order = activeDevices().map((d) => d.id).filter((id) => id !== device.id);
      order.unshift(device.id);
      localStorage.setItem('sentinel.order', JSON.stringify(order));
      renderDevices();
    }
  });

  $('#about-body').innerHTML = `
    <h4>Sentinel Live</h4>
    <p>DVRIP live view, PTZ, snapshots, recording and motion events for
    Xiongmai / XMeye hardware.</p>
    <button class="ghost small" id="about-help">How do I connect these?</button>
    <h4>Keyboard</h4>
    <ul>
      <li><code>1</code>–<code>6</code> switch grid layout</li>
      <li><code>H</code> health · <code>R</code> recordings · <code>N</code> add device</li>
    </ul>`;
  $('#about-help').onclick = setupModal;

  document.addEventListener('keydown', (e) => {
    if (e.target.matches('input, textarea')) return;
    const map = { 1: 1, 2: 2, 3: 4, 4: 6, 5: 9, 6: 16 };
    if (map[e.key]) {
      const btn = $(`.layouts button[data-layout="${map[e.key]}"]`);
      btn?.click();
    } else if (e.key === 'h') healthModal();
    else if (e.key === 'r') recordingsModal();
    else if (e.key === 'n') editDevice(null);
  });

  window.addEventListener('resize', () => {
    state.healthTimer = null;
  });

  $('#gate-form').addEventListener('submit', submitLogin);

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/app/sw.js').catch(() => { /* offline shell is optional */ });
  }
}

wireChrome();

(async () => {
  const ok = await checkSession();
  if (ok) await refresh();
  setInterval(async () => {
    if (!document.getElementById('gate').hidden) {
      // Still signed out - quietly pick up a login in another tab.
      await checkSession();
      return;
    }
    await refresh();
  }, 5000);
  setInterval(renderEvents, 10000);
})();