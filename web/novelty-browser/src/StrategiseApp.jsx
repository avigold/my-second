import React, { useEffect, useState } from 'react'
import Chessground from '@react-chess/chessground'
import ReactMarkdown from 'react-markdown'
import { Chess } from 'chess.js'

function useIsMobile(breakpoint = 640) {
  const [isMobile, setIsMobile] = useState(
    () => typeof window !== 'undefined' && window.innerWidth < breakpoint
  )
  useEffect(() => {
    const handler = () => setIsMobile(window.innerWidth < breakpoint)
    window.addEventListener('resize', handler)
    return () => window.removeEventListener('resize', handler)
  }, [breakpoint])
  return isMobile
}

// Resolve a move to { fen: postMoveFen, orig, dest }.
// Priority: explicit fen_after > orig/dest UCI > SAN string > fallback (pre-move FEN, no highlight).
function resolveMove(fen, fenAfter, orig, dest, moveSan) {
  if (fenAfter && orig && dest) return { fen: fenAfter, orig, dest }
  if (orig && dest) {
    try {
      const c = new Chess(fen)
      c.move({ from: orig, to: dest, promotion: 'q' })
      return { fen: c.fen(), orig, dest }
    } catch {}
  }
  if (moveSan) {
    try {
      const c = new Chess(fen)
      const m = c.move(moveSan)
      if (m) return { fen: c.fen(), orig: m.from, dest: m.to }
    } catch {}
  }
  return { fen, orig: null, dest: null }
}

// ---------------------------------------------------------------------------
// Colour palette
// ---------------------------------------------------------------------------
const C = {
  bg:        '#030712',
  surface:   '#0d1117',
  border:    '#1e2d45',
  border2:   '#1f2937',
  amber:     '#f59e0b',
  amberDim:  '#d97706',
  red:       '#ef4444',
  redDim:    '#f87171',
  green:     '#22c55e',
  greenDim:  '#4ade80',
  blue:      '#3b82f6',
  blueDim:   '#60a5fa',
  textPri:   '#f3f4f6',
  textSec:   '#9ca3af',
  textDim:   '#6b7280',
  textFaint: '#4b5563',
}

// ---------------------------------------------------------------------------
// Main app
// ---------------------------------------------------------------------------
export default function StrategiseApp({ jobId, side }) {
  const [data,      setData]      = useState(null)
  const [error,     setError]     = useState(null)
  const [activeTab, setActiveTab] = useState('brief')
  const isMobile = useIsMobile()

  useEffect(() => {
    fetch(`/api/jobs/${jobId}/strategise`)
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json() })
      .then(d => {
        if (!d.player) { setError('Report not ready — check the job log.'); return }
        setData(d)
      })
      .catch(err => setError(err.message))
  }, [jobId])

  if (error) return (
    <div style={{ padding: 40, color: C.redDim, fontFamily: 'monospace' }}>
      {error}
      <div style={{ marginTop: 12 }}>
        <a href={`/jobs/${jobId}`} style={{ color: C.amber, textDecoration: 'none', fontSize: 13 }}>
          ← Job log
        </a>
      </div>
    </div>
  )
  if (!data) return (
    <div style={{ padding: 40, color: C.textSec }}>Loading report…</div>
  )

  const tabs = [
    { id: 'brief',        label: 'Strategic Brief' },
    { id: 'battlegrounds',label: 'Battlegrounds'   },
    { id: 'weaknesses',   label: 'Their Weaknesses' },
    { id: 'gaps',         label: 'Your Gaps'        },
    { id: 'lines',        label: 'Key Lines'        },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh', background: C.bg, color: C.textPri }}>

      {/* Header */}
      <div style={{ borderBottom: `1px solid ${C.border}`, padding: isMobile ? '10px 12px' : '10px 20px',
                    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                    flexShrink: 0, flexWrap: 'wrap', gap: 8 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: isMobile ? 8 : 16, flex: 1, minWidth: 0, overflow: 'hidden' }}>
          <PlayerPill meta={data.player}   label="You"      color={C.amber}  isMobile={isMobile} />
          <span style={{ color: C.textDim, fontSize: 13, flexShrink: 0, alignSelf: 'center', lineHeight: 1 }}>vs</span>
          <PlayerPill meta={data.opponent} label="Opponent" color={C.redDim} isMobile={isMobile} />
        </div>
        <a href={`/jobs/${jobId}`}
           style={{ color: C.textDim, fontSize: 12, textDecoration: 'none', whiteSpace: 'nowrap', flexShrink: 0 }}>
          ← Job log
        </a>
      </div>

      {/* Tabs */}
      <div style={{ position: 'relative', flexShrink: 0 }}>
        <div className="tab-strip"
             style={{ display: 'flex', gap: 2, padding: '0 20px', borderBottom: `1px solid ${C.border}`,
                      overflowX: 'auto', scrollbarWidth: 'none', msOverflowStyle: 'none' }}>
          {tabs.map(t => (
            <button key={t.id} onClick={() => setActiveTab(t.id)}
              style={{
                padding: '10px 16px', fontSize: 13, border: 'none', cursor: 'pointer',
                background: 'transparent',
                color: activeTab === t.id ? C.amber : C.textDim,
                borderBottom: activeTab === t.id ? `2px solid ${C.amber}` : '2px solid transparent',
                whiteSpace: 'nowrap',
              }}>
              {t.label}
            </button>
          ))}
        </div>
        {/* Fade hint — signals more tabs are scrollable */}
        <div style={{
          position: 'absolute', right: 0, top: 0, bottom: 1,
          width: 48, pointerEvents: 'none',
          background: `linear-gradient(to right, transparent, ${C.bg})`,
        }} />
      </div>

      {/* Content */}
      <div style={{ flex: 1, overflowY: 'auto', padding: isMobile ? '16px 12px' : 24 }}>
        {activeTab === 'brief'         && <BriefTab         data={data} />}
        {activeTab === 'battlegrounds' && <BattlegroundsTab data={data} />}
        {activeTab === 'weaknesses'    && <WeaknessTab      data={data} side={side} />}
        {activeTab === 'gaps'          && <GapsTab          data={data} side={side} />}
        {activeTab === 'lines'         && <LinesTab         data={data} side={side} />}
      </div>
    </div>
  )
}

function PlayerPill({ meta, label, color, isMobile }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
      <div style={{ width: 28, height: 28, borderRadius: '50%', background: '#1f2937',
                    border: `1px solid ${color}40`, display: 'flex', alignItems: 'center',
                    justifyContent: 'center', fontSize: 12, fontWeight: 700, color, flexShrink: 0 }}>
        {meta.username[0].toUpperCase()}
      </div>
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: 13, fontWeight: 600, color,
                      overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                      maxWidth: isMobile ? 110 : 'none' }}>
          {meta.username}
        </div>
        <div style={{ fontSize: 10, color: C.textFaint }}>{label} · {meta.platform} · {meta.color}</div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Brief tab
// ---------------------------------------------------------------------------
function BriefTab({ data }) {
  const isMobile = useIsMobile()
  return (
    <div style={{ maxWidth: 860, margin: '0 auto' }}>

      {/* Style profiles side by side */}
      <div style={{ display: 'grid', gridTemplateColumns: isMobile ? '1fr' : '1fr 1fr', gap: 16, marginBottom: 24 }}>
        <StyleCard profile={data.player_style}   phaseStats={data.player_phase_stats}   meta={data.player}   accentColor={C.amber}  />
        <StyleCard profile={data.opponent_style} phaseStats={data.opponent_phase_stats} meta={data.opponent} accentColor={C.redDim} />
      </div>

      {/* AI brief or rule-based summary */}
      {data.ai_available && data.strategic_brief ? (
        <div style={{ background: C.surface, border: `1px solid ${C.border}`,
                      borderRadius: 10, padding: 24, marginBottom: 16 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 16 }}>
            <span style={{ color: C.textDim, fontSize: 10, textTransform: 'uppercase',
                           letterSpacing: '0.06em' }}>Strategic Brief</span>
            <span style={{ fontSize: 10, color: C.blueDim, background: '#1e3a5f',
                           border: `1px solid ${C.blueDim}40`, borderRadius: 99,
                           padding: '1px 7px' }}>My Second</span>
          </div>
          <div style={{ color: C.textPri, fontSize: 14, lineHeight: 1.7 }}
               className="md-brief">
            <ReactMarkdown>{data.strategic_brief}</ReactMarkdown>
          </div>
        </div>
      ) : (
        <AutoSummary data={data} />
      )}
    </div>
  )
}

function StyleCard({ profile, phaseStats, meta, accentColor }) {
  const isPlayer = accentColor === C.amber
  const label    = isPlayer ? 'Your Style' : 'Opponent Style'

  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`,
                  borderRadius: 10, padding: 20 }}>
      <div style={{ color: accentColor, fontSize: 10, textTransform: 'uppercase',
                    letterSpacing: '0.06em', marginBottom: 4 }}>{label}</div>
      <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 14 }}>{meta.username}</div>

      <StatRow label="Win rate"    value={`${(profile.avg_win_rate * 100).toFixed(0)}%`}
               bar={profile.avg_win_rate}
               color={profile.avg_win_rate >= 0.5 ? C.green : C.redDim} />
      <StatRow label="Decisive"    value={`${(profile.decisive_rate * 100).toFixed(0)}%`}
               bar={profile.decisive_rate} color={C.amber} />
      <StatRow label="Draw rate"   value={`${(profile.draw_rate * 100).toFixed(0)}%`}
               bar={profile.draw_rate} color={C.blueDim} />
      <StatRow label="Solidity"    value={`${(profile.solidness_score * 100).toFixed(0)}%`}
               bar={profile.solidness_score} color={C.blue} />
      <StatRow label="Diversity"   value={`${(profile.opening_diversity * 100).toFixed(0)}%`}
               bar={profile.opening_diversity} color={C.textSec} />

      {phaseStats && phaseStats.total_games > 0 && (
        <div style={{ marginTop: 12, paddingTop: 10, borderTop: `1px solid ${C.border2}` }}>
          <div style={{ fontSize: 10, color: C.textFaint, textTransform: 'uppercase',
                        letterSpacing: '0.05em', marginBottom: 6 }}>Game Phases</div>
          {phaseStats.avg_length_by_speed && Object.entries(phaseStats.avg_length_by_speed).map(([speed, len]) => (
            <PhaseRow key={speed} label={`${speed[0].toUpperCase() + speed.slice(1)} length`} value={`${len} moves`} />
          ))}
          <PhaseRow label="Endgame rate"  value={`${(phaseStats.endgame_reach_rate * 100).toFixed(0)}%`} />
          <PhaseRow label="EG conversion" value={`${(phaseStats.endgame_conversion_rate * 100).toFixed(0)}%`} />
        </div>
      )}

      {profile.top_openings && profile.top_openings.length > 0 && (
        <div style={{ marginTop: 12, paddingTop: 10, borderTop: `1px solid ${C.border2}` }}>
          <div style={{ fontSize: 10, color: C.textFaint, textTransform: 'uppercase',
                        letterSpacing: '0.05em', marginBottom: 6 }}>Top Lines</div>
          {profile.top_openings.slice(0, 4).map((o, i) => (
            <div key={i} style={{ display: 'flex', justifyContent: 'space-between',
                                  fontSize: 11, marginBottom: 3, gap: 8 }}>
              <span style={{ color: C.textSec, fontFamily: 'monospace', overflow: 'hidden',
                             textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>
                {o.move_sequence || o.fen}
              </span>
              <span style={{ color: accentColor, fontFamily: 'monospace', flexShrink: 0 }}>
                {o.games}g
              </span>
            </div>
          ))}
        </div>
      )}

      <div style={{ marginTop: 12, fontSize: 11, color: C.textFaint }}>
        {profile.total_positions.toLocaleString()} positions · {profile.total_moves_indexed.toLocaleString()} moves indexed
      </div>
    </div>
  )
}

function PhaseRow({ label, value }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 3 }}>
      <span style={{ fontSize: 11, color: C.textDim }}>{label}</span>
      <span style={{ fontSize: 11, color: C.textSec, fontFamily: 'monospace' }}>{value}</span>
    </div>
  )
}

function StatRow({ label, value, bar, color }) {
  return (
    <div style={{ marginBottom: 8 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 3 }}>
        <span style={{ fontSize: 11, color: C.textSec }}>{label}</span>
        <span style={{ fontSize: 11, color, fontFamily: 'monospace', fontWeight: 600 }}>{value}</span>
      </div>
      <div style={{ background: '#1e2d45', height: 5, borderRadius: 3 }}>
        <div style={{ background: color, height: 5, borderRadius: 3,
                      width: `${Math.min(bar * 100, 100)}%`, transition: 'width 0.4s' }} />
      </div>
    </div>
  )
}

function AutoSummary({ data }) {
  const wns = data.opponent_weaknesses.length
  const gps = data.prep_gaps.length
  const bgs = data.battlegrounds.filter(b => b.advantage === 'player').length

  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10, padding: 24 }}>
      <div style={{ color: C.textDim, fontSize: 10, textTransform: 'uppercase',
                    letterSpacing: '0.06em', marginBottom: 14 }}>Summary</div>
      <ul style={{ color: C.textPri, fontSize: 14, lineHeight: 1.9, paddingLeft: 20 }}>
        <li>{data.battlegrounds.length} opening battleground{data.battlegrounds.length !== 1 ? 's' : ''} found where both players have data.</li>
        {bgs > 0 && <li style={{ color: C.greenDim }}>{bgs} battleground{bgs !== 1 ? 's' : ''} favour{bgs === 1 ? 's' : ''} you based on win rates.</li>}
        {wns > 0 && <li style={{ color: C.amber }}>{wns} opponent weakness{wns !== 1 ? 'es' : ''} are reachable from your repertoire — exploit these.</li>}
        {gps > 0 && <li style={{ color: C.redDim }}>{gps} prep gap{gps !== 1 ? 's' : ''} where you play poorly and the opponent has data — address these.</li>}
      </ul>
      <div style={{ marginTop: 16, padding: '10px 14px', background: '#111827',
                    borderRadius: 8, border: `1px solid ${C.border2}`,
                    color: C.textFaint, fontSize: 12 }}>
        Add an Anthropic API key to your next Strategise job for a full AI-generated brief.
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Battlegrounds tab
// ---------------------------------------------------------------------------
function BattlegroundsTab({ data }) {
  const [selected, setSelected] = useState(null)
  const bgs = data.battlegrounds

  if (!bgs.length) return <EmptyState msg="No battleground positions found." />

  return (
    <div style={{ maxWidth: 860, margin: '0 auto' }}>
      <SectionHeader>Opening Battlegrounds</SectionHeader>
      <p style={{ color: C.textSec, fontSize: 13, marginBottom: 16 }}>
        Positions where both players have cache data — direct win-rate comparison.
      </p>
      <div style={{ overflowX: 'auto', WebkitOverflowScrolling: 'touch' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13, minWidth: 520 }}>
        <thead>
          <tr style={{ color: C.textFaint, fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
            <Th align="left">#</Th>
            <Th align="right">Your games</Th>
            <Th align="right">Your W%</Th>
            <Th align="right">Opp games</Th>
            <Th align="right">Opp W%</Th>
            <Th align="center">Advantage</Th>
            <Th align="left">Your move</Th>
            <Th align="left">Their reply</Th>
          </tr>
        </thead>
        <tbody>
          {bgs.map((bg, i) => {
            const isSelected = selected === i
            const advColor = bg.advantage === 'player' ? C.greenDim
                           : bg.advantage === 'opponent' ? C.redDim : C.textSec
            return (
              <React.Fragment key={i}>
                <tr onClick={() => setSelected(isSelected ? null : i)}
                    style={{ borderTop: `1px solid ${C.border2}`, cursor: 'pointer',
                             background: isSelected ? '#0f1e2e' : 'transparent' }}>
                  <Td>{i + 1}</Td>
                  <Td align="right" dim>{bg.player_games}</Td>
                  <Td align="right"><Pct v={bg.player_win_rate} /></Td>
                  <Td align="right" dim>{bg.opponent_games}</Td>
                  <Td align="right"><Pct v={bg.opponent_win_rate} invert /></Td>
                  <Td align="center">
                    <span style={{ color: advColor, fontWeight: 600, fontSize: 11 }}>
                      {bg.advantage === 'player' ? '✓ You' : bg.advantage === 'opponent' ? '✗ Them' : '≈ Equal'}
                    </span>
                  </Td>
                  <Td mono>{bg.player_top_move_san}</Td>
                  <Td mono dim>{bg.opponent_top_response_san}</Td>
                </tr>
                {isSelected && (() => {
                  const mv = resolveMove(bg.fen, bg.fen_after, bg.player_top_move_orig, bg.player_top_move_dest, bg.player_top_move_san)
                  return (
                    <tr style={{ background: '#0a0f1a' }}>
                      <td colSpan={8} style={{ padding: '16px 8px' }}>
                        <MiniBoard fen={mv.fen} side={data.player.color} orig={mv.orig} dest={mv.dest} />
                      </td>
                    </tr>
                  )
                })()}
              </React.Fragment>
            )
          })}
        </tbody>
      </table>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Weaknesses tab
// ---------------------------------------------------------------------------
function WeaknessTab({ data, side }) {
  const [selected, setSelected] = useState(null)
  const items = data.opponent_weaknesses

  if (!items.length) return <EmptyState msg="No reachable opponent weaknesses found." />

  return (
    <div style={{ maxWidth: 860, margin: '0 auto' }}>
      <SectionHeader>Opponent Weaknesses</SectionHeader>
      <p style={{ color: C.textSec, fontSize: 13, marginBottom: 16 }}>
        Positions where the opponent habitually plays suboptimally — and you can reach them.
      </p>
      <HabitTable items={items} selected={selected} onSelect={setSelected}
                  side={side} playerLabel="Opp's move" />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Gaps tab
// ---------------------------------------------------------------------------
function GapsTab({ data, side }) {
  const [selected, setSelected] = useState(null)
  const items = data.prep_gaps

  if (!items.length) return <EmptyState msg="No prep gaps found." />

  return (
    <div style={{ maxWidth: 860, margin: '0 auto' }}>
      <SectionHeader>Your Prep Gaps</SectionHeader>
      <p style={{ color: C.textSec, fontSize: 13, marginBottom: 16 }}>
        Positions where you play poorly and the opponent has data — fix these before you meet.
      </p>
      <HabitTable items={items} selected={selected} onSelect={setSelected}
                  side={side} playerLabel="Your move" accentColor={C.redDim}
                  extraCol={item => `Opp: ${item.opponent_games_here}g`} />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Key Lines tab — helpers
// ---------------------------------------------------------------------------

function parseSANMoves(pgn) {
  if (!pgn) return []
  return pgn.split(/\s+/)
    .map(t => t.replace(/^\d+\./, ''))
    .filter(t => /^[a-zA-Z]/.test(t))
}

function fenAtStep(moves, step) {
  // step = number of moves played from the start (0 = starting position)
  try {
    const ch = new Chess()
    const end = Math.min(step, moves.length)
    for (let i = 0; i < end; i++) ch.move(moves[i])
    return ch.fen()
  } catch {
    return 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
  }
}

function lastMoveAtStep(moves, step) {
  if (step <= 0 || !moves.length) return { orig: null, dest: null }
  const idx = Math.min(step, moves.length) - 1
  try {
    const ch = new Chess()
    for (let i = 0; i < idx; i++) ch.move(moves[i])
    const m = ch.move(moves[idx])
    if (m) return { orig: m.from, dest: m.to }
  } catch {}
  return { orig: null, dest: null }
}

function typeColor(t) {
  return t === 'battleground' ? C.blue : t === 'weakness' ? C.amber : C.redDim
}

function TypeBadge({ type }) {
  const color = typeColor(type)
  const icon  = type === 'battleground' ? '⚔' : type === 'weakness' ? '⚡' : '⚠'
  const label = type === 'battleground' ? 'Battleground' : type === 'weakness' ? 'Weakness' : 'Gap'
  return (
    <span style={{
      fontSize: 10, color, background: `${color}18`,
      border: `1px solid ${color}40`, borderRadius: 99, padding: '2px 8px',
      textTransform: 'uppercase', letterSpacing: '0.06em',
    }}>
      {icon} {label}
    </span>
  )
}

// ---------------------------------------------------------------------------
// Key Lines tab
// ---------------------------------------------------------------------------
function LinesTab({ data, side }) {
  const isMobile = useIsMobile()
  const lines = data.key_positions || []
  const [selectedIdx, setSelectedIdx] = useState(0)
  const [moveStep,    setMoveStep]    = useState(null)  // null = show final position

  if (!lines.length) return <EmptyState msg="No key lines identified." />

  function selectLine(i) {
    setSelectedIdx(i)
    setMoveStep(null)
  }

  const line  = lines[selectedIdx] || lines[0]
  const moves = parseSANMoves(line?.move_sequence || '')
  // null step = show full line (all moves played)
  const step  = moveStep === null ? moves.length : moveStep

  const boardFen  = moves.length > 0 ? fenAtStep(moves, step) : (line?.fen_after || line?.fen || chess_start)
  const { orig, dest } = moves.length > 0
    ? lastMoveAtStep(moves, step)
    : { orig: line?.move_orig || null, dest: line?.move_dest || null }

  return (
    <div style={{ maxWidth: 920, margin: '0 auto' }}>
      <SectionHeader>Key Lines</SectionHeader>
      <p style={{ color: C.textSec, fontSize: 13, marginBottom: 16 }}>
        The most important opening lines to prepare before facing this opponent.
        Click any move to step through the line on the board.
      </p>

      <div style={{ display: 'flex', gap: 16, flexDirection: isMobile ? 'column' : 'row', alignItems: 'flex-start' }}>

        {/* ── Line list ─────────────────────────────────────────────────── */}
        <div style={{ width: isMobile ? '100%' : 280, flexShrink: 0 }}>
          {lines.map((l, i) => (
            <div key={i} onClick={() => selectLine(i)}
              style={{
                padding: '10px 12px', marginBottom: 6, borderRadius: 8, cursor: 'pointer',
                background: selectedIdx === i ? '#0f1e2e' : C.surface,
                border: `1px solid ${selectedIdx === i ? C.amber : C.border}`,
                transition: 'all 0.15s',
              }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 5 }}>
                <TypeBadge type={l.type} />
              </div>
              <div style={{
                fontFamily: 'monospace', fontSize: 12,
                color: selectedIdx === i ? C.textPri : C.textSec,
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              }}>
                {l.move_sequence || '(starting position)'}
              </div>
              <div style={{ fontSize: 11, color: C.textDim, marginTop: 4 }}>
                {l.type === 'battleground'
                  ? `You ${(l.player_win_rate * 100).toFixed(0)}% · Opp ${(l.opponent_win_rate * 100).toFixed(0)}%`
                  : `${l.move_san} → best: ${l.best_move_san} (${l.eval_gap_cp > 0 ? '+' : ''}${l.eval_gap_cp}cp)`}
              </div>
            </div>
          ))}
        </div>

        {/* ── Board panel ───────────────────────────────────────────────── */}
        {line && (
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10, padding: 20 }}>

              {/* Type badge + label */}
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 14 }}>
                <TypeBadge type={line.type} />
                <span style={{ fontSize: 13, color: C.textSec }}>{line.label}</span>
              </div>

              {/* Move chips — clickable PGN stepper */}
              {moves.length > 0 && (
                <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center',
                              gap: '2px 1px', marginBottom: 14, padding: '8px 10px',
                              background: '#0a0f1a', borderRadius: 6 }}>
                  {/* "Start" button */}
                  <button onClick={() => setMoveStep(0)}
                    style={{
                      padding: '2px 6px', borderRadius: 3, border: 'none', cursor: 'pointer',
                      fontSize: 10, background: step === 0 ? C.amber : '#1e2d45',
                      color: step === 0 ? '#030712' : C.textDim, marginRight: 4,
                    }}>
                    ⏮
                  </button>
                  {moves.map((san, i) => {
                    const afterThis = i + 1
                    const isActive  = step === afterThis
                    const isPast    = step > afterThis
                    return (
                      <React.Fragment key={i}>
                        {i % 2 === 0 && (
                          <span style={{ color: C.textFaint, fontSize: 11, fontFamily: 'monospace',
                                         marginLeft: i === 0 ? 0 : 4, marginRight: 1 }}>
                            {i / 2 + 1}.
                          </span>
                        )}
                        <button onClick={() => setMoveStep(afterThis)}
                          style={{
                            padding: '2px 5px', borderRadius: 3, border: 'none', cursor: 'pointer',
                            fontSize: 12, fontFamily: 'monospace',
                            background: isActive ? C.amber : isPast ? '#1e2d45' : 'transparent',
                            color:      isActive ? '#030712' : isPast ? C.textPri : C.textDim,
                            fontWeight: isActive ? 700 : 400,
                            transition: 'all 0.1s',
                          }}>
                          {san}
                        </button>
                      </React.Fragment>
                    )
                  })}
                  {/* "End" button */}
                  <button onClick={() => setMoveStep(null)}
                    style={{
                      padding: '2px 6px', borderRadius: 3, border: 'none', cursor: 'pointer',
                      fontSize: 10, background: step === moves.length ? C.amber : '#1e2d45',
                      color: step === moves.length ? '#030712' : C.textDim, marginLeft: 4,
                    }}>
                    ⏭
                  </button>
                </div>
              )}

              {/* Board */}
              <MiniBoard fen={boardFen} side={side} orig={orig} dest={dest} size={380} />

              {/* Context detail */}
              <div style={{ marginTop: 14, padding: '10px 14px', background: '#0a0f1a',
                            borderRadius: 6, fontSize: 12 }}>
                {line.type === 'battleground' ? (
                  <div style={{ display: 'flex', gap: 20, flexWrap: 'wrap' }}>
                    <span>Your move: <strong style={{ color: C.amber, fontFamily: 'monospace' }}>{line.move_san}</strong></span>
                    <span>Their reply: <strong style={{ color: C.redDim, fontFamily: 'monospace' }}>{line.opp_response_san}</strong></span>
                    <span style={{ color: line.advantage === 'player' ? C.greenDim : line.advantage === 'opponent' ? C.redDim : C.textSec, fontWeight: 600 }}>
                      {line.advantage === 'player' ? '✓ Favours you' : line.advantage === 'opponent' ? '✗ Favours them' : '≈ Equal'}
                    </span>
                  </div>
                ) : (
                  <div style={{ display: 'flex', gap: 20, flexWrap: 'wrap' }}>
                    <span>Typical: <strong style={{ color: C.redDim, fontFamily: 'monospace' }}>{line.move_san}</strong></span>
                    {line.best_move_san && (
                      <span>Better: <strong style={{ color: C.greenDim, fontFamily: 'monospace' }}>{line.best_move_san}</strong></span>
                    )}
                    {line.eval_gap_cp != null && (
                      <span>Gap: <strong style={{ color: C.amber }}>{line.eval_gap_cp > 0 ? '+' : ''}{line.eval_gap_cp}cp</strong></span>
                    )}
                    {line.total_games > 0 && (
                      <span style={{ color: C.textDim }}>{line.total_games} games</span>
                    )}
                  </div>
                )}
              </div>

            </div>
          </div>
        )}
      </div>
    </div>
  )
}

const chess_start = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'

// ---------------------------------------------------------------------------
// Shared components
// ---------------------------------------------------------------------------

function HabitTable({ items, selected, onSelect, side, playerLabel, accentColor = C.amber, extraCol }) {
  return (
    <div style={{ overflowX: 'auto', WebkitOverflowScrolling: 'touch' }}>
    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13, minWidth: 420 }}>
      <thead>
        <tr style={{ color: C.textFaint, fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
          <Th align="left">#</Th>
          <Th align="left">{playerLabel}</Th>
          <Th align="left">Best move</Th>
          <Th align="right">Gap</Th>
          <Th align="right">Freq</Th>
          <Th align="right">Score</Th>
          {extraCol && <Th align="right">Context</Th>}
        </tr>
      </thead>
      <tbody>
        {items.map((item, i) => {
          const isSelected = selected === i
          return (
            <React.Fragment key={i}>
              <tr onClick={() => onSelect(isSelected ? null : i)}
                  style={{ borderTop: `1px solid ${C.border2}`, cursor: 'pointer',
                           background: isSelected ? '#0f1e2e' : 'transparent' }}>
                <Td dim>{item.rank}</Td>
                <Td mono><span style={{ color: accentColor, fontWeight: 700 }}>{item.player_move_san}</span></Td>
                <Td mono><span style={{ color: C.greenDim }}>{item.best_move_san}</span></Td>
                <Td align="right" mono>
                  <EvalBadge cp={item.eval_gap_cp} />
                </Td>
                <Td align="right" dim>{item.total_games}×</Td>
                <Td align="right"><span style={{ color: C.amber }}>{item.score.toFixed(1)}</span></Td>
                {extraCol && <Td align="right" dim>{extraCol(item)}</Td>}
              </tr>
              {isSelected && (() => {
                const mv = resolveMove(item.fen, item.fen_after, item.player_move_orig, item.player_move_dest, item.player_move_san)
                return (
                  <tr style={{ background: '#0a0f1a' }}>
                    <td colSpan={extraCol ? 7 : 6} style={{ padding: '16px 8px' }}>
                      <MiniBoard fen={mv.fen} side={side} orig={mv.orig} dest={mv.dest} />
                      <div style={{ marginTop: 10, fontSize: 12, color: C.textSec, maxWidth: 500 }}>
                        <span style={{ color: accentColor, fontWeight: 600 }}>{item.player_move_san}</span>
                        {' '}played {item.total_games} times — eval gap{' '}
                        <span style={{ color: C.redDim }}>{item.eval_gap_cp > 0 ? '+' : ''}{item.eval_gap_cp.toFixed(0)}cp</span>.
                        {' '}Best: <span style={{ color: C.greenDim, fontWeight: 600 }}>{item.best_move_san}</span>.
                      </div>
                    </td>
                  </tr>
                )
              })()}
            </React.Fragment>
          )
        })}
      </tbody>
    </table>
    </div>
  )
}

function MiniBoard({ fen, side, orig, dest, size = 320 }) {
  const boardSize = typeof window !== 'undefined'
    ? Math.min(size, window.innerWidth - 48)
    : size
  const config = {
    fen,
    orientation: side || 'white',
    lastMove: orig && dest ? [orig, dest] : undefined,
    movable:  { free: false, color: 'none' },
    draggable: { enabled: false },
    selectable: { enabled: false },
  }
  return (
    <div style={{ width: boardSize, height: boardSize }}>
      <Chessground width={boardSize} height={boardSize} config={config} />
    </div>
  )
}

function EvalBadge({ cp }) {
  const color = cp >= 75 ? C.redDim : cp >= 30 ? '#fbbf24' : C.textSec
  return <span style={{ color, fontFamily: 'monospace' }}>{cp > 0 ? '+' : ''}{cp.toFixed(0)}cp</span>
}

function Pct({ v, invert }) {
  const display = `${(v * 100).toFixed(0)}%`
  const color   = invert
    ? (v >= 0.55 ? C.redDim  : v <= 0.45 ? C.greenDim : C.textSec)
    : (v >= 0.55 ? C.greenDim : v <= 0.45 ? C.redDim   : C.textSec)
  return <span style={{ color, fontFamily: 'monospace', fontWeight: 600 }}>{display}</span>
}

function Th({ children, align = 'left' }) {
  return <th style={{ padding: '4px 8px', textAlign: align, fontWeight: 500 }}>{children}</th>
}
function Td({ children, align = 'left', dim, mono }) {
  return (
    <td style={{
      padding: '7px 8px', textAlign: align,
      color: dim ? C.textDim : C.textPri,
      fontFamily: mono ? 'monospace' : 'inherit',
    }}>
      {children}
    </td>
  )
}

function SectionHeader({ children }) {
  return (
    <div style={{ fontSize: 16, fontWeight: 700, marginBottom: 6 }}>{children}</div>
  )
}

function EmptyState({ msg }) {
  return (
    <div style={{ padding: 40, color: C.textFaint, textAlign: 'center', fontSize: 14 }}>{msg}</div>
  )
}
