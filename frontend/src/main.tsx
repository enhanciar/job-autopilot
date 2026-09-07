import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import './index.css'
import App from './App'
import Overview from './pages/Overview'
import Jobs from './pages/Jobs'
import Review from './pages/Review'
import Applications from './pages/Applications'
import OutreachPage from './pages/Outreach'
import Platforms from './pages/Platforms'
import RunSystem from './pages/RunSystem'
import Runs from './pages/Runs'
import Settings from './pages/Settings'
import Profile from './pages/Profile'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route element={<App />}>
          <Route index element={<Navigate to="/overview" replace />} />
          <Route path="overview" element={<Overview />} />
          <Route path="jobs" element={<Jobs />} />
          <Route path="review" element={<Review />} />
          <Route path="applications" element={<Applications />} />
          <Route path="outreach" element={<OutreachPage />} />
          <Route path="run" element={<RunSystem />} />
          <Route path="platforms" element={<Platforms />} />
          <Route path="runs" element={<Runs />} />
          <Route path="settings" element={<Settings />} />
          <Route path="profile" element={<Profile />} />
        </Route>
      </Routes>
    </BrowserRouter>
  </StrictMode>,
)
