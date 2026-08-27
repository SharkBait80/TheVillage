import { test, expect } from '@playwright/test'

// Smoke test against the live deployed site (PROD_URL). Confirms the mock badge
// is not present on the real CloudFront-served bundle.
test('live deployed site does not show the mock badge', async ({ page }) => {
  await page.goto('/')
  await page.waitForLoadState('domcontentloaded')
  await expect(page.getByLabel('Running in mock mode')).toHaveCount(0)
  await expect(page.locator('.hud-chip', { hasText: 'Mock' })).toHaveCount(0)
})
