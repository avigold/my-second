import React, { useEffect, useState } from 'react'
import HabitsTable from './components/HabitsTable.jsx'
import HabitsBoard from './components/HabitsBoard.jsx'

export default function HabitsApp({ jobId, side }) {
  const [habits,   setHabits]   = useState(null)
  const [error,    setError]    = useState(null)
  const [selected, setSelected] = useState(0)

  useEffect(() => {
    fetch(`/api/jobs/${jobId}/habits`)
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json()
      })
      .then(data => {
        setHabits(data)
        setSelected(0)
      })
      .catch(err => setError(err.message))
  }, [jobId])

  if (error) return (
    <div style={{ padding: 32, color: '#f87171' }}>
      Failed to load habits: {error}
    </div>
  )

  if (habits === null) return (
    <div style={{ padding: 32, color: '#9ca3af' }}>Loading habits…</div>
  )

  if (habits.length === 0) return (
    <div style={{ padding: 32, color: '#9ca3af' }}>
      No habit inaccuracies found in this job's output.
    </div>
  )

  const current = habits[selected]

  return (
    <div style={{ display: 'flex', height: '100vh', overflow: 'hidden' }}>
      {/* Left: table */}
      <div style={{
        width: 380,
        minWidth: 280,
        overflowY: 'auto',
        borderRight: '1px solid #1f2937',
        flexShrink: 0,
      }}>
        <div style={{ padding: '12px 16px', borderBottom: '1px solid #1f2937' }}>
          <span style={{ color: '#9ca3af', fontSize: 12 }}>
            {habits.length} habits · job {jobId.slice(0, 8)}
          </span>
          <a href={`/jobs/${jobId}`}
             style={{ float: 'right', color: '#6b7280', fontSize: 12, textDecoration: 'none' }}>
            ← Job log
          </a>
        </div>
        <HabitsTable
          habits={habits}
          selected={selected}
          onSelect={setSelected}
        />
      </div>

      {/* Right: board + detail */}
      <div style={{ flex: 1, overflowY: 'auto', padding: 24 }}>
        <HabitsBoard
          habit={current}
          orientation={side}
        />
      </div>
    </div>
  )
}
