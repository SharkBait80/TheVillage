import { test, expect } from '@playwright/test'
import type { Page } from '@playwright/test'

// In mock mode (VITE_MOCK=1) the login screen is skipped and the app mounts
// directly against the built-in fake backend, so these tests need no auth or
// network. They run on both the desktop and mobile projects defined in
// playwright.config.ts.

async function gotoApp(page: Page) {
  await page.goto('/')
  // Brand mark in the HUD confirms the shell mounted.
  await expect(page.getByText('Melbourne Agent Village').first()).toBeVisible()
}

test.describe('core UI', () => {
  test('renders HUD, brand, status and simulation controls', async ({ page }) => {
    await gotoApp(page)

    // HUD chips.
    await expect(page.getByText('Simulated Time')).toBeVisible()
    await expect(page.getByText('Acceleration')).toBeVisible()

    // Control buttons exist (validity/enabled-state depends on sim status).
    const controls = page.getByRole('group', { name: 'Simulation controls' })
    await expect(controls.getByRole('button', { name: 'Start' })).toBeVisible()
    await expect(controls.getByRole('button', { name: 'Pause' })).toBeVisible()
    await expect(controls.getByRole('button', { name: 'Resume' })).toBeVisible()
    await expect(controls.getByRole('button', { name: 'Stop' })).toBeVisible()

    // Mock-mode badge.
    await expect(page.getByLabel('Running in mock mode')).toBeVisible()
  })

  test('map container is present and fills the main area', async ({ page }) => {
    await gotoApp(page)
    const map = page.locator('.leaflet-container')
    await expect(map).toBeVisible()
    const box = await map.boundingBox()
    expect(box).not.toBeNull()
    expect(box!.width).toBeGreaterThan(200)
    expect(box!.height).toBeGreaterThan(200)
  })

  test('list view toggle opens a text-equivalent table and closes again', async ({ page }) => {
    await gotoApp(page)

    await page.getByRole('button', { name: 'List view' }).click()
    const list = page.getByRole('region', { name: /Agent list/i })
    await expect(list).toBeVisible()
    await expect(list.getByRole('table')).toBeVisible()

    // Toggle label flips to "Map view" and closing hides the list.
    await page.getByRole('button', { name: 'Map view' }).click()
    await expect(list).toBeHidden()
  })

  test('conversations panel toggles open', async ({ page }) => {
    await gotoApp(page)
    await page.getByRole('button', { name: 'Conversations' }).click()
    const dialog = page.getByRole('dialog', { name: 'Conversations' })
    await expect(dialog).toBeVisible()
    await page.getByRole('button', { name: 'Close chats' }).click()
    await expect(dialog).toBeHidden()
  })

  test('no decorative emoji in the brand mark', async ({ page }) => {
    await gotoApp(page)
    const brand = page.locator('.brand')
    const text = (await brand.textContent()) ?? ''
    // The redesign replaced the emoji brand with a plain monogram + name.
    expect(text).not.toMatch(/[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2728}]/u)
    expect(text).toContain('Melbourne Agent Village')
  })
})

test.describe('responsive layout', () => {
  test('no horizontal overflow at the current viewport', async ({ page }) => {
    await gotoApp(page)
    const overflow = await page.evaluate(() => {
      const de = document.documentElement
      return de.scrollWidth - de.clientWidth
    })
    // Allow 1px for sub-pixel rounding.
    expect(overflow).toBeLessThanOrEqual(1)
  })

  test('list view stays within the viewport width', async ({ page }) => {
    await gotoApp(page)
    await page.getByRole('button', { name: 'List view' }).click()
    const list = page.getByRole('region', { name: /Agent list/i })
    await expect(list).toBeVisible()
    const box = await list.boundingBox()
    const vw = page.viewportSize()!.width
    expect(box).not.toBeNull()
    expect(box!.x).toBeGreaterThanOrEqual(-1)
    expect(box!.width).toBeLessThanOrEqual(vw + 1)
  })
})

test.describe('mobile bottom sheet', () => {
  test.skip(({ viewport }) => !viewport || viewport.width > 640, 'mobile-only behaviour')

  test('list view docks to the bottom of the screen on phones', async ({ page }) => {
    await gotoApp(page)
    await page.getByRole('button', { name: 'List view' }).click()
    const list = page.getByRole('region', { name: /Agent list/i })
    await expect(list).toBeVisible()
    const box = await list.boundingBox()
    const vp = page.viewportSize()!
    expect(box).not.toBeNull()
    // Full-width and anchored to the bottom edge (within tolerance).
    expect(box!.width).toBeGreaterThanOrEqual(vp.width - 2)
    expect(box!.y + box!.height).toBeGreaterThanOrEqual(vp.height - 2)
  })
})
