import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
//
// Mock mode is derived strictly from the build mode, not from ambient env
// files. Only an explicit `--mode mock` build (npm run build:mock) enables the
// mock backend; every other build (including the default `vite build`, which
// runs in "production" mode) forces VITE_MOCK=0. This makes it impossible to
// accidentally ship the mock badge/backend via a stray .env / .env.local /
// exported VITE_MOCK, because the value is pinned at compile time here.
export default defineConfig(({ mode }) => ({
  plugins: [react()],
  base: './',
  define: {
    'import.meta.env.VITE_MOCK': JSON.stringify(mode === 'mock' ? '1' : '0'),
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
}))
