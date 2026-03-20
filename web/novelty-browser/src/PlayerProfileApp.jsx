import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import Chessground from '@react-chess/chessground'
import { Chess } from 'chess.js'
import RepertoireBoard from './components/RepertoireBoard.jsx'

// ---------------------------------------------------------------------------
// Hooks
// ---------------------------------------------------------------------------

function useIsMobile(bp = 640) {
  const [v, setV] = useState(() => window.innerWidth < bp)
  useEffect(() => {
    const h = () => setV(window.innerWidth < bp)
    window.addEventListener('resize', h)
    return () => window.removeEventListener('resize', h)
  }, [bp])
  return v
}

function computeBoardSize(isMobile) {
  if (isMobile) return Math.min(520, window.innerWidth - 32)
  return Math.max(320, Math.min(560, window.innerHeight - 160))
}

function useBoardSize(isMobile) {
  const [size, setSize] = useState(() => computeBoardSize(isMobile))
  useEffect(() => {
    const h = () => setSize(computeBoardSize(isMobile))
    window.addEventListener('resize', h)
    return () => window.removeEventListener('resize', h)
  }, [isMobile])
  useEffect(() => { setSize(computeBoardSize(isMobile)) }, [isMobile])
  return size
}

// ---------------------------------------------------------------------------
// Chess helpers
// ---------------------------------------------------------------------------

function getLegalDests(fen, playerColor) {
  try {
    const chess = new Chess(fen)
    const color = playerColor === 'white' ? 'w' : 'b'
    const dests = new Map()
    for (const sq of chess.board().flat()) {
      if (!sq || sq.color !== color) continue
      const ms = chess.moves({ square: sq.square, verbose: true })
      if (ms.length > 0) dests.set(sq.square, ms.map(m => m.to))
    }
    return dests
  } catch { return new Map() }
}

function applyUciToFen(fen, uci) {
  try {
    const chess = new Chess(fen)
    const move = chess.move({ from: uci.slice(0, 2), to: uci.slice(2, 4), promotion: uci[4] || 'q' })
    return move ? { fen: chess.fen(), chess, san: move.san } : null
  } catch { return null }
}

function randomColor() { return Math.random() < 0.5 ? 'white' : 'black' }

const STARTING_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
const normFen = fen => fen.split(' ').slice(0, 3).join(' ') + ' -'

function getBookMove(fen, book) {
  if (!book) return null
  const all = book[normFen(fen)] || []
  const moves = all.filter(m => m.games >= 10)
  const pool = moves.length ? moves : (all.length ? all.slice(0, 1) : [])
  if (!pool.length) return null
  const total = pool.reduce((s, m) => s + m.games, 0)
  let r = Math.random() * total
  for (const m of pool) { r -= m.games; if (r <= 0) return m.uci }
  return pool[0].uci
}

// ---------------------------------------------------------------------------
// Opening name lookup
// ---------------------------------------------------------------------------
const OPENING_NAMES = {
  '1.e4 e5 2.Nf3 Nc6 3.Bb5 a6': 'Ruy Lopez, Morphy',
  '1.e4 e5 2.Nf3 Nc6 3.Bb5':    'Ruy Lopez',
  '1.e4 e5 2.Nf3 Nc6 3.Bc4':    'Italian Game',
  '1.e4 e5 2.Nf3 Nc6 3.d4':     'Scotch Game',
  '1.e4 e5 2.Nf3 Nc6':          "King's Knight Opening",
  '1.e4 e5 2.Nc3 Nc6':          'Vienna Game',
  '1.e4 e5 2.f4':               "King's Gambit",
  '1.e4 e5':                    'Open Game',
  '1.e4 c5 2.Nf3 d6 3.d4 cxd4 4.Nxd4 Nf6 5.Nc3 a6': 'Sicilian, Najdorf',
  '1.e4 c5 2.Nf3 d6 3.d4 cxd4 4.Nxd4 Nf6 5.Nc3':    'Sicilian, Open',
  '1.e4 c5 2.Nf3 d6 3.d4 cxd4': 'Sicilian, Open',
  '1.e4 c5 2.Nf3 Nc6 3.d4':     'Sicilian, Classical',
  '1.e4 c5 2.Nf3 e6 3.d4':      'Sicilian, Scheveningen',
  '1.e4 c5 2.Nf3 d6':           'Sicilian Defense',
  '1.e4 c5 2.Nf3 Nc6':          'Sicilian, Classical',
  '1.e4 c5 2.Nf3 e6':           'Sicilian Defense',
  '1.e4 c5 2.Nc3':              'Sicilian, Closed',
  '1.e4 c5 2.c3':               'Sicilian, Alapin',
  '1.e4 c5 2.f4':               'Sicilian, Grand Prix',
  '1.e4 c5 2.Nf3':              'Sicilian Defense',
  '1.e4 c5':                    'Sicilian Defense',
  '1.e4 e6 2.d4 d5 3.Nc3':      'French, Classical',
  '1.e4 e6 2.d4 d5 3.Nd2':      'French, Tarrasch',
  '1.e4 e6 2.d4 d5 3.e5':       'French, Advance',
  '1.e4 e6 2.d4':               'French Defense',
  '1.e4 e6':                    'French Defense',
  '1.e4 c6 2.d4 d5 3.Nc3':      'Caro-Kann, Classical',
  '1.e4 c6 2.d4 d5 3.e5':       'Caro-Kann, Advance',
  '1.e4 c6 2.d4 d5 3.Nd2':      'Caro-Kann, Karpov',
  '1.e4 c6 2.d4':               'Caro-Kann Defense',
  '1.e4 c6':                    'Caro-Kann Defense',
  '1.e4 d5 2.exd5':             'Scandinavian Defense',
  '1.e4 d5':                    'Scandinavian Defense',
  '1.e4 Nf6 2.e5':              "Alekhine's Defense",
  '1.e4 Nf6':                   "Alekhine's Defense",
  '1.e4 g6':                    'Modern Defense',
  '1.e4 d6 2.d4 Nf6':          'Pirc Defense',
  '1.e4 d6':                    'Pirc Defense',
  '1.e4 Nc6':                   'Nimzowitsch Defense',
  '1.e4':                       "King's Pawn",
  '1.d4 Nf6 2.c4 g6 3.Nc3 Bg7 4.e4': "King's Indian, Classical",
  '1.d4 Nf6 2.c4 g6 3.Nc3 Bg7':      "King's Indian Defense",
  '1.d4 Nf6 2.c4 g6':                "King's Indian Defense",
  '1.d4 Nf6 2.c4 e6 3.Nc3 Bb4':      'Nimzo-Indian Defense',
  '1.d4 Nf6 2.c4 e6 3.Nf3 b6':       "Queen's Indian Defense",
  '1.d4 Nf6 2.c4 e6':                "Queen's Indian / Nimzo-Indian",
  '1.d4 Nf6 2.c4 c5 3.d5':           'Benoni Defense',
  '1.d4 Nf6 2.c4 c5':                'Benoni Defense',
  '1.d4 Nf6 2.c4':                   'Indian Defense',
  '1.d4 Nf6':                        'Indian Defense',
  '1.d4 d5 2.c4 e6 3.Nc3 Nf6':       "Queen's Gambit Declined",
  '1.d4 d5 2.c4 dxc4':               "Queen's Gambit Accepted",
  '1.d4 d5 2.c4 c6':                 'Slav Defense',
  '1.d4 d5 2.c4 e6':                 "Queen's Gambit Declined",
  '1.d4 d5 2.c4':                    "Queen's Gambit",
  '1.d4 d5 2.Nf3 Nf6':              'London System',
  '1.d4 d5':                         "Queen's Gambit",
  '1.d4 f5':                         'Dutch Defense',
  '1.d4 g6':                         'Modern Defense',
  '1.d4':                            "Queen's Pawn",
  '1.c4 e5 2.Nc3':                   'English, Reversed Sicilian',
  '1.c4 e5':                         'English Opening',
  '1.c4 c5':                         'English, Symmetrical',
  '1.c4':                            'English Opening',
  '1.Nf3 d5 2.c4':                   'Réti Opening',
  '1.Nf3 d5':                        'Réti Opening',
  '1.Nf3':                           'Réti Opening',
  '1.g3':                            'Benko Opening',
  '1.b3':                            'Nimzowitsch-Larsen Attack',
  '1.f4':                            "Bird's Opening",
  '1.b4':                            'Polish Opening',
}

function openingName(moveSeq) {
  if (!moveSeq) return null
  const parts = moveSeq.split(' ')
  for (let len = parts.length; len >= 1; len--) {
    const key = parts.slice(0, len).join(' ')
    if (OPENING_NAMES[key]) return OPENING_NAMES[key]
  }
  return null
}

// ---------------------------------------------------------------------------
// Small UI components
// ---------------------------------------------------------------------------

function ThinkingDots() {
  const [frame, setFrame] = useState(0)
  useEffect(() => {
    const id = setInterval(() => setFrame(f => (f + 1) % 4), 350)
    return () => clearInterval(id)
  }, [])
  return (
    <span style={{ color: '#60a5fa', fontSize: 14, fontWeight: 700, width: 16, display: 'inline-block' }}>
      {'.'.repeat(frame)}
    </span>
  )
}

function MoveHistory({ moves, style }) {
  const endRef = useRef(null)
  useEffect(() => { endRef.current?.scrollIntoView({ block: 'nearest' }) }, [moves.length])
  const rows = []
  for (let i = 0; i < moves.length; i += 2)
    rows.push({ num: Math.floor(i / 2) + 1, white: moves[i], black: moves[i + 1] })
  return (
    <div style={{ overflowY: 'auto', fontFamily: 'ui-monospace, monospace', fontSize: 13, ...style }}>
      {rows.length === 0
        ? <div style={{ color: '#4b5563', fontSize: 12, textAlign: 'center', padding: '16px 0' }}>Game not started</div>
        : <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <tbody>
              {rows.map(({ num, white, black }) => (
                <tr key={num} style={{ background: num % 2 === 0 ? 'rgba(255,255,255,0.02)' : 'transparent' }}>
                  <td style={{ color: '#4b5563', padding: '2px 6px', width: 28, userSelect: 'none' }}>{num}.</td>
                  <td style={{ padding: '2px 8px', color: white ? '#e5e7eb' : '#4b5563' }}>{white?.san ?? '—'}</td>
                  <td style={{ padding: '2px 8px', color: black ? '#e5e7eb' : '#4b5563' }}>{black?.san ?? ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
      }
      <div ref={endRef} />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Opening fingerprint
// ---------------------------------------------------------------------------
function OpeningFingerprint({ profile }) {
  const [colorTab, setColorTab] = useState('white')
  if (!profile) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        {[1,2,3,4,5].map(i => (
          <div key={i} style={{ background: '#111827', borderRadius: 10, height: 64, opacity: 0.4 + i * 0.1 }} />
        ))}
      </div>
    )
  }
  const style = colorTab === 'white' ? profile.style_white : profile.style_black
  const openings = (style?.top_openings || []).slice(0, 8)
  const maxGames = openings.reduce((m, x) => Math.max(m, x.games ?? 0), 1)
  const barColor = colorTab === 'white' ? '#3b82f6' : '#8b5cf6'

  return (
    <div>
      <div style={{ display: 'flex', gap: 8, marginBottom: 20 }}>
        {[['white', 'As White'], ['black', 'As Black']].map(([val, label]) => (
          <button key={val} onClick={() => setColorTab(val)} style={{
            padding: '7px 18px', borderRadius: 20, fontSize: 13, fontWeight: 600,
            border: `1px solid ${colorTab === val ? '#3b82f6' : '#374151'}`,
            background: colorTab === val ? '#1e3a5f' : 'transparent',
            color: colorTab === val ? '#93c5fd' : '#6b7280',
            cursor: 'pointer', transition: 'all 0.15s',
          }}>
            {label}
          </button>
        ))}
      </div>

      {openings.length === 0
        ? <div style={{ color: '#4b5563', fontSize: 14 }}>No opening data available.</div>
        : <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {openings.map((op, i) => {
              const name = openingName(op.move_sequence) || op.move_sequence
              const isNamed = !!openingName(op.move_sequence)
              const pct = Math.round((op.games / maxGames) * 100)
              const winPct = op.win_rate != null ? Math.round(op.win_rate * 100) : null
              const winColor = winPct == null ? '#6b7280' : winPct >= 60 ? '#4ade80' : winPct >= 50 ? '#facc15' : '#f87171'
              return (
                <div key={i} style={{ background: '#0d1117', borderRadius: 10, border: '1px solid #1f2937', padding: '12px 16px' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8, gap: 12 }}>
                    <div style={{ minWidth: 0 }}>
                      <span style={{ color: '#f3f4f6', fontSize: 14, fontWeight: 600 }}>{name}</span>
                      {isNamed && (
                        <span style={{ color: '#374151', fontSize: 11, fontFamily: 'ui-monospace, monospace', marginLeft: 10 }}>
                          {op.move_sequence}
                        </span>
                      )}
                    </div>
                    <div style={{ display: 'flex', gap: 14, flexShrink: 0, alignItems: 'center' }}>
                      <span style={{ color: '#4b5563', fontSize: 12 }}>{op.games.toLocaleString()} games</span>
                      {winPct != null && (
                        <span style={{ fontSize: 12, fontWeight: 700, color: winColor }}>{winPct}%</span>
                      )}
                    </div>
                  </div>
                  <div style={{ background: '#1f2937', borderRadius: 4, height: 5, overflow: 'hidden' }}>
                    <div style={{ width: `${pct}%`, height: '100%', background: barColor, borderRadius: 4, transition: 'width 0.5s ease' }} />
                  </div>
                </div>
              )
            })}
          </div>
      }
    </div>
  )
}

// ---------------------------------------------------------------------------
// Repertoire section
// ---------------------------------------------------------------------------
function flattenTree(node, map = new Map()) {
  map.set(node.id, node)
  for (const child of node.children) flattenTree(child, map)
  return map
}

function RepertoireSection({ slug }) {
  const [colorTab, setColorTab] = useState('white')
  const [nodeMap,  setNodeMap]  = useState(null)
  const [curId,    setCurId]    = useState(null)
  const [path,     setPath]     = useState([])
  const [loading,  setLoading]  = useState(false)
  const [error,    setError]    = useState(null)

  useEffect(() => {
    setNodeMap(null); setCurId(null); setPath([]); setError(null); setLoading(true)
    fetch(`/api/players/${slug}/repertoire/${colorTab}`)
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(data => {
        if (!data.root) { setError('No repertoire data.'); return }
        const map = flattenTree(data.root)
        setNodeMap(map); setCurId(data.root.id); setPath([data.root.id])
      })
      .catch(() => setError('Failed to load repertoire.'))
      .finally(() => setLoading(false))
  }, [slug, colorTab])

  const goTo = useCallback((id) => {
    if (!nodeMap) return
    const newPath = []; let n = nodeMap.get(id)
    while (n) { newPath.unshift(n.id); n = n.parent_id ? nodeMap.get(n.parent_id) : null }
    setCurId(id); setPath(newPath)
  }, [nodeMap])

  const goBack = useCallback(() => {
    if (path.length <= 1) return; goTo(path[path.length - 2])
  }, [path, goTo])

  useEffect(() => {
    const h = (e) => {
      if (!nodeMap || !curId) return
      const cur = nodeMap.get(curId)
      if ((e.key === 'ArrowRight' || e.key === 'ArrowDown') && cur?.children?.length > 0) goTo(cur.children[0].id)
      if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') goBack()
    }
    window.addEventListener('keydown', h); return () => window.removeEventListener('keydown', h)
  }, [nodeMap, curId, goTo, goBack])

  const node = nodeMap && curId ? nodeMap.get(curId) : null

  return (
    <div>
      <div style={{ display: 'flex', gap: 8, marginBottom: 20 }}>
        {[['white', 'As White'], ['black', 'As Black']].map(([val, label]) => (
          <button key={val} onClick={() => setColorTab(val)} style={{
            padding: '7px 18px', borderRadius: 20, fontSize: 13, fontWeight: 600,
            border: `1px solid ${colorTab === val ? '#3b82f6' : '#374151'}`,
            background: colorTab === val ? '#1e3a5f' : 'transparent',
            color: colorTab === val ? '#93c5fd' : '#6b7280',
            cursor: 'pointer', transition: 'all 0.15s',
          }}>
            {label}
          </button>
        ))}
      </div>
      {loading && <div style={{ color: '#4b5563', fontSize: 14, padding: '32px 0' }}>Loading repertoire…</div>}
      {error   && <div style={{ color: '#6b7280', fontSize: 14 }}>{error}</div>}
      {node    && <RepertoireBoard node={node} nodeMap={nodeMap} orientation={colorTab} onNavigate={goTo} onBack={goBack} canGoBack={path.length > 1} treeStats={null} />}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Stat card
// ---------------------------------------------------------------------------
function StatCard({ label, value, sub, accent }) {
  return (
    <div style={{
      background: '#0d1117', borderRadius: 12, padding: '18px 20px',
      border: '1px solid #1f2937', flex: '1 1 150px',
      borderTop: accent ? `2px solid ${accent}` : '1px solid #1f2937',
    }}>
      <div style={{ color: '#6b7280', fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 8 }}>
        {label}
      </div>
      <div style={{ color: '#f3f4f6', fontSize: 26, fontWeight: 800, lineHeight: 1, letterSpacing: '-0.5px' }}>
        {value}
      </div>
      {sub && <div style={{ color: '#374151', fontSize: 11, marginTop: 6 }}>{sub}</div>}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Contextual blurbs
// ---------------------------------------------------------------------------
function OpeningsBlurb({ displayName, profile }) {
  if (!profile) return null
  const topW = profile.style_white?.top_openings?.[0]
  const topB = profile.style_black?.top_openings?.[0]
  const nameW = topW ? openingName(topW.move_sequence) || topW.move_sequence : null
  const nameB = topB ? openingName(topB.move_sequence) || topB.move_sequence : null
  if (!nameW && !nameB) return null
  return (
    <p style={{ color: '#6b7280', fontSize: 14, lineHeight: 1.7, margin: '0 0 24px', maxWidth: 660 }}>
      {nameW && <>{displayName}'s most-played opening as White is the <strong style={{ color: '#d1d5db' }}>{nameW}</strong>.</>}
      {nameB && <> As Black, the most common choice is the <strong style={{ color: '#d1d5db' }}>{nameB}</strong>.</>}
    </p>
  )
}

function RepertoireBlurb({ displayName, totalGames }) {
  return (
    <p style={{ color: '#6b7280', fontSize: 14, lineHeight: 1.7, margin: '0 0 24px', maxWidth: 660 }}>
      Explore {displayName}'s full opening tree{totalGames ? ` from ${totalGames.toLocaleString()} indexed games` : ''}.
      Click any move to navigate deeper — use ← → arrow keys to step through lines.
    </p>
  )
}

function PracticeBlurb({ displayName }) {
  return (
    <p style={{ color: '#6b7280', fontSize: 14, lineHeight: 1.7, margin: '0 0 24px', maxWidth: 660 }}>
      Play an opening game against {displayName}'s bot. It follows {displayName}'s book moves and falls back to engine play when out of theory.
    </p>
  )
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

const TABS = [
  { id: 'overview',   label: 'Overview' },
  { id: 'openings',   label: 'Openings' },
  { id: 'repertoire', label: 'Repertoire' },
  { id: 'practice',   label: 'Practice' },
]

const SOURCE_LABEL = {
  opening: { text: 'Opening', color: '#60a5fa' },
  engine:  { text: 'Engine',  color: '#9ca3af' },
}

export default function PlayerProfileApp({ slug, displayName, elo, title, loggedIn, description, username, platform, photoPosition = 25 }) {
  const isMobile  = useIsMobile()
  const boardSize = useBoardSize(isMobile)

  const [activeTab, setActiveTab] = useState('overview')

  // Data
  const [whiteBook, setWhiteBook] = useState(null)
  const [blackBook, setBlackBook] = useState(null)
  const [bookError, setBookError] = useState(false)
  const [profile,   setProfile]   = useState(null)

  // Practice state
  const [userColor,    setUserColor]    = useState('white')
  const [colorChoice,  setColorChoice]  = useState('white')
  const [fen,          setFen]          = useState(STARTING_FEN)
  const [lastMove,     setLastMove]     = useState(null)
  const [thinking,     setThinking]     = useState(false)
  const [gameOver,     setGameOver]     = useState(null)
  const [resetKey,     setResetKey]     = useState(0)
  const [moveSource,   setMoveSource]   = useState(null)
  const [moves,        setMoves]        = useState([])
  const thinkingRef = useRef(false)

  const isLoggedIn = loggedIn === 'true'

  useEffect(() => {
    Promise.all([
      fetch(`/api/players/${slug}/book/white`).then(r => r.json()),
      fetch(`/api/players/${slug}/book/black`).then(r => r.json()),
    ]).then(([w, b]) => {
      setWhiteBook(w.positions || {})
      setBlackBook(b.positions || {})
    }).catch(() => setBookError(true))

    fetch(`/api/players/${slug}/profile`)
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        setProfile(data)
        if (data && !data.avatar_url && platform === 'chesscom' && username) {
          fetch(`https://api.chess.com/pub/player/${username.toLowerCase()}`)
            .then(r => r.ok ? r.json() : null)
            .then(u => { if (u?.avatar) setProfile(p => p ? { ...p, avatar_url: u.avatar } : p) })
            .catch(() => {})
        }
      })
      .catch(() => {})
  }, [slug])

  const booksLoaded = whiteBook !== null && blackBook !== null

  useEffect(() => {
    setFen(STARTING_FEN); setLastMove(null); setGameOver(null)
    setMoveSource(null); setMoves([]); setResetKey(k => k + 1)
    thinkingRef.current = false; setThinking(false)
  }, [userColor])

  const triggerBotMove = useCallback(async (currentFen, prevFen) => {
    const botColor = userColor === 'white' ? 'black' : 'white'
    if (thinkingRef.current) return
    thinkingRef.current = true; setThinking(true); setMoveSource(null)

    const applyMove = (uci, source) => {
      const result = applyUciToFen(currentFen, uci)
      if (!result) { if (prevFen != null) setFen(prevFen); return }
      const { fen: newFen, chess: c, san } = result
      setFen(newFen); setLastMove([uci.slice(0, 2), uci.slice(2, 4)]); setMoveSource(source)
      setMoves(prev => [...prev, { san, color: botColor === 'white' ? 'w' : 'b' }])
      if (c.isGameOver())
        setGameOver(c.isCheckmate() ? 'checkmate' : c.isStalemate() ? 'stalemate' : 'draw')
    }

    try {
      const botBook = botColor === 'white' ? whiteBook : blackBook
      const bookMove = getBookMove(currentFen, botBook)
      if (bookMove) { applyMove(bookMove, 'opening'); return }
      const controller = new AbortController()
      const abortTimer = setTimeout(() => controller.abort(), 10_000)
      try {
        const res = await fetch(`/api/players/${slug}/engine-move`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ fen: currentFen }), signal: controller.signal,
        })
        if (!res.ok) { if (prevFen != null) setFen(prevFen); return }
        applyMove((await res.json()).uci, 'engine')
      } finally { clearTimeout(abortTimer) }
    } catch (e) {
      console.error('Bot move error:', e)
      if (prevFen != null) setFen(prevFen)
    } finally {
      thinkingRef.current = false; setThinking(false)
    }
  }, [slug, userColor, whiteBook, blackBook])

  useEffect(() => {
    if (fen !== STARTING_FEN) return
    if (userColor === 'black' && !thinkingRef.current && booksLoaded)
      triggerBotMove(STARTING_FEN, null)
  }, [userColor, resetKey, booksLoaded]) // eslint-disable-line react-hooks/exhaustive-deps

  const handleUserMove = (orig, dest) => {
    if (thinking || gameOver) return
    const c = new Chess(fen)
    if ((c.turn() === 'w' ? 'white' : 'black') !== userColor) return
    const move = c.move({ from: orig, to: dest, promotion: 'q' })
    if (!move) return
    const prevFen = fen, newFen = c.fen()
    setFen(newFen); setLastMove([orig, dest]); setMoveSource(null)
    setMoves(prev => [...prev, { san: move.san, color: userColor === 'white' ? 'w' : 'b' }])
    if (c.isGameOver()) { setGameOver(c.isCheckmate() ? 'checkmate' : c.isStalemate() ? 'stalemate' : 'draw'); return }
    triggerBotMove(newFen, prevFen)
  }

  const handleNewGame = useCallback(() => {
    const newColor = colorChoice === 'random' ? randomColor() : colorChoice
    if (newColor === userColor) {
      setFen(STARTING_FEN); setLastMove(null); setGameOver(null); setMoveSource(null)
      setMoves([]); setResetKey(k => k + 1); thinkingRef.current = false; setThinking(false)
      if (newColor === 'black' && booksLoaded) triggerBotMove(STARTING_FEN, null)
    } else { setUserColor(newColor) }
  }, [colorChoice, userColor, triggerBotMove, booksLoaded])

  const legalDests = useMemo(() => {
    if (thinking || gameOver || !booksLoaded) return new Map()
    return getLegalDests(fen, userColor)
  }, [fen, thinking, gameOver, userColor, booksLoaded])

  const botColor = userColor === 'white' ? 'black' : 'white'
  const cgConfig = {
    fen, orientation: userColor,
    turnColor: thinking ? botColor : userColor,
    lastMove: lastMove ?? undefined,
    movable: {
      free: false,
      color: (thinking || gameOver || !booksLoaded) ? 'none' : userColor,
      dests: legalDests,
      events: { after: handleUserMove },
    },
    draggable: { enabled: !thinking && !gameOver && booksLoaded },
    selectable: { enabled: !thinking && !gameOver && booksLoaded },
    animation: { enabled: true, duration: 200 },
    highlight: { lastMove: true, check: true },
  }

  const finalTurn = (() => { try { return new Chess(fen).turn() } catch { return 'w' } })()
  const gameOverText = gameOver === 'checkmate'
    ? (finalTurn === (userColor === 'white' ? 'w' : 'b') ? 'You were checkmated' : 'You checkmated the bot!')
    : gameOver === 'stalemate' ? 'Stalemate — draw' : 'Draw'

  // Stats
  const totalGames    = profile?.total_games ?? profile?.phase_stats?.total_games
  const totalPositions = (profile?.style_white?.total_positions ?? 0) + (profile?.style_black?.total_positions ?? 0)
  const winRate       = profile?.style_white?.avg_win_rate
  const drawRate      = profile?.style_white?.draw_rate
  const endgameConv   = profile?.phase_stats?.endgame_conversion_rate

  const fmt = (v, d = 0) => v != null && !isNaN(v) ? (v * 100).toFixed(d) + '%' : '—'

  // Avatar initials fallback
  const initials = displayName?.split(' ').map(w => w[0]).join('').slice(0, 2).toUpperCase() || '?'

  // Content padding
  const contentPad = isMobile ? '24px 20px 48px' : '32px 32px 64px'

  return (
    <div style={{ background: '#030712', color: '#f3f4f6', minHeight: '100vh' }}>

      {/* ================================================================== */}
      {/* HERO — full-bleed photo */}
      {/* ================================================================== */}
      {(() => {
        const photoSrc = profile?.photo_url || profile?.avatar_url
        return (
          <div style={{
            position: 'relative',
            height: isMobile ? 300 : 440,
            overflow: 'hidden',
            background: 'linear-gradient(160deg, #0a1628 0%, #0d1f3c 25%, #080f20 60%, #030712 100%)',
          }}>
            {/* Full-bleed photo */}
            {photoSrc && (
              <img
                src={photoSrc}
                alt={displayName}
                style={{
                  position: 'absolute', inset: 0,
                  width: '100%', height: '100%',
                  objectFit: 'cover', objectPosition: `center ${photoPosition}%`,
                }}
              />
            )}

            {/* Gradient overlay — heavier when photo present */}
            <div style={{
              position: 'absolute', inset: 0,
              background: photoSrc
                ? 'linear-gradient(to bottom, rgba(3,7,18,0.15) 0%, rgba(3,7,18,0.4) 40%, rgba(3,7,18,0.82) 72%, #030712 100%)'
                : 'none',
            }} />

            {/* Back nav */}
            <div style={{ position: 'relative', zIndex: 2, padding: isMobile ? '14px 20px' : '18px 32px' }}>
              <a href="/players" style={{
                color: photoSrc ? 'rgba(255,255,255,0.55)' : '#4b5563',
                fontSize: 13, textDecoration: 'none',
              }}>
                ← Featured Players
              </a>
            </div>

            {/* Name + badges pinned to bottom of photo */}
            <div style={{
              position: 'absolute', bottom: 0, left: 0, right: 0, zIndex: 2,
              padding: isMobile ? '0 20px 24px' : '0 32px 28px',
            }}>
              <div style={{ maxWidth: 900, margin: '0 auto' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                  <h1 style={{
                    margin: 0, fontSize: isMobile ? 30 : 48, fontWeight: 800,
                    color: '#fff', letterSpacing: '-1px', lineHeight: 1.05,
                    textShadow: photoSrc ? '0 2px 16px rgba(0,0,0,0.7)' : 'none',
                  }}>
                    {displayName}
                  </h1>
                  {title && (
                    <span style={{
                      fontSize: 12, fontWeight: 700, padding: '3px 9px', borderRadius: 5,
                      background: 'rgba(245,158,11,0.25)', color: '#fcd34d',
                      border: '1px solid rgba(245,158,11,0.45)',
                      backdropFilter: 'blur(6px)',
                    }}>
                      {title}
                    </span>
                  )}
                  {elo && (
                    <span style={{
                      fontSize: 13, fontWeight: 600, padding: '3px 11px', borderRadius: 5,
                      background: 'rgba(0,0,0,0.4)', color: '#d1d5db',
                      border: '1px solid rgba(255,255,255,0.18)',
                      backdropFilter: 'blur(6px)',
                    }}>
                      {elo} Elo
                    </span>
                  )}
                </div>
              </div>
            </div>
          </div>
        )
      })()}

      {/* Description + stat chips below the photo */}
      {(description || profile) && (
        <div style={{ background: '#030712', borderBottom: '1px solid #1a2744' }}>
          <div style={{
            maxWidth: 900, margin: '0 auto',
            padding: isMobile ? '20px 20px 18px' : '24px 32px 20px',
          }}>
            {description && (
              <p style={{
                margin: profile ? '0 0 16px' : '0',
                color: '#9ca3af', fontSize: isMobile ? 13 : 14,
                lineHeight: 1.7, maxWidth: 720,
              }}>
                {description}
              </p>
            )}
            {profile && (
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
                {totalGames != null && (
                  <span style={{
                    fontSize: 13, color: '#6b7280',
                    background: 'rgba(255,255,255,0.05)', padding: '4px 10px', borderRadius: 6,
                    border: '1px solid rgba(255,255,255,0.08)',
                  }}>
                    <strong style={{ color: '#e5e7eb', fontWeight: 700 }}>{totalGames.toLocaleString()}</strong> games
                  </span>
                )}
                {winRate != null && (
                  <span style={{
                    fontSize: 13, color: '#6b7280',
                    background: 'rgba(74,222,128,0.08)', padding: '4px 10px', borderRadius: 6,
                    border: '1px solid rgba(74,222,128,0.15)',
                  }}>
                    <strong style={{ color: '#4ade80', fontWeight: 700 }}>{Math.round(winRate * 100)}%</strong> win rate
                  </span>
                )}
                {endgameConv != null && (
                  <span style={{
                    fontSize: 13, color: '#6b7280',
                    background: 'rgba(96,165,250,0.08)', padding: '4px 10px', borderRadius: 6,
                    border: '1px solid rgba(96,165,250,0.15)',
                  }}>
                    <strong style={{ color: '#60a5fa', fontWeight: 700 }}>{Math.round(endgameConv * 100)}%</strong> endgame conversion
                  </span>
                )}
              </div>
            )}
          </div>
        </div>
      )}

      {/* ================================================================== */}
      {/* STICKY TAB BAR */}
      {/* ================================================================== */}
      <div style={{
        position: 'sticky', top: 0, zIndex: 20,
        background: '#030712',
        borderBottom: '1px solid #1f2937',
        boxShadow: '0 2px 12px rgba(0,0,0,0.4)',
      }}>
        <div style={{
          maxWidth: 900, margin: '0 auto',
          padding: isMobile ? '0 12px' : '0 24px',
          display: 'flex',
        }}>
          {TABS.map(tab => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              style={{
                padding: isMobile ? '14px 14px' : '16px 22px',
                fontSize: isMobile ? 13 : 14,
                fontWeight: activeTab === tab.id ? 600 : 400,
                color: activeTab === tab.id ? '#f3f4f6' : '#6b7280',
                background: 'transparent',
                border: 'none',
                borderBottom: `2px solid ${activeTab === tab.id ? '#3b82f6' : 'transparent'}`,
                cursor: 'pointer',
                whiteSpace: 'nowrap',
                transition: 'color 0.15s',
                marginBottom: -1,
              }}
            >
              {tab.label}
            </button>
          ))}
        </div>
      </div>

      {/* ================================================================== */}
      {/* TAB CONTENT */}
      {/* ================================================================== */}
      <div style={{ maxWidth: 900, margin: '0 auto', padding: contentPad }}>

        {/* Overview */}
        {activeTab === 'overview' && (
          <div>
            {profile && totalGames != null && (
              <p style={{ color: '#6b7280', fontSize: 14, lineHeight: 1.7, margin: '0 0 24px', maxWidth: 660 }}>
                {displayName} has{' '}
                <strong style={{ color: '#d1d5db' }}>{totalGames.toLocaleString()} games</strong> indexed
                {winRate != null && <>, with a <strong style={{ color: '#4ade80' }}>{Math.round(winRate * 100)}%</strong> win rate as White</>}
                {drawRate != null && <> and <strong style={{ color: '#d1d5db' }}>{Math.round(drawRate * 100)}%</strong> draw rate</>}
                {endgameConv != null && <>. Endgames convert at <strong style={{ color: '#60a5fa' }}>{Math.round(endgameConv * 100)}%</strong></>}.
              </p>
            )}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(155px, 1fr))', gap: 12 }}>
              <StatCard label="Win Rate" value={fmt(winRate)} sub="as White" accent="#4ade80" />
              <StatCard label="Draw Rate" value={fmt(drawRate)} sub="as White" accent="#facc15" />
              <StatCard label="Games Indexed" value={totalGames != null ? totalGames.toLocaleString() : '—'} sub="in opening cache" accent="#3b82f6" />
              <StatCard label="Opening Depth" value={totalPositions > 0 ? totalPositions.toLocaleString() : '—'} sub="positions analysed" accent="#8b5cf6" />
              <StatCard label="Endgame Conv." value={fmt(endgameConv)} sub="win % after endgame" accent="#f97316" />
            </div>
          </div>
        )}

        {/* Openings */}
        {activeTab === 'openings' && (
          <div>
            <OpeningsBlurb displayName={displayName} profile={profile} />
            <OpeningFingerprint profile={profile} />
          </div>
        )}

        {/* Repertoire */}
        {activeTab === 'repertoire' && (
          <div>
            <RepertoireBlurb displayName={displayName} totalGames={totalGames} />
            <RepertoireSection slug={slug} />
          </div>
        )}

        {/* Practice */}
        {activeTab === 'practice' && (
          <div>
            <PracticeBlurb displayName={displayName} />
            <div style={isMobile ? {} : { display: 'flex', gap: 28, alignItems: 'flex-start' }}>

              {/* Board */}
              <div style={{ flexShrink: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 12 }}>
                <div style={{ width: boardSize, height: boardSize, position: 'relative' }}>
                  <Chessground key={resetKey} width={boardSize} height={boardSize} config={cgConfig} />
                  {/* Game over overlay */}
                  {gameOver && (
                    <div style={{
                      position: 'absolute', inset: 0, zIndex: 20,
                      background: 'rgba(3,7,18,0.82)',
                      display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 14,
                    }}>
                      <div style={{ fontWeight: 700, fontSize: 17, color: '#f3f4f6', textAlign: 'center', padding: '0 16px' }}>
                        {gameOverText}
                      </div>
                      <button onClick={handleNewGame} style={{
                        background: '#1d4ed8', color: '#fff', border: 'none', borderRadius: 6,
                        padding: '8px 22px', fontSize: 13, fontWeight: 600, cursor: 'pointer',
                      }}>
                        Play Again
                      </button>
                    </div>
                  )}
                  {/* Loading overlay */}
                  {!booksLoaded && !bookError && (
                    <div style={{
                      position: 'absolute', inset: 0, zIndex: 20,
                      background: 'rgba(3,7,18,0.78)',
                      display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 8,
                    }}>
                      <ThinkingDots />
                      <div style={{ color: '#9ca3af', fontSize: 13 }}>Loading opening book…</div>
                    </div>
                  )}
                </div>
                {!isLoggedIn && (
                  <div style={{
                    width: boardSize, padding: '12px 16px',
                    background: '#0a1628', borderRadius: 8, border: '1px solid #1a2744',
                    fontSize: 13, color: '#9ca3af', textAlign: 'center',
                  }}>
                    Want to analyse <strong style={{ color: '#e5e7eb' }}>your own</strong> openings the same way?{' '}
                    <a href="/login" style={{ color: '#60a5fa', fontWeight: 600 }}>Sign up free →</a>
                  </div>
                )}
              </div>

              {/* Side panel */}
              <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 12, marginTop: isMobile ? 16 : 0 }}>
                {/* Color picker */}
                <div style={{ background: '#0d1117', borderRadius: 10, padding: '16px', border: '1px solid #1f2937' }}>
                  <div style={{ color: '#6b7280', fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: 10 }}>
                    You play as
                  </div>
                  <div style={{ display: 'flex', gap: 6, marginBottom: 10 }}>
                    {[['white', 'White'], ['random', 'Random'], ['black', 'Black']].map(([val, label]) => (
                      <button key={val} onClick={() => setColorChoice(val)} style={{
                        flex: 1, padding: '7px 0', borderRadius: 6,
                        border: `1px solid ${colorChoice === val ? '#3b82f6' : '#374151'}`,
                        background: colorChoice === val ? '#1e3a5f' : 'transparent',
                        color: colorChoice === val ? '#93c5fd' : '#6b7280',
                        fontSize: 12, fontWeight: 600, cursor: 'pointer', transition: 'all 0.15s',
                      }}>
                        {label}
                      </button>
                    ))}
                  </div>
                  <button onClick={handleNewGame} disabled={thinking || !booksLoaded} style={{
                    width: '100%', background: '#1d4ed8', color: '#fff',
                    border: 'none', borderRadius: 7, padding: '9px 0',
                    fontSize: 13, fontWeight: 600,
                    cursor: (thinking || !booksLoaded) ? 'default' : 'pointer',
                    opacity: (thinking || !booksLoaded) ? 0.5 : 1, transition: 'opacity 0.15s',
                  }}>
                    New Game
                  </button>
                </div>

                {/* Status */}
                <div style={{ height: 28, display: 'flex', alignItems: 'center', gap: 6 }}>
                  {!booksLoaded && !bookError ? (
                    <><ThinkingDots /><span style={{ color: '#9ca3af', fontSize: 12 }}>Loading opening book…</span></>
                  ) : thinking ? (
                    <><ThinkingDots /><span style={{ color: '#9ca3af', fontSize: 12 }}>Thinking…</span></>
                  ) : moveSource && SOURCE_LABEL[moveSource] ? (
                    <><span style={{ fontSize: 12, color: '#4b5563' }}>Bot played:</span>
                      <span style={{ fontSize: 12, fontWeight: 700, color: SOURCE_LABEL[moveSource].color }}>{SOURCE_LABEL[moveSource].text}</span></>
                  ) : null}
                </div>

                {/* Move history */}
                <div style={{ background: '#0d1117', borderRadius: 10, border: '1px solid #1f2937', overflow: 'hidden' }}>
                  <div style={{
                    color: '#4b5563', fontSize: 11, textTransform: 'uppercase',
                    letterSpacing: '0.06em', padding: '10px 14px 8px', borderBottom: '1px solid #1f2937',
                  }}>
                    Move History
                  </div>
                  <MoveHistory moves={moves} style={{ maxHeight: 280, padding: '6px 8px 12px' }} />
                </div>
              </div>
            </div>
          </div>
        )}

      </div>
    </div>
  )
}
