import React, { useEffect, useState } from 'react'
import HabitsPracticeBoard from './components/HabitsPracticeBoard.jsx'

function useIsMobile(bp = 640) {
  const [v, setV] = useState(() => window.innerWidth < bp)
  useEffect(() => {
    const h = () => setV(window.innerWidth < bp)
    window.addEventListener('resize', h)
    return () => window.removeEventListener('resize', h)
  }, [bp])
  return v
}

export default function HabitsPracticeApp({ jobId, side }) {
  const isMobile = useIsMobile()
  const [habits,   setHabits]   = useState(null)
  const [error,    setError]    = useState(null)
  const [index,    setIndex]    = useState(0)
  const [correct,  setCorrect]  = useState(0)
  const [skipped,  setSkipped]  = useState(0)
  const [finished, setFinished] = useState(false)

  useEffect(() => {
    fetch(`/api/jobs/${jobId}/habits`)
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json() })
      .then(data => { setHabits(data); setIndex(0) })
      .catch(err => setError(err.message))
  }, [jobId])

  const advance = (result) => {
    if (!habits) return
    if (result === 'correct') setCorrect(c => c + 1)
    else if (result === 'skip') setSkipped(s => s + 1)
    const next = index + 1
    if (next >= habits.length) {
      setFinished(true)
    } else {
      setIndex(next)
    }
  }

  const restart = () => {
    setIndex(0)
    setCorrect(0)
    setSkipped(0)
    setFinished(false)
  }

  if (error) return (
    <div style={{ padding: 32, color: '#f87171' }}>Failed to load habits: {error}</div>
  )
  if (habits === null) return (
    <div style={{ padding: 32, color: '#9ca3af' }}>Loading habits…</div>
  )
  if (habits.length === 0) return (
    <div style={{ padding: 32, color: '#9ca3af' }}>No habit inaccuracies found.</div>
  )

  if (finished) {
    return <FinishedScreen total={habits.length} correct={correct} skipped={skipped} onRestart={restart} jobId={jobId} />
  }

  const habit = habits[index]
  const progress = {
    current: index + 1,
    total: habits.length,
    correct,
    skipped,
  }

  return (
    <div style={{ display: 'flex', height: '100vh', overflow: 'hidden', background: '#030712', color: '#f3f4f6' }}>
      {/* Header bar */}
      <div style={{
        position: 'absolute', top: 0, left: 0, right: 0,
        padding: '10px 20px',
        borderBottom: '1px solid #1f2937',
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        background: '#030712', zIndex: 10,
      }}>
        <span style={{ fontWeight: 600, fontSize: 14 }}>Habit Practice</span>
        <div style={{ display: 'flex', gap: 16, alignItems: 'center' }}>
          <a href={`/jobs/${jobId}/habits-browser`}
             style={{ color: '#6b7280', fontSize: 12, textDecoration: 'none' }}>
            ← Browse Habits
          </a>
          <a href={`/jobs/${jobId}`}
             style={{ color: '#6b7280', fontSize: 12, textDecoration: 'none' }}>
            ← Job log
          </a>
        </div>
      </div>

      {/* Main content — padded below header */}
      <div style={{ flex: 1, overflowY: 'auto', padding: isMobile ? '60px 12px 16px' : '64px 24px 24px' }}>
        <HabitsPracticeBoard
          key={habit.rank}
          habit={habit}
          orientation={side}
          onNext={(result) => advance(result)}
          onSkip={() => advance('skip')}
          progress={progress}
        />
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// FinishedScreen
// ---------------------------------------------------------------------------
function FinishedScreen({ total, correct, skipped, onRestart, jobId }) {
  const pct = total > 0 ? Math.round((correct / total) * 100) : 0
  return (
    <div style={{
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      height: '100vh', background: '#030712', color: '#f3f4f6',
    }}>
      <div style={{ textAlign: 'center', maxWidth: 360 }}>
        <div style={{ fontSize: 48, marginBottom: 16 }}>
          {pct >= 80 ? '🎯' : pct >= 50 ? '📈' : '💪'}
        </div>
        <h1 style={{ fontSize: 24, fontWeight: 700, marginBottom: 8 }}>Session Complete</h1>
        <p style={{ color: '#9ca3af', marginBottom: 24 }}>
          You worked through all {total} habit exercises.
        </p>
        <div style={{ display: 'flex', justifyContent: 'center', gap: 24, marginBottom: 28 }}>
          <div>
            <div style={{ color: '#4ade80', fontSize: 28, fontWeight: 700 }}>{correct}</div>
            <div style={{ color: '#6b7280', fontSize: 12 }}>Correct</div>
          </div>
          <div>
            <div style={{ color: '#6b7280', fontSize: 28, fontWeight: 700 }}>{skipped}</div>
            <div style={{ color: '#6b7280', fontSize: 12 }}>Skipped</div>
          </div>
          <div>
            <div style={{ color: '#fbbf24', fontSize: 28, fontWeight: 700 }}>{pct}%</div>
            <div style={{ color: '#6b7280', fontSize: 12 }}>Score</div>
          </div>
        </div>
        <div style={{ display: 'flex', gap: 10, justifyContent: 'center' }}>
          <button
            onClick={onRestart}
            style={{
              background: '#1d4ed8', color: '#fff', border: 'none',
              borderRadius: 6, padding: '8px 20px', fontSize: 14,
              cursor: 'pointer', fontWeight: 600,
            }}
          >
            Practice Again
          </button>
          <a
            href={`/jobs/${jobId}/habits-browser`}
            style={{
              background: '#1f2937', color: '#9ca3af',
              borderRadius: 6, padding: '8px 20px', fontSize: 14,
              textDecoration: 'none', display: 'inline-block',
            }}
          >
            Browse Habits
          </a>
        </div>
      </div>
    </div>
  )
}
