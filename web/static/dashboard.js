// ─── Mini chess board renderer ─────────────────────────────────────────────
const PIECES = {
  K:'♔', Q:'♕', R:'♖', B:'♗', N:'♘', P:'♙',
  k:'♚', q:'♛', r:'♜', b:'♝', n:'♞', p:'♟',
};

function renderMiniBoard(fen, orig, dest, size = 128) {
  const sq = size / 8;
  const pos = fen.split(' ')[0];
  const ranks = pos.split('/');
  const cells = [];

  for (let r = 0; r < 8; r++) {
    let f = 0;
    for (const ch of ranks[r]) {
      if (ch >= '1' && ch <= '8') {
        for (let i = 0; i < +ch; i++) cells.push({ piece: null, r, f: f++ });
      } else {
        cells.push({ piece: ch, r, f: f++ });
      }
    }
  }

  const files = 'abcdefgh';
  const sqName = (r, f) => files[f] + (8 - r);

  const cellsHtml = cells.map(({ piece, r, f }) => {
    const name    = sqName(r, f);
    const isLight = (r + f) % 2 === 0;
    const isOrig  = name === orig;
    const isDest  = name === dest;

    let bg;
    if (isOrig)        bg = '#aaa23a';
    else if (isDest)   bg = '#cdd26a';
    else               bg = isLight ? '#ede0c8' : '#9c7248';

    const pu = piece ? (PIECES[piece] || '') : '';
    const isWhite = piece && piece === piece.toUpperCase();
    const col    = isWhite ? '#1c1208' : '#f5e6cc';
    const shadow = isWhite
      ? '0 1px 1px rgba(0,0,0,0.6),0 0 2px rgba(0,0,0,0.4)'
      : '0 1px 1px rgba(255,255,255,0.3),0 0 2px rgba(0,0,0,0.5)';

    return `<div style="width:${sq}px;height:${sq}px;background:${bg};display:flex;`
         + `align-items:center;justify-content:center;font-size:${sq*0.72}px;`
         + `line-height:1;color:${col};text-shadow:${shadow};">${pu}</div>`;
  }).join('');

  return `<div style="display:grid;grid-template-columns:repeat(8,${sq}px);`
       + `border-radius:4px;overflow:hidden;box-shadow:0 2px 10px rgba(0,0,0,0.5);">`
       + cellsHtml + `</div>`;
}

// ─── Eval bar ──────────────────────────────────────────────────────────────
function evalBar(gapCp) {
  const width = Math.min(gapCp / 200 * 100, 100).toFixed(1);
  return `<div class="eval-bar-track"><div class="eval-bar-fill" style="width:${width}%;"></div></div>`;
}

// ─── Gap color ─────────────────────────────────────────────────────────────
function gapColor(cp) {
  if (cp >= 75)  return '#f87171';
  if (cp >= 40)  return '#f97316';
  return '#fbbf24';
}

function evalColor(cp) {
  if (cp >= 50)  return '#4ade80';
  if (cp >= 10)  return '#fbbf24';
  if (cp >= -10) return '#e5e7eb';
  return '#f87171';
}

function nagLabel(cp) { return cp >= 75 ? '?' : '?!'; }

// ─── Stat card ────────────────────────────────────────────────────────────
function statCard(value, label, accent = '#fbbf24') {
  return `<div class="rounded-xl border border-gray-800 p-5 text-center" style="background:#0f172a;">
    <div class="text-3xl font-bold mb-1" style="color:${accent};">${value}</div>
    <div class="text-gray-500 text-xs uppercase tracking-wider">${label}</div>
  </div>`;
}

// ─── Compact relative timestamp ───────────────────────────────────────────
function compactTime(iso) {
  const diff = (Date.now() - new Date(iso)) / 1000;  // seconds ago
  if (diff < 60)          return 'just now';
  if (diff < 3600)        return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400)       return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 7 * 86400)   return `${Math.floor(diff / 86400)}d ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

// ─── Recent job row ───────────────────────────────────────────────────────
function recentJobRow(j) {
  const statusColor = { done:'#4ade80', running:'#60a5fa', failed:'#f87171' };
  const col   = statusColor[j.status] || '#9ca3af';
  const time  = compactTime(j.started_at);
  const p     = j.params || {};
  const label = j.command === 'fetch'      ? `${p.username || ''} / ${p.color || ''}${p.platform ? ' · '+p.platform : ''}`
              : j.command === 'import'     ? `${p.username || ''} / ${p.color || ''} — ${p.filename || 'PGN'}`
              : j.command === 'habits'     ? `${p.username || ''} / ${p.color || ''} — habits (${p.platform || 'lichess'})`
              : j.command === 'repertoire' ? `${p.username || ''} / ${p.color || ''} — repertoire`
              : j.command === 'strategise' ? `${p.player || ''} (${p.player_color || '?'}) vs ${p.opponent || ''}`
              : `${p.side || ''} · player=${p.player||'—'} opp=${p.opponent||'—'}`;

  return `<div class="flex items-center gap-3 rounded-lg border border-gray-800 px-4 py-3 text-sm" style="background:#0f172a;">
    <span class="font-mono text-xs text-gray-600">${j.id.slice(0,8)}</span>
    <span class="text-xs bg-gray-800 px-2 py-0.5 rounded text-gray-400">${j.command}</span>
    <span class="text-xs font-mono" style="color:${col};">${j.status}</span>
    <span class="text-gray-400 truncate flex-1">${label}</span>
    <span class="text-gray-600 text-xs shrink-0">${time}</span>
    <a href="/jobs/${j.id}" class="text-amber-500 text-xs hover:underline shrink-0">View →</a>
  </div>`;
}

// ─── Render habits panel ──────────────────────────────────────────────────
function renderHabitsPanel(habits, stats) {
  if (!habits.length) return '';

  const username = habits[0].username || '';
  const jobId    = habits[0].job_id   || '';

  const header = `<div class="flex items-center justify-between px-5 py-4 border-b border-gray-800">
    <div>
      <span class="text-xs font-semibold text-gray-400 uppercase tracking-wider">Worst Habits</span>
      ${username ? `<span class="text-gray-600 text-xs ml-2">· ${username}</span>` : ''}
    </div>
    <a href="/jobs/${jobId}/habits-browser" class="text-xs text-amber-500 hover:underline">Browse all →</a>
  </div>`;

  const rows = habits.map(h => {
    const nag  = nagLabel(h.eval_gap_cp);
    const col  = gapColor(h.eval_gap_cp);
    const board = renderMiniBoard(h.fen, h.player_move_orig, h.player_move_dest, 80);
    return `<div class="flex items-center gap-4 px-5 py-4 border-b border-gray-900 hover:bg-white/[0.02] transition-colors">
      <div class="shrink-0">${board}</div>
      <div class="flex-1 min-w-0">
        <div class="flex items-baseline gap-2 mb-1">
          <span class="font-mono font-bold text-base" style="color:#f87171;">${h.player_move_san}${nag}</span>
          <span class="text-gray-600 text-xs">→</span>
          <span class="font-mono text-sm text-green-400">${h.best_move_san}</span>
        </div>
        <div class="flex items-center gap-2 mb-2">
          ${evalBar(h.eval_gap_cp)}
          <span class="font-mono text-xs shrink-0" style="color:${col};">+${h.eval_gap_cp.toFixed(0)}&nbsp;cp</span>
        </div>
        <div class="flex gap-3 text-xs text-gray-600">
          <span>${h.total_games}× at position</span>
          <span>score&nbsp;${h.score.toFixed(1)}</span>
        </div>
      </div>
    </div>`;
  }).join('');

  const footer = `<div class="px-5 py-3 flex gap-3">
    <a href="/jobs/${jobId}/habits-practice" class="text-xs text-amber-400 hover:underline font-semibold">▶ Practice Mode</a>
    <a href="/jobs/${jobId}/habits-browser"  class="text-xs text-gray-500 hover:underline">Browse &amp; review →</a>
  </div>`;

  return header + rows + footer;
}

// ─── Placeholder CTA panel ────────────────────────────────────────────────
function renderPlaceholder(icon, title, desc, href, cta) {
  return `<div class="flex flex-col items-center justify-center text-center px-8 h-full" style="min-height:260px;">
    <div class="text-5xl mb-4" style="opacity:0.18;">${icon}</div>
    <h3 class="text-gray-400 font-semibold text-sm mb-2">${title}</h3>
    <p class="text-gray-600 text-xs leading-relaxed mb-6" style="max-width:22ch;">${desc}</p>
    <a href="${href}" class="no-underline text-xs font-semibold text-amber-400 px-4 py-2 rounded-lg border border-amber-900 hover:border-amber-600 hover:bg-amber-900/20 transition-colors">${cta}</a>
  </div>`;
}

// ─── Render novelties panel ───────────────────────────────────────────────
function renderNoveltyPanel(novelties) {
  if (!novelties.length) return '';

  const jobId = novelties[0].job_id || '';

  function moveLabel(n) {
    const ply = n.book_moves_san.length;
    const num = Math.floor(ply / 2) + 1;
    const dots = ply % 2 === 1 ? '…' : '.';
    return `${num}${dots}${n.novelty_san}`;
  }

  const header = `<div class="flex items-center justify-between px-5 py-4 border-b border-gray-800">
    <span class="text-xs font-semibold text-gray-400 uppercase tracking-wider">Best Novelties</span>
    <a href="/jobs/${jobId}/novelties" class="text-xs text-amber-500 hover:underline">Browse all →</a>
  </div>`;

  const rows = novelties.map(n => {
    const ec   = evalColor(n.eval_cp);
    const isTN = n.post_novelty_games === 0;
    const post = isTN
      ? `<span style="color:#60a5fa;font-size:10px;">True Novelty</span>`
      : `<span class="text-gray-600 text-xs">${n.post_novelty_games} post-games</span>`;

    return `<div class="flex items-center gap-3 px-5 py-3 border-b border-gray-900 hover:bg-white/[0.02] transition-colors">
      <span class="text-gray-600 text-xs w-5 text-right shrink-0">${n.rank}</span>
      <span class="font-mono font-bold text-sm flex-1" style="color:#e5e7eb;">${moveLabel(n)}</span>
      <span class="font-mono text-xs shrink-0" style="color:${ec};">${n.eval_cp >= 0 ? '+' : ''}${n.eval_cp.toFixed(0)}&nbsp;cp</span>
      <span class="shrink-0 text-xs">${post}</span>
    </div>`;
  }).join('');

  const footer = `<div class="px-5 py-3">
    <a href="/jobs/${jobId}/novelties" class="text-xs text-gray-500 hover:underline">Full analysis →</a>
  </div>`;

  return header + rows + footer;
}

// ─── Render bots panel ────────────────────────────────────────────────────
function renderBotsPanel(bots) {
  const header = `<div class="flex items-center justify-between px-5 py-4 border-b border-gray-800">
    <span class="text-xs font-semibold text-gray-400 uppercase tracking-wider">Your Bots</span>
    <a href="/bots" class="text-xs text-amber-500 hover:underline">Manage →</a>
  </div>`;

  if (!bots.length) {
    return header + `<div class="px-5 py-6 text-center">
      <p class="text-gray-600 text-xs mb-3">No bots ready yet. Train one to practice against an opponent.</p>
      <a href="/bots" class="text-xs font-semibold text-amber-400 px-4 py-2 rounded-lg border border-amber-900 hover:border-amber-600 hover:bg-amber-900/20 transition-colors">Create a Bot →</a>
    </div>`;
  }

  const rows = bots.map(b => {
    const elo = b.opponent_elo ? `<span class="text-gray-500 text-xs">${b.opponent_elo} Elo</span>` : '';
    return `<div class="flex items-center gap-4 px-5 py-4 border-b border-gray-900 hover:bg-white/[0.02] transition-colors">
      <div class="w-9 h-9 rounded-full bg-gray-800 flex items-center justify-center text-sm font-bold text-gray-300 shrink-0 border border-gray-700">
        ${b.opponent_username[0].toUpperCase()}
      </div>
      <div class="flex-1 min-w-0">
        <div class="font-medium text-white text-sm truncate">${b.opponent_username}</div>
        <div class="flex gap-2 items-center mt-0.5">${elo}<span class="text-gray-600 text-xs">${b.opponent_platform}</span></div>
      </div>
      <a href="/bots/${b.id}/practice"
         class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white text-xs font-medium shrink-0">
        Practice
      </a>
    </div>`;
  }).join('');

  return header + rows;
}

// ─── Main load ────────────────────────────────────────────────────────────
async function loadDashboard() {
  let data;
  try {
    const res = await fetch('/api/dashboard');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    data = await res.json();
  } catch (err) {
    document.getElementById('dash-loading').textContent = 'Could not load insights.';
    return;
  }

  document.getElementById('dash-loading').classList.add('hidden');

  const { stats, top_habits, top_novelties, recent_jobs, ready_bots = [] } = data;

  if (!stats.done_jobs && !recent_jobs.length) {
    document.getElementById('dash-empty').classList.remove('hidden');
    return;
  }

  document.getElementById('dash-content').classList.remove('hidden');

  // Stats
  const statsEl = document.getElementById('stats-row');
  statsEl.innerHTML =
    statCard(stats.done_jobs,       'Jobs Completed',  '#fbbf24') +
    statCard(stats.total_habits,    'Habits Analysed', '#f87171') +
    statCard(stats.total_novelties, 'Novelties Found', '#4ade80');

  // Habits panel — always visible; placeholder if no data yet
  const hp = document.getElementById('habits-panel');
  hp.classList.remove('hidden');
  hp.innerHTML = top_habits.length
    ? renderHabitsPanel(top_habits, stats)
    : renderPlaceholder('♞', 'No habit analysis yet',
        'Find positions where you consistently choose a suboptimal move — then drill the fix.',
        '/habits', 'Analyse Habits →');

  // Novelties panel — always visible; placeholder if no data yet
  const np = document.getElementById('novelties-panel');
  np.classList.remove('hidden');
  np.innerHTML = top_novelties.length
    ? renderNoveltyPanel(top_novelties)
    : renderPlaceholder('♕', 'No novelties found yet',
        'Discover surprise weapons and rare lines to take your opponent out of prep.',
        '/search', 'Find Novelties →');

  // Bots panel — always visible
  const bp = document.getElementById('bots-panel');
  bp.classList.remove('hidden');
  bp.innerHTML = renderBotsPanel(ready_bots);

  // Recent activity
  if (recent_jobs.length) {
    document.getElementById('recent-list').innerHTML =
      recent_jobs.map(recentJobRow).join('');
  }
}

loadDashboard();
