import React from 'react'

const COL = { color: '#9ca3af', fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.05em' }

function evalColor(cp) {
  if (cp >= 50)  return '#4ade80'  // green
  if (cp >= 10)  return '#fbbf24'  // amber
  if (cp >= -10) return '#f3f4f6'  // neutral
  return '#f87171'                 // red
}

export default function NoveltyTable({ novelties, selected, onSelect }) {
  return (
    <table style={{ width: '100%', borderCollapse: 'collapse' }}>
      <thead>
        <tr style={{ position: 'sticky', top: 0, background: '#030712', zIndex: 1 }}>
          {['#', 'Move', 'Ply', 'Eval', 'Pre', 'Post', 'Score'].map(h => (
            <th key={h} style={{ ...COL, padding: '8px 10px', textAlign: h === '#' ? 'center' : 'left',
                                  borderBottom: '1px solid #1f2937' }}>
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {novelties.map((n, i) => {
          const isSelected = i === selected
          return (
            <tr
              key={n.rank}
              onClick={() => onSelect(i)}
              style={{
                cursor: 'pointer',
                background: isSelected ? '#1c1f2e' : 'transparent',
                borderLeft: isSelected ? '3px solid #f59e0b' : '3px solid transparent',
                transition: 'background 0.1s',
              }}
            >
              <td style={{ padding: '7px 10px', textAlign: 'center', color: '#6b7280', fontSize: 12 }}>
                {n.rank}
              </td>
              <td style={{ padding: '7px 10px', fontWeight: 600, fontFamily: 'monospace' }}>
                {_moveLabel(n)}
              </td>
              <td style={{ padding: '7px 10px', color: '#9ca3af', fontSize: 12 }}>
                {n.book_moves_san.length + 1}
              </td>
              <td style={{ padding: '7px 10px', fontFamily: 'monospace', fontSize: 12,
                            color: evalColor(n.eval_cp) }}>
                {n.eval_cp >= 0 ? '+' : ''}{n.eval_cp.toFixed(0)}
              </td>
              <td style={{ padding: '7px 10px', color: '#9ca3af', fontSize: 12 }}>
                {n.pre_novelty_games.toLocaleString()}
              </td>
              <td style={{ padding: '7px 10px', fontSize: 12,
                            color: n.post_novelty_games === 0 ? '#60a5fa' : '#9ca3af' }}>
                {n.post_novelty_games === 0 ? 'TN' : n.post_novelty_games}
              </td>
              <td style={{ padding: '7px 10px', color: '#fbbf24', fontSize: 12 }}>
                {n.score.toFixed(1)}
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

function _moveLabel(n) {
  const ply = n.book_moves_san.length  // 0-indexed ply of the novelty
  const moveNum = Math.floor(ply / 2) + 1
  const dots = ply % 2 === 1 ? '…' : '.'
  return `${moveNum}${dots}${n.novelty_san}`
}
