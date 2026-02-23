import React from 'react'
import Chessground from '@react-chess/chessground'

const BOARD_SIZE = 480

export default function RepertoireBoard({ node, nodeMap, orientation, onNavigate, onBack, canGoBack }) {
  if (!node) return null

  const config = {
    fen: node.fen,
    orientation,
    lastMove: node.move_orig && node.move_dest ? [node.move_orig, node.move_dest] : undefined,
    movable:  { free: false, color: 'none' },
    draggable: { enabled: false },
    selectable: { enabled: false },
    animation: { enabled: true, duration: 200 },
  }

  const children = node.children || []

  return (
    <div style={{ display: 'flex', gap: 24, alignItems: 'flex-start' }}>

      {/* Left: board + nav */}
      <div style={{ flexShrink: 0 }}>
        <div style={{ width: BOARD_SIZE, height: BOARD_SIZE }}>
          <Chessground width={BOARD_SIZE} height={BOARD_SIZE} config={config} />
        </div>
        <NavBar onBack={onBack} canGoBack={canGoBack} />
      </div>

      {/* Right: move choices + annotation */}
      <div style={{ flex: 1, minWidth: 0 }}>
        <AnnotationPanel node={node} />
        <MovesPanel children={children} nodeMap={nodeMap} onNavigate={onNavigate} />
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// NavBar
// ---------------------------------------------------------------------------
function NavBar({ onBack, canGoBack }) {
  return (
    <div style={{ marginTop: 10, display: 'flex', gap: 6, justifyContent: 'center' }}>
      <button
        onClick={onBack}
        disabled={!canGoBack}
        title="Back (← or ↑)"
        style={{
          background: canGoBack ? '#1f2937' : '#111',
          color: canGoBack ? '#9ca3af' : '#374151',
          border: 'none', borderRadius: 4,
          padding: '5px 16px', fontSize: 13, cursor: canGoBack ? 'pointer' : 'default',
        }}
      >
        ◀ Back
      </button>
      <span style={{ color: '#4b5563', fontSize: 12, alignSelf: 'center' }}>
        {canGoBack ? '' : 'Start position'}
      </span>
    </div>
  )
}

// ---------------------------------------------------------------------------
// AnnotationPanel — shows move info and frequency comment for current node
// ---------------------------------------------------------------------------
function AnnotationPanel({ node }) {
  if (!node.move_san) {
    return (
      <div style={{ background: '#111827', borderRadius: 8, padding: '12px 16px', marginBottom: 14 }}>
        <div style={{ color: '#9ca3af', fontSize: 12 }}>Starting position</div>
        <div style={{ color: '#6b7280', fontSize: 11, marginTop: 4 }}>
          Use ← → arrow keys or click moves below to navigate.
        </div>
      </div>
    )
  }

  return (
    <div style={{ background: '#111827', borderRadius: 8, padding: '12px 16px', marginBottom: 14 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
        <span style={{
          fontFamily: 'monospace', fontSize: 22, fontWeight: 700,
          color: node.is_player_move ? '#fbbf24' : '#9ca3af',
        }}>
          {node.move_san}
        </span>
        <span style={{ fontSize: 11, color: '#6b7280' }}>
          {node.is_player_move ? 'your move' : "opponent's move"}
        </span>
        <span style={{ fontSize: 11, color: '#6b7280' }}>· ply {node.depth}</span>
      </div>
      {node.comment && (
        <div style={{ color: '#9ca3af', fontSize: 12, marginTop: 6, fontFamily: 'monospace' }}>
          {node.comment}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// MovesPanel — shows available continuations from the current node
// ---------------------------------------------------------------------------
function MovesPanel({ children, nodeMap, onNavigate }) {
  if (!children || children.length === 0) {
    return (
      <div style={{ background: '#111827', borderRadius: 8, padding: '12px 16px', color: '#4b5563', fontSize: 12 }}>
        End of line.
      </div>
    )
  }

  // Determine if these are player moves or opponent responses
  const isPlayerTurn = children[0]?.is_player_move

  return (
    <div style={{ background: '#111827', borderRadius: 8, padding: '12px 16px' }}>
      <div style={{
        color: '#6b7280', fontSize: 10, textTransform: 'uppercase',
        letterSpacing: '0.05em', marginBottom: 10,
      }}>
        {isPlayerTurn ? 'Your moves' : "Opponent's responses"}
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {children.map((child, idx) => (
          <MoveOption
            key={child.id}
            node={child}
            isMainline={idx === 0}
            isPlayerMove={child.is_player_move}
            onClick={() => onNavigate(child.id)}
          />
        ))}
      </div>
    </div>
  )
}

function MoveOption({ node, isMainline, isPlayerMove, onClick }) {
  const [hovered, setHovered] = React.useState(false)

  const bgColor   = hovered ? '#1f2937' : '#0f172a'
  const textColor = isPlayerMove
    ? (isMainline ? '#fbbf24' : '#d97706')
    : (isMainline ? '#e5e7eb' : '#9ca3af')
  const badge = isMainline
    ? { text: 'mainline', color: '#1d4ed8', bg: '#1e3a8a' }
    : { text: 'alternative', color: '#374151', bg: '#1f2937' }

  return (
    <div
      onClick={onClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        display: 'flex', alignItems: 'center', gap: 10,
        padding: '8px 12px', borderRadius: 6,
        background: bgColor,
        border: `1px solid ${isMainline ? '#1d4ed8' : '#1f2937'}`,
        cursor: 'pointer', transition: 'background 0.1s',
      }}
    >
      <span style={{ fontFamily: 'monospace', fontSize: 16, fontWeight: isMainline ? 700 : 400, color: textColor, minWidth: 48 }}>
        {node.move_san}
      </span>
      {node.comment && (
        <span style={{ color: '#6b7280', fontSize: 11, fontFamily: 'monospace' }}>
          {node.comment}
        </span>
      )}
      {node.children.length === 0 && (
        <span style={{ marginLeft: 'auto', color: '#374151', fontSize: 10 }}>end</span>
      )}
    </div>
  )
}
