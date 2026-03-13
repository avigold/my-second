import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import Chessground from '@react-chess/chessground'
import { Chess } from 'chess.js'

// ---------------------------------------------------------------------------
// Helpers
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

const PANEL_W = 280   // right-panel width on desktop (px)
const HEADER_H = 45   // approx header height (px)

function computeBoardSize(isMobile) {
  if (isMobile) {
    return Math.min(520, window.innerWidth - 16)
  }
  const byWidth  = window.innerWidth  - PANEL_W - 72   // 72 = left+right padding
  const byHeight = window.innerHeight - HEADER_H - 64  // 64 = top+bottom padding
  return Math.max(320, Math.min(800, Math.min(byWidth, byHeight)))
}

function useBoardSize(isMobile) {
  const [size, setSize] = useState(() => computeBoardSize(isMobile))
  useEffect(() => {
    const h = () => setSize(computeBoardSize(isMobile))
    window.addEventListener('resize', h)
    return () => window.removeEventListener('resize', h)
  }, [isMobile])
  // Recompute when mobile breakpoint changes too
  useEffect(() => { setSize(computeBoardSize(isMobile)) }, [isMobile])
  return size
}

function getLegalDests(fen, playerColor) {
  try {
    const chess = new Chess(fen)
    const color = playerColor === 'white' ? 'w' : 'b'
    const dests  = new Map()
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
    const from  = uci.slice(0, 2)
    const to    = uci.slice(2, 4)
    const promo = uci[4] || undefined
    const move  = chess.move({ from, to, promotion: promo || 'q' })
    return move ? { fen: chess.fen(), chess, san: move.san } : null
  } catch { return null }
}

function randomColor() { return Math.random() < 0.5 ? 'white' : 'black' }

const STARTING_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'

const SOURCE_LABEL = {
  opening: { text: 'Opening', color: '#60a5fa' },
  habit:   { text: 'Habit!',  color: '#f87171' },
  engine:  { text: 'Engine',  color: '#9ca3af' },
}

// ---------------------------------------------------------------------------
// Move history component
// ---------------------------------------------------------------------------

function MoveHistory({ moves, style }) {
  const endRef = useRef(null)
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'nearest' })
  }, [moves.length])

  // Group into pairs: [{moveNum, white, black}]
  const rows = []
  for (let i = 0; i < moves.length; i += 2) {
    rows.push({ num: Math.floor(i / 2) + 1, white: moves[i], black: moves[i + 1] })
  }

  return (
    <div style={{
      overflowY: 'auto',
      fontFamily: 'ui-monospace, "Cascadia Code", monospace',
      fontSize: 13,
      ...style,
    }}>
      {rows.length === 0 ? (
        <div style={{ color: '#4b5563', fontSize: 12, textAlign: 'center', padding: '16px 0' }}>
          Game not started
        </div>
      ) : (
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <tbody>
            {rows.map(({ num, white, black }) => (
              <tr key={num} style={{
                background: num % 2 === 0 ? 'rgba(255,255,255,0.02)' : 'transparent',
              }}>
                <td style={{ color: '#4b5563', padding: '2px 6px', width: 28, userSelect: 'none' }}>
                  {num}.
                </td>
                <td style={{
                  padding: '2px 8px',
                  color: white ? '#e5e7eb' : '#4b5563',
                  fontWeight: !black ? 600 : 400,
                }}>
                  {white?.san ?? '—'}
                </td>
                <td style={{
                  padding: '2px 8px',
                  color: black ? '#e5e7eb' : '#4b5563',
                  fontWeight: black && !moves[moves.length - 1]?.san !== black?.san ? 400 : 400,
                }}>
                  {black?.san ?? ''}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div ref={endRef} />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function BotPracticeApp({ botId }) {
  const isMobile  = useIsMobile()
  const boardSize = useBoardSize(isMobile)

  const [bot, setBot]               = useState(null)
  const [error, setError]           = useState(null)
  const [userColor, setUserColor]   = useState('white')
  const [colorChoice, setColorChoice] = useState('white')
  const [fen, setFen]               = useState(STARTING_FEN)
  const [lastMove, setLastMove]     = useState(null)
  const [thinking, setThinking]     = useState(false)
  const [gameOver, setGameOver]     = useState(null)
  const [resetKey, setResetKey]     = useState(0)
  const [moveSource, setMoveSource] = useState(null)
  const [moves, setMoves]           = useState([])   // [{san, color: 'w'|'b'}]
  const thinkingRef = useRef(false)

  useEffect(() => {
    fetch(`/api/bots/${botId}`)
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json() })
      .then(setBot)
      .catch(e => setError(e.message))
  }, [botId])

  // Reset board whenever userColor changes
  useEffect(() => {
    setFen(STARTING_FEN)
    setLastMove(null)
    setGameOver(null)
    setMoveSource(null)
    setMoves([])
    setResetKey(k => k + 1)
    thinkingRef.current = false
    setThinking(false)
  }, [userColor])

  // ---------------------------------------------------------------------------
  // Bot move
  // ---------------------------------------------------------------------------
  const triggerBotMove = useCallback(async (currentFen, prevFen) => {
    const botColor = userColor === 'white' ? 'black' : 'white'
    if (thinkingRef.current) return
    thinkingRef.current = true
    setThinking(true)
    setMoveSource(null)

    const controller = new AbortController()
    const abortTimer = setTimeout(() => controller.abort(), 10_000)

    try {
      const res = await fetch(`/api/bots/${botId}/move`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ fen: currentFen, color: botColor }),
        signal: controller.signal,
      })
      if (!res.ok) {
        if (prevFen != null) setFen(prevFen)
        return
      }
      const data   = await res.json()
      const result = applyUciToFen(currentFen, data.uci)
      if (!result) {
        if (prevFen != null) setFen(prevFen)
        return
      }
      const { fen: newFen, chess: c, san } = result
      setFen(newFen)
      setLastMove([data.uci.slice(0, 2), data.uci.slice(2, 4)])
      setMoveSource(data.source)
      setMoves(prev => [...prev, { san, color: botColor === 'white' ? 'w' : 'b' }])
      if (c.isGameOver()) {
        setGameOver(
          c.isCheckmate() ? 'checkmate'
          : c.isStalemate() ? 'stalemate'
          : 'draw'
        )
      }
    } catch (e) {
      console.error('Bot move error:', e)
      if (prevFen != null) setFen(prevFen)
    } finally {
      clearTimeout(abortTimer)
      thinkingRef.current = false
      setThinking(false)
    }
  }, [botId, userColor])

  // Bot plays first when user chose black
  useEffect(() => {
    if (fen !== STARTING_FEN) return
    if (userColor === 'black' && !thinkingRef.current) {
      triggerBotMove(STARTING_FEN, null)
    }
  }, [userColor, resetKey])  // eslint-disable-line react-hooks/exhaustive-deps

  // ---------------------------------------------------------------------------
  // User move handler
  // ---------------------------------------------------------------------------
  const handleUserMove = (orig, dest) => {
    if (thinking || gameOver) return
    const c = new Chess(fen)
    if ((c.turn() === 'w' ? 'white' : 'black') !== userColor) return

    const move = c.move({ from: orig, to: dest, promotion: 'q' })
    if (!move) return

    const prevFen = fen
    const newFen  = c.fen()
    setFen(newFen)
    setLastMove([orig, dest])
    setMoveSource(null)
    setMoves(prev => [...prev, { san: move.san, color: userColor === 'white' ? 'w' : 'b' }])

    if (c.isGameOver()) {
      setGameOver(
        c.isCheckmate() ? 'checkmate'
        : c.isStalemate() ? 'stalemate'
        : 'draw'
      )
      return
    }
    triggerBotMove(newFen, prevFen)
  }

  // ---------------------------------------------------------------------------
  // New game
  // ---------------------------------------------------------------------------
  const handleNewGame = useCallback(() => {
    const newColor = colorChoice === 'random' ? randomColor() : colorChoice
    if (newColor === userColor) {
      setFen(STARTING_FEN)
      setLastMove(null)
      setGameOver(null)
      setMoveSource(null)
      setMoves([])
      setResetKey(k => k + 1)
      thinkingRef.current = false
      setThinking(false)
      if (newColor === 'black') triggerBotMove(STARTING_FEN, null)
    } else {
      setUserColor(newColor)
    }
  }, [colorChoice, userColor, triggerBotMove])

  // ---------------------------------------------------------------------------
  // Chessground config
  // ---------------------------------------------------------------------------
  const legalDests = useMemo(() => {
    if (thinking || gameOver) return new Map()
    return getLegalDests(fen, userColor)
  }, [fen, thinking, gameOver, userColor])

  const botColor = userColor === 'white' ? 'black' : 'white'
  const cgConfig = {
    fen,
    orientation: userColor,
    turnColor: thinking ? botColor : userColor,
    lastMove: lastMove ?? undefined,
    movable: {
      free: false,
      color: (thinking || gameOver) ? 'none' : userColor,
      dests: legalDests,
      events: { after: handleUserMove },
    },
    draggable: { enabled: !thinking && !gameOver },
    selectable: { enabled: !thinking && !gameOver },
    animation: { enabled: true, duration: 200 },
    highlight: { lastMove: true, check: true },
  }

  // ---------------------------------------------------------------------------
  // Render helpers
  // ---------------------------------------------------------------------------

  const finalTurn = (() => { try { return new Chess(fen).turn() } catch { return 'w' } })()

  const gameOverText = gameOver === 'checkmate'
    ? (finalTurn === (userColor === 'white' ? 'w' : 'b') ? 'You were checkmated' : 'You checkmated the bot!')
    : gameOver === 'stalemate' ? 'Stalemate — draw'
    : 'Draw'

  // Rendered as an absolute overlay on the board so it never gets clipped
  const gameOverOverlay = gameOver && (
    <div style={{
      position: 'absolute', inset: 0, zIndex: 20,
      background: 'rgba(3,7,18,0.80)',
      display: 'flex', flexDirection: 'column',
      alignItems: 'center', justifyContent: 'center',
      gap: 14,
    }}>
      <div style={{ fontWeight: 700, fontSize: 17, color: '#f3f4f6', textAlign: 'center', padding: '0 16px' }}>
        {gameOverText}
      </div>
      <button
        onClick={handleNewGame}
        style={{
          background: '#1d4ed8', color: '#fff',
          border: 'none', borderRadius: 6,
          padding: '8px 22px', fontSize: 13, fontWeight: 600, cursor: 'pointer',
        }}
      >
        Play Again
      </button>
    </div>
  )

  // ---------------------------------------------------------------------------
  // Error / loading
  // ---------------------------------------------------------------------------
  if (error) return <div style={{ padding: 32, color: '#f87171' }}>Failed to load bot: {error}</div>
  if (!bot)  return <div style={{ padding: 32, color: '#9ca3af' }}>Loading…</div>

  // ---------------------------------------------------------------------------
  // Mobile layout
  // ---------------------------------------------------------------------------
  if (isMobile) {
    return (
      <div style={{ background: '#030712', color: '#f3f4f6', minHeight: '100vh' }}>
        {/* Header */}
        <div style={{
          padding: '10px 16px', borderBottom: '1px solid #1f2937',
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          background: '#030712',
        }}>
          <span style={{ fontWeight: 600, fontSize: 14 }}>
            Practice vs {bot.opponent_username}
          </span>
          <a href="/bots" style={{ color: '#6b7280', fontSize: 12, textDecoration: 'none' }}>
            ← Bots
          </a>
        </div>

        <div style={{ padding: '12px 8px', display: 'flex', flexDirection: 'column', gap: 12 }}>
          {/* Mobile info bar */}
          <MobileInfoBar
            bot={bot}
            colorChoice={colorChoice}
            setColorChoice={setColorChoice}
            onNewGame={handleNewGame}
            thinking={thinking}
            moveSource={moveSource}
          />

          {/* Board */}
          <div style={{ display: 'flex', justifyContent: 'center' }}>
            <div style={{ width: boardSize, height: boardSize, position: 'relative' }}>
              <Chessground key={resetKey} width={boardSize} height={boardSize} config={cgConfig} />
              {gameOverOverlay}
            </div>
          </div>

          {/* Move history (mobile) */}
          <div style={{
            background: '#111827', borderRadius: 8,
            padding: '10px 4px', maxHeight: 200, overflow: 'hidden',
            display: 'flex', flexDirection: 'column',
          }}>
            <div style={{
              color: '#4b5563', fontSize: 11, textTransform: 'uppercase',
              letterSpacing: '0.05em', paddingLeft: 8, marginBottom: 4,
            }}>
              Move History
            </div>
            <MoveHistory moves={moves} style={{ flex: 1, overflowY: 'auto' }} />
          </div>
        </div>
      </div>
    )
  }

  // ---------------------------------------------------------------------------
  // Desktop layout
  // ---------------------------------------------------------------------------
  return (
    <div style={{
      display: 'flex', flexDirection: 'column',
      height: '100vh', overflow: 'hidden',
      background: '#030712', color: '#f3f4f6',
    }}>
      {/* Header */}
      <div style={{
        flexShrink: 0,
        padding: '10px 24px', borderBottom: '1px solid #1f2937',
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        background: '#030712', zIndex: 10,
        height: HEADER_H,
        boxSizing: 'border-box',
      }}>
        <span style={{ fontWeight: 600, fontSize: 14 }}>
          Practice vs {bot.opponent_username}
          {bot.opponent_elo ? (
            <span style={{ color: '#6b7280', fontWeight: 400, marginLeft: 8 }}>
              {bot.opponent_elo} Elo
            </span>
          ) : null}
        </span>
        <a href="/bots" style={{ color: '#6b7280', fontSize: 12, textDecoration: 'none' }}>
          ← Bots
        </a>
      </div>

      {/* Body */}
      <div style={{
        flex: 1, overflow: 'hidden',
        display: 'flex', flexDirection: 'row', gap: 0,
      }}>
        {/* Board area — centered */}
        <div style={{
          flex: 1, overflow: 'hidden',
          display: 'flex', flexDirection: 'column',
          alignItems: 'center', justifyContent: 'center',
          padding: '24px 24px 24px 32px',
          gap: 10,
        }}>
          <div style={{ width: boardSize, height: boardSize, position: 'relative' }}>
            <Chessground key={resetKey} width={boardSize} height={boardSize} config={cgConfig} />
            {gameOverOverlay}
          </div>
        </div>

        {/* Right panel */}
        <div style={{
          width: PANEL_W, flexShrink: 0,
          borderLeft: '1px solid #1f2937',
          display: 'flex', flexDirection: 'column',
          overflow: 'hidden',
        }}>
          {/* Bot info */}
          <div style={{ padding: '16px 16px 0' }}>
            <div style={{
              background: '#111827', borderRadius: 8, padding: '12px 14px',
              marginBottom: 12,
            }}>
              <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 3 }}>
                {bot.opponent_username}
              </div>
              <div style={{ color: '#6b7280', fontSize: 12 }}>
                {bot.opponent_platform}
                {bot.opponent_elo ? ` · ${bot.opponent_elo} Elo` : ''}
              </div>
              <div style={{ color: '#6b7280', fontSize: 12, marginTop: 2 }}>
                {bot.speeds}
              </div>
            </div>

            {/* Color picker */}
            <div style={{ marginBottom: 10 }}>
              <div style={{
                color: '#9ca3af', fontSize: 11, textTransform: 'uppercase',
                letterSpacing: '0.05em', marginBottom: 6,
              }}>
                You play as
              </div>
              <div style={{ display: 'flex', gap: 6 }}>
                {[['white', 'White'], ['random', 'Random'], ['black', 'Black']].map(([val, label]) => (
                  <button
                    key={val}
                    onClick={() => setColorChoice(val)}
                    style={{
                      flex: 1, padding: '5px 0', borderRadius: 5,
                      border: `1px solid ${colorChoice === val ? '#3b82f6' : '#374151'}`,
                      background: colorChoice === val ? '#1e3a5f' : '#1f2937',
                      color: colorChoice === val ? '#93c5fd' : '#9ca3af',
                      fontSize: 11, fontWeight: 600, cursor: 'pointer',
                    }}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>

            {/* New game button */}
            <button
              onClick={handleNewGame}
              disabled={thinking}
              style={{
                width: '100%',
                background: '#1d4ed8', color: '#fff',
                border: 'none', borderRadius: 7,
                padding: '8px 0', fontSize: 13, fontWeight: 600,
                cursor: thinking ? 'default' : 'pointer',
                opacity: thinking ? 0.5 : 1,
                marginBottom: 10,
              }}
            >
              New Game
            </button>

            {/* Status indicator */}
            <div style={{ height: 28, marginBottom: 12, display: 'flex', alignItems: 'center', gap: 6 }}>
              {thinking ? (
                <>
                  <ThinkingDots />
                  <span style={{ color: '#9ca3af', fontSize: 12 }}>Thinking…</span>
                </>
              ) : moveSource && SOURCE_LABEL[moveSource] ? (
                <>
                  <span style={{ fontSize: 12, color: '#6b7280' }}>Bot played:</span>
                  <span style={{ fontSize: 12, fontWeight: 700, color: SOURCE_LABEL[moveSource].color }}>
                    {SOURCE_LABEL[moveSource].text}
                  </span>
                </>
              ) : null}
            </div>

            {/* Move history label */}
            <div style={{
              color: '#4b5563', fontSize: 11, textTransform: 'uppercase',
              letterSpacing: '0.05em', borderTop: '1px solid #1f2937',
              paddingTop: 10, marginBottom: 0,
            }}>
              Move History
            </div>
          </div>

          {/* Scrollable move history */}
          <MoveHistory
            moves={moves}
            style={{
              flex: 1,
              overflowY: 'auto',
              padding: '6px 8px 16px',
            }}
          />
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Mobile info bar
// ---------------------------------------------------------------------------
function MobileInfoBar({ bot, colorChoice, setColorChoice, onNewGame, thinking, moveSource }) {
  return (
    <div style={{
      display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center',
      background: '#111827', borderRadius: 8, padding: '10px 12px',
    }}>
      <div style={{ fontWeight: 700, fontSize: 14 }}>
        {bot.opponent_username}
        {bot.opponent_elo ? (
          <span style={{ color: '#6b7280', fontWeight: 400, fontSize: 12, marginLeft: 6 }}>
            {bot.opponent_elo} Elo
          </span>
        ) : null}
      </div>
      <div style={{ display: 'flex', gap: 5, marginLeft: 'auto', alignItems: 'center' }}>
        {[['white', 'W'], ['random', 'R'], ['black', 'B']].map(([val, label]) => (
          <button
            key={val}
            onClick={() => setColorChoice(val)}
            style={{
              padding: '4px 10px', borderRadius: 5,
              border: `1px solid ${colorChoice === val ? '#3b82f6' : '#374151'}`,
              background: colorChoice === val ? '#1e3a5f' : '#1f2937',
              color: colorChoice === val ? '#93c5fd' : '#9ca3af',
              fontSize: 11, fontWeight: 600, cursor: 'pointer',
            }}
          >
            {label}
          </button>
        ))}
        <button
          onClick={onNewGame}
          disabled={thinking}
          style={{
            background: '#1d4ed8', color: '#fff',
            border: 'none', borderRadius: 5,
            padding: '4px 12px', fontSize: 11, fontWeight: 600,
            cursor: thinking ? 'default' : 'pointer', opacity: thinking ? 0.5 : 1,
          }}
        >
          New Game
        </button>
      </div>
      {thinking ? (
        <div style={{ width: '100%', display: 'flex', gap: 6, alignItems: 'center' }}>
          <ThinkingDots />
          <span style={{ color: '#9ca3af', fontSize: 11 }}>Thinking…</span>
        </div>
      ) : moveSource && SOURCE_LABEL[moveSource] ? (
        <div style={{ fontSize: 11, color: SOURCE_LABEL[moveSource].color, fontWeight: 600 }}>
          Bot: {SOURCE_LABEL[moveSource].text}
        </div>
      ) : null}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Thinking dots animation
// ---------------------------------------------------------------------------
function ThinkingDots() {
  const [frame, setFrame] = useState(0)
  useEffect(() => {
    const id = setInterval(() => setFrame(f => (f + 1) % 4), 350)
    return () => clearInterval(id)
  }, [])
  const dots = '.'.repeat(frame)
  return (
    <span style={{ color: '#60a5fa', fontSize: 14, fontWeight: 700, width: 16, display: 'inline-block' }}>
      {dots}
    </span>
  )
}
