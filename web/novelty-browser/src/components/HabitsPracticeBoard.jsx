import React, { useState, useEffect, useMemo, useRef } from 'react'
import Chessground from '@react-chess/chessground'
import { Chess } from 'chess.js'

const BOARD_SIZE = 480

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function getLegalDests(fen, playerColor) {
  try {
    const chess = new Chess(fen)
    const color = playerColor === 'white' ? 'w' : 'b'
    const dests = new Map()
    for (const square of chess.board().flat()) {
      if (!square || square.color !== color) continue
      const moves = chess.moves({ square: square.square, verbose: true })
      if (moves.length > 0) {
        dests.set(square.square, moves.map(m => m.to))
      }
    }
    return dests
  } catch {
    return new Map()
  }
}

function applyMove(fen, orig, dest) {
  try {
    const chess = new Chess(fen)
    const result = chess.move({ from: orig, to: dest, promotion: 'q' })
    return result ? chess.fen() : null
  } catch {
    return null
  }
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function HabitsPracticeBoard({ habit, orientation, onNext, onSkip, progress }) {
  // feedback: null | 'correct' | 'habit' | 'other'
  const [feedback,     setFeedback]     = useState(null)
  const [boardFen,     setBoardFen]     = useState(habit.fen)
  const [lastMove,     setLastMove]     = useState(null)
  const [showAnswer,   setShowAnswer]   = useState(false)
  const [revealed,     setRevealed]     = useState(false)
  const autoAdvanceRef = useRef(null)

  // Reset state when habit changes
  useEffect(() => {
    setFeedback(null)
    setBoardFen(habit.fen)
    setLastMove(null)
    setShowAnswer(false)
    setRevealed(false)
    if (autoAdvanceRef.current) {
      clearTimeout(autoAdvanceRef.current)
      autoAdvanceRef.current = null
    }
  }, [habit.rank])

  // Cleanup timeout on unmount
  useEffect(() => () => {
    if (autoAdvanceRef.current) clearTimeout(autoAdvanceRef.current)
  }, [])

  const handleMove = (orig, dest) => {
    if (feedback === 'correct') return  // already solved

    const newFen = applyMove(boardFen, orig, dest)
    if (!newFen) return

    const isCorrect = orig === habit.best_move_orig && dest === habit.best_move_dest
    const isHabit   = orig === habit.player_move_orig && dest === habit.player_move_dest

    if (isCorrect) {
      setBoardFen(newFen)
      setLastMove([orig, dest])
      setFeedback('correct')
      // Auto-advance after 1.8 s
      autoAdvanceRef.current = setTimeout(() => onNext('correct'), 1800)
    } else if (isHabit) {
      setFeedback('habit')
      // Reset board after a short pause
      setTimeout(() => {
        setBoardFen(habit.fen)
        setLastMove(null)
        setFeedback(null)
      }, 1400)
    } else {
      setBoardFen(newFen)
      setLastMove([orig, dest])
      setFeedback('other')
      setTimeout(() => {
        setBoardFen(habit.fen)
        setLastMove(null)
        setFeedback(null)
      }, 1400)
    }
  }

  const handleReveal = () => {
    setShowAnswer(true)
    setRevealed(true)
    setFeedback(null)
  }

  // Chessground autoShapes: when showing the answer, draw the best-move arrow
  const autoShapes = useMemo(() => {
    if (!showAnswer) return []
    return [
      { orig: habit.best_move_orig, dest: habit.best_move_dest, brush: 'green' },
    ].filter(s => s.orig && s.dest)
  }, [showAnswer, habit.best_move_orig, habit.best_move_dest])

  // Only allow moves before feedback or when resetting
  const canMove = feedback === null || feedback === undefined

  const legalDests = useMemo(() => {
    if (!canMove) return new Map()
    return getLegalDests(boardFen, orientation)
  }, [boardFen, orientation, canMove])

  const config = {
    fen: boardFen,
    orientation,
    lastMove: lastMove ?? undefined,
    movable: {
      free: false,
      color: canMove ? orientation : 'none',
      dests: legalDests,
      events: { after: handleMove },
    },
    draggable: { enabled: canMove },
    selectable: { enabled: canMove },
    animation: { enabled: true, duration: 200 },
    drawable: {
      enabled: false,
      visible: true,
      autoShapes,
    },
  }

  return (
    <div style={{ display: 'flex', gap: 24, alignItems: 'flex-start' }}>

      {/* Left: board */}
      <div style={{ flexShrink: 0 }}>
        <div style={{ width: BOARD_SIZE, height: BOARD_SIZE, position: 'relative' }}>
          <Chessground width={BOARD_SIZE} height={BOARD_SIZE} config={config} />
          {/* Overlay feedback flash */}
          {feedback && feedback !== 'correct' && (
            <div style={{
              position: 'absolute', inset: 0,
              background: feedback === 'habit' ? 'rgba(239,68,68,0.18)' : 'rgba(251,191,36,0.12)',
              pointerEvents: 'none', borderRadius: 2,
              transition: 'opacity 0.2s',
            }} />
          )}
          {feedback === 'correct' && (
            <div style={{
              position: 'absolute', inset: 0,
              background: 'rgba(74,222,128,0.18)',
              pointerEvents: 'none', borderRadius: 2,
            }} />
          )}
        </div>

        {/* Action buttons below board */}
        <div style={{ marginTop: 10, display: 'flex', gap: 8, justifyContent: 'center' }}>
          <button
            onClick={handleReveal}
            disabled={revealed || feedback === 'correct'}
            style={{
              background: '#1f2937', color: revealed ? '#374151' : '#9ca3af',
              border: 'none', borderRadius: 4, padding: '5px 14px',
              fontSize: 13, cursor: revealed ? 'default' : 'pointer',
            }}
          >
            Reveal Answer
          </button>
          <button
            onClick={() => onSkip()}
            style={{
              background: '#1f2937', color: '#9ca3af',
              border: 'none', borderRadius: 4, padding: '5px 14px',
              fontSize: 13, cursor: 'pointer',
            }}
          >
            Skip →
          </button>
        </div>
      </div>

      {/* Right: feedback + info */}
      <div style={{ flex: 1, minWidth: 0 }}>
        <FeedbackPanel feedback={feedback} habit={habit} showAnswer={showAnswer} />
        <ProgressPanel progress={progress} />
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// FeedbackPanel
// ---------------------------------------------------------------------------
function FeedbackPanel({ feedback, habit, showAnswer }) {
  const nag = habit.eval_gap_cp >= 75 ? '?' : '?!'

  const messages = {
    correct: {
      color: '#4ade80',
      bg: '#052e16',
      border: '#166534',
      title: '✓ Correct!',
      body: `${habit.best_move_san} is the best move (+${(habit.eval_gap_cp / 100).toFixed(2)} over ${habit.player_move_san}${nag})`,
    },
    habit: {
      color: '#f87171',
      bg: '#450a0a',
      border: '#7f1d1d',
      title: `✗ That's your habit! (${habit.player_move_san}${nag})`,
      body: `This is the move you usually play — it loses ${habit.eval_gap_cp.toFixed(0)} cp. Try again.`,
    },
    other: {
      color: '#fbbf24',
      bg: '#451a03',
      border: '#78350f',
      title: '~ Not the best move',
      body: `That's not your usual mistake, but it's not optimal here. Try again.`,
    },
  }

  const msg = feedback ? messages[feedback] : null

  const gapColor = habit.eval_gap_cp >= 75 ? '#f87171' : '#fbbf24'

  return (
    <div>
      {/* Feedback message */}
      {msg && (
        <div style={{
          background: msg.bg, border: `1px solid ${msg.border}`,
          borderRadius: 8, padding: '12px 16px', marginBottom: 14,
        }}>
          <div style={{ color: msg.color, fontWeight: 700, fontSize: 14, marginBottom: 4 }}>{msg.title}</div>
          <div style={{ color: '#9ca3af', fontSize: 12 }}>{msg.body}</div>
        </div>
      )}

      {/* Habit info panel */}
      <div style={{ background: '#111827', borderRadius: 8, padding: '12px 16px', marginBottom: 14 }}>
        <div style={{ color: '#6b7280', fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 10 }}>
          Habit #{habit.rank}
        </div>

        {/* Move cards — shown after answer revealed or correct */}
        {(showAnswer || feedback === 'correct') && (
          <div style={{ display: 'flex', gap: 10, marginBottom: 12 }}>
            <div style={{ flex: 1, background: '#1f2937', borderRadius: 6, padding: '8px 12px', border: '1px solid #450a0a' }}>
              <div style={{ color: '#6b7280', fontSize: 10, textTransform: 'uppercase', marginBottom: 4 }}>Your Habit {nag}</div>
              <div style={{ color: '#f87171', fontFamily: 'monospace', fontSize: 20, fontWeight: 700 }}>{habit.player_move_san}</div>
              <div style={{ color: '#9ca3af', fontSize: 11, marginTop: 2 }}>
                {(habit.player_eval_cp / 100 >= 0 ? '+' : '')}{(habit.player_eval_cp / 100).toFixed(2)}
              </div>
            </div>
            <div style={{ flex: 1, background: '#1f2937', borderRadius: 6, padding: '8px 12px', border: '1px solid #052e16' }}>
              <div style={{ color: '#6b7280', fontSize: 10, textTransform: 'uppercase', marginBottom: 4 }}>Best Move ✓</div>
              <div style={{ color: '#4ade80', fontFamily: 'monospace', fontSize: 20, fontWeight: 700 }}>{habit.best_move_san}</div>
              <div style={{ color: '#9ca3af', fontSize: 11, marginTop: 2 }}>
                {(habit.eval_cp / 100 >= 0 ? '+' : '')}{(habit.eval_cp / 100).toFixed(2)}
              </div>
            </div>
          </div>
        )}

        {/* Stats */}
        {[
          ['Eval Gap',  `${habit.eval_gap_cp >= 0 ? '+' : ''}${habit.eval_gap_cp.toFixed(0)} cp`, gapColor],
          ['Frequency', `${habit.total_games}× at this position`, null],
          ['Score',     habit.score.toFixed(1), '#fbbf24'],
        ].map(([label, val, color]) => (
          <div key={label} style={{ display: 'flex', justifyContent: 'space-between', padding: '3px 0', borderBottom: '1px solid #1f2937' }}>
            <span style={{ color: '#6b7280', fontSize: 12 }}>{label}</span>
            <span style={{ fontFamily: 'monospace', fontSize: 12, color: color || '#e5e7eb' }}>{val}</span>
          </div>
        ))}

        {/* Prompt when no feedback yet */}
        {!feedback && !showAnswer && (
          <div style={{ marginTop: 12, color: '#6b7280', fontSize: 12 }}>
            Find the best move. Play it on the board.
          </div>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// ProgressPanel
// ---------------------------------------------------------------------------
function ProgressPanel({ progress }) {
  const { current, total, correct, skipped } = progress
  const pct = total > 0 ? Math.round((current / total) * 100) : 0

  return (
    <div style={{ background: '#111827', borderRadius: 8, padding: '12px 16px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
        <span style={{ color: '#9ca3af', fontSize: 12 }}>{current} / {total}</span>
        <div style={{ display: 'flex', gap: 12 }}>
          <span style={{ color: '#4ade80', fontSize: 12 }}>✓ {correct}</span>
          <span style={{ color: '#6b7280', fontSize: 12 }}>→ {skipped}</span>
        </div>
      </div>
      {/* Progress bar */}
      <div style={{ background: '#1f2937', borderRadius: 4, height: 4 }}>
        <div style={{
          background: '#22c55e', borderRadius: 4, height: 4,
          width: `${pct}%`, transition: 'width 0.3s',
        }} />
      </div>
    </div>
  )
}
