import React, { useEffect, useState, useCallback } from 'react'
import RepertoireBoard from './components/RepertoireBoard.jsx'

// ---------------------------------------------------------------------------
// Flatten the nested tree into a Map<id, node> for O(1) lookup.
// ---------------------------------------------------------------------------
function flattenTree(node, map = new Map()) {
  map.set(node.id, node)
  for (const child of node.children) flattenTree(child, map)
  return map
}

export default function RepertoireApp({ jobId, side }) {
  const [nodeMap,  setNodeMap]  = useState(null)
  const [rootId,   setRootId]   = useState(null)
  const [curId,    setCurId]    = useState(null)
  const [path,     setPath]     = useState([])   // [id, ...] from root to current
  const [error,    setError]    = useState(null)

  useEffect(() => {
    fetch(`/api/jobs/${jobId}/repertoire`)
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json()
      })
      .then(data => {
        if (!data.root) { setError('No repertoire data found.'); return }
        const map = flattenTree(data.root)
        setNodeMap(map)
        setRootId(data.root.id)
        setCurId(data.root.id)
        setPath([data.root.id])
      })
      .catch(err => setError(err.message))
  }, [jobId])

  // Navigate to a child node
  const goTo = useCallback((id) => {
    if (!nodeMap) return
    // Build path from root
    const newPath = []
    let n = nodeMap.get(id)
    while (n) {
      newPath.unshift(n.id)
      n = n.parent_id ? nodeMap.get(n.parent_id) : null
    }
    setCurId(id)
    setPath(newPath)
  }, [nodeMap])

  // Go back one step
  const goBack = useCallback(() => {
    if (path.length <= 1) return
    goTo(path[path.length - 2])
  }, [path, goTo])

  // Keyboard navigation
  useEffect(() => {
    const handler = (e) => {
      if (!nodeMap || !curId) return
      const cur = nodeMap.get(curId)
      if (e.key === 'ArrowRight' || e.key === 'ArrowDown') {
        if (cur?.children?.length > 0) goTo(cur.children[0].id)
      }
      if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
        goBack()
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [nodeMap, curId, goTo, goBack])

  if (error) return (
    <div style={{ padding: 32, color: '#f87171' }}>
      Failed to load repertoire: {error}
    </div>
  )
  if (nodeMap === null) return (
    <div style={{ padding: 32, color: '#9ca3af' }}>Loading repertoire…</div>
  )

  const currentNode = nodeMap.get(curId)

  return (
    <div style={{ display: 'flex', height: '100vh', overflow: 'hidden', background: '#030712', color: '#f3f4f6' }}>

      {/* Left: move history (path from root) */}
      <div style={{
        width: 260,
        minWidth: 200,
        overflowY: 'auto',
        borderRight: '1px solid #1f2937',
        flexShrink: 0,
        fontFamily: 'monospace',
        fontSize: 13,
      }}>
        <div style={{ padding: '12px 16px', borderBottom: '1px solid #1f2937' }}>
          <span style={{ color: '#9ca3af', fontSize: 12 }}>
            Repertoire · job {jobId.slice(0, 8)}
          </span>
          <a href={`/jobs/${jobId}`}
             style={{ float: 'right', color: '#6b7280', fontSize: 12, textDecoration: 'none' }}>
            ← Job log
          </a>
        </div>
        <PathList
          path={path}
          nodeMap={nodeMap}
          curId={curId}
          onSelect={goTo}
        />
      </div>

      {/* Right: board + branches */}
      <div style={{ flex: 1, overflowY: 'auto', padding: 24 }}>
        <RepertoireBoard
          node={currentNode}
          nodeMap={nodeMap}
          orientation={side}
          onNavigate={goTo}
          onBack={goBack}
          canGoBack={path.length > 1}
        />
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// PathList — breadcrumb of moves taken to reach current node
// ---------------------------------------------------------------------------
function PathList({ path, nodeMap, curId, onSelect }) {
  if (!nodeMap) return null

  // Build rows: pair up moves (white + black)
  const moves = path.slice(1).map(id => nodeMap.get(id)).filter(Boolean)
  const rows = []
  let i = 0
  // Detect if root's first move is black (board started as black to move)
  const firstMove = moves[0]
  const startsAsBlack = firstMove && !firstMove.is_player_move
    ? false  // can't tell easily; just show sequentially
    : false

  while (i < moves.length) {
    rows.push({ num: Math.floor(i / 2) + 1, white: moves[i], black: moves[i + 1] ?? null })
    i += 2
  }

  return (
    <div style={{ padding: '8px 4px' }}>
      {rows.map(({ num, white, black }) => (
        <div key={num} style={{ display: 'flex', gap: 2, padding: '1px 4px' }}>
          <span style={{ color: '#4b5563', minWidth: 24, textAlign: 'right', fontSize: 11, paddingTop: 2 }}>
            {num}.
          </span>
          {white && (
            <PathChip node={white} isActive={white.id === curId} onSelect={onSelect} />
          )}
          {black && (
            <PathChip node={black} isActive={black.id === curId} onSelect={onSelect} />
          )}
        </div>
      ))}
    </div>
  )
}

function PathChip({ node, isActive, onSelect }) {
  return (
    <span
      onClick={() => onSelect(node.id)}
      style={{
        padding: '1px 5px',
        borderRadius: 3,
        cursor: 'pointer',
        fontWeight: isActive ? 700 : 400,
        background: isActive ? '#1e3a5f' : 'transparent',
        color: node.is_player_move ? '#fbbf24' : '#9ca3af',
        minWidth: 48,
        display: 'inline-block',
      }}
    >
      {node.move_san}
    </span>
  )
}
