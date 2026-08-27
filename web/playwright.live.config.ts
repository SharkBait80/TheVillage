import { defineConfig, devices } from '@playwright/test'

// One-off config to smoke-test the LIVE deployed CloudFront site (set via
// PROD_URL) — confirms the shipped site does not show the mock badge.
export default defineConfig({
  testDir: './e2e-live',
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: [['list']],
  use: {
    baseURL: process.env.PROD_URL,
    trace: 'off',
    launchOptions: { args: ['--no-sandbox'] },
  },
  projects: [
    { name: 'desktop-chromium', use: { ...devices['Desktop Chrome'] } },
  ],
})
