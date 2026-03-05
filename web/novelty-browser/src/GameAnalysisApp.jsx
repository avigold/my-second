import React, {
  useState, useEffect, useCallback, useRef, useMemo,
} from 'react'
import Chessground from '@react-chess/chessground'
import { Chess } from 'chess.js'

// ---------------------------------------------------------------------------
// Move classification
// ---------------------------------------------------------------------------

const GRADES = {
  best:       { sym: '✓',  color: '#22c55e', label: 'Best'       },
  excellent:  { sym: '!',  color: '#06b6d4', label: 'Excellent'  },
  good:       { sym: '⊕',  color: '#60a5fa', label: 'Good'       },
  inaccuracy: { sym: '?!', color: '#f59e0b', label: 'Inaccuracy' },
  mistake:    { sym: '?',  color: '#f97316', label: 'Mistake'    },
  blunder:    { sym: '??', color: '#ef4444', label: 'Blunder'    },
}

function gradeMove(cpLoss) {
  if (cpLoss <=   5) return 'best'
  if (cpLoss <=  15) return 'excellent'
  if (cpLoss <=  30) return 'good'
  if (cpLoss <= 100) return 'inaccuracy'
  if (cpLoss <= 250) return 'mistake'
  return 'blunder'
}

// Return the pixel top-left of a square on a boardSize×boardSize board.
function squarePx(sq, boardSize, orientation) {
  const file = sq.charCodeAt(0) - 97   // a=0 … h=7
  const rank = parseInt(sq[1]) - 1     // '1'→0, '8'→7
  const sz   = boardSize / 8
  const col  = orientation === 'white' ? file     : 7 - file
  const row  = orientation === 'white' ? 7 - rank : rank
  return { left: col * sz, top: row * sz, sz }
}

// ---------------------------------------------------------------------------
// Stockfish WASM hook
// ---------------------------------------------------------------------------

function useStockfish() {
  const workerRef  = useRef(null)
  const pendingRef = useRef({})   // multipv → latest parsed line
  const fenRef     = useRef(null) // FEN currently being searched
  const nextFenRef = useRef(null) // FEN queued for after readyok
  const [lines, setLines] = useState([])

  // Create (or recreate) the Worker.  Called on mount and after crashes.
  const startWorker = useCallback(() => {
    workerRef.current?.terminate()
    workerRef.current = null

    let worker
    try { worker = new Worker('/static/stockfish.js?v=18s') } catch (_) { return }

    worker.onmessage = ({ data }) => {
      const msg = typeof data === 'string' ? data : String(data)

      // Engine finished stopping — now safe to send new position.
      if (msg === 'readyok') {
        const fen = nextFenRef.current
        nextFenRef.current = null
        if (fen) {
          fenRef.current = fen
          pendingRef.current = {}
          setLines([])
          worker.postMessage(`position fen ${fen}`)
          worker.postMessage('go depth 20 multipv 3')
        }
        return
      }

      if (!msg.startsWith('info depth')) return

      // Parse: info depth N seldepth X multipv M score cp V pv uci...
      const depthM = msg.match(/depth (\d+)/)
      const mpvM   = msg.match(/multipv (\d+)/)
      const cpM    = msg.match(/score cp (-?\d+)/)
      const mateM  = msg.match(/score mate (-?\d+)/)
      const pvM    = msg.match(/ pv (.+)$/)

      if (!mpvM || !pvM) return
      const depth = depthM ? parseInt(depthM[1]) : 0
      const mpv   = parseInt(mpvM[1])
      const pvUCI = pvM[1].trim().split(' ')

      let scoreStr = '0.00'
      if (mateM) {
        scoreStr = `#${mateM[1]}`
      } else if (cpM) {
        const cp  = parseInt(cpM[1])
        const val = (cp / 100).toFixed(2)
        scoreStr  = cp >= 0 ? `+${val}` : `${val}`
      }

      // Convert PV UCI moves to SAN + FEN sequence for interactive navigation
      let pvMoves = []  // [{san, fen, uci, isWhite, moveNum}]
      try {
        const ch = new Chess(fenRef.current)
        for (const uci of pvUCI.slice(0, 8)) {
          if (ch.isGameOver()) break
          const from    = uci.slice(0, 2)
          const to      = uci.slice(2, 4)
          const prom    = uci.length === 5 ? uci[4] : undefined
          const moveNum = ch.moveNumber()
          const move    = ch.move({ from, to, promotion: prom })
          if (!move) break
          pvMoves.push({ san: move.san, fen: ch.fen(), uci, isWhite: move.color === 'w', moveNum })
        }
      } catch (_) {}

      pendingRef.current[mpv] = { depth, score: scoreStr, pvMoves, mpv }
      setLines(Object.values(pendingRef.current).sort((a, b) => a.mpv - b.mpv))
    }

    // If the WASM engine crashes, clear the ref so analyse() restarts it.
    worker.onerror = () => { workerRef.current = null }

    worker.postMessage('uci')
    worker.postMessage('setoption name MultiPV value 3')
    worker.postMessage('isready')
    workerRef.current = worker
  }, [])

  useEffect(() => {
    startWorker()
    return () => { workerRef.current?.terminate(); workerRef.current = null }
  }, [startWorker])

  const analyse = useCallback((fen) => {
    // Auto-restart if the engine crashed.
    if (!workerRef.current) startWorker()
    const w = workerRef.current
    if (!w) return

    // Queue the FEN; it will be dispatched after the engine acknowledges stop.
    nextFenRef.current = fen
    w.postMessage('stop')
    w.postMessage('isready')  // readyok fires when stop is processed → position + go sent
  }, [startWorker])

  const stop = useCallback(() => {
    workerRef.current?.postMessage('stop')
  }, [])

  return { lines, analyse, stop }
}

// ---------------------------------------------------------------------------
// Game analysis hook — grades every move at depth 12 in a background worker
// ---------------------------------------------------------------------------

function useGameAnalysis(moves) {
  const workerRef   = useRef(null)
  const evalsRef    = useRef([])    // evalsRef.current[ply] = {cp, isMate}
  const lastInfoRef = useRef(null)  // last 'info depth' line
  const [grades, setGrades]     = useState({})  // ply → grade string
  const [progress, setProgress] = useState(0)   // 0..1

  useEffect(() => {
    // Clean up previous analysis.
    workerRef.current?.terminate()
    workerRef.current = null
    evalsRef.current  = []
    lastInfoRef.current = null
    setGrades({})
    setProgress(0)

    if (!moves || moves.length < 2) return

    let worker
    try { worker = new Worker('/static/stockfish.js?v=18s') } catch (_) { return }

    let cancelled = false
    let plyIdx = 0

    const evalToCP = (e) => e.isMate ? (e.cp > 0 ? 30000 : -30000) : e.cp

    const addGrade = (i) => {
      const ev = evalsRef.current
      if (!ev[i] || !ev[i - 1] || !moves[i]) return
      const cpBefore = evalToCP(ev[i - 1])
      const cpAfter  = evalToCP(ev[i])
      const loss = moves[i].color === 'white'
        ? cpBefore - cpAfter   // white: eval should stay high
        : cpAfter  - cpBefore  // black: eval should stay low (for white)
      const grade = gradeMove(Math.max(0, loss))
      if (!cancelled) setGrades(prev => ({ ...prev, [i]: grade }))
    }

    const sendNext = () => {
      if (cancelled || plyIdx >= moves.length) {
        if (!cancelled) setProgress(1)
        worker.terminate()
        return
      }
      worker.postMessage(`position fen ${moves[plyIdx].fen}`)
      worker.postMessage('go depth 12')
    }

    worker.onmessage = ({ data }) => {
      if (cancelled) return
      const msg = typeof data === 'string' ? data : String(data)

      if (msg === 'readyok') { sendNext(); return }

      if (msg.startsWith('info depth')) { lastInfoRef.current = msg; return }

      if (msg.startsWith('bestmove')) {
        const info = lastInfoRef.current || ''
        const cpM  = info.match(/score cp (-?\d+)/)
        const mateM = info.match(/score mate (-?\d+)/)
        evalsRef.current[plyIdx] = mateM
          ? { cp: parseInt(mateM[1]) > 0 ? 30000 : -30000, isMate: true }
          : { cp: cpM ? parseInt(cpM[1]) : 0, isMate: false }
        lastInfoRef.current = null
        plyIdx++
        if (!cancelled) setProgress(plyIdx / moves.length)
        if (plyIdx >= 2) addGrade(plyIdx - 1)
        sendNext()
      }
    }

    worker.onerror = () => { if (!cancelled) setProgress(1) }

    workerRef.current = worker
    worker.postMessage('uci')
    worker.postMessage('setoption name MultiPV value 1')
    worker.postMessage('isready')

    return () => { cancelled = true; worker.terminate() }
  }, [moves])

  return { grades, progress }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function useIsMobile() {
  const [mobile, setMobile] = useState(() => window.innerWidth < 768)
  useEffect(() => {
    const fn = () => setMobile(window.innerWidth < 768)
    window.addEventListener('resize', fn)
    return () => window.removeEventListener('resize', fn)
  }, [])
  return mobile
}

function useDebounce(value, delay) {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay)
    return () => clearTimeout(t)
  }, [value, delay])
  return debounced
}

const RESULT_ICON = { win: '✓', draw: '½', loss: '✗', unknown: '?' }
const RESULT_COLOR = { win: '#4ade80', draw: '#facc15', loss: '#f87171', unknown: '#9ca3af' }

function formatDate(d) {
  if (!d) return ''
  return d.replace(/\./g, '-').replace('??', '').trim()
}

// ---------------------------------------------------------------------------
// GameList
// ---------------------------------------------------------------------------

function GameList({ jobId, selectedIndex, onSelect }) {
  const isMobile = useIsMobile()

  const [page, setPage]         = useState(1)
  const [q, setQ]               = useState('')
  const [resultFilter, setResultFilter] = useState('all')
  const [data, setData]         = useState(null)
  const [loading, setLoading]   = useState(false)

  const debouncedQ = useDebounce(q, 300)

  useEffect(() => {
    setPage(1)
  }, [debouncedQ, resultFilter])

  useEffect(() => {
    setLoading(true)
    const params = new URLSearchParams({
      page, per_page: 20, q: debouncedQ, result: resultFilter,
    })
    fetch(`/api/jobs/${jobId}/pgn-games?${params}`)
      .then(r => r.json())
      .then(d => { setData(d); setLoading(false) })
      .catch(() => setLoading(false))
  }, [jobId, page, debouncedQ, resultFilter])

  const totalPages = data ? Math.ceil(data.total / 20) : 1

  const filterBtn = (label, val) => (
    <button
      key={val}
      onClick={() => setResultFilter(val)}
      style={{
        padding: '3px 10px',
        borderRadius: 6,
        border: 'none',
        cursor: 'pointer',
        fontSize: 12,
        background: resultFilter === val ? '#3b82f6' : '#374151',
        color: '#fff',
      }}
    >{label}</button>
  )

  return (
    <div style={{
      width: isMobile ? '100%' : 280,
      minWidth: isMobile ? undefined : 220,
      display: 'flex',
      flexDirection: 'column',
      borderRight: isMobile ? 'none' : '1px solid #374151',
      borderBottom: isMobile ? '1px solid #374151' : 'none',
      background: '#111827',
      overflow: 'hidden',
    }}>
      {/* Filters */}
      <div style={{ padding: '10px 12px', borderBottom: '1px solid #374151' }}>
        <div style={{ display: 'flex', gap: 4, marginBottom: 8, flexWrap: 'wrap' }}>
          {filterBtn('All', 'all')}
          {filterBtn('Win', 'win')}
          {filterBtn('Draw', 'draw')}
          {filterBtn('Loss', 'loss')}
        </div>
        <input
          value={q}
          onChange={e => setQ(e.target.value)}
          placeholder="Search opponent…"
          style={{
            width: '100%', boxSizing: 'border-box',
            padding: '5px 8px', borderRadius: 6,
            border: '1px solid #374151', background: '#1f2937',
            color: '#f9fafb', fontSize: 13,
          }}
        />
      </div>

      {/* Game rows */}
      <div style={{ flex: 1, overflowY: 'auto' }}>
        {loading && (
          <div style={{ padding: 16, color: '#9ca3af', fontSize: 13 }}>Loading…</div>
        )}
        {!loading && data && data.games.map(g => {
          const isSelected = g.index === selectedIndex
          const ic  = RESULT_ICON[g.player_result]  || '?'
          const col = RESULT_COLOR[g.player_result] || '#9ca3af'
          return (
            <div
              key={g.index}
              onClick={() => onSelect(g.index)}
              style={{
                padding: '8px 12px',
                cursor: 'pointer',
                borderBottom: '1px solid #1f2937',
                background: isSelected ? '#1d4ed8' : 'transparent',
                display: 'flex',
                alignItems: 'flex-start',
                gap: 8,
              }}
            >
              <span style={{ color: col, fontSize: 14, fontWeight: 700, lineHeight: 1.4, minWidth: 14 }}>{ic}</span>
              <div style={{ minWidth: 0 }}>
                <div style={{ fontSize: 13, color: '#f9fafb', fontWeight: 500, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  {g.opponent}
                </div>
                <div style={{ fontSize: 11, color: '#9ca3af' }}>
                  {g.opening || g.eco || '—'} · {formatDate(g.date)}
                </div>
              </div>
            </div>
          )
        })}
        {!loading && data && data.games.length === 0 && (
          <div style={{ padding: 16, color: '#9ca3af', fontSize: 13 }}>No games found.</div>
        )}
      </div>

      {/* Pagination */}
      {data && totalPages > 1 && (
        <div style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          padding: '8px 12px', borderTop: '1px solid #374151', fontSize: 12, color: '#9ca3af',
        }}>
          <button
            onClick={() => setPage(p => Math.max(1, p - 1))}
            disabled={page <= 1}
            style={{ background: 'none', border: 'none', color: page > 1 ? '#60a5fa' : '#374151', cursor: page > 1 ? 'pointer' : 'default', fontSize: 16 }}
          >←</button>
          <span>{page} / {totalPages}</span>
          <button
            onClick={() => setPage(p => Math.min(totalPages, p + 1))}
            disabled={page >= totalPages}
            style={{ background: 'none', border: 'none', color: page < totalPages ? '#60a5fa' : '#374151', cursor: page < totalPages ? 'pointer' : 'default', fontSize: 16 }}
          >→</button>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// EvalBar
// ---------------------------------------------------------------------------

function EvalBar({ lines }) {
  // White advantage fraction 0–1 (0.5 = equal)
  const score = lines[0]?.score
  let frac = 0.5
  if (score) {
    if (score.startsWith('#')) {
      frac = score.includes('-') ? 0.02 : 0.98
    } else {
      const cp = parseFloat(score) * 100
      frac = 1 / (1 + Math.exp(-cp / 400))
    }
  }
  const whitePct = `${Math.round(frac * 100)}%`
  const blackPct = `${Math.round((1 - frac) * 100)}%`

  return (
    <div style={{
      width: 14, borderRadius: 4, overflow: 'hidden',
      display: 'flex', flexDirection: 'column', flexShrink: 0,
      border: '1px solid #374151', alignSelf: 'stretch',
    }}>
      <div style={{ flex: `0 0 ${blackPct}`, background: '#111827' }} />
      <div style={{ flex: `0 0 ${whitePct}`, background: '#f9fafb' }} />
    </div>
  )
}

// ---------------------------------------------------------------------------
// MoveList
// ---------------------------------------------------------------------------

function MoveList({ moves, currentPly, onPlySelect, grades }) {
  const listRef  = useRef(null)
  const activeRef = useRef(null)

  useEffect(() => {
    activeRef.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, [currentPly])

  // Group into pairs: [{moveNum, white: {ply, san}, black: {ply, san}}]
  const pairs = useMemo(() => {
    const out = []
    for (let i = 1; i < moves.length; i++) {
      const m = moves[i]
      if (m.color === 'white') {
        out.push({ moveNum: m.move_number, white: { ply: i, san: m.san }, black: null })
      } else {
        const last = out[out.length - 1]
        if (last && !last.black) last.black = { ply: i, san: m.san }
        else out.push({ moveNum: m.move_number, white: null, black: { ply: i, san: m.san } })
      }
    }
    return out
  }, [moves])

  const chipStyle = (ply) => ({
    padding: '2px 5px',
    borderRadius: 4,
    cursor: 'pointer',
    background: currentPly === ply ? '#2563eb' : 'transparent',
    color: currentPly === ply ? '#fff' : '#d1d5db',
    fontSize: 13,
    fontFamily: 'monospace',
    border: 'none',
    display: 'inline',
  })

  const GradeTag = ({ ply }) => {
    const g = grades?.[ply]
    if (!g) return null
    const { sym, color } = GRADES[g]
    return <span style={{ fontSize: 10, color, marginLeft: 1, fontWeight: 700, verticalAlign: 'super' }}>{sym}</span>
  }

  return (
    <div ref={listRef} style={{
      overflowY: 'auto',
      flex: '0 0 auto',
      maxHeight: 140,
      padding: '4px 8px',
      borderBottom: '1px solid #374151',
      lineHeight: 1.8,
    }}>
      {pairs.map(({ moveNum, white, black }) => (
        <span key={moveNum}>
          <span style={{ color: '#6b7280', fontSize: 12, marginRight: 2 }}>{moveNum}.</span>
          {white && (
            <>
              <button
                ref={currentPly === white.ply ? activeRef : null}
                onClick={() => onPlySelect(white.ply)}
                style={chipStyle(white.ply)}
              >{white.san}</button>
              <GradeTag ply={white.ply} />
            </>
          )}
          {!white && <span style={{ color: '#6b7280', fontSize: 13 }}>…</span>}
          {' '}
          {black && (
            <>
              <button
                ref={currentPly === black.ply ? activeRef : null}
                onClick={() => onPlySelect(black.ply)}
                style={chipStyle(black.ply)}
              >{black.san}</button>
              <GradeTag ply={black.ply} />
            </>
          )}
          {' '}
        </span>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// AnalysisPanel
// ---------------------------------------------------------------------------

function AnalysisPanel({ jobId, selectedIndex, side }) {
  const isMobile = useIsMobile()

  const [gameData, setGameData] = useState(null)
  const [loading, setLoading]   = useState(false)
  const [ply, setPly]           = useState(0)
  // pvState: null = game mode  |  {lineIdx, pvPly, baseFen} = exploring an engine line
  const [pvState, setPvState]   = useState(null)

  const { lines, analyse }    = useStockfish()
  const { grades, progress }  = useGameAnalysis(gameData?.moves)

  // Fetch game data when selection changes
  useEffect(() => {
    if (selectedIndex == null) return
    setLoading(true)
    setGameData(null)
    setPly(0)
    setPvState(null)
    fetch(`/api/jobs/${jobId}/pgn-games/${selectedIndex}`)
      .then(r => r.json())
      .then(d => { setGameData(d); setLoading(false) })
      .catch(() => setLoading(false))
  }, [jobId, selectedIndex])

  // Any game-ply navigation exits PV mode
  useEffect(() => { setPvState(null) }, [ply])

  // Debounce engine analysis — board updates immediately on every ply change,
  // but we only ask Stockfish to search after the user pauses navigation.
  const enginePly = useDebounce(ply, 300)

  useEffect(() => {
    if (!gameData?.moves) return
    const fen = gameData.moves[enginePly]?.fen
    if (fen) analyse(fen)
  }, [enginePly, gameData, analyse])

  // Keyboard navigation: arrows work for both game and PV modes
  useEffect(() => {
    const handler = (e) => {
      if (!gameData?.moves) return
      if (e.key === 'ArrowLeft') {
        if (pvState) {
          if (pvState.pvPly > 0) setPvState(s => ({ ...s, pvPly: s.pvPly - 1 }))
          else setPvState(null)
        } else {
          setPly(p => Math.max(0, p - 1))
        }
      }
      if (e.key === 'ArrowRight') {
        if (pvState) {
          const maxPly = lines[pvState.lineIdx]?.pvMoves?.length ?? 0
          setPvState(s => ({ ...s, pvPly: Math.min(maxPly, s.pvPly + 1) }))
        } else {
          setPly(p => Math.min(gameData.moves.length - 1, p + 1))
        }
      }
      if (e.key === 'Escape') setPvState(null)
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [gameData, pvState, lines])

  const moves   = gameData?.moves || []
  const headers = gameData?.headers || {}

  // Board position and last-move highlight depend on whether we're in PV mode
  const activePvMoves = pvState ? (lines[pvState.lineIdx]?.pvMoves ?? []) : []
  const boardFen = pvState
    ? (pvState.pvPly === 0 ? pvState.baseFen : activePvMoves[pvState.pvPly - 1]?.fen ?? pvState.baseFen)
    : (moves[ply]?.fen ?? 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1')

  const lastMoveUci = pvState
    ? (pvState.pvPly > 0 ? activePvMoves[pvState.pvPly - 1]?.uci : null)
    : (ply > 0 ? moves[ply]?.uci : null)   // moves[ply].uci is the move that reached position ply
  const lastMove = lastMoveUci ? [lastMoveUci.slice(0, 2), lastMoveUci.slice(2, 4)] : undefined

  // Badge shown on the destination square after a move (game mode only)
  const currentGrade = !pvState && ply > 0 ? grades[ply] : null

  const boardSize = Math.min(isMobile ? window.innerWidth - 24 : 420, 520)

  if (selectedIndex == null) {
    return (
      <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#6b7280', fontSize: 14 }}>
        Select a game from the list
      </div>
    )
  }

  if (loading) {
    return (
      <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#9ca3af', fontSize: 14 }}>
        Loading…
      </div>
    )
  }

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden', minWidth: 0 }}>
      {/* Game header */}
      <div style={{ padding: '8px 16px', borderBottom: '1px solid #374151', fontSize: 13, color: '#9ca3af' }}>
        <span style={{ color: '#f9fafb', fontWeight: 600 }}>{headers.White}</span>
        {' vs '}
        <span style={{ color: '#f9fafb', fontWeight: 600 }}>{headers.Black}</span>
        <span style={{ marginLeft: 8 }}>{headers.Result}</span>
        {headers.Date && <span style={{ marginLeft: 8 }}>{formatDate(headers.Date)}</span>}
        {headers.Opening && <span style={{ marginLeft: 8, fontStyle: 'italic' }}>{headers.Opening}</span>}
      </div>

      {/* Board + engine panel */}
      <div style={{ flex: 1, display: 'flex', flexDirection: isMobile ? 'column' : 'row', overflow: 'hidden', minHeight: 0 }}>
        {/* Board column */}
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', padding: 12, gap: 8, flexShrink: 0 }}>
          {/* Board + badge overlay */}
          <div style={{ position: 'relative', width: boardSize, height: boardSize }}>
            <Chessground
              width={boardSize}
              height={boardSize}
              config={{
                fen:         boardFen,
                orientation: side,
                lastMove,
                movable:     { free: false, color: 'none' },
                draggable:   { enabled: false },
                selectable:  { enabled: false },
                animation:   { enabled: true, duration: 150 },
              }}
            />
            {currentGrade && moves[ply]?.uci && (() => {
              const { left, top, sz } = squarePx(moves[ply].uci.slice(2, 4), boardSize, side)
              const { sym, color }    = GRADES[currentGrade]
              return (
                <div style={{
                  position: 'absolute', pointerEvents: 'none',
                  left: left + sz * 0.58, top: top + sz * 0.04,
                  width: sz * 0.36, height: sz * 0.36,
                  borderRadius: '50%', background: color,
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  fontSize: sz * 0.17, color: '#fff', fontWeight: 800,
                  boxShadow: '0 1px 4px rgba(0,0,0,.7)',
                  border: '1.5px solid rgba(0,0,0,.25)', zIndex: 10,
                }}>
                  {sym}
                </div>
              )
            })()}
          </div>

          {/* Nav controls */}
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            {pvState ? (
              <>
                <button onClick={() => setPvState(s => ({ ...s, pvPly: 0 }))}
                  style={{ background: '#374151', border: 'none', color: '#fff', borderRadius: 6, padding: '4px 10px', cursor: 'pointer', fontSize: 16 }}>⏮</button>
                <button onClick={() => setPvState(s => ({ ...s, pvPly: Math.max(0, s.pvPly - 1) }))}
                  style={{ background: '#374151', border: 'none', color: '#fff', borderRadius: 6, padding: '4px 10px', cursor: 'pointer', fontSize: 16 }}>←</button>
                <button onClick={() => setPvState(null)}
                  style={{ background: 'none', border: '1px solid #374151', color: '#60a5fa', borderRadius: 6, padding: '3px 8px', cursor: 'pointer', fontSize: 11 }}>
                  ↩ game
                </button>
                <button onClick={() => setPvState(s => ({ ...s, pvPly: Math.min(activePvMoves.length, s.pvPly + 1) }))}
                  style={{ background: '#374151', border: 'none', color: '#fff', borderRadius: 6, padding: '4px 10px', cursor: 'pointer', fontSize: 16 }}>→</button>
                <button onClick={() => setPvState(s => ({ ...s, pvPly: activePvMoves.length }))}
                  style={{ background: '#374151', border: 'none', color: '#fff', borderRadius: 6, padding: '4px 10px', cursor: 'pointer', fontSize: 16 }}>⏭</button>
              </>
            ) : (
              <>
                <button onClick={() => setPly(0)}
                  style={{ background: '#374151', border: 'none', color: '#fff', borderRadius: 6, padding: '4px 10px', cursor: 'pointer', fontSize: 16 }}>⏮</button>
                <button onClick={() => setPly(p => Math.max(0, p - 1))}
                  style={{ background: '#374151', border: 'none', color: '#fff', borderRadius: 6, padding: '4px 10px', cursor: 'pointer', fontSize: 16 }}>←</button>
                <span style={{ color: '#9ca3af', fontSize: 12, minWidth: 60, textAlign: 'center' }}>
                  {ply === 0 ? 'Start' : `Move ${moves[ply]?.move_number} (${moves[ply]?.color})`}
                </span>
                <button onClick={() => setPly(p => Math.min(moves.length - 1, p + 1))}
                  style={{ background: '#374151', border: 'none', color: '#fff', borderRadius: 6, padding: '4px 10px', cursor: 'pointer', fontSize: 16 }}>→</button>
                <button onClick={() => setPly(moves.length - 1)}
                  style={{ background: '#374151', border: 'none', color: '#fff', borderRadius: 6, padding: '4px 10px', cursor: 'pointer', fontSize: 16 }}>⏭</button>
              </>
            )}
          </div>
        </div>

        {/* Move list + engine column */}
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden', minWidth: 0, borderLeft: isMobile ? 'none' : '1px solid #374151', borderTop: isMobile ? '1px solid #374151' : 'none' }}>
          {moves.length > 0 && (
            <MoveList
              moves={moves}
              currentPly={pvState ? -1 : ply}
              onPlySelect={(p) => { setPvState(null); setPly(p) }}
              grades={grades}
            />
          )}
          {/* Analysis progress bar */}
          {progress > 0 && progress < 1 && (
            <div style={{ padding: '4px 12px 2px', flexShrink: 0 }}>
              <div style={{ height: 2, background: '#1f2937', borderRadius: 1 }}>
                <div style={{ height: '100%', width: `${progress * 100}%`, background: '#3b82f6', borderRadius: 1, transition: 'width .2s' }} />
              </div>
            </div>
          )}

          {/* Engine panel */}
          <div style={{ flex: 1, padding: '10px 12px', overflow: 'auto' }}>
            <div style={{ fontSize: 11, color: '#6b7280', marginBottom: 8, textTransform: 'uppercase', letterSpacing: '0.05em', display: 'flex', alignItems: 'center', gap: 6 }}>
              <span>Engine lines {lines[0] ? <span style={{ color: '#374151' }}>· depth {lines[0].depth}</span> : null}</span>
              {currentGrade && ['mistake', 'blunder', 'inaccuracy'].includes(currentGrade) && (
                <span style={{
                  fontSize: 10, fontWeight: 700, padding: '1px 6px', borderRadius: 10,
                  background: GRADES[currentGrade].color + '22',
                  color: GRADES[currentGrade].color, textTransform: 'none', letterSpacing: 0,
                }}>
                  {GRADES[currentGrade].sym} {GRADES[currentGrade].label} — engine recommends ↓
                </span>
              )}
            </div>
            {lines.length === 0 ? (
              <div style={{ color: '#4b5563', fontSize: 13 }}>Analysing…</div>
            ) : (
              <div style={{ display: 'flex', gap: 8 }}>
                <EvalBar lines={lines} />
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4, flex: 1, minWidth: 0 }}>
                  {lines.map((ln, lineIdx) => {
                    const isActiveLine = pvState?.lineIdx === lineIdx
                    const scoreColor = ln.score?.startsWith('#') ? '#f59e0b'
                      : parseFloat(ln.score) > 0 ? '#4ade80'
                      : parseFloat(ln.score) < 0 ? '#f87171' : '#d1d5db'
                    return (
                      <div key={lineIdx} style={{
                        padding: '4px 6px', borderRadius: 5,
                        background: isActiveLine ? '#1e3a5f' : 'transparent',
                        border: `1px solid ${isActiveLine ? '#2563eb' : 'transparent'}`,
                      }}>
                        <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'baseline', gap: '0 2px' }}>
                          {/* Score chip — clicking jumps to first move of this line */}
                          <button
                            onClick={() => setPvState({ lineIdx, pvPly: 1, baseFen: moves[ply]?.fen ?? boardFen })}
                            style={{
                              fontSize: 12, fontWeight: 700, fontFamily: 'monospace',
                              minWidth: 44, marginRight: 4, padding: '1px 4px',
                              borderRadius: 4, border: 'none', cursor: 'pointer',
                              background: isActiveLine ? '#2563eb' : '#1f2937',
                              color: scoreColor,
                            }}
                          >{ln.score}</button>
                          {/* Individual move chips */}
                          {ln.pvMoves.map((mv, pvIdx) => {
                            const isActiveMove = isActiveLine && pvState.pvPly === pvIdx + 1
                            return (
                              <button
                                key={pvIdx}
                                onClick={() => setPvState({ lineIdx, pvPly: pvIdx + 1, baseFen: moves[ply]?.fen ?? boardFen })}
                                style={{
                                  padding: '1px 3px', borderRadius: 3,
                                  border: 'none', cursor: 'pointer',
                                  background: isActiveMove ? '#2563eb' : 'transparent',
                                  color: isActiveMove ? '#fff' : '#d1d5db',
                                  fontSize: 12, fontFamily: 'monospace',
                                }}
                              >
                                {mv.isWhite ? `${mv.moveNum}.` : ''}{mv.san}
                              </button>
                            )
                          })}
                        </div>
                      </div>
                    )
                  })}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Move list (mobile: below board) — already inside flex column above */}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Root app
// ---------------------------------------------------------------------------

export default function GameAnalysisApp({ jobId, side }) {
  const isMobile = useIsMobile()
  const [selectedIndex, setSelectedIndex] = useState(null)

  return (
    <div style={{
      display: 'flex',
      flexDirection: isMobile ? 'column' : 'row',
      height: '100vh',
      background: '#0f172a',
      color: '#f9fafb',
      fontFamily: 'system-ui, sans-serif',
      overflow: 'hidden',
    }}>
      <GameList
        jobId={jobId}
        selectedIndex={selectedIndex}
        onSelect={setSelectedIndex}
      />
      <AnalysisPanel
        jobId={jobId}
        selectedIndex={selectedIndex}
        side={side}
      />
    </div>
  )
}
