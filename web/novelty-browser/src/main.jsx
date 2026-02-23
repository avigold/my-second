import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'
import HabitsApp from './HabitsApp.jsx'
import './chessground.base.css'
import './chessground.brown.css'
import './chessground.cburnett.css'
import './index.css'

const rootEl = document.getElementById('root')
const jobId  = rootEl.dataset.jobId
const side   = rootEl.dataset.side || 'white'
const mode   = rootEl.dataset.mode || 'novelties'

createRoot(rootEl).render(
  mode === 'habits'
    ? <HabitsApp jobId={jobId} side={side} />
    : <App       jobId={jobId} side={side} />
)
