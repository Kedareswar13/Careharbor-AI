import React, { useEffect, useState } from 'react'
import { LayoutDashboard, MessageSquareText, Moon, Phone, Sun } from 'lucide-react'
import { getJSON } from './api.js'
import { BRAND, POSITIONING, TAGLINE } from './brand.js'
import Workspace from './Chat.jsx'
import Staff from './Staff.jsx'

const route = () => (window.location.hash.startsWith('#/staff') ? 'staff' : 'chat')

export default function App() {
  const [page, setPage] = useState(route())
  const [theme, setTheme] = useState(() => localStorage.getItem('careharbor_theme') || 'light')
  const [config, setConfig] = useState(null)
  const [health, setHealth] = useState(null)
  const [verified, setVerified] = useState(null)

  useEffect(() => { const f = () => setPage(route()); window.addEventListener('hashchange', f); return () => window.removeEventListener('hashchange', f) }, [])
  useEffect(() => { document.documentElement.dataset.theme = theme; localStorage.setItem('careharbor_theme', theme) }, [theme])
  useEffect(() => {
    getJSON('/api/config').then(setConfig).catch(() => {})
    const ping = () => getJSON('/api/health').then(setHealth).catch(() => setHealth(null))
    ping()
    const t = setInterval(ping, 20000)
    return () => clearInterval(t)
  }, [])

  return (
    <div className="app">
      <header className="header">
        <div className="logo">
          <span className="logo-mark" aria-hidden="true">
            <svg width="18" height="18" viewBox="0 0 24 24"><path d="M9 3h6v6h6v6h-6v6H9v-6H3V9h6z" fill="#fff" /></svg>
          </span>
          <span className="logo-text" title={`${POSITIONING} (${config?.clinic || 'Sunrise Family Clinic'})`}><b>{BRAND}</b><span>{TAGLINE.charAt(0).toUpperCase() + TAGLINE.slice(1)}</span></span>
        </div>
        <nav className="tabs">
          <a href="#/" className={`tab ${page === 'chat' ? 'active' : ''}`}><MessageSquareText size={16} /> Patient assistant</a>
          <a href="#/staff" className={`tab ${page === 'staff' ? 'active' : ''}`}><LayoutDashboard size={16} /> Staff console</a>
        </nav>
        <span className="spacer" />
        <span className="hchip" title={health ? `${health.documents} documents · tenant ${health.tenant} · retrieval ${health.rag_mode}` : 'The API is not reachable'}>
          <span className={`dot ${health ? '' : 'off'}`} />{health ? `Online · ${health.model}` : 'Offline'}
        </span>
        <button className="hbtn" onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')} title="Switch colour theme">
          {theme === 'light' ? <Moon size={15} /> : <Sun size={15} />} {theme === 'light' ? 'Dark' : 'Light'}
        </button>
      </header>
      <div className="notice" role="note">
        <Phone size={14} /> <span>Demo with synthetic data, not medical advice. In an emergency call <a href="tel:112">112</a> or <a href="tel:108">108</a>.</span>
      </div>
      {page === 'chat' ? <Workspace config={config} health={health} verified={verified} onVerified={setVerified} /> : <Staff />}
    </div>
  )
}
