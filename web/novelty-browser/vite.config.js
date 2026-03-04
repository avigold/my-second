import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { copyFileSync, mkdirSync } from 'fs'
import { resolve } from 'path'

export default defineConfig({
  plugins: [
    react(),
    {
      name: 'copy-stockfish',
      closeBundle() {
        // Copy Stockfish WASM worker files to web/static/ so the browser
        // can load them as Web Workers (outside the Vite bundle).
        const staticDir = resolve('../static')
        mkdirSync(staticDir, { recursive: true })
        // Use the single-threaded lite variant — doesn't require SharedArrayBuffer.
        const variants = [
          ['stockfish-18-lite-single.js',   'stockfish.js'],
          ['stockfish-18-lite-single.wasm', 'stockfish.wasm'],
        ]
        for (const [src, dst] of variants) {
          try {
            copyFileSync(
              resolve(`node_modules/stockfish/bin/${src}`),
              resolve(`../static/${dst}`)
            )
          } catch (_) {}
        }
      }
    }
  ],
  // Source lives in novelty-browser/, build output goes to web/static/dist/
  build: {
    outDir: '../static/dist',
    emptyOutDir: true,
  },
  // Flask serves static files from /static/dist/
  base: '/static/dist/',
})
