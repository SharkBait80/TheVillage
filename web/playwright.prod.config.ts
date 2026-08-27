import { defineConfig, devices } from '@playwright/test'

/**
 * Playwright config for the PRODUCTION bundle (the artifact CDK deploys).
 *
 * Unlike playwright.config.ts (which builds with VITE_MOCK=1 to exercise the
 * mock backend), this config builds with the default production mode
 * (`npm run build` → VITE_MOCK=0) and serves it. Its sole job is to prove the
 * shipped bundle does NOT run in mock mode — i.e. the "Mode / Mock" HUD chip is
 * absent. This guards against regressions where a mock build gets deployed.
 *
 * The production app auto-logs-in against the live backend; asserting the mock
 * badge is absent only requires the shell to render, so we keep it minimal and
 * do not depend on live network responses.
 */
const PORT = 4318
const BASE_URL = `http://127.0.0.1:${PORT}`

export default defineConfig({
  testDir: './e2e-prod',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: BASE_URL,
    trace: 'retain-on-failure',
    launchOptions: { args: ['--no-sandbox'] },
  },
  projects: [
    {
      name: 'desktop-chromium',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 } },
    },
  ],
  webServer: {
    // Default production build (mode=production → VITE_MOCK=0).
    command: `npm run build && npm run preview -- --host 127.0.0.1 --port ${PORT} --strictPort`,
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 180_000,
  },
})
