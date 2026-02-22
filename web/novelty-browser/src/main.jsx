import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'
import './chessground.base.css'
import './chessground.brown.css'
import './chessground.cburnett.css'
import './index.css'

const rootEl = document.getElementById('root')
const jobId  = rootEl.dataset.jobId
const side   = rootEl.dataset.side || 'white'

createRoot(rootEl).render(<App jobId={jobId} side={side} />)
