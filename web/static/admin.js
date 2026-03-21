const TITLED_ROLES = new Set(['GM','WGM','IM','WIM','FM','WFM','CM','WCM','NM']);
const ALL_ROLES = ['user','GM','WGM','IM','WIM','FM','WFM','CM','WCM','NM','admin'];

// ─── User map (id → username) built after users load ─────────────────
let userMap = {};

// ─── Stats ────────────────────────────────────────────────────────────
async function loadStats() {
  try {
    const r = await fetch('/api/admin/stats');
    if (!r.ok) throw new Error(r.status);
    const s = await r.json();
    document.getElementById('sv-users').textContent   = s.total_users;
    document.getElementById('sv-pro').textContent     = s.pro_subscribers;
    document.getElementById('sv-today').textContent   = s.jobs_today;
    document.getElementById('sv-running').textContent = s.running_jobs;
  } catch(e) {
    console.error('stats', e);
  }
}

// ─── Helpers ──────────────────────────────────────────────────────────
function fmtDate(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleString(undefined, {
    month:'short', day:'numeric', year:'numeric',
    hour:'2-digit', minute:'2-digit',
  });
}

function roleBadge(role) {
  if (role === 'admin')         return `<span class="badge badge-admin">${role}</span>`;
  if (TITLED_ROLES.has(role))   return `<span class="badge badge-titled">${role}</span>`;
  return `<span class="badge badge-free">${role}</span>`;
}

function planBadge(plan, sub_status, role) {
  // Mirror _effective_plan(): admins and titled players always get pro.
  const effectivePro = role === 'admin' || TITLED_ROLES.has(role)
    || (plan === 'pro' && ['active', 'trialing'].includes(sub_status));
  if (effectivePro) return `<span class="badge badge-pro">pro</span>`;
  return `<span class="badge badge-free">free</span>`;
}

function statusBadge(status) {
  const cls = {running:'badge-running', done:'badge-done', failed:'badge-failed',
                queued:'badge-queued'}[status] || 'badge-other';
  return `<span class="badge ${cls}">${status}</span>`;
}

function escHtml(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ─── Users ────────────────────────────────────────────────────────────
async function loadUsers() {
  try {
    const r = await fetch('/api/admin/users');
    if (!r.ok) throw new Error(r.status);
    const users = await r.json();

    userMap = {};
    users.forEach(u => { userMap[u.id] = u.username; });

    document.getElementById('users-count').textContent = `${users.length} users`;
    const tbody = document.getElementById('users-tbody');
    if (!users.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="px-4 py-6 text-center text-gray-600 text-xs">No users yet.</td></tr>';
      return;
    }
    tbody.innerHTML = users.map(u => `
      <tr class="admin-table-row" data-uid="${u.id}">
        <td class="px-4 py-3 font-mono text-sm text-gray-200">${escHtml(u.username)}</td>
        <td class="px-4 py-3 text-xs text-gray-500">${escHtml(u.platform)}</td>
        <td class="px-4 py-3">
          <select class="role-select" data-uid="${u.id}" onchange="setRole('${u.id}', this.value)">
            ${ALL_ROLES.map(r => `<option value="${r}" ${r === u.role ? 'selected' : ''}>${r}</option>`).join('')}
          </select>
        </td>
        <td class="px-4 py-3">${planBadge(u.plan, u.sub_status, u.role)}</td>
        <td class="px-4 py-3 text-right font-mono text-xs text-gray-400">${u.total_jobs}</td>
        <td class="px-4 py-3 text-xs text-gray-500">${fmtDate(u.last_active)}</td>
        <td class="px-4 py-3 text-xs text-gray-600">${fmtDate(u.created_at)}</td>
      </tr>
    `).join('');
  } catch(e) {
    document.getElementById('users-tbody').innerHTML =
      `<tr><td colspan="7" class="px-4 py-4 text-red-400 text-xs px-4">Error: ${e}</td></tr>`;
  }
}

async function setRole(userId, role) {
  try {
    const r = await fetch(`/api/admin/users/${userId}/role`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ role }),
    });
    if (!r.ok) {
      const d = await r.json();
      alert(`Error: ${d.error || r.status}`);
    }
  } catch(e) {
    alert(`Network error: ${e}`);
  }
}

// ─── Jobs ─────────────────────────────────────────────────────────────
async function loadJobs() {
  try {
    const r = await fetch('/api/admin/jobs');
    if (!r.ok) throw new Error(r.status);
    const jobs = await r.json();
    document.getElementById('jobs-count').textContent = `last ${jobs.length}`;
    const tbody = document.getElementById('jobs-tbody');
    if (!jobs.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="px-4 py-6 text-center text-gray-600 text-xs">No jobs yet.</td></tr>';
      return;
    }
    tbody.innerHTML = jobs.map(j => {
      const p = j.params || {};
      const desc = j.command === 'fetch'      ? `${p.username||''} / ${p.color||''}`
                 : j.command === 'import'     ? `${p.username||''} / ${p.color||''} — ${p.filename||'PGN'}`
                 : j.command === 'habits'     ? `${p.username||''} / ${p.color||''}`
                 : j.command === 'repertoire' ? `${p.username||''} / ${p.color||''}`
                 : j.command === 'strategise' ? `${p.player||''} vs ${p.opponent||''}`
                 : j.command === 'search'     ? `${p.side||''} · ${p.player||p.username||''}`
                 : JSON.stringify(p).slice(0,40);
      const username = j.user_id ? (userMap[j.user_id] || j.user_id.slice(0,8)) : '—';
      return `
        <tr class="admin-table-row">
          <td class="px-4 py-3 font-mono text-xs text-gray-600">
            <a href="/jobs/${j.id}" class="hover:text-amber-400 transition-colors">${j.id.slice(0,8)}</a>
          </td>
          <td class="px-4 py-3">
            <span class="text-xs bg-gray-800 px-2 py-0.5 rounded text-gray-400">${escHtml(j.command)}</span>
          </td>
          <td class="px-4 py-3">${statusBadge(j.status)}</td>
          <td class="px-4 py-3 text-xs text-gray-400">${escHtml(username)}</td>
          <td class="px-4 py-3 text-xs text-gray-500 max-w-xs truncate">${escHtml(desc)}</td>
          <td class="px-4 py-3 text-xs text-gray-600">${fmtDate(j.started_at)}</td>
        </tr>
      `;
    }).join('');
  } catch(e) {
    document.getElementById('jobs-tbody').innerHTML =
      `<tr><td colspan="6" class="px-4 py-4 text-red-400 text-xs">Error: ${e}</td></tr>`;
  }
}

// ─── Featured Players ────────────────────────────────────────────────
let _players = [];

async function loadPlayers() {
  try {
    const r = await fetch('/api/admin/players');
    if (!r.ok) throw new Error(r.status);
    const players = await r.json();
    _players = players;
    const tbody = document.getElementById('players-tbody');
    if (!players.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="px-4 py-6 text-center text-gray-600 text-xs">No featured players yet.</td></tr>';
      return;
    }
    tbody.innerHTML = players.map(p => {
      const statusCls = p.status === 'ready' ? 'badge-done' : p.status === 'failed' ? 'badge-failed' : 'badge-queued';
      return `
        <tr class="admin-table-row">
          <td class="px-4 py-3 font-mono text-xs text-gray-300">
            <a href="/players/${escHtml(p.slug)}" class="hover:text-blue-400">${escHtml(p.slug)}</a>
          </td>
          <td class="px-4 py-3 text-sm text-gray-200">${escHtml(p.display_name)}
            ${p.title ? `<span class="badge badge-titled ml-1">${escHtml(p.title)}</span>` : ''}
          </td>
          <td class="px-4 py-3 text-xs text-gray-500">${escHtml(p.platform)}</td>
          <td class="px-4 py-3 text-xs text-gray-400">${p.elo || '—'}</td>
          <td class="px-4 py-3"><span class="badge ${statusCls}">${escHtml(p.status)}</span></td>
          <td class="px-4 py-3 flex gap-2">
            <button onclick="editPlayer('${escHtml(p.slug)}')"
                    class="text-xs bg-blue-900 hover:bg-blue-800 text-blue-300 px-2 py-1 rounded transition-colors">
              Edit
            </button>
            <button onclick="retrainPlayer('${escHtml(p.slug)}')"
                    class="text-xs bg-gray-800 hover:bg-gray-700 text-gray-300 px-2 py-1 rounded transition-colors">
              Re-train
            </button>
            <button onclick="deletePlayer('${escHtml(p.slug)}')"
                    class="text-xs bg-red-900 hover:bg-red-800 text-red-300 px-2 py-1 rounded transition-colors">
              Delete
            </button>
          </td>
        </tr>
      `;
    }).join('');
  } catch(e) {
    document.getElementById('players-tbody').innerHTML =
      `<tr><td colspan="6" class="px-4 py-4 text-red-400 text-xs px-4">Error: ${e}</td></tr>`;
  }
}

function editPlayer(slug) {
  const p = _players.find(x => x.slug === slug);
  if (!p) return;
  document.getElementById('edit-slug').value          = slug;
  document.getElementById('edit-display-name').value  = p.display_name || '';
  document.getElementById('edit-title').value         = p.title || '';
  document.getElementById('edit-description').value   = p.description || '';
  document.getElementById('edit-msg').textContent     = '';
  document.getElementById('edit-photo-msg').textContent = '';
  document.getElementById('edit-photo-file').value    = '';
  const pos = p.photo_position != null ? p.photo_position : 25;
  document.getElementById('edit-photo-position').value = pos;
  document.getElementById('edit-photo-position-val').textContent = pos + '%';
  const preview = document.getElementById('edit-photo-preview');
  const img = document.getElementById('edit-photo-img');
  if (p.photo_url) {
    img.src = p.photo_url + '?t=' + Date.now();
    preview.classList.remove('hidden');
  } else {
    preview.classList.add('hidden');
  }
  document.getElementById('edit-modal').classList.remove('hidden');
}

function closeEditModal() {
  document.getElementById('edit-modal').classList.add('hidden');
}

async function uploadPhoto() {
  const slug = document.getElementById('edit-slug').value;
  const fileInput = document.getElementById('edit-photo-file');
  const file = fileInput.files[0];
  const msg = document.getElementById('edit-photo-msg');
  if (!file) { msg.style.color = '#f87171'; msg.textContent = 'Select a file first.'; return; }
  const formData = new FormData();
  formData.append('photo', file);
  msg.style.color = '#9ca3af';
  msg.textContent = 'Uploading…';
  try {
    const r = await fetch(`/api/admin/players/${slug}/photo`, { method: 'POST', body: formData });
    const d = await r.json();
    if (!r.ok) {
      msg.style.color = '#f87171';
      msg.textContent = `Error: ${d.error || r.status}`;
    } else {
      msg.style.color = '#4ade80';
      msg.textContent = 'Uploaded!';
      const img = document.getElementById('edit-photo-img');
      img.src = d.photo_url + '?t=' + Date.now();
      document.getElementById('edit-photo-preview').classList.remove('hidden');
      const p = _players.find(x => x.slug === slug);
      if (p) p.photo_url = d.photo_url;
    }
  } catch(e) {
    msg.style.color = '#f87171';
    msg.textContent = `Network error: ${e}`;
  }
}

async function savePlayerEdit() {
  const slug = document.getElementById('edit-slug').value;
  const data = {
    display_name:   document.getElementById('edit-display-name').value.trim(),
    title:          document.getElementById('edit-title').value.trim(),
    description:    document.getElementById('edit-description').value.trim(),
    photo_position: parseInt(document.getElementById('edit-photo-position').value),
  };
  const msg = document.getElementById('edit-msg');
  msg.style.color = '#9ca3af';
  msg.textContent = 'Saving…';
  try {
    const r = await fetch(`/api/admin/players/${slug}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    const d = await r.json();
    if (!r.ok) {
      msg.style.color = '#f87171';
      msg.textContent = `Error: ${d.error || r.status}`;
    } else {
      msg.style.color = '#4ade80';
      msg.textContent = 'Saved.';
      setTimeout(() => { closeEditModal(); loadPlayers(); }, 700);
    }
  } catch(e) {
    msg.style.color = '#f87171';
    msg.textContent = `Network error: ${e}`;
  }
}

async function addPlayer(e) {
  e.preventDefault();
  const form = e.target;
  const data = Object.fromEntries(new FormData(form).entries());
  const msg = document.getElementById('add-player-msg');
  msg.textContent = 'Submitting…';
  msg.style.color = '#9ca3af';
  try {
    const r = await fetch('/api/admin/players', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    const d = await r.json();
    if (!r.ok) {
      msg.textContent = `Error: ${d.error || r.status}`;
      msg.style.color = '#f87171';
    } else {
      msg.innerHTML = `Training job started — <a href="/jobs/${d.job_id}" class="underline hover:text-green-300">${d.job_id?.slice(0,8)}</a>`;
      msg.style.color = '#4ade80';
      form.reset();
      setTimeout(() => { loadPlayers(); loadJobs(); }, 1000);
    }
  } catch(err) {
    msg.textContent = `Network error: ${err}`;
    msg.style.color = '#f87171';
  }
}

function retrainPlayer(slug) {
  const p = _players.find(x => x.slug === slug);
  if (!p) return;
  document.getElementById('retrain-slug').value         = slug;
  document.getElementById('retrain-player-name').textContent = p.display_name;
  document.getElementById('retrain-platform').value     = p.platform || 'lichess';
  document.getElementById('retrain-username').value     = p.username || '';
  document.getElementById('retrain-speeds').value       = p.speeds || 'blitz,rapid';
  document.getElementById('retrain-msg').textContent    = '';
  document.getElementById('retrain-modal').classList.remove('hidden');
}

function closeRetrainModal() {
  document.getElementById('retrain-modal').classList.add('hidden');
}

async function submitRetrain() {
  const slug     = document.getElementById('retrain-slug').value;
  const platform = document.getElementById('retrain-platform').value.trim();
  const username = document.getElementById('retrain-username').value.trim();
  const speeds   = document.getElementById('retrain-speeds').value.trim();
  const msg      = document.getElementById('retrain-msg');

  if (!username) { msg.textContent = 'Username is required.'; msg.style.color = '#f87171'; return; }

  msg.textContent = 'Starting…';
  msg.style.color = '#9ca3af';
  try {
    const r = await fetch(`/api/admin/players/${slug}/retrain`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ platform, username, speeds }),
    });
    const d = await r.json();
    if (!r.ok) { msg.textContent = `Error: ${d.error || r.status}`; msg.style.color = '#f87171'; return; }
    closeRetrainModal();
    loadPlayers();
  } catch(e) { msg.textContent = `Network error: ${e}`; msg.style.color = '#f87171'; }
}

async function deletePlayer(slug) {
  if (!confirm(`Delete featured player "${slug}" and their book files?`)) return;
  try {
    const r = await fetch(`/api/admin/players/${slug}`, { method: 'DELETE' });
    if (!r.ok) { const d = await r.json(); alert(`Error: ${d.error || r.status}`); return; }
    loadPlayers();
  } catch(e) { alert(`Network error: ${e}`); }
}

// ─── Backups ──────────────────────────────────────────────────────────
function toggleBackupForm() {
  const f = document.getElementById('backup-form');
  f.style.display = f.style.display === 'none' ? 'flex' : 'none';
}

function showBackupStatus(msg, type = 'info') {
  const el = document.getElementById('backup-status');
  const colors = {
    info:    'border-blue-900 bg-blue-950/30 text-blue-300',
    success: 'border-green-900 bg-green-950/30 text-green-300',
    error:   'border-red-900 bg-red-950/30 text-red-300',
  };
  el.className = `rounded-lg px-4 py-3 mb-4 text-sm border ${colors[type] || colors.info}`;
  el.textContent = msg;
  el.style.display = 'block';
}

function pollBackupStatus(opId, onDone) {
  const interval = setInterval(async () => {
    try {
      const res = await fetch(`/api/admin/backups/${opId}/status`);
      const data = await res.json();
      if (data.status === 'running') {
        showBackupStatus(data.message || 'Working…', 'info');
      } else if (data.status === 'done') {
        clearInterval(interval);
        showBackupStatus(data.message || 'Done.', 'success');
        onDone && onDone();
      } else if (data.status === 'failed') {
        clearInterval(interval);
        showBackupStatus(`Failed: ${data.message}`, 'error');
      }
    } catch (e) {
      clearInterval(interval);
      showBackupStatus(`Error polling status: ${e}`, 'error');
    }
  }, 2000);
}

async function createBackup() {
  const description = document.getElementById('backup-description').value.trim();
  showBackupStatus('Creating backup…', 'info');
  document.getElementById('backup-form').style.display = 'none';
  try {
    const res = await fetch('/api/admin/backup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ description }),
    });
    const data = await res.json();
    pollBackupStatus(data.id, () => {
      document.getElementById('backup-description').value = '';
      loadBackups();
    });
  } catch (e) {
    showBackupStatus(`Error: ${e}`, 'error');
  }
}

async function loadBackups() {
  const tbody = document.getElementById('backups-list');
  try {
    const res = await fetch('/api/admin/backups');
    const backups = await res.json();
    if (!backups.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="px-4 py-6 text-center text-gray-600">No backups yet.</td></tr>';
      return;
    }
    tbody.innerHTML = backups.map(b => {
      const date = new Date(b.created_at).toLocaleString();
      const commit = (b.git_commit || 'unknown').slice(0, 8);
      const desc = b.description || '—';
      return `<tr class="border-b border-gray-900 hover:bg-white/[0.02]">
        <td class="px-4 py-3 font-mono text-gray-400">${date}</td>
        <td class="px-4 py-3 text-gray-300">${desc}</td>
        <td class="px-4 py-3 font-mono text-amber-500">${commit}</td>
        <td class="px-4 py-3 text-gray-500">${b.db_size_fmt || '—'}</td>
        <td class="px-4 py-3 text-gray-500">${b.files_size_fmt || '—'}</td>
        <td class="px-4 py-3 flex gap-2">
          <button onclick="restoreBackup('${b.id}','${commit}')"
                  class="text-xs px-2 py-1 rounded bg-blue-700 hover:bg-blue-600 text-white">Restore</button>
          <button onclick="deleteBackup('${b.id}')"
                  class="text-xs px-2 py-1 rounded bg-red-900/50 hover:bg-red-800 text-red-300">Delete</button>
        </td>
      </tr>`;
    }).join('');
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6" class="px-4 py-4 text-center text-red-500 text-xs">Error: ${e}</td></tr>`;
  }
}

async function restoreBackup(id, commitShort) {
  if (!confirm(
    `Restore backup ${id}?\n\n` +
    `Git commit at backup time: ${commitShort}\n\n` +
    `This will overwrite the current database and player files. ` +
    `The server will need to be restarted after restore. Continue?`
  )) return;
  showBackupStatus('Restoring…', 'info');
  try {
    const res = await fetch(`/api/admin/backups/${id}/restore`, { method: 'POST' });
    const data = await res.json();
    if (data.error) { showBackupStatus(`Error: ${data.error}`, 'error'); return; }
    pollBackupStatus(data.id, () => loadBackups());
  } catch (e) {
    showBackupStatus(`Error: ${e}`, 'error');
  }
}

async function deleteBackup(id) {
  if (!confirm(`Delete backup ${id}? This cannot be undone.`)) return;
  try {
    await fetch(`/api/admin/backups/${id}`, { method: 'DELETE' });
    loadBackups();
  } catch (e) {
    showBackupStatus(`Error deleting: ${e}`, 'error');
  }
}

// ─── Init ─────────────────────────────────────────────────────────────
loadStats();
loadUsers().then(() => loadJobs());
loadPlayers();
loadBackups();
