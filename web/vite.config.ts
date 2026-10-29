import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// 개발 중: 화면은 Vite (127.0.0.1:5173), /api · /auth 는 서버 (python -m lefsite, 127.0.0.1:8000) 로 넘긴다
const server = 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  server: { host: '127.0.0.1', port: 5173, strictPort: true, proxy: { '/api': server, '/auth': server } },
  build: { outDir: 'dist', sourcemap: false },
  test: { environment: 'node', include: ['src/**/*.test.ts'] },
})
