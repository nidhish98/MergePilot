import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/run': 'http://localhost:8000',
      '/stream': {
        target: 'http://localhost:8000',
        ws: false,
      },
    },
  },
})
