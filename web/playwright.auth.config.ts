import { defineConfig, devices } from '@playwright/test'

/**
 * Playwright config for the AUTH e2e suite (change-password + session
 * persistence). Unlike the main mock config, this builds a NON-mock bundle so
 * the real Cognito auth path in src/auth.ts is exercised. Cognito network calls
 * (cognito-idp.*.amazonaws.com) and the Simulation API are stubbed per-test via
 * page.route(), so no AWS backend is required.
 *
 * Test Cognito config is injected at build time via .env.auth-e2e (loaded by
 * Vite for the "auth-e2e" mode). VITE_MOCK stays 0 (see vite.config.ts), so the
 * login screen / auto-login path runs.
 */
const PORT = 4319
const BASE_URL = `http://127.0.0.1:${PORT}`

export default defineConfig({
  testDir: './e2e-auth',
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
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
    command: `npm run build:auth-e2e && npm run preview -- --host 127.0.0.1 --port ${PORT} --strictPort`,
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
})
