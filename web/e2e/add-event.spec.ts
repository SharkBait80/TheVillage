import { test, expect } from '@playwright/test'
import type { Page } from '@playwright/test'

// In mock mode (VITE_MOCK=1) the login screen is skipped and the app mounts
// directly against the built-in fake backend. mockBackend.createEvent validates
// bounds/lengths and rejects descriptions containing implausible marker words
// (e.g. "dragon"), so these tests exercise both the success and rejection paths
// with no auth or network. Runs on both desktop and mobile projects.

async function gotoApp(page: Page) {
  await page.goto('/')
  await expect(page.getByText('Melbourne Agent Village').first()).toBeVisible()
}

/** Click an in-bounds Melbourne point on the Leaflet map, avoiding the zoom
 *  control (top-left) and the dense marker cluster near the exact centre. The
 *  location markers sit around the map centre, so we click low on the map
 *  (further south, still inside the Melbourne bounds) where the tiles are
 *  empty on both the desktop and (smaller) mobile viewports. */
async function clickMapCentre(page: Page) {
  const map = page.locator('.leaflet-container')
  await expect(map).toBeVisible()
  const box = await map.boundingBox()
  expect(box).not.toBeNull()
  await page.mouse.click(box!.x + box!.width * 0.5, box!.y + box!.height * 0.82)
}

test.describe('add event', () => {
  test('Add event button is visible in the HUD', async ({ page }) => {
    await gotoApp(page)
    await expect(page.getByRole('button', { name: 'Add event' })).toBeVisible()
  })

  test('clicking the button arms add-event mode', async ({ page }) => {
    await gotoApp(page)
    const btn = page.getByRole('button', { name: 'Add event' })
    await expect(btn).toHaveAttribute('aria-pressed', 'false')
    await btn.click()
    await expect(btn).toHaveAttribute('aria-pressed', 'true')
    // The hint appears while armed.
    await expect(page.getByText('Click the map to place an event')).toBeVisible()
  })

  test('clicking the map opens the AddEventModal dialog', async ({ page }) => {
    await gotoApp(page)
    await page.getByRole('button', { name: 'Add event' }).click()
    await clickMapCentre(page)
    const dialog = page.getByRole('dialog', { name: /add event/i })
    await expect(dialog).toBeVisible()
    // Mode disarms once the modal opens (header button, scoped to the banner).
    await expect(
      page.getByRole('banner').getByRole('button', { name: 'Add event' }),
    ).toHaveAttribute('aria-pressed', 'false')
  })

  test('filling in and submitting a valid event shows a success confirmation', async ({ page }) => {
    await gotoApp(page)
    await page.getByRole('button', { name: 'Add event' }).click()
    await clickMapCentre(page)
    const dialog = page.getByRole('dialog', { name: /add event/i })
    await expect(dialog).toBeVisible()

    await dialog.getByLabel('Title').fill('Street festival on Swanston')
    await dialog.getByLabel('Description').fill('A lively pop-up market with food stalls and music.')
    await dialog.getByRole('button', { name: 'Add event', exact: true }).click()

    // Success confirmation appears, then the dialog auto-closes.
    await expect(dialog.getByText(/Event (accepted|recorded)/i)).toBeVisible()
    await expect(dialog).toBeHidden({ timeout: 5000 })
  })

  test('an obviously-rejected description surfaces the rejection message', async ({ page }) => {
    await gotoApp(page)
    await page.getByRole('button', { name: 'Add event' }).click()
    await clickMapCentre(page)
    const dialog = page.getByRole('dialog', { name: /add event/i })
    await expect(dialog).toBeVisible()

    await dialog.getByLabel('Title').fill('Something strange')
    await dialog.getByLabel('Description').fill('A giant dragon lands on Flinders Street.')
    await dialog.getByRole('button', { name: 'Add event', exact: true }).click()

    // The mock rejects implausible content; the message is shown inline.
    await expect(dialog.getByRole('alert')).toContainText(/rejected/i)
    // Dialog stays open so the operator can amend and retry.
    await expect(dialog).toBeVisible()
  })

  test('Escape closes the dialog', async ({ page }) => {
    await gotoApp(page)
    await page.getByRole('button', { name: 'Add event' }).click()
    await clickMapCentre(page)
    const dialog = page.getByRole('dialog', { name: /add event/i })
    await expect(dialog).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(dialog).toBeHidden()
  })
})
