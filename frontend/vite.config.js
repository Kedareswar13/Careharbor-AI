import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// run.ps1 passes the chosen ports (-ApiPort / -UiPort) through these variables.
const apiPort = Number(process.env.API_PORT) || 8010
const uiPort = Number(process.env.UI_PORT) || 5174

export default defineConfig({
  plugins: [react()],
  server: {
    port: uiPort,
    strictPort: true,
    proxy: { '/api': { target: `http://127.0.0.1:${apiPort}`, changeOrigin: true } },
  },
  build: { outDir: 'dist', chunkSizeWarningLimit: 900 },
})