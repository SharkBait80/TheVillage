import { test, expect } from '@playwright/test'

// Live check: the deployed site renders the new "Conversations" section inside
// the agent detail panel (against the real backend, auto-logged-in via the
// operator creds baked into the production bundle).
test('agent detail panel shows a Conversations section on the live site', async ({ page }) => {
  await page.goto('/')
  // Wait for the shell (auto-login completes and the HUD renders).
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
