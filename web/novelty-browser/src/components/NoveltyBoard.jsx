import React, { useState } from 'react'
import Chessground from '@react-chess/chessground'

const BOARD_SIZE = 480

export default function NoveltyBoard({ novelty, orientation }) {
  const [showAfter, setShowAfter] = useState(false)

  if (!novelty) return null

  const fen        = showAfter ? novelty.fen_after : novelty.fen_before
  const lastMove   = showAfter ? undefined : [novelty.novelty_orig, novelty.novelty_dest]

  // Chessground config
  const config = {
    fen,
    orientation,
    lastMove,
    movable:    { free: false, color: 'none' },
    draggable:  { enabled: false },
    selectable: { enabled: false },
    animation:  { enabled: true, duration: 200 },
  }

  const ply     = novelty.book_moves_san.length
  const moveNum = Math.floor(ply / 2) + 1
  const dots    = ply % 2 === 1 ? '…' : '.'

  return (
    <div>
      {/* Move path breadcrumb */}
      <div style={{ marginBottom: 16, fontSize: 13, color: '#9ca3af', lineHeight: 1.6 }}>
        {novelty.book_moves_san.length === 0
          ? <span style={{ color: '#6b7280' }}>Starting position</span>
          : <MovePath moves={novelty.book_moves_san} />
        }
        <span style={{ color: '#f3f4f6', fontWeight: 700, marginLeft: 6 }}>
          → {moveNum}{dots}
          <span style={{ color: '#fbbf24' }}>{novelty.novelty_san}</span>
          {novelty.post_novelty_games === 0 &&
            <span style={{ color: '#60a5fa', fontSize: 11, marginLeft: 4 }}>(TN)</span>}
        </span>
      </div>

      {/* Board + right panel side-by-side */}
      <div style={{ display: 'flex', gap: 24, alignItems: 'flex-start' }}>
        {/* Board */}
        <div style={{ flexShrink: 0 }}>
          <div style={{ width: BOARD_SIZE, height: BOARD_SIZE }}>
            <Chessground width={BOARD_SIZE} height={BOARD_SIZE} config={config} />
          </div>

          {/* Before / After toggle */}
          <div style={{ marginTop: 10, display: 'flex', gap: 8 }}>
            <ToggleBtn active={!showAfter} onClick={() => setShowAfter(false)}>
              Before
            </ToggleBtn>
            <ToggleBtn active={showAfter} onClick={() => setShowAfter(true)}>
              After {novelty.novelty_san}
            </ToggleBtn>
          </div>
        </div>

        {/* Right panel: eval + continuations */}
        <div style={{ flex: 1, minWidth: 0 }}>
          <EvalPanel novelty={novelty} />
          {novelty.continuations_san.length > 0 && (
            <ContinuationPanel moves={novelty.continuations_san} startPly={ply + 1} />
          )}
        </div>
      </div>
    </div>
  )
}

/* ---- Sub-components ---- */

function MovePath({ moves }) {
  const parts = []
  moves.forEach((san, i) => {
    if (i % 2 === 0) parts.push(
      <span key={`n${i}`} style={{ color: '#6b7280', marginRight: 2 }}>
        {Math.floor(i / 2) + 1}.
      </span>
    )
    parts.push(
      <span key={`m${i}`} style={{ color: '#d1d5db', marginRight: 4 }}>{san}</span>
    )
  })
  return <>{parts}</>
}

function EvalPanel({ novelty }) {
  const { eval_cp, stability, score, pre_novelty_games, post_novelty_games, depth_evals } = novelty
  const rows = [
    ['Eval',      `${eval_cp >= 0 ? '+' : ''}${eval_cp.toFixed(1)} cp`],
    ['Stability', `±${stability.toFixed(1)} cp`],
    ['Score',     score.toFixed(1)],
    ['Pre-novelty games',  pre_novelty_games.toLocaleString()],
    ['Post-novelty games', post_novelty_games === 0 ? 'True novelty (0)' : post_novelty_games],
  ]
  return (
    <div style={{ background: '#111827', borderRadius: 8, padding: 16, marginBottom: 16 }}>
      <div style={{ color: '#9ca3af', fontSize: 11, textTransform: 'uppercase',
                    letterSpacing: '0.05em', marginBottom: 10 }}>Evaluation</div>
      {rows.map(([label, val]) => (
        <div key={label} style={{ display: 'flex', justifyContent: 'space-between',
                                   padding: '3px 0', borderBottom: '1px solid #1f2937' }}>
          <span style={{ color: '#6b7280', fontSize: 12 }}>{label}</span>
          <span style={{ fontFamily: 'monospace', fontSize: 12 }}>{val}</span>
        </div>
      ))}
      {Object.keys(depth_evals).length > 0 && (
        <div style={{ marginTop: 8 }}>
          {Object.entries(depth_evals).sort(([a],[b]) => +a - +b).map(([d, v]) => (
            <div key={d} style={{ display: 'flex', justifyContent: 'space-between', padding: '2px 0' }}>
              <span style={{ color: '#4b5563', fontSize: 11 }}>depth {d}</span>
              <span style={{ fontFamily: 'monospace', fontSize: 11, color: '#9ca3af' }}>{v}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function ContinuationPanel({ moves, startPly }) {
  const parts = []
  moves.forEach((san, i) => {
    const ply = startPly + i
    if (ply % 2 === 0) parts.push(
      <span key={`n${i}`} style={{ color: '#4b5563', marginRight: 2 }}>
        {Math.floor(ply / 2) + 1}.
      </span>
    )
    parts.push(
      <span key={`m${i}`} style={{ color: '#d1d5db', marginRight: 5 }}>{san}</span>
    )
  })
  return (
    <div style={{ background: '#111827', borderRadius: 8, padding: 16 }}>
      <div style={{ color: '#9ca3af', fontSize: 11, textTransform: 'uppercase',
                    letterSpacing: '0.05em', marginBottom: 8 }}>Engine continuation</div>
      <div style={{ lineHeight: 2, fontSize: 13 }}>{parts}</div>
    </div>
  )
}

function ToggleBtn({ active, onClick, children }) {
  return (
    <button onClick={onClick} style={{
      background: active ? '#f59e0b' : '#1f2937',
      color:      active ? '#030712' : '#9ca3af',
      border:     'none',
      borderRadius: 4,
      padding:    '5px 12px',
      fontSize:   12,
      fontWeight: active ? 700 : 400,
      cursor:     'pointer',
      transition: 'all 0.15s',
    }}>
      {children}
    </button>
  )
}
