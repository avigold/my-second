import React, { useState, useEffect, useMemo } from 'react'
import Chessground from '@react-chess/chessground'
import { Chess } from 'chess.js'

function useIsMobile(bp = 640) {
  const [v, setV] = useState(() => window.innerWidth < bp)
  useEffect(() => {
    const h = () => setV(window.innerWidth < bp)
    window.addEventListener('resize', h)
    return () => window.removeEventListener('resize', h)
  }, [bp])
  return v
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function HabitsBoard({ habit, orientation }) {
  const isMobile = useIsMobile()
  const boardSize = isMobile
    ? Math.min(480, window.innerWidth - 24)
    : 480

  // 'arrows' = show starting position with both move arrows
  // 'player' = show result of player's habitual move
  // 'best'   = show result of engine's best move
  const [view, setView] = useState('arrows')

  // Reset to arrows view whenever we switch to a different habit
  useEffect(() => {
    setView('arrows')
  }, [habit?.rank])

  // Arrow-key navigation between views
  useEffect(() => {
    const views = ['arrows', 'player', 'best']
    const handler = (e) => {
      if (e.key === 'ArrowLeft') {
        setView(v => {
          const i = views.indexOf(v)
          return views[Math.max(0, i - 1)]
        })
      }
      if (e.key === 'ArrowRight') {
        setView(v => {
          const i = views.indexOf(v)
          return views[Math.min(views.length - 1, i + 1)]
        })
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])

  const { fen, lastMove, autoShapes } = useMemo(() => {
    if (!habit) return { fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1', lastMove: null, autoShapes: [] }

    if (view === 'arrows') {
      const shapes = []
      if (habit.player_move_orig && habit.player_move_dest) {
        shapes.push({ orig: habit.player_move_orig, dest: habit.player_move_dest, brush: 'red' })
      }
      if (habit.best_move_orig && habit.best_move_dest) {
        shapes.push({ orig: habit.best_move_orig, dest: habit.best_move_dest, brush: 'green' })
      }
      return { fen: habit.fen, lastMove: null, autoShapes: shapes }
    }

    const orig = view === 'player' ? habit.player_move_orig : habit.best_move_orig
    const dest = view === 'player' ? habit.player_move_dest : habit.best_move_dest
    try {
      const chess = new Chess(habit.fen)
      chess.move({ from: orig, to: dest, promotion: 'q' })
      return { fen: chess.fen(), lastMove: [orig, dest], autoShapes: [] }
    } catch {
      return { fen: habit.fen, lastMove: null, autoShapes: [] }
    }
  }, [habit, view])

  if (!habit) return null

  const config = {
    fen,
    orientation,
    lastMove: lastMove ?? undefined,
    movable:  { free: false, color: 'none' },
    draggable: { enabled: false },
    selectable: { enabled: false },
    animation: { enabled: true, duration: 200 },
    drawable: {
      enabled: false,
      visible: true,
      autoShapes,
    },
  }

  return (
    <div style={{ display: 'flex', flexDirection: isMobile ? 'column' : 'row',
                  gap: isMobile ? 16 : 24, alignItems: 'flex-start' }}>

      {/* Board + view controls */}
      <div style={{ flexShrink: 0 }}>
        <div style={{ width: boardSize, height: boardSize }}>
          <Chessground width={boardSize} height={boardSize} config={config} />
        </div>
        <ViewBar view={view} setView={setView} />
      </div>

      {/* Eval panel + Lichess link */}
      <div style={{ flex: 1, minWidth: 0, width: isMobile ? '100%' : 'auto' }}>
        <EvalPanel habit={habit} />
        <LichessLink fen={habit.fen} />
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// ViewBar
// ---------------------------------------------------------------------------

function ViewBar({ view, setView }) {
  const btn = (label, value, title) => (
    <button
      key={value}
      onClick={() => setView(value)}
      title={title}
      style={{
        background: view === value ? '#78350f' : '#1f2937',
        color:      view === value ? '#fbbf24' : '#9ca3af',
        border: 'none', borderRadius: 4,
        padding: '5px 14px', fontSize: 13, cursor: 'pointer',
        transition: 'background 0.1s',
      }}
    >
      {label}
    </button>
  )
  return (
    <div style={{ marginTop: 10, display: 'flex', gap: 6, justifyContent: 'center' }}>
      {btn('↔ Both',   'arrows', 'Show both moves as arrows (←)')}
      {btn('✗ Player', 'player', "Show player's habitual move")}
      {btn('✓ Best',   'best',   "Show engine's best move (→)")}
    </div>
  )
}

// ---------------------------------------------------------------------------
// EvalPanel
// ---------------------------------------------------------------------------

function EvalPanel({ habit }) {
  const nag      = habit.eval_gap_cp >= 75 ? '?' : '?!'
  const gapColor = habit.eval_gap_cp >= 75 ? '#f87171' : habit.eval_gap_cp >= 25 ? '#fbbf24' : '#9ca3af'

  const fmt = (cp) => {
    const v = cp / 100
    return `${v >= 0 ? '+' : ''}${v.toFixed(2)}`
  }

  return (
    <div style={{ background: '#111827', borderRadius: 8, padding: 16, marginBottom: 12 }}>
      <div style={{
        color: '#9ca3af', fontSize: 11, textTransform: 'uppercase',
        letterSpacing: '0.05em', marginBottom: 12,
      }}>
        Habit #{habit.rank}
      </div>

      {/* Side-by-side move cards */}
      <div style={{ display: 'flex', gap: 10, marginBottom: 14 }}>
        <div style={{
          flex: 1, background: '#1f2937', borderRadius: 6,
          padding: '10px 12px', border: '1px solid #450a0a',
        }}>
          <div style={{ color: '#6b7280', fontSize: 10, textTransform: 'uppercase', marginBottom: 4 }}>
            Player {nag}
          </div>
          <div style={{ color: '#f87171', fontFamily: 'monospace', fontSize: 22, fontWeight: 700, lineHeight: 1 }}>
            {habit.player_move_san}
          </div>
          <div style={{ color: '#9ca3af', fontSize: 11, marginTop: 4 }}>
            {fmt(habit.player_eval_cp)}
          </div>
        </div>
        <div style={{
          flex: 1, background: '#1f2937', borderRadius: 6,
          padding: '10px 12px', border: '1px solid #052e16',
        }}>
          <div style={{ color: '#6b7280', fontSize: 10, textTransform: 'uppercase', marginBottom: 4 }}>
            Best ✓
          </div>
          <div style={{ color: '#4ade80', fontFamily: 'monospace', fontSize: 22, fontWeight: 700, lineHeight: 1 }}>
            {habit.best_move_san}
          </div>
          <div style={{ color: '#9ca3af', fontSize: 11, marginTop: 4 }}>
            {fmt(habit.eval_cp)}
          </div>
        </div>
      </div>

      {/* Stats rows */}
      {[
        ['Eval Gap',  `${habit.eval_gap_cp >= 0 ? '+' : ''}${habit.eval_gap_cp.toFixed(0)} cp`, gapColor],
        ['Frequency', `${habit.total_games}× at this position`, null],
        ['Score',     habit.score.toFixed(1), '#fbbf24'],
      ].map(([label, val, color]) => (
        <div key={label} style={{
          display: 'flex', justifyContent: 'space-between',
          padding: '4px 0', borderBottom: '1px solid #1f2937',
        }}>
          <span style={{ color: '#6b7280', fontSize: 12 }}>{label}</span>
          <span style={{ fontFamily: 'monospace', fontSize: 12, color: color || '#e5e7eb' }}>{val}</span>
        </div>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// LichessLink
// ---------------------------------------------------------------------------

function LichessLink({ fen }) {
  const url = `https://lichess.org/analysis/${fen.replace(/ /g, '_')}`
  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      style={{
        display: 'inline-block', color: '#9ca3af', fontSize: 12,
        textDecoration: 'none', padding: '5px 10px',
        border: '1px solid #374151', borderRadius: 4,
        transition: 'color 0.1s',
      }}
    >
      Open in Lichess ↗
    </a>
  )
}
