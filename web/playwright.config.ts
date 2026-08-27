import { defineConfig, devices } from '@playwright/test'

/**
 * Playwright e2e config for the Melbourne Agent Village SPA.
 *
 * The app is exercised against its built-in mock backend (VITE_MOCK=1) so no
 * AWS/API/auth is required. We build the production bundle and serve it with
 * `vite preview`; the webServer block builds+serves automatically.
 *
 * Two projects run every spec: a desktop viewport and a mobile (Pixel-5-sized)
 * viewport, so the responsive layout is verified on both.
 */
const PORT = 4317
const BASE_URL = `http://127.0.0.1:${PORT}`

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: BASE_URL,
    trace: 'retain-on-failure',
    // Sandbox is unavailable as root in this container.
    launchOptions: { args: ['--no-sandbox'] },
  },
  projects: [
    {
      name: 'desktop-chromium',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 } },
    },
    {
      name: 'mobile-chromium',
      use: { ...devices['Pixel 5'] },
    },
  ],
  webServer: {
    command: `npm run build:mock && npm run preview -- --host 127.0.0.1 --port ${PORT} --strictPort`,
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
})
