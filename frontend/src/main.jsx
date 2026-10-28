import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'
import './styles.css'

// Remove browser storage keys left by earlier versions (they used a short "cp_" prefix).
for (const key of ['cp_session_v2', 'cp_conversations', 'cp_theme_v2', 'cp_panel', 'cp_model']) localStorage.removeItem(key)

createRoot(document.getElementById('root')).render(<App />)
