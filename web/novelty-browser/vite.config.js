import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  // Source lives in novelty-browser/, build output goes to web/static/dist/
  build: {
    outDir: '../static/dist',
    emptyOutDir: true,
  },
  // Flask serves static files from /static/dist/
  base: '/static/dist/',
})
