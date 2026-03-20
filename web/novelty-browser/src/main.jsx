import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'
import HabitsApp from './HabitsApp.jsx'
import HabitsPracticeApp from './HabitsPracticeApp.jsx'
import RepertoireApp from './RepertoireApp.jsx'
import StrategiseApp from './StrategiseApp.jsx'
import GameAnalysisApp from './GameAnalysisApp.jsx'
import BotPracticeApp from './BotPracticeApp.jsx'
import PlayerProfileApp from './PlayerProfileApp.jsx'
import './chessground.base.css'
import './chessground.brown.css'
import './chessground.cburnett.css'
import './index.css'

const rootEl = document.getElementById('root')
const jobId  = rootEl.dataset.jobId
const side   = rootEl.dataset.side || 'white'
const mode   = rootEl.dataset.mode || 'novelties'
const botId  = rootEl.dataset.botId

const apps = {
  'novelties':       <App                jobId={jobId} side={side} />,
  'habits':          <HabitsApp          jobId={jobId} side={side} />,
  'habits-practice': <HabitsPracticeApp  jobId={jobId} side={side} />,
  'repertoire':      <RepertoireApp      jobId={jobId} side={side} />,
  'strategise':      <StrategiseApp      jobId={jobId} side={side} />,
  'game-analysis':   <GameAnalysisApp    jobId={jobId} side={side} />,
  'bot-practice':    <BotPracticeApp     botId={botId} />,
  'player-practice': <PlayerProfileApp
    slug={rootEl.dataset.slug}
    displayName={rootEl.dataset.displayName}
    elo={rootEl.dataset.elo ? parseInt(rootEl.dataset.elo) : null}
    title={rootEl.dataset.title || null}
    loggedIn={rootEl.dataset.loggedIn || 'false'}
    description={rootEl.dataset.description || ''}
    username={rootEl.dataset.username || ''}
    platform={rootEl.dataset.platform || ''}
    photoPosition={rootEl.dataset.photoPosition ? parseInt(rootEl.dataset.photoPosition) : 25}
  />,
}

createRoot(rootEl).render(apps[mode] ?? apps['novelties'])
