import React, { useEffect, useState } from 'react'
import NoveltyTable from './components/NoveltyTable.jsx'
import NoveltyBoard from './components/NoveltyBoard.jsx'

function useIsMobile(bp = 640) {
  const [v, setV] = useState(() => window.innerWidth < bp)
  useEffect(() => {
    const h = () => setV(window.innerWidth < bp)
    window.addEventListener('resize', h)
    return () => window.removeEventListener('resize', h)
  }, [bp])
  return v
}

export default function App({ jobId, side }) {
  const isMobile = useIsMobile()
  const [novelties, setNovelties] = useState(null)  // null = loading
  const [error,     setError]     = useState(null)
  const [selected,  setSelected]  = useState(0)

  useEffect(() => {
    fetch(`/api/jobs/${jobId}/novelties`)
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json()
      })
      .then(data => {
        setNovelties(data)
        setSelected(0)
      })
      .catch(err => setError(err.message))
  }, [jobId])

  if (error) return (
    <div style={{ padding: 32, color: '#f87171' }}>
      Failed to load novelties: {error}
    </div>
  )

  if (novelties === null) return (
    <div style={{ padding: 32, color: '#9ca3af' }}>Loading novelties…</div>
  )

  if (novelties.length === 0) return (
    <div style={{ padding: 32, color: '#9ca3af' }}>
      No novelties found in this job's output PGN.
    </div>
  )

  const current = novelties[selected]

  const handleSelectNovelty = (novelty) => {
    const idx = novelties.findIndex(n => n.rank === novelty.rank)
    if (idx !== -1) setSelected(idx)
  }

  if (isMobile) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', minHeight: '100vh',
                    background: '#030712', color: '#f3f4f6' }}>
        {/* Header */}
        <div style={{ padding: '10px 14px', borderBottom: '1px solid #1f2937',
                      display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                      flexShrink: 0 }}>
          <span style={{ color: '#9ca3af', fontSize: 12 }}>
            {novelties.length} novelties · job {jobId.slice(0, 8)}
          </span>
          <a href={`/jobs/${jobId}`}
             style={{ color: '#6b7280', fontSize: 12, textDecoration: 'none' }}>
            ← Job
          </a>
        </div>

        {/* Board */}
        <div style={{ padding: '14px 12px' }}>
          <NoveltyBoard
            novelty={current}
            allNovelties={novelties}
            orientation={side}
            onSelectNovelty={handleSelectNovelty}
          />
        </div>

        {/* Table */}
        <div style={{ borderTop: '1px solid #1f2937', overflowX: 'auto' }}>
          <div style={{ minWidth: 420 }}>
            <NoveltyTable novelties={novelties} selected={selected} onSelect={setSelected} />
          </div>
        </div>
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', height: '100vh', overflow: 'hidden' }}>
      {/* Left: table */}
      <div style={{
        width: 420,
        minWidth: 320,
        overflowY: 'auto',
        borderRight: '1px solid #1f2937',
        flexShrink: 0,
      }}>
        <div style={{ padding: '12px 16px', borderBottom: '1px solid #1f2937' }}>
          <span style={{ color: '#9ca3af', fontSize: 12 }}>
            {novelties.length} novelties · job {jobId.slice(0, 8)}
          </span>
          <a href={`/jobs/${jobId}`}
             style={{ float: 'right', color: '#6b7280', fontSize: 12, textDecoration: 'none' }}>
            ← Job log
          </a>
        </div>
        <NoveltyTable
          novelties={novelties}
          selected={selected}
          onSelect={setSelected}
        />
      </div>

      {/* Right: board + detail */}
      <div style={{ flex: 1, overflowY: 'auto', padding: 24 }}>
        <NoveltyBoard
          novelty={current}
          allNovelties={novelties}
          orientation={side}
          onSelectNovelty={handleSelectNovelty}
        />
      </div>
    </div>
  )
}
