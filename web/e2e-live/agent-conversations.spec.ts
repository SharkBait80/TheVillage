import { test, expect } from '@playwright/test'

// Live check: the deployed site renders the new "Conversations" section inside
// the agent detail panel (against the real backend).
//
// SECURITY: operator credentials are NO LONGER baked into the production
// bundle. The deployed SPA now shows an interactive LoginScreen, so this test
// signs in through that form using credentials supplied via environment
// variables (E2E_OPERATOR_USER / E2E_OPERATOR_PASS). It never hardcodes a
// password. When those env vars are absent the test is skipped rather than
// failing, so CI without secrets stays green.
const E2E_USER = process.env.E2E_OPERATOR_USER
const E2E_PASS = process.env.E2E_OPERATOR_PASS

test('agent detail panel shows a Conversations section on the live site', async ({ page }) => {
  test.skip(!E2E_USER || !E2E_PASS, 'E2E_OPERATOR_USER / E2E_OPERATOR_PASS not set')

  await page.goto('/')
  await page.waitForLoadState('domcontentloaded')

  // Interactive sign-in via the LoginScreen (no baked-in creds anymore).
  const usernameField = page.getByLabel('Username')
  await expect(usernameField).toBeVisible({ timeout: 30_000 })
  await usernameField.fill(E2E_USER as string)
  await page.getByLabel('Password').fill(E2E_PASS as string)
  await page.getByRole('button', { name: /sign in/i }).click()

  // Wait for the shell (login completes and the HUD renders).
  await expect(page.getByText('Melbourne Agent Village').first()).toBeVisible({ timeout: 30_000 })

  // Open the list view and select the first agent.
  await page.getByRole('button', { name: 'List view' }).click()
  const list = page.getByRole('region', { name: /Agent list/i })
  await expect(list).toBeVisible({ timeout: 20_000 })
  const firstAgentBtn = list.locator('tbody tr th button').first()
  await expect(firstAgentBtn).toBeVisible({ timeout: 20_000 })
  await firstAgentBtn.click()

  const panel = page.getByRole('dialog', { name: 'Agent details' })
  await expect(panel).toBeVisible()

  // The new section header must be present (content depends on live data).
  await expect(panel.getByText('Conversations', { exact: true })).toBeVisible({ timeout: 15_000 })
})
