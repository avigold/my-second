import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'
import HabitsApp from './HabitsApp.jsx'
import HabitsPracticeApp from './HabitsPracticeApp.jsx'
import RepertoireApp from './RepertoireApp.jsx'
import StrategiseApp from './StrategiseApp.jsx'
import './chessground.base.css'
import './chessground.brown.css'
import './chessground.cburnett.css'
import './index.css'

const rootEl = document.getElementById('root')
const jobId  = rootEl.dataset.jobId
const side   = rootEl.dataset.side || 'white'
const mode   = rootEl.dataset.mode || 'novelties'

const apps = {
  'novelties':       <App                jobId={jobId} side={side} />,
  'habits':          <HabitsApp          jobId={jobId} side={side} />,
  'habits-practice': <HabitsPracticeApp  jobId={jobId} side={side} />,
  'repertoire':      <RepertoireApp      jobId={jobId} side={side} />,
  'strategise':      <StrategiseApp      jobId={jobId} side={side} />,
}

createRoot(rootEl).render(apps[mode] ?? apps['novelties'])
