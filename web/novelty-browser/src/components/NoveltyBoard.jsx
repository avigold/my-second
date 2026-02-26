import React, { useState, useEffect, useMemo, useRef } from 'react'
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

export default function NoveltyBoard({ novelty, allNovelties, orientation, onSelectNovelty }) {
  const isMobile = useIsMobile()
  const boardSize = isMobile ? Math.min(480, window.innerWidth - 24) : 480
  const [currentPly, setCurrentPly] = useState(0)

  // ---------- Build positions array from root FEN + all moves ----------
  const { positions, noveltyPly } = useMemo(() => {
    if (!novelty) return { positions: [], noveltyPly: 0 }

    const chess = new Chess(novelty.root_fen)
    const positions = [{ fen: chess.fen(), lastMove: null, san: null, isNovelty: false, isCont: false }]

    for (const san of novelty.book_moves_san) {
      const m = chess.move(san)
      positions.push({ fen: chess.fen(), lastMove: [m.from, m.to], san, isNovelty: false, isCont: false })
    }

    const noveltyPly = positions.length
    try {
      const m = chess.move(novelty.novelty_san)
      positions.push({ fen: chess.fen(), lastMove: [m.from, m.to], san: novelty.novelty_san, isNovelty: true, isCont: false })
    } catch {
      // Fallback if chess.js can't parse the SAN (shouldn't happen, but be safe)
      positions.push({ fen: novelty.fen_after, lastMove: [novelty.novelty_orig, novelty.novelty_dest], san: novelty.novelty_san, isNovelty: true, isCont: false })
    }

    for (const san of novelty.continuations_san) {
      try {
        const m = chess.move(san)
        positions.push({ fen: chess.fen(), lastMove: [m.from, m.to], san, isNovelty: false, isCont: true })
      } catch { break }
    }

    return { positions, noveltyPly }
  }, [novelty])

  // Jump to the novelty position whenever the selected novelty changes
  useEffect(() => {
    setCurrentPly(noveltyPly)
  }, [noveltyPly, novelty?.rank])

  // Arrow-key navigation
  useEffect(() => {
    const handler = (e) => {
      if (e.key === 'ArrowLeft')  setCurrentPly(p => Math.max(0, p - 1))
      if (e.key === 'ArrowRight') setCurrentPly(p => Math.min(positions.length - 1, p + 1))
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [positions.length])

  // ---------- Branch map: position index → [{san, rank}] ----------
  // branchMap[i] = alternative moves from position i that other novelties take
  const branchMap = useMemo(() => {
    if (!novelty || !allNovelties?.length) return {}
    const map = {}
    const myMoves = [...novelty.book_moves_san, novelty.novelty_san]

    for (const other of allNovelties) {
      if (other.rank === novelty.rank) continue
      const otherMoves = [...other.book_moves_san, other.novelty_san]
      for (let i = 0; i < myMoves.length && i < otherMoves.length; i++) {
        if (myMoves[i] !== otherMoves[i]) {
          if (!map[i]) map[i] = []
          if (!map[i].find(b => b.san === otherMoves[i]))
            map[i].push({ san: otherMoves[i], rank: other.rank })
          break
        }
      }
    }
    return map
  }, [novelty, allNovelties])

  if (!novelty) return null

  const pos = positions[currentPly] || positions[0]
  const config = {
    fen: pos.fen,
    orientation,
    lastMove: pos.lastMove ?? undefined,
    movable:  { free: false, color: 'none' },
    draggable: { enabled: false },
    selectable: { enabled: false },
    animation: { enabled: true, duration: 200 },
  }

  if (isMobile) {
    return (
      <div>
        {/* Board */}
        <div style={{ width: boardSize, height: boardSize }}>
          <Chessground width={boardSize} height={boardSize} config={config} />
        </div>
        <NavBar
          currentPly={currentPly}
          maxPly={positions.length - 1}
          noveltyPly={noveltyPly}
          setCurrentPly={setCurrentPly}
        />
        {/* Move list + eval */}
        <div style={{ marginTop: 12 }}>
          <MoveList
            positions={positions}
            currentPly={currentPly}
            noveltyPly={noveltyPly}
            noveltyRank={novelty.rank}
            onSelect={setCurrentPly}
            branchMap={branchMap}
            rootFen={novelty.root_fen}
            onSelectNovelty={onSelectNovelty}
            allNovelties={allNovelties}
          />
          <EvalPanel novelty={novelty} />
        </div>
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', gap: 24, alignItems: 'flex-start' }}>

      {/* Left: board + nav */}
      <div style={{ flexShrink: 0 }}>
        <div style={{ width: 480, height: 480 }}>
          <Chessground width={480} height={480} config={config} />
        </div>
        <NavBar
          currentPly={currentPly}
          maxPly={positions.length - 1}
          noveltyPly={noveltyPly}
          setCurrentPly={setCurrentPly}
        />
      </div>

      {/* Right: move list + eval */}
      <div style={{ flex: 1, minWidth: 0 }}>
        <MoveList
          positions={positions}
          currentPly={currentPly}
          noveltyPly={noveltyPly}
          noveltyRank={novelty.rank}
          onSelect={setCurrentPly}
          branchMap={branchMap}
          rootFen={novelty.root_fen}
          onSelectNovelty={onSelectNovelty}
          allNovelties={allNovelties}
        />
        <EvalPanel novelty={novelty} />
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// NavBar
// ---------------------------------------------------------------------------

function NavBar({ currentPly, maxPly, noveltyPly, setCurrentPly }) {
  const btn = (label, onClick, title, highlight) => (
    <button onClick={onClick} title={title} style={{
      background: highlight ? '#78350f' : '#1f2937',
      color: highlight ? '#fbbf24' : '#9ca3af',
      border: 'none', borderRadius: 4,
      padding: '5px 10px', fontSize: 13, cursor: 'pointer',
      transition: 'background 0.1s',
    }}>{label}</button>
  )
  return (
    <div style={{ marginTop: 10, display: 'flex', gap: 6, justifyContent: 'center' }}>
      {btn('⏮', () => setCurrentPly(0),       'Start (Home)')}
      {btn('◀', () => setCurrentPly(p => Math.max(0, p - 1)),       'Previous (←)')}
      {btn('▶', () => setCurrentPly(p => Math.min(maxPly, p + 1)),  'Next (→)')}
      {btn('⏭', () => setCurrentPly(maxPly),  'End')}
      {btn('★', () => setCurrentPly(noveltyPly), 'Jump to novelty', true)}
    </div>
  )
}

// ---------------------------------------------------------------------------
// MoveList — full game score with click-to-navigate and branch annotations
// ---------------------------------------------------------------------------

function MoveList({ positions, currentPly, noveltyPly, noveltyRank, onSelect, branchMap, rootFen, onSelectNovelty, allNovelties }) {
  const containerRef = useRef(null)

  // Helper: scroll a [data-ply] element to the vertical center of the container.
  // Uses getBoundingClientRect so it's correct regardless of DOM nesting, and
  // sets scrollTop directly so it always targets THIS container rather than
  // letting scrollIntoView pick the nearest scrollable ancestor (which can be
  // the outer page when the list content barely exceeds maxHeight).
  const scrollToPly = (ply) => {
    const container = containerRef.current
    if (!container) return
    const el = container.querySelector(`[data-ply="${ply}"]`)
    if (!el) return
    const cr = container.getBoundingClientRect()
    const er = el.getBoundingClientRect()
    container.scrollTop += er.top - cr.top - (container.clientHeight - er.height) / 2
  }

  // Scroll when the user navigates (currentPly changes).
  useEffect(() => { scrollToPly(currentPly) }, [currentPly])

  // Scroll to the novelty move whenever the displayed novelty changes.
  // Uses noveltyRank (not currentPly) so it fires even when two novelties share
  // the same ply number — in that case currentPly never changes and the
  // [currentPly] effect wouldn't fire.  We query noveltyPly directly from the
  // new DOM, bypassing any stale-currentPly timing issue.
  useEffect(() => { scrollToPly(noveltyPly) }, [noveltyRank])

  // Parse root FEN for move-number and side-to-move
  const fenParts   = (rootFen || '').split(' ')
  const rootIsBlack = fenParts[1] === 'b'
  const startMoveNum = parseInt(fenParts[5]) || 1

  // Build flat list of move descriptors (one per position[1..N])
  const moves = positions.slice(1).map((pos, j) => {
    // j = 0-indexed position in slice → plyIndex = j+1 in original positions array
    // Which side played move j+1?
    const turnOffset = rootIsBlack ? 1 : 0
    const isWhite = (j + turnOffset) % 2 === 0
    const moveNum = startMoveNum + Math.floor((j + turnOffset) / 2)
    return {
      ...pos,
      plyIndex: j + 1,
      isWhite,
      moveNum,
      isCurrent: currentPly === j + 1,
      branches: branchMap[j] || [],   // alternatives at positions[j] (before this move)
    }
  })

  // Group into display rows: [{ moveNum, white, black }]
  const rows = []
  let i = 0
  if (rootIsBlack && moves.length > 0) {
    rows.push({ moveNum: moves[0].moveNum, white: null, black: moves[0] })
    i = 1
  }
  while (i < moves.length) {
    rows.push({ moveNum: moves[i].moveNum, white: moves[i], black: moves[i + 1] ?? null })
    i += 2
  }

  // Separator index: between last book move and novelty in the rows
  const noveltyMoveIndex = noveltyPly - 1  // index in `moves` array

  return (
    <div ref={containerRef} style={{
      background: '#0f172a', borderRadius: 8, padding: '10px 12px',
      marginBottom: 12, maxHeight: 300, overflowY: 'auto',
      fontFamily: 'monospace', fontSize: 13, lineHeight: 1.7,
    }}>
      {rows.map(({ moveNum, white, black }, rowIdx) => {
        // Check if novelty falls in this row — to draw the separator line
        const noveltyIsWhiteHere = white?.plyIndex === noveltyPly
        const noveltyIsBlackHere = black?.plyIndex === noveltyPly

        return (
          <React.Fragment key={rowIdx}>
            {(noveltyIsWhiteHere || noveltyIsBlackHere) && (
              <div style={{ borderTop: '1px solid #1f2937', margin: '4px 0' }} />
            )}
            <div style={{ display: 'flex', alignItems: 'flex-start', gap: 2 }}>
              <span style={{ color: '#4b5563', minWidth: 32, textAlign: 'right',
                             paddingRight: 6, paddingTop: 2, fontSize: 11 }}>
                {moveNum}.{rootIsBlack && rowIdx === 0 ? '..' : ''}
              </span>
              <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', flex: 1 }}>
                {white && <MoveChip move={white} onSelect={onSelect}
                            onSelectNovelty={onSelectNovelty} allNovelties={allNovelties} />}
                {!white && black && (
                  // Black-only row at start (root is black to move)
                  <span style={{ minWidth: 52, display: 'inline-block' }} />
                )}
                {black && <MoveChip move={black} onSelect={onSelect}
                            onSelectNovelty={onSelectNovelty} allNovelties={allNovelties} />}
              </div>
            </div>
          </React.Fragment>
        )
      })}
    </div>
  )
}

function MoveChip({ move, onSelect, onSelectNovelty, allNovelties }) {
  const { san, plyIndex, isCurrent, isNovelty, isCont, branches } = move

  const textColor  = isNovelty ? '#fbbf24' : isCont ? '#6b7280' : '#e5e7eb'
  const bgColor    = isCurrent
    ? (isNovelty ? '#78350f' : '#1e3a5f')
    : 'transparent'

  return (
    <span data-ply={plyIndex} style={{ display: 'inline-flex', flexDirection: 'column', minWidth: 52 }}>
      {/* Main move */}
      <span
        onClick={() => onSelect(plyIndex)}
        title={isCont ? 'Engine continuation' : isNovelty ? 'Novelty' : 'Book move'}
        style={{
          color: textColor, background: bgColor,
          borderRadius: 3, padding: '1px 5px',
          cursor: 'pointer', fontWeight: isNovelty ? 700 : 400,
          display: 'inline-block',
        }}
      >
        {san}{isNovelty ? '!' : ''}
      </span>

      {/* Branch alternatives from other novelties */}
      {branches.length > 0 && (
        <span style={{ display: 'flex', flexDirection: 'column', gap: 1, paddingLeft: 5, marginTop: 1 }}>
          {branches.map(({ san: bSan, rank }) => (
            <BranchChip key={rank} san={bSan} rank={rank}
              onSelectNovelty={onSelectNovelty} allNovelties={allNovelties} />
          ))}
        </span>
      )}
    </span>
  )
}

function BranchChip({ san, rank, onSelectNovelty, allNovelties }) {
  const handleClick = (e) => {
    e.stopPropagation()
    if (!onSelectNovelty || !allNovelties) return
    const target = allNovelties.find(n => n.rank === rank)
    if (target) onSelectNovelty(target)
  }
  return (
    <span
      onClick={handleClick}
      title={`Rank ${rank}: ${san}`}
      style={{
        color: '#4ade80', fontSize: 10, cursor: 'pointer',
        display: 'inline-block', padding: '0 3px',
        border: '1px solid #166534', borderRadius: 3,
        lineHeight: 1.6,
      }}
    >
      ↳ {san}
    </span>
  )
}

// ---------------------------------------------------------------------------
// EvalPanel
// ---------------------------------------------------------------------------

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
    <div style={{ background: '#111827', borderRadius: 8, padding: 16 }}>
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
