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

function getLegalDests(chess) {
  const dests = new Map()
  const color = chess.turn() === 'w' ? 'w' : 'b'
  for (const sq of chess.board().flat()) {
    if (!sq || sq.color !== color) continue
    const moves = chess.moves({ square: sq.square, verbose: true })
    if (moves.length > 0) dests.set(sq.square, moves.map(m => m.to))
  }
  return dests
}

function applyUci(chess, uci) {
  const from = uci.slice(0, 2)
  const to   = uci.slice(2, 4)
  const promo = uci[4] || undefined
  return chess.move({ from, to, promotion: promo || 'q' })
}

function randomColor() {
  return Math.random() < 0.5 ? 'white' : 'black'
}

const STARTING_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function BotPracticeApp({ botId }) {
  const isMobile = useIsMobile()
  const boardSize = isMobile
    ? Math.min(480, window.innerWidth - 24)
    : 480

  const [bot, setBot]               = useState(null)
  const [error, setError]           = useState(null)
  const [userColor, setUserColor]   = useState('white')
  const [colorChoice, setColorChoice] = useState('white')  // 'white'|'black'|'random'
  const [chess]                     = useState(() => new Chess())
  const [fen, setFen]               = useState(STARTING_FEN)
  const [lastMove, setLastMove]     = useState(null)
  const [thinking, setThinking]     = useState(false)
  const [gameOver, setGameOver]     = useState(null)   // null | 'checkmate' | 'draw' | 'stalemate'
  const [resetKey, setResetKey]     = useState(0)
  const [moveSource, setMoveSource] = useState(null)   // last bot move source tag
  const thinkingRef = useRef(false)   // prevent double-firing

  // Fetch bot metadata once on mount.
  useEffect(() => {
    fetch(`/api/bots/${botId}`)
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json() })
      .then(data => setBot(data))
      .catch(err => setError(err.message))
  }, [botId])

  // Start a new game whenever userColor changes.
  useEffect(() => {
    chess.reset()
    setFen(STARTING_FEN)
    setLastMove(null)
    setGameOver(null)
    setMoveSource(null)
    setResetKey(k => k + 1)
    thinkingRef.current = false
    setThinking(false)
  }, [userColor])

  // If the bot moves first (user chose black), trigger the bot's opening move.
  useEffect(() => {
    if (chess.fen() !== STARTING_FEN) return
    if (userColor === 'black' && !thinking) {
      triggerBotMove()
    }
  }, [userColor, resetKey])   // eslint-disable-line react-hooks/exhaustive-deps

  const triggerBotMove = useCallback(async (currentFen) => {
    const fenToSend = currentFen || chess.fen()
    const botColor = userColor === 'white' ? 'black' : 'white'
    if (thinkingRef.current) return
    thinkingRef.current = true
    setThinking(true)
    setMoveSource(null)

    try {
      const res = await fetch(`/api/bots/${botId}/move`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ fen: fenToSend, color: botColor }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        console.error('Bot move error:', err)
        return
      }
      const data = await res.json()
      const move = applyUci(chess, data.uci)
      if (!move) return
      const newFen = chess.fen()
      setFen(newFen)
      setLastMove([data.uci.slice(0, 2), data.uci.slice(2, 4)])
      setMoveSource(data.source)
      if (chess.isGameOver()) {
        setGameOver(
          chess.isCheckmate() ? 'checkmate'
          : chess.isStalemate() ? 'stalemate'
          : 'draw'
        )
      }
    } catch (e) {
      console.error('Bot move fetch error:', e)
    } finally {
      thinkingRef.current = false
      setThinking(false)
    }
  }, [botId, userColor, chess])

  const handleUserMove = useCallback((orig, dest) => {
    if (thinking) return
    if (gameOver) return
    // Validate it's the user's turn.
    const turnColor = chess.turn() === 'w' ? 'white' : 'black'
    if (turnColor !== userColor) return

    const move = chess.move({ from: orig, to: dest, promotion: 'q' })
    if (!move) return
    const newFen = chess.fen()
    setFen(newFen)
    setLastMove([orig, dest])
    setMoveSource(null)

    if (chess.isGameOver()) {
      setGameOver(
        chess.isCheckmate() ? 'checkmate'
        : chess.isStalemate() ? 'stalemate'
        : 'draw'
      )
      return
    }
    // Trigger bot response.
    triggerBotMove(newFen)
  }, [chess, thinking, gameOver, userColor, triggerBotMove])

  const handleNewGame = useCallback(() => {
    const newColor = colorChoice === 'random' ? randomColor() : colorChoice
    if (newColor === userColor) {
      // Force re-trigger by incrementing resetKey.
      chess.reset()
      setFen(STARTING_FEN)
      setLastMove(null)
      setGameOver(null)
      setMoveSource(null)
      setResetKey(k => k + 1)
      thinkingRef.current = false
      setThinking(false)
      if (newColor === 'black') triggerBotMove(STARTING_FEN)
    } else {
      setUserColor(newColor)
    }
  }, [colorChoice, userColor, chess, triggerBotMove])

  const legalDests = useMemo(() => {
    if (thinking || gameOver) return new Map()
    const turnColor = chess.turn() === 'w' ? 'white' : 'black'
    if (turnColor !== userColor) return new Map()
    return getLegalDests(chess)
  }, [fen, thinking, gameOver, userColor])   // eslint-disable-line react-hooks/exhaustive-deps

  const cgConfig = {
    fen,
    orientation: userColor,
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

  if (error) return (
    <div style={{ padding: 32, color: '#f87171' }}>Failed to load bot: {error}</div>
  )
  if (!bot) return (
    <div style={{ padding: 32, color: '#9ca3af' }}>Loading…</div>
  )

  const botColor = userColor === 'white' ? 'black' : 'white'

  const sourceLabel = {
    opening: { text: 'Opening', color: '#60a5fa' },
    habit:   { text: 'Habit!',  color: '#f87171' },
    engine:  { text: 'Engine',  color: '#9ca3af' },
  }

  return (
    <div style={{
      display: 'flex',
      height: '100vh',
      overflow: 'hidden',
      background: '#030712',
      color: '#f3f4f6',
    }}>

      {/* Header */}
      <div style={{
        position: 'absolute', top: 0, left: 0, right: 0,
        padding: '10px 20px',
        borderBottom: '1px solid #1f2937',
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        background: '#030712', zIndex: 10,
      }}>
        <span style={{ fontWeight: 600, fontSize: 14 }}>
          Practice vs {bot.opponent_username}
        </span>
        <a href="/bots" style={{ color: '#6b7280', fontSize: 12, textDecoration: 'none' }}>
          ← Bots
        </a>
      </div>

      {/* Main layout */}
      <div style={{
        flex: 1,
        display: 'flex',
        flexDirection: isMobile ? 'column' : 'row',
        padding: isMobile ? '60px 12px 16px' : '64px 24px 24px',
        gap: isMobile ? 16 : 24,
        overflowY: 'auto',
      }}>

        {/* --- Left/top panel: bot info + controls --- */}
        {isMobile ? (
          // Mobile: info inline above board
          <MobileInfoBar
            bot={bot}
            userColor={userColor}
            colorChoice={colorChoice}
            setColorChoice={setColorChoice}
            onNewGame={handleNewGame}
            thinking={thinking}
            moveSource={moveSource}
            sourceLabel={sourceLabel}
          />
        ) : (
          <SidePanel
            bot={bot}
            userColor={userColor}
            colorChoice={colorChoice}
            setColorChoice={setColorChoice}
            onNewGame={handleNewGame}
            thinking={thinking}
            moveSource={moveSource}
            sourceLabel={sourceLabel}
          />
        )}

        {/* --- Board --- */}
        <div style={{ flexShrink: 0 }}>
          <div style={{ width: boardSize, height: boardSize, position: 'relative' }}>
            <Chessground
              key={resetKey}
              width={boardSize}
              height={boardSize}
              config={cgConfig}
            />
            {thinking && (
              <div style={{
                position: 'absolute', inset: 0,
                background: 'rgba(0,0,0,0.25)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                pointerEvents: 'none', borderRadius: 2,
              }}>
                <span style={{ color: '#9ca3af', fontSize: 13, fontStyle: 'italic' }}>
                  thinking…
                </span>
              </div>
            )}
          </div>

          {/* Game over banner */}
          {gameOver && (
            <div style={{
              marginTop: 10,
              background: '#111827',
              border: '1px solid #374151',
              borderRadius: 8,
              padding: '10px 16px',
              textAlign: 'center',
            }}>
              <div style={{ fontWeight: 600, marginBottom: 4 }}>
                {gameOver === 'checkmate'
                  ? (chess.turn() === (userColor === 'white' ? 'w' : 'b')
                      ? 'You were checkmated'
                      : 'You checkmated the bot!')
                  : gameOver === 'stalemate' ? 'Stalemate — draw'
                  : 'Draw'}
              </div>
              <button
                onClick={handleNewGame}
                style={{
                  background: '#1d4ed8', color: '#fff',
                  border: 'none', borderRadius: 6,
                  padding: '6px 16px', fontSize: 13,
                  cursor: 'pointer', fontWeight: 600,
                }}
              >
                Play Again
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Side panel (desktop)
// ---------------------------------------------------------------------------
function SidePanel({ bot, userColor, colorChoice, setColorChoice, onNewGame, thinking, moveSource, sourceLabel }) {
  return (
    <div style={{
      width: 220, flexShrink: 0,
      display: 'flex', flexDirection: 'column', gap: 12,
    }}>
      {/* Bot info */}
      <div style={{ background: '#111827', borderRadius: 8, padding: '12px 14px' }}>
        <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 4 }}>
          {bot.opponent_username}
        </div>
        <div style={{ color: '#6b7280', fontSize: 12, marginBottom: 8 }}>
          {bot.opponent_platform}
          {bot.opponent_elo ? ` · ${bot.opponent_elo} Elo` : ''}
        </div>
        <div style={{ color: '#6b7280', fontSize: 12 }}>
          Speeds: {bot.speeds}
        </div>
      </div>

      {/* Color picker */}
      <div style={{ background: '#111827', borderRadius: 8, padding: '12px 14px' }}>
        <div style={{ color: '#9ca3af', fontSize: 11, textTransform: 'uppercase',
                      letterSpacing: '0.05em', marginBottom: 8 }}>
          You play as
        </div>
        <div style={{ display: 'flex', gap: 6 }}>
          {[['white', 'White'], ['random', 'Rand'], ['black', 'Black']].map(([val, label]) => (
            <button
              key={val}
              onClick={() => setColorChoice(val)}
              style={{
                flex: 1,
                padding: '5px 0',
                borderRadius: 5,
                border: `1px solid ${colorChoice === val ? '#3b82f6' : '#374151'}`,
                background: colorChoice === val ? '#1e3a5f' : '#1f2937',
                color: colorChoice === val ? '#93c5fd' : '#9ca3af',
                fontSize: 11, fontWeight: 600,
                cursor: 'pointer',
              }}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {/* New game button */}
      <button
        onClick={onNewGame}
        disabled={thinking}
        style={{
          background: '#1d4ed8', color: '#fff',
          border: 'none', borderRadius: 7,
          padding: '8px 0', fontSize: 13, fontWeight: 600,
          cursor: thinking ? 'default' : 'pointer',
          opacity: thinking ? 0.5 : 1,
        }}
      >
        New Game
      </button>

      {/* Last bot move source */}
      {moveSource && sourceLabel[moveSource] && (
        <div style={{
          background: '#111827', borderRadius: 8, padding: '10px 14px',
          display: 'flex', alignItems: 'center', gap: 6,
        }}>
          <span style={{ fontSize: 11, color: '#6b7280' }}>Bot played:</span>
          <span style={{
            fontSize: 11, fontWeight: 700,
            color: sourceLabel[moveSource].color,
          }}>
            {sourceLabel[moveSource].text}
          </span>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Mobile info bar
// ---------------------------------------------------------------------------
function MobileInfoBar({ bot, colorChoice, setColorChoice, onNewGame, thinking, moveSource, sourceLabel }) {
  return (
    <div style={{
      display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'center',
      background: '#111827', borderRadius: 8, padding: '10px 14px',
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
              padding: '4px 10px',
              borderRadius: 5,
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
            padding: '4px 10px', fontSize: 11, fontWeight: 600,
            cursor: thinking ? 'default' : 'pointer',
            opacity: thinking ? 0.5 : 1,
          }}
        >
          New Game
        </button>
      </div>
      {moveSource && sourceLabel[moveSource] && (
        <div style={{ fontSize: 11, color: sourceLabel[moveSource].color, fontWeight: 600 }}>
          Bot: {sourceLabel[moveSource].text}
        </div>
      )}
    </div>
  )
}
