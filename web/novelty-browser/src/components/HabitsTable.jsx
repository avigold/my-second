import React from 'react'

const COL = { color: '#9ca3af', fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.05em' }

function gapColor(gap) {
  if (gap >= 75) return '#f87171'  // red — mistake
  if (gap >= 25) return '#fbbf24'  // amber — inaccuracy
  return '#9ca3af'
}

function nagLabel(gap) {
  return gap >= 75 ? '?' : '?!'
}

export default function HabitsTable({ habits, selected, onSelect }) {
  return (
    <table style={{ width: '100%', borderCollapse: 'collapse' }}>
      <thead>
        <tr style={{ position: 'sticky', top: 0, background: '#030712', zIndex: 1 }}>
          {['#', 'Player', 'Best', 'Gap', 'Freq', 'Score'].map(h => (
            <th key={h} style={{
              ...COL,
              padding: '8px 10px',
              textAlign: h === '#' ? 'center' : 'left',
              borderBottom: '1px solid #1f2937',
            }}>
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {habits.map((h, i) => {
          const isSelected = i === selected
          const nag = nagLabel(h.eval_gap_cp)
          return (
            <tr
              key={h.rank}
              onClick={() => onSelect(i)}
              style={{
                cursor: 'pointer',
                background: isSelected ? '#1c1f2e' : 'transparent',
                borderLeft: isSelected ? '3px solid #f59e0b' : '3px solid transparent',
                transition: 'background 0.1s',
              }}
            >
              <td style={{ padding: '7px 10px', textAlign: 'center', color: '#6b7280', fontSize: 12 }}>
                {h.rank}
              </td>
              <td style={{ padding: '7px 10px', fontWeight: 600, fontFamily: 'monospace', color: '#f87171' }}>
                {h.player_move_san}{nag}
              </td>
              <td style={{ padding: '7px 10px', fontFamily: 'monospace', color: '#4ade80' }}>
                {h.best_move_san}
              </td>
              <td style={{ padding: '7px 10px', fontFamily: 'monospace', fontSize: 12,
                            color: gapColor(h.eval_gap_cp) }}>
                {h.eval_gap_cp >= 0 ? '+' : ''}{h.eval_gap_cp.toFixed(0)}
              </td>
              <td style={{ padding: '7px 10px', color: '#9ca3af', fontSize: 12 }}>
                {h.total_games}×
              </td>
              <td style={{ padding: '7px 10px', color: '#fbbf24', fontSize: 12 }}>
                {h.score.toFixed(1)}
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}
