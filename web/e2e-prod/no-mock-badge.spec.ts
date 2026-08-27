import { test, expect } from '@playwright/test'

// Verifies the PRODUCTION bundle (VITE_MOCK=0) does not render the mock badge.
// This is the regression guard for the "why is there still 'Mode Mock'?" bug:
// the deployed artifact must never advertise mock mode.
//
// In production the app may attempt an auto-login / live API calls, so we block
// network to keep the test hermetic — the HUD shell still renders regardless.

test.describe('production bundle', () => {
  test('does NOT show the "Mode / Mock" badge', async ({ page }) => {
    // Block outbound API/auth calls so the test does not depend on a live
    // backend; the SPA shell (and thus the absence of the mock chip) renders
    // from static assets alone.
    await page.route('**/cognito-idp.*.amazonaws.com/**', (r) => r.abort())
    await page.route('**/v1/**', (r) => r.abort())

    await page.goto('/')

    // The app shell mounts either at the HUD (if auto-login/mock) or the login
    // screen (live, no creds). Either way, the mock badge must be absent.
    await page.waitForLoadState('domcontentloaded')

    // The mock chip is <span aria-label="Running in mock mode">.
    await expect(page.getByLabel('Running in mock mode')).toHaveCount(0)

    // Belt-and-suspenders: the literal "Mode"/"Mock" HUD chip pairing must not
    // be present.
    const modeChip = page.locator('.hud-chip', { hasText: 'Mock' })
    await expect(modeChip).toHaveCount(0)
  })
})
