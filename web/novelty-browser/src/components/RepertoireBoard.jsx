import React from 'react'
import Chessground from '@react-chess/chessground'

const BOARD_SIZE = 480

export default function RepertoireBoard({ node, nodeMap, orientation, onNavigate, onBack, canGoBack, treeStats }) {
  if (!node) return null

  const config = {
    fen: node.fen,
    orientation,
    lastMove: node.move_orig && node.move_dest ? [node.move_orig, node.move_dest] : undefined,
    movable:  { free: false, color: 'none' },
    draggable: { enabled: false },
    selectable: { enabled: false },
    animation: { enabled: true, duration: 200 },
  }

  const children = node.children || []

  return (
    <div style={{ display: 'flex', gap: 24, alignItems: 'flex-start' }}>

      {/* Left: board + nav + stats */}
      <div style={{ flexShrink: 0, width: BOARD_SIZE }}>
        <div style={{ width: BOARD_SIZE, height: BOARD_SIZE }}>
          <Chessground width={BOARD_SIZE} height={BOARD_SIZE} config={config} />
        </div>
        <NavBar onBack={onBack} canGoBack={canGoBack} />
        <StatsPanel node={node} treeStats={treeStats} orientation={orientation} />
      </div>

      {/* Right: move choices + annotation */}
      <div style={{ flex: 1, minWidth: 0 }}>
        <AnnotationPanel node={node} />
        <MovesPanel children={children} nodeMap={nodeMap} onNavigate={onNavigate} />
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// NavBar
// ---------------------------------------------------------------------------
function NavBar({ onBack, canGoBack }) {
  return (
    <div style={{ marginTop: 10, display: 'flex', gap: 6, justifyContent: 'center' }}>
      <button
        onClick={onBack}
        disabled={!canGoBack}
        title="Back (← or ↑)"
        style={{
          background: canGoBack ? '#1f2937' : '#111',
          color: canGoBack ? '#9ca3af' : '#374151',
          border: 'none', borderRadius: 4,
          padding: '5px 16px', fontSize: 13, cursor: canGoBack ? 'pointer' : 'default',
        }}
      >
        ◀ Back
      </button>
      <span style={{ color: '#4b5563', fontSize: 12, alignSelf: 'center' }}>
        {canGoBack ? '' : 'Start position'}
      </span>
    </div>
  )
}

// ---------------------------------------------------------------------------
// StatsPanel — below the board
// ---------------------------------------------------------------------------
function StatsPanel({ node, treeStats, orientation }) {
  const freq = node?.freq   // null for root / opponent nodes

  const hasWdl = freq && freq.wins !== null && freq.draws !== null && freq.losses !== null
  const wdlTotal = hasWdl ? (freq.wins + freq.draws + freq.losses) : 0
  const winPct  = hasWdl && wdlTotal > 0 ? (freq.wins   / wdlTotal) * 100 : 0
  const drawPct = hasWdl && wdlTotal > 0 ? (freq.draws  / wdlTotal) * 100 : 0
  const lossPct = hasWdl && wdlTotal > 0 ? (freq.losses / wdlTotal) * 100 : 0
  // Performance score: 1 per win, 0.5 per draw, 0 per loss, normalised 0-100
  const score   = hasWdl && wdlTotal > 0
    ? ((freq.wins + freq.draws * 0.5) / wdlTotal) * 100
    : null
  const solidity = hasWdl && wdlTotal > 0 ? winPct + drawPct : null

  // Quality by depth entries — player moves only, sorted by depth
  const qualityEntries = treeStats?.quality_by_depth
    ? Object.entries(treeStats.quality_by_depth)
        .map(([d, q]) => [Number(d), q])
        .sort((a, b) => a[0] - b[0])
    : []

  return (
    <div style={{
      marginTop: 12,
      background: '#0a0f1a',
      border: '1px solid #1e2d45',
      borderRadius: 10,
      padding: '16px',
      fontSize: 12,
    }}>

      {/* ── Position stats (only for player move nodes with freq data) ── */}
      {freq && (
        <div style={{ marginBottom: treeStats ? 16 : 0 }}>
          <SectionLabel>This Line</SectionLabel>

          {/* WDL bar */}
          {hasWdl ? (
            <>
              <div style={{
                display: 'flex', height: 12, borderRadius: 6,
                overflow: 'hidden', marginBottom: 6,
              }}>
                <div style={{ width: `${winPct}%`,  background: '#22c55e', transition: 'width 0.35s' }} />
                <div style={{ width: `${drawPct}%`, background: '#64748b', transition: 'width 0.35s' }} />
                <div style={{ width: `${lossPct}%`, background: '#ef4444', transition: 'width 0.35s' }} />
              </div>
              <div style={{ display: 'flex', gap: 14, color: '#9ca3af', marginBottom: 12, fontSize: 11 }}>
                <span style={{ color: '#4ade80' }}>▬ {winPct.toFixed(0)}% W</span>
                <span style={{ color: '#94a3b8' }}>▬ {drawPct.toFixed(0)}% D</span>
                <span style={{ color: '#f87171' }}>▬ {lossPct.toFixed(0)}% L</span>
              </div>
            </>
          ) : (
            /* Frequency bar even without WDL */
            freq.total > 0 && (
              <div style={{ marginBottom: 10 }}>
                <div style={{ display: 'flex', height: 8, borderRadius: 4, overflow: 'hidden', background: '#1e2d45', marginBottom: 4 }}>
                  <div style={{ width: `${freq.pct}%`, background: '#f59e0b', transition: 'width 0.35s' }} />
                </div>
              </div>
            )
          )}

          {/* Metric chips */}
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            {hasWdl && score !== null && (
              <MetricChip
                label="Advantage"
                value={`${winPct >= 50 ? '+' : ''}${winPct.toFixed(0)}%`}
                color={winPct >= 55 ? '#4ade80' : winPct >= 45 ? '#fbbf24' : '#f87171'}
              />
            )}
            {solidity !== null && (
              <MetricChip
                label="Solidity"
                value={`${solidity.toFixed(0)}%`}
                color={solidity >= 70 ? '#60a5fa' : solidity >= 50 ? '#9ca3af' : '#f87171'}
              />
            )}
            {score !== null && (
              <MetricChip
                label="Score"
                value={(score / 100).toFixed(2)}
                color={score >= 55 ? '#fbbf24' : score >= 45 ? '#9ca3af' : '#f87171'}
              />
            )}
            {freq.games !== undefined && (
              <MetricChip
                label="Frequency"
                value={`${freq.games}/${freq.total}`}
                color="#9ca3af"
                subtitle={`${freq.pct}% of games`}
              />
            )}
          </div>
        </div>
      )}

      {/* ── Tree overview ── */}
      {treeStats && (
        <div style={{ borderTop: freq ? '1px solid #1e2d45' : 'none', paddingTop: freq ? 14 : 0 }}>
          <SectionLabel>Repertoire Overview</SectionLabel>

          {/* Summary chips */}
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 14 }}>
            <MetricChip label="Lines"      value={treeStats.leaf_count}      color="#f3f4f6" />
            <MetricChip label="Positions"  value={treeStats.total_positions}  color="#f3f4f6" />
            <MetricChip label="Your moves" value={treeStats.player_moves}     color="#fbbf24" />
            <MetricChip label="Max depth"  value={`${treeStats.max_depth}p`}  color="#60a5fa" />
          </div>

          {/* Prep quality by depth */}
          {qualityEntries.length > 0 ? (
            <>
              <div style={{ color: '#6b7280', fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 6 }}>
                Prep quality by depth
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                {qualityEntries.map(([depth, q]) => {
                  const score = q.score
                  const barColor = score >= 55 ? '#22c55e' : score >= 45 ? '#f59e0b' : '#ef4444'
                  const labelColor = score >= 55 ? '#4ade80' : score >= 45 ? '#fbbf24' : '#f87171'
                  return (
                    <div key={depth} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <span style={{ width: 32, textAlign: 'right', fontSize: 10, color: '#4b5563', flexShrink: 0 }}>
                        ply {depth}
                      </span>
                      <div style={{ flex: 1, background: '#1e2d45', borderRadius: 3, height: 10, overflow: 'hidden' }}>
                        <div style={{
                          width: `${score}%`,
                          height: '100%',
                          background: barColor,
                          borderRadius: 3,
                          transition: 'width 0.35s',
                          opacity: 0.85,
                        }} />
                      </div>
                      <span style={{ width: 32, fontSize: 10, color: labelColor, textAlign: 'right', fontFamily: 'monospace' }}>
                        {score.toFixed(0)}%
                      </span>
                    </div>
                  )
                })}
              </div>
              <div style={{ fontSize: 10, color: '#4b5563', marginTop: 6 }}>
                Performance score (wins + ½ draws) across all your moves at each ply
              </div>
            </>
          ) : (
            <div style={{ color: '#4b5563', fontSize: 11 }}>
              Quality data available after running a new repertoire job.
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function SectionLabel({ children }) {
  return (
    <div style={{
      color: '#6b7280', fontSize: 10, textTransform: 'uppercase',
      letterSpacing: '0.06em', marginBottom: 8,
    }}>
      {children}
    </div>
  )
}

function MetricChip({ label, value, color, subtitle }) {
  return (
    <div style={{
      background: '#111827',
      border: '1px solid #1e2d45',
      borderRadius: 7,
      padding: '7px 11px',
      minWidth: 58,
    }}>
      <div style={{ color, fontSize: 16, fontWeight: 700, fontFamily: 'monospace', lineHeight: 1.2 }}>
        {value}
      </div>
      <div style={{ color: '#6b7280', fontSize: 10, marginTop: 2 }}>{label}</div>
      {subtitle && <div style={{ color: '#4b5563', fontSize: 9, marginTop: 1 }}>{subtitle}</div>}
    </div>
  )
}

// ---------------------------------------------------------------------------
// AnnotationPanel — shows move info and frequency comment for current node
// ---------------------------------------------------------------------------
function AnnotationPanel({ node }) {
  if (!node.move_san) {
    return (
      <div style={{ background: '#111827', borderRadius: 8, padding: '12px 16px', marginBottom: 14 }}>
        <div style={{ color: '#9ca3af', fontSize: 12 }}>Starting position</div>
        <div style={{ color: '#6b7280', fontSize: 11, marginTop: 4 }}>
          Use ← → arrow keys or click moves below to navigate.
        </div>
      </div>
    )
  }

  return (
    <div style={{ background: '#111827', borderRadius: 8, padding: '12px 16px', marginBottom: 14 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
        <span style={{
          fontFamily: 'monospace', fontSize: 22, fontWeight: 700,
          color: node.is_player_move ? '#fbbf24' : '#9ca3af',
        }}>
          {node.move_san}
        </span>
        <span style={{ fontSize: 11, color: '#6b7280' }}>
          {node.is_player_move ? 'your move' : "opponent's move"}
        </span>
        <span style={{ fontSize: 11, color: '#6b7280' }}>· ply {node.depth}</span>
      </div>
      {node.freq && (
        <div style={{ color: '#9ca3af', fontSize: 12, marginTop: 6 }}>
          {node.freq.games}/{node.freq.total} games · {node.freq.pct}%
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// MovesPanel — shows available continuations from the current node
// ---------------------------------------------------------------------------
function MovesPanel({ children, nodeMap, onNavigate }) {
  if (!children || children.length === 0) {
    return (
      <div style={{ background: '#111827', borderRadius: 8, padding: '12px 16px', color: '#4b5563', fontSize: 12 }}>
        End of line.
      </div>
    )
  }

  // Determine if these are player moves or opponent responses
  const isPlayerTurn = children[0]?.is_player_move

  return (
    <div style={{ background: '#111827', borderRadius: 8, padding: '12px 16px' }}>
      <div style={{
        color: '#6b7280', fontSize: 10, textTransform: 'uppercase',
        letterSpacing: '0.05em', marginBottom: 10,
      }}>
        {isPlayerTurn ? 'Your moves' : "Opponent's responses"}
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {children.map((child, idx) => (
          <MoveOption
            key={child.id}
            node={child}
            isMainline={idx === 0}
            isPlayerMove={child.is_player_move}
            onClick={() => onNavigate(child.id)}
          />
        ))}
      </div>
    </div>
  )
}

function MoveOption({ node, isMainline, isPlayerMove, onClick }) {
  const [hovered, setHovered] = React.useState(false)

  const bgColor   = hovered ? '#1f2937' : '#0f172a'
  const textColor = isPlayerMove
    ? (isMainline ? '#fbbf24' : '#d97706')
    : (isMainline ? '#e5e7eb' : '#9ca3af')
  const badge = isMainline
    ? { text: 'mainline', color: '#1d4ed8', bg: '#1e3a8a' }
    : { text: 'alternative', color: '#374151', bg: '#1f2937' }

  return (
    <div
      onClick={onClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        display: 'flex', alignItems: 'center', gap: 10,
        padding: '8px 12px', borderRadius: 6,
        background: bgColor,
        border: `1px solid ${isMainline ? '#1d4ed8' : '#1f2937'}`,
        cursor: 'pointer', transition: 'background 0.1s',
      }}
    >
      <span style={{ fontFamily: 'monospace', fontSize: 16, fontWeight: isMainline ? 700 : 400, color: textColor, minWidth: 48 }}>
        {node.move_san}
      </span>
      {/* Show frequency inline for player moves */}
      {node.freq && (
        <span style={{ color: '#6b7280', fontSize: 11 }}>
          {node.freq.pct}%
        </span>
      )}
      {node.children.length === 0 && (
        <span style={{ marginLeft: 'auto', color: '#374151', fontSize: 10 }}>end</span>
      )}
    </div>
  )
}
